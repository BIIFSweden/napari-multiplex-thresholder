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


def test_discovery_honours_custom_suffixes():
    """A dataset named by another convention opens by changing the two suffixes."""
    with tempfile.TemporaryDirectory() as tmp:
        tiles, quant, masks = _fixture(Path(tmp))
        for stem in ("T_tile0", "T_tile1"):
            (quant / f"{stem}{_core.QUANT_SUFFIX}").rename(quant / f"{stem}.measure.csv")
            (masks / f"{stem}{_core.MASK_SUFFIX}").rename(masks / f"{stem}-cells.tiff")

        found, problems = _core.discover_tiles(tiles, quant, masks)
        assert found == [] and len(problems) == 2  # the defaults no longer match

        found, problems = _core.discover_tiles(
            tiles, quant, masks, quant_suffix=".measure.csv", mask_suffix="-cells.tiff"
        )
        assert [t.stem for t in found] == ["T_tile0", "T_tile1"]
        assert problems == []
        assert found[0].quant.name == "T_tile0.measure.csv"
        assert found[0].mask.name == "T_tile0-cells.tiff"

        # empty means "the default", never a bare stem: the widget's dialog treats a
        # cleared field the same way
        assert _core.discover_tiles(tiles, quant, masks, quant_suffix="")[0] == []


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


def test_flat_mask_is_one_shared_plane():
    """A plain 2D label image — Cellpose writes one — backs every marker.

    It has no channel axis at all, so the plane count cannot say where the first
    marker is and there is nothing to line up against the quantification: every
    channel reads plane 0 and the LUTs are what tell them apart.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plane = np.zeros((32, 40), dtype=np.uint32)
        plane[:10, :] = np.arange(1, 11, dtype=np.uint32)[:, None]
        flat = root / "cells_cyto3_labels.tif"
        tifffile.imwrite(flat, plane, compression="zlib")

        source = _core.PlaneSource(flat)
        assert source.shape == (1, 32, 40) and source.n_planes == 1
        assert np.array_equal(source.read(0), plane)
        try:
            source.read(1)
        except IndexError:
            pass
        else:
            raise AssertionError("a one-plane source has no plane 1")

        for n_channels in (1, 3):
            info = _core.inspect_mask(flat, n_channels)
            assert info.shared and info.n_planes == 1 and info.plane_offset == 0
            assert all(_core.plane_for_channel(c, info) == 0 for c in range(n_channels))


def test_shared_mask_gates_each_marker_on_its_own():
    """Same objects, one LUT per marker, and one exported plane per marker."""
    from napari_multiplex_thresholder import _lazy

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plane = np.zeros((32, 40), dtype=np.uint32)
        for lab in range(1, 11):
            plane[lab - 1, :] = lab
        flat = root / "cells_cyto3_labels.tif"
        tifffile.imwrite(flat, plane, compression="zlib")

        labels = _lazy.LazyGatedLabels(flat, n_channels=2, plane_offset=0, shared=True)
        assert labels.plane_of(0) == 0 and labels.plane_of(1) == 0

        keep_low = np.zeros(11, dtype=bool); keep_low[1:4] = True    # labels 1-3
        keep_high = np.zeros(11, dtype=bool); keep_high[8:11] = True  # labels 8-10
        labels.set_lut(0, keep_low)
        labels.set_lut(1, keep_high)
        assert set(np.unique(labels.plane(0))) - {0} == {1, 2, 3}
        assert set(np.unique(labels.plane(1))) - {0} == {8, 9, 10}

        # export: one plane per plane of the *tile* — the empty DAPI slot, then one
        # gating of the shared image per marker, all reading source plane 0
        out, empty = _core.export_gated_mask(
            flat, root / "out" / "gated.tif",
            luts={1: keep_low, 2: keep_high}, n_planes=3,
            source_planes={1: 0, 2: 0},
        )
        written = tifffile.imread(out)
        assert written.shape == (3, 32, 40)
        assert empty == [0] and written[0].max() == 0
        assert set(np.unique(written[1])) - {0} == {1, 2, 3}
        assert set(np.unique(written[2])) - {0} == {8, 9, 10}


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


def test_nuclei_mask_is_found_beside_the_masks():
    """The DAPI plane's mask lives one folder over, and the folder has two names."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _, _, masks = _fixture(root)
        stem = "T_tile0"
        assert _core.find_nuclei_mask(masks, stem) is None  # nothing written yet

        # current pipeline layout: nuclei/ beside multilayer/
        nuclei = root / "nuclei"
        nuclei.mkdir()
        written = nuclei / f"{stem}{_core.NUCLEI_SUFFIX}"
        tifffile.imwrite(written, np.zeros((64, 80), dtype=np.int64), compression="zlib")
        assert _core.find_nuclei_mask(masks, stem) == written
        assert _core.read_single_plane(written).shape == (64, 80)

        # older runs call it segmentation_nuclei/, and the file must still be found
        renamed = root / "segmentation_nuclei"
        nuclei.rename(renamed)
        assert _core.find_nuclei_mask(masks, stem) == renamed / f"{stem}{_core.NUCLEI_SUFFIX}"

        # the masks folder itself wins over a sibling, and the match is per stem: a
        # tile with no nuclei mask of its own must not be handed another tile's
        beside = masks / f"{stem}{_core.NUCLEI_SUFFIX}"
        tifffile.imwrite(beside, np.zeros((64, 80), dtype=np.int64), compression="zlib")
        assert _core.find_nuclei_mask(masks, stem) == beside
        assert _core.find_nuclei_mask(masks, "T_tile1") is None
        assert _core.find_nuclei_mask(masks, "not_a_tile") is None


def test_threshold_space_conversion_and_raw_sidecar():
    # arcsinh is monotonic: the same cells pass whichever space the number is in
    raw = 1234.5
    as_arcsinh = _core.convert_threshold(raw, from_raw=True, to_raw=False)
    assert np.isclose(as_arcsinh, np.arcsinh(raw))
    assert np.isclose(_core.convert_threshold(as_arcsinh, from_raw=False, to_raw=True), raw)
    with_cof = _core.convert_threshold(raw, from_raw=True, to_raw=False, cofactor=5.0)
    assert np.isclose(with_cof, np.arcsinh(raw / 5.0))
    assert np.isclose(
        _core.convert_threshold(with_cof, from_raw=False, to_raw=True, cofactor=5.0), raw
    )
    values = np.array([10.0, 1234.5, 5000.0, np.nan])
    assert np.array_equal(values >= raw, _core.transform(values) >= as_arcsinh, equal_nan=False)
    # same space and NaN pass through untouched
    assert _core.convert_threshold(raw, from_raw=True, to_raw=True) == raw
    assert np.isnan(_core.convert_threshold(float("nan"), from_raw=True, to_raw=False))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manual_thresholds_test.csv"
        meta_path = _core.ThresholdTable.meta_path(path)
        table = _core.ThresholdTable.load(path, CHANNELS, ["T_tile0", "T_tile1"])
        # no sidecar yet -> nothing is raw
        assert _core.read_raw_pairs(meta_path, table) == set()

        table.set("T_tile0", "MarkerA", raw)          # raw
        table.set("T_tile0", "MarkerB", 7.5)          # arcsinh
        table.save(path, meta={"cofactor": 1.0, "raw_space": [["T_tile0", "MarkerA"],
                                                              ["T_tile1", "MarkerC"]]})
        # the CSV itself stays pure numbers for the downstream reader
        back = pd.read_csv(path, index_col=0)
        assert back.at["MarkerA", "T_tile0"] == raw and back.at["MarkerB", "T_tile0"] == 7.5
        # the pair whose value is NaN is dropped; the gated raw one survives
        assert _core.read_raw_pairs(meta_path, table) == {("T_tile0", "MarkerA")}
        # without a table every listed pair comes back
        assert _core.read_raw_pairs(meta_path) == {("T_tile0", "MarkerA"), ("T_tile1", "MarkerC")}
        # a broken sidecar is tolerated
        meta_path.write_text("{not json")
        assert _core.read_raw_pairs(meta_path, table) == set()


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
