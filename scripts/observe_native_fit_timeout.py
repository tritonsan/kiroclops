"""Read-only samples of one native timed shell command and its Python child.

No kill, suspend, command replay, candidate change or acceptance override.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.backend import _identity
from show_native_calls import calls


def run(label):
    session = ROOT/'artifacts/phase3a/completion-paired-cohort-01/sessions/svg-tools-02/svg'
    output = ROOT/'artifacts/phase3a/completion-20260927'/('svg-tools-02-fit-process-'+label+'.json')
    assert not output.exists()
    call_id = 'run_command_toolu_bdrk_01VbjAwd4WzLcTfAreagnkks'
    log = session/'kiro/run/stdout.jsonl'
    rows, incomplete = calls(log)
    matching = [r for r in rows if r['tool_call_id'] == call_id]
    assert len(matching) == 1
    call = matching[0]
    assert call['arguments'][0]['command'] == 'python .kiroclops-scratch\\fit.py'
    assert call['arguments'][0]['timeout'] == 300000
    ps = "$p=Get-CimInstance Win32_Process; @($p | Where-Object { $_.ProcessId -in 19408,12644,20676 }) | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    value = subprocess.check_output(['pwsh.exe', '-NoProfile', '-NonInteractive', '-Command', ps], text=True, encoding='utf-8')
    processes = json.loads(value) if value.strip() else []
    if isinstance(processes, dict):
        processes = [processes]
    for process in processes:
        process['observed_process_identity'] = _identity(process['ProcessId'])
    initial = output.with_name('svg-tools-02-fit-process-before-timeout.json')
    if label != 'before-timeout':
        prior = read_json(initial)
        expected = {r['ProcessId']: r['observed_process_identity'] for r in prior['processes']}
        for process in processes:
            process['same_process_as_initial'] = process['observed_process_identity'] == expected.get(process['ProcessId'])
    proof = read_json(ROOT/'artifacts/phase3a/panel-opus55-native-audit-02/source-proof.json')
    raw = Path(proof['source']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == proof['source_sha256']
    text = raw.decode('utf-8')
    fragments = []
    for start, end in [(8185300, 8190250), (10533900, 10535700)]:
        fragment = text[start:end]
        fragments.append({'character_offset': start, 'text': fragment,
                          'utf8_sha256': hashlib.sha256(fragment.encode()).hexdigest()})
    sample = {'kind': 'ReadOnlyNativeCommandProcessSample', 'sampled_at': time.time(),
        'label': label, 'call_id': call_id, 'call_status': call['status'],
        'arguments_sha256': [digest(a) for a in call['arguments']],
        'native_log_incomplete_tail': incomplete, 'processes': processes,
        'installed_cli_source': proof['source'], 'installed_cli_source_sha256': proof['source_sha256'],
        'source_fragments': fragments, 'observer_sha256': file_sha256(__file__),
        'process_modified': False, 'model_coached': False, 'candidate_edited': False,
        'interpretation': 'Observed command/PID/parent/time correspondence only. Windows default terminal source signals its shell process; a later same-identity child sample is needed to establish survival. This does not alone attribute any capture failure.'}
    write_json(output, sample)
    print({'label': label, 'native_status': call['status'],
           'processes': [{k:r.get(k) for k in ('ProcessId', 'ParentProcessId', 'observed_process_identity', 'same_process_as_initial')}
                         for r in processes]})


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--label', required=True, choices=['before-timeout', 'after-timeout', 'later', 'after-cleanup'])
    run(p.parse_args().label)
