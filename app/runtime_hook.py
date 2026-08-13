"""Runs inside the bundle before the entry script, to remove three guesses.

PyInstaller executes runtime hooks first, so anything set here is in place before napari,
qtpy or matplotlib are imported.

Not here: `MPLCONFIGDIR`. PyInstaller ships its own matplotlib runtime hook that
*assigns* it a fresh temp directory, so whatever this file sets is thrown away — the
persistent font cache is set up in `_app._use_persistent_matplotlib_cache()` instead,
which runs after every runtime hook and still before matplotlib is first imported.
"""

import os

# qtpy picks a binding by trying imports in order. The bundle contains exactly one
# (PyQt6), and being explicit means a stray PySide6 on the user's machine can never be
# picked up instead.
os.environ.setdefault("QT_API", "pyqt6")

# matplotlib must not go looking for a GUI framework of its own; the KDE dock embeds a
# QtAgg canvas in napari's window.
os.environ.setdefault("MPLBACKEND", "QtAgg")

# napari's experimental async loader adds threads and a code path this widget has never
# been tested against. Off unless the user asks for it.
os.environ.setdefault("NAPARI_ASYNC", "0")
