"""Portable session evidence on top of the existing Phase 1 artifact viewer."""
from __future__ import annotations

import html
import json
from pathlib import Path
import shutil

from kiroclops.report import generate_report
from .gameplay_review import verify_wall_review


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _escape(value):
    return html.escape(str(value), quote=True)


def _json(value):
    return _escape(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def generate_session_report(session_dir) -> Path:
    """Read snapshots only; neither calculate acceptance nor modify a session.

    All linked evidence is copied beneath report/. Paths in session metadata
    must remain inside this session, including when resolved through symlinks.
    """
    session = Path(session_dir).resolve()
    state = _read(session / "session.json")
    if state.get("experiment_id") == "ground-motion-v1":
        from .ground_reporting import generate_ground_report
        return generate_ground_report(session)
    issues = []

    def local(value, *, default=None):
        if value is None:
            value = default
        if value is None:
            return None
        path = Path(value)
        path = (path if path.is_absolute() else session / path).resolve()
        if not path.is_relative_to(session):
            raise ValueError("session report cannot read evidence outside its session")
        return path

    reference = state.get("reference") or {}
    baseline = state.get("baseline_runs") or {}
    final = state.get("final") or {}
    candidates = []
    runs = []
    for group in (reference, baseline):
        for split in ("calibration", "validation"):
            runs.extend(local(value) for value in group.get(split, []))
    for candidate in state.get("candidates", []):
        candidate = dict(candidate)
        cid = candidate.get("candidate_id", "unknown")
        folder = local(f"candidates/{cid}")
        snapshot = folder / "candidate.json"
        if snapshot.is_file():
            saved = _read(snapshot)
            if saved.get("candidate_id") != cid:
                raise ValueError("candidate snapshot identity differs from session")
            if saved != candidate:
                issues.append(f"Candidate snapshot and session state differ: {cid}; session state is shown.")
        else:
            issues.append(f"Missing candidate snapshot: {cid}")
        candidate["_folder"] = folder
        candidate["_snapshot"] = snapshot
        candidates.append(candidate)
    selected_id = state.get("selected_candidate_id")
    if selected_id:
        visual_candidate = next((candidate for candidate in candidates if candidate.get("candidate_id") == selected_id), None)
        visual_basis = "selected candidate"
        if visual_candidate is None:
            issues.append("Selected candidate is missing; no other candidate is substituted in videos or curves.")
    else:
        finished = [candidate for candidate in candidates if candidate.get("status") in
                    {"evaluated", "finalized", "completed", "failed", "cancelled", "interrupted"}]
        visual_candidate = finished[-1] if finished else None
        visual_basis = "latest finished attempt; no candidate selected yet"
    if visual_candidate is not None:
        candidate = visual_candidate
        folder = candidate["_folder"]
        for split in ("calibration", "validation"):
            runs.extend(local(value) for value in candidate.get(f"{split}_runs", []))
            # Final validation paths may be written before session.json is updated.
            runs.extend(local(path.parent) for path in sorted((folder / "runs" / split).glob("*/manifest.json")))
        normal = folder / "smoke/normal"
        if (normal / "manifest.json").is_file():
            runs.append(local(normal))
    # Preserve order while avoiding repeated asset copies.
    runs = list(dict.fromkeys(runs))
    profile = local(reference.get("profile_path"), default="reference/measured-profile.json")
    if not profile.is_file():
        profile = None
    comparison = local(final.get("comparison_path"), default="final/comparison.json")
    comparison_data = _read(comparison) if comparison.is_file() else {}
    output = session / "report"
    page = generate_report(runs, output, profile_path=profile,
                           comparison_paths=[comparison] if comparison.is_file() else [])
    assets = output / "session-assets"
    assets.mkdir(parents=True, exist_ok=True)

    def copy_link(source, name, label):
        source = local(source)
        if not source.is_file():
            return f'<span>Missing: {_escape(label)}</span>'
        destination = assets / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return f'<a href="session-assets/{_escape(name)}" download>{_escape(label)}</a>'

    state_link = copy_link(session / "session.json", "session.json", "Session snapshot")
    numerical = comparison_data.get("numerical_status", final.get("numerical_status", "pending"))
    manual = comparison_data.get("manual_validation_status", final.get("measurement_status", "pending"))
    smoke = final.get("smoke_status", "pending")
    wall = verify_wall_review(session)
    recorded_status = comparison_data.get("status", "pending")
    selected = state.get("selected_candidate_id") or "not selected"
    visual_id = visual_candidate.get("candidate_id") if visual_candidate else "none available"
    trace = []
    for index, candidate in enumerate(candidates):
        cid, folder = candidate.get("candidate_id"), candidate["_folder"]
        prefix = f"candidate-{index:03d}"
        links = [copy_link(candidate["_snapshot"], f"{prefix}/candidate.json", "Candidate snapshot")]
        diff = local(candidate.get("diff_path"), default=folder / "patch.diff")
        links.append(copy_link(diff, f"{prefix}/patch.diff", "Source diff"))
        diff_text = diff.read_text(encoding="utf-8-sig") if diff.is_file() else "Diff is not available."
        facts = {key: candidate.get(key) for key in
                 ("candidate_id", "status", "source_hashes", "feedback", "smoke", "error")}
        trace.append(f'<details class="artifact"><summary>Candidate {_escape(cid)} · '
                     f'{_escape(candidate.get("status", "pending"))}</summary>'
                     f'<p>{" · ".join(links)}</p><pre>{_json(facts)}</pre>'
                     f'<details><summary>Recorded source changes</summary><pre>{_escape(diff_text)}</pre></details></details>')
    job_links = []
    for index, job in enumerate(sorted((session / "jobs").glob("*.json"))):
        job_links.append(copy_link(job, f"job-{index:03d}.json", job.name))
    # Optional Kiro execution records are links to the recorded trace, not proof
    # of tool use by themselves; the trace and candidate diff remain inspectable.
    execution_links = []
    for relative in ("kiro/launch.json", "kiro/model-inventory.json", "kiro/stdout.jsonl",
                     "kiro/stderr.log", "kiro/tool-audit.jsonl", "kiro/authorship.json",
                     "ide/evidence.json", "ide/session.zip", "ide/finish-receipt.json",
                     "ide/environment-policy.json", "review/operator-contribution.json",
                     "kiro/launch-start.json"):
        path = local(relative)
        if path.is_file():
            execution_links.append(copy_link(path, relative, relative))
    launch_path = local("kiro/launch.json")
    if launch_path.is_file():
        launch = _read(launch_path)
        for stage in ("ui_observed", "ui_observed_after"):
            for index, item in enumerate((launch.get(stage) or {}).get("evidence", [])):
                screenshot = local(item["path"])
                execution_links.append(copy_link(screenshot, f"ide/{stage}-{index:02d}{screenshot.suffix}",
                                                 f"IDE {stage} screenshot {index + 1}"))
    if final.get("manual_review_path"):
        execution_links.append(copy_link(final["manual_review_path"], "manual-review.json",
                                         "Independent visual review receipt"))
    if (session / "final/gameplay-review.json").is_file():
        execution_links.append(copy_link(session / "final/gameplay-review.json", "gameplay-review.json",
                                         "Supplemental wall-jump review receipt"))
        if visual_candidate is not None and selected_id:
            wall_video = visual_candidate["_folder"] / "supplemental/wall/video.mp4"
            if wall_video.is_file():
                execution_links.append(copy_link(wall_video, "wall-jump.mp4", "Supplemental wall-jump video"))
    facts = {key: state.get(key) for key in
             ("session_id", "mode", "status", "created_at", "started_at", "deadline_at", "baseline_hashes", "budget")}
    block = ('<section class="result-card pending" aria-label="Session evaluation">'
             f'<h2>KiroClops session: {_escape(state.get("session_id", session.name))}</h2>'
             f'<p>Selected candidate: {_escape(selected)} · Recorded comparison: {_escape(recorded_status)}</p>'
             f'<p>Video/curve candidate: {_escape(visual_id)} ({_escape(visual_basis)}). '
             'All attempts remain in the metrics and source-diff history below.</p>'
             f'<p><strong>Numerical gates: {_escape(numerical)}</strong> · '
             f'<strong>Independent visual validation: {_escape(manual)}</strong> · '
             f'<strong>Movement smoke checks: {_escape(smoke)}</strong></p>'
             f'<p><strong>Wall-jump behavior: {_escape(wall["status"])}</strong> · '
             f'{_escape(wall.get("reason", ""))}</p>'
             '<p>The original wall smoke sequence could accept a jump after landing. '
             'Its recorded pass alone does not verify a wall launch; the supplemental '
             'review checks a direct WallJump → Jump transition while airborne.</p>'
             '<p>Numerical agreement alone is not a validated result. Candidate visual controls are checked '
             'separately from baseline evidence. Pending checks remain pending.</p>'
             f'<p>{state_link}</p><details><summary>Session provenance and budget</summary><pre>{_json(facts)}</pre></details>'
             f'<h3>Candidate attempts and source changes</h3>{"".join(trace) or "<p>No candidate has been recorded.</p>"}'
             f'<h3>Job and execution traces</h3><p>{" · ".join(job_links + execution_links) or "No execution trace has been recorded."}</p>'
             f'<ul>{"".join(f"<li>{_escape(issue)}</li>" for issue in issues)}</ul></section>')
    content = page.read_text(encoding="utf-8")
    content = content.replace("KiroClops / Stage 1", "KiroClops / Stage 2")
    if "<body><main>" not in content:
        raise ValueError("legacy report template has changed; session summary was not inserted")
    page.write_text(content.replace("<body><main>", "<body><main>" + block, 1), encoding="utf-8")
    return page
