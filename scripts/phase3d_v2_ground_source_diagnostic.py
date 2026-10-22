"""Append-only ground source facts, native verdict and independent review links.

This is a diagnostic consumer.  It never writes canonical Session acceptance,
never aliases native CLI versions and never manufactures inspection verdicts.
Run it only after recording and the native turn/cleanup have finished.  The
decisions file must come from an actual independent source/transcript review.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import difflib
import json
from pathlib import Path
import shutil
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine import evidence, review, suite_final_raw_review as final
from kiroclops.local_engine.session import Session, _ordinary, source_hashes
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from scripts import phase3d_v2_ground_final_raw as ground
from scripts import phase3d_v2_ground_normal_raw as normal

VERSION = 'canonical-ground-source-diagnostic-v1'
CHECKS = ('actual_code_authorship', 'no_coordinate_replay', 'no_private_reference_access',
          'no_operator_coaching', 'protected_behavior_preserved')
NATIVE_FILES = ('kiro/launch.json', 'kiro/config.json', 'kiro/run/stdout.jsonl',
                'kiro/run/outbound.jsonl', 'kiro/tool-audit.jsonl')
TEXT_SUFFIXES = {'.gd', '.tscn', '.tres', '.godot', '.py', '.js', '.mjs', '.json', '.md', '.cfg'}


def _need(value, message, code='invalid_source_diagnostic'):
    if not value:
        raise VisualError(code, message)


def _put(inventory, path, expected=None):
    path = _ordinary(path)
    sha = file_sha256(path)
    _need(expected is None or sha == expected, 'Bound input changed: ' + str(path), 'stale_diagnostic')
    _need(str(path) not in inventory or inventory[str(path)] == sha, 'Conflicting input byte identity.')
    inventory[str(path)] = sha
    return sha


def _code_sources():
    return final._code_sources() | {str(Path(module.__file__).resolve()): file_sha256(module.__file__)
                                   for module in (ground, normal)} | {
        str(Path(__file__).resolve()): file_sha256(__file__)}


def _lines(path):
    path = _ordinary(path)
    _need(path.stat().st_size <= 32 * 1024 * 1024, 'Native log exceeds the ordinary audit bound.')
    text = path.read_text(encoding='utf-8')
    _need(not text or text.endswith('\n'), 'Native log has an incomplete last row.')
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    _need(rows and all(isinstance(row, dict) for row in rows), 'A complete object-valued native log is required.')
    return rows


def observed_calls(inbound, session_id):
    """Actual starting inputs and terminal outcomes; no native-policy approval."""
    calls = {}
    for number, event in enumerate(inbound, 1):
        params = event.get('params', {})
        update = params.get('update', {})
        if not isinstance(update, dict) or update.get('sessionUpdate') not in ('tool_call', 'tool_call_update'):
            continue
        identity = update.get('toolCallId')
        _need(params.get('sessionId') == session_id and isinstance(identity, str) and identity,
              'Tool update belongs to an unbound native session.')
        if update['sessionUpdate'] == 'tool_call':
            _need(identity not in calls, 'Duplicate native tool start.')
            calls[identity] = {'tool_call_id': identity, 'start_line': number, 'updates': [], 'lines': []}
        if identity not in calls:
            # A pre-handle interception is retained for inspection, never
            # silently excluded or accepted as an application mutation.
            calls[identity] = {'tool_call_id': identity, 'start_line': None, 'updates': [], 'lines': []}
        calls[identity]['updates'].append(deepcopy(update))
        calls[identity]['lines'].append(number)
    for call in calls.values():
        call['raw_inputs'] = [u['rawInput'] for u in call['updates'] if 'rawInput' in u]
        call['input_set_sha256'] = digest(call['raw_inputs'])
        call['updates_sha256'] = digest(call['updates'])
        call['title'] = next((u['title'] for u in call['updates'] if u.get('title')), None)
        call['status'] = call['updates'][-1].get('status')
    return calls


def _output_job_ids(value):
    """Read only structured tool output; model prose is not a job binding."""
    found = set()
    if isinstance(value, dict):
        if isinstance(value.get('job_id'), str):
            found.add(value['job_id'])
        for key, item in value.items():
            if key == 'text' and isinstance(item, str):
                try:
                    parsed = json.loads(item)
                except (ValueError, TypeError):
                    continue
                if isinstance(parsed, (dict, list)):
                    found.update(_output_job_ids(parsed))
            elif isinstance(item, (dict, list)):
                found.update(_output_job_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_output_job_ids(item))
    return found


def _call_tool(call):
    tool = review._native_tool_name(call['title'])
    if tool == 'tool_load':
        ids = {value.get('tool_id') for value in call['raw_inputs'] if isinstance(value, dict)}
        if len(ids) == 1:
            return review._native_tool_name(next(iter(ids)))
    return tool


def replay_selected_text(calls, *, checkpoint_id, evaluation_job_id, target, baseline, selected,
                         baseline_hashes, selected_hashes, session_id):
    """Mechanical observed edit-payload replay, independent of audit PASS/FAIL.

    Strict current diff/checkpoint shapes are inspected using the existing
    literal helper.  This does not claim that an unsupported CLI version is
    authorized by that helper's source-bound native policy.
    """
    checkpoint = calls.get(checkpoint_id)
    _need(checkpoint is not None and checkpoint['start_line'] is not None
          and _call_tool(checkpoint) == '@kiroclops/evaluate_candidate'
          and checkpoint['status'] == 'completed'
          and evaluation_job_id in _output_job_ids(checkpoint['updates'][-1].get('rawOutput')),
          'Supply the actual selected evaluation call and its structured job identity.', 'wrong_authorship_checkpoint')
    target, baseline, selected = map(_ordinary, (target, baseline, selected))
    changed = sorted(name for name in set(baseline_hashes) | set(selected_hashes)
                     if baseline_hashes.get(name) != selected_hashes.get(name))
    texts, expected, errors, mutations = {}, {}, [], []
    for name in changed:
        if name not in selected_hashes or Path(name).suffix.lower() not in TEXT_SUFFIXES:
            errors.append({'code': 'unsupported_changed_source', 'path': name})
            continue
        try:
            texts[name] = safe_path(baseline, name).read_bytes().decode('utf-8') if name in baseline_hashes else ''
            expected[name] = safe_path(selected, name).read_bytes().decode('utf-8')
        except UnicodeError:
            errors.append({'code': 'non_text_changed_source', 'path': name})
    ordered = sorted(calls.values(), key=lambda call: call['lines'][-1])
    for call in ordered:
        if call['lines'][-1] >= checkpoint['start_line']:
            continue
        tool = _call_tool(call)
        if tool not in ('fs_write', 'fs_append', 'str_replace'):
            continue
        args = [review._native_arguments(tool, value) for value in call['raw_inputs']]
        if not args or any(value is None for value in args) or len({digest(value) for value in args}) != 1:
            errors.append({'code': 'ambiguous_native_edit_input', 'tool_call_id': call['tool_call_id']})
            continue
        arg = args[0]
        try:
            path = _ordinary(Path(arg['path']) if Path(arg['path']).is_absolute() else target / arg['path'])
            _need(path.is_relative_to(target), 'Observed native edit escapes its target.')
            relative = path.relative_to(target).as_posix()
            relative = next((name for name in texts if name.casefold() == relative.casefold()), relative)
        except (VisualError, KeyError, TypeError, ValueError) as exc:
            errors.append({'code': 'outside_target_or_unknown_edit_path', 'tool_call_id': call['tool_call_id'], 'message': str(exc)})
            continue
        if relative not in texts:
            continue  # All such helper inputs remain in the inspection manifest.
        terminal = call['updates'][-1]
        if call['status'] != 'completed' or not review._builtin_227_edit_terminal(
                terminal, arg, tool, str(target), session_id):
            errors.append({'code': 'unsupported_observed_edit_terminal', 'tool_call_id': call['tool_call_id'], 'path': relative})
            continue
        diff = terminal['content'][0]
        if diff['oldText'] != texts[relative]:
            errors.append({'code': 'native_edit_prior_text_mismatch', 'tool_call_id': call['tool_call_id'], 'path': relative})
            continue
        texts[relative] = diff['newText']
        mutations.append({'path': relative, 'tool': tool, 'tool_call_id': call['tool_call_id'],
                          'start_line': call['start_line'], 'terminal_line': call['lines'][-1],
                          'arguments_sha256': digest(arg), 'updates_sha256': call['updates_sha256']})
    matches = {name: texts.get(name) == value for name, value in expected.items()}
    for name, matches_selected in matches.items():
        if not matches_selected:
            errors.append({'code': 'selected_source_not_replayed', 'path': name})
    return {'kind': 'ObservedSelectedNativeTextPayloadReplay', 'status': 'pass' if not errors else 'fail',
            'checkpoint_tool_call_id': checkpoint_id, 'checkpoint_line': checkpoint['start_line'],
            'evaluation_job_id': evaluation_job_id, 'changed_files': changed, 'matches': matches,
            'mutations': mutations, 'errors': errors, 'native_policy_acceptance_granted': False,
            'unsupported_cli_version_aliased': False}


def _decisions(value, context, state, source_hashes_, baseline_hashes, native_hashes, calls, inventory):
    _need(value.get('kind') == 'ActualGroundSourceInspectionDecisions' and value.get('schema_version') == 1
          and value.get('actual_source_and_native_inputs_inspected') is True
          and isinstance(value.get('reviewer'), str) and value['reviewer'].strip()
          and _ordinary(value['session_directory']) == _ordinary(context['directory'])
          and value.get('selected_candidate_id') == state['selected_candidate_id']
          and value.get('candidate_sha256') == digest(source_hashes_)
          and value.get('source_hashes') == source_hashes_ and value.get('baseline_hashes') == baseline_hashes
          and value.get('native_evidence_hashes') == native_hashes,
          'Actual inspection decisions must bind the exact selected source, baseline and native bytes.')
    checks = value.get('checks', [])
    _need(isinstance(checks, list) and len(checks) == 5 and {row.get('id') for row in checks} == set(CHECKS)
          and all(row.get('verdict') in ('pass', 'fail', 'pending')
                  and isinstance(row.get('evidence'), str) and row['evidence'].strip() for row in checks),
          'Supply exactly five explicit evidence-bearing decisions; no automatic PASS defaults.')
    _need(value.get('inspected_tool_calls') == {identity: call['input_set_sha256'] for identity, call in calls.items()},
          'Every actually observed native input set must be independently inspected and bound.')
    inspected = value.get('inspected_source_files')
    _need(isinstance(inspected, list) and inspected, 'Actual source-file inspections are required.')
    paths = set()
    for item in inspected:
        path = _ordinary(item['path'])
        _need(str(path) not in paths and item.get('actually_inspected') is True
              and isinstance(item.get('observations'), str) and item['observations'].strip(),
              'Every inspected file needs a distinct exact path and actual observation.')
        paths.add(str(path))
        _put(inventory, path, item['sha256'])
    changed = {name for name in set(source_hashes_) | set(baseline_hashes)
               if source_hashes_.get(name) != baseline_hashes.get(name)}
    selected = _ordinary(context['sealed_source'])
    _need(all(str(safe_path(selected, name)) in paths for name in changed if name in source_hashes_),
          'Every changed selected file must actually be inspected; filenames alone are insufficient.')
    return deepcopy(checks)


def _transport_facts(outbound, config, launch):
    _need(len(outbound) % 2 == 0, 'Incomplete write-ahead native outbound log.')
    messages = []
    for index in range(0, len(outbound), 2):
        attempt, sent = outbound[index:index + 2]
        _need(attempt.get('phase') == 'attempt' and sent.get('phase') == 'sent'
              and attempt.get('sequence') == sent.get('sequence') == index // 2 + 1
              and attempt.get('payload_sha256') == sent.get('payload_sha256') == digest(attempt.get('message'))
              and 'message' not in sent, 'Native outbound byte binding differs.')
        messages.append(attempt['message'])
    creates = [message for message in messages if message.get('method') == 'session/new']
    prompts = [message for message in messages if message.get('method') == 'session/prompt']
    task = next((i for i, message in enumerate(messages) if message.get('method') == 'session/prompt'), None)
    posttask_requests = [message for message in messages[task + 1:] if message.get('method')] if task is not None else []
    sid = launch.get('preflight', {}).get('session_id')
    exact_prompt = (len(prompts) == 1 and prompts[0].get('params') ==
                    {'sessionId': sid, 'prompt': [{'type': 'text', 'text': Path(config['prompt_path']).read_text('utf-8')}]})
    return {'fresh_session_count': len(creates), 'task_prompt_count': len(prompts),
            'posttask_client_requests': posttask_requests, 'exact_prepared_prompt': exact_prompt,
            'no_followup_prompt_observed': len(creates) == len(prompts) == 1 and exact_prompt and not posttask_requests,
            'native_policy_acceptance_granted': False}


def _raw_proof(path, *, kind, validator, output, session, chain, chain_sha, source_sha, inventory):
    if path is None:
        _need(output is None, 'A review directory without its full standalone proof is incomplete.')
        return {'status': 'pending', 'kind': kind, 'reason': 'Independent raw proof has not been supplied.'}
    _need(output is not None, 'Supply both the independent aid directory and its full verification proof.')
    _put(inventory, path)
    supplied = read_json(path)
    current = validator(output, session_directory=session, parent_chain=chain, parent_chain_sha256=chain_sha)
    _need(supplied == current and supplied.get('kind') == kind and supplied.get('status') == 'pass'
          and supplied.get('parent_chain_sha256') == chain_sha
          and supplied.get('full_behavior_acceptance') is False
          and supplied.get('ordinary_reader_compatibility_claimed') is False,
          'Standalone raw proof differs from its fresh strict validator.', 'stale_diagnostic')
    identity = supplied.get('sealed_source_sha256', supplied.get('source_sha256'))
    _need(identity == source_sha, 'Independent raw proof belongs to another selected source.', 'wrong_candidate')
    files = supplied.get('consumed_files')
    _need(isinstance(files, dict) and files and supplied.get('consumed_files_sha256') == digest(files),
          'Complete standalone raw input closure is required.')
    for name, sha in files.items():
        _need(str(_ordinary(name)) not in inventory or inventory[str(_ordinary(name))] == sha,
              'Independent proofs bind conflicting input identities.')
        inventory[str(_ordinary(name))] = sha  # The just-executed validator verified these bytes.
    return {'status': 'pass', 'kind': kind, 'path': str(_ordinary(path)),
            'file_sha256': file_sha256(path), 'result_sha256': supplied['result_sha256'],
            'consumed_files_sha256': supplied['consumed_files_sha256']}


def _native_result(session):
    try:
        return review.audit_native_run(session)
    except (VisualError, OSError, ValueError, KeyError, TypeError) as exc:
        return {'status': 'fail', 'exception_type': type(exc).__name__,
                'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc),
                'acceptance_override': False}


def prepare(session_directory, output, *, parent_chain, parent_chain_sha256, decisions,
            raw_review=None, raw_proof=None, normal_review=None, normal_proof=None):
    session, output = map(_ordinary, (session_directory, output))
    _need(not output.exists() and not output.is_relative_to(session), 'Use a fresh external diagnostic output.', 'output_exists')
    state = Session(session)._load()
    _need(state.get('status') == 'finalized' and state.get('active_job_id') is None,
          'Recording, model work and finalization must be terminal before source inspection.')
    context, _ = final._context_session(session)
    _need(context['domain'] == 'game' and context['stage'] == 'ground', 'Only the canonical ground stage is supported.')
    _need(not output.is_relative_to(_ordinary(context['suite']))
          and not output.is_relative_to(_ordinary(state['target']))
          and not output.is_relative_to(_ordinary(context['authoring_parent']))
          and not output.is_relative_to(_ordinary(context['original'])), 'Diagnostic output cannot alter producer source.', 'unsafe_path')
    chain_link = ground._parent_chain(parent_chain, parent_chain_sha256, context)
    selected = next(row for row in state['candidates'] if row['candidate_id'] == state['selected_candidate_id'])
    source_sha = selected['source_sha256']
    current = source_hashes(context['sealed_source'])
    baseline = source_hashes(state['baseline'])
    _need(digest(current) == source_sha and baseline == state['baseline_hashes'], 'Selected source or authoring baseline changed.')
    inventory = {}
    code = _code_sources()
    for name in ('session.json', 'binding.json'):
        _put(inventory, session / name)
    _put(inventory, parent_chain, parent_chain_sha256)
    _put(inventory, decisions)
    for project, hashes in ((context['sealed_source'], current), (state['baseline'], baseline)):
        for name, sha in hashes.items():
            _put(inventory, safe_path(_ordinary(project), name), sha)
    for job_id in (state['prepare_job_id'], selected['job_id'], state['final_job_id']):
        for name in ('job.json', 'binding.json', 'result.json'):
            _put(inventory, session / 'jobs' / job_id / name)
    evaluation = evidence._completed_job(session, state, selected['job_id'], 'evaluate', selected)
    result = evidence._completed_job(session, state, state['final_job_id'], 'finalize', selected)
    _need(evaluation.get('eligible') is True and result.get('suite_sha256') == context['suite_sha256']
          and result.get('pass') is state['final'].get('pass'), 'Completed result belongs to another selection or suite.')
    final_result = session / 'jobs' / state['final_job_id'] / 'result.json'
    native_hashes = {name: _put(inventory, session / name) for name in NATIVE_FILES}
    config, launch = read_json(session / 'kiro/config.json'), read_json(session / 'kiro/launch.json')
    _need(launch.get('terminal_cleanup', {}).get('status') == 'pass' and launch.get('finished_at') is not None,
          'Wait for actual terminal native cleanup; a prompt response alone is insufficient.')
    _put(inventory, config['prompt_path'])
    inbound, outbound = _lines(session / 'kiro/run/stdout.jsonl'), _lines(session / 'kiro/run/outbound.jsonl')
    calls = observed_calls(inbound, launch['preflight']['session_id'])
    inspected = read_json(decisions)
    checks = _decisions(inspected, context, state, current, baseline, native_hashes, calls, inventory)
    replay = replay_selected_text(calls, checkpoint_id=inspected['selected_evaluation_tool_call_id'],
        evaluation_job_id=selected['job_id'], target=config['target_dir'], baseline=state['baseline'],
        selected=context['sealed_source'], baseline_hashes=baseline, selected_hashes=current,
        session_id=launch['preflight']['session_id'])
    transport = _transport_facts(outbound, config, launch)
    compact_inputs = dict(inventory)
    raw_link = _raw_proof(raw_proof, kind='VerifiedCanonicalGroundRawInspection', validator=ground.validate,
        output=raw_review, session=session, chain=parent_chain, chain_sha=parent_chain_sha256,
        source_sha=source_sha, inventory=inventory)
    normal_link = _raw_proof(normal_proof, kind='VerifiedCanonicalGroundNormalRawInspection', validator=normal.validate,
        output=normal_review, session=session, chain=parent_chain, chain_sha=parent_chain_sha256,
        source_sha=source_sha, inventory=inventory)
    by_id = {row['id']: row for row in checks}
    if by_id['actual_code_authorship']['verdict'] == 'pass' and replay['status'] != 'pass':
        by_id['actual_code_authorship'].update(verdict='fail', evidence=by_id['actual_code_authorship']['evidence']
                                            + ' Observed source payload replay failed; see native-source-replay.json.')
    elif by_id['actual_code_authorship']['verdict'] == 'pass' and not replay['changed_files']:
        by_id['actual_code_authorship'].update(verdict='pending', evidence=by_id['actual_code_authorship']['evidence']
                                              + ' Selected source is unchanged; new source authorship is not established here.')
    if by_id['no_operator_coaching']['verdict'] == 'pass' and not transport['no_followup_prompt_observed']:
        by_id['no_operator_coaching'].update(verdict='fail', evidence=by_id['no_operator_coaching']['evidence']
                                           + ' Actual outbound task history does not support the claimed single-prompt workflow.')
    protected = by_id['protected_behavior_preserved']
    expected_cases = {(case['case_id'], case['role']) for case in context['cases'] if case['role'] != 'calibration'}
    comparisons = result.get('comparisons')
    found_cases = [(row.get('case_id'), row.get('role')) for row in comparisons] if isinstance(comparisons, list) else []
    behavior = (result.get('pass') is True and result.get('preservation_pass') is True
                and result.get('visual_wall_status') == result.get('normal_level_status') == 'pass'
                and len(found_cases) == len(expected_cases) and set(found_cases) == expected_cases
                and all(row.get('pass') is True for row in comparisons))
    if protected['verdict'] == 'pass' and not behavior:
        protected.update(verdict='fail', evidence=protected['evidence']
                         + ' Bound completed final behavior/preservation/functional results do not all pass.')
    elif protected['verdict'] == 'pass' and (raw_link['status'] != 'pass' or normal_link['status'] != 'pass'):
        protected.update(verdict='pending', evidence=protected['evidence']
                         + ' Both independent final and normal-level raw proofs are still required.')
    native = _native_result(session)
    facts_status = ('fail' if any(row['verdict'] == 'fail' for row in checks)
                    else 'pending' if any(row['verdict'] == 'pending' for row in checks) else 'pass')
    policy_context = {'cli_version': launch.get('cli_version'), 'kas_version': launch.get('kas_version'),
                      'launcher_sha256': config.get('launcher_sha256'), 'target_dir': config.get('target_dir')}
    supported = review._builtin_policy_227(policy_context) is not None
    consumed_count = launch.get('stream_configuration', {}).get('events_seen')
    tail = (inbound[consumed_count:] if type(consumed_count) is int and 0 <= consumed_count <= len(inbound) else [])
    tail_diagnostic = {'kind': 'ObservedNativePostResponseTailDiagnostic', 'schema_version': 1,
        'live_consumed_events_as_recorded': consumed_count, 'recorded_event_count': len(inbound),
        'tail_count': len(tail), 'diagnostic_preview_limit': 64,
        'events': [{'line': consumed_count + index + 1, 'payload_sha256': digest(value), 'payload': value}
                   for index, value in enumerate(tail[:64])],
        'full_stream_copy': 'native-evidence/02-stdout.jsonl',
        'native_acceptance_override': False, 'unsupported_version_aliased': False,
        'model_deviation_inferred_from_audit_failure': False}
    # Recheck the compact canonical/inspection inventory before publication.
    # Large raw inventories have already been checked by their official fresh validators.
    for path, sha in compact_inputs.items():
        _need(file_sha256(path) == sha, 'Diagnostic input changed before publication.', 'stale_diagnostic')
    _need(code == _code_sources(), 'Diagnostic or native-reader source changed.', 'stale_diagnostic')
    output.mkdir(parents=True)
    evidence_copies = []
    for number, name in enumerate(NATIVE_FILES):
        destination = output / 'native-evidence' / f'{number:02}-{Path(name).name}'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(session / name, destination)
        _need(file_sha256(destination) == native_hashes[name], 'Native evidence copy differs.')
        evidence_copies.append({'source': str(session / name), 'copy': str(destination), 'sha256': native_hashes[name]})
    diffs = []
    for name in replay['changed_files']:
        if name in current and Path(name).suffix.lower() in TEXT_SUFFIXES:
            before = safe_path(_ordinary(state['baseline']), name).read_text('utf-8').splitlines(True) if name in baseline else []
            after = safe_path(_ordinary(context['sealed_source']), name).read_text('utf-8').splitlines(True)
            diffs.extend(difflib.unified_diff(before, after, fromfile='authoring-parent/' + name, tofile='selected/' + name))
    (output / 'candidate.diff').write_text(''.join(diffs), encoding='utf-8')
    write_json(output / 'native-source-replay.json', replay)
    write_json(output / 'native-audit.json', native)
    write_json(output / 'native-tail-diagnostic.json', tail_diagnostic)
    write_json(output / 'transport-facts.json', transport)
    write_json(output / 'inspection-decisions.json', inspected)
    source_review = {'kind': 'IndependentGroundSourceDiagnostic', 'schema_version': 1, 'version': VERSION,
        'status': facts_status, 'reviewer': inspected['reviewer'], 'session_directory': str(session),
        'selected_candidate_id': state['selected_candidate_id'], 'candidate_sha256': source_sha,
        'source_hashes': current, 'baseline_hashes': baseline, 'native_evidence_hashes': native_hashes,
        'checks': checks, 'native_validation_status': native['status'], 'native_audit': native,
        'native_policy_context': policy_context, 'current_builtin_policy_supported': supported,
        'unsupported_version_aliased': False, 'prospective_parent_chain': chain_link,
        'raw_review': raw_link, 'normal_raw_review': normal_link,
        'full_behavior_acceptance': False, 'canonical_source_review_published': False,
        'ordinary_reader_compatibility_claimed': False, 'diagnostic_facts_only': True,
        'method': 'independent_source_and_native_trace_audit', 'new_model_generation': False,
        'original_outcomes_rewritten': False}
    write_json(output / 'source-diagnostic.json', source_review)
    generated = {path.relative_to(output).as_posix(): file_sha256(path)
                 for path in sorted(output.rglob('*')) if path.is_file()}
    receipt = {'kind': 'AppendOnlyGroundSourceDiagnosticReceipt', 'schema_version': 1, 'version': VERSION,
        'status': 'fail' if native['status'] != 'pass' or facts_status == 'fail' else facts_status,
        'session_directory': str(session), 'candidate_sha256': source_sha,
        'diagnostic_sha256': digest(source_review), 'diagnostic_file_sha256': file_sha256(output / 'source-diagnostic.json'),
        'final_result_file_sha256': inventory[str(_ordinary(final_result))], 'parent_chain_sha256': parent_chain_sha256,
        'consumed_files': inventory, 'consumed_files_sha256': digest(inventory),
        'code_sources': code, 'generated_files': generated, 'evidence_copies': evidence_copies,
        'native_validation_status': native['status'], 'source_facts_status': facts_status,
        'protected_behavior_status': protected['verdict'], 'full_behavior_acceptance': False,
        'ordinary_reader_compatibility_claimed': False, 'canonical_source_review_published': False,
        'native_acceptance_override': False, 'historical_outcomes_rewritten': False}
    write_json(output / 'receipt.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('session', 'output', 'parent-chain', 'decisions'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--parent-chain-sha256', required=True)
    for name in ('raw-review', 'raw-proof', 'normal-review', 'normal-proof'):
        parser.add_argument('--' + name, type=Path)
    args = parser.parse_args()
    value = prepare(args.session, args.output, parent_chain=args.parent_chain,
        parent_chain_sha256=args.parent_chain_sha256, decisions=args.decisions,
        raw_review=args.raw_review, raw_proof=args.raw_proof,
        normal_review=args.normal_review, normal_proof=args.normal_proof)
    print(json.dumps({key: value[key] for key in ('kind', 'status', 'native_validation_status',
        'source_facts_status', 'protected_behavior_status', 'full_behavior_acceptance')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
