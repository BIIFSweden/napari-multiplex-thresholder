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

Both workflows are manual — nothing runs on a push. Open the repository's **Actions**
tab, pick the workflow in the left sidebar, press **Run workflow**, choose the branch or
tag, and start it:

| workflow | input | result |
|---|---|---|
| **Build desktop apps** | `all`, or one platform | the four double-clickable archives, ~10 min per platform |
| **Build and test wheel** | `full` or `quick` | sdist + wheel, installed and tested on Ubuntu/Windows/macOS × Python 3.11–3.13 |

Every desktop job launches what it just froze and runs its `--self-test`, so a bundle
that cannot start fails the job. That check has two tiers: the packaging checks (bundled
TIFF decoders, npe2 discovery, matplotlib's Qt backend) always have to pass, while the
napari-viewer check needs an OpenGL context and reports a visible SKIP on a machine that
has none. Runners get one anyway — xvfb on Linux, Qt's llvmpipe copied in on Windows —
and are then run with `--require-gui`, which turns a missing context back into a failure.

Download the results from the finished run under **Artifacts**.

## Release

```bash
# bump `version` in pyproject.toml, then:
git commit -am "release v0.1.1"
git push
git tag v0.1.1
git push origin v0.1.1
```

Then, on GitHub:

1. Run **Build desktop apps** (`all`) and **Build and test wheel** against the tag —
   pick `v0.1.1` in the Run workflow ref dropdown. On a `v*` tag the wheel build also
   fails if the tag disagrees with `pyproject.toml`.
2. Download both runs' artifacts.
3. **Releases ▸ Draft a new release**, choose the tag `v0.1.1`, and attach the four
   platform archives and the wheel.

Neither workflow publishes by itself, so the release is only what you attach.
