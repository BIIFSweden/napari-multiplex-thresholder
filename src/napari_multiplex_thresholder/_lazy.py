"""Lazy (C, H, W) stacks so napari's channel slider works without loading a tile.

An earlier implementation read whole stacks — every mask eagerly, a full second copy
of all of them, and every raw image handed to napari — so resident memory scaled with
the number of tiles opened rather than with what is on screen, and reached hundreds of
gigabytes.

Nothing needs that. The widget only ever displays one (tile, channel) plane, and a
single page read from these deflate-compressed TIFFs takes ~21 ms. So each layer is
a dask stack of one-plane-per-block delayed reads: napari materialises only the
plane the slider is on, and a small LRU keeps recently visited planes.

`tifffile.memmap` is not an option here — it needs uncompressed, contiguous data,
and these files are written with zlib. Per-page `asarray()` is what works.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import dask
import dask.array as da
import numpy as np

from . import _core

#: How many decoded planes to keep per source file. Raw uint16 planes are cheap
#: (~0.2 GB on the largest tile) so two make next/back instant. Label planes are
#: cast to uint32 on read but still ~0.45 GB there, so masks keep one: re-decoding
#: costs tens of milliseconds, holding a second plane costs half a gigabyte.
DEFAULT_CACHE_PLANES = 2
LABEL_CACHE_PLANES = 1

#: napari's Labels layer wants a compact integer dtype. Combined masks are written
#: int64, but per-tile label values fit comfortably in 32 bits, so uint32 is ample
#: and halves what crosses into the viewer.
LABEL_DTYPE = np.dtype(np.uint32)


class PlaneReader:
    """`_core.PlaneSource` plus a tiny LRU cache, so next/back does not re-decode."""

    def __init__(self, path, cache_planes: int = DEFAULT_CACHE_PLANES, cast=None):
        self.source = _core.PlaneSource(path)
        self.cache_planes = max(1, cache_planes)
        # Cast on read, not after: an int64 label plane is 0.9 GB on the largest
        # tile and 0.45 GB as uint32, and the cache holds whatever read() returns.
        self.cast = np.dtype(cast) if cast is not None else None
        self._cache: OrderedDict[int, np.ndarray] = OrderedDict()

    @property
    def path(self) -> Path:
        return self.source.path

    @property
    def shape(self) -> tuple[int, ...]:
        return self.source.shape

    @property
    def dtype(self) -> np.dtype:
        return self.cast or self.source.dtype

    @property
    def n_planes(self) -> int:
        return self.source.n_planes

    @property
    def plane_shape(self) -> tuple[int, int]:
        return self.source.plane_shape

    def read(self, index: int) -> np.ndarray:
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached
        plane = self.source.read(index)
        if self.cast is not None and plane.dtype != self.cast:
            plane = plane.astype(self.cast, copy=False)
        self._cache[index] = plane
        while len(self._cache) > self.cache_planes:
            self._cache.popitem(last=False)
        return plane

    def clear_cache(self) -> None:
        self._cache.clear()


def _delayed_stack(fn, n: int, plane_shape, dtype) -> da.Array:
    """Stack `n` lazily-read planes into one (n, H, W) dask array.

    `pure=False` matters: the gated reader's output depends on mutable LUT state,
    so dask must not treat two calls with the same index as interchangeable and
    serve a cached graph after a threshold changes.
    """
    delayed_fn = dask.delayed(fn, pure=False)
    return da.stack(
        [
            da.from_delayed(delayed_fn(i), shape=plane_shape, dtype=dtype)
            for i in range(n)
        ]
    )


class LazyImage:
    """The raw tile as a lazy (C, H, W) stack, DAPI dropped."""

    def __init__(self, tile_path, drop_first: bool = True):
        self.reader = PlaneReader(tile_path)
        self.offset = 1 if drop_first else 0
        self.n_channels = self.reader.n_planes - self.offset

    def plane(self, channel: int) -> np.ndarray:
        return self.reader.read(channel + self.offset)

    def as_dask(self) -> da.Array:
        return _delayed_stack(
            self.plane, self.n_channels, self.reader.plane_shape, self.reader.dtype
        )

    #: Stride used when estimating contrast limits. np.percentile sorts a full copy
    #: of what it is given — 224 MB for one plane of the largest tile — and every
    #: 16th pixel gives the same 1st/99.9th percentile to display precision.
    CONTRAST_STRIDE = 4

    def contrast_limits(self, channel: int, low=1.0, high=99.9) -> tuple[float, float]:
        """Percentiles of one plane — the layer is lazy, so napari cannot guess these."""
        plane = self.plane(channel)
        sample = plane[:: self.CONTRAST_STRIDE, :: self.CONTRAST_STRIDE]
        lo, hi = (float(v) for v in np.percentile(sample, [low, high]))
        if hi <= lo:
            hi = lo + 1.0
        return lo, hi


class LazyGatedLabels:
    """The combined mask as a lazy (C, H, W) label stack with per-channel gating.

    `set_lut(channel, lut)` swaps in a boolean label→keep table; the plane the
    slider is on is then rebuilt on the next refresh, so sub-threshold cells vanish
    exactly as they did in the earlier implementation. `set_lut(channel, None)` shows
    everything again. The on-disk plane is always the source, so gating is never
    cumulative and there is no second full copy of the masks to hold.
    """

    def __init__(self, mask_path, n_channels: int, plane_offset: int = 1):
        self.reader = PlaneReader(
            mask_path, cache_planes=LABEL_CACHE_PLANES, cast=LABEL_DTYPE
        )
        self.plane_offset = plane_offset
        self.n_channels = n_channels
        self._luts: dict[int, np.ndarray] = {}

        if self.reader.n_planes < n_channels + plane_offset:
            raise ValueError(
                f"{self.reader.path.name} has {self.reader.n_planes} planes, need "
                f"{n_channels + plane_offset}"
            )

    # --- gating state ---

    def set_lut(self, channel: int, lut: np.ndarray | None) -> None:
        if lut is None:
            self._luts.pop(channel, None)
        else:
            self._luts[channel] = lut

    def lut(self, channel: int) -> np.ndarray | None:
        return self._luts.get(channel)

    def gated_channels(self) -> list[int]:
        return sorted(self._luts)

    def luts_by_plane(self) -> dict[int, np.ndarray]:
        """LUTs keyed by *mask plane* index, for `_core.export_gated_mask`."""
        return {c + self.plane_offset: lut for c, lut in self._luts.items()}

    # --- data access ---

    def raw_plane(self, channel: int) -> np.ndarray:
        return self.reader.read(channel + self.plane_offset)

    def max_label(self, channel: int) -> int:
        return int(self.raw_plane(channel).max())

    def plane(self, channel: int) -> np.ndarray:
        """The plane as displayed: gated if a LUT is set, otherwise as stored."""
        plane = self.raw_plane(channel)  # already LABEL_DTYPE
        lut = self._luts.get(channel)
        if lut is None:
            return plane
        keep = lut[np.clip(plane, 0, lut.size - 1)]
        return np.where(keep, plane, 0).astype(LABEL_DTYPE, copy=False)

    def write_plane_into(self, channel: int, out: np.ndarray) -> np.ndarray:
        """Write the displayed plane into `out` with as little scratch as possible.

        `plane()` allocates a fresh 0.45 GB array on the largest tile every time it is
        called; the widget calls this on every channel change and every Run, and those
        temporaries dominated what the allocator held on to. Writing into the buffer
        napari already aliases needs only a bool mask (a quarter of the size).
        """
        raw = self.raw_plane(channel)
        if raw.shape != out.shape:
            raise ValueError(f"buffer is {out.shape}, plane is {raw.shape}")
        np.copyto(out, raw, casting="unsafe")
        lut = self._luts.get(channel)
        if lut is not None:
            drop = np.logical_not(lut[np.clip(raw, 0, lut.size - 1)])
            np.copyto(out, LABEL_DTYPE.type(0), where=drop)
        return out

    def as_dask(self) -> da.Array:
        """The whole mask as a lazy stack.

        Not what the widget displays — napari caches the slice it rendered and will not
        re-run a dask graph on `refresh()`, so a threshold change never appeared. Kept
        because it is the natural way to hand the whole mask to something else lazily.
        """
        return _delayed_stack(
            self.plane, self.n_channels, self.reader.plane_shape, LABEL_DTYPE
        )
