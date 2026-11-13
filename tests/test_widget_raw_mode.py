"""The "no arcsinh" tick box, driven through the real widget on a synthetic dataset.

Ticked, the slider, value box and plot work in raw mean intensity and Run stores the
raw value; the CSV may then mix spaces, so the sidecar's `raw_space` lists which
(tile, marker) thresholds are raw and is read back when the CSV is reopened. A stored
threshold is shown in whichever space the box currently selects.

Uses tests/test_core.py's fixture, so it needs no study data, and a throwaway QSettings
domain, so it never touches the operator's remembered paths. Runs standalone:
`python tests/test_widget_raw_mode.py`. Not under QT_QPA_PLATFORM=offscreen (see
test_widget_real_data.py).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

os.environ.pop("QT_QPA_PLATFORM", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import CHANNELS, _fixture  # noqa: E402


def main() -> int:
    import napari
    from qtpy.QtCore import QSettings
    from qtpy.QtWidgets import QApplication

    from napari_multiplex_thresholder import _core
    from napari_multiplex_thresholder._widget import DEFAULT_SUFFIXES, GatingControls, KdePlot

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tiles, quant, masks = _fixture(root)
        csv_path = root / "manual_thresholds_rawtest.csv"
        meta_path = _core.ThresholdTable.meta_path(csv_path)

        viewer = napari.Viewer(show=False)
        app = QApplication.instance()
        widget = GatingControls(viewer, kde=KdePlot(dark=True))
        widget.settings = QSettings("napari-multiplex-thresholder", "widget-tests")
        widget.suffixes = dict(DEFAULT_SUFFIXES)
        for key, value in (("tiles", tiles), ("quant", quant), ("masks", masks),
                           ("thresholds", csv_path), ("export", root)):
            widget.paths[key].set_value(str(value))
        widget.refresh_tiles()
        assert widget.tile_combo.count() == 2, widget.tile_combo.count()
        widget.tile_combo.setCurrentIndex(0)
        widget.load_tile()
        app.processEvents()
        stem = widget.tile.stem
        widget._select_channel(1)  # first marker; plane 0 is the DAPI slot
        channel = widget.channel
        assert channel == CHANNELS[0], channel
        raw_values = widget.quant.column(channel)
        lo_raw, hi_raw = float(np.nanmin(raw_values)), float(np.nanmax(raw_values))

        # --- off (default): arcsinh space, cofactor live -----------------------------
        box = widget.no_arcsinh_box
        assert box.text() == "no arcsinh" and not box.isChecked()
        assert widget.cofactor_box.isEnabled()
        assert widget.thr_box.title() == "arcsinh threshold"
        assert abs(widget.value_box.maximum() - np.arcsinh(hi_raw)) < 1e-3
        assert "arcsinh" in widget.kde.ax.get_xlabel()
        print("ok   default is arcsinh: title, range and plot axis all say so")

        # --- on: raw space everywhere ------------------------------------------------
        box.setChecked(True)
        app.processEvents()
        assert widget.raw_mode and not widget.cofactor_box.isEnabled()
        assert widget.thr_box.title() == "intensity threshold"
        assert abs(widget.value_box.maximum() - hi_raw) < 1e-3, (widget.value_box.maximum(), hi_raw)
        assert abs(widget.value_box.minimum() - lo_raw) < 1e-3, (widget.value_box.minimum(), lo_raw)
        xlabel = widget.kde.ax.get_xlabel()
        assert xlabel.startswith("mean intensity") and "raw" in xlabel, xlabel
        assert "raw" in widget.range_label.text()
        print(f"ok   ticked: range {lo_raw:.1f}..{hi_raw:.1f} raw, cofactor greyed, plot axis raw")

        # --- Run stores the raw value and flags it in the sidecar --------------------
        widget.value_box.setValue(float(np.nanmedian(raw_values)))
        thr_raw = float(widget.value_box.value())
        widget.apply_threshold()
        app.processEvents()
        csv = pd.read_csv(csv_path, index_col=0)
        assert csv.at[channel, stem] == thr_raw, (csv.at[channel, stem], thr_raw)
        meta = json.loads(meta_path.read_text())
        assert meta["raw_space"] == [[stem, channel]], meta["raw_space"]
        assert "raw_space_transform" in meta
        lut = widget.labels.lut(widget.channel_index)
        kept = {int(l) for l, v in zip(widget.quant.labels, raw_values) if v >= thr_raw}
        assert {i for i in range(len(lut)) if lut[i]} == kept, "LUT must follow the raw cut"
        n_hidden = int(np.sum(~(raw_values >= thr_raw)))
        print(f"ok   Run stored raw {thr_raw:.1f}; sidecar flags ({stem}, {channel}); "
              f"{len(kept)} cells kept, {n_hidden} hidden")

        # --- untick: the stored raw value is shown converted, CSV untouched ----------
        box.setChecked(False)
        app.processEvents()
        shown = float(widget.value_box.value())
        assert abs(shown - np.arcsinh(thr_raw)) < 2e-4, (shown, np.arcsinh(thr_raw))
        assert pd.read_csv(csv_path, index_col=0).at[channel, stem] == thr_raw
        print(f"ok   unticked: the same threshold reads {shown:.4f} in arcsinh space, CSV still raw")

        # --- gate another marker in arcsinh space: the CSV mixes, the sidecar says which
        widget._select_channel(2)
        other = widget.channel
        other_vals = widget.transformed(other)
        widget.value_box.setValue(float(np.nanmedian(other_vals)))
        thr_arc = float(widget.value_box.value())
        widget.apply_threshold()
        csv = pd.read_csv(csv_path, index_col=0)
        meta = json.loads(meta_path.read_text())
        assert csv.at[other, stem] == thr_arc and csv.at[channel, stem] == thr_raw
        assert meta["raw_space"] == [[stem, channel]]
        print(f"ok   mixed CSV: {channel} raw {thr_raw:.1f}, {other} arcsinh {thr_arc:.4f}; "
              f"sidecar lists only {channel}")

        # --- reopen: flags come back from the sidecar, raw value shown raw again -----
        widget.load_tile()
        app.processEvents()
        assert widget.raw_pairs == {(stem, channel)}, widget.raw_pairs
        widget._select_channel(1)
        box.setChecked(True)
        app.processEvents()
        assert abs(float(widget.value_box.value()) - thr_raw) < 1e-3, widget.value_box.value()
        print("ok   reloaded: raw flag restored from the sidecar, raw value shown raw")

        # --- re-Run unticked rewrites in arcsinh space and drops the flag ------------
        box.setChecked(False)
        app.processEvents()
        widget.apply_threshold()
        csv = pd.read_csv(csv_path, index_col=0)
        meta = json.loads(meta_path.read_text())
        assert abs(csv.at[channel, stem] - np.arcsinh(thr_raw)) < 2e-4
        assert meta["raw_space"] == [], meta["raw_space"]
        print("ok   Run with the box off stores arcsinh and clears the flag")

        # --- Un-gate removes value and flag ------------------------------------------
        box.setChecked(True)
        app.processEvents()
        widget.apply_threshold()
        assert json.loads(meta_path.read_text())["raw_space"] == [[stem, channel]]
        widget.clear_threshold()
        assert np.isnan(pd.read_csv(csv_path, index_col=0).at[channel, stem])
        assert json.loads(meta_path.read_text())["raw_space"] == []
        print("ok   Un-gate: NaN in the CSV and no raw flag")

        viewer.close()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
