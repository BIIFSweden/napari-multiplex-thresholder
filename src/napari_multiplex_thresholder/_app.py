"""The double-clickable application: napari with both gating docks already open.

Three ways in, one code path:

    Multiplex Thresholder.app / .exe      # the frozen bundle (app/MultiplexThresholder.spec)
    multiplex-thresholder                 # console script from a pip install
    python -m napari_multiplex_thresholder

The app deliberately builds the widget *directly* rather than through napari's plugin
menu. npe2 discovery reads `importlib.metadata` entry points, which only exist in a
frozen bundle if the `.dist-info` directories were copied into it; instantiating the
class needs none of that, so the app opens even if the metadata copy regresses. The
manifest is still bundled, so the menu entry works too — `--self-test` checks both, and
reports the difference instead of hiding it.

`--self-test` is what CI runs on all four platforms. It builds the whole GUI, then
exercises the three things that break silently when frozen and *only* when frozen:

  * the matplotlib Qt backend (the KDE dock's canvas),
  * imagecodecs' compiled decoders, by writing and reading back a zlib TIFF,
  * npe2 entry-point discovery, i.e. whether the Plugins menu will be populated.

It never opens a window and always exits non-zero on failure.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

APP_NAME = "Multiplex Thresholder"
CONTROLS_DOCK_NAME = APP_NAME


def _user_dir(kind: str) -> Path:
    """Per-user `logs` or `cache` directory, by platform convention."""
    if sys.platform == "darwin":
        return (Path.home() / "Library" / ("Logs" if kind == "logs" else "Caches")) / "MultiplexThresholder"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MultiplexThresholder"
        return base / ("Logs" if kind == "logs" else "Cache")
    if kind == "logs":
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "multiplex-thresholder"


def _use_persistent_matplotlib_cache() -> None:
    """Undo PyInstaller's per-launch matplotlib config directory.

    PyInstaller ships a matplotlib runtime hook that points `MPLCONFIGDIR` at a fresh
    temp directory and deletes it again at exit, so every launch paid several seconds to
    rebuild the font cache ("Matplotlib is building the font cache", on a stderr a
    double-clicked app does not have). Its reason is a `--onefile` bundle, which unpacks
    to a new path each time and would leave the cached font paths dangling.

    This bundle is one-*dir*, so its paths are stable and the cache can be kept — but it
    is keyed by where the bundle currently is, so moving the app to /Applications rebuilds
    the cache once instead of leaving it pointing into the old location.

    Assigns rather than `setdefault`s, because PyInstaller's hook has already run and set
    it; an `MPLCONFIGDIR` from outside the temp directory is somebody's deliberate choice
    and is left alone.
    """
    if not getattr(sys, "frozen", False):
        return
    import hashlib
    import tempfile

    current = os.environ.get("MPLCONFIGDIR")
    if current and not current.startswith(tempfile.gettempdir()):
        return
    try:
        key = hashlib.sha256(str(Path(sys.executable).resolve().parent).encode()).hexdigest()[:8]
        target = _user_dir("cache") / f"matplotlib-{key}"
        target.mkdir(parents=True, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = str(target)
    except OSError:
        pass  # a read-only home is not a reason to refuse to start


def crash_log_path() -> Path:
    """Where a windowed build writes a traceback nobody would otherwise see.

    A double-clicked `.app` has no stdout and a windowed `.exe` has no console, so an
    import-time failure in the bundle would otherwise be a bounce with no explanation.
    """
    return _user_dir("logs") / "last-crash.log"


def _report_crash(exc: BaseException) -> Path | None:
    """Write the traceback where support can ask for it, and say where that was."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    sys.stderr.write(text)
    try:
        path = crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        import datetime

        header = f"{datetime.datetime.now().isoformat(timespec='seconds')}  {environment_summary()}\n\n"
        path.write_text(header + text, encoding="utf-8")
    except Exception:  # noqa: BLE001 — a crash report must not crash
        return None
    sys.stderr.write(f"\nwritten to {path}\n")
    try:  # a bare .app user never sees stderr, so try for a dialog as well
        from qtpy.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv[:1])
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(f"{APP_NAME} could not start")
        box.setText(f"{type(exc).__name__}: {exc}")
        box.setDetailedText(text)
        box.setInformativeText(f"Details written to:\n{path}")
        box.exec()
        del app
    except Exception:  # noqa: BLE001 — no Qt is exactly the case that got us here
        pass
    return path


def environment_summary() -> str:
    frozen = getattr(sys, "frozen", False)
    return (
        f"{APP_NAME} | python {sys.version.split()[0]} | {sys.platform} | "
        f"frozen={bool(frozen)} | exe={sys.executable}"
    )


# -----------------------------------------------------------------------------
# self-test
# -----------------------------------------------------------------------------


def _check_plane_reading() -> str:
    """Round-trip a zlib TIFF through the same reader the widget uses.

    This is the check that catches a bundle missing imagecodecs' compiled decoders:
    `tifffile` imports fine without them and only fails at decode time, i.e. the first
    time a user picks a tile.
    """
    import tempfile

    import numpy as np
    import tifffile

    from . import _core

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "selftest_mask.tif"
        stack = np.arange(3 * 8 * 10, dtype="int64").reshape(3, 8, 10)
        tifffile.imwrite(path, stack, compression="zlib", photometric="minisblack")
        source = _core.PlaneSource(path)
        plane = source.read(2)
        if not np.array_equal(plane, stack[2]):
            raise AssertionError("zlib TIFF plane read back wrong")
        return f"read plane 2 of {source.shape} {source.dtype} through {'pages' if source._pages_are_planes else 'zarr'}"


def _check_manifest() -> str:
    """Is the npe2 manifest discoverable — i.e. will the Plugins menu be populated?"""
    from npe2 import PluginManifest

    mf = PluginManifest.from_distribution("napari-multiplex-thresholder")
    widgets = [w.display_name for w in mf.contributions.widgets]
    return f"{mf.name} -> {widgets}"


def self_test() -> int:
    """Build everything, verify it, tear it down. No window, no event loop."""
    print(environment_summary(), flush=True)
    failures: list[str] = []

    import napari

    print(f"napari {napari.__version__}", flush=True)

    viewer = napari.Viewer(title=APP_NAME, show=False)
    try:
        from ._widget import KDE_DOCK_NAME, make_gating_widget

        # make_gating_widget, not GatingControls: it docks the KDE straight away
        # instead of on the next event-loop turn, and there is no loop here.
        controls = make_gating_widget(viewer)
        viewer.window.add_dock_widget(controls, area="right", name=CONTROLS_DOCK_NAME)

        # `dock_widgets` since napari 0.8; `_dock_widgets` warns but still works, and
        # older napari has only that.
        docks = list(getattr(viewer.window, "dock_widgets", None) or getattr(viewer.window, "_dock_widgets", {}))
        print(f"docks: {docks}", flush=True)
        for expected in (CONTROLS_DOCK_NAME, KDE_DOCK_NAME):
            if expected not in docks:
                failures.append(f"dock missing: {expected!r} (have {docks})")

        # The KDE canvas exists only if matplotlib's Qt backend came along.
        if getattr(controls.kde, "canvas", None) is None:
            failures.append("KDE dock has no matplotlib canvas")
        else:
            print(f"matplotlib canvas: {type(controls.kde.canvas).__name__}", flush=True)

        for label, check in (("plane reading", _check_plane_reading), ("npe2 manifest", _check_manifest)):
            try:
                print(f"{label}: {check()}", flush=True)
            except Exception as exc:  # noqa: BLE001 — collect every failure, not the first
                failures.append(f"{label}: {type(exc).__name__}: {exc}")
                traceback.print_exc()
    finally:
        viewer.close()

    if failures:
        print("\nSELF-TEST FAILED", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        return 1
    print("\nSELF-TEST OK", flush=True)
    return 0


# -----------------------------------------------------------------------------
# entry point
# -----------------------------------------------------------------------------


def run(smoke_seconds: float | None = None) -> int:
    """Open the viewer with both docks and hand over to the Qt event loop.

    `smoke_seconds` closes the window again after that long. `--self-test` never enters
    the event loop, so this is the only check that covers `napari.run()` itself — worth
    having, because a frozen Qt app can build every widget correctly and still fail the
    moment it is asked to show a window.
    """
    import napari

    viewer = napari.Viewer(title=APP_NAME)
    from ._widget import GatingControls

    controls = GatingControls(viewer)
    # GatingControls adds the KDE dock itself, one event-loop turn later, so that it
    # lands beneath this one rather than while napari is still docking it.
    viewer.window.add_dock_widget(controls, area="right", name=CONTROLS_DOCK_NAME)

    if smoke_seconds:
        from qtpy.QtCore import QTimer

        print(f"{environment_summary()}\nwindow open, closing in {smoke_seconds:g} s", flush=True)
        QTimer.singleShot(int(smoke_seconds * 1000), viewer.close)

    napari.run()
    if smoke_seconds:
        print("SMOKE OK — the window opened and the event loop ran", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    # Frozen builds re-execute this file in every spawned child process; without this
    # the app would open a second window per worker instead of returning to the parent.
    import multiprocessing

    multiprocessing.freeze_support()

    # Before matplotlib is imported, which first happens inside KdePlot.__init__, and
    # after every PyInstaller runtime hook has had its say.
    _use_persistent_matplotlib_cache()

    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        from . import __version__

        print(__version__)
        return 0
    if "-h" in args or "--help" in args:
        print(
            f"{APP_NAME}\n\n"
            "  (no arguments)   open the viewer with both gating docks\n"
            "  --self-test      build the GUI headless, verify the bundle, exit\n"
            "  --smoke [SECS]   open the window for real, close it again (default 5 s)\n"
            "  --version        print the version\n\n"
            f"crash log: {crash_log_path()}"
        )
        return 0

    smoke: float | None = None
    if "--smoke" in args:
        after = args[args.index("--smoke") + 1 :]
        try:
            smoke = float(after[0]) if after else 5.0
        except ValueError:
            smoke = 5.0

    try:
        return self_test() if "--self-test" in args else run(smoke)
    except Exception as exc:  # noqa: BLE001 — the outermost frame of a windowed app
        _report_crash(exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
