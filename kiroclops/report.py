"""Build a portable, offline HTML report from KiroClops capture artifacts.

The report is a viewer, not a judge: analysis readiness and comparison outcomes
remain separate, and missing or malformed artifacts remain visible.
"""

from __future__ import annotations

import html
import json
import math
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def _safe_json(value: Any) -> Any:
    """Keep embedded JSON valid even when an upstream metric is non-finite."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    return value


def _json_text(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(_safe_json(value), ensure_ascii=False, indent=indent, allow_nan=False)


def _embedded_json(value: Any) -> str:
    # A script element is HTML raw text even when its type is application/json.
    return (_json_text(value).replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _read_json(path: Path, issues: list[str], *, object_only: bool = True) -> Any:
    if not path.is_file():
        issues.append(f"Missing file: {path.name}")
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        issues.append(f"Unreadable JSON: {path.name} ({type(exc).__name__})")
        return None
    if object_only and not isinstance(result, dict):
        issues.append(f"Invalid JSON structure: {path.name}; an object is expected.")
        return None
    return _safe_json(result)


def _copy_file(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    return True


def _paths(value: Iterable[str | Path] | str | Path | None) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [Path(value)]
    return [Path(item) for item in value]


def _status(analysis: dict[str, Any] | None, issues: list[str]) -> str:
    if analysis is None:
        return "invalid" if any("JSON" in issue for issue in issues) else "pending"
    state = str(analysis.get("status", "pending")).lower()
    if state in {"invalid", "failed", "error", "rejected"}:
        return "invalid"
    if state in {"ok", "valid", "success", "complete", "completed", "ready", "passed"}:
        return "ready"
    return "pending"


def _auxiliary(path: Path, target: Path, href: str) -> dict[str, Any]:
    issues: list[str] = []
    data = _read_json(path, issues, object_only=False)
    copied = _copy_file(path, target)
    return {"name": path.name, "data": data, "issues": issues,
            "href": href if copied else None}


def generate_report(
    run_dirs: Iterable[str | Path] | str | Path,
    output_dir: str | Path,
    profile_path: str | Path | None = None,
    comparison_paths: Iterable[str | Path] | str | Path | None = None,
) -> Path:
    """Copy artifacts and return the generated ``index.html`` path.

    A run can be incomplete while capture or analysis is still in progress.
    Malformed JSON is shown as an issue rather than silently dropping the run.
    No network requests, external dependencies, or local server are required.
    """
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    for index, source in enumerate(_paths(run_dirs)):
        issues: list[str] = []
        manifest = _read_json(source / "manifest.json", issues)
        analysis = _read_json(source / "analysis.json", issues)
        folder = f"assets/run-{index:03d}"
        links: dict[str, str] = {}
        for name in ("manifest.json", "analysis.json", "video.mp4"):
            if _copy_file(source / name, output / folder / name):
                links[name] = f"{folder}/{name}"
        if "video.mp4" not in links:
            issues.append("Missing file: video.mp4")
        metadata = manifest or {}
        measurement = analysis or {}
        raw_scenario = metadata.get("scenario", measurement.get("scenario", {}))
        scenario = raw_scenario if isinstance(raw_scenario, dict) else {}
        if raw_scenario and not isinstance(raw_scenario, dict):
            issues.append("Invalid scenario: an object is expected.")
        normal_level = bool(metadata.get("normal_level") is True
                            or scenario.get("normal_level") is True
                            or measurement.get("normal_level") is True)
        if normal_level and "analysis.json" not in links:
            # Normal-map recordings are visual context, not controlled trials.
            issues = [issue for issue in issues if issue != "Missing file: analysis.json"]
        runs.append({
            "id": f"run-{index:03d}", "name": source.name,
            "game": metadata.get("game", measurement.get("game", "unknown")),
            "variant": metadata.get("variant", measurement.get("variant", "unknown")),
            "scenario": scenario, "manifest": manifest, "analysis": analysis,
            "status": _status(analysis, issues), "issues": issues, "links": links,
            "normal_level": normal_level,
        })

    profile = None
    if profile_path is not None:
        profile = _auxiliary(Path(profile_path), output / "assets/profile.json", "assets/profile.json")
    comparisons = [
        _auxiliary(path, output / f"assets/comparison-{index:03d}.json",
                   f"assets/comparison-{index:03d}.json")
        for index, path in enumerate(_paths(comparison_paths))
    ]
    data = {"runs": runs, "profile": profile, "comparisons": comparisons}
    counts = Counter(run["status"] for run in runs if not run["normal_level"])
    normal_count = sum(run["normal_level"] for run in runs)
    summary = (f"Recordings: {len(runs)} · Measurement ready: {counts['ready']} · "
               f"Pending: {counts['pending']} · Invalid: {counts['invalid']}")
    if normal_count:
        summary += f" · Normal-level recordings: {normal_count}"
    substitutions = {
        "__SUMMARY__": _escape(summary),
        "__COMPARISON_SUMMARY__": _comparison_summary(comparisons),
        "__ARTIFACTS__": _artifact_index(runs, profile, comparisons),
        "__REPORT_DATA__": _embedded_json(data),
    }
    page = re.sub(r"__(?:SUMMARY|COMPARISON_SUMMARY|ARTIFACTS|REPORT_DATA)__",
                  lambda match: substitutions[match.group()], _PAGE)
    result = output / "index.html"
    result.write_text(page, encoding="utf-8")
    return result


def _comparison_summary(comparisons: list[dict[str, Any]]) -> str:
    """Present recorded decisions; never derive a pass from metrics or a profile."""
    if not comparisons:
        return ('<section class="result-card pending" aria-label="Validation result">'
                '<h2>Held-out validation result pending</h2>'
                '<p>No comparison file has been added yet; there is no final pass/fail decision.</p></section>')
    cards: list[str] = []
    for item in comparisons:
        result = item["data"] if isinstance(item["data"], dict) else {}
        status = result.get("status")
        tone, title = {
            "pass": ("ready", "Held-out validation passed"),
            "fail": ("invalid", "Held-out validation did not pass"),
            "failed": ("invalid", "Held-out validation did not pass"),
        }.get(status if isinstance(status, str) else "", ("pending", "Held-out validation result pending"))
        facts: list[str] = []
        coverage = result.get("coverage")
        if isinstance(coverage, dict):
            complete, required = coverage.get("complete_triplets"), coverage.get("required_triplets")
            if type(complete) is int and type(required) is int and complete >= 0 and required > 0:
                facts.append(f"{complete}/{required} conditions have reference, before and after recordings.")
        scenarios = result.get("validation_scenarios", [])
        scenarios = scenarios if isinstance(scenarios, list) else []
        for key, label, digits, scale, unit in (
            ("improvement_fraction", "Curve error reduction", 2, 100, "%"),
            ("after_rmse_body", "Final curve error (RMSE)", 4, 1, "body heights"),
        ):
            values = [row[key] * scale for row in scenarios if isinstance(row, dict)
                      and type(row.get(key)) in (int, float) and math.isfinite(row[key])]
            if values:
                low, high = (f"{value:.{digits}f}" for value in (min(values), max(values)))
                spread = low if low == high else f"{low}–{high}"
                facts.append(f"{label}: {spread}{'%' if unit == '%' else ''}"
                             f"{' ' + unit if unit != '%' else ''}.")
        if not facts:
            facts.append("The coverage and measurement summary has not been recorded yet; you can inspect the raw file.")
        if item["issues"]:
            facts.extend(item["issues"])
        details = "".join(f"<p>{_escape(fact)}</p>" for fact in facts)
        cards.append(f'<section class="result-card {tone}" aria-label="Validation result">'
                     f'<h2>{title}</h2>{details}<p class="notice">'
                     f'Source: {_escape(item["name"])} · Comparison result as recorded in the file. '
                     'The status of the calibration profile is kept separate.</p></section>')
    return "\n".join(cards)


def _artifact_index(runs: list[dict[str, Any]], profile: dict[str, Any] | None,
                    comparisons: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    labels = {"ready": "Measurement ready", "pending": "Pending", "invalid": "Invalid"}
    for run in runs:
        status_label = ("Measurement not applied" if run["normal_level"] and "analysis.json" not in run["links"]
                        else labels[run["status"]])
        links = " · ".join(
            f'<a href="{_escape(href)}" download>{_escape(name)}</a>'
            for name, href in run["links"].items())
        issues = "".join(f"<li>{_escape(issue)}</li>" for issue in run["issues"])
        issue_list = f'<ul class="issues">{issues}</ul>' if issues else ""
        errors = (run["analysis"] or {}).get("errors", [])
        provenance = {"manifest": run["manifest"], "analysis_status": (run["analysis"] or {}).get("status"),
                      "analysis_provenance": (run["analysis"] or {}).get("provenance"),
                      "independent_validation": (run["analysis"] or {}).get("annotation_validation"),
                      "analysis_errors": errors}
        sections.append(
            '<details class="artifact"><summary>'
            f'{_escape(run["name"])} <span class="subtle">· {_escape(run["game"])} / '
            f'{_escape(run["variant"])} · {status_label}</span></summary>'
            f'<p>{links or "No downloadable files."}</p>'
            f'{issue_list}'
            '<h3>Source, tool versions and recording information</h3>'
            f'<pre>{_escape(_json_text(provenance, indent=2))}</pre></details>'
        )
    if not runs:
        sections.append('<p class="empty">No recordings have been added yet. The report is waiting for data.</p>')
    if profile is not None:
        sections.append(_auxiliary_html("Fitted profile", profile))
    else:
        sections.append('<p class="subtle">No profile has been added yet.</p>')
    if comparisons:
        sections.append('<h3>Comparison results</h3><p class="subtle">'
                        'The results below are shown as recorded in the files. '
                        'The presence of a file does not imply success.</p>')
        sections.extend(_auxiliary_html(item["name"], item) for item in comparisons)
    else:
        sections.append('<p class="subtle">Comparison result pending; there is no final pass/fail decision.</p>')
    return "\n".join(sections)


def _auxiliary_html(title: str, item: dict[str, Any]) -> str:
    link = (f'<a href="{_escape(item["href"])}" download>Download JSON</a>'
            if item["href"] else "File not available yet.")
    issues = "".join(f"<li>{_escape(issue)}</li>" for issue in item["issues"])
    issue_list = f'<ul class="issues">{issues}</ul>' if issues else ""
    return (f'<details class="artifact"><summary>{_escape(title)}</summary><p>{link}</p>'
            f'{issue_list}'
            f'<pre>{_escape(_json_text(item["data"], indent=2))}</pre></details>')


_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>KiroClops · Motion comparison</title>
<style>
:root{color-scheme:dark;--bg:#10151c;--panel:#18212b;--line:#344353;--text:#edf2f7;--muted:#acbbca;--ref:#91d4ed;--before:#ffbd7a;--after:#a7db9e}
.result-card{margin:0 0 24px;padding:18px 22px;background:var(--panel);border:1px solid var(--line);border-left:4px solid #7d8fa0;border-radius:8px}.result-card.ready{border-left-color:var(--after)}.result-card.invalid{border-left-color:#ffb9bd}.result-card h2{margin:0 0 10px}.result-card p{margin:5px 0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1320px;margin:auto;padding:34px 26px 60px}header{border-bottom:1px solid var(--line);padding-bottom:24px;margin-bottom:24px}.brand{color:var(--ref);font-size:13px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}h1{font-size:clamp(25px,3vw,38px);line-height:1.18;font-weight:650;margin:12px 0}h2{font-size:20px;margin:0 0 14px}h3{font-size:15px;margin:0}p{margin:10px 0}.subtle,.description{color:var(--muted)}.description{max-width:850px}.summary{font-variant-numeric:tabular-nums;color:var(--muted);margin-top:16px}.toolbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:20px 0}.toolbar label{font-weight:600}select,button,input{font:inherit}select,button{background:#23303e;border:1px solid #576a7e;color:var(--text);border-radius:7px;padding:9px 13px}select{min-width:min(100%,420px);max-width:100%}button{cursor:pointer}button:hover:not(:disabled){background:#304258}button:disabled{opacity:.45;cursor:not-allowed}button:focus-visible,select:focus-visible,input:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid var(--ref);outline-offset:3px}a{color:var(--ref);text-underline-offset:3px}.panes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.pane{min-width:0;border:1px solid var(--line);border-radius:10px;background:var(--panel);overflow:hidden}.pane-head{padding:14px 16px;border-top:3px solid var(--tone);display:flex;align-items:center;justify-content:space-between;gap:8px}.video-wrap{aspect-ratio:16/10;background:#080b10;display:flex;align-items:center;justify-content:center}.video-wrap video{width:100%;height:100%;object-fit:contain;image-rendering:pixelated}.video-wrap .empty{padding:20px;text-align:center}.pane-meta{padding:12px 16px;color:var(--muted);font-size:13px}.badge{display:inline-block;white-space:nowrap;border:1px solid #58697b;border-radius:5px;padding:2px 7px;font-size:12px;font-weight:500;background:#24313e}.badge.ready{border-color:#5c8465;color:#bbdfc1}.badge.invalid{border-color:#b96d74;color:#ffb9bd}.badge.pending{color:#d5dee8}.empty{color:var(--muted)}.controls{display:flex;gap:9px;align-items:center;flex-wrap:wrap;padding:17px 0 5px}.timeline{width:100%;accent-color:var(--ref);margin:12px 0}.time-label{font-variant-numeric:tabular-nums;margin-left:auto;color:var(--muted);font-size:13px}.notice{color:var(--muted);font-size:13px;min-height:20px}.issues{color:#ffc6a5}.section{margin-top:30px;border-top:1px solid var(--line);padding-top:24px}.graph-wrap{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;overflow:hidden}.graph-wrap svg{display:block;width:100%;height:auto;min-height:230px}.legend{display:flex;gap:22px;flex-wrap:wrap;margin:6px 0 12px;font-size:13px}.legend span::before{content:"";display:inline-block;width:20px;height:3px;margin:0 7px 4px 0;background:var(--tone)}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{padding:12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:13px;color:var(--muted);font-weight:600}td:first-child,th:first-child{padding-left:0}.artifact{margin:10px 0;border:1px solid var(--line);border-radius:7px;background:var(--panel);padding:13px 16px}.artifact summary{cursor:pointer;font-weight:600;overflow-wrap:anywhere}.artifact pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#111820;border-radius:6px;padding:14px;max-height:450px;overflow:auto;font:12px/1.6 ui-monospace,Consolas,monospace}.artifact h3{margin-top:18px}.normal-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}.normal-grid video{width:100%;max-height:320px;background:#080b10}.normal-card{border:1px solid var(--line);border-radius:8px;overflow:hidden}.normal-card .pane-meta{padding:14px}footer{margin-top:26px;font-size:12px;color:var(--muted)}[hidden]{display:none!important}@media(max-width:760px){main{padding:24px 16px 40px}.panes{grid-template-columns:1fr}.video-wrap{aspect-ratio:16/9}.toolbar{align-items:stretch;flex-direction:column}.time-label{width:100%;margin-left:0}th,td{padding:10px 7px}.graph-wrap{padding:2px}}
</style>
</head>
<body><main>
<header><div class="brand">KiroClops / Stage 1</div>
<h1>Motion comparison</h1>
<p class="description">Review the reference, pre-change and post-change recordings together.
Videos are aligned to the input moment; curves are aligned to the moment of takeoff. Times are real seconds; the time scale is not altered.</p>
<p class="summary">__SUMMARY__</p></header>
__COMPARISON_SUMMARY__
<noscript><p class="issues">JavaScript is required for synchronized playback and charts. You can open the recording files below directly.</p></noscript>
<section aria-labelledby="comparison-heading"><h2 id="comparison-heading">Controlled jump</h2>
<div class="toolbar"><label for="scenario">Scenario</label><select id="scenario" aria-describedby="scenario-description"></select></div>
<p id="scenario-description" class="notice"></p>
<div class="panes" id="panes"></div>
<div class="controls"><button id="play" type="button">Play together</button><button id="previous" type="button" aria-label="Back one reference frame">← Frame</button><button id="next" type="button" aria-label="Forward one reference frame">Frame →</button><button id="input-time" type="button">Input time</button><output class="time-label" id="time-label" for="seek">Relative to input: 0.000 s</output></div>
<input id="seek" class="timeline" type="range" min="0" max="1" value="0" step="0.016666667" aria-label="Shared video time">
<p id="playback-notice" class="notice" role="status" aria-live="polite"></p>
</section>
<section class="section" aria-labelledby="trajectory-heading"><h2 id="trajectory-heading">Jump curve</h2>
<div class="legend"><span style="--tone:var(--ref)">Reference</span><span style="--tone:var(--before)">Before</span><span style="--tone:var(--after)">After</span></div>
<div class="graph-wrap"><svg id="trajectory" viewBox="0 0 1050 345" role="img" aria-label="Jump height relative to body height and real elapsed time"></svg></div>
<p class="notice">Height is divided by the character's body height. Frames where tracking is invalid are not joined; invalid recordings are drawn with a dashed line.</p></section>
<section class="section" aria-labelledby="metrics-heading"><h2 id="metrics-heading">Measurements</h2><div class="table-wrap"><table id="metrics"></table></div>
<p class="notice">“Measurement ready” is only an analysis status. The success of the transfer is judged by the separate comparison and validation results.</p></section>
<section id="normal-section" class="section" hidden aria-labelledby="normal-heading"><h2 id="normal-heading">Normal-level recordings</h2>
<p class="description">These recordings are for examining in-game behavior. They are kept separate from the controlled jump comparison.</p><div id="normal-runs" class="normal-grid"></div></section>
<section class="section" aria-labelledby="artifacts-heading"><h2 id="artifacts-heading">Results and source records</h2>
<p class="description">Tool versions, source information, analysis errors and raw results are kept here. No success result is inferred from missing data.</p>
__ARTIFACTS__</section>
<footer>Offline report · Videos and JSON files can be moved together with this folder. No network connection or server is required.</footer>
</main>
<script id="report-data" type="application/json">__REPORT_DATA__</script>
<script>
"use strict";
(() => {
const data = JSON.parse(document.getElementById("report-data").textContent);
const variants = ["reference", "before", "after"];
const labels = {reference:"Reference",before:"Before",after:"After"};
const colors = {reference:"#91d4ed",before:"#ffbd7a",after:"#a7db9e"};
const statusLabels = {ready:"Measurement ready",invalid:"Invalid",pending:"Pending"};
const splitLabels = {calibration:"Calibration",validation:"Held-out validation"};
const byId = (id) => document.getElementById(id);
const el = (tag,text,className) => {const node=document.createElement(tag);if(text!==undefined)node.textContent=String(text);if(className)node.className=className;return node;};
const num = (value) => typeof value === "number" && Number.isFinite(value) ? value : null;
const positive = (value,fallback) => num(value)>0 ? value : fallback;
const format = (value,digits=3) => num(value)===null ? "—" : value.toLocaleString("en-US",{minimumFractionDigits:digits,maximumFractionDigits:digits});
const fpsOf = (run) => positive(run?.manifest?.fps,positive(run?.analysis?.fps,60));
const startOf = (run) => (num(run?.scenario?.start_tick) ?? 0) / fpsOf(run);
const badge = (status) => el("span",statusLabels[status] || "Pending",`badge ${status || "pending"}`);
const groups = new Map();
for (const run of data.runs.filter(item=>!item.normal_level)) {
  if(!variants.includes(run.variant))continue;
  const s=run.scenario||{};
  const key=JSON.stringify([s.split??"unknown",s.hold_ticks??null,s.repeat??1,s.spawn_offset??0]);
  if(!groups.has(key))groups.set(key,{split:s.split??"unknown",hold:s.hold_ticks,repeat:s.repeat??1,offset:s.spawn_offset??0,runs:{},duplicates:[]});
  const group=groups.get(key);
  if(group.runs[run.variant])group.duplicates.push(run.name);else group.runs[run.variant]=run;
}
const allGroups=[...groups.values()].sort((a,b)=>{
  const rank={calibration:0,validation:1};
  return (rank[a.split]??2)-(rank[b.split]??2)||(num(a.hold)??0)-(num(b.hold)??0)||String(a.repeat).localeCompare(String(b.repeat));
});
const select=byId("scenario");const optionGroups=new Map();
allGroups.forEach((group,index)=>{
  if(!optionGroups.has(group.split)){const node=el("optgroup");node.label=splitLabels[group.split]||String(group.split);select.append(node);optionGroups.set(group.split,node);}
  const text=`${group.hold??"?"}-frame press · repeat ${group.repeat} · start offset ${group.offset}`;
  const option=el("option",text);option.value=String(index);optionGroups.get(group.split).append(option);
});
if(!allGroups.length){select.append(el("option","No matchable scenario yet"));select.disabled=true;}
let videos=[],currentGroup=null,playing=false,animation=null,cursor=0,minTime=0,maxTime=1,masterFps=60,generation=0;
const seek=byId("seek");
function stop(){playing=false;if(animation!==null)cancelAnimationFrame(animation);animation=null;videos.forEach(item=>item.video.pause());byId("play").textContent="Play together";}
function updateLabel(){byId("time-label").textContent=`Relative to input: ${format(cursor)} s · ${format(1/masterFps,4)} s / frame`;}
function setTime(value){cursor=Math.min(maxTime,Math.max(minTime,value));seek.value=String(cursor);updateLabel();for(const item of videos){const t=Math.max(0,cursor+item.start);if(Number.isFinite(item.video.duration)){item.video.currentTime=Math.min(t,Math.max(0,item.video.duration-0.0001));}else{try{item.video.currentTime=t;}catch(_){/* Metadata will retry the seek. */}}}}
function timeline(){
  if(!videos.length){minTime=0;maxTime=1;}else{
    minTime=Math.max(...videos.map(item=>-item.start));
    const ends=videos.map(item=>{const duration=Number.isFinite(item.video.duration)?item.video.duration:item.duration;return duration-item.start;});
    maxTime=Math.min(...ends);if(!Number.isFinite(maxTime)||maxTime<=minTime)maxTime=minTime+1/masterFps;
  }
  seek.min=String(minTime);seek.max=String(maxTime);seek.step=String(1/masterFps);
  for(const id of ["play","previous","next","input-time","seek"])byId(id).disabled=!videos.length;
  setTime(cursor);
}
function transportNotice(){
  if(videos.some(item=>item.video.readyState>=2&&item.video.seekable.length&&item.video.seekable.end(item.video.seekable.length-1)<=0)){
    byId("playback-notice").textContent="This preview server does not support seeking in video. For synchronized frame-by-frame inspection, open the report folder offline or use a server that supports byte ranges.";
  }
}
function tick(){
  if(!playing||!videos.length)return;
  const master=videos[0];cursor=master.video.currentTime-master.start;
  if(master.video.ended||cursor>=maxTime-0.5/masterFps){stop();setTime(maxTime);return;}
  seek.value=String(cursor);updateLabel();
  for(const item of videos.slice(1)){const expected=cursor+item.start;if(Math.abs(item.video.currentTime-expected)>0.075&&!item.video.seeking)item.video.currentTime=Math.max(0,expected);}
  animation=requestAnimationFrame(tick);
}
async function play(){
  if(playing){stop();return;}if(!videos.length)return;
  if(cursor>=maxTime-1/masterFps)setTime(minTime);
  const token=generation;playing=true;byId("play").textContent="Pause together";
  const results=await Promise.allSettled(videos.map(item=>item.video.play()));
  if(token!==generation||!playing)return;
  if(results.some(result=>result.status==="rejected")){stop();byId("playback-notice").textContent="A video could not be played. Try opening the MP4 file directly from the source records.";return;}
  byId("playback-notice").textContent="";transportNotice();animation=requestAnimationFrame(tick);
}
byId("play").addEventListener("click",play);
byId("previous").addEventListener("click",()=>{stop();setTime(cursor-1/masterFps);});
byId("next").addEventListener("click",()=>{stop();setTime(cursor+1/masterFps);});
byId("input-time").addEventListener("click",()=>{stop();setTime(0);});
seek.addEventListener("input",()=>{stop();setTime(Number(seek.value));});
function render(group){
  stop();generation+=1;const token=generation;videos=[];currentGroup=group;cursor=0;
  byId("panes").replaceChildren();byId("playback-notice").textContent="";
  const reference=group?.runs.reference;masterFps=fpsOf(reference||group?.runs.before||group?.runs.after);
  byId("scenario-description").textContent=group?`${splitLabels[group.split]||group.split}. Matching: press duration, repeat and start offset.${group.duplicates.length?" This scenario has duplicate recordings; the first one is shown and all are kept in the sources section.":""}`:"The report can be regenerated as missing recordings arrive.";
  for(const variant of variants){
    const run=group?.runs[variant];const card=el("article",undefined,"pane");card.style.setProperty("--tone",colors[variant]);
    const head=el("div",undefined,"pane-head");head.append(el("h3",labels[variant]),badge(run?.status));card.append(head);
    const wrap=el("div",undefined,"video-wrap");
    if(run?.links["video.mp4"]){
      const video=el("video");video.src=run.links["video.mp4"];video.preload="metadata";video.muted=true;video.playsInline=true;video.setAttribute("aria-label",`${labels[variant]} video`);
      const item={video,start:startOf(run),duration:positive(run.manifest?.frame_count,240)/fpsOf(run)};videos.push(item);
      video.addEventListener("loadedmetadata",()=>{if(token===generation)timeline();});
      video.addEventListener("canplay",()=>{if(token===generation)transportNotice();});
      video.addEventListener("error",()=>{if(token===generation)byId("playback-notice").textContent="At least one video could not be read; the measurements and source files remain available for inspection.";});
      wrap.append(video);
    }else wrap.append(el("p","No video yet.","empty"));
    card.append(wrap);
    const meta=el("div",undefined,"pane-meta");meta.append(el("div",run?`${run.game} · ${run.name}`:"Waiting for a recording for this scenario."));
    if(run)meta.append(el("div",`${fpsOf(run)} fps · input frame ${run.scenario.start_tick??"?"}`));
    card.append(meta);byId("panes").append(card);
  }
  timeline();renderGraph(group);renderMetrics(group);
}
select.addEventListener("change",()=>render(allGroups[Number(select.value)]));
const svgNS="http://www.w3.org/2000/svg";
function svgNode(tag,attrs={},text){const node=document.createElementNS(svgNS,tag);Object.entries(attrs).forEach(([key,value])=>node.setAttribute(key,String(value)));if(text!==undefined)node.textContent=String(text);return node;}
function pointsOf(run){
  const a=run?.analysis;if(!a||!Array.isArray(a.frames)||num(a.events?.takeoff_frame)===null)return[];
  const takeoff=a.events.takeoff_frame,landing=num(a.events?.landing_frame),fps=fpsOf(run);
  const origin=a.frames.find(frame=>frame?.frame_index===takeoff);
  const originTime=num(origin?.time_s)??takeoff/fps;
  const baseline=num(origin?.y),body=positive(a.body_height_px,null);
  return a.frames.filter(frame=>frame&&num(frame.frame_index)!==null&&frame.frame_index>=takeoff&&(landing===null||frame.frame_index<=landing)).map(frame=>{
    const time=(num(frame.time_s)??frame.frame_index/fps)-originTime;
    const height=num(frame.height_body)??(baseline!==null&&num(frame.y)!==null&&body?(baseline-frame.y)/body:null);
    return frame.valid===false||height===null||!Number.isFinite(time)?null:{x:time,y:height};
  });
}
function renderGraph(group){
  const svg=byId("trajectory");svg.replaceChildren();
  const series=variants.map(variant=>({variant,run:group?.runs[variant],points:pointsOf(group?.runs[variant])}));
  const all=series.flatMap(item=>item.points.filter(Boolean));
  if(!all.length){svg.append(svgNode("text",{x:525,y:165,"text-anchor":"middle",fill:"#acbbca","font-size":16},"Waiting for valid trajectory data."));return;}
  const box={left:74,top:24,width:946,height:260};
  let maxX=Math.max(...all.map(point=>point.x),0.1);let minY=Math.min(0,...all.map(point=>point.y));let maxY=Math.max(0.1,...all.map(point=>point.y));
  maxY+=(maxY-minY)*.08;
  const x=value=>box.left+value/maxX*box.width;const y=value=>box.top+box.height-(value-minY)/(maxY-minY)*box.height;
  for(let i=0;i<=4;i++){
    const yv=minY+(maxY-minY)*i/4,xv=maxX*i/4;
    svg.append(svgNode("line",{x1:box.left,y1:y(yv),x2:box.left+box.width,y2:y(yv),stroke:"#344353"}));
    svg.append(svgNode("text",{x:box.left-12,y:y(yv)+4,"text-anchor":"end",fill:"#acbbca","font-size":12},format(yv,2)));
    svg.append(svgNode("text",{x:x(xv),y:box.top+box.height+24,"text-anchor":"middle",fill:"#acbbca","font-size":12},format(xv,2)));
  }
  svg.append(svgNode("text",{x:box.left,y:14,fill:"#acbbca","font-size":12},"Height / body height"));
  svg.append(svgNode("text",{x:box.left+box.width/2,y:337,"text-anchor":"middle",fill:"#acbbca","font-size":12},"Real time since takeoff (s)"));
  for(const item of series){
    let path="",connected=false;
    for(const point of item.points){if(!point){connected=false;continue;}path+=`${connected?"L":"M"}${x(point.x).toFixed(2)},${y(point.y).toFixed(2)} `;connected=true;}
    if(!path)continue;
    const node=svgNode("path",{d:path,fill:"none",stroke:colors[item.variant],"stroke-width":2.6,"stroke-linejoin":"round","stroke-linecap":"round"});
    if(item.run.status!=="ready")node.setAttribute("stroke-dasharray","7 5");
    node.append(svgNode("title",{},`${labels[item.variant]} · ${statusLabels[item.run.status]}`));svg.append(node);
  }
}
function renderMetrics(group){
  const table=byId("metrics");table.replaceChildren();const head=el("thead");const header=el("tr");
  header.append(el("th","Measurement"));variants.forEach(variant=>header.append(el("th",labels[variant])));head.append(header);table.append(head);
  const body=el("tbody");
  const rows=[
    ["Analysis status",run=>badge(run?.status)],
    ["Jump height",run=>`${format(run?.analysis?.metrics?.height_body)} body heights`],
    ["Rise time",run=>`${format(run?.analysis?.metrics?.rise_s)} s`],
    ["Fall time",run=>`${format(run?.analysis?.metrics?.fall_s)} s`],
    ["Input → takeoff",run=>`${format(run?.analysis?.metrics?.input_latency_s)} s`],
    ["Body height",run=>`${format(run?.analysis?.body_height_px,1)} px`],
    ["Valid tracking ratio",run=>num(run?.analysis?.valid_ratio)===null?"—":`${format(run.analysis.valid_ratio*100,1)}%`],
    ["Takeoff / apex / landing frame",run=>["takeoff_frame","apex_frame","landing_frame"].map(key=>num(run?.analysis?.events?.[key])??"—").join(" / ")],
  ];
  for(const [label,read] of rows){const row=el("tr");const title=el("th",label);title.scope="row";row.append(title);for(const variant of variants){const cell=el("td");const value=read(group?.runs[variant]);if(value instanceof Node)cell.append(value);else cell.textContent=value;row.append(cell);}body.append(row);}table.append(body);
}
const normalRuns=data.runs.filter(run=>run.normal_level);
if(normalRuns.length){byId("normal-section").hidden=false;for(const run of normalRuns){const card=el("article",undefined,"normal-card");const meta=el("div",undefined,"pane-meta");const state=run.links["analysis.json"]?badge(run.status):el("span","Measurement not applied","badge");meta.append(el("h3",`${labels[run.variant]||run.variant} · ${run.game}`),el("p",run.name),state);card.append(meta);if(run.links["video.mp4"]){const video=el("video");video.src=run.links["video.mp4"];video.controls=true;video.preload="metadata";video.setAttribute("aria-label",`${run.name} normal-level video`);card.append(video);}else card.append(el("p","No video yet.","pane-meta"));byId("normal-runs").append(card);}}
render(allGroups[0]);
})();
</script></body></html>
'''
