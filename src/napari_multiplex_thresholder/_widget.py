"""The two dock widgets: gating controls, and the KDE plot.

Dock 1 (`GatingControls`), top to bottom:

    paths            tiles / quantification CSVs / combined masks / thresholds CSV
                     (+ an optional folder for exported gated masks)
    tile             dropdown + Load (the list re-reads whenever a path changes)
    channel          dropdown + Back / Next, two-way bound to napari's channel slider
    threshold        arcsinh slider, exact value and cofactor on one line, Run below

Dock 2 (`KdePlot`) shows the distribution for the current (tile, channel) with the
threshold drawn on it.

Run applies the threshold the way an interactive notebook widget would — cells whose
`arcsinh(mean intensity)` is below it disappear from the label layer — and writes
the value into the thresholds CSV in the layout the downstream analysis expects.
Six differences from that earlier approach are deliberate, and each one fixes a bug
it had: the slider range is per (tile, channel) rather than `arcsinh(n_cells)`,
ungated channels stay NaN instead of 0, files are matched on stem, the CSV is
written atomically, the percentage divides by the cells this channel measured, and
nothing is loaded that is not on screen.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from qtpy.QtCore import QLocale, QSettings, Qt, QTimer
from qtpy.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import _core, _lazy

#: The slider is an integer Qt slider mapped onto the channel's value range. Its
#: own resolution, rather than a declared float `step` that the widget stack may or
#: may not honour — magicgui/superqt ignored the float `step: 1.0` an earlier widget
#: declared, and silently used its own resolution instead.
SLIDER_STEPS = 2000

#: The value and cofactor boxes sit on the slider's line, so they are sized to their
#: content (4 decimals of an arcsinh value, and a small cofactor) rather than stretched.
VALUE_BOX_WIDTH = 90
COFACTOR_BOX_WIDTH = 66

#: The ◀ ▶ channel steppers are `QPushButton`s, not `QToolButton`s. Under napari's theme
#: a QToolButton gets no frame and — measured by grabbing the widget and comparing pixels
#: — *no pressed state at all*: clicking one changed nothing on screen. A QPushButton
#: inherits the same border, hover and pressed styling as every other button in the app,
#: so the arrows now look and behave like napari's own (delete-layer and friends).
#: Fixed width, because a themed QPushButton's padding would otherwise make two arrows
#: as wide as the dropdown; the spacing keeps them off the combo box.
STEP_BUTTON_WIDTH = 34
STEP_BUTTON_SPACING = 6

#: Numbers are shown and typed the way they appear in the CSV: '.' decimals. Under a
#: Swedish or German locale Qt renders 9.31 as "9,31" and can reject a typed '.',
#: which for a threshold file that other code parses as floats is a real hazard.
C_LOCALE = QLocale(QLocale.Language.C)

KDE_DOCK_NAME = "Multiplex thresholder — KDE"

SETTINGS_ORG = "SciLifeLab-BIIF"
SETTINGS_APP = "napari-multiplex-thresholder"

PATH_FIELDS = [
    ("tiles", "Raw tiles", "dir", "Folder with the per-tissue tiles (*.tif)."),
    ("quant", "Quantification CSVs", "dir", "Folder with *_quant.csv, one per tile."),
    ("masks", "Multilayer masks", "dir", "Folder with *_entire_mask.tif, one per tile."),
    ("thresholds", "Thresholds CSV", "file",
     "Where thresholds are written. An existing file is loaded and extended, so "
     "gating can be resumed. Layout matches what the downstream analysis reads."),
    ("export", "Gated mask output", "dir",
     "Only needed for 'Export gated mask'. Left empty, export is disabled."),
]


def widen_to_decimals(lo: float, hi: float, decimals: int) -> tuple[float, float]:
    """Grow [lo, hi] outwards to the spin box's precision.

    `QDoubleSpinBox.setRange` rounds to `decimals`, which can pull the bounds
    *inside* the channel's real range — the highest-intensity cell then sits above
    a maximum the operator cannot reach, and the lowest can be excluded at the
    minimum. Rounding out instead of to-nearest keeps the whole channel reachable.
    """
    import math

    step = 10.0 ** -decimals
    return math.floor(lo / step) * step, math.ceil(hi / step) * step


class PathRow(QWidget):
    """Line edit plus a browse button."""

    def __init__(self, mode: str, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.line = QLineEdit()
        self.line.setMinimumWidth(360)
        button = QToolButton()
        button.setText("…")
        button.clicked.connect(self._browse)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.line)
        row.addWidget(button)

    def _browse(self) -> None:
        start = self.value() or str(Path.home())
        if not Path(start).is_dir():
            start = str(Path(start).parent)
        if self.mode == "dir":
            picked = QFileDialog.getExistingDirectory(self, "Select folder", start)
        else:
            picked, _ = QFileDialog.getSaveFileName(
                self, "Thresholds CSV", start, "CSV (*.csv)"
            )
        if picked:
            self.line.setText(picked)
            self.line.setCursorPosition(0)
            # Same signal a typed edit ends with, so whoever listens re-reads the
            # folder after a Browse as well.
            self.line.editingFinished.emit()

    def value(self) -> str:
        return self.line.text().strip()

    def set_value(self, text: str) -> None:
        self.line.setText(text or "")
        self.line.setCursorPosition(0)  # show the start of a long path, not its tail


class KdePlot(QWidget):
    """Dock 2: the intensity distribution for the current (tile, channel).

    The curve is computed once per (tile, channel) and cached; moving the threshold
    only moves the vertical line. An earlier version recomputed a seaborn KDE over
    every value on each callback, which is where its lag came from.
    """

    def __init__(self, dark: bool = True, parent=None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
        from matplotlib.figure import Figure

        self.dark = dark
        self.fg = "#e0e0e0" if dark else "#202020"
        self.bg = "#262930" if dark else "#ffffff"

        self.figure = Figure(figsize=(4, 3), tight_layout=True, facecolor=self.bg)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.ax = self.figure.add_subplot(111)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self.canvas)

        self._cache: dict[tuple[str, str, float], tuple[np.ndarray, np.ndarray]] = {}
        self.clear("Load a tile to see its distribution")

    # --- drawing ---

    def _style(self) -> None:
        self.ax.set_facecolor(self.bg)
        for spine in ("top", "right"):
            self.ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            self.ax.spines[spine].set_color(self.fg)
        self.ax.tick_params(colors=self.fg, labelsize=8)
        self.ax.xaxis.label.set_color(self.fg)
        self.ax.yaxis.label.set_color(self.fg)
        self.ax.title.set_color(self.fg)

    def clear(self, message: str = "") -> None:
        self.ax.clear()
        self._style()
        if message:
            self.ax.text(
                0.5, 0.5, message, ha="center", va="center",
                transform=self.ax.transAxes, color=self.fg, fontsize=9,
            )
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.canvas.draw_idle()

    def curve(self, key, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Cached density estimate. Falls back to a histogram if a KDE is degenerate."""
        if key in self._cache:
            return self._cache[key]

        finite = values[np.isfinite(values)]
        if finite.size < 5 or np.allclose(finite, finite[0]):
            self._cache[key] = (np.array([]), np.array([]))
            return self._cache[key]

        sample = finite
        if sample.size > 20000:  # a KDE of 20 k points is visually identical and quick
            rng = np.random.default_rng(0)  # fixed seed: the plot must not flicker
            sample = rng.choice(finite, 20000, replace=False)

        grid = np.linspace(finite.min(), finite.max(), 512)
        try:
            from scipy.stats import gaussian_kde

            density = gaussian_kde(sample)(grid)
        except Exception:
            counts, edges = np.histogram(finite, bins=128, density=True)
            grid = 0.5 * (edges[:-1] + edges[1:])
            density = counts
        self._cache[key] = (grid, density)
        return self._cache[key]

    def show_channel(
        self,
        stem: str,
        channel: str,
        transformed: np.ndarray,
        threshold: float,
        cofactor: float,
        gated: bool,
    ) -> None:
        grid, density = self.curve((stem, channel, cofactor), transformed)
        self.ax.clear()
        self._style()

        if grid.size:
            self.ax.plot(grid, density, color=self.fg, linewidth=1.2)
            self.ax.fill_between(grid, density, color=self.fg, alpha=0.12)
        else:
            self.ax.text(
                0.5, 0.5, "not enough measured cells", ha="center", va="center",
                transform=self.ax.transAxes, color=self.fg, fontsize=9,
            )

        pct = _core.positive_fraction(transformed, threshold)
        if np.isfinite(threshold):
            self.ax.axvline(threshold, color="#ff5555", linestyle="--", linewidth=1.2)

        measured = int(np.isfinite(transformed).sum())
        cof = "" if cofactor == 1 else f" / {cofactor:g}"
        state = "gated" if gated else "not gated yet"
        self.ax.set_title(
            f"{channel} — {pct:.2f}% positive of {measured:,} measured  ({state})",
            fontsize=9,
        )
        self.ax.set_xlabel(f"arcsinh(mean intensity{cof})", fontsize=8)
        self.ax.set_ylabel("density", fontsize=8)
        self.canvas.draw_idle()


class GatingControls(QWidget):
    """Dock 1: paths, tile, channel, threshold.

    This class *is* the npe2 widget contribution, and the first parameter must stay
    named `napari_viewer`: that is how napari decides to inject the viewer
    (`_get_widget_viewer_param` accepts a parameter with that name, or one annotated
    `napari.viewer.Viewer`). A factory *function* gets no arguments at all, which is
    why one no longer serves as the entry point.

    `kde` is the second dock. Passing one in — as the tests do — keeps construction
    free of side effects; leaving it None makes this build its own and add it to the
    viewer, which is what happens when the plugin is opened from the Plugins menu.
    """

    def __init__(self, napari_viewer, kde: KdePlot | None = None, parent=None):
        super().__init__(parent)
        self.viewer = napari_viewer
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self._owns_kde = kde is None
        self.kde = kde if kde is not None else KdePlot(dark=self._viewer_is_dark())

        # loaded state
        self.tiles: list[_core.TileRef] = []
        self.tile: _core.TileRef | None = None
        self.quant: _core.Quantification | None = None
        self.mask_info: _core.MaskInfo | None = None
        self.labels: _lazy.LazyGatedLabels | None = None
        self.image: _lazy.LazyImage | None = None
        self.table: _core.ThresholdTable | None = None
        self.image_layer = None
        self.labels_layer = None
        self._display: np.ndarray | None = None  # the 2D buffer napari aliases
        self._transformed: dict[str, np.ndarray] = {}
        self._syncing = False
        self._loading = False

        self._build()
        self._restore_paths()
        self.viewer.dims.events.current_step.connect(self._on_dims_changed)

        if self._owns_kde:
            # Deferred by one event-loop turn so napari finishes docking *this*
            # widget first and the KDE lands beneath it, rather than being added
            # while napari is still in the middle of adding dock 1.
            QTimer.singleShot(0, self._add_kde_dock)

    def _viewer_is_dark(self) -> bool:
        return str(getattr(self.viewer, "theme", "dark")).lower() != "light"

    def _add_kde_dock(self) -> None:
        """Put dock 2 in the viewer. Idempotent, and never fatal."""
        window = getattr(self.viewer, "window", None)
        if window is None or self.kde is None:
            return
        try:
            window.add_dock_widget(self.kde, area="right", name=KDE_DOCK_NAME)
        except Exception:  # noqa: BLE001 — the controls are still usable without it
            pass

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        # --- paths ---
        box = QGroupBox("Paths")
        form = QFormLayout(box)
        self.paths: dict[str, PathRow] = {}
        for key, label, mode, tip in PATH_FIELDS:
            row = PathRow(mode)
            row.setToolTip(tip)
            # editingFinished, not textChanged: re-listing three directories on every
            # keystroke is wasteful, and on the external drive it is slow. This fires
            # on Enter or focus loss, and PathRow emits it after a Browse too.
            row.line.editingFinished.connect(self._on_path_changed)
            self.paths[key] = row
            tag = QLabel(label)
            tag.setToolTip(tip)
            form.addRow(tag, row)
        outer.addWidget(box)

        # --- tile ---
        tile_box = QGroupBox("Tile")
        tile_layout = QVBoxLayout(tile_box)
        self.tile_combo = QComboBox()
        self.tile_combo.setToolTip(
            "Tiles that have all three files (tile, quantification, mask), matched on "
            "filename stem. The list refreshes whenever a path above changes."
        )
        self.load_button = QPushButton("Load")
        self.load_button.setToolTip("Open the tile, its mask and its quantification.")
        self.load_button.clicked.connect(self.load_tile)
        tile_row = QHBoxLayout()
        tile_row.addWidget(self.tile_combo, 1)
        tile_row.addWidget(self.load_button)
        tile_layout.addLayout(tile_row)
        self.tile_status = QLabel("")
        self.tile_status.setWordWrap(True)
        tile_layout.addWidget(self.tile_status)
        outer.addWidget(tile_box)

        # --- channel ---
        channel_box = QGroupBox("Channel")
        channel_grid = QGridLayout(channel_box)
        self.channel_combo = QComboBox()
        self.channel_combo.setToolTip(
            "Marker to gate. Stays in step with the channel slider under the image."
        )
        self.channel_combo.currentIndexChanged.connect(self._on_channel_combo)
        self.back_button = QPushButton("◀")
        self.back_button.setToolTip("Previous channel")
        self.back_button.setFixedWidth(STEP_BUTTON_WIDTH)
        self.back_button.clicked.connect(lambda: self.step_channel(-1))
        self.next_button = QPushButton("▶")
        self.next_button.setToolTip("Next channel")
        self.next_button.setFixedWidth(STEP_BUTTON_WIDTH)
        self.next_button.clicked.connect(lambda: self.step_channel(+1))
        channel_grid.addWidget(self.channel_combo, 0, 0)
        channel_grid.addWidget(self.back_button, 0, 1)
        channel_grid.addWidget(self.next_button, 0, 2)
        channel_grid.setColumnStretch(0, 1)
        channel_grid.setHorizontalSpacing(STEP_BUTTON_SPACING)
        outer.addWidget(channel_box)

        # --- threshold ---
        thr_box = QGroupBox("arcsinh threshold")
        thr_layout = QVBoxLayout(thr_box)

        # slider, exact value and cofactor all on one line
        slider_row = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, SLIDER_STEPS)
        self.slider.setToolTip(
            "Range is this channel's own min..max in arcsinh space, per tile."
        )
        self.slider.valueChanged.connect(self._on_slider)

        self.value_box = QDoubleSpinBox()
        self.value_box.setLocale(C_LOCALE)
        self.value_box.setDecimals(4)
        self.value_box.setSingleStep(0.01)
        self.value_box.setKeyboardTracking(False)
        self.value_box.setFixedWidth(VALUE_BOX_WIDTH)
        self.value_box.setToolTip("The threshold itself. Typing here moves the slider.")
        self.value_box.valueChanged.connect(self._on_value_box)

        self.cofactor_box = QDoubleSpinBox()
        self.cofactor_box.setLocale(C_LOCALE)
        self.cofactor_box.setDecimals(2)
        self.cofactor_box.setRange(0.01, 1000.0)
        self.cofactor_box.setValue(_core.NOTEBOOK_COFACTOR)
        self.cofactor_box.setFixedWidth(COFACTOR_BOX_WIDTH)
        self.cofactor_box.setToolTip(
            "arcsinh(x / cofactor). 1 = the space the downstream analysis works in, "
            "and what previously saved thresholds are expressed in. 5 = the space "
            "asinh-normalised exports and the cell-typing tools reading them use."
        )
        self.cofactor_box.valueChanged.connect(self._on_cofactor)

        cofactor_tag = QLabel("cofactor")
        cofactor_tag.setToolTip(self.cofactor_box.toolTip())
        slider_row.addWidget(self.slider, 1)
        slider_row.addWidget(self.value_box)
        slider_row.addWidget(cofactor_tag)
        slider_row.addWidget(self.cofactor_box)
        thr_layout.addLayout(slider_row)

        self.range_label = QLabel("")
        thr_layout.addWidget(self.range_label)
        self.cofactor_warning = QLabel("")
        self.cofactor_warning.setWordWrap(True)
        thr_layout.addWidget(self.cofactor_warning)

        self.run_button = QPushButton("Run — apply threshold")
        self.run_button.setToolTip(
            "Hide every cell below the threshold in this channel, record the value, and "
            "write the thresholds CSV."
        )
        self.run_button.clicked.connect(self.apply_threshold)
        thr_layout.addWidget(self.run_button)

        small_row = QHBoxLayout()
        self.clear_button = QPushButton("Un-gate channel")
        self.clear_button.setToolTip("Show all cells again and set this channel back to NaN.")
        self.clear_button.clicked.connect(self.clear_threshold)
        self.export_button = QPushButton("Export gated mask")
        self.export_button.setToolTip(
            "Write a (C, H, W) TIFF beside the tiles in which every channel plane keeps "
            "only the cells that passed that channel's threshold — the gated masks as an "
            "actual file, zlib-compressed and streamed one plane at a time. Ungated "
            "channels become empty planes. Nothing downstream needs it: the analysis "
            "re-derives its own thresholded masks from the CSV. It is for QC, for sharing, "
            "and for tools outside this pipeline."
        )
        self.export_button.clicked.connect(self.export_mask)
        small_row.addWidget(self.clear_button)
        small_row.addWidget(self.export_button)
        thr_layout.addLayout(small_row)

        outer.addWidget(thr_box)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        outer.addStretch(1)

        self._set_enabled(False)

    def _set_enabled(self, loaded: bool) -> None:
        for widget in (
            self.channel_combo, self.back_button, self.next_button, self.slider,
            self.value_box, self.run_button, self.clear_button, self.export_button,
        ):
            widget.setEnabled(loaded)

    # ------------------------------------------------------------- path memory

    def _restore_paths(self) -> None:
        for key, row in self.paths.items():
            row.set_value(str(self.settings.value(f"paths/{key}", "") or ""))
        if self.paths["tiles"].value():
            self.refresh_tiles()

    def _save_paths(self) -> None:
        for key, row in self.paths.items():
            self.settings.setValue(f"paths/{key}", row.value())

    def _on_path_changed(self) -> None:
        """Any path edited: remember it, and re-list the tiles it could affect."""
        self._save_paths()
        self.refresh_tiles()

    def _path(self, key: str) -> str:
        return self.paths[key].value()

    # ------------------------------------------------------------------ tiles

    def refresh_tiles(self) -> None:
        """Fill the tile dropdown with tiles that have all three files."""
        self._save_paths()
        tiles_dir, quant_dir, mask_dir = self._path("tiles"), self._path("quant"), self._path("masks")
        if not all((tiles_dir, quant_dir, mask_dir)):
            self.tile_status.setText("Set the tiles, quantification and mask folders first.")
            return

        self.tiles, problems = _core.discover_tiles(tiles_dir, quant_dir, mask_dir)
        self.tile_combo.clear()
        self.tile_combo.addItems([t.stem for t in self.tiles])
        message = f"{len(self.tiles)} tile(s) ready."
        if problems:
            message += f" Skipped {len(problems)}: " + "; ".join(problems[:3])
            if len(problems) > 3:
                message += f" (+{len(problems) - 3} more)"
        self.tile_status.setText(message)

    def load_tile(self) -> None:
        """Open the selected tile: quantification, mask geometry, lazy layers."""
        if not self.tiles:
            self.refresh_tiles()
        index = self.tile_combo.currentIndex()
        if not (0 <= index < len(self.tiles)):
            QMessageBox.warning(self, "No tile", "Select a tile first.")
            return
        if not self._path("thresholds"):
            QMessageBox.warning(
                self, "No thresholds CSV",
                "Set a path for the thresholds CSV — it is where Run records values.",
            )
            return

        tile = self.tiles[index]
        try:
            quant = _core.load_quantification(tile.quant, tile.stem)
            info = _core.inspect_mask(tile.mask, len(quant.channels))
            labels = _lazy.LazyGatedLabels(tile.mask, len(quant.channels), info.plane_offset)
            image = _lazy.LazyImage(tile.tile, drop_first=info.plane_offset == 1)
        except Exception as exc:  # noqa: BLE001 — surface anything to the operator
            QMessageBox.critical(self, "Could not load tile", f"{type(exc).__name__}: {exc}")
            return

        if image.n_channels != len(quant.channels):
            QMessageBox.warning(
                self, "Channel count mismatch",
                f"The tile has {image.n_channels} channels after dropping DAPI but the "
                f"quantification has {len(quant.channels)}. Continuing with the "
                f"quantification's channels.",
            )

        self.tile, self.quant, self.mask_info = tile, quant, info
        self.labels, self.image = labels, image
        self._transformed = {}
        self.table = _core.ThresholdTable.load(
            self._path("thresholds"), quant.channels, [t.stem for t in self.tiles]
        )

        self._add_layers()

        self._syncing = True
        self.channel_combo.clear()
        self.channel_combo.addItems(quant.channels)
        self._syncing = False

        self._set_enabled(True)
        self.tile_status.setText(
            f"{tile.stem}: {quant.n_cells:,} cells, {len(quant.channels)} channels, "
            f"mask {info.shape} {info.dtype} (plane {info.plane_offset} = first marker)"
        )
        self._select_channel(0)

    def _add_layers(self) -> None:
        """Replace this widget's layers with the loaded tile's.

        Guarded: removing a layer makes napari call `dims.reset()`, which emits
        `current_step` and re-enters `_on_dims_changed` — with `self.labels` already
        pointing at the new tile while `self._display` is still the old tile's buffer.
        That crashed with a broadcast error on the second Load.
        """
        self._loading = True
        try:
            self._swap_layers()
        finally:
            self._loading = False

    def _swap_layers(self) -> None:
        self._display = None  # nothing may write into the old buffer from here on
        for layer in (self.image_layer, self.labels_layer):
            if layer is not None and layer in self.viewer.layers:
                self.viewer.layers.remove(layer)
        self.image_layer = self.labels_layer = None

        name = self.tile.stem
        # The raw tile stays a lazy (C, H, W) dask stack: that is what gives the canvas
        # its channel slider, and napari re-slices it correctly when the slider moves.
        self.image_layer = self.viewer.add_image(
            self.image.as_dask(),
            name=f"{name} — raw",
            colormap="gray",
            contrast_limits=self.image.contrast_limits(0),
        )
        # The mask is a single reused 2D buffer holding the current channel, NOT a
        # lazy stack. Two reasons, both measured against napari 0.8:
        #   * napari caches the slice it rendered, and `refresh()` — even
        #     `refresh(force=True)` — redraws from that cache without re-running a dask
        #     graph, so a threshold applied to the displayed plane never appeared.
        #   * Reassigning `.data` to a fresh dask stack does update the display, but
        #     napari holds on to the superseded slices: 20 Runs on the largest tile grew
        #     resident memory from 4.95 GB to 10.05 GB.
        # napari aliases a numpy array (verified with np.shares_memory), so writing into
        # this buffer in place and calling refresh() shows the gated plane immediately
        # and allocates nothing. Memory stays flat across a whole session.
        self._display = np.array(self.labels.plane(0))
        self.labels_layer = self.viewer.add_labels(
            self._display,
            name=f"{name} — mask",
            opacity=0.5,
            blending="translucent_no_depth",
        )
        # (C, H, W) image: axis 0 is the channel slider under the canvas.
        self.viewer.dims.set_current_step(0, 0)

    # --------------------------------------------------------------- channels

    @property
    def channel_index(self) -> int:
        return max(0, self.channel_combo.currentIndex())

    @property
    def channel(self) -> str | None:
        return self.channel_combo.currentText() or None

    def transformed(self, channel: str) -> np.ndarray:
        """arcsinh-transformed intensities for one channel, cached per cofactor."""
        key = f"{channel}@{self.cofactor_box.value():g}"
        if key not in self._transformed:
            self._transformed[key] = _core.transform(
                self.quant.column(channel), self.cofactor_box.value()
            )
        return self._transformed[key]

    def step_channel(self, delta: int) -> None:
        """Back / Next. Moves the dropdown, which moves napari's slider."""
        if self.quant is None:
            return
        target = self.channel_index + delta
        if 0 <= target < self.channel_combo.count():
            self._select_channel(target)

    def _select_channel(self, index: int) -> None:
        self._syncing = True
        self.channel_combo.setCurrentIndex(index)
        self.viewer.dims.set_current_step(0, index)
        self._syncing = False
        self._refresh_for_channel()

    def _on_channel_combo(self, index: int) -> None:
        if self._syncing or self.quant is None or index < 0:
            return
        self._syncing = True
        self.viewer.dims.set_current_step(0, index)
        self._syncing = False
        self._refresh_for_channel()

    def _on_dims_changed(self, event=None) -> None:
        """Dragging napari's channel slider moves the dropdown with it."""
        if self._syncing or self._loading or self.quant is None:
            return
        steps = self.viewer.dims.current_step
        if not steps:
            return
        index = int(steps[0])
        if index == self.channel_combo.currentIndex() or not (
            0 <= index < self.channel_combo.count()
        ):
            return
        self._syncing = True
        self.channel_combo.setCurrentIndex(index)
        self._syncing = False
        self._refresh_for_channel()

    def _refresh_for_channel(self) -> None:
        """Slider range, stored value, contrast limits and KDE for the current channel."""
        channel = self.channel
        if channel is None or self.quant is None or self._loading:
            return
        values = self.transformed(channel)
        true_lo, true_hi = _core.channel_range(values)
        lo, hi = widen_to_decimals(true_lo, true_hi, self.value_box.decimals())
        stored = self.table.get(self.tile.stem, channel)
        gated = bool(np.isfinite(stored))
        value = float(stored) if gated else lo

        self._syncing = True
        self.value_box.setRange(lo, hi)
        self.value_box.setValue(min(max(value, lo), hi))
        self.slider.setValue(self._to_slider(value, lo, hi))
        self._syncing = False
        self._range = (lo, hi)
        self.range_label.setText(f"range {true_lo:.4f} … {true_hi:.4f}")

        # The mask layer is 2D and never re-sliced by napari, so its buffer has to be
        # refilled for the newly selected channel.
        self._redraw_labels()

        if self.image_layer is not None:
            try:
                self.image_layer.contrast_limits = self.image.contrast_limits(
                    self.channel_index
                )
            except Exception:  # noqa: BLE001 — a display nicety, never fatal
                pass

        self._update_kde()
        self._update_status()

    def _redraw_labels(self) -> None:
        """Show the current channel's plane, gated by whatever LUT it has.

        Writes into the buffer napari already aliases, so this is what makes a Run
        visible. Called after every LUT change *and* on every channel change — the
        mask layer is 2D, so unlike the image stack napari will not re-slice it.
        """
        if self.labels_layer is None or self.labels is None or self._display is None:
            return
        try:
            self.labels.write_plane_into(self.channel_index, self._display)
        except ValueError:
            return  # buffer belongs to another tile; a Load is in flight
        self.labels_layer.refresh()

    # -------------------------------------------------------------- threshold

    def _to_slider(self, value: float, lo: float, hi: float) -> int:
        if hi <= lo:
            return 0
        return int(round(SLIDER_STEPS * (min(max(value, lo), hi) - lo) / (hi - lo)))

    def _from_slider(self, position: int) -> float:
        lo, hi = getattr(self, "_range", (0.0, 1.0))
        return lo + (hi - lo) * position / SLIDER_STEPS

    def _on_slider(self, position: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.value_box.setValue(self._from_slider(position))
        self._syncing = False
        self._update_kde()

    def _on_value_box(self, value: float) -> None:
        if self._syncing:
            return
        lo, hi = getattr(self, "_range", (0.0, 1.0))
        self._syncing = True
        self.slider.setValue(self._to_slider(value, lo, hi))
        self._syncing = False
        self._update_kde()

    def _on_cofactor(self, value: float) -> None:
        self.cofactor_warning.setText(
            "" if value == 1 else
            "⚠ the downstream analysis assumes cofactor 1; thresholds saved with another "
            "cofactor are not comparable with previously saved manual_thresholds_*.csv. "
            "The value is recorded in the .meta.json sidecar."
        )
        if self.quant is not None:
            self._transformed = {}
            self._refresh_for_channel()

    def apply_threshold(self) -> None:
        """Run: hide sub-threshold cells in this channel and record the value."""
        if self.labels is None or self.channel is None:
            return
        channel, index = self.channel, self.channel_index
        threshold = float(self.value_box.value())
        values = self.transformed(channel)

        max_label = self.labels.max_label(index)
        lut = _core.keep_lut(self.quant.labels, values, threshold, max_label)
        self.labels.set_lut(index, lut)
        self._redraw_labels()

        self.table.set(self.tile.stem, channel, threshold)
        saved = self.save_thresholds()
        self._update_kde()
        kept = int(lut.sum())
        written = f" · saved to {Path(saved).name}" if saved else " · NOT SAVED"
        self._update_status(
            f"{channel}: threshold {threshold:.4f} → {kept:,} cells kept "
            f"({_core.positive_fraction(values, threshold):.2f}% of measured){written}"
        )

    def clear_threshold(self) -> None:
        """Un-gate this channel: all cells visible again, value back to NaN."""
        if self.labels is None or self.channel is None:
            return
        self.labels.set_lut(self.channel_index, None)
        self._redraw_labels()
        self.table.clear(self.tile.stem, self.channel)
        self.save_thresholds()
        self._refresh_for_channel()
        self._update_status(f"{self.channel}: un-gated (NaN)")

    # ------------------------------------------------------------------ output

    def _meta(self) -> dict:
        return {
            "cofactor": float(self.cofactor_box.value()),
            "transform": "arcsinh(x / cofactor)",
            "tiles_dir": self._path("tiles"),
            "quant_dir": self._path("quant"),
            "masks_dir": self._path("masks"),
        }

    def save_thresholds(self) -> Path | None:
        """Write the CSV and its sidecar. Called by Run and by Un-gate.

        There is no Save button: every Run persists, so quitting napari cannot lose
        gating work. What is still ungated is reported in the status line instead of a
        dialog, which on every Run would be in the way.
        """
        if self.table is None:
            return None
        try:
            return self.table.save(self._path("thresholds"), meta=self._meta())
        except OSError as exc:
            QMessageBox.critical(self, "Could not write thresholds", str(exc))
            return None

    def export_mask(self) -> None:
        """Write a gated (C, H, W) mask, streaming one plane at a time."""
        if self.labels is None or self.tile is None:
            return
        out_dir = self._path("export")
        if not out_dir:
            QMessageBox.warning(
                self, "No output folder",
                "Set 'Gated mask output' to export. The downstream analysis does not need "
                "this file — it re-derives thresholded masks from the CSV.",
            )
            return

        gated = self.labels.gated_channels()
        total = len(self.quant.channels)
        if len(gated) < total:
            answer = QMessageBox.question(
                self, "Export with ungated channels?",
                f"{len(gated)} of {total} channels are gated. The other "
                f"{total - len(gated)} will be written as EMPTY planes.\n\nExport anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        out_path = Path(out_dir) / f"{self.tile.stem}_gated_mask.tif"
        n_planes = self.mask_info.n_planes
        dialog = QProgressDialog("Exporting gated mask…", "Cancel", 0, n_planes, self)
        dialog.setWindowModality(Qt.WindowModal)

        def progress(index: int, total_planes: int) -> None:
            dialog.setValue(index)
            dialog.setLabelText(f"Plane {index + 1} of {total_planes}")

        try:
            written, empty = _core.export_gated_mask(
                self.tile.mask, out_path, self.labels.luts_by_plane(),
                n_planes=n_planes, compression="zlib", progress=progress,
            )
        except Exception as exc:  # noqa: BLE001
            dialog.close()
            QMessageBox.critical(self, "Export failed", f"{type(exc).__name__}: {exc}")
            return
        dialog.setValue(n_planes)

        size_mb = written.stat().st_size / 1e6
        QMessageBox.information(
            self, "Gated mask written",
            f"{written}\n\n{size_mb:.0f} MB, zlib, {n_planes} planes "
            f"({len(empty)} written empty).",
        )

    # ------------------------------------------------------------------ status

    def _update_kde(self) -> None:
        if self.kde is None or self.quant is None or self.channel is None:
            return
        self.kde.show_channel(
            self.tile.stem,
            self.channel,
            self.transformed(self.channel),
            float(self.value_box.value()),
            float(self.cofactor_box.value()),
            gated=self.labels.lut(self.channel_index) is not None,
        )

    def _update_status(self, message: str = "") -> None:
        if self.table is None or self.tile is None:
            self.status.setText(message)
            return
        done, total = self.table.gated_count(self.tile.stem)
        nan_note = ""
        if self.channel is not None:
            missing = self.quant.nan_count(self.channel)
            if missing:
                nan_note = f" · {missing:,} cells unmeasured in this compartment"
        prefix = f"{done}/{total} channels gated for this tile{nan_note}"
        if done < total:
            # The standing version of the warning the Save button used to pop up.
            prefix += (
                f"\n⚠ {total - done} channel(s) still NaN — the downstream analysis will "
                f"call every cell NEGATIVE for those"
            )
        self.status.setText(f"{prefix}\n{message}" if message else prefix)


def make_gating_widget(napari_viewer):
    """Build both docks programmatically (scripts, notebooks, tests).

    Not the npe2 entry point — that is `GatingControls` itself, because napari does
    not pass the viewer to a function contribution. This adds the KDE dock straight
    away instead of on the next event-loop turn, so callers without a running loop
    still get it.
    """
    controls = GatingControls(napari_viewer, kde=KdePlot(dark=str(
        getattr(napari_viewer, "theme", "dark")).lower() != "light"))
    controls._add_kde_dock()
    return controls
