"""Prospective browser recipes and strict comparison for reference suites.

Historical matrices and uploaded single-clip acceptance are unchanged. Only
selected pixels enter analysis; the optional functional browser checks are a
separate application oracle and never provide fitting coordinates.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import math
from pathlib import Path
import statistics

from kiroclops.visual.schema import VisualError, digest, write_json
from kiroclops.web_local.specs import (
    TIMING_LIMITS, SVG_CAPTURE_POLICY, QUALIFIED_CAPTURE_POLICY,
    canonical_domain, capture_scenarios, validate_scenario,
)

VERSION = "reference-suite-web-v1"
PANEL_EVENT_POLICY = "observed-confirmed-rest-departure-v3"


def event_contract(domain):
    """The exact policy shared by production analysis and independent review."""
    domain = canonical_domain(domain)
    return {
        "min_valid_ratio": .95, "require_static_camera": True,
        "event_options": {
            "motion_policy": PANEL_EVENT_POLICY, "rest_duration_s": .20 if domain == "panel" else .25,
            "connection_tolerance_px": 2., "max_gap_s": .12,
            "max_event_uncertainty_s": .1,
        },
    }


def _click(time_s, selector):
    return {"time_s": time_s, "action": "click", "selector": selector}


def _drag(domain, start, *, duration=.30, release=None, travel=.40):
    destination = {"travel_fraction": travel} if domain == "panel" else {"viewport_x": .64, "viewport_y": .32}
    result = [{"time_s": start, "action": "drag_start",
               "selector": "#panel-handle" if domain == "panel" else '[data-node-id="b"]',
               "destination": destination}]
    result += [{"time_s": start + duration * index / 8, "action": "drag_move", "fraction": index / 8}
               for index in range(1, 9)]
    result.append({"time_s": release if release is not None else start + duration + .05,
                   "action": "drag_end"})
    return result


def scenarios(domain, split):
    """Return new immutable-by-value recipes; known cases are never blind."""
    domain = canonical_domain(domain)
    split = {"known": "known_regression", "regression": "known_regression"}.get(split, split)
    if split not in {"calibration", "validation", "known_regression"}:
        raise VisualError("invalid_suite_split", "Expected calibration, validation or known_regression")
    if split == "known_regression":
        result = capture_scenarios(domain, "validation", QUALIFIED_CAPTURE_POLICY)
        for value in result:
            value["legacy_id"] = value["id"]
            value["case_id"] = value["id"] = f"suite-known-{domain}-{value['id']}"
            value["suite_split"] = split
            value["suite_version"] = VERSION
            value["blind_validation"] = False
        return deepcopy(result)
    if domain == "panel":
        first, second = "#open-panel", "#close-panel"
        recipes = [
            ("open", [_click(.4, first)], 3.),
            ("open-close", [_click(.4, first), _click(1.8, second)], 3.8),
            ("interrupt", [_click(.4, first), _click(.65, second)], 3.),
            ("reverse", [_click(.4, first), _click(.65, second), _click(.85, first)], 3.8),
        ] if split == "calibration" else [
            ("interrupt-new", [_click(.4, first), _click(.58, second)], 3.),
            ("reverse-new", [_click(.4, first), _click(.58, second), _click(.81, first)], 3.8),
            ("drag-release-new", [_click(.4, first), *_drag(domain, 1., release=1.35)], 3.8),
        ]
    else:
        first, second = "#add-node", "#remove-node"
        recipes = [
            ("three-to-four", [_click(.4, first)], 3.),
            ("four-to-three", [_click(.4, second)], 3.),
        ] if split == "calibration" else [
            ("three-to-five-new", [_click(.2, '[data-node-id="b"]'), _click(.4, first), _click(.62, first)], 3.8),
            ("five-to-six-drag-new", [_click(.4, first), *_drag(domain, .60, release=.95)], 3.8),
            ("six-to-four-new", [_click(.2, '[data-node-id="b"]'), _click(.4, second), _click(.63, second)], 3.8),
        ]
    sizes = [(960, 640)] if split == "calibration" else [(768, 640), (1280, 720)]
    result = []
    for name, events, duration in recipes:
        for width, height in sizes:
            for repeat in (1, 2):
                case_id = f"suite-{domain}-{split}-{name}-w{width}-r{repeat}"
                value = {
                    "id": case_id, "case_id": case_id, "recipe": name, "domain": domain,
                    "split": split, "suite_split": split, "repeat": repeat,
                    "viewport": [width, height], "device_scale_factor": 1,
                    "duration_s": duration, "events": deepcopy(events),
                    "timing_limits": dict(TIMING_LIMITS), "version": VERSION,
                    "suite_version": VERSION, "blind_validation": split == "validation",
                    "frame_driver": "natural_browser_raf_v1",
                }
                if domain == "svg":
                    counts = {"three-to-four": (3, 4), "four-to-three": (4, 3),
                              "three-to-five-new": (3, 5), "five-to-six-drag-new": (5, 6),
                              "six-to-four-new": (6, 4)}
                    initial, final = counts[name]
                    value.update(SVG_CAPTURE_POLICY)
                    value.update(initial_node_count=initial, expected_final_node_count=final,
                                 expected_selected_id="b" if split == "validation" else None)
                validate_scenario(value)
                result.append(value)
    return result


def capture_record(project, output, scenario, check, progress, cancel, variant, reference_selection=None):
    """Capture with existing native clocks, then analyze once with suite policy."""
    from . import evaluation
    from kiroclops.visual.package import load_package
    from kiroclops.web_local.selection import select_package_from_pixels

    validate_scenario(scenario)
    record = evaluation._capture_web(project, output, scenario, check, progress, cancel, variant, analyze=False)
    manifest = load_package(record["package"])
    domain = canonical_domain(scenario["domain"])
    try:
        selection = select_package_from_pixels(record["package"], domain)
    except ValueError as exc:
        raise VisualError("tracking_failure", str(exc)) from exc
    if reference_selection is not None:
        bindings = reference_selection["lifecycle_bindings"]
        intervals = evaluation._map_lifecycle(bindings, manifest)
        lookup = {obj["id"]: obj for obj in selection["objects"]}
        for expected in reference_selection["objects"]:
            if expected["id"] not in lookup:
                raise VisualError("tracking_failure", "Expected visible object missing: " + expected["id"])
            if scenario.get("analysis_policy") != "observed_response_v2":
                lookup[expected["id"]]["active_intervals_s"] = intervals[expected["id"]]
        for key in ("connections", "relative_motion"):
            if reference_selection.get(key):
                selection[key] = deepcopy(reference_selection[key])
    else:
        bindings = evaluation._lifecycle_bindings(selection, manifest)
    selection["lifecycle_bindings"] = deepcopy(bindings)
    selection["input_origin_s"] = manifest["inputs"]["events"][0]["time_s"] if manifest["inputs"]["events"] else 0.
    destination = Path(output) / "measurement"
    write_json(destination / "selection.json", selection)
    contract = event_contract(domain)
    result = evaluation._analysis(record["package"], selection, destination, check, progress,
                                  contract=contract, candidate_sha256=record.get("source_sha256"))
    return {**record, "analysis": str(destination / "analysis.json"), "selection": selection,
            "measurement_status": result.get("measurement_status"), "observations": result["observations"],
            "events": result["events"], "suite_event_contract": deepcopy(contract),
            "independent_review_contract": deepcopy(contract), "event_contract_sha256": digest(contract)}


def _input_identity(event):
    return {key: event.get(key) for key in ("action", "selector", "fraction", "destination") if key in event}


def _required_inputs(reference, other):
    reference_inputs = reference["observations"].get("inputs", {})
    other_inputs = other["observations"].get("inputs", {})
    if reference_inputs.get("provenance") != "recorder_verified" or other_inputs.get("provenance") != "recorder_verified":
        raise VisualError("unverified_suite_inputs", "Browser suites require actual verified input receipts")
    if [_input_identity(e) for e in reference_inputs.get("events", [])] != [_input_identity(e) for e in other_inputs.get("events", [])]:
        raise VisualError("capture_timing_failure", "Suite input receipt identities differ")


def compare_records(reference, original, candidate, require_improvement=True):
    """All required windows pass independently; means only rank candidates."""
    from . import evaluation
    from .geometry_acceptance import svg_preservation
    from kiroclops.visual.comparison import compare_observations

    domain = canonical_domain(reference["scenario"]["domain"])
    for other in (original, candidate):
        if digest(other["scenario"]) != digest(reference["scenario"]):
            raise VisualError("suite_recipe_mismatch", "Comparison must use the same fixed suite recipe")
        _required_inputs(reference, other)
    policy = event_contract(domain)
    for record in (reference, original, candidate):
        if record.get("suite_event_contract") is not None and record["suite_event_contract"] != policy:
            raise VisualError("suite_policy_mismatch", "Event/review policy differs from the suite contract")
    contract = evaluation._contract(reference, candidate)
    old_contract = evaluation._contract(reference, original)
    contract["event_options"] = deepcopy(policy["event_options"])
    old_contract["event_options"] = deepcopy(policy["event_options"])
    old = compare_observations(reference["observations"], original["observations"], old_contract)
    new = compare_observations(reference["observations"], candidate["observations"], contract)
    pairs = {pair["reference_id"]: pair for pair in contract["object_pairs"]}
    windows = {window["id"]: window for window in contract.get("windows", [])}
    key = lambda row: (row["reference_id"], row["window_id"], row.get("segment_index", 0))
    prior = {key(row): row for row in old["object_windows"]}
    rows = []
    for row in new["object_windows"]:
        previous = prior.get(key(row))
        before = previous.get("position_rmse") if previous else None
        after = row.get("position_rmse")
        finite = lambda value: type(value) in (int, float) and math.isfinite(value) and value >= 0
        gain = 1. - after / before if finite(before) and before > 1e-9 and finite(after) else None
        window = windows.get(row["window_id"], {})
        initial_preservation = (row["window_id"] in ("initial-preservation", "initial_preservation")
                                and window.get("end_s") is not None and window["end_s"] <= 0)
        static = pairs[row["reference_id"]]["static"]
        applicable = not static and not initial_preservation
        gain_pass = not require_improvement or not applicable or gain is not None and gain >= .5
        pixel_gates = row.get("gates", {})
        absolute_pass = all(pixel_gates.get(name) is True for name in ("coverage", "position", "size", "endpoint"))
        rows.append({"object_id": row["reference_id"], "window_id": row["window_id"],
                     "segment_index": row.get("segment_index", 0), "before_rmse": before, "after_rmse": after,
                     "improvement_fraction": gain, "required_fraction": .5, "gain_applicable": applicable,
                     "gain_applicability": "not_applicable" if not applicable else "required" if require_improvement else "not_required",
                     "gain_reason": "static_preservation" if static else "initial_preservation" if initial_preservation
                     else "no_demonstrable_gain" if finite(before) and before <= 1e-9 else "per_response_window_gain",
                     "gain_pass": gain_pass, "absolute_pass": absolute_pass,
                     "pass": absolute_pass and gain_pass,
                     "reason": "static_preservation" if static else "initial_preservation" if initial_preservation
                     else "no_demonstrable_gain" if finite(before) and before <= 1e-9 else "per_window_gain"})
    dynamics = evaluation._dynamic_checks(reference, candidate, contract)
    order = evaluation._relative_timing_checks(reference, candidate, contract)
    order_required = domain == "panel"
    unresolved = order_required and (not order or any(
        (item.get("reference") or {}).get("timing_status") != "resolved"
        or (item.get("candidate") or {}).get("timing_status") != "resolved" for item in order))
    order_pass = not order_required or bool(order) and all(item.get("pass") is True for item in order)
    order_status = "insufficient_observability" if unresolved else "pass" if order_pass else "fail"
    geometry = None
    if domain == "svg":
        geometry = svg_preservation(reference["observations"], candidate["observations"],
                                    reference.get("selection", {}).get("connections", []))
    original_measured = original.get("measurement_status") == "ok" and old.get("measurement_status") == "pass"
    measured = all(record.get("measurement_status") == "ok" for record in (reference, candidate)) and new.get("measurement_status") == "pass"
    measured = measured and (not require_improvement or original_measured) and not unresolved
    if geometry is not None:
        measured = measured and geometry.get("measurement_status") == "ok"
    functional = candidate.get("functional", {}).get("status") == "pass"
    passed = bool(rows and new.get("success") and measured and functional and order_pass
                  and all(row["pass"] for row in rows)
                  and all(row["pass"] for row in dynamics if row["observable"])
                  and (geometry is None or geometry.get("pass")))
    values = [row["after_rmse"] for row in rows if type(row["after_rmse"]) in (int, float) and math.isfinite(row["after_rmse"])]
    baseline_coverage = [row for row in old["object_windows"] if row.get("gates", {}).get("coverage") is False]
    failure_class = geometry.get("failure_class") if geometry else None
    if unresolved:
        failure_class = "required_order_unobservable"
    elif not order_pass:
        failure_class = "required_order_mismatch"
    elif any(row.get("gates", {}).get("coverage") is False for row in new["object_windows"]):
        failure_class = "comparison_coverage_failure"
    elif require_improvement and baseline_coverage:
        failure_class = "baseline_comparison_coverage_failure"
    elif not measured:
        failure_class = "comparison_measurement_failure"
    elif not functional:
        failure_class = "application_integrity_failure"
    elif any(not row["absolute_pass"] for row in rows):
        failure_class = "per_window_pixel_failure"
    elif any(not row["gain_pass"] for row in rows):
        failure_class = "per_window_improvement_failure"
    elif any(not row["pass"] for row in dynamics if row["observable"]):
        failure_class = "observable_dynamics_failure"
    return {"scenario_id": reference["scenario"]["id"], "case_id": reference["scenario"].get("case_id", reference["scenario"]["id"]),
            "pass": passed, "score": statistics.mean(values) if len(values) == len(rows) and rows else None,
            "pixel_comparison": new, "original_pixel_comparison": old,
            "improvement": rows, "per_window_gates": rows, "functional": candidate.get("functional"),
            "measurement_pass": bool(measured), "baseline_measurement_pass": original_measured,
            "baseline_coverage_failures": baseline_coverage,
            "contract": contract, "dynamics": dynamics, "relative_timing": order,
            "required_order_status": order_status, "svg_geometry_preservation": geometry,
            "failure_class": failure_class,
            "suite_event_contract": policy, "independent_review_contract": deepcopy(policy),
            "acceptance_scope": "All required object/window gates; an average score cannot excuse a failure."}


async def _functional_browser_checks(project, domain, check, cancel):
    from playwright.async_api import async_playwright
    from kiroclops.web_local.capture import local_server
    from kiroclops.web_local.natural_capture import browser_arguments
    from .environment import configure_environment

    configure_environment()
    rows, errors = [], []
    def guard():
        check()
        if cancel and cancel():
            raise VisualError("job_cancelled", "Functional checks cancelled")
    async def wait(page, milliseconds=600):
        guard()
        await page.wait_for_timeout(milliseconds)
        guard()
    async def snapshot(page):
        return await page.evaluate("""() => {
          const box=e=>{const b=e.getBoundingClientRect();return [b.x,b.y,b.width,b.height];};
          const objects=[...document.querySelectorAll('#panel,#panel-handle,.bookmark,.star,[data-node-id]')];
          const handle=document.querySelector('#panel-handle');
          return {finite:objects.length>=3&&objects.every(e=>box(e).every(Number.isFinite)),
            count:document.querySelectorAll('[data-node-id]').length,
            selected:[...document.querySelectorAll('[data-node-id].selected')].map(e=>e.dataset.nodeId),
            pointer_capture:objects.some(e=>e.hasPointerCapture(window.__kcSuitePointerId||1)),
            value:handle?Number(handle.getAttribute('aria-valuenow')):null,
            max:handle?Number(handle.getAttribute('aria-valuemax')):null};
        }""")
    with local_server(project) as url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=browser_arguments(True))
            try:
                page = await browser.new_page(viewport={"width": 960, "height": 640}, device_scale_factor=1)
                page.set_default_timeout(10000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.add_init_script("document.addEventListener('pointerdown',e=>window.__kcSuitePointerId=e.pointerId,true)")
                guard()
                await page.goto(url, wait_until="networkidle")
                await page.locator("#reset").click()
                if domain == "panel":
                    handle = page.locator("#panel-handle")
                    await handle.focus()
                    initial = await snapshot(page)
                    await handle.press("ArrowRight")
                    await wait(page, 1000)
                    opened = await snapshot(page)
                    await handle.press("ArrowLeft")
                    await wait(page, 1000)
                    closed = await snapshot(page)
                    rows.append({"id": "keyboard_open_close", "pass": initial["finite"] and opened["finite"] and closed["finite"]
                                 and opened["value"] > initial["value"] + 1 and closed["value"] < opened["value"] - 1})
                    await page.locator("#open-panel").click()
                    await wait(page, 1000)
                    old_size = await snapshot(page)
                    await page.set_viewport_size({"width": 1280, "height": 720})
                    await wait(page, 800)
                    resized = await snapshot(page)
                    rows.append({"id": "resize_open", "pass": resized["finite"] and resized["max"] != old_size["max"]
                                 and abs(resized["value"] - resized["max"]) < abs(old_size["value"] - resized["max"])})
                    drag_target = handle
                else:
                    await page.locator('[data-node-id="b"]').click()
                    await page.locator("#add-node").click()
                    await wait(page)
                    added = await snapshot(page)
                    await page.locator("#remove-node").click()
                    await wait(page)
                    removed = await snapshot(page)
                    rows.append({"id": "selection_add_remove", "pass": added["finite"] and removed["finite"]
                                 and added["count"] == 4 and removed["count"] == 3 and added["selected"] == removed["selected"] == ["b"]})
                    await page.set_viewport_size({"width": 1280, "height": 720})
                    await wait(page)
                    resized = await snapshot(page)
                    rows.append({"id": "resize_structure", "pass": resized["finite"] and resized["count"] == 3 and resized["selected"] == ["b"]})
                    drag_target = page.locator('[data-node-id="b"]')
                box = await drag_target.bounding_box()
                if not box:
                    raise VisualError("application_integrity_failure", "Functional drag target is missing")
                await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                await page.mouse.down()
                captured = await snapshot(page)
                pointer_id = await page.evaluate("window.__kcSuitePointerId||1")
                await drag_target.dispatch_event("pointercancel", {"pointerId": pointer_id, "bubbles": True})
                await page.mouse.up()
                await wait(page)
                released = await snapshot(page)
                rows.append({"id": "pointer_cancel", "pass": captured["pointer_capture"] and not released["pointer_capture"] and released["finite"],
                             "cancel_event_provenance": "synthetic_functional_event"})
                selectors = ("#open-panel", "#close-panel", "#open-panel") if domain == "panel" else ("#add-node", "#remove-node", "#add-node")
                for selector in selectors:
                    guard()
                    await page.locator(selector).click()
                    await wait(page, 40)
                await wait(page, 800)
                rapid = await snapshot(page)
                rows.append({"id": "rapid_mode_changes", "pass": rapid["finite"] and not rapid["pointer_capture"]
                             and (domain == "panel" or rapid["count"] == 4 and rapid["selected"] == ["b"])})
                await page.locator("#reset").click()
                await wait(page)
                reset = await snapshot(page)
                rows.append({"id": "reset", "pass": reset["finite"] and not reset["pointer_capture"]
                             and (domain == "panel" or reset["count"] == 3 and not reset["selected"])})
            finally:
                await browser.close()
    return {"checks": rows, "page_errors": errors,
            "status": "pass" if rows and all(row["pass"] for row in rows) and not errors else "fail"}


def functional_checks(project, domain="panel", output=None, check=lambda: None, cancel=None):
    """Run an explicit separate oracle once per source, never pixel analysis."""
    from kiroclops.web_local.capture import project_hashes

    domain = canonical_domain(domain)
    project = Path(project).resolve()
    before = project_hashes(project)
    if not before:
        raise VisualError("invalid_project", "Functional checks need an existing source project")
    check()
    result = asyncio.run(_functional_browser_checks(project, domain, check, cancel))
    after = project_hashes(project)
    if before != after:
        raise VisualError("source_changed", "Application sources changed during functional checks")
    result.update(kind="ReferenceSuiteFunctionalChecks", schema_version=1, domain=domain,
                  source_sha256=digest(before), checks_source="separate_private_browser_oracle",
                  scope="Keyboard, resize, cancel, rapid input and reset; no reference fitting or pixel acceptance.")
    if output is not None:
        write_json(Path(output), result)
    return result
