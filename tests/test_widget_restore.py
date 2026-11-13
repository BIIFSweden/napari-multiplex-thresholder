"""Load applies the thresholds a CSV already holds, driven through the real widget.

A thresholds CSV written before the widget is opened — one marker in arcsinh space,
one in raw mean intensity flagged by the sidecar, one left NaN — and then Load. The
gated channels must open gated on the canvas, in the KDE title and in the status
count, without a Run, and Load must not rewrite the file.

Uses tests/test_core.py's fixture, so it needs no study data, and a throwaway QSettings
domain, so it never touches the operator's remembered paths. Runs standalone:
`python tests/test_widget_restore.py`. Not under QT_QPA_PLATFORM=offscreen (see
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
import tifffile

os.environ.pop("QT_QPA_PLATFORM", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import CHANNELS, _fixture  # noqa: E402

#: A mask label with no row in the quantification, above every quantified label.
ORPHAN = 5000


def rendered_labels(layer) -> set[int]:
    """Label IDs napari is displaying (`layer._slice.image.raw`), background dropped.

    The rendered slice and not the source array, for the reason given in
    test_widget_real_data.py: the two have disagreed before.
    """
    try:
        shown = np.asarray(layer._slice.image.raw)
    except AttributeError as exc:  # pragma: no cover
        raise AssertionError(
            "cannot read the rendered slice from this napari version — find the new "
            "accessor rather than dropping the check"
        ) from exc
    return {int(v) for v in np.unique(shown)} - {0}


def main() -> int:
    import napari
    from qtpy.QtCore import QSettings
    from qtpy.QtWidgets import QApplication

    from napari_multiplex_thresholder import _core
    from napari_multiplex_thresholder._widget import DEFAULT_SUFFIXES, GatingControls, KdePlot

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tiles, quant, masks = _fixture(root)
        stems = ["T_tile0", "T_tile1"]
        a, b, c = CHANNELS

        # an object the quantification never measured, in a row the fixture leaves empty
        mask_path = masks / f"{stems[0]}{_core.MASK_SUFFIX}"
        mask = tifffile.imread(mask_path)
        mask[1:, 50, :] = ORPHAN
        tifffile.imwrite(mask_path, mask, compression="zlib")

        q0 = _core.load_quantification(quant / f"{stems[0]}{_core.QUANT_SUFFIX}")
        q1 = _core.load_quantification(quant / f"{stems[1]}{_core.QUANT_SUFFIX}")
        # four decimals, so a Run at the value the box shows is the same threshold
        thr_a = round(float(np.nanmedian(np.arcsinh(q0.column(a)))), 4)  # arcsinh space
        thr_b = round(float(np.nanmedian(q0.column(b))), 4)              # raw space
        thr_c1 = round(float(np.nanmedian(np.arcsinh(q1.column(c)))), 4)

        # --- a CSV and sidecar from "an earlier session", written by hand ------------
        csv_path = root / "manual_thresholds_restoretest.csv"
        frame = pd.DataFrame(np.nan, index=CHANNELS, columns=stems)
        frame.at[a, stems[0]] = thr_a
        frame.at[b, stems[0]] = thr_b
        frame.at[c, stems[1]] = thr_c1
        frame.to_csv(csv_path)
        meta_path = _core.ThresholdTable.meta_path(csv_path)
        meta_path.write_text(json.dumps({"cofactor": 1.0, "raw_space": [[stems[0], b]]}))
        csv_before, meta_before = csv_path.read_bytes(), meta_path.read_bytes()

        keep_a = {int(l) for l, v in zip(q0.labels, np.arcsinh(q0.column(a))) if v >= thr_a}
        keep_b = {int(l) for l, v in zip(q0.labels, q0.column(b)) if v >= thr_b}
        keep_c1 = {int(l) for l, v in zip(q1.labels, np.arcsinh(q1.column(c))) if v >= thr_c1}
        everything = {int(l) for l in q0.labels} | {ORPHAN}
        assert 0 < len(keep_a) < q0.n_cells and 0 < len(keep_b) < q0.n_cells

        viewer = napari.Viewer(show=False)
        app = QApplication.instance()
        widget = GatingControls(viewer, kde=KdePlot(dark=True))
        widget.settings = QSettings("napari-multiplex-thresholder", "widget-tests")
        widget.suffixes = dict(DEFAULT_SUFFIXES)
        for key, value in (("tiles", tiles), ("quant", quant), ("masks", masks),
                           ("thresholds", csv_path), ("export", root)):
            widget.paths[key].set_value(str(value))
        widget.refresh_tiles()
        widget.tile_combo.setCurrentIndex(0)
        widget.load_tile()
        app.processEvents()

        # --- Load alone gates what the CSV holds -------------------------------------
        assert widget.channel == a, widget.channel
        assert widget.labels.gated_channels() == [0, 1], widget.labels.gated_channels()
        assert rendered_labels(widget.labels_layer) == keep_a, (
            "the first marker has a stored threshold and must open gated"
        )
        assert abs(float(widget.value_box.value()) - thr_a) < 1e-9
        assert "not gated" not in widget.kde.ax.get_title(), widget.kde.ax.get_title()
        assert widget.status.text().startswith("2/3"), widget.status.text()
        print(f"ok   Load gated {a} at {thr_a}: {len(keep_a)} of {q0.n_cells} cells shown, "
              f"KDE says gated, status says 2/3")

        # --- Load reads, it does not write -------------------------------------------
        assert csv_path.read_bytes() == csv_before and meta_path.read_bytes() == meta_before
        print("ok   the CSV and its sidecar are byte-identical after Load")

        # --- a raw-space threshold is applied in raw space ---------------------------
        widget._select_channel(2)
        app.processEvents()
        assert widget.channel == b
        assert rendered_labels(widget.labels_layer) == keep_b
        assert ORPHAN not in keep_b and int(q0.labels[0]) not in keep_b  # NaN cell drops
        print(f"ok   {b} (raw, from the sidecar) gated at {thr_b}: {len(keep_b)} cells shown; "
              f"the unmeasured cell and the unquantified object are hidden")

        # --- an ungated channel still shows everything -------------------------------
        widget._select_channel(3)
        app.processEvents()
        assert widget.channel == c and widget.labels.lut(2) is None
        assert rendered_labels(widget.labels_layer) == everything
        assert "not gated" in widget.kde.ax.get_title()
        print(f"ok   {c} has no stored value: all {len(everything)} objects shown, not gated")

        # --- the restored gating is what a Run at the stored value produces ----------
        widget._select_channel(1)
        app.processEvents()
        restored = rendered_labels(widget.labels_layer)
        widget.apply_threshold()
        app.processEvents()
        assert rendered_labels(widget.labels_layer) == restored == keep_a
        assert pd.read_csv(csv_path, index_col=0).at[a, stems[0]] == thr_a
        print("ok   pressing Run at the stored value changes nothing on screen or in the CSV")

        # --- another tile gets its own thresholds, nothing carried over --------------
        widget.tile_combo.setCurrentIndex(1)
        widget.load_tile()
        app.processEvents()
        assert widget.labels.gated_channels() == [2], widget.labels.gated_channels()
        assert rendered_labels(widget.labels_layer) == {int(l) for l in q1.labels}  # MarkerA
        widget._select_channel(3)
        app.processEvents()
        assert rendered_labels(widget.labels_layer) == keep_c1
        assert widget.status.text().startswith("1/3"), widget.status.text()
        print(f"ok   {stems[1]}: only {c} gated, {len(keep_c1)} cells shown")

        # --- Un-gate still clears a restored threshold --------------------------------
        widget.clear_threshold()
        app.processEvents()
        assert widget.labels.gated_channels() == []
        assert rendered_labels(widget.labels_layer) == {int(l) for l in q1.labels}
        assert np.isnan(pd.read_csv(csv_path, index_col=0).at[c, stems[1]])
        print("ok   Un-gate on a restored channel: all cells back, NaN in the CSV")

        viewer.close()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
