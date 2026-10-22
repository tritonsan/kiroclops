"""Isolated replay of a rejected Delete File envelope; no cohort mutation.

The original result remains failed. This diagnoses an auditor compatibility
defect and adversarially checks the proposed correction before production use.
"""
from pathlib import Path
from copy import deepcopy
import sys
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, digest, file_sha256
from kiroclops.local_engine import review


def run(session, destination):
    session, destination = Path(session).resolve(), Path(destination).resolve()
    assert not destination.exists(), 'Preserve existing diagnostic evidence'
    config = read_json(session/'kiro/config.json')
    launch = read_json(session/'kiro/launch.json')
    inbound = [json.loads(line) for line in (session/'kiro/run/stdout.jsonl').read_text('utf-8').splitlines() if line.strip()]
    decisions = [json.loads(line) for line in (session/'kiro/tool-audit.jsonl').read_text('utf-8').splitlines() if line.strip()]
    kwargs = dict(session_id=launch['preflight']['session_id'], started_at=launch['started_at'],
        finished_at=launch['finished_at'], scope_sha256=digest(config['generated_hashes']),
        runtime_context={'cli_version': launch.get('cli_version'), 'kas_version': launch.get('kas_version'),
            'launcher_sha256': config['launcher_sha256'], 'target_dir': config['target_dir'],
            'hook_id': str(Path(config['target_dir'])/'.kiro/hooks/kiroclops-guard.json')+'#hook-0'})
    original_name, original_denial = review._native_tool_name, review._hook_denied_no_output
    before = review.audit_native_tool_outcomes(inbound, decisions, **kwargs)

    def name(value):
        return 'delete_file' if value in ('Delete File', 'delete_file') else original_name(value)

    def denial(updates, tool, args):
        if tool != 'delete_file':
            return original_denial(updates, tool, args)
        # The observed schema uses targetFile, unlike read/write's path. The
        # original arguments remain unchanged for decision matching/hashing.
        if (set(args) != {'targetFile', 'explanation'} or
                not all(isinstance(args[k], str) and args[k] for k in args)):
            return False
        return original_denial(updates, tool, {**args, 'path': args['targetFile']})

    review._native_tool_name, review._hook_denied_no_output = name, denial
    try:
        after = review.audit_native_tool_outcomes(inbound, decisions, **kwargs)
        full = review.audit_native_run(session)
        rejected = [event for event in inbound if event.get('params', {}).get('update', {}).get('title') == 'Delete File']
        assert len(rejected) == 1
        identity = rejected[0]['params']['update']['toolCallId']
        results = []
        for case in ('returned_output', 'empty_output_key', 'wrong_location', 'missing_hook',
                     'changed_decision', 'claimed_execution', 'unknown_metadata'):
            stream, audit = deepcopy(inbound), deepcopy(decisions)
            update = next(e['params']['update'] for e in stream
                          if e.get('params', {}).get('update', {}).get('toolCallId') == identity)
            if case == 'returned_output': update['content'] = [{'type': 'content', 'content': {'type': 'text', 'text': 'file deleted'}}]
            elif case == 'empty_output_key': update['rawOutput'] = {}
            elif case == 'wrong_location': update['locations'] = [{'path': 'C:\\unexpected\\private.txt'}]
            elif case == 'missing_hook': audit = [row for row in audit if row.get('tool') != 'delete_file']
            elif case == 'changed_decision':
                next(row for row in audit if row.get('tool') == 'delete_file')['arguments']['targetFile'] += '.other'
            elif case == 'claimed_execution': update['status'] = 'completed'
            elif case == 'unknown_metadata': update['_meta']['kiro']['unverified'] = True
            value = review.audit_native_tool_outcomes(stream, audit, **kwargs)
            results.append({'case': case, 'correctly_rejected': value['status'] == 'fail', 'errors': value['errors']})
        assert before['status'] == 'fail' and after['status'] == full['status'] == 'pass'
        assert all(item['correctly_rejected'] for item in results)
        write_json(destination, {'status': 'diagnostic_pass', 'acceptance_override': False,
            'source_or_cohort_modified': False, 'model_invoked': False,
            'method': 'Isolated-process exact alias and targetFile location binding; original empty pre-handle rejection predicate unchanged.',
            'original': before, 'proposed_tool_replay': after, 'proposed_full_native_replay': full,
            'negative_controls': results, 'script_sha256': file_sha256(Path(__file__)),
            'production_review_sha256': file_sha256(Path(review.__file__))})
        print({'status': 'diagnostic_pass', 'native_calls': after['application_call_count'],
               'negative_controls': len(results), 'cohort_result_unchanged': True}, flush=True)
    finally:
        review._native_tool_name, review._hook_denied_no_output = original_name, original_denial


if __name__ == '__main__':
    run(*sys.argv[1:])
