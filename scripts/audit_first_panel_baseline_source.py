"""Supplementary read-only source authorship/access audit, outside acceptance."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine.review import audit_native_run


def main():
    base = ROOT / 'artifacts/phase3a'
    session = base / 'completion-paired-cohort-01/sessions/panel-baseline-01/panel'
    output = base / 'completion-20260927/panel-baseline-01-source-audit.json'
    assert not output.exists()
    state = read_json(session / 'session.json')
    assert state['status'] == 'finalized' and not state.get('active_job_id')
    target = Path(state['target']).resolve()
    candidate = next(c for c in state['candidates'] if c['candidate_id'] == state['selected_candidate_id'])
    frozen = session / 'candidates' / candidate['candidate_id'] / 'source'
    hashes = source_hashes(frozen)
    assert digest(hashes) == candidate['source_sha256']
    changed = sorted(k for k in set(hashes) | set(state['baseline_hashes'])
                     if hashes.get(k) != state['baseline_hashes'].get(k))
    assert changed == ['src/panel-motion.js']
    native = audit_native_run(session)
    writes = {}
    authors = {}
    for line in (session / 'kiro/run/stdout.jsonl').read_text('utf-8').splitlines():
        update = json.loads(line).get('params', {}).get('update', {})
        if update.get('sessionUpdate') != 'tool_call':
            continue
        args = update.get('rawInput') or {}
        if update.get('title') not in ('Write File', 'Replace in File'):
            continue
        path = Path(args['path']).resolve()
        assert path.is_relative_to(target)
        name = path.relative_to(target).as_posix()
        if update['title'] == 'Write File':
            writes[name] = args['text']
        else:
            assert name in writes and writes[name].count(args['oldStr']) == 1
            writes[name] = writes[name].replace(args['oldStr'], args['newStr'], 1)
        authors.setdefault(name, []).append({'tool_call_id': update['toolCallId'],
                                             'arguments_sha256': digest(args)})
    assert set(writes) == {
        'src/panel-motion.js', '.kiroclops-scratch/track.py', '.kiroclops-scratch/track2.py',
        '.kiroclops-scratch/fit.py', '.kiroclops-scratch/fit_before.py',
        '.kiroclops-scratch/compare.py', '.kiroclops-scratch/sim.mjs',
    }
    checked = []
    for name, text in writes.items():
        path = frozen / name if name in hashes else target / name
        assert path.read_text('utf-8') == text
        checked.append({'name': name, 'file_sha256': file_sha256(path),
                        'native_writes': authors[name], 'full_source_read_by_reviewer': True})
    write_json(output, {
        'kind': 'SupplementarySourceAuthorshipAudit', 'status': 'pass',
        'scope': 'Actual source and six scratch helpers read by Codex assistant; native writes replayed. No external human attestation. Not a numerical/visual acceptance override.',
        'candidate_sha256': candidate['source_sha256'], 'changed_application_files': changed,
        'native_audit': native, 'reviewed_files': checked,
        'source_observations': [
            'Only the real panel motion controller changes; rendering, dimensions, pointer dispatch and styles remain bound to baseline hashes.',
            'Three analytic damped springs integrate actual state, target, dt and inputs; the controller does not load recorded coordinates.',
            'Normal Kiro independently wrote pixel tracking and spring fitting scripts using its ordinary file/terminal tools.',
            'track.py and track2.py read published calibration PNGs/manifests. fit.py and fit_before.py consume their local tracks.json. compare.py reads published candidate/reference calibration. sim.mjs imports only the edited controller.',
            'No helper reads reference source, evaluator-private coordinates or heldout results; helper source contains no dynamic eval/exec or remote calls.',
        ],
        'primary_result_unchanged': True, 'numerical_result': '3/12',
        'independent_visual_review': 'pending',
        'evidence_hashes': {n: file_sha256(session / n) for n in
                            ('session.json', 'kiro/launch.json', 'kiro/run/stdout.jsonl', 'kiro/tool-audit.jsonl')},
        'audit_source_sha256': file_sha256(__file__),
    })
    print({'source_authorship': 'pass', 'native_audit': native['status'],
           'reviewed_files': len(checked), 'motion_acceptance_changed': False})


if __name__ == '__main__':
    main()
