"""Bind read-only process samples to the actual timed-out native command."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest


def run():
    out = ROOT/'artifacts/phase3a/completion-20260927'
    receipt = out/'svg-tools-02-native-timeout-diagnostic.json'
    assert not receipt.exists()
    labels = ['before-timeout', 'after-timeout', 'later', 'after-cleanup']
    paths = [out/f'svg-tools-02-fit-process-{label}.json' for label in labels]
    samples = [read_json(p) for p in paths]
    assert all(s['process_modified'] is False and s['model_coached'] is False for s in samples)
    assert all(a['sampled_at'] < b['sampled_at'] for a, b in zip(samples, samples[1:]))
    before = {p['ProcessId']: p for p in samples[0]['processes']}
    shell, child = before[12644], before[19408]
    assert shell['ParentProcessId'] == 20676 and child['ParentProcessId'] == 12644
    assert shell['observed_process_identity'] == '134350059566709081'
    assert child['observed_process_identity'] == '134350059571581677'
    for sample in samples[1:3]:
        processes = {p['ProcessId']: p for p in sample['processes']}
        assert sample['call_status'] == 'failed'
        assert 12644 not in processes
        assert processes[19408]['same_process_as_initial'] is True
    final_processes = {p['ProcessId']: p for p in samples[3]['processes']}
    assert 19408 not in final_processes
    assert final_processes[20676]['same_process_as_initial'] is True
    log = ROOT/'artifacts/phase3a/completion-paired-cohort-01/sessions/svg-tools-02/svg/kiro/run/stdout.jsonl'
    failed = []
    for number, line in enumerate(log.read_text('utf-8').splitlines(), 1):
        update = json.loads(line).get('params', {}).get('update', {})
        if update.get('toolCallId') == samples[0]['call_id'] and update.get('status') == 'failed':
            failed.append({'line': number, 'event': update})
    assert len(failed) == 1
    event = failed[0]['event']
    assert digest(event['rawInput']) in samples[0]['arguments_sha256']
    assert event['rawOutput']['output'].strip() == 'Command timed out after 300000ms'
    source = Path(samples[0]['installed_cli_source'])
    assert file_sha256(source) == samples[0]['installed_cli_source_sha256']
    text = source.read_bytes().decode('utf-8')
    for sample in samples:
        assert sample['installed_cli_source_sha256'] == samples[0]['installed_cli_source_sha256']
        for fragment in sample['source_fragments']:
            start = fragment['character_offset']
            assert text[start:start+len(fragment['text'])] == fragment['text']
    result = {
        'kind': 'NativeWindowsCommandTimeoutDiagnostic', 'status': 'child_survival_confirmed',
        'native_call_id': samples[0]['call_id'], 'failed_event': failed[0],
        'samples': [{'path': p.name, 'sha256': file_sha256(p), 'sampled_at': s['sampled_at'],
                     'process_ids': [v['ProcessId'] for v in s['processes']]}
                    for p, s in zip(paths, samples)],
        'same_child_survival_observed_seconds': samples[2]['sampled_at']-samples[1]['sampled_at'],
        'installed_cli_source_sha256': file_sha256(source),
        'source_diagnosis': 'The installed DefaultTerminal path uses detached:false on Windows and process.kill on its shell, while process-group kill is only used off Windows. It does not own the Windows child tree per command.',
        'final_sample_interpretation': 'The label after-cleanup was chosen before sampling; it does NOT establish cleanup. Python19408 was absent while the same KAS20676 still lived. The child may have finished naturally. No inferred exit reason or exact child exit time.',
        'limits': ['No operator terminated or modified these processes.',
                   'Observed survival does not prove a later capture failed because of this process.',
                   'This is a local terminal lifecycle defect, not evidence of model disconnect or fallback.',
                   'The cohort source, native call and original trial outcome are unchanged.'],
        'repair_direction': 'Use a supported client terminal with a suspended-start Windows Job per command, preserving verified asynchronous analysis-worker handoffs and killing all command-owned descendants on timeout. This requires separate integration and testing; it is not an applied fix.',
        'repair_applied': False, 'primary_outcome_modified': False,
        'producer_sha256': file_sha256(__file__)}
    write_json(receipt, result)
    print({k: result[k] for k in ('status', 'same_child_survival_observed_seconds', 'repair_applied')})


if __name__ == '__main__':
    run()
