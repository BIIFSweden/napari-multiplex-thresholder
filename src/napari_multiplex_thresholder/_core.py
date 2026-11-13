

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

# macOS AppleDouble sidecars: on exFAT volumes they sit next to the real files and
# carry the same extension.
APPLEDOUBLE = "._"

MASK_SUFFIX = "_entire_mask.tif"
QUANT_SUFFIX = "_quant.csv"

#: Cofactor that keeps this widget's numbers comparable with the downstream analysis.
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


def discover_tiles(
    tiles_dir,
    quant_dir,
    mask_dir,
    quant_suffix: str = QUANT_SUFFIX,
    mask_suffix: str = MASK_SUFFIX,
) -> tuple[list[TileRef], list[str]]:
    """Tiles that have all three files, plus a human-readable list of what's missing.

    Matching is by stem, never by position in two directory listings: an earlier
    version paired them positionally, where one stray file silently shifts every
    mask against its image.

    The two suffixes are what this study's step 4 writes. They are arguments because
    a dataset produced by another naming convention is otherwise unopenable, and the
    widget's Path suffix dialog is what sets them; the stem, and therefore the pairing,
    is still the raw tile's filename.
    """
    tiles_dir, quant_dir, mask_dir = Path(tiles_dir), Path(quant_dir), Path(mask_dir)
    quant_suffix = quant_suffix or QUANT_SUFFIX
    mask_suffix = mask_suffix or MASK_SUFFIX
    found: list[TileRef] = []
    problems: list[str] = []

    for name in list_files(tiles_dir, ".tif"):
        stem = name[: -len(".tif")]
        quant = quant_dir / f"{stem}{quant_suffix}"
        mask = mask_dir / f"{stem}{mask_suffix}"
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
    pixels in the compartment this marker is measured in". NaN never passes
    `>= threshold`, so such cells are simply never called positive.
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

    cofactor 1 is plain `np.arcsinh(quant)`, the space the downstream analysis works
    in and the one every previously saved `manual_thresholds_*.csv` is expressed in.
    cofactor 5 matches asinh-normalised AnnData exports and the cell-typing tools
    that read them. The two are not interchangeable; whichever is used is recorded
    in the sidecar.
    """
    if cofactor <= 0:
        raise ValueError(f"cofactor must be > 0, got {cofactor}")
    return np.arcsinh(values / cofactor)


def convert_threshold(
    value: float, *, from_raw: bool, to_raw: bool, cofactor: float = NOTEBOOK_COFACTOR
) -> float:
    """Move a stored threshold between raw mean intensity and `arcsinh(x / cofactor)`.

    Both directions are monotonic, so the cells a threshold selects are the same
    before and after; only the number changes. NaN (ungated) passes through, and a
    value already in the requested space is returned as is.
    """
    if not np.isfinite(value) or from_raw == to_raw:
        return float(value)
    if cofactor <= 0:
        raise ValueError(f"cofactor must be > 0, got {cofactor}")
    if to_raw:  # stored as arcsinh, wanted raw
        return float(np.sinh(value) * cofactor)
    return float(np.arcsinh(value / cofactor))  # stored raw, wanted arcsinh


def read_raw_pairs(meta_path, table: "ThresholdTable | None" = None) -> set[tuple[str, str]]:
    """(tile, channel) pairs the sidecar marks as raw mean intensity (no arcsinh).

    Tolerant on purpose: no sidecar, unreadable JSON or no `raw_space` key all give
    an empty set, since the CSV is the data and the sidecar only qualifies it. Pairs
    whose threshold is NaN in `table` are dropped, so a cleared threshold does not
    keep a stale raw flag.
    """
    path = Path(meta_path)
    if not path.exists():
        return set()
    try:
        with open(path) as f:
            payload = json.load(f)
        pairs = {(str(p[0]), str(p[1])) for p in payload.get("raw_space", []) if len(p) == 2}
    except (OSError, ValueError, TypeError, KeyError):
        return set()
    if table is not None:
        pairs = {p for p in pairs if np.isfinite(table.get(*p))}
    return pairs


def channel_range(transformed: np.ndarray) -> tuple[float, float]:
    """(min, max) of one channel, NaN-safe, for the slider bounds.

    Per channel and per tile on purpose. An earlier version used one global maximum,
    taken from a frame that still had a cell-count column mixed into it, so its
    slider topped out at `arcsinh(cell count)` — which can put the correct threshold
    out of reach entirely.
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

    Indexing a LUT with the label image is what the downstream analysis does and is
    far faster than `np.isin` at these sizes. NaN fails the comparison, so unmeasured
    cells drop out.
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
    label count of the whole multi-plane mask, which is what an earlier version
    divided by.
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

    A plain 2D image — a Cellpose label image, a single-channel tile — is read as a
    stack of one plane rather than rejected: `shape` reports `(1, H, W)` and
    `read(0)` returns it. Segmentation that is one label image for the whole tile is
    a layout this project meets in the wild, and the alternative was asking for a
    file to be rewritten with an axis that carries no information.

    Two layouts turn up for real stacks. The pipeline's masks and tiles have one page
    per channel,
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
            shape = tuple(series.shape)
            self.dtype = np.dtype(series.dtype)
            if len(shape) == 2:
                shape = (1, *shape)  # one label image for the whole tile
            elif len(shape) != 3:
                raise ValueError(
                    f"expected a (C, H, W) stack or a single (H, W) plane, got "
                    f"{shape} in {self.path.name}"
                )
            self.shape = shape
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


#: What the pipeline calls a tile's nuclei segmentation. It lands in `nuclei/` in a
#: current run and in `segmentation_nuclei/` in older ones, both beside the combined
#: masks, so it is found by name rather than by folder.
NUCLEI_SUFFIX = "_nuclei_mask.tif"


def find_nuclei_mask(masks_dir, stem: str) -> Path | None:
    """The nuclei segmentation belonging to a tile's combined mask, if it is there.

    The combined mask's first plane is empty by construction — the pipeline fills
    `entire_mask[m + 1]` and never the DAPI slot — so there is nothing to draw beside
    the DAPI channel. The nuclei mask DAPI was segmented into comes out of the same
    run, one folder over. Returns None rather than raising: it is a reference view,
    nothing is gated on it, and a dataset shipped without that folder must still work.
    """
    masks_dir = Path(masks_dir)
    name = f"{stem}{NUCLEI_SUFFIX}"
    if (masks_dir / name).is_file():
        return masks_dir / name
    parent = masks_dir.parent
    if not parent.is_dir():
        return None
    for sibling in sorted(parent.iterdir()):
        if not sibling.is_dir() or sibling.name.startswith("."):
            continue
        candidate = sibling / name
        if candidate.is_file():
            return candidate
    return None


def read_single_plane(path) -> np.ndarray:
    """A one-plane mask file — `<stem>_nuclei_mask.tif` — as a 2D array."""
    plane = tifffile.imread(str(path))
    if plane.ndim != 2:
        raise ValueError(
            f"expected a single (H, W) plane, got {plane.shape} in {Path(path).name}"
        )
    return plane


@dataclass(frozen=True)
class MaskInfo:
    """How the mask file is laid out, in the mask's own plane numbers.

    `plane_offset` is where the first marker sits *in the mask* — 1 for this study's
    combined masks, whose plane 0 is the empty DAPI slot. It is not where the first
    marker sits on screen: that comes from the raw tile, which always leads with
    DAPI, and the two are different numbers whenever the mask has no DAPI slot.

    `shared` means the file holds one label image that backs every marker — a
    Cellpose segmentation of the whole tile, gated per channel against the same
    objects. Then there is no per-marker plane at all and every channel reads
    plane 0.
    """

    shape: tuple[int, ...]
    dtype: np.dtype
    n_planes: int
    plane_offset: int  # 1 when plane 0 is the empty DAPI slot
    shared: bool = False  # one plane for every marker

    def plane_for(self, channel_index: int) -> int:
        """The mask plane a marker is read from."""
        return 0 if self.shared else channel_index + self.plane_offset


def inspect_mask(mask_path, n_channels: int) -> MaskInfo:
    """Read the combined mask's geometry from the header, without loading pixels."""
    source = PlaneSource(mask_path)
    shape, dtype, n_planes = source.shape, source.dtype, source.n_planes

    if len(shape) != 3:
        raise ValueError(f"expected a (C, H, W) mask, got {shape} in {Path(mask_path).name}")

    if n_planes == 1:
        # One label image for the whole tile: every marker is gated against the same
        # objects. Also the reading of a 1-channel quantification beside a one-plane
        # mask, where "shared" and "one plane per marker" are the same mapping.
        offset, shared = 0, True
    elif n_planes == n_channels + 1:
        offset, shared = 1, False  # this study: plane 0 empty, marker m at m + 1
    elif n_planes == n_channels:
        offset, shared = 0, False  # already sliced, or the empty plane written last
    else:
        raise ValueError(
            f"{Path(mask_path).name} has {n_planes} planes but the quantification has "
            f"{n_channels} channels; expected {n_channels + 1} (plane 0 = DAPI slot), "
            f"{n_channels}, or 1 (one label image shared by every marker)."
        )
    return MaskInfo(tuple(shape), dtype, n_planes, offset, shared)


def plane_for_channel(channel_index: int, info: MaskInfo) -> int:
    return info.plane_for(channel_index)


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
        self.frame.index.name = None  # the downstream analysis reads with index_col=0

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
        # key, which is how a name drift would quietly add junk rows.
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

        Atomic because an earlier version truncated and rewrote the whole CSV on
        every slider commit, so an interrupt there can leave an empty file.
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
        """Sidecar provenance. The CSV itself must stay pure numbers for the reader."""
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
    source_planes: dict[int, int] | None = None,
) -> tuple[Path, list[int]]:
    """Write a (C, H, W) mask where each plane keeps only cells that passed.

    Streams: one plane is read, filtered and handed to tifffile at a time, so peak
    memory is two planes rather than the whole stack, which for a label mask of this
    shape runs to gigabytes. `tifffile.imwrite` accepts an iterator when given
    `shape` and `dtype`.

    A plane with no LUT — an ungated channel, or the empty DAPI slot — is written
    as zeros, which is what the downstream analysis produces from a NaN threshold.
    The indices of those planes are returned so the caller can say so out loud.

    `source_planes` maps an output plane to the source plane it is read from, and
    defaults to reading plane i for plane i. It is what lets a mask with one shared
    label image still export one plane per marker: several output planes then read
    the same source plane and differ only in the LUT applied to it.
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
            plane = source.read((source_planes or {}).get(index, index))
            keep = lut[np.clip(plane, 0, lut.size - 1)]
            yield np.where(keep, plane, 0).astype(out_dtype, copy=False)

    # tifffile takes an iterator when given shape and dtype, so only one plane is
    # ever in memory instead of the whole multi-gigabyte stack.
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
