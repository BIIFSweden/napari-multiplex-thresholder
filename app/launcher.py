"""PyInstaller's entry script. Kept to four lines on purpose.

Everything real lives in `napari_multiplex_thresholder._app`, so the frozen app and a
pip install run the same code. `freeze_support()` comes before that import because a
frozen child process re-executes *this* file: without it, anything that spawns a worker
would start a second copy of the whole application instead of returning to the parent.
"""

import multiprocessing

multiprocessing.freeze_support()

from napari_multiplex_thresholder._app import main  # noqa: E402

raise SystemExit(main())
