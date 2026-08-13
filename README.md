# napari-multiplex-thresholder

Manual per-channel gating of Phenocycler tiles, as two napari dock widgets. A
rewrite of the magicgui widget in `new_notebooks/5_apply_manual_threshold.ipynb`:
same job, same threshold semantics, without the notebook's memory profile and
threshold bookkeeping.

## Install

The whole package is one pure-Python wheel (`py3-none-any`), so **the same file
installs on Windows, macOS and Linux** — nothing is compiled, and there is no
per-platform build. Python 3.11 or newer.

### Option 1 — from PyPI (best, once published)

```bash
pip install "napari-multiplex-thresholder[app]"      # no napari yet: brings napari + Qt
pip install napari-multiplex-thresholder             # already have napari
```

Nothing to copy, upgrades are `pip install -U`, and napari's own plugin manager lists
it. Not published yet — see *Building and releasing* below. **Until it is, use option
2 or 3.**

### Option 2 — from a GitHub Release

Open <https://github.com/BIIFSweden/napari-multiplex-thresholder/releases>, copy the link to the
`.whl` file on the newest release, and:

```bash
# no napari yet — the [app] extra brings napari and its Qt bindings with it
pip install "napari-multiplex-thresholder[app] @ <paste the .whl link here>"

# already using napari — leaves your Qt binding alone
pip install <paste the .whl link here>
```

For example, for v0.1.0:

```bash
pip install "napari-multiplex-thresholder[app] @ https://github.com/BIIFSweden/napari-multiplex-thresholder/releases/download/v0.1.0/napari_multiplex_thresholder-0.1.0-py3-none-any.whl"
```

The wheel filename carries the version, so there is no stable "latest" URL to hard-code —
that is why the instruction is to copy the link from the Releases page.

Needs no git, no compiler and no build step. Or download the `.whl` first and
`pip install path\to\napari_multiplex_thresholder-0.1.0-py3-none-any.whl`.

### Option 3 — from the repository (needs git)

```bash
pip install "napari-multiplex-thresholder[app] @ git+https://github.com/BIIFSweden/napari-multiplex-thresholder.git@v0.1.0"
```

Drop the `@v0.1.0` to track `main`.

### Starting from nothing, step by step

```bash
python -m venv napari-env
source napari-env/bin/activate            # macOS / Linux
napari-env\Scripts\Activate.ps1           # Windows PowerShell

pip install "napari-multiplex-thresholder[app] @ <wheel link>"
napari
```

Then **Plugins ▸ Multiplex Thresholder**, which opens both docks. Verified from a bare
Python 3.13 environment: it pulls napari 0.8, PyQt6, dask, tifffile, zarr and the rest,
and the plugin is discovered with the viewer injected correctly.

### For development, or inside the 7904 project

```bash
git clone https://github.com/BIIFSweden/napari-multiplex-thresholder.git
cd napari-multiplex-thresholder
pip install -e ".[app,test]"      # drop `app` if this environment already has napari
```

`requirements.txt` is the same thing spelled out for people who prefer it, and it
includes the plugin itself (`-e .`) plus a Qt binding, so it is enough on its own:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # or -r requirements-dev.txt to also get pytest
napari
```

With [uv](https://docs.astral.sh/uv/), which needs no activation and no system Python:

```bash
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -e ".[app,test]"
.venv/bin/napari                          # Plugins ▸ Multiplex Thresholder
.venv/bin/python tests/test_core.py
```

`.venv/` is in `.gitignore`. On Windows the interpreter is `.venv\Scripts\python.exe`.

In the 7904 project's own Cellpose-4 environment, which already has every dependency:

```bash
./7904_cpose4/bin/python -m pip install -e napari-multiplex-thresholder --no-deps --no-build-isolation
```

`--no-deps` because that environment has to keep coexisting with `7904_cpose3` and pip
should have no reason to touch it; `--no-build-isolation` uses the setuptools already
there instead of fetching a build backend.

### Things worth knowing before you support someone else's install

* **Python 3.11 is the floor**, because it is napari 0.8's floor. On 3.10 pip silently
  resolves back to napari 0.5–0.7 and zarr 2, neither of which this was tested against.
* **Windows and macOS resolution is checked, not assumed**: every dependency has a
  binary wheel for `win_amd64` on 3.11/3.12/3.13 and for macOS on arm64 and x86_64.
* `[app]` exists so that a first-time install is one command. Do not use it when
  installing into an existing napari — it can pull a second Qt binding.
* If napari starts but the plugin is not in the menu, the manifest did not ship:
  `python -c "from npe2 import PluginManifest; print(PluginManifest.from_distribution('napari-multiplex-thresholder').name)"`
* A headless machine has nothing to show — napari needs a display. The non-GUI half
  (`napari_multiplex_thresholder._core`: discovery, quantification, thresholds, mask export)
  imports and runs without Qt, which is how CI tests it on three platforms.

## Building and releasing

`.github/workflows/build.yml` — **Build and test wheel** — is started by hand and never
runs on its own. There is no push, pull-request or schedule trigger:

> **Actions ▸ Build and test wheel ▸ Run workflow** → choose the branch or tag, choose
> `full` or `quick`, press the green button.

| input | |
|---|---|
| `full` | Ubuntu + Windows + macOS × Python 3.11, 3.12, 3.13 — nine installs |
| `quick` | Ubuntu × 3.13 only, for a fast check while iterating |

It builds the sdist and the wheel once on Python 3.11 (the floor), checks the metadata
with `twine check`, asserts that the npe2 manifest is **inside** the wheel and that the
wheel is `py3-none-any`, and then installs *that wheel* on each platform in the matrix.
Those installs deliberately omit the `app` extra, so there is no Qt binding: they prove
the dependencies resolve to binary wheels on every platform, that the manifest is
discoverable through npe2, and that `tests/test_core.py` passes with no display.

Both files are attached to the run, so a release candidate can be downloaded and
installed by hand before any tag exists — **Actions ▸ the run ▸ Artifacts ▸ dist**.

The workflow is listed on the Actions page only once this file is on the repository's
default branch. Push it to `main` first, or the Run workflow button is nowhere to be
found.

To release:

```bash
# bump `version` in pyproject.toml, commit, then:
git tag v0.1.1
git push origin v0.1.1
```

Then run the workflow once more, picking `v0.1.1` in the ref dropdown — the dropdown
lists tags as well as branches, and on a `v*` ref one extra check applies: the run fails
if the tag disagrees with the version in `pyproject.toml`, so a mistagged commit is
caught before anyone sees it.

The workflow **does not publish**: it creates no GitHub Release and uploads nothing to
PyPI. Take `dist` from that run and attach the two files to a release by hand, or add a
`release.yml` (build + `softprops/action-gh-release`, plus a PyPI
[trusted publisher](https://docs.pypi.org/trusted-publishers/) if PyPI is wanted).

## Using it

**Dock 1 — controls**

| | |
|---|---|
| Paths | raw tiles · quantification CSVs · multilayer masks · thresholds CSV (+ optional export folder). Editing any of them re-lists the tiles — there is no refresh button |
| Tile | dropdown of tiles that have all three files, with **Load** beside it |
| Channel | dropdown, plus ◀ ▶ — kept in step with napari's channel slider in both directions |
| Threshold | arcsinh slider with the exact value and the cofactor on the same line, **Run** below, then **Un-gate channel** and **Export gated mask** |

**Dock 2 — KDE** shows the distribution of the current channel with the threshold
drawn on it, the percentage of measured cells above it, and whether the channel has
been gated yet.

**Run** hides every cell whose `arcsinh(mean intensity)` is below the threshold, as
the notebook did, records the value, **and writes the thresholds CSV**. There is no
save button: nothing is ever left only in memory, so quitting napari cannot lose
gating work. What is still ungated is shown in the status line at the bottom rather
than in a dialog.

**Un-gate channel** brings every cell back and sets that channel to NaN.

**Export gated mask** writes `<stem>_gated_mask.tif` into the export folder: a
`(C, H, W)` TIFF where each channel plane keeps only the cells that passed *that*
channel's threshold, zlib-compressed and streamed one plane at a time (6 MB for the
small tile, 23 MB for the largest). Channels you have not gated become empty planes.
Nothing downstream reads it — notebook 6 re-derives its own thresholded masks from the
CSV — so it exists for QC, for sharing, and for tools outside this pipeline.

Paths are remembered between sessions (`QSettings`), so a resumed session only needs
tile → Load.

## Output

`manual_thresholds_*.csv` — rows are markers, columns are tile stems, exactly what
`6_statistics_manual_gating.ipynb` reads with `index_col=0`. Written atomically
(temp file + `os.replace`), so an interrupted write cannot truncate a day's work.

`manual_thresholds_*.csv.meta.json` — provenance: cofactor, source folders,
timestamp, and the list of ungated pairs. It lives beside the CSV rather than inside
it because notebook 6's `read_csv` has no `comment=` and would read a header line as
data.

An **ungated channel is NaN**, never 0. Notebook 6 compares `>= thr`, so NaN calls
every cell negative — visible and conservative — where 0 silently calls every cell
positive (BUG-04 in the project's CLAUDE.md).

**Cofactor** defaults to 1, matching notebooks 5 and 6 (`np.arcsinh(quant)`) and
every threshold in `data/new/manual_thresholds_*.csv`. Set it to 5 to work in the
space `*asinh_normalised.h5ad` and ASTIR use — but notebook 6 hardcodes 1, so it
would have to be changed to match. The widget warns when the cofactor is not 1.

## Memory

Notebook 5 loaded every tile's mask and raw image up front and then copied all the
masks: **164 GB resident** for the six legacy tiles. Here nothing is loaded that is not
on screen. The raw tile is a lazy `(C, H, W)` dask stack — that is what gives the canvas
its channel slider — and the mask is a single 2D buffer holding the current channel; one
page read from these zlib-compressed TIFFs takes ~21 ms.

Measured on the largest tile (`21 x 10700 x 10460 int64`, 17.9 GB for the mask alone):

| | |
|---|---|
| after Load | 1.2–3.7 GB |
| gating all 20 channels | rises to ~12 GB, then settles |
| three more passes over the same 20 channels | 12.0 → 10.3 → 8.4 GB — **bounded, not a leak** |
| a 3-tile, 60-channel session | ends at 10.3 GB |

The rise is per *visited channel*, not per operation: each new channel decodes an
**int64** page (0.9 GB) that is cast to uint32, and the allocator holds those buffers for
reuse. Two things reduce it, neither required:

* `quantification.entire_mask_dtype: uint32` in the pipeline config halves every mask
  from step 4 onwards — for notebook 6 as well.
* napari keeps an opportunistic dask cache (Preferences ▸ Application, ~9.7 GB by
  default on this machine). `napari.utils.resize_dask_cache(int(1e9))` trims about a
  gigabyte; most of the footprint is not this cache.

## Tests

```bash
cd napari-multiplex-thresholder
.venv/bin/python tests/test_core.py               # pure logic, synthetic data
.venv/bin/python tests/test_widget_real_data.py   # real tiles from data/new/

# or in the 7904 project's own environment
../7904_cpose4/bin/python tests/test_core.py
../7904_cpose4/bin/python tests/test_widget_real_data.py
```

Both run standalone because `7904_cpose4` has no pytest (`pip install -e
'.[test]'` adds it if wanted). The second test needs a real OpenGL context —
`napari.Viewer(show=False)` provides one without showing a window, but
`QT_QPA_PLATFORM=offscreen` does **not**, and napari segfaults under it. It finds
`data/new/` by searching this file's ancestors and their siblings, or takes
`GATING_TEST_DATA=<path>`, and skips itself if there is nothing to find — which is why
CI runs only the first file.
