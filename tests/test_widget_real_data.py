"""Drives the real widgets against real tiles in the study data folder, offscreen.

This is the test that matters: it opens a napari viewer, loads a tile through the
Load button's code path, walks channels with Next/Back, checks the channel slider
and the dropdown stay in step, applies a threshold and verifies that cells below it
actually disappear from the label layer, then checks the CSV the downstream analysis
will read.

It also measures resident memory across the whole thing, because the point of the
rewrite is that a tile no longer has to be held in memory in full.


NOT with QT_QPA_PLATFORM=offscreen: napari renders through vispy, the offscreen Qt
platform provides no OpenGL context, and the process dies with SIGSEGV before the
first assertion. `napari.Viewer(show=False)` on macOS gets a real context without
putting a window on screen, which is what this uses.

Skips itself if the data folder is not present.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Deliberately not forcing an offscreen platform — see the module docstring.
os.environ.pop("QT_QPA_PLATFORM", None)

def find_data() -> Path:
    """Locate the study's data folder, wherever this package now lives.

    This repository is developed both inside a larger analysis repository and
    standalone, so the data cannot be assumed to sit a fixed number of levels up.
    Order: an explicit GATING_TEST_DATA, then the conventional data folder in this
    file's ancestors or their siblings. Returns a non-existent path if nothing is
    found, and main() then skips.
    """
    override = os.environ.get("GATING_TEST_DATA")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        for candidate in (parent / "data" / "new", *sorted(parent.glob("*/data/new"))):
            if (candidate / "tiles").is_dir():
                return candidate
    return here.parents[2] / "data" / "new"


DATA = find_data()
TILES = DATA / "tiles"
QUANT = DATA / "quantification_combined"
MASKS = DATA / "segmentation_combined"


def _pooled_slider_bound() -> int:
    """The n_cells an earlier global slider maximum was derived from: the largest tile."""
    best = 0
    for f in sorted(QUANT.glob("*_quant.csv")):
        best = max(best, len(pd.read_csv(f, usecols=[0])))
    return best


def peak_gb() -> float:
    """Peak resident set size. Monotonic — a high-water mark, never current usage.

    `resource` is Unix-only; this whole file is a developer test that needs the real
    data, so on Windows the memory figures are simply reported as nan
    rather than making the run fail.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return float("nan")

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes
    return peak / 1e9 if sys.platform == "darwin" else peak / 1e6


def rss_gb() -> float:
    """Current resident set size, without adding psutil to the environment."""
    import shutil
    import subprocess

    ps = shutil.which("ps")
    if ps is None:  # pragma: no cover - Windows
        return float("nan")
    out = subprocess.run(
        [ps, "-o", "rss=", "-p", str(os.getpid())],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return int(out) / 1e6 if out.isdigit() else float("nan")


def rendered_slice(layer) -> np.ndarray:
    """The label plane napari is actually displaying.

    Private (`layer._slice.image.raw`), but there is no public way to see the
    rendered slice, and asserting on the *source* array is exactly the hole that let
    a stale-display bug through: `layer.refresh()` does not re-run a dask graph, so
    the mask showed every cell while the source was correctly gated. If napari moves
    this attribute, this raises rather than silently passing.
    """
    try:
        return np.asarray(layer._slice.image.raw)
    except AttributeError as exc:  # pragma: no cover
        raise AssertionError(
            "cannot read the rendered slice from this napari version — find the new "
            "accessor rather than dropping the check"
        ) from exc


def main() -> int:
    if not (TILES.is_dir() and QUANT.is_dir() and MASKS.is_dir()):
        print(f"SKIP: {DATA} not available")
        return 0

    import napari
    from qtpy.QtWidgets import QApplication

    from napari_multiplex_thresholder import _core
    from napari_multiplex_thresholder._widget import GatingControls, KdePlot

    out_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "napari_multiplex_thresholder_test"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "manual_thresholds_widgettest.csv"
    csv_path.unlink(missing_ok=True)
    _core.ThresholdTable.meta_path(csv_path).unlink(missing_ok=True)

    baseline = rss_gb()
    viewer = napari.Viewer(show=False)
    kde = KdePlot(dark=True)
    widget = GatingControls(viewer, kde=kde)

    widget.paths["tiles"].set_value(str(TILES))
    widget.paths["quant"].set_value(str(QUANT))
    widget.paths["masks"].set_value(str(MASKS))
    widget.paths["thresholds"].set_value(str(csv_path))
    widget.paths["export"].set_value(str(out_dir))

    # No Refresh button and no Save button: a path edit re-lists the tiles by itself,
    # and every Run persists the CSV.
    assert not hasattr(widget, "refresh_button")
    assert not hasattr(widget, "save_button")

    # point somewhere with no tiles -> the list empties
    widget.paths["tiles"].set_value(str(out_dir))
    widget.paths["tiles"].line.editingFinished.emit()
    assert widget.tile_combo.count() == 0, "a bad tiles path should empty the list"
    # point back -> it fills again, with no button pressed
    widget.paths["tiles"].set_value(str(TILES))
    widget.paths["tiles"].line.editingFinished.emit()
    stems = [widget.tile_combo.itemText(i) for i in range(widget.tile_combo.count())]
    assert stems, "editing a path did not refresh the tile list"
    print(f"ok   a path edit re-listed the tiles by itself: {len(stems)} found, "
          f"{stems[0]} …")

    # smallest tile, so the test is quick but still real data
    target = os.environ.get("GATING_TEST_TILE", "")
    index = stems.index(target) if target in stems else 0
    widget.tile_combo.setCurrentIndex(index)
    widget.load_tile()
    assert widget.quant is not None, "load_tile did not complete"
    stem = widget.tile.stem
    n_channels = len(widget.quant.channels)
    print(f"ok   loaded {stem}: {widget.quant.n_cells:,} cells, {n_channels} channels, "
          f"mask {widget.mask_info.shape} {widget.mask_info.dtype}")
    after_load = rss_gb()

    # --- the raw tile is a lazy (C, H, W) stack: that is what gives the channel
    #     slider. The mask is one reused 2D buffer that napari aliases.
    assert widget.image_layer.data.shape[0] == n_channels
    assert widget.labels_layer.data.ndim == 2, widget.labels_layer.data.shape
    assert widget.labels_layer.data is widget._display
    assert np.shares_memory(widget._display, rendered_slice(widget.labels_layer)), (
        "napari copied the buffer instead of aliasing it — in-place updates would not show"
    )
    plane_shape = widget.mask_info.shape[1:]
    full_stack_gb = n_channels * int(np.prod(plane_shape)) * 8 / 1e9
    print(f"ok   raw tile is a ({n_channels}, H, W) dask stack; mask is one aliased "
          f"{plane_shape} buffer (an eager int64 stack would be {full_stack_gb:.1f} GB)")

    # --- channel <-> slider stay in step, both directions ---
    widget.step_channel(+1)
    widget.step_channel(+1)
    assert widget.channel_index == 2
    assert viewer.dims.current_step[0] == 2, viewer.dims.current_step
    widget.step_channel(-1)
    assert widget.channel_index == 1 and viewer.dims.current_step[0] == 1
    print(f"ok   Next/Back moved the napari channel slider to {viewer.dims.current_step[0]}")

    viewer.dims.set_current_step(0, 7)
    assert widget.channel_index == 7, widget.channel_index
    assert widget.channel == widget.quant.channels[7]
    print(f"ok   dragging the slider moved the dropdown to {widget.channel!r}")

    widget.step_channel(-99)  # clamped, not an error
    assert widget.channel_index == 7
    # steady state: a tile is loaded, several channels have been visited, and the
    # test has not yet made copies of its own
    steady = rss_gb()

    # --- slider range is this channel's own, not a pooled maximum ---
    channel = widget.channel
    values = widget.transformed(channel)
    lo, hi = _core.channel_range(values)
    # the spin box rounds to its decimals, so the range must *cover* the channel,
    # never sit inside it — the extreme cells have to stay reachable
    assert widget.value_box.minimum() <= lo, (widget.value_box.minimum(), lo)
    assert widget.value_box.maximum() >= hi, (widget.value_box.maximum(), hi)
    assert widget.value_box.maximum() - hi < 1e-3 and lo - widget.value_box.minimum() < 1e-3
    # The earlier bound was one global max over all tiles, taken from a frame that
    # still had a cell-count column mixed in: max over tiles of arcsinh(n_cells).
    # Report both so the difference is visible on real data.
    pooled_bound = float(np.arcsinh(_pooled_slider_bound()))
    print(f"ok   slider range for {channel}: {lo:.4f}..{hi:.4f}  "
          f"(an earlier version used one global bound of arcsinh(max n_cells) = "
          f"{pooled_bound:.4f} for every channel and tile)")

    # --- Run hides sub-threshold cells, exactly as before ---
    plane_before = widget.labels.plane(widget.channel_index)
    labels_before = np.unique(plane_before)
    widget.value_box.setValue(float(np.nanmedian(values)))
    threshold = float(widget.value_box.value())  # what the 4-decimal box actually holds
    widget.apply_threshold()

    plane_after = widget.labels.plane(widget.channel_index)
    labels_after = np.unique(plane_after)
    assert labels_after.size < labels_before.size, (labels_after.size, labels_before.size)

    # ...and napari is actually SHOWING the gated plane, not a cached one
    shown = rendered_slice(widget.labels_layer)
    assert np.array_equal(shown, plane_after), (
        f"the viewer still shows {np.count_nonzero(shown):,} label pixels while the "
        f"gated plane has {np.count_nonzero(plane_after):,} — the display did not update"
    )
    print(f"ok   the viewer's slice matches the gated plane "
          f"({np.count_nonzero(shown):,} label px, was {np.count_nonzero(plane_before):,})")
    kept = set(labels_after.tolist()) - {0}
    expected = set(widget.quant.labels[values >= threshold].tolist())
    assert kept <= expected, f"{len(kept - expected)} labels survived that should not have"
    print(f"ok   Run at {threshold:.4f}: {labels_before.size - 1:,} labels -> "
          f"{labels_after.size - 1:,} ({_core.positive_fraction(values, threshold):.1f}% positive)")

    # every surviving cell is genuinely above the threshold
    lookup = dict(zip(widget.quant.labels.tolist(), values.tolist()))
    below = [l for l in kept if not (lookup.get(l, -np.inf) >= threshold)]
    assert not below, f"{len(below)} kept cells are below the threshold"
    print(f"ok   all {len(kept):,} surviving cells are >= the threshold")

    # --- un-gating restores the plane, and is not cumulative ---
    widget.clear_threshold()
    assert np.array_equal(widget.labels.plane(widget.channel_index), plane_before)
    assert np.array_equal(rendered_slice(widget.labels_layer), plane_before), (
        "un-gate did not put every label back on screen"
    )
    assert np.isnan(widget.table.get(stem, channel))
    print("ok   un-gate restored every label on screen and set the CSV cell to NaN")

    # --- gate three channels and check the CSV the downstream analysis will read ---
    gated = {}
    for c in (0, 1, 2):
        viewer.dims.set_current_step(0, c)
        vals = widget.transformed(widget.channel)
        widget.value_box.setValue(float(np.nanpercentile(vals, 75)))
        widget.apply_threshold()
        gated[widget.channel] = float(widget.value_box.value())

    # No explicit save: each Run must have persisted the CSV on its own
    assert csv_path.exists(), "Run did not write the thresholds CSV"
    frame = pd.read_csv(csv_path, index_col=0)
    assert list(frame.index) == widget.quant.channels
    assert stem in frame.columns
    for name, thr in gated.items():
        # the CSV must hold exactly the value that was applied
        assert frame.at[name, stem] == thr, (name, frame.at[name, stem], thr)
        assert widget.table.get(stem, name) == thr
    unset = frame[stem].isna().sum()
    assert unset == n_channels - 3, unset
    print(f"ok   CSV: rows=markers, cols=tiles, 3 gated, {unset} still NaN")

    meta = _core.ThresholdTable.meta_path(csv_path)
    assert meta.exists()
    import json

    assert json.loads(meta.read_text())["cofactor"] == 1.0
    print(f"ok   sidecar {meta.name} records cofactor 1 (the space the downstream analysis works in)")

    # --- export streams a gated mask ---
    out, empty = _core.export_gated_mask(
        widget.tile.mask,
        out_dir / f"{stem}_gated_mask.tif",
        widget.labels.luts_by_plane(),
        n_planes=widget.mask_info.n_planes,
        compression="zlib",
    )
    import tifffile

    with tifffile.TiffFile(out) as tif:
        shape = tuple(tif.series[0].shape)
    assert shape == tuple(widget.mask_info.shape), (shape, widget.mask_info.shape)
    assert len(empty) == widget.mask_info.n_planes - 3
    size_mb = out.stat().st_size / 1e6
    print(f"ok   exported {shape} gated mask, zlib, {size_mb:.0f} MB "
          f"({len(empty)} ungated planes written empty)")

    # --- the plugin opens the way the Plugins menu opens it ---------------------
    # This is the path that broke: napari injects the viewer only into *class*
    # contributions, and passes nothing at all to a function, so a factory function
    # died with "missing 1 required positional argument". Exercise the real command.
    from napari._qt._qplugins._qnpe2 import _get_widget_viewer_param
    from npe2 import PluginManifest

    manifest = PluginManifest.from_distribution("napari-multiplex-thresholder")
    contributed = manifest.contributions.commands[0].python_name
    module_name, _, attr = contributed.partition(":")
    import importlib

    contribution = getattr(importlib.import_module(module_name), attr)
    injected = _get_widget_viewer_param(contribution, "Multiplex Thresholder")
    assert injected, (
        f"napari would call {contributed} with no viewer — the contribution must be a "
        f"class whose __init__ takes `napari_viewer`"
    )
    print(f"ok   napari injects the viewer into {contributed} as {injected!r}")

    from napari._app_model import get_app_model

    docks_before = set(viewer.window.dock_widgets)   # public since napari 0.8
    get_app_model().commands.execute_command("napari-multiplex-thresholder.gating_controls").result()
    QApplication.processEvents()  # let the deferred KDE dock land
    added = set(viewer.window.dock_widgets) - docks_before
    assert added, "executing the plugin command added no dock"
    print(f"ok   Plugins menu command added: {sorted(added)}")

    import napari_multiplex_thresholder

    entry = napari_multiplex_thresholder.make_gating_widget(viewer)
    assert entry.__class__.__name__ == "GatingControls" and entry.kde is not None
    print("ok   make_gating_widget() still works for scripted use")

    # --- the KDE dock actually drew something; save it so it can be eyeballed ---
    assert kde.ax.lines, "KDE axes have no curve"
    png = out_dir / f"kde_{stem}.png"
    kde.figure.savefig(png, dpi=110, facecolor=kde.bg)
    print(f"ok   KDE rendered ({len(kde.ax.lines)} lines incl. threshold) -> {png}")

    peak, high_water = rss_gb(), peak_gb()
    print(f"\nmemory (current RSS): {baseline:.2f} GB at start, {after_load:.2f} GB after Load, "
          f"{steady:.2f} GB after walking channels, {peak:.2f} GB at the end; "
          f"high-water {high_water:.2f} GB (transient int64 page decodes, plus this "
          f"test's own full-plane copies and the export)")
    print(f"        an eager implementation would hold {full_stack_gb:.1f} GB for this tile's mask alone, "
          f"plus a second copy")
    viewer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
