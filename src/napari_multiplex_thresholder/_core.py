"""Everything that does not need Qt: file discovery, quantification, thresholds.

Kept free of napari and Qt imports so it can be tested headless and reasoned
about on its own. `_widget.py` is the only place that knows about the GUI.

Design notes that matter for compatibility with the rest of project 7904:

* Threshold CSV layout is fixed by `6_statistics_manual_gating.ipynb`, which does
  `pd.read_csv(path, index_col=0)` and then iterates `thresholds.columns` as tile
  stems and `thresholds.index` as marker names, comparing
  `np.arcsinh(quant[marker]) >= thr`. Rows are markers, columns are tiles, the
  index has no name, and nothing else may appear in the file — a `#` comment line
  would be read as data. Provenance therefore goes in a sidecar `.meta.json`.
* An ungated (tile, channel) is **NaN**, not 0. `NaN >= x` is False, so notebook 6
  calls every cell negative for that marker instead of silently positive, which is
  the BUG-04 failure mode reversed to the safe direction. `unset_pairs()` exists so
  the UI can say what is still missing.
* Marker `m` (0-based, DAPI excluded) lives in plane `m + 1` of the combined mask;
  plane 0 is the empty DAPI slot. `plane_for_channel` detects the alternative
  layout rather than assuming.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import tifffile

# macOS AppleDouble sidecars: on the exFAT drives this project uses they sit next
# to the real files and carry the same extension.
APPLEDOUBLE = "._"

MASK_SUFFIX = "_entire_mask.tif"
QUANT_SUFFIX = "_quant.csv"

#: Cofactor that keeps this widget's numbers comparable with notebooks 5 and 6.
NOTEBOOK_COFACTOR = 1.0


def _natsorted(names):
    from natsort import natsorted

    return natsorted(names)


def list_files(folder: str | os.PathLike, suffix: str) -> list[str]:
    """Names in `folder` ending with `suffix`, natsorted, AppleDouble skipped."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return [
        f
        for f in _natsorted(os.listdir(folder))
        if not f.startswith(APPLEDOUBLE) and f.endswith(suffix)
    ]


@dataclass(frozen=True)
class TileRef:
    """One tile with the three files it needs, matched on the filename stem."""

    stem: str
    tile: Path
    quant: Path
    mask: Path


def discover_tiles(tiles_dir, quant_dir, mask_dir) -> tuple[list[TileRef], list[str]]:
    """Tiles that have all three files, plus a human-readable list of what's missing.

    Matching is by stem, never by position in two directory listings — that is
    BUG-13 in CLAUDE.md, where one stray file silently shifts every mask against
    its image.
    """
    tiles_dir, quant_dir, mask_dir = Path(tiles_dir), Path(quant_dir), Path(mask_dir)
    found: list[TileRef] = []
    problems: list[str] = []

    for name in list_files(tiles_dir, ".tif"):
        stem = name[: -len(".tif")]
        quant = quant_dir / f"{stem}{QUANT_SUFFIX}"
        mask = mask_dir / f"{stem}{MASK_SUFFIX}"
        missing = [str(p.name) for p in (quant, mask) if not p.exists()]
        if missing:
            problems.append(f"{stem}: no {', '.join(missing)}")
            continue
        found.append(TileRef(stem, tiles_dir / name, quant, mask))

    return found, problems


@dataclass
class Quantification:
    """Per-cell mean intensity for one tile: labels, channel names, values.

    `values` is (n_cells, n_channels) and keeps NaN, which means "this cell has no
    pixels in the compartment this marker is measured in" (BUG-05). NaN never
    passes `>= threshold`, so such cells are simply never called positive.
    """

    stem: str
    labels: np.ndarray  # int64, the global (offset) label IDs
    channels: list[str]
    values: np.ndarray  # float64 (n_cells, n_channels), NaN preserved

    @property
    def n_cells(self) -> int:
        return int(self.labels.size)

    def column(self, channel: str) -> np.ndarray:
        return self.values[:, self.channels.index(channel)]

    def nan_count(self, channel: str) -> int:
        return int(np.isnan(self.column(channel)).sum())


def load_quantification(quant_path, stem: str | None = None) -> Quantification:
    """Read a `*_quant.csv`. Channel order in the file is the mask plane order."""
    quant_path = Path(quant_path)
    df = pd.read_csv(quant_path)
    # Legacy CSVs written with index=True carry an unnamed index column.
    df = df.loc[:, [c for c in df.columns if not c.startswith("Unnamed:")]]
    if "label" not in df.columns:
        raise ValueError(f"{quant_path.name} has no 'label' column")

    labels = df["label"].to_numpy(dtype=np.int64)
    channels = [c for c in df.columns if c != "label"]
    values = df[channels].to_numpy(dtype=np.float64)
    return Quantification(
        stem or quant_path.name.replace(QUANT_SUFFIX, ""), labels, channels, values
    )


def transform(values: np.ndarray, cofactor: float = NOTEBOOK_COFACTOR) -> np.ndarray:
    """`arcsinh(x / cofactor)`.

    cofactor 1 reproduces notebooks 5 and 6 (`np.arcsinh(quant)`), which is what
    every saved threshold in `data/new/manual_thresholds_*.csv` is expressed in.
    cofactor 5 matches `*asinh_normalised.h5ad` and therefore ASTIR. The two are
    not interchangeable; whichever is used is recorded in the sidecar.
    """
    if cofactor <= 0:
        raise ValueError(f"cofactor must be > 0, got {cofactor}")
    return np.arcsinh(values / cofactor)


def channel_range(transformed: np.ndarray) -> tuple[float, float]:
    """(min, max) of one channel, NaN-safe, for the slider bounds.

    Per channel and per tile on purpose. Notebook 5 used one global maximum taken
    from a frame that still contained the melted-in `n_cells` column, so its slider
    topped out at `arcsinh(cell count)` — BUG-19, which can put the correct
    threshold out of reach entirely.
    """
    if transformed.size == 0 or np.all(np.isnan(transformed)):
        return 0.0, 1.0
    lo, hi = float(np.nanmin(transformed)), float(np.nanmax(transformed))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return (lo if np.isfinite(lo) else 0.0), (lo + 1.0 if np.isfinite(lo) else 1.0)
    return lo, hi


def keep_lut(
    labels: np.ndarray, transformed: np.ndarray, threshold: float, max_label: int
) -> np.ndarray:
    """Boolean lookup table over label IDs: True where the cell passes `threshold`.

    Indexing a LUT with the label image is what notebook 6 does and is far faster
    than `np.isin` at these sizes. NaN fails the comparison, so unmeasured cells
    drop out.
    """
    lut = np.zeros(max_label + 1, dtype=bool)
    if labels.size == 0:
        return lut
    with np.errstate(invalid="ignore"):
        keep = labels[transformed >= threshold]
    keep = keep[(keep >= 0) & (keep <= max_label)]
    lut[keep] = True
    lut[0] = False  # background is never a kept object
    return lut


def positive_fraction(transformed: np.ndarray, threshold: float) -> float:
    """Fraction of *measured* cells at or above `threshold`, as a percentage.

    Denominator is the cells this channel actually has an intensity for — not the
    label count of the whole multi-plane mask, which is what notebook 5 divided by
    (BUG-22).
    """
    measured = ~np.isnan(transformed)
    n = int(measured.sum())
    if n == 0:
        return 0.0
    with np.errstate(invalid="ignore"):
        return 100.0 * float(np.count_nonzero(transformed[measured] >= threshold)) / n


# -----------------------------------------------------------------------------
# Mask geometry
# -----------------------------------------------------------------------------


class PlaneSource:
    """Reads single (H, W) planes out of a (C, H, W) TIFF, whatever its layout.

    Two layouts turn up. The pipeline's masks and tiles have one page per channel,
    where `pages[i].asarray()` decodes just that channel — ~21 ms on the real
    zlib-compressed masks. But tifffile stores a small leading axis as *samples per
    pixel* instead (a 4-plane stack becomes one page with 4 samples), and then
    `pages` has a single entry and `asarray(key=...)` raises. So the plane count
    always comes from `series.shape[0]`, never from `len(pages)`, and the
    sample-interleaved case falls back to tifffile's zarr store.

    `tifffile.memmap` is not usable either way: it needs uncompressed, contiguous
    data, and everything here is written with zlib.
    """

    def __init__(self, path):
        self.path = Path(path)
        with tifffile.TiffFile(self.path) as tif:
            series = tif.series[0]
            self.shape = tuple(series.shape)
            self.dtype = np.dtype(series.dtype)
            if len(self.shape) != 3:
                raise ValueError(f"expected a (C, H, W) stack, got {self.shape} in {self.path.name}")
            self.n_planes = int(self.shape[0])
            self._pages_are_planes = len(series.pages) == self.n_planes

    @property
    def plane_shape(self) -> tuple[int, int]:
        return int(self.shape[1]), int(self.shape[2])

    def read(self, index: int) -> np.ndarray:
        if not 0 <= index < self.n_planes:
            raise IndexError(f"plane {index} out of range (0..{self.n_planes - 1})")
        with tifffile.TiffFile(self.path) as tif:
            if self._pages_are_planes:
                return tif.series[0].pages[index].asarray()
            try:
                import zarr
            except ImportError as exc:  # pragma: no cover - zarr ships with this env
                raise RuntimeError(
                    f"{self.path.name} stores its {self.n_planes} planes as samples in one "
                    f"page; reading a single plane from it needs zarr installed."
                ) from exc
            return np.asarray(zarr.open(tif.aszarr(), mode="r")[index])


@dataclass(frozen=True)
class MaskInfo:
    shape: tuple[int, ...]
    dtype: np.dtype
    n_planes: int
    plane_offset: int  # 1 when plane 0 is the empty DAPI slot


def inspect_mask(mask_path, n_channels: int) -> MaskInfo:
    """Read the combined mask's geometry from the header, without loading pixels."""
    source = PlaneSource(mask_path)
    shape, dtype, n_planes = source.shape, source.dtype, source.n_planes

    if len(shape) != 3:
        raise ValueError(f"expected a (C, H, W) mask, got {shape} in {Path(mask_path).name}")

    if n_planes == n_channels + 1:
        offset = 1  # current layout: plane 0 empty, marker m at m + 1
    elif n_planes == n_channels:
        offset = 0  # already sliced, or the legacy layout with the empty plane last
    else:
        raise ValueError(
            f"{Path(mask_path).name} has {n_planes} planes but the quantification has "
            f"{n_channels} channels; expected {n_channels + 1} (plane 0 = DAPI slot) "
            f"or {n_channels}."
        )
    return MaskInfo(tuple(shape), dtype, n_planes, offset)


def plane_for_channel(channel_index: int, info: MaskInfo) -> int:
    return channel_index + info.plane_offset


# -----------------------------------------------------------------------------
# Threshold table
# -----------------------------------------------------------------------------


class ThresholdTable:
    """Rows = channels, columns = tiles, NaN = never gated.

    Accumulates across sessions: an existing file is loaded and its values kept,
    so gating one tile today and another tomorrow both land in the same CSV.
    """

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.frame.index.name = None  # notebook 6 reads with index_col=0

    # --- construction ---

    @classmethod
    def load(cls, path, channels: list[str], tiles: list[str]) -> "ThresholdTable":
        """Existing file (if any) widened to cover `channels` x `tiles`."""
        frame = pd.DataFrame(dtype=float)
        path = Path(path)
        if path.exists():
            frame = pd.read_csv(path, index_col=0)
            frame = frame.apply(pd.to_numeric, errors="coerce")

        rows = list(frame.index) + [c for c in channels if c not in frame.index]
        cols = list(frame.columns) + [t for t in tiles if t not in frame.columns]
        return cls(frame.reindex(index=rows, columns=cols).astype(float))

    # --- access ---

    def get(self, tile: str, channel: str) -> float:
        try:
            return float(self.frame.at[channel, tile])
        except KeyError:
            return float("nan")

    def set(self, tile: str, channel: str, value: float) -> None:
        # Explicit membership check: pandas .loc/.at silently *enlarge* on a missing
        # key, which is how a name drift would quietly add junk rows (BUG-23).
        if channel not in self.frame.index:
            raise KeyError(f"channel {channel!r} is not in the threshold table")
        if tile not in self.frame.columns:
            raise KeyError(f"tile {tile!r} is not in the threshold table")
        self.frame.at[channel, tile] = float(value)

    def clear(self, tile: str, channel: str) -> None:
        self.set(tile, channel, float("nan"))

    def unset_pairs(self, tiles: list[str] | None = None) -> list[tuple[str, str]]:
        """(tile, channel) pairs still NaN, for the save-time warning."""
        cols = tiles if tiles is not None else list(self.frame.columns)
        out = []
        for tile in cols:
            if tile not in self.frame.columns:
                continue
            for channel in self.frame.index:
                if np.isnan(self.frame.at[channel, tile]):
                    out.append((tile, channel))
        return out

    def gated_count(self, tile: str) -> tuple[int, int]:
        """(gated, total) channels for one tile."""
        if tile not in self.frame.columns:
            return 0, int(self.frame.shape[0])
        col = self.frame[tile]
        return int(col.notna().sum()), int(col.size)

    # --- persistence ---

    def save(self, path, meta: dict | None = None) -> Path:
        """Write atomically, and drop provenance next to it.

        Atomic because notebook 5 truncates and rewrites the whole CSV on every
        slider commit, so an interrupt there can leave an empty file (BUG-15).
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        os.close(handle)
        try:
            self.frame.to_csv(tmp)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

        if meta is not None:
            self.write_meta(path, meta)
        return path

    @staticmethod
    def meta_path(csv_path) -> Path:
        return Path(csv_path).with_suffix(Path(csv_path).suffix + ".meta.json")

    def write_meta(self, csv_path, meta: dict) -> Path:
        """Sidecar provenance. The CSV itself must stay pure numbers for notebook 6."""
        target = self.meta_path(csv_path)
        payload = {
            "written": time.strftime("%Y-%m-%d %H:%M:%S"),
            "written_by": "napari-multiplex-thresholder",
            **meta,
            "unset": [list(p) for p in self.unset_pairs()],
        }
        with open(target, "w") as f:
            json.dump(payload, f, indent=2)
        return target


# -----------------------------------------------------------------------------
# Gated mask export
# -----------------------------------------------------------------------------


def export_gated_mask(
    mask_path,
    out_path,
    luts: dict[int, np.ndarray],
    n_planes: int,
    compression: str = "zlib",
    dtype: np.dtype | None = None,
    progress=None,
) -> tuple[Path, list[int]]:
    """Write a (C, H, W) mask where each plane keeps only cells that passed.

    Streams: one plane is read, filtered and handed to tifffile at a time, so peak
    memory is two planes rather than the 5-18 GB the whole stack would take.
    `tifffile.imwrite` accepts an iterator when given `shape` and `dtype`.

    A plane with no LUT — an ungated channel, or the empty DAPI slot — is written
    as zeros, which is what notebook 6 produces from a NaN threshold. The indices
    of those planes are returned so the caller can say so out loud.
    """
    mask_path, out_path = Path(mask_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    source = PlaneSource(mask_path)
    out_dtype = np.dtype(dtype) if dtype is not None else source.dtype
    empty: list[int] = []

    def planes():
        for index in range(n_planes):
            if progress is not None:
                progress(index, n_planes)
            lut = luts.get(index)
            if lut is None:
                empty.append(index)
                yield np.zeros(source.plane_shape, dtype=out_dtype)
                continue
            plane = source.read(index)
            keep = lut[np.clip(plane, 0, lut.size - 1)]
            yield np.where(keep, plane, 0).astype(out_dtype, copy=False)

    # tifffile takes an iterator when given shape and dtype, so only one plane is
    # ever in memory instead of the 5-18 GB the whole stack would need.
    #
    # photometric="minisblack" is not cosmetic: without it tifffile treats a small
    # leading axis as samples-per-pixel and tries to write one interleaved page,
    # which does not match a plane-at-a-time iterator at all. Being explicit also
    # guarantees the output reads back as one page per channel.
    tifffile.imwrite(
        out_path,
        data=planes(),
        shape=(n_planes, *source.plane_shape),
        dtype=out_dtype,
        compression=compression,
        photometric="minisblack",
    )
    return out_path, empty
