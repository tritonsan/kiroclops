# Local web/SVG adapter v1

This package runs only a fresh headless Chromium and a loopback HTTP server.
It never opens a visible browser, takes desktop input, changes candidate code,
calls a model or contacts AWS. Reference fixture sources are private engineering
inputs and must never be copied into a model workspace.

## Public interfaces

```python
from kiroclops.web_local.specs import fixture_project, scenarios, editable_files
from kiroclops.web_local.capture import capture_run
from kiroclops.web_local.selection import select_package_from_pixels

scenario = scenarios("panel", "calibration")[0]
result = capture_run(fixture_project("panel", "reference"),
                     "artifacts/phase3a/new-output", scenario, variant="reference")
selection = select_package_from_pixels(result["package_dir"], "panel")
```

- Domains: `panel` (`web`, `web-panel` aliases), `svg` (`structural` alias).
- `fixture_project(domain, variant)` returns the separate `reference` or clean
  `target` project; nothing launches or changes on import.
- `scenarios(domain, split)` returns four calibration or twelve validation
  recipes. SVG cases genuinely change node counts: 3→4 / 4→3 calibration;
  3→5, 5→6 with interrupted drag, 6→4 validation at two sizes/two repeats.
- `editable_files(domain)` is `src/panel-motion.js` or `src/layout-motion.js`.
- `capture_run(project, output, scenario, *, variant='target', cancel=None,
  progress=None, build_package=True)` returns `status`, `package_dir`,
  `oracle_path`, `timing`, `functional`, `record_path`, `source_hashes`.
  An existing output is rejected. Cancel may be a threading Event or callable.
- Package pixels/time/input receipts are in `output/package`. DOM/native
  values are in `output/private`; they never enter the visual package.
- `select_package_from_pixels(package, domain)` finds genuine colored UI
  artwork in rendered PNGs; emits root tracking `objects`, `connections` and
  a visually chosen static header `background_rois`. Each object has `method`,
  `roi`, `anchor`, `frame_index`, first/last pixel appearance and optional
  independent full-card contour ROI. Use reference selections/contracts for
  candidates with input-clock alignment; do not excuse candidate omissions
  by generating its own expected lifecycle.

## Clock and evidence

Motion frames use CDP `Page.screencastFrame.metadata.timestamp` (epoch seconds).
Trusted input events use `performance.timeOrigin + performance.now()`; host
round-trip brackets verify this clock mapping at start and end. No nominal
60 FPS or Godot physics-step assumption is used.

Compositor recording can omit identical paints. Initial, idle and final real
CDP screenshots provide independent observations of stationary intervals.
Their actual request intervals and half-width uncertainty are stored per
frame. The compositor stream stays live during idle checkpoints. If a real
compositor timestamp overlaps a screenshot's uncertainty bracket, the redundant
screenshot is excluded and its actual bracket and PNG hash remain in the receipt.
The retained compositor observation is never shifted to impose an artificial order.
Retained frame intervals must not overlap. A timestamp uncertainty over 20 ms
or an excessive motion gap fails the recording; timestamps are never repaired.

The private DOM oracle verifies visible object/node identity, actual counts,
selection preservation and edge attachment separately. It is not a tracker,
fitter or substitute for a failed visual check. A visual connection uses
observed card borders and rendered line segments, not oracle node centers.

The panel has three independently meaningful observations: its drag handle,
the bookmark inside its real title group, and the star inside its real content
group. `advanceMotion(state,target,dt,{open,dragging})` returns panel x/velocity,
titleY/titleVelocity and contentY/contentVelocity; additional controller state
persists. Fixed app rendering applies these to genuine HTML groups. Selection
declares title/content motion relative to the handle so a large parent movement
cannot conceal a wrong child delay. Initial UI offsets are 24/40 CSS pixels;
the private reference's movement law is never copied into a model workspace.

## Local execution

Use the isolated `.venv-local`; dependencies are in
`requirements-web-local.txt`. Playwright/Chromium versions are recorded.

```powershell
.\.venv-local\Scripts\python.exe -B -m kiroclops.web_local.capture --domain panel --variant reference --output artifacts/phase3a/new-panel-capture
.\.venv-local\Scripts\python.exe -B -m kiroclops.web_local.capture --domain svg --variant reference --split validation --scenario 4 --output artifacts/phase3a/new-svg-capture
.\.venv-local\Scripts\python.exe -B -m unittest tests.test_web_local
```

Set `KIROCLOPS_BROWSER_TESTS=1` only when intentionally running the additional
headless integration test; unit tests do not otherwise start a browser.
No historical protocol or game adapter is modified by this package.
