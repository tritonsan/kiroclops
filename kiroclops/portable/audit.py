"""Read-only replay plus explicit, immutable post-run independent acceptance.

No source or manual verdict is inferred from numerical success. Native transport
logs are evidence records, not cryptographic attestation of external UI activity.
"""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from kiroclops.integration.storage import IntegrationError, project_hashes, read_json, sha256, utc_now
from kiroclops.integration import kiro as native
from . import launcher, measurement, review, runtime, integrity
from .session import Session, digest, editable, ordinary

VERSION = "portable-independent-audit-v2"
CHECKS = {
    "jump": {"normal_jump_scope", "ground_horizontal_preserved", "airborne_horizontal_preserved",
             "double_jump_preserved", "wall_jump_preserved", "coyote_rights_preserved", "hazard_return_preserved"},
    "ground": {"ground_only", "jump_preserved", "airborne_horizontal_preserved", "double_jump_preserved",
               "wall_jump_preserved", "coyote_rights_preserved", "hazard_return_preserved"},
}


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _jsonl(path):
    path = ordinary(path)
    _need(path.stat().st_size <= 32*1024*1024, "Native log exceeds its registered limit")
    values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    _need(all(isinstance(v, dict) for v in values), "Native log is not an object stream")
    return values


def _time(value):
    time = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _need(time.tzinfo is not None, "Evidence time must include its timezone")
    return time


def verify_native(directory, launch, config, inbound, outbound):
    """Replay every generation selection and exact single prepared ACP prompt."""
    guard = native.StreamConfigurationGuard(config["agent_name"], model="gpt-5.6-sol", effort="max")
    observed_autopilot = None
    for event in inbound:
        for obj in native._stream_objects(event):
            options = obj.get("configOptions", [])
            for option in options if isinstance(options, list) else []:
                if isinstance(option, dict) and option.get("id") == "autopilot":
                    observed_autopilot = option.get("currentValue")
        guard.observe(event)
        if guard.generation_started:
            _need(observed_autopilot == "on", "Native Autopilot was not on during generation")
    summary = guard.summary()
    _need(summary.get("generation_configuration_verified") and summary == launch.get("stream_configuration"),
          "Recorded native model/effort generation configuration differs")
    _need(launch.get("status") == "completed" and launch.get("prompt_sent") is True
          and launch.get("model_requested") == "gpt-5.6-sol" and launch.get("effort_requested") == "max"
          and launch.get("surface") == "cli" and launch.get("transport") == "acp"
          and launch.get("process_returncode_after_cleanup") is not None
          and launch.get("model_launcher") == config["model_launcher"], "Native CLI launch is not a completed exact Sol/max run")
    argv = launch.get("argv")
    _need(isinstance(argv, list) and bool(argv) and isinstance(argv[0], str) and bool(argv[0])
          and argv == launcher.acp_argv(argv[0])
          and launch.get("approval_mode") == config.get("approval_mode") == launcher.APPROVAL_MODE
          and launch.get("acp_preflight", {}).get("argv") == argv
          and launch["acp_preflight"].get("approval_mode") == launcher.APPROVAL_MODE
          and launch["acp_preflight"].get("autopilot") == "on",
          "Native session-consent transport and preflight approval binding differ")
    _need(observed_autopilot == "on", "Native Autopilot was not observed on")
    started, ended = _time(launch["started_at"]), _time(launch["finished_at"])
    _need(0 <= (ended-started).total_seconds() <= 1800 and 0 <= launch.get("elapsed_seconds", 1801) <= 1800,
          "Native stage exceeded the fixed thirty-minute budget")
    _need(bool(outbound) and len(outbound) % 2 == 0, "Every ACP attempt needs its adjacent sent receipt")
    messages, previous = [], started
    for i in range(0, len(outbound), 2):
        attempt, sent = outbound[i:i+2]
        _need(attempt.get("phase") == "attempt" and sent.get("phase") == "sent"
              and attempt.get("sequence") == sent.get("sequence") == i//2+1
              and attempt.get("payload_sha256") == sent.get("payload_sha256") == _canonical(attempt.get("message"))
              and "message" not in sent, "ACP write-ahead attempt or send receipt differs")
        a, b = _time(attempt["at"]), _time(sent["at"])
        _need(previous <= a <= b <= ended, "ACP send chronology differs")
        previous = b
        messages.append(attempt["message"])
    creates = [m for m in messages if m.get("method") == "session/new"]
    prompts = [m for m in messages if m.get("method") == "session/prompt"]
    _need(len(creates) == len(prompts) == 1, "Expected exactly one fresh session and one unchanged task")
    prompt_path = ordinary(config["prompt_path"])
    prompt = prompt_path.read_text(encoding="utf-8")
    _need(launch.get("prompt_sha256") == hashlib.sha256(prompt.encode()).hexdigest(), "Prepared task digest differs")
    sid = launch.get("acp_preflight", {}).get("session_id")
    _need(bool(sid) and launch["acp_preflight"].get("workspace_verified") == str(Path(directory)/"target")
          and ordinary(creates[0]["params"]["cwd"]) == Path(directory)/"target"
          and prompts[0].get("params") == {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
          "Actual task/session identity differs from its prepared workspace")
    responses = lambda m: [r for r in inbound if "method" not in r and "id" in r and
                           type(r["id"]) is type(m.get("id")) and r["id"] == m.get("id")]
    new = responses(creates[0])
    final = responses(prompts[0])
    _need(len(new) == len(final) == 1 and new[0].get("result", {}).get("sessionId") == sid
          and final[0].get("result", {}).get("stopReason") == "end_turn"
          and final[0]["result"] == launch.get("prompt_result"), "Native creation or task completion is missing/ambiguous")
    after_prompt, requests = False, set()
    controls = {"initialize", "session/new", "session/set_mode", "session/set_config_option", "_kiro/hooks/list"}
    for message in messages:
        _need(message.get("jsonrpc") == "2.0", "Invalid ACP message")
        method = message.get("method")
        if method is None:
            origins = [r for r in inbound if "method" in r and "id" in r and r["id"] == message.get("id")]
            _need(len(origins) == 1 and ("result" in message) != ("error" in message), "Unbound client permission/response message")
            if origins[0]["method"] == "session/request_permission":
                options = origins[0].get("params", {}).get("options", [])
                preferred = next((o for kind in ("allow_always", "allow_once") for o in options
                                  if isinstance(o, dict) and o.get("kind") == kind and isinstance(o.get("optionId"), str)), None)
                response = message.get("result", {})
                outcome = response.get("outcome", {})
                if outcome.get("outcome") == "selected":
                    _need(preferred is not None and outcome.get("optionId") == preferred["optionId"],
                          "Native consent response did not use the available Always Allow option")
                    expected_meta = {"kiro":{"consent":{"scope":"session"}}} if preferred["kind"] == "allow_always" else None
                    _need(response.get("_meta") == expected_meta, "Native permission consent exceeded its recorded session scope")
                else:
                    _need(outcome == {"outcome":"cancelled"}, "Malformed native permission outcome")
            continue
        key = (type(message.get("id")).__name__, str(message.get("id")))
        _need("id" in message and key not in requests, "Duplicate native request identity")
        requests.add(key)
        if method == "session/prompt":
            after_prompt = True
        else:
            _need(method in controls and not after_prompt, "Unexpected request or post-task coaching/configuration")
    return {"status": "pass", "native_session_id": sid, "prompt_count": 1,
            "model": "gpt-5.6-sol", "effort": "max", "generation": summary,
            "scope": "Recorded native ACP only; no assertion about an unobserved external UI transport"}


def _relative(target, value):
    _need(isinstance(value, str) and not any(c in value for c in ("$", "%", "://")) and not value.startswith("~"), "Native file path is not literal")
    path = Path(value)
    path = ordinary(path if path.is_absolute() else target/path)
    _need(path.is_relative_to(target), "Native operation accessed outside this target workspace")
    return path.relative_to(target).as_posix()


def _output_object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    if "job_id" in value:
        return value
    for key in ("response", "message", "result", "structuredContent", "structured_content"):
        found = _output_object(value.get(key))
        if found is not None:
            return found
    return None


def verify_authorship(directory, state, candidate, inbound, hooks):
    """Replay native full writes/replacements from baseline bytes to evaluation snapshots."""
    directory, target = Path(directory), Path(directory)/"target"
    baseline = Path(state["baseline"])
    contents = {p: (baseline/p).read_bytes() for p in state["baseline_hashes"] if editable(p)}
    hashes = dict(state["baseline_hashes"])
    calls, matched, writes, hooks_used, cloud_ids = {}, {}, [], set(), set()
    native_sid = read_json(directory/"kiro/launch.json")["acp_preflight"]["session_id"]
    permissions = {}
    for row in inbound:
        if row.get("method") == "session/request_permission":
            params = row.get("params", {})
            permissions[params.get("toolCall", {}).get("toolCallId")] = params.get("_meta", {}).get("kiro", {}).get("toolId")
    mutation_names = {"fs_write", "fsWrite", "write_file", "str_replace", "strReplace", "replace_text"}
    allowed_hooks = [(i, h) for i,h in enumerate(hooks) if h.get("allow") is True and h.get("tool", "").removeprefix("@builtin/") in mutation_names]
    for h in hooks:
        if h.get("allow") is True:
            for path in h.get("paths", []):
                rel = _relative(target, path)
                _need(not rel.startswith((".kiroclops-private/", ".kiroclops-evidence/validation/", ".kiroclops-evidence/heldout/")),
                      "An allowed native hook accessed private held-out evidence")
    for line, row in enumerate(inbound, 1):
        if row.get("method") != "session/update":
            continue
        params = row.get("params", {})
        update = params.get("update", {})
        if update.get("sessionUpdate") not in {"tool_call", "tool_call_update"}:
            continue
        tid, meta = update.get("toolCallId"), update.get("_meta", {}).get("kiro", {})
        if meta.get("toolId") == "fetch_cloud_config":
            cloud_ids.add(tid)
        if tid in cloud_ids:
            continue
        _need(params.get("sessionId") == native_sid and isinstance(tid, str), "Tool trace belongs to another native session")
        if update["sessionUpdate"] == "tool_call":
            _need(tid not in calls and isinstance(update.get("rawInput"), dict), "Repeated or unstructured native call")
            args = {k:v for k,v in update["rawInput"].items() if k != "_meta"}
            is_edit = update.get("kind") in {"edit", "delete", "move"} or meta.get("toolId") in mutation_names
            if is_edit:
                _need(meta.get("toolOrigin") in {"default", "acp"} and not meta.get("serverName"), "Mutation is not a native file tool")
                rel = _relative(target, args.get("path", args.get("file")))
                _need(editable(rel), "Native write changed a protected Heart source")
            elif update.get("kind") == "read" and not meta.get("serverName"):
                for path in native._path_values(args):
                    _relative(target, path)
            elif update.get("kind") == "execute":
                raise ValueError("Shell execution is outside this cohort's native tool contract")
            # The observed MCP server plus its exact native tool title/permission identity
            # identifies evaluation; source identity is then bound to the returned job ID.
            is_evaluate = meta.get("serverName") == "kiroclops" and (
                update.get("title") == "@kiroclops/evaluate_candidate" or "evaluate_candidate" in str(meta.get("toolId") or permissions.get(tid) or ""))
            calls[tid] = {"args": args, "edit": is_edit, "evaluate": is_evaluate,
                          "snapshot": dict(hashes) if is_evaluate else None, "line": line}
        elif update.get("status") in {"completed", "failed", "cancelled"}:
            _need(tid in calls and not calls[tid].get("complete"), "Orphan or repeated native completion")
            call = calls[tid]
            call["complete"] = True
            if update.get("status") != "completed":
                continue
            args = call["args"]
            completed_args = {k:v for k,v in update.get("rawInput", args).items() if k != "_meta"}
            _need(all(completed_args.get(k) == v for k,v in args.items())
                  and set(completed_args)-set(args) <= {"replace_all"}
                  and completed_args.get("replace_all", False) is False, "Completed native input changed")
            if call["edit"]:
                rel = _relative(target, args.get("path", args.get("file")))
                current = contents.get(rel, b"")
                if "oldStr" in args:
                    old, new = args["oldStr"], args.get("newStr")
                    _need(isinstance(old, str) and bool(old) and isinstance(new, str), "Replacement lacks explicit old/new text")
                    text = current.decode("utf-8")
                    pattern = re.compile(r"\r?\n".join(re.escape(s) for s in old.replace("\r\n", "\n").split("\n")))
                    matches = list(pattern.finditer(text))
                    _need(len(matches) == 1, "Ambiguous native replacement cannot establish authorship")
                    m = matches[0]
                    modified = (text[:m.start()]+new+text[m.end():]).encode()
                else:
                    text = args.get("text", args.get("content"))
                    _need(isinstance(text, str), "Unsupported native mutation; full write text is required")
                    modified = text.encode()
                preview = meta.get("preview", {})
                _need(isinstance(preview.get("originalContent"), str) and isinstance(preview.get("modifiedContent"), str)
                      and preview["originalContent"].encode() == current and preview["modifiedContent"].encode() == modified,
                      "Completed native full-file preview differs from replay bytes")
                hook = next(((i,h) for i,h in allowed_hooks if i not in hooks_used), None)
                _need(hook is not None and len(hook[1].get("paths", [])) == 1
                      and _relative(target, hook[1]["paths"][0]) == rel, "Native write lacks its ordered allowed hook")
                hooks_used.add(hook[0])
                if tid in permissions:
                    extra = next(((i,h) for i,h in allowed_hooks if i not in hooks_used), None)
                    _need(extra is not None and extra[1].get("tool") == permissions[tid]
                          and len(extra[1].get("paths", [])) == 1 and _relative(target,extra[1]["paths"][0]) == rel,
                          "Native write permission lacks its second bound approval hook")
                    hooks_used.add(extra[0])
                contents[rel], hashes[rel] = modified, hashlib.sha256(modified).hexdigest()
                writes.append({"tool_call_id": tid, "path": rel, "stdout_line": line, "sha256": hashes[rel]})
            if call["evaluate"]:
                output = _output_object(update.get("rawOutput"))
                if not output:
                    continue  # Rejected invocation without a reserved attempt.
                jid, cid = output.get("job_id"), output.get("candidate_id")
                job = read_json(directory/"jobs"/(jid+".json"))
                _need(job.get("action") == "evaluate" and job.get("session_id") == state["session_id"] and job.get("candidate_id") == cid,
                      "Native evaluation output belongs to a different stage/job")
                checkpoint = next((c for c in state["candidates"] if c["candidate_id"] == cid), None)
                _need(checkpoint is not None and cid not in matched, "Unknown or duplicated candidate evaluation")
                if checkpoint.get("source_hashes"):
                    _need(call["snapshot"] == checkpoint["source_hashes"]
                          and job.get("candidate_source_sha256") == digest(call["snapshot"]),
                          "Candidate is not the source authored before its native evaluate call")
                matched[cid] = {"job_id": jid, "tool_call_id": tid, "source_sha256": digest(call["snapshot"])}
    _need(bool(writes) and candidate["candidate_id"] in matched, "Selected candidate lacks native write/evaluation evidence")
    _need(len(hooks_used) == len(allowed_hooks), "Allowed mutation hooks remain unexplained by native writes")
    _need(set(matched) == {c["candidate_id"] for c in state["candidates"]}, "Candidate attempt history differs from native calls")
    _need(all(c.get("complete") for c in calls.values()), "Native tool call has no terminal completion")
    return {"status": "pass", "writes": writes, "evaluation_bindings": matched,
            "scope": "Exact baseline-to-candidate native write replay, no operator patch inferred"}


def source_review_template(directory):
    session = Session(directory)
    state = session.verify()
    candidate = session.verify_candidate(state["selected_candidate_id"])
    files = {rel:h for rel,h in candidate["source_hashes"].items()
             if rel.endswith(".gd") or rel in {"DefaultMovementData.tres", "player.tscn"}}
    return {"source": "human", "verdict": "pending", "reviewed_at": None,
            "candidate_id": candidate["candidate_id"], "source_sha256": candidate["source_sha256"],
            "trace_sha256": sha256(session.directory/"kiro/stdout.jsonl"), "inspected_files": files,
            "findings": [{"check": c, "verdict": "pending", "evidence": ""} for c in sorted(CHECKS[state["experiment_spec"]["stage"]])]}


def _compute(directory, observations_path, source_review_path):
    directory = ordinary(directory)
    session = Session(directory)
    state = session.verify()
    evidence = {}
    def bind(path):
        path = ordinary(path)
        evidence[str(path)] = sha256(path)
        return path
    def data(path):
        return read_json(bind(path))
    candidate = session.verify_candidate(state["selected_candidate_id"])
    cid, stage = candidate["candidate_id"], state["experiment_spec"]["stage"]
    comparison_path = directory/"final/comparison.json"
    comparison = data(comparison_path)
    launch = data(directory/"kiro/launch.json")
    config = launcher.load_model_config(directory)
    from .cohort import verify_launch_registration
    verify_launch_registration(directory)
    for path in (directory/"session.json", directory/"session-binding.json", directory/"execution-controls.json",
                 directory/"target/.kiro/kiroclops-session.json", directory/"candidates"/cid/"candidate.json"):
        bind(path)
    for rel in config["generated_hashes"]:
        bind(directory/"target/.kiro"/rel)
    inbound = _jsonl(bind(directory/"kiro/stdout.jsonl"))
    outbound = _jsonl(bind(directory/"kiro/outbound.jsonl"))
    hooks = _jsonl(bind(directory/"kiro/tool-audit.jsonl"))
    gates = {"native_model_and_autonomy": verify_native(directory, launch, config, inbound, outbound),
             "native_authorship": verify_authorship(directory, state, candidate, inbound, hooks)}
    _need(state["status"] == "finished" and not state.get("active_job_id") and state.get("sealed_at")
          and 1 <= len(state["candidates"]) <= 3 and comparison.get("candidate_id") == cid
          and comparison.get("source_sha256") == candidate["source_sha256"]
          and comparison.get("session_id") == state["session_id"], "Selected final candidate identity or stage budget differs")
    jobs = [data(p) for p in sorted((directory/"jobs").glob("job-*.json"))]
    eligible = [c for c in state["candidates"] if c.get("status") == "evaluated" and c.get("feedback",{}).get("eligible") is True
                and c["feedback"].get("automatic_integrity_status") == "pass"]
    _need(eligible and min(eligible,key=lambda c:(c["feedback"]["mean_rmse_body"],c["candidate_id"]))["candidate_id"] == cid,
          "Chosen candidate was not the lowest-error eligible calibration result")
    final_jobs = [j for j in jobs if j.get("action") == "finalize" and j.get("status") == "succeeded" and j.get("candidate_id") == cid
                  and j.get("candidate_source_sha256") == candidate["source_sha256"] and j.get("result") == state["final"]]
    _need(len(final_jobs) == 1 and final_jobs[0]["session_id"] == state["session_id"], "Final result lacks its immutable completed job binding")
    _need(comparison.get("numerical_status") == comparison.get("automatic_integrity_status") == "pass", "Final numerical or gameplay acceptance failed")
    gates["source_and_final_job"] = {"status": "pass", "candidate_id": cid, "source_sha256": candidate["source_sha256"]}
    for rel, h in candidate["source_hashes"].items():
        _need(sha256(bind(Path(candidate["project_path"])/rel)) == h, "Selected source bytes changed")
    source_review = data(source_review_path)
    expected_review = source_review_template(directory)
    findings = source_review.get("findings", [])
    _need(source_review.get("source") in {"human", "assistant_visual"} and source_review.get("verdict") == "pass"
          and source_review.get("candidate_id") == cid and source_review.get("source_sha256") == candidate["source_sha256"]
          and source_review.get("trace_sha256") == evidence[str(directory/"kiro/stdout.jsonl")]
          and source_review.get("inspected_files") == expected_review["inspected_files"]
          and _time(source_review["reviewed_at"]) >= _time(launch["finished_at"])
          and len(findings) == len(CHECKS[stage]) and {f.get("check") for f in findings} == CHECKS[stage]
          and all(f.get("verdict") == "pass" and isinstance(f.get("evidence"),str) and f["evidence"].strip() for f in findings),
          "Independent source reviewer did not attest each scoped check against this exact candidate and trace")
    gates["independent_source_review"] = {"status": "pass", "source": source_review["source"], "checks": sorted(CHECKS[stage])}
    manual = data(observations_path)
    _need(manual.get("candidate_id") == cid and manual.get("source_sha256") == candidate["source_sha256"], "Manual observations belong to another candidate")
    mechanics = {"jump"} if stage == "jump" else {"ground", "jump"}
    _need(set(comparison.get("runs",{})) == mechanics and set(manual.get("groups",{})) == mechanics, "Final/manual mechanic groups differ")
    runtime_count, manuals, numbers, visited_captures = 0, {}, {}, {}
    def capture(path, sources):
        nonlocal runtime_count
        path = ordinary(path)
        if str(path) in visited_captures:
            _need(visited_captures[str(path)] == sources, "A capture was reused with another source role")
            return
        manifest = data(path/"manifest.json")
        provenance = manifest["provenance"]
        _need(provenance["full_source_hashes_before"] == provenance["full_source_hashes_after"] == sources, "Capture belongs to another source")
        receipt_path = bind(provenance["runtime_receipt"])
        _need(evidence[str(receipt_path)] == provenance["runtime_receipt_sha256"], "Capture runtime receipt changed")
        verified = runtime.verify_runtime_receipt(receipt_path, expected_source_hashes=sources)
        _need(verified.get("status") == "pass", "Private runtime receipt failed replay")
        frames = runtime._frames(path)
        _need(runtime._digest(frames) == manifest["raw_frames_sha256"], "Raw frame set changed")
        for name in frames:
            bind(path/"frames"/name)
        recording = data(path/"recording.json")
        _need(evidence[str(path/"recording.json")] == provenance["recording_sha256"], "Recording metadata changed")
        runtime.validate_recording(recording, manifest["scenario"], manifest.get("normal_level", False))
        if manifest.get("normal_level"):
            gameplay = runtime.gameplay_checks(recording, manifest["scenario"])
            _need(gameplay == manifest.get("gameplay") and gameplay.get("status") == manifest.get("gameplay_status") == "pass",
                  "Normal-map behavior does not replay as successful")
        visited_captures[str(path)] = sources
        runtime_count += 1
    for mechanic in sorted(mechanics):
        groups = comparison["runs"][mechanic]
        _need(set(groups) == {"reference", "before", "after"} and set(manual["groups"][mechanic]) == set(groups), "Manual role groups differ")
        numeric = measurement.compare_motion(mechanic, groups["reference"], groups["before"], groups["after"], "validation")
        _need(numeric == comparison["comparisons"][mechanic] and numeric["pass"], "Final numerical matrix does not replay")
        numbers[mechanic] = {"status": "pass", "passed": numeric["passed"], "required": numeric["required"]}
        for role, values in groups.items():
            paths = [bind(p) for p in values]
            observations = manual["groups"][mechanic][role]
            _need(observations.get("analysis_files") == {str(p): evidence[str(p)] for p in paths}, "Manual analysis file linkage differs")
            observed = review.validate_observations(paths, observations, minimum=20, require_events=mechanic == "jump",
                          required_categories=("brake","reverse","flip") if mechanic == "ground" else ())
            _need(observed.get("status") == "pass", "Independent visual observations failed or are incomplete")
            manuals[mechanic+"/"+role] = observed
            for path in paths:
                analysis = data(path)
                run = ordinary(analysis["run_id"])
                manifest = data(run/"manifest.json")
                _need(analysis["provenance"]["manifest_sha256"] == evidence[str(run/"manifest.json")], "Analysis manifest linkage changed")
                if role != "reference":
                    expected = candidate["source_hashes"] if role == "after" else (
                        state["original_baseline"]["source_hashes"] if mechanic == "jump" and stage == "ground" else state["baseline_hashes"])
                    capture(run, expected)
                else:
                    for point in observations.get("points",[]):
                        if ordinary(point["run"]) == run:
                            bind(run/"frames"/f'{point["frame_index"]:06d}.png')
    # Include calibration and every completed preservation check used to select the candidate.
    for j in jobs:
        if j.get("action") not in {"prepare", "evaluate", "finalize"} or j.get("status") != "succeeded":
            continue
        folder = directory/"final/integrity" if j["action"] == "finalize" else directory/"measurements"/j["job_id"]/"integrity"
        expected_candidate = state["baseline_hashes"] if j["action"] == "prepare" else session.verify_candidate(j["candidate_id"])["source_hashes"]
        checked = integrity.verify_checks(folder, expected_candidate_sources=expected_candidate, expected_baseline_sources=state["baseline_hashes"])
        _need(checked.get("status") == "pass", "Preservation evidence failed source/private replay")
        _need(checked.get("preservation_status") == "pass" or j["action"] == "evaluate" and not j["result"].get("eligible"), "Failed preservation was promoted")
        bind(folder/"comparison.json")
        if j["action"] == "prepare":
            for values in j["result"].get("baseline_runs", {}).values():
                for p in values:
                    analysis = data(p)
                    capture(Path(analysis["run_id"]), state["baseline_hashes"])
        elif j["action"] == "evaluate":
            for values in (j["result"].get("calibration_runs"), j["result"].get("jump_calibration_runs")):
                for p in values or []:
                    analysis = data(p)
                    capture(Path(analysis["run_id"]), expected_candidate)
    capture(Path(comparison["normal_capture"]), candidate["source_hashes"])
    capture(Path(comparison["slope_capture"]), candidate["source_hashes"])
    gates.update(numerical={"status":"pass","mechanics":numbers}, private_runtime={"status":"pass","captures":runtime_count},
                 preservation={"status":"pass"}, independent_visual={"status":"pass","groups":manuals})
    for path, expected in evidence.items():
        _need(sha256(path) == expected, "Evidence changed while independent review was running")
    return {"schema_version":1,"audit_version":VERSION,"status":"pass","candidate_id":cid,
            "source_sha256":candidate["source_sha256"],"comparison_sha256":sha256(comparison_path),
            "launch_sha256":sha256(directory/"kiro/launch.json"),"observations_path":str(ordinary(observations_path)),
            "source_review_path":str(ordinary(source_review_path)),"gates":gates,"evidence_files":evidence,
            "scope":"Two prepared Heart mechanics; finite preservation, native CLI authorship and independent visual review"}


def audit_session(directory, observations_path, source_review_path):
    destination = ordinary(directory)/"final/independent-review.json"
    _need(not destination.exists(), "Independent review is immutable; preserve the previous attempt")
    try:
        record = _compute(directory, observations_path, source_review_path)
    except (Exception,) as exc:
        record = {"schema_version":1,"audit_version":VERSION,"status":"fail","reason":str(exc),
                  "observations_path":str(observations_path),"source_review_path":str(source_review_path)}
    record["reviewed_at"] = utc_now()
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open("x",encoding="utf-8",newline="\n") as stream:
        json.dump(record,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write("\n")
    return record


def verify_review(directory):
    path = ordinary(directory)/"final/independent-review.json"
    if not path.is_file():
        return {"status":"pending","reason":"Independent review is not present"}
    try:
        saved = read_json(path)
        _need(saved.get("status") == "pass" and saved.get("audit_version") == VERSION, "Independent review did not pass or has another protocol")
        for name, expected in saved["evidence_files"].items():
            _need(sha256(ordinary(name)) == expected, "Independent evidence changed")
        current = _compute(directory,saved["observations_path"],saved["source_review_path"])
        _need(current == {k:v for k,v in saved.items() if k != "reviewed_at"}, "Independent acceptance no longer replays")
        return {"status":"pass","candidate_id":saved["candidate_id"],"source_sha256":saved["source_sha256"],
                "comparison_sha256":saved["comparison_sha256"],"audit_sha256":sha256(path)}
    except Exception as exc:
        return {"status":"fail","reason":str(exc)}
