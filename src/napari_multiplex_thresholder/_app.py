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

`--self-test` is what CI runs on all four platforms. It never opens a window, and it
separates two kinds of failure that a single check used to conflate:

  * **Bundle checks**, which need no OpenGL and are always fatal when they fail:
    imagecodecs' compiled decoders (by writing and reading back a zlib TIFF), npe2
    entry-point discovery — i.e. whether the Plugins menu will be populated — and
    matplotlib's Qt backend, built as a bare canvas without napari.
  * **The viewer check**, a real napari viewer with both docks, which needs a GL
    context. A machine without one (a CI runner with no GPU driver) reports SKIP, not
    failure; `--require-gui` turns that back into a failure where a context was
    arranged on purpose.

Failures print and, for a real user, also raise a dialog — but never under
`--self-test`, `--smoke` or `CI`, because a modal dialog on a runner blocks until the
job times out. See `is_interactive`.
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


def is_interactive(args: list[str] | None = None) -> bool:
    """Is there a person in front of this process to click a dialog?

    `QMessageBox.exec()` is **modal**: with nobody to press OK it blocks forever. In CI
    that turned a two-second failure into a job that ran until its timeout, with the
    traceback already printed and the runner apparently hung. So the dialog is offered
    only to a real user: not under `--self-test`/`--smoke`, and not when a CI runner has
    set `CI`. `MULTIPLEX_THRESHOLDER_NO_DIALOG=1` forces it off anywhere.
    """
    if os.environ.get("MULTIPLEX_THRESHOLDER_NO_DIALOG"):
        return False
    if os.environ.get("CI"):
        return False
    args = list(sys.argv[1:] if args is None else args)
    return not ({"--self-test", "--smoke", "--require-gui"} & set(args))


def _report_crash(exc: BaseException, interactive: bool = True) -> Path | None:
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
    if not interactive:
        return path
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


#: Substrings that mean "this machine has no usable OpenGL", not "the bundle is broken".
#: A windows-latest runner has no GPU driver, so PyOpenGL resolves its entry points to
#: NULL and its `latebind` wrapper raises `TypeError: 'NoneType' object is not callable`
#: the moment vispy asks for a canvas. Same class of failure as a Linux box with no
#: libGL, or a Mac over SSH.
_NO_OPENGL_MARKERS = (
    "latebind",
    "opengl",
    "glerror",
    "libgl",
    "wglcreatecontext",
    "egl",
    "vispy",
    "could not create",
    "failed to create",
)


def _is_missing_opengl(exc: BaseException) -> bool:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).lower()
    return any(marker in text for marker in _NO_OPENGL_MARKERS)


def _check_matplotlib_canvas() -> str:
    """Build a QtAgg canvas without napari, so the check survives a GL-less machine.

    The KDE dock's canvas is the thing being proved here — matplotlib's Qt backend, which
    PyInstaller has to be told about explicitly. It needs a QApplication but no OpenGL.
    """
    from qtpy.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    from ._widget import KdePlot

    kde = KdePlot(dark=True)
    name = type(kde.canvas).__name__
    kde.deleteLater()
    del app
    return name


def _check_viewer() -> str:
    """The real thing: a napari viewer with both docks. Needs an OpenGL context."""
    import napari

    from ._widget import KDE_DOCK_NAME, make_gating_widget

    viewer = napari.Viewer(title=APP_NAME, show=False)
    try:
        # make_gating_widget, not GatingControls: it docks the KDE straight away
        # instead of on the next event-loop turn, and there is no loop here.
        controls = make_gating_widget(viewer)
        viewer.window.add_dock_widget(controls, area="right", name=CONTROLS_DOCK_NAME)

        # `dock_widgets` since napari 0.8; `_dock_widgets` warns but still works, and
        # older napari has only that.
        docks = list(getattr(viewer.window, "dock_widgets", None) or getattr(viewer.window, "_dock_widgets", {}))
        missing = [d for d in (CONTROLS_DOCK_NAME, KDE_DOCK_NAME) if d not in docks]
        if missing:
            raise AssertionError(f"dock missing: {missing} (have {docks})")
        if getattr(controls.kde, "canvas", None) is None:
            raise AssertionError("KDE dock has no matplotlib canvas")
        return f"napari {napari.__version__}, docks {docks}"
    finally:
        viewer.close()


def self_test(require_gui: bool = False) -> int:
    """Verify the bundle, then tear it down. No window, no event loop.

    Two tiers, because they fail for different reasons:

    * **Bundle checks** need no OpenGL — the compiled TIFF decoders, npe2 discovery, and
      matplotlib's Qt backend. A failure here is always a packaging bug.
    * **The viewer check** builds a real napari viewer with both docks, and needs a GL
      context. On a machine that has none (a CI runner with no GPU driver) it is reported
      as SKIP rather than failure, unless `--require-gui` says a context was arranged and
      its absence is itself the bug.
    """
    print(environment_summary(), flush=True)
    failures: list[str] = []
    skipped: list[str] = []

    checks = (
        ("plane reading", _check_plane_reading),
        ("npe2 manifest", _check_manifest),
        ("matplotlib canvas", _check_matplotlib_canvas),
    )
    for label, check in checks:
        try:
            print(f"{label}: {check()}", flush=True)
        except Exception as exc:  # noqa: BLE001 — collect every failure, not the first
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    try:
        print(f"viewer: {_check_viewer()}", flush=True)
    except Exception as exc:  # noqa: BLE001
        if _is_missing_opengl(exc) and not require_gui:
            skipped.append(f"viewer: no usable OpenGL on this machine ({type(exc).__name__}: {exc})")
        else:
            failures.append(f"viewer: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    if failures:
        print("\nSELF-TEST FAILED", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        return 1
    if skipped:
        print("\nSELF-TEST OK (with skips)", flush=True)
        for s in skipped:
            print(f"  ~ {s}", flush=True)
        print("  the bundle is complete; its GUI was not exercised here", flush=True)
        return 0
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
            "  --self-test      verify the bundle headlessly and exit; the viewer part\n"
            "                   is skipped, not failed, where there is no OpenGL\n"
            "  --require-gui    with --self-test: a missing OpenGL context is a failure\n"
            "  --smoke [SECS]   open the window for real, close it again (default 5 s)\n"
            "  --version        print the version\n\n"
            f"crash log: {crash_log_path()}\n"
            "no dialog on failure: --self-test, --smoke, CI=1, or "
            "MULTIPLEX_THRESHOLDER_NO_DIALOG=1"
        )
        return 0

    smoke: float | None = None
    if "--smoke" in args:
        after = [a for a in args[args.index("--smoke") + 1 :] if not a.startswith("-")]
        try:
            smoke = float(after[0]) if after else 5.0
        except ValueError:
            smoke = 5.0

    try:
        if "--self-test" in args:
            return self_test(require_gui="--require-gui" in args)
        return run(smoke)
    except Exception as exc:  # noqa: BLE001 — the outermost frame of a windowed app
        _report_crash(exc, interactive=is_interactive(args))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
