"""Logic tests for napari_multiplex_thresholder._core — no Qt, no napari, synthetic data.

Runs under pytest, or directly (`python tests/test_core.py`) so it also works in an
analysis environment that has no pytest installed.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import tifffile

from napari_multiplex_thresholder import _core

CHANNELS = ["MarkerA", "MarkerB", "MarkerC"]


def _fixture(root: Path, stems=("T_tile0", "T_tile1"), n_cells=40, shape=(64, 80)):
    """A miniature version of the real layout: tiles/, quant/, masks/."""
    tiles, quant, masks = root / "tiles", root / "quant", root / "masks"
    for d in (tiles, quant, masks):
        d.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(0)
    for t, stem in enumerate(stems):
        labels = np.arange(1, n_cells + 1) + t * 1000  # offset, as the pipeline does
        values = rng.uniform(1, 5000, size=(n_cells, len(CHANNELS)))
        values[0, 1] = np.nan  # a cell with no pixels in this compartment
        df = pd.DataFrame(values, columns=CHANNELS)
        df.insert(0, "label", labels)
        df.to_csv(quant / f"{stem}{_core.QUANT_SUFFIX}", index=False)

        # (C, H, W): plane 0 empty (DAPI slot), one row of pixels per cell
        mask = np.zeros((len(CHANNELS) + 1, *shape), dtype=np.int64)
        for i, lab in enumerate(labels):
            mask[1:, i % shape[0], :] = lab
        tifffile.imwrite(masks / f"{stem}{_core.MASK_SUFFIX}", mask, compression="zlib")

        tile = (rng.uniform(0, 6000, (len(CHANNELS) + 1, *shape))).astype(np.uint16)
        tifffile.imwrite(tiles / f"{stem}.tif", tile, compression="zlib")

    (tiles / "._T_tile0.tif").write_bytes(b"AppleDouble")  # must be ignored
    return tiles, quant, masks


def test_discovery_matches_on_stem():
    with tempfile.TemporaryDirectory() as tmp:
        tiles, quant, masks = _fixture(Path(tmp))
        # a stray file in masks/ must not shift anything
        (masks / "zzz_stray_entire_mask.tif").write_bytes(b"")
        found, problems = _core.discover_tiles(tiles, quant, masks)
        assert [t.stem for t in found] == ["T_tile0", "T_tile1"]
        assert problems == []
        assert all(t.quant.exists() and t.mask.exists() for t in found)

        # a tile with no quantification is reported, not silently paired
        (tiles / "T_tile9.tif").write_bytes(b"")
        found, problems = _core.discover_tiles(tiles, quant, masks)
        assert [t.stem for t in found] == ["T_tile0", "T_tile1"]
        assert any("T_tile9" in p for p in problems)


def test_quantification_and_transform():
    with tempfile.TemporaryDirectory() as tmp:
        _, quant, _ = _fixture(Path(tmp))
        q = _core.load_quantification(quant / "T_tile0_quant.csv")
        assert q.channels == CHANNELS and q.n_cells == 40
        assert q.nan_count("MarkerB") == 1 and q.nan_count("MarkerA") == 0

        raw = q.column("MarkerA")
        assert np.allclose(_core.transform(raw, 1.0), np.arcsinh(raw))
        assert np.allclose(_core.transform(raw, 5.0), np.arcsinh(raw / 5.0))
        # cofactor 1 is the space the downstream analysis works in
        assert _core.NOTEBOOK_COFACTOR == 1.0


def test_channel_range_is_per_channel():
    """The range must come from this channel, not from a pooled maximum."""
    values = np.array([1.0, 2.0, 9.0, np.nan])
    assert _core.channel_range(values) == (1.0, 9.0)
    assert _core.channel_range(np.array([np.nan, np.nan])) == (0.0, 1.0)
    lo, hi = _core.channel_range(np.array([3.0, 3.0]))  # degenerate
    assert lo == 3.0 and hi > lo


def test_keep_lut_and_percentage_are_nan_safe():
    labels = np.array([1, 2, 3, 4])
    values = np.array([1.0, 5.0, 9.0, np.nan])
    lut = _core.keep_lut(labels, values, 5.0, max_label=4)
    assert lut.tolist() == [False, False, True, True, False]  # NaN excluded, 0 never kept
    assert abs(_core.positive_fraction(values, 5.0) - 200 / 3) < 1e-9  # 2 of 3 measured
    assert _core.positive_fraction(np.array([np.nan]), 0.0) == 0.0


def test_mask_layout_detection():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, masks = _fixture(Path(tmp))
        info = _core.inspect_mask(masks / "T_tile0_entire_mask.tif", len(CHANNELS))
        assert info.n_planes == len(CHANNELS) + 1
        assert info.plane_offset == 1
        assert _core.plane_for_channel(0, info) == 1
        assert _core.plane_for_channel(2, info) == 3
        try:
            _core.inspect_mask(masks / "T_tile0_entire_mask.tif", 17)
        except ValueError as exc:
            assert "planes" in str(exc)
        else:
            raise AssertionError("a mismatched channel count must raise")


def test_threshold_table_nan_semantics_and_atomic_save():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manual_thresholds_test.csv"
        table = _core.ThresholdTable.load(path, CHANNELS, ["T_tile0", "T_tile1"])
        assert table.frame.shape == (3, 2)
        assert table.frame.isna().all().all()  # unset is NaN, never 0
        assert len(table.unset_pairs()) == 6
        assert table.gated_count("T_tile0") == (0, 3)

        table.set("T_tile0", "MarkerB", 8.25)
        assert table.get("T_tile0", "MarkerB") == 8.25
        assert np.isnan(table.get("T_tile1", "MarkerB"))
        assert table.gated_count("T_tile0") == (1, 3)

        # a name that is not in the grid must raise, not silently enlarge
        for bad in (("T_tile0", "NOPE"), ("NOPE", "MarkerB")):
            try:
                table.set(*bad, 1.0)
            except KeyError:
                pass
            else:
                raise AssertionError(f"{bad} should have raised")

        table.save(path, meta={"cofactor": 1.0})
        assert path.exists()
        assert not list(path.parent.glob(".*tmp*")), "temp file left behind"

        # exactly what the downstream analysis does with it
        back = pd.read_csv(path, index_col=0)
        assert list(back.index) == CHANNELS and list(back.columns) == ["T_tile0", "T_tile1"]
        assert back.at["MarkerB", "T_tile0"] == 8.25 and np.isnan(back.at["MarkerB", "T_tile1"])

        meta = json.loads(_core.ThresholdTable.meta_path(path).read_text())
        assert meta["cofactor"] == 1.0 and len(meta["unset"]) == 5

        # reloading keeps what was set and widens for a new tile
        again = _core.ThresholdTable.load(path, CHANNELS, ["T_tile0", "T_tile1", "T_tile2"])
        assert again.get("T_tile0", "MarkerB") == 8.25
        assert list(again.frame.columns) == ["T_tile0", "T_tile1", "T_tile2"]


def test_export_gated_mask_streams_and_zeroes_ungated():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _, quant, masks = _fixture(root)
        mask_path = masks / "T_tile0_entire_mask.tif"
        q = _core.load_quantification(quant / "T_tile0_quant.csv")
        info = _core.inspect_mask(mask_path, len(CHANNELS))

        source = tifffile.imread(mask_path)
        values = _core.transform(q.column("MarkerA"), 1.0)
        threshold = float(np.nanmedian(values))
        lut = _core.keep_lut(q.labels, values, threshold, int(source.max()))

        out, empty = _core.export_gated_mask(
            mask_path, root / "out" / "T_tile0_gated_mask.tif",
            luts={1: lut}, n_planes=info.n_planes,
        )
        written = tifffile.imread(out)
        assert written.shape == source.shape
        # plane 1 keeps exactly the labels that passed
        kept = set(np.unique(written[1])) - {0}
        expected = set(q.labels[values >= threshold].tolist())
        assert kept == expected and kept, (len(kept), len(expected))
        # ungated planes are empty, and reported as such
        assert empty == [0, 2, 3]
        assert written[2].max() == 0 and written[3].max() == 0
        assert source[2].max() > 0  # the source did have data there


def test_plane_reader_reads_one_plane_at_a_time():
    from napari_multiplex_thresholder import _lazy

    with tempfile.TemporaryDirectory() as tmp:
        _, _, masks = _fixture(Path(tmp))
        reader = _lazy.PlaneReader(masks / "T_tile0_entire_mask.tif", cache_planes=2)
        assert reader.n_planes == len(CHANNELS) + 1
        first = reader.read(1)
        assert first.ndim == 2 and first.shape == reader.plane_shape
        assert reader.read(1) is first  # cached
        reader.read(2), reader.read(3)
        assert len(reader._cache) == 2  # LRU capped

        labels = _lazy.LazyGatedLabels(
            masks / "T_tile0_entire_mask.tif", len(CHANNELS), plane_offset=1
        )
        assert labels.plane(0).dtype == _lazy.LABEL_DTYPE
        ungated = int((labels.plane(0) > 0).sum())
        lut = np.zeros(labels.max_label(0) + 1, dtype=bool)
        lut[labels.raw_plane(0).max()] = True  # keep a single label
        labels.set_lut(0, lut)
        assert 0 < int((labels.plane(0) > 0).sum()) < ungated
        assert labels.gated_channels() == [0]
        assert set(labels.luts_by_plane()) == {1}
        labels.set_lut(0, None)
        assert int((labels.plane(0) > 0).sum()) == ungated  # not cumulative

        stack = labels.as_dask()
        assert stack.shape == (len(CHANNELS), *reader.plane_shape)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback

            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    raise SystemExit(1 if failures else 0)
