# napari-multiplex-thresholder

Desktop installable napari application with thresholding widget, where multiplexed data (for example whole slide imaging spatial proteomics data) can be manually gated.

Pick a tile, pick a channel, drag an arcsinh threshold, watch the cells below it
disappear, press **Run**. Each Run writes the threshold to a CSV immediately, so gating
work is never only in memory. Ungated channels stay `NaN` to see which channels have already been thresholded.

It reads three folders, matched on filename stem — `<stem>.tif` (raw tile),
`<stem>_quant.csv` (one column per marker), `<stem>_entire_mask.tif` (per-channel label
masks) — and writes `manual_thresholds_*.csv`: rows are markers, columns are tiles, plus
a `.meta.json` sidecar with the cofactor and provenance. Channel names and their order
come from the quant CSV's columns.

## Install

Download the file for your system from the
[Releases page](https://github.com/BIIFSweden/napari-multiplex-thresholder/releases),
unzip it, and double-click.

| | |
|---|---|
| **macOS** | `…-macos-arm64.zip` for Apple Silicon, `…-macos-x86_64.zip` for Intel. Move `Multiplex Thresholder.app` to Applications, then **right-click ▸ Open** the first time — the app is not signed, so a plain double-click is refused. If there is no *Open* button, run `xattr -dr com.apple.quarantine "/Applications/Multiplex Thresholder.app"`. |
| **Windows** | `…-windows-x64.zip`. Unzip the whole folder and keep it together, double-click `Multiplex Thresholder.exe`, then **More info ▸ Run anyway**. |
| **Linux** | `…-linux-x64.tar.gz`. Unpack and run `./"Multiplex Thresholder"/"Multiplex Thresholder"`. Needs a desktop with OpenGL. |

## Development

```bash
git clone https://github.com/BIIFSweden/napari-multiplex-thresholder.git
cd napari-multiplex-thresholder

uv venv .venv --python 3.13                                  # or python -m venv .venv
uv pip install --python .venv/bin/python -e ".[app,test]"     # or pip install -r requirements-dev.txt
```

Run it, straight from the source tree:

```bash
.venv/bin/python -m napari_multiplex_thresholder      # the app: viewer + both docks
.venv/bin/multiplex-thresholder --self-test           # build the GUI headless and check it
.venv/bin/napari                                      # plain napari, load it from the Plugins menu
```

Tests:

```bash
.venv/bin/python tests/test_core.py               # 8 checks, synthetic data, no display
.venv/bin/python tests/test_widget_real_data.py   # 17 checks against real tiles; skips if absent
```

Build the double-clickable app locally (~1.5 min):

```bash
uv pip install --python .venv/bin/python -e ".[bundle]"
.venv/bin/pyinstaller app/MultiplexThresholder.spec --noconfirm
open "dist/Multiplex Thresholder.app"                 # or dist/Multiplex Thresholder/ on Windows/Linux
```

Python 3.11 is the floor (napari 0.8's own floor).

## Build on GitHub

One workflow, **Build desktop apps**, and nothing builds on a push to a branch. Open the
repository's **Actions** tab, press **Run workflow**, choose the branch and either `all`
platforms or a single one while iterating — about 10 minutes per platform. Download the
results from the finished run under **Artifacts**.

Each job runs `tests/test_core.py` on its own platform, freezes the app, then **launches
what it just froze** and runs its `--self-test`, so a bundle that cannot start fails the
job. That check has two tiers: the packaging checks (bundled TIFF decoders, npe2 discovery,
matplotlib's Qt backend) always have to pass, while the napari-viewer check needs a live
OpenGL context and reports a visible SKIP where there is none. macOS and Linux runners have
one — real, and xvfb with mesa — so they add `--require-gui`, which makes a missing context
a failure. The Windows runner has no GPU driver, and software GL there is best-effort, so
the viewer check may report a skip; check a Windows release candidate on real hardware with
`--smoke`.

## Release

Rehearse first, then tag. Both use the same build.

**1. Try it.** Actions ▸ **Build desktop apps** ▸ Run workflow on `main`, platforms
`all`. When it finishes, download the archives from the run's **Artifacts** and open the
app. No release is created by a dispatched run.

**2. Publish.** Make the version and the tag agree, then push the tag:

```bash
# `version` in pyproject.toml must equal the tag without its "v"
git commit -am "release v0.1.1"
git push
git tag v0.1.1
git push origin v0.1.1
```

The tag runs the same four builds and then a `release` job, which attaches all four
platform archives **and** the wheel to a new GitHub Release, named after the tag, with
download instructions and the commit log in the notes. Nothing to upload by hand.

Guards, so a bad tag cannot become a release: the release job refuses to run if the tag
disagrees with `version` in `pyproject.toml`, and it only runs at all if every platform
built *and* passed its self-test. A tag containing `-` (`v0.2.0-rc1`) is marked as a
pre-release.
