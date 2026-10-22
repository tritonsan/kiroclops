"""Bind the five actually read files and 41 native calls for SVG baseline02.

This trial-specific assistant inspection never edits candidate or primary result.
It records authorship separately from numerical and independent image acceptance.
"""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.session import source_hashes
from kiroclops.local_engine import review


def run(record_only=False):
    base = ROOT/'artifacts/phase3a'
    session = base/'completion-paired-cohort-01/sessions/svg-baseline-02/svg'
    out = base/'completion-20260927'
    state = read_json(session/'session.json')
    assert state['selected_candidate_id'] == 'candidate-01'
    target = Path(state['target']).resolve()
    frozen = session/'candidates/candidate-01/source'
    hashes = source_hashes(frozen)
    assert sorted(k for k in set(hashes)|set(state['baseline_hashes'])
                  if hashes.get(k) != state['baseline_hashes'].get(k)) == ['src/layout-motion.js']
    calls, writes, authors = {}, {}, {}
    for line in (session/'kiro/run/stdout.jsonl').read_text('utf-8').splitlines():
        update = json.loads(line).get('params', {}).get('update', {})
        if update.get('sessionUpdate') != 'tool_call':
            continue
        args = update.get('rawInput')
        calls[update['toolCallId']] = digest(args)
        if update.get('title') not in ('Write File', 'Replace in File'):
            continue
        path = Path(args['path']).resolve()
        assert path.is_relative_to(target)
        name = path.relative_to(target).as_posix()
        if update['title'] == 'Write File':
            writes[name] = args['text']
        else:
            assert writes[name].count(args['oldStr']) == 1
            writes[name] = writes[name].replace(args['oldStr'], args['newStr'], 1)
        authors.setdefault(name, []).append({'tool_call_id': update['toolCallId'],
                                            'arguments_sha256': digest(args)})
    assert len(calls) == 41 and list(calls)[-1] == 'run_command_toolu_bdrk_016GL6Pcvgh8u741xdv36wUC'
    assert set(writes) == {'src/layout-motion.js', '.kiroclops-scratch/track.py',
        '.kiroclops-scratch/fit.py', '.kiroclops-scratch/fit2.py', '.kiroclops-scratch/compare.py'}
    files = []
    for name, content in writes.items():
        path = frozen/name if name in hashes else target/name
        assert path.read_text('utf-8') == content
        files.append({'name': name, 'sha256': file_sha256(path),
                      'native_writes': authors[name], 'full_source_read_by_reviewer': True})
    inspection_path = out/'svg-baseline-02-sealed-source-inspection.json'
    inspection = {'kind': 'ActualAssistantSourceInspection',
        'status': 'source_read_full_acceptance_pending', 'candidate_id': 'candidate-01',
        'candidate_sha256': digest(hashes), 'reviewed_files': files,
        'inspected_native_call_arguments': calls,
        'findings': [
            'All41 actual command arguments and all Write/Replace source bodies read. The final track.py, fit.py and fit2.py were also read in full from disk.',
            'track.py measures colour-isolated icon alpha centroids from public calibration and candidate PNGs. fit.py reads derived trajectories; fit2.py compares analytic and semi-implicit springs with its own coordinate search. compare.py compares the derived public candidate tracks.',
            'Fit helper assumes a60Hz trajectory during its parameter search; actual controller consumes live dt. This assumption is a potential fitting limitation, not evidence of coordinate replay.',
            'Controller integrates live positions/velocities toward live targets with stiffness145,damping16 and up to32 substeps at300Hz. The dragged node retains pointer position and zero velocity. No recorded trajectory lookup.',
            'Onlysrc/layout-motion.js changed among application files. Source hash comparison inspects hashes in public manifests, not reference source contents.',
            'No operator correction or private source/heldout data access observed. Numeric, functional and independent image verdicts remain separate.'
        ], 'producer_sha256': file_sha256(__file__), 'primary_outcome_modified': False}
    if record_only:
        assert not inspection_path.exists()
        write_json(inspection_path, inspection)
        print({'source_inspection': 'recorded', 'files': len(files), 'native_calls': len(calls),
               'candidate_sha256': digest(hashes), 'full_acceptance': 'pending'})
        return
    prior = read_json(inspection_path)
    assert prior['reviewed_files'] == files and prior['inspected_native_call_arguments'] == calls
    assert prior['candidate_sha256'] == digest(hashes)
    launch = read_json(session/'kiro/launch.json')
    row = next(r for r in read_json(base/'completion-paired-cohort-01/progress.json')['rows']
               if r['id'] == 'svg-baseline-02')
    assert launch.get('finished_at') and row['status'] in ('failed', 'completed')
    assert not state.get('active_job_id')
    native = review.audit_native_run(session)
    result_path = session/'jobs'/state['final_job_id']/'result.json'
    assert state['final']['status'] == 'succeeded' and result_path.is_file(), 'No full final measurement: preserve inspection only'
    result = read_json(result_path)
    assert len(result['comparisons']) == 12
    protected = all(c['functional']['status'] == 'pass' and c['svg_geometry_preservation']['pass']
                    for c in result['comparisons'])
    checks = [
        ('actual_code_authorship', 'pass', 'Five source files fully read and native writes replayed to the immutable candidate controller and four scratch helpers. Onlysrc/layout-motion.js changes in the application. No operator code edit.'),
        ('no_coordinate_replay', 'pass', 'Live damped springs consume current node state, target,dt and draggedId; fixed integration substeps are computed fromdt. No recorded position table, scenario lookup or clip playback.'),
        ('no_private_reference_access', 'pass', 'All41 native argument sets inspected. Image tracking/fit/comparison use public calibration and candidate PNGs/manifests and locally derived tracks. No private physics, oracle or heldout results consumed during editing.'),
        ('no_operator_coaching', 'pass', 'One fresh native Opus5.5/max Always Allow conversation and one prepared prompt; Kiro wrote, evaluated and finalized one candidate itself. No follow-up steering or replacement trial.'),
        ('protected_behavior_preserved', 'pass' if protected else 'fail', 'Application rendering/input/styles retain baseline hashes. Source-bound final functional and SVG geometry checks ' + ('all pass.' if protected else 'do not all pass.')),
    ]
    verdict = {'kind': 'IndependentSourceReview', 'status': 'pass' if protected else 'fail',
        'reviewer': 'Codex assistant', 'method': 'independent_source_and_native_trace_audit',
        'candidate_sha256': digest(hashes), 'source_hashes': hashes,
        'baseline_hashes': state['baseline_hashes'], 'native_evidence_hashes': native['evidence_hashes'],
        'checks': [{'id': name, 'verdict': status, 'evidence': evidence} for name, status, evidence in checks],
        'actual_source_inspection_sha256': file_sha256(inspection_path),
        'final_result_sha256': file_sha256(result_path),
        'scope': 'Actual assistant source/trace review; not external human attestation or independent image acceptance.'}
    path = out/'svg-baseline-02-completed-source-review.json'
    canonical = session/'source_review.json'
    receipt = out/'svg-baseline-02-source-review-publication.json'
    assert not any(p.exists() for p in (path, canonical, receipt))
    write_json(path, verdict)
    if protected:
        assert review.validate_source_review(path, session, candidate_sha256=digest(hashes))['status'] == 'pass'
    canonical.write_bytes(path.read_bytes())
    write_json(receipt, {'status': 'published', 'native': native['status'],
        'source_review': verdict['status'], 'source_review_sha256': file_sha256(path),
        'numeric_passed': result['passed'], 'numeric_required': result['required'],
        'primary_outcome_modified': False, 'independent_visual_review': 'pending',
        'producer_sha256': file_sha256(__file__)})
    print({'native': native['status'], 'source': verdict['status'],
           'numeric': f"{result['passed']}/{result['required']}", 'visual': 'pending'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-inspection', action='store_true')
    run(parser.parse_args().record_inspection)
