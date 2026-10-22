"""Run the authorized, preregistered cohort only after both current gates pass.

This launcher supplies no solution edits or live model feedback. It never
retries a failed trial and preserves all qualification and comparison evidence.
"""
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256
from kiroclops.local_engine.qualification import combine_manifests
from kiroclops.local_engine.cohort import register, run_cohort


def run():
    base = ROOT / 'artifacts/phase3a'
    receipt = base / 'completion-20260927/paired-pipeline.json'
    cohort = base / 'completion-paired-cohort-01'
    combined = base / 'completion-qualification-03/manifest.json'
    review = base / 'completion-web-qualification-review-03'
    reference_pipeline = base / 'completion-20260927/reference-pipeline.json'
    references = base / 'completion-shared-references-03/manifest.json'
    gate = base / 'completion-reference-observability-02/result.json'
    if receipt.exists() or cohort.exists() or combined.exists():
        raise ValueError('Fresh destinations required; never replace prior evidence')
    state = {'status': 'waiting_for_acceptance_gates', 'model_started': False,
             'model': 'claude-opus-5.5', 'effort': 'max', 'headless_only': True,
             'started_at': time.time()}

    def save(**values):
        state.update(values)
        write_json(receipt, state)

    save()
    try:
        deadline = time.monotonic() + 7200
        while True:
            refs = read_json(reference_pipeline)
            if refs['status'] in ('failed', 'interrupted'):
                raise ValueError('Reference gate failed; no model trial will start')
            binding = review / 'binding-result.json'
            if binding.exists() and read_json(binding).get('status') != 'pass':
                raise ValueError('Independent review failed; no model trial will start')
            if refs.get('finished_at') and binding.exists():
                assert refs['status'] == 'automated_gates_pass_independent_review_pending'
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Acceptance gates did not complete within two hours')
            time.sleep(5)
        save(status='combining_qualified_evidence',
             web_review_sha256=file_sha256(binding), reference_gate_sha256=file_sha256(gate))
        components = [base / 'completion-game-qualification-03/manifest-reviewed.json',
                      review / 'manifest-reviewed.json',
                      base / 'completion-error-qualification-03/reviewed-manifest.json']
        combined.parent.mkdir(parents=True, exist_ok=False)
        combine_manifests(components, combined)
        assert read_json(combined)['status'] == 'pass'
        save(status='preregistering', qualification_sha256=file_sha256(combined))
        print('24-control qualification passed; preregistering all18 trials', flush=True)
        registration = register(cohort, combined, web_references=references, reference_gate=gate)
        assert registration['trials'] == 18
        assert (registration['model'], registration['effort']) == ('claude-opus-5.5', 'max')
        save(status='running_preregistered_cohort', model_started=True,
             registration=str(cohort/'registration.json'),
             registration_sha256=file_sha256(cohort/'registration.json'))
        print('Registered18 trials; starting native CLI with no operator solution or coaching', flush=True)
        result = run_cohort(cohort/'registration.json')
        save(status='cohort_execution_finished_review_required', summary_sha256=file_sha256(cohort/'summary.json'),
             result_status=result.get('status'), finished_at=time.time())
        print(state['status'], flush=True)
    except BaseException as exc:
        save(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'blocked',
             error={'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)},
             finished_at=time.time())
        raise


if __name__ == '__main__':
    run()
