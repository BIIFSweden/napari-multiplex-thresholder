"""
    Plugins ▸ Multiplex Thresholder          # in napari
    from napari_multiplex_thresholder import make_gating_widget   # from a script

The widget classes are imported lazily, so `napari_multiplex_thresholder._core` — file
discovery, quantification, thresholds, mask export — can be used and tested
without a Qt binding present.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._core import (
    Quantification,
    ThresholdTable,
    TileRef,
    discover_tiles,
    export_gated_mask,
    load_quantification,
    transform,
)

if TYPE_CHECKING:  # pragma: no cover
    from ._widget import GatingControls, KdePlot, make_gating_widget

__version__ = "0.1.0"

#: Names served from `._widget`, which needs Qt.
_QT_EXPORTS = {"GatingControls", "KdePlot", "make_gating_widget"}

__all__ = [
    "GatingControls",
    "KdePlot",
    "Quantification",
    "ThresholdTable",
    "TileRef",
    "discover_tiles",
    "export_gated_mask",
    "load_quantification",
    "make_gating_widget",
    "transform",
    "__version__",
]


def __getattr__(name: str):
    """Import the Qt-dependent widgets only when they are actually asked for.

    Keeps `import napari_multiplex_thresholder` usable in a headless environment — CI runs the
    core tests on three platforms without installing napari or a Qt binding.
    """
    if name in _QT_EXPORTS:
        from . import _widget

        return getattr(_widget, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
