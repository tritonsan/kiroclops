"""Frozen-before-model web/SVG scenario recipes, with real timestamp receipts."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import math

# Prospective v3 input challenge: eight real pointer moves in the same 0.25s
# drag. It is not interchangeable with preserved v2 (15 moves/60Hz) evidence.
# Reference and both Kiro arms must be recorded from this identical v3 recipe.
VERSION = "local-web-svg-v3"
TIMING_LIMITS = {"median_interval_s": .050, "p95_interval_s": .080,
                 "maximum_motion_gap_s": .120, "clock_uncertainty_s": .020}
# Qualified together on 16 reference recordings plus independent pixel review:
# artifacts/phase3a/svg-visible-clock-review-01/independent-review.json.
# Partial policies retain qualification-only status; legacy defaults stay intact.
SVG_CAPTURE_POLICY = {'frame_driver':'natural_browser_raf_v1',
                      'analysis_policy':'observed_response_v2',
                      'visual_frame_clock':'svg_raf_tag_v1'}
QUALIFIED_CAPTURE_POLICY = 'qualified-natural-20260927'


def canonical_domain(domain):
    aliases = {"web": "panel", "web-panel": "panel", "panel": "panel", "svg": "svg", "structural": "svg"}
    if domain not in aliases:
        raise ValueError(f"Unsupported local browser domain: {domain}")
    return aliases[domain]


def fixture_project(domain, variant="target"):
    domain = canonical_domain(domain)
    if variant not in {"reference", "target"}:
        raise ValueError("Variant must be reference or target")
    return Path(__file__).resolve().parents[2] / "fixtures" / ("phase3a-web" if domain == "panel" else "phase3a-svg") / variant


def editable_files(domain):
    return ["src/panel-motion.js" if canonical_domain(domain) == "panel" else "src/layout-motion.js"]


def _click(t, selector):
    return {"time_s": t, "action": "click", "selector": selector}


def _drag(domain, start=.9):
    # Pointer-down at .9; eight evenly spaced moves over .25s; .05s hold;
    # pointer-up at 1.2. Browser-received event times remain authoritative.
    selector = "#panel-handle" if domain == "panel" else '[data-node-id="b"]'
    result = [{"time_s": start, "action": "drag_start", "selector": selector,
               "destination": {"travel_fraction": .55} if domain == "panel" else {"viewport_x": .64, "viewport_y": .32}}]
    result += [{"time_s": start + .25*i/8, "action": "drag_move", "fraction": i/8} for i in range(1, 9)]
    result += [{"time_s": start + .3, "action": "drag_end"}]
    return result


def scenarios(domain, split):
    domain = canonical_domain(domain)
    if split not in {"calibration", "validation"}:
        raise ValueError("Split must be calibration or validation")
    if domain == "panel":
        first, second = "#open-panel", "#close-panel"
    else:
        first, second = "#add-node", "#remove-node"
    recipes = [("open", [_click(.4, first)], 3.),
               ("open-close", [_click(.4, first), _click(1.8, second)], 3.8)] if split == "calibration" else [
                   ("interrupt", [_click(.4, first), _click(.65, second)], 3.),
                   ("drag-release", [_click(.4, first), *_drag(domain)], 3.8),
                   ("reverse", [_click(.4, first), _click(.65, second), _click(.85, first)], 3.8)]
    if domain == "svg":
        recipes = [("three-to-four", [_click(.4, first)], 3.),
                   ("four-to-three", [_click(.4, second)], 3.)] if split == "calibration" else [
                       ("three-to-five", [_click(.2, '[data-node-id="b"]'), _click(.4, first), _click(.7, first)], 3.8),
                       ("five-to-six-drag", [_click(.4, first), *_drag(domain, .65)], 3.8),
                       ("six-to-four", [_click(.2, '[data-node-id="b"]'), _click(.4, second), _click(.7, second)], 3.8)]
    sizes = [(960, 640)] if split == "calibration" else [(768, 640), (1280, 720)]
    result = []
    for name, events, duration in recipes:
        for width, height in sizes:
            for repeat in (1, 2):
                record = {"id": f"{name}-w{width}-r{repeat}", "recipe": name,
                               "domain": domain, "split": split, "repeat": repeat,
                               "viewport": [width, height], "device_scale_factor": 1,
                               "duration_s": duration, "events": deepcopy(events),
                               "timing_limits": dict(TIMING_LIMITS), "version": VERSION}
                if domain == "svg":
                    before, after = {"three-to-four":(3,4),"four-to-three":(4,3),"three-to-five":(3,5),
                                     "five-to-six-drag":(5,6),"six-to-four":(6,4)}[name]
                    record.update(initial_node_count=before, expected_final_node_count=after,
                                  expected_selected_id="b" if split=="validation" else None)
                result.append(record)
    return result


def capture_scenarios(domain, split, policy):
    """Explicit prospective selection; old scenario recipes stay unchanged."""
    if policy != QUALIFIED_CAPTURE_POLICY:
        raise ValueError('Unknown qualified capture policy')
    result = scenarios(domain, split)
    for record in result:
        record.update(SVG_CAPTURE_POLICY if canonical_domain(domain) == 'svg'
                      else {'frame_driver':'natural_browser_raf_v1'})
        validate_scenario(record)
    return result


def validate_scenario(scenario):
    canonical_domain(scenario["domain"])
    svg_qualified=(canonical_domain(scenario['domain'])=='svg'
        and all(scenario.get(k)==v for k,v in SVG_CAPTURE_POLICY.items())
        and scenario.get('split') in {'calibration','validation','qualification'})
    if scenario.get('visual_frame_clock') is not None:
        if (scenario['visual_frame_clock']!='svg_raf_tag_v1' or scenario.get('analysis_policy')!='observed_response_v2'
                or (scenario.get('split')!='qualification' and not svg_qualified) or canonical_domain(scenario['domain'])!='svg'):
            raise ValueError('Visible frame clock is SVG qualification-only')
    if scenario.get('analysis_policy') is not None:
        if (scenario['analysis_policy']!='observed_response_v2' or (scenario.get('split')!='qualification' and not svg_qualified)
                or canonical_domain(scenario['domain'])!='svg'):
            raise ValueError('Observed response v2 is SVG qualification-only until independently qualified')
        if scenario.get('frame_driver')!='natural_browser_raf_v1':
            raise ValueError('Observed response qualification requires the natural RAF recorder')
    driver=scenario.get('frame_driver','controlled_begin_frame_v1')
    if driver not in {'controlled_begin_frame_v1','natural_browser_v1','natural_browser_raf_v1'}:
        raise ValueError('Unsupported frame driver')
    if driver.startswith('natural_browser'):
        qualified=svg_qualified or (driver=='natural_browser_raf_v1' and canonical_domain(scenario['domain'])=='panel'
            and scenario.get('split') in {'calibration','validation','qualification'})
        if (not qualified and scenario.get('split')!='qualification') or scenario.get('click_schedule','dispatch_at_due_v1')!='dispatch_at_due_v1':
            raise ValueError('Natural frame observer is qualification-only outside the qualified panel mode, with its own native input schedule')
    if scenario.get('click_schedule','dispatch_at_due_v1') not in {'dispatch_at_due_v1','release_at_due_v1','prepared_hover_v2'}:
        raise ValueError('Unsupported click schedule')
    if scenario.get('click_schedule','dispatch_at_due_v1')!='dispatch_at_due_v1' and scenario.get('split')!='qualification':
        raise ValueError('Experimental click scheduling is qualification-only; not approved for model trials')
    width, height = scenario["viewport"]
    if any(type(n) is not int or not 320 <= n <= 2048 for n in (width, height)):
        raise ValueError("Unsupported viewport")
    if scenario.get("device_scale_factor", 1) != 1:
        raise ValueError("This qualified fixture uses DPR 1")
    if canonical_domain(scenario["domain"]) == "svg" and any(type(scenario.get(k)) is not int or not 3<=scenario[k]<=6
            for k in ("initial_node_count","expected_final_node_count")):
        raise ValueError("SVG initial/final node counts must be integers 3..6")
    duration = scenario["duration_s"]
    if not isinstance(duration, (int, float)) or not 1 <= duration <= 30:
        raise ValueError("Invalid duration")
    previous = -1.
    pressed = False
    for event in scenario["events"]:
        if 'replay_client_point' in event:
            point=event['replay_client_point']
            if (not isinstance(point,list) or len(point)!=2 or any(type(n) not in (int,float) or not math.isfinite(n) for n in point)
                    or not 0<=point[0]<width or not 0<=point[1]<height):
                raise ValueError('Reference pointer replay requires two finite in-viewport client coordinates')
        t = event["time_s"]
        if not isinstance(t, (int, float)) or not previous <= t < duration:
            raise ValueError("Events must be time ordered inside the recording")
        previous = t
        action = event["action"]
        if action not in {"click", "drag_start", "drag_move", "drag_end"}:
            raise ValueError("Unsupported action")
        if action == 'click' and pressed:
            raise ValueError('Click during active drag')
        if action == "drag_start":
            if pressed:
                raise ValueError("Nested drag")
            pressed = True
        elif action in {"drag_move", "drag_end"}:
            if not pressed:
                raise ValueError("Drag action without pointer-down")
            if action == "drag_move" and not 0 <= event["fraction"] <= 1:
                raise ValueError("Invalid drag fraction")
            if action == "drag_end":
                pressed = False
    if pressed:
        raise ValueError("Unreleased pointer")
    return deepcopy(scenario)
