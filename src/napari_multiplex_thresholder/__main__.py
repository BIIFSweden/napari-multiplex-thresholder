"""`python -m napari_multiplex_thresholder` — the same entry point the app uses."""

from ._app import main

if __name__ == "__main__":
    raise SystemExit(main())
