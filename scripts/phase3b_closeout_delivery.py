"""Build a local, source-bound closeout without launching AWS or Kiro.

Historical runs, failed reviews and original reports stay unchanged. This is
an engineering evidence index, not a public submission or a new acceptance run.
"""
from __future__ import annotations

import hashlib
import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "artifacts/phase3b"
OUT = BASE / "closeout-20261001"


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    if OUT.exists():
        raise SystemExit("Closeout already exists; preserve it and use a new version.")
    previous = read(BASE / "kiro-integration-audit-01/result.json")
    panel = read(BASE / "panel-kiro-recovery-02/evidence-posthoc-audit/result.json")
    tests = read(BASE / "startup-transport-proof-01/tests-03/result.json")
    assert tests["status"] == "pass" and tests["tests_run"] == 160
    for path, expected in tests["sources"].items():
        assert sha(ROOT / path) == expected, f"Test source changed: {path}"
    old_sources = read(BASE / "regression-05/start.json")
    changed = []
    for path, expected in old_sources["source_hashes"].items():
        current = sha(ROOT / path)
        if current != expected:
            changed.append({"path": path, "historical_sha256": expected, "current_sha256": current})
    assert {row["path"] for row in changed} == {
        "kiroclops/local_engine/launcher.py", "kiroclops/local_engine/review.py"
    }
    frozen = {}
    for key in ("legacy_hashes", "heart_v2_hashes"):
        values = old_sources[key]
        assert all(sha(ROOT / path) == expected for path, expected in values.items())
        frozen[key] = {"files": len(values), "all_match": True}
    OUT.mkdir()
    (OUT / "media").mkdir()
    rows = []
    paths = {"game": BASE / "kiro-integration-01/game/session",
             "svg": BASE / "kiro-integration-01/svg/session",
             "panel": BASE / "panel-kiro-recovery-02/session"}
    for domain in ("game", "svg", "panel"):
        evidence = panel if domain == "panel" else next(x for x in previous["rows"] if x["domain"] == domain)
        session = paths[domain]
        state = read(session / "session.json")
        report = read(session / "report/report.json")
        assert evidence["launcher_status"] == "completed"
        assert evidence["native_audit_status"] == "pass"
        assert evidence["strict_1800_second_stage_pass"] is True
        group = next(g for g in report["groups"] if g["id"].split("/")[0] == state["final_job_id"])
        if domain == "panel":
            comparison = evidence["comparison"][0]
        else:
            final_job = next(j for j in evidence["jobs"] if j["operation"] == "finalize")
            comparison = final_job["result"]["comparisons"][0]
        if domain == "game":
            phase = comparison["phases"][0]
            before, after, improvement = phase["before_rmse_body"], phase["after_rmse_body"], phase["improvement_fraction"]
        else:
            before, after, improvement = comparison["before_score"], comparison["score"], comparison["improvement_fraction"]
        media = {}
        for role in ("reference", "before", "final"):
            item = group["roles"][role]
            source = session / "report" / item["video"]
            manifest = session / "report" / item["package"]
            assert read(manifest)["package_sha256"] == item["package_sha256"]
            target = OUT / "media" / f"{domain}-{role}.mp4"
            shutil.copyfile(source, target)
            assert sha(source) == sha(target)
            media[role] = {"video": target.relative_to(OUT).as_posix(), "video_sha256": sha(target),
                           "source_video": rel(source), "source_manifest": rel(manifest),
                           "manifest_file_sha256": sha(manifest), "package_sha256": item["package_sha256"],
                           "input_origin_s": item["input_origin_s"], "frame_count": len(item["frames"])}
        shutil.copyfile(session / "report/candidate.diff", OUT / f"{domain}-candidate.diff")
        if domain == "game":
            review = read(BASE / "visual-closeout-02/game-limited/result.json")
            visual_status, source_status = "limited", evidence["native_audit_status"]
            review_path = BASE / "visual-closeout-02/game-limited/visual-inspection.json"
            detail = "Positions agree; 10/16/13/13 distinct images per recording, below the required count of 20. No full event/relationship acceptance."
        else:
            review_path = BASE / f"visual-closeout-02/{domain}/check-result.json"
            review = read(review_path)
            assert review["candidate_sha256"] == report["candidate_sha256"]
            visual_status, source_status = review["visual_status"], review["source_native_status"]
            detail = ("All 9 visual scopes, the source and the native recording audit passed." if domain == "svg" else
                      "768 position points passed. The event/relationship scopes of the final candidate passed; there are relationship mismatches in the evaluation record of the reference, baseline and selected candidate. The event-count uncertainty is in the baseline recording. The overall visual acceptance did not pass.")
        rows.append({"domain": domain, "session": rel(session), "model": "claude-opus-5.5", "effort": "max",
                     "candidate_count": evidence["candidate_count"], "selected_candidate_id": state["selected_candidate_id"],
                     "candidate_sha256": report["candidate_sha256"], "final_job_id": state["final_job_id"],
                     "numerical_status": "pass", "before_curve_error": before, "final_curve_error": after,
                     "improvement_fraction": improvement, "start_to_validation_seconds": evidence["start_to_validation_seconds"],
                     "analysis_client_wall_seconds": evidence["analysis_client_wall_seconds"],
                     "analysis_fraction_of_stage": evidence["analysis_fraction_of_stage"], "native_audit_status": "pass",
                     "source_review_status": source_status, "visual_review_status": visual_status,
                     "visual_review_detail": detail, "visual_review_path": rel(review_path), "visual_review_sha256": sha(review_path),
                     "heldout_status": "not_provided", "acceptance": "partial", "media": media,
                     "original_report": rel(session / "report/index.html"),
                     "original_report_json_sha256": sha(session / "report/report.json")})
    result = {"kind": "Phase3BScopeCloseout", "schema_version": 1,
              "created_at": datetime.now(timezone.utc).isoformat(), "status": "partial_with_explicit_limits",
              "scope": {"new_external_users": "excluded_from_hackathon_by_user", "hosted_cool_license": "deferred_to_external_user_stage",
                        "vendor_contact_sent": False, "public_beta_deployed": False},
              "registered_set": {"runs": 3, "completed": 2, "startup_failed": 1, "preserved": True},
              "separate_panel_recovery": {"completed": True, "replaces_registered_run": False,
                                         "capture_timing_failures_before_success": 3, "operator_patch_or_followup": False},
              "integrations": rows,
              "cool": {"feasibility": "closed_with_unmatched_base_limit", "equivalent_packages": 12,
                       "pairwise_equivalent_packages": 36, "causal_speedup_percent": None,
                       "closure": "artifacts/phase3b/cool-feasibility-closure-01/REPORT.md"},
              "cleanup": read(BASE / "panel-cloud-recovery-01/cleanup-verification-02.json"),
              "tests": {"current_targeted": {"tests": 160, "errors": 0, "failures": 0, "skipped": 0,
                                            "receipt": "artifacts/phase3b/startup-transport-proof-01/tests-03/result.json"},
                        "historical_full": {"tests": 2557, "errors": 0, "failures": 0, "skipped": 5, "rerun_this_turn": False},
                        "historical_service": {"tests": 41, "rerun_this_turn": False},
                        "intentional_changes_since_full_suite": changed, "frozen_sources": frozen},
              "review_method": "Codex assistant inspected raw-pixel review sheets and separate raw geometry aids; no external human review.",
              "limits": ["No unseen-input validation supplied to these three integrations.",
                         "Panel required visual review matrix failed despite the final recording passing.",
                         "Game full 20 distinct-image gate not met.",
                         "One completed panel recovery does not prove general MCP/capture reliability.",
                         "Full Phase3A and general improvement over normal Kiro remain unproven."],
              "producer_sha256": sha(Path(__file__))}
    write_json(OUT / "result.json", result)
    def time_text(seconds):
        return f"{int(seconds)//60}:{int(seconds)%60:02d}"
    table = "\n".join(f"| {x['domain']} | {100*x['improvement_fraction']:.2f}% | {time_text(x['start_to_validation_seconds'])} | {100*x['analysis_fraction_of_stage']:.2f}% | {x['candidate_count']} | {x['visual_review_status']} |" for x in rows)
    text = f"""# KiroClops — October 1 internal engineering closeout

**The Kiro + AWS analysis flow was demonstrated in three domains; full behavior acceptance is partial.** The panel connection was opened in a separate clean run and Kiro wrote the real code itself. The first three registered attempts are preserved as **2 completed / 1 MCP startup failure**; the new panel result does not replace the old failure.

[Local report with videos](index.html) · [Source-bound result](result.json) · [File integrity](../DELIVERY_MANIFEST.json)

## The user's scope decision

- Three real new-user tests were removed from the hackathon scope; they are not counted as tests performed or as a beta accepted.
- The hosted COOL license discussion was deferred to the external-user stage. The draft was not sent.
- No new portal, similarity layer, broad comparison cohort or external beta was opened.

## Real CLI runs

Opus 5.5/max, native Always Allow; at most three candidates and 1,800 seconds per run. Source editing, recording and function checks were done locally; the measurement was done on the standard OpenCV AWS worker. Kiro was given no ready-made solution patch or live steering.

| Domain | Curve error reduction | Start → final validation | Remote analysis wait share | Candidates | Independent visual status |
| --- | ---: | ---: | ---: | ---: | --- |
{table}

These percentages belong to the measurement of the given clip; different units/scales are used. Since held-out inputs were not provided, they do not count as evidence of generalization or as a new comparison with normal Kiro.

## Panel connection and preserved failures

A direct MCP startup check and a bounded native reconnect were added. After the task/model has started, no automatic new chat or budget reset is performed. The new panel run produced two candidates and pinned candidate-01, which had the lower calibration error. The native recording audit passed 53 tool calls. The failure of the first attempt and the first auditor incompatibility are preserved; the new audit proves with exact source digests that the launcher change is limited to the transport layer.

In the same panel conversation, three preparation recordings returned `capture_timing_failure`; the fourth recording passed. The total duration includes these repeats. It is not claimed that the startup and recording root causes have been resolved in general.

## Independent visual review

The reviewer is the Codex assistant; the raw images were checked with separate pixel/edge/geometry aids. No external human approval or manual clicking of every single point is claimed. Position, event and relationship decisions were also compared using a measurement independent of the production trajectory.

- **SVG:** Nine scopes, the source and the native audit passed. This is the visual adequacy for the given clip; there is no held-out validation.
- **Panel:** 768 position points agree. The final recording passed the event/relationship checks. There are relationship mismatches in the evaluation record of the selected candidate and in the reference and baseline recordings; there is event-count uncertainty in the **baseline recording**. Because not all required recordings pass together, the overall visual status is **fail**. An earlier incorrect reading of the role-order guess was corrected with [a separate correction receipt](../visual-closeout-02/panel/role-map-correction.json); the observations/verdict were not changed.
- **Game:** The raw positions of the four recordings agree. The distinct-image counts are 10/16/13/13; the required 20 was not met. Full visual event/relationship acceptance was not granted. The first minimum frame in a flat peak interval was not counted as a single exact peak time.

[SVG audit](../visual-closeout-02/svg/check-result.json) · [Panel audit](../visual-closeout-02/panel/check-result.json) · [Limited game review](../visual-closeout-02/game-limited/visual-inspection.json)

## COOL and cost

With the current sources, local/standard ARM/COOL are equivalent for **12/12 packages and 36/36 in three pairwise comparisons**. No reproducible standard source base for the COOL images could be found. The feasibility study was closed **without a speedup-percentage claim**; the absolute times were saved. No new COOL attempt or license correspondence was made.

In the October 1 AWS usage/billing audit, the September 29 COOL software was recorded as **4 hours / 0.02 USD**, without credit. This is a Cost Explorer usage record; it is not the final bill/tax amount. The conservative gross estimate for EC2+IPv4 across all of Phase3B is **0.570 USD / 3.465 worker-hours**; it is not a bill and does not include EBS/network/API/tax or the effect of credits. The five-USD reserve for other expenses is not an amount spent. Credit coverage has been verified in past offsets; it is not guaranteed that every item will be offset in the future.

The standard ARM stack, EC2, S3 and EBS of the panel were cleaned up; local service credentials were removed. No external beta was left open. [Final cleanup](../panel-cloud-recovery-01/cleanup-verification-02.json) · [COOL feasibility decision](../cool-feasibility-closure-01/REPORT.md)

## Software verification and open limits

**160 relevant tests passed** for the new startup/native/cloud routing. Historical full suite: 2,557 tests, 0 errors, 5 skipped; service suite: 41 tests. These were not fully re-run in this closeout. After the full suite, only the launcher and the historical audit reader changed among the old 206 production sources; a new startup module was added. All of the frozen **81 old / 25 Heart v2** sources are still identical.

The full 3A acceptance, the game chain and general KiroClops superiority remain open. The measurement/relationship difference of the panel and the image coverage of the game are kept as failed/limited in the closeout report. The user test taken out of scope and the deferred license discussion were not converted into technical success.
"""
    (OUT / "REPORT.md").write_text(text, encoding="utf-8")
    labels = {"game": "Game", "svg": "SVG diagram", "panel": "Web panel"}
    sections = []
    for row in rows:
        videos = []
        for role, label in (("reference", "Reference"), ("before", "Before"), ("final", "After Kiro")):
            item = row["media"][role]
            videos.append(f'<figure><figcaption>{label}</figcaption><video preload="metadata" controls muted playsinline data-origin="{item["input_origin_s"]}" src="{html.escape(item["video"])}"></video></figure>')
        original_link = "../" + row["original_report"].removeprefix("artifacts/phase3b/")
        sections.append(f'''<section><header><h2>{labels[row['domain']]}</h2><span>Curve error −{100*row['improvement_fraction']:.2f}%</span></header>
<p>{html.escape(row['visual_review_detail'])}</p><div class="videos">{''.join(videos)}</div>
<div class="controls"><button data-action="play">Align to input and play</button><button data-action="pause">Pause</button><button data-action="reset">Reset to start</button></div>
<p class="meta">Opus 5.5 / max · {row['candidate_count']} candidates · {time_text(row['start_to_validation_seconds'])} · Remote analysis share {100*row['analysis_fraction_of_stage']:.2f}% · Held-out validation: none</p>
<p><a href="{html.escape(original_link)}">Detailed original report and curves</a> · <a href="{row['domain']}-candidate.diff">Kiro code diff</a></p></section>''')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>KiroClops — October 1 engineering closeout</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#111827;color:#e5e7eb;font:16px/1.55 system-ui,sans-serif}main{max-width:1380px;margin:auto;padding:28px}h1,h2{line-height:1.2}h1{font-size:clamp(26px,4vw,42px)}a{color:#93c5fd}section,.notice{border:1px solid #374151;border-radius:12px;background:#182235;padding:22px;margin:22px 0}header{display:flex;align-items:center;justify-content:space-between;gap:15px;flex-wrap:wrap}header span{color:#86efac;font-weight:650}.videos{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}figure{margin:0}figcaption{margin:0 0 8px;font-weight:600}video{width:100%;aspect-ratio:3/2;background:#050912;object-fit:contain;border:1px solid #475569;border-radius:6px}.controls{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}button{padding:10px 14px;border:1px solid #64748b;background:#27364e;color:white;border-radius:6px;cursor:pointer}.meta{font-size:14px;color:#cbd5e1}.notice strong{color:#fcd34d}@media(max-width:760px){main{padding:16px}.videos{grid-template-columns:1fr}section{padding:16px}}
</style><main><p class="meta">October 1, 2026 · Local internal engineering evidence</p><h1>Kiro measured the footage,<br>and changed the real code.</h1>
<p>The KiroClops + AWS analysis flow was demonstrated in three domains. The earlier registered set is preserved as 2 completed / 1 startup failure; the panel was completed in a separate clean run.</p>
<div class="notice"><strong>Full acceptance is partial.</strong> There is a numerical improvement on the given clips; there are no held-out inputs in these three flows. The SVG visual review passed. The final recording of the panel passed, but its overall review did not. The image coverage of the game is limited. The three real-user tests are outside the hackathon, and the COOL license discussion is in a later stage.</div>
''' + "".join(sections) + '''<section><h2>COOL, cost and sources</h2><p>12/12 accuracy equivalence. Because no matched standard source base could be found, there is no COOL speedup-percentage claim.</p><p>In the COOL AWS usage record: <b>0.02 USD</b>; not the final bill/tax. Gross upper estimate for EC2+IPv4 across all of Phase3B: <b>0.570 USD</b>; not a bill, excluding other expenses. Temporary AWS resources and local tokens were cleaned up.</p><p>160 relevant tests passed; the historical 2,557 tests and 41 service tests are a separate record. Full 3A acceptance is open.</p><p><a href="REPORT.md">Scope and findings</a> · <a href="result.json">Source-bound JSON</a> · <a href="../DELIVERY_MANIFEST.json">Evidence file digests</a></p></section>
<p class="meta">Playback is for presentation; the numerical measurement by frame time is in the original report/data. This page does not start any network, AWS or Kiro connection.</p></main>
<script>document.querySelectorAll('section').forEach(section=>{const videos=[...section.querySelectorAll('video')];section.querySelectorAll('button').forEach(button=>button.addEventListener('click',async()=>{const action=button.dataset.action;if(action==='pause'){videos.forEach(v=>v.pause());return;}videos.forEach(v=>{v.pause();v.currentTime=action==='reset'?0:Number(v.dataset.origin);});if(action==='play'){const results=await Promise.allSettled(videos.map(v=>v.play()));if(results.some(r=>r.status==='rejected'))button.textContent='Open the videos with their separate playback controls';}}));});</script></html>'''
    (OUT / "index.html").write_text(page, encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": rel(OUT), "videos": 9,
                      "source_changes": [x["path"] for x in changed], "frozen": frozen}, ensure_ascii=False))


if __name__ == "__main__":
    main()
