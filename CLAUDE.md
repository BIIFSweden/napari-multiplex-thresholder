# napari-multiplex-thresholder

Two napari dock widgets for manual per-channel gating of multiplexed tissue images.
Pick a tile, pick a channel, drag an arcsinh threshold, watch sub-threshold cells
disappear, save the value. ~1 700 lines of package code, 570 of tests, no compiled
extensions.

Rewritten from `new_notebooks/5_apply_manual_threshold.ipynb` in the SciLifeLab BIIF
project **7904** (granulomatous skin inflammation, Phenocycler). That notebook still
exists and still works; this is an alternative front end with the same output contract
and none of its memory behaviour. The bug numbers referenced below (BUG-04, BUG-08 …)
are that project's `CLAUDE.md`, §6b.

**Names.** The distribution is `napari-multiplex-thresholder`, the import package
`napari_multiplex_thresholder`, the menu entry **Plugins ▸ Multiplex Thresholder**. Every
remaining `7904` in this repository is *provenance* — the study, its notebooks, its
`7904_cpose4` venv, its `data/new/` — and must stay; nothing in the package identity
carries a project number any more.

```bash
# self-contained env in this folder (gitignored), which is what to develop against
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -e ".[app,test]"
.venv/bin/napari                # Plugins ▸ Multiplex Thresholder  (adds both docks)

# or inside the 7904 project's Cellpose-4 venv, which has every dependency already
./7904_cpose4/bin/python -m pip install -e napari-multiplex-thresholder --no-deps --no-build-isolation
./7904_cpose4/bin/napari

# tests (either interpreter)
cd napari-multiplex-thresholder
.venv/bin/python tests/test_core.py              # 8 checks, synthetic, no Qt
.venv/bin/python tests/test_widget_real_data.py  # 17 checks against data/new/
```

---

## 1. Layout

| file | lines | what |
|---|---|---|
| `src/napari_multiplex_thresholder/_core.py` | 459 | No Qt, no napari: tile discovery, quantification, plane reading, LUTs, the threshold table, mask export. Everything testable headless. |
| `src/napari_multiplex_thresholder/_lazy.py` | 222 | Per-plane readers with a small LRU, and the dask stack for the image layer. |
| `src/napari_multiplex_thresholder/_widget.py` | 921 | `GatingControls` (dock 1) and `KdePlot` (dock 2). The only module that imports Qt. |
| `src/napari_multiplex_thresholder/__init__.py` | 67 | Re-exports; the Qt names come through a lazy `__getattr__`. |
| `src/napari_multiplex_thresholder/napari.yaml` | — | npe2 manifest, one widget contribution. **Must ship inside the wheel.** |
| `tests/test_core.py` | 231 | Synthetic fixtures, runs anywhere, doubles as a script (`python tests/test_core.py`). |
| `tests/test_widget_real_data.py` | 363 | Real napari viewer against the project's `data/new/`; skips itself if absent. |

`pyproject.toml`, `LICENSE`, `MANIFEST.in`, `requirements*.txt`, `.gitignore` and
`.github/workflows/build.yml` are the distribution (§9). `pyproject.toml` is the
authoritative dependency list; `requirements.txt` mirrors it and adds the two things a
bare checkout also needs — `napari[all]` for a Qt binding and `-e .` for the plugin
itself — so it drifts if one changes without the other.

---

## 2. Where it sits in the pipeline

Reads three folders produced by 7904 step 4 (`new_notebooks/pipeline.py`), matched by
filename **stem**:

```
<tiles>/<stem>.tif                       21 planes, uint16, DAPI first
<quant>/<stem>_quant.csv                 label + one column per marker (DAPI dropped)
<masks>/<stem>_entire_mask.tif           (C, H, W) int64; plane 0 empty, marker m at m+1
```

Writes the thresholds CSV that 7904 step 6 (`6_statistics_manual_gating.ipynb`) reads,
and optionally a gated mask that nothing downstream needs (§4).

Channel names and their order come from the **quant CSV's columns**, not from a marker
table: that order is the mask's plane order, and it is what keeps `channel → plane`
correct without a second file to keep in sync.

---

## 3. The two docks

Dock 1, `GatingControls`, top to bottom:

| | |
|---|---|
| Paths | raw tiles · quantification CSVs · multilayer masks · thresholds CSV · (optional) gated-mask output. Editing any of them re-lists the tiles; there is no refresh button. Remembered between sessions in `QSettings`. |
| Tile | dropdown of tiles that have all three files, **Load** beside it |
| Channel | dropdown + ◀ ▶, two-way bound to napari's channel slider |
| Threshold | slider, exact value box and cofactor on one line; **Run** below; then **Un-gate channel** and **Export gated mask** |
| Status | `n/20 channels gated` plus a standing warning naming how many are still NaN |

Dock 2, `KdePlot`: the distribution of the current (tile, channel) with the threshold
drawn on it, the percentage of *measured* cells above it, and whether the channel has
been gated. The curve is computed once per (tile, channel, cofactor) and cached — only
the vertical line moves while the slider does.

**Run** is the only thing that writes: it sets the LUT, redraws, and saves the CSV.
There is no save button, so gating work is never only in memory.

---

## 4. Output contract — do not break these

**`manual_thresholds_*.csv`** — rows are markers, columns are tile stems, index has no
name. Fixed by notebook 6, which does `pd.read_csv(path, index_col=0)`, iterates
`thresholds.columns` as tile stems and `thresholds.index` as marker names, and compares
`np.arcsinh(quant[marker]) >= thr`. Nothing else may appear in the file — that notebook's
`read_csv` has no `comment=` and would read a header line as data.

- Written **atomically** (temp file in the same directory + `os.replace`). Notebook 5
  truncated and rewrote on every commit, so an interrupt there could leave it empty
  (BUG-15).
- An existing file is **loaded and widened**, never replaced, so gating one tile today
  and another tomorrow accumulate in one CSV.
- **Ungated is NaN, not 0** (BUG-04). `NaN >= x` is False, so notebook 6 calls every
  cell *negative* for a marker nobody opened; `0` called every cell *positive*. Neither
  is silently correct, but this one fails in the safe direction, and the status line says
  how many are unset.
- `ThresholdTable.set()` **raises** on a channel or tile that is not already in the grid.
  `.loc`/`.at` would silently *enlarge* the frame instead, which is how a name drift
  would quietly add junk rows (BUG-23).

**`<name>.csv.meta.json`** — sidecar provenance: cofactor, source folders, timestamp,
and the list of unset pairs. It is a sidecar precisely because the CSV must stay pure
numbers.

**Cofactor** is a UI field defaulting to **1**, i.e. `arcsinh(x)`, which is notebook 5/6
space and what every threshold in `data/new/manual_thresholds_20260528.csv` is expressed
in. 5 would match `*asinh_normalised.h5ad` and ASTIR, but notebook 6 hardcodes 1, so the
widget warns when it is not 1.

**`<stem>_gated_mask.tif`** (export button) — `(C, H, W)`, same plane order as the source,
zlib, streamed one plane at a time. Ungated channels are written as **empty planes**,
which is what notebook 6 produces from a NaN threshold. Nothing downstream reads this
file; notebook 6 re-derives its own thresholded masks from the CSV. It exists for QC,
sharing and external tools.

---

## 5. Invariants that look like details and are not

1. **The npe2 contribution is the `GatingControls` class, and its first `__init__`
   parameter must stay named `napari_viewer`.** napari injects the viewer only into class
   contributions, matching that name or a `napari.viewer.Viewer` annotation; a plain
   *function* contribution is called with **no arguments**
   (`napari/_qt/_qplugins/_qnpe2.py::_get_widget_viewer_param`). A
   `make_gating_widget(napari_viewer)` factory installed fine, was discovered fine, and
   then failed the moment the menu item was clicked. `make_gating_widget()` survives for
   scripted use only.
2. **`napari.yaml` must stay in `[tool.setuptools.package-data]`.** Otherwise the wheel
   installs and the plugin never appears in the Plugins menu.
3. **The mask layer is a 2D buffer, the image layer is a `(C, H, W)` dask stack.** The
   stack is what gives the canvas its channel slider; the buffer is what makes a Run
   visible (§6.1). Because a 2D layer is never re-sliced by napari, `_redraw_labels()`
   must run on **every channel change** as well as every LUT change.
4. **Plane count comes from `series.shape[0]`, never `len(series.pages)`** (§6.3).
5. **Writing needs `photometric="minisblack"`** (§6.4).
6. **Spin boxes get `QLocale.Language.C`**, so a threshold reads `9.3100` and not
   `9,3100` under a Swedish or German locale — and so a typed `.` is accepted.
7. **Slider bounds are widened outwards to the spin box's precision**
   (`widen_to_decimals`). `QDoubleSpinBox.setRange` rounds to `decimals`, which can pull
   the bounds *inside* the channel's real range and leave the extreme cells unreachable.
8. **Files are paired on stem**, never by position in two directory listings (BUG-13),
   and macOS AppleDouble sidecars (`._*`) are skipped everywhere — on the project's exFAT
   drives they sit beside real files with the same extension.
9. **The slider range is per (tile, channel)**, from that channel's own min/max. Notebook
   5 used one global maximum taken from a frame that still contained a melted-in
   `n_cells` column, so its slider topped out at `arcsinh(cell count)` — 11.834284 for
   these tiles, which is a cell count, not an intensity (BUG-19).
10. **`requires-python = ">=3.11"`**, because that is napari 0.8's floor. On 3.10 pip
    silently resolves back to napari 0.5–0.7 *and* zarr 2, whose store API differs from
    the zarr 3 one `_core.PlaneSource` was written against.

---

## 6. Gotchas already paid for

### 6.1 A threshold change will not show if the mask layer is a dask stack

napari caches the slice it rendered. `layer.refresh()` — and `refresh(force=True)` —
redraw from that cache without re-running the graph; `Labels._set_view_slice()` raises
`NotImplementedError`; emitting `events.data` does nothing. Result: the mask showed every
cell no matter where the slider was, while the source plane was correctly gated.

Reassigning `layer.data = <new dask stack>` *does* re-slice, and keeps the slider position
and camera zoom — but napari retains the superseded slices: 20 Runs on the largest tile
grew resident memory from 4.95 GB to 10.05 GB.

What works: **a single 2D numpy buffer that napari aliases** (`np.shares_memory` confirms
it). Write the gated plane into it in place, call `refresh()`. Correct display, and no
allocation.

### 6.2 Removing layers re-enters the dims callback

`viewer.layers.remove()` triggers `dims.reset()` → `current_step` → `_on_dims_changed`.
On the *second* Load that ran while `self.labels` was already the new tile and
`self._display` was still the old tile's buffer: `ValueError: could not broadcast
(10600,10360) into (10700,10460)`. `_add_layers` sets a `_loading` flag that the dims and
channel handlers check, and clears `_display` before removing anything.

### 6.3 `len(series.pages)` is not the plane count

tifffile stores a small leading axis as *samples per pixel*: a 4-plane stack becomes one
page with 4 samples, `pages` has a single entry, and `series.asarray(key=i)` raises
`IndexError`. The 21-channel production files do have 21 pages, so the fast path works
there; `_core.PlaneSource` takes the count from `series.shape[0]` and falls back to
tifffile's zarr store for the interleaved case.

### 6.4 Writing a plane-at-a-time iterator needs `photometric="minisblack"`

Same cause: without it tifffile treats a small leading axis as samples and tries to write
one interleaved page, which does not match the iterator at all
(`cannot reshape array of size 5120 into shape (4,1,64,80,1)`). Being explicit also
guarantees the export reads back as one page per channel.

### 6.5 `tifffile.memmap` is unusable here

It needs uncompressed, contiguous data. Everything in this project is zlib. Per-page
`asarray()` is the way, and it costs ~21 ms on the real masks.

### 6.6 napari segfaults under `QT_QPA_PLATFORM=offscreen`

Exit 139: vispy gets no OpenGL context. `napari.Viewer(show=False)` on macOS gets a real
context without a visible window, which is how `tests/test_widget_real_data.py` runs.
Canvas screenshots from such a hidden window come back **black**, so they are not usable
as evidence — assert on the slice instead (§7).

### 6.7 `dask.delayed(fn, pure=False)`

Required for any stack whose output depends on mutable state. With the default
`pure=True`, dask may treat two calls with the same plane index as interchangeable and
serve a stale graph.

---

## 7. Memory, measured

Notebook 5 held every tile's mask and raw image plus a full second copy of every mask:
**164 GB** for the six tiles. Here nothing off-screen is loaded. On the largest tile
(`21 × 10700 × 10460 int64` = 17.9 GB for the mask alone):

| | |
|---|---|
| after Load | 1.2–3.7 GB |
| gating all 20 channels | rises to ~12 GB |
| three more passes over the same 20 channels | 12.0 → 10.3 → 8.4 GB — **bounded** |
| 3-tile, 60-channel session | ends ~9.7 GB |

The rise is per *visited channel* and it stops. Isolated: 20 plane reads with no napari
plateau at 1.93 GB; a viewer with only the lazy image layer costs +5.2 GB over 20 slider
moves and +0.2 GB over 20 more. Each new channel decodes an **int64** page (0.9 GB) that
is cast to uint32, and the allocator keeps those buffers for reuse.

Levers, neither needed: `quantification.entire_mask_dtype: uint32` in the 7904 pipeline
halves every mask from step 4 on (and helps notebook 6 too); napari's opportunistic dask
cache (`napari.utils.resize_dask_cache`, ~9.7 GB by default) accounts for only ~1 GB of
the total.

---

## 8. Tests

`tests/test_core.py` — 8 checks on synthetic fixtures: stem matching (including a stray
file that must not shift anything), NaN-safe LUTs and percentages, mask layout detection,
threshold-table NaN semantics and atomic save, sdist-safe export with ungated planes
zeroed, per-plane reading and LRU capping. No Qt, no napari, no display: runs on Ubuntu,
Windows and macOS across Python 3.11–3.13 whenever the build workflow is dispatched (§9).

`tests/test_widget_real_data.py` — 17 checks with a real viewer against `data/new/`:
path-edit refresh, Load, lazy-vs-buffer layer shapes, two-way channel/slider sync, the
per-channel slider range, Run, un-gate, the CSV and its sidecar, the streamed export, and
the plugin opened **through napari's own command registry** the way the menu does it.

Two rules learned the hard way:

- **Assert on the rendered slice, not the source array.** The §6.1 display bug was
  invisible to a test that checked `widget.labels.plane(...)`. The check that catches it
  reads `layer._slice.image.raw` — private, and it raises rather than skipping if napari
  moves it.
- **Exercise the plugin through `get_app_model().commands`**, not by calling the widget
  factory directly, or §5.1 goes unnoticed.

Both files run standalone because the project's cp4 venv has no pytest;
`pip install -e ".[test]"` adds it.

---

## 9. Packaging and release

One **pure-Python wheel** (`py3-none-any`, 28 KB) covers Windows, macOS and Linux —
nothing compiles, so there is no per-platform build or asset.

- Bare `napari>=0.5` as a dependency, so installing into an existing napari never pulls a
  second Qt binding; an **`[app]` extra** (`napari[all]>=0.5`) so a first-time user gets a
  working viewer in one command.
- `__init__` imports the widgets lazily, so `_core` works with no Qt binding at all.
- Verified rather than assumed: `pip install --dry-run --platform win_amd64
  --python-version 3.11|3.12|3.13 --only-binary :all:` resolves, as do macOS arm64 and
  x86_64. A real install into a bare Python 3.13 venv pulled napari 0.8.0 + PyQt6 and the
  plugin was discovered with the viewer injected; that resolution picked pandas 3.0.5 /
  numpy 2.5.2 (newer than cp4's) and the core tests pass there.
- **A GitHub "latest" URL cannot be hard-coded**: the asset filename contains the version,
  so `releases/latest/download/<file>` rots on the next tag. The README tells users to copy
  the link from the Releases page, and points at PyPI as the real fix.

### `.github/workflows/build.yml`

The only workflow, and **`workflow_dispatch` only — it never starts by itself.** No push,
PR or schedule trigger: Actions ▸ *Build and test wheel* ▸ Run workflow, pick a ref, pick
`scope`. Build the sdist and wheel once on 3.11 (the floor), `twine check`, then install
*that wheel* across the matrix and run `tests/test_core.py` there. Both artefacts are
attached to the run, so a candidate is installable before any tag exists.

- `scope` is a `choice` input: `full` (3 OS × 3 Python) or `quick` (ubuntu × 3.13). It is
  turned into a matrix by a step that prints `json=…` to `$GITHUB_OUTPUT`, consumed as
  `matrix: include: ${{ fromJSON(needs.build.outputs.matrix) }}` — the only way to vary a
  matrix by input. The input reaches that step through `env:`, never string-interpolated
  into the shell.
- **The workflow is invisible until this file is on the default branch.** GitHub lists
  dispatchable workflows from the default branch only, so on a repo whose `main` does not
  have it yet there is no Run workflow button to press.

- Two assertions on the built wheel encode §5.2 and §9: `napari_multiplex_thresholder/napari.yaml`
  must be **inside** the wheel, and the wheel must be `py3-none-any`. A missing manifest
  installs cleanly and then never appears in the Plugins menu — silent, hence asserted.
- The matrix installs **without the `app` extra**, so no Qt binding is present. That is the
  point: it proves the dependencies resolve to binary wheels on three platforms and that the
  non-GUI half imports headless. It replaces the manual `pip install --dry-run --platform …`
  checks above.
- `tests/test_widget_real_data.py` is **not** in CI: it needs a real OpenGL context (§6.6)
  and the study's tiles. It stays a local gate.
- Dispatched against a `v*` **tag** (the ref dropdown lists tags too) it additionally
  **fails if the tag disagrees with `version` in `pyproject.toml`**. It does *not* create
  the Release or upload to PyPI — that is still manual (download `dist` from the run). A
  `release.yml` doing it with `softprops/action-gh-release` plus a trusted publisher is
  the obvious next step; the README says so too, so the two must be updated together.

This folder is its own git repository with its own `.gitignore`, so the parent
`7904_granulo` ignore rules — including its unanchored `CLAUDE.md` — no longer apply and
this file commits normally. `.venv/`, `dist/`, `build/`, `*.egg-info/` and anything under
`data/` are ignored; the tiles are gigabytes and the thresholds CSV is a per-study result.

---

## 10. What it fixes relative to notebook 5, and what it does not

Fixed: BUG-19 (slider range per tile+channel, not `arcsinh(n_cells)`), BUG-04 (NaN for
ungated), BUG-13 (stem matching), BUG-15 (atomic write, no all-zeros pre-fill), BUG-20
(export covers every channel and reports which are empty), BUG-21 (own integer slider
mapping instead of a float `step` the widget stack ignored), BUG-22 (the percentage
divides by the cells the channel measured), BUG-23 (membership checked before writing),
BUG-08 (one plane at a time).

Not carried over: notebook 5's cell 11, which saved whatever happened to be in the
layers — each plane holding whatever threshold was last displayed for it, unvisited planes
keeping every label, and no record of which pairs were touched. Export here is explicit
and complete.

Deliberately absent: anything that computes cell types, statistics or spatial measures.
This widget chooses thresholds and records them; notebook 6 onwards does the rest.

### Worth doing next, roughly in order

1. **Read the compartment mapping** (7904's `Spatial_staining_toBIIF.csv`) and load the
   three compartment masks rather than 20 mask planes. The 21 planes of an
   `_entire_mask.tif` hold only **4 distinct images** (empty + cell + nuclei + cyto,
   verified), so this removes ~6.7× of both I/O and the int64 decodes that dominate §7 —
   at the cost of needing that CSV and the per-tile label offset.
2. **A histogram toggle** beside the KDE, for channels where the density estimate hides
   a bimodal split.
3. **Multi-tile gating in one pass** — apply one channel's threshold across every loaded
   tile, which is how the panel is usually reasoned about.
4. **Undo for the CSV**, or at least keeping the previous version alongside it. Runs
   overwrite silently today; the atomic write protects against interruption, not against
   a mis-drag.
