# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the double-clickable app.

    pyinstaller app/MultiplexThresholder.spec --noconfirm      # from the repo root

Produces a *one-folder* build in `dist/`:

    macOS    dist/Multiplex Thresholder.app        (plus the same tree in dist/Multiplex Thresholder/)
    Windows  dist/Multiplex Thresholder/Multiplex Thresholder.exe
    Linux    dist/Multiplex Thresholder/Multiplex Thresholder

One folder rather than one file: a --onefile napari would unpack ~800 MB of Qt to a temp
directory on every launch, and Qt's plugin loader is unhappy about being moved there.

napari upstream does not support PyInstaller — it ships its own bundle through conda
constructor — so everything below is this project's own recipe, and the reason each entry
exists is written down. Verify a change with the app's own check, on every platform:

    "dist/Multiplex Thresholder.app/Contents/MacOS/Multiplex Thresholder" --self-test

which builds the whole GUI headless and asserts that the KDE canvas, the compiled TIFF
decoders and npe2 discovery all survived the freeze (`_app.py`).
"""

import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

ROOT = Path(SPECPATH).parent  # noqa: F821 — SPECPATH is injected by PyInstaller
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

APP_NAME = "Multiplex Thresholder"      # what the user sees and double-clicks
BUNDLE_ID = "se.scilifelab.biif.multiplex-thresholder"

datas, binaries, hiddenimports = [], [], []


def collect(package: str) -> None:
    d, b, h = collect_all(package)
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)


# Packages whose *data* files are load-bearing and which PyInstaller cannot infer:
# napari's Qt stylesheets, icons and its own npe2 manifest; vispy's GLSL shaders; the
# plugin's napari.yaml; dask's dask.yaml defaults; imagecodecs' and zarr's compiled
# codecs. collect_all also pulls each package's submodules, which covers napari's very
# dynamic imports (`napari._qt`, the layer controls, the plugin machinery).
for package in (
    "napari",
    "napari_builtins",          # napari's own reader/writer plugin — File ▸ Open
    "napari_svg",               # ships with napari[all]; writer contribution
    "napari_metadata",
    "napari_console",           # the >_ button. Drop this line (and IPython/qtconsole
    "IPython",                  # below) for a ~60 MB smaller bundle without a console.
    "qtconsole",
    "napari_multiplex_thresholder",
    "vispy",
    "npe2",
    "app_model",
    "magicgui",
    "superqt",
    "psygnal",
    "in_n_out",
    "imagecodecs",
    "zarr",
    "numcodecs",
    "dask",
):
    collect(package)

# npe2 finds plugins through `importlib.metadata` entry points, which exist only if the
# matching `.dist-info` directory is inside the bundle. Without this the app still opens
# — it builds its widget directly — but napari's Plugins menu is empty and File ▸ Open
# has no readers. `--self-test` reports it (`_app._check_manifest`).
for dist in (
    "napari",
    "npe2",
    "napari-console",
    "napari-svg",
    "napari-metadata",
    "napari-multiplex-thresholder",
    "vispy",
    "imagecodecs",
    "zarr",
    "tifffile",
    "dask",
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "natsort",
    "app-model",
    "magicgui",
    "superqt",
    "psygnal",
):
    datas += copy_metadata(dist, recursive=True)

hiddenimports += [
    # The KDE dock imports this inside KdePlot.__init__, too late for the analysis.
    "matplotlib.backends.backend_qtagg",
    # scipy.stats.gaussian_kde reaches these through lazy loaders.
    "scipy.stats",
    "scipy.special",
    "scipy._lib.array_api_compat.numpy.fft",
    # tifffile imports its codecs by name at decode time, not at import time.
    "tifffile",
    "imagecodecs",
    "imagecodecs._imcd",
]

excludes = [
    # Exactly one Qt binding may be in the bundle: two would fight over the plugin path
    # and the app would abort with "could not load the Qt platform plugin".
    "PySide2",
    "PySide6",
    "PyQt5",
    "tkinter",
    "pytest",
    "sphinx",
]

analysis = Analysis(  # noqa: F821
    [str(ROOT / "app" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[str(ROOT / "app" / "runtime_hook.py")],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX corrupts Qt frameworks; never worth the size here
    console=False,      # windowed: a crash goes to _app.crash_log_path(), not a console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,   # whatever the runner is; CI builds arm64 and x86_64 separately
    codesign_identity=None,   # ad-hoc signature on Apple Silicon, which is enough to run
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,          # or the canvas renders blurry
            "NSRequiresAquaSystemAppearance": False,  # follow the system dark theme
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.medical",
            # The app has no document types: tiles are chosen through its own path
            # fields, which is also how it finds the quant CSVs and masks beside them.
            "NSHumanReadableCopyright": "SciLifeLab BioImage Informatics Unit",
        },
    )
