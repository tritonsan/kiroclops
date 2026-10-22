"""Close the review/repair work after both reports and production tests exist.

This is documentation only. It never turns an old failed trial into a success.
"""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256


def main():
    base = ROOT/'artifacts/phase3a'
    closure = base/'completion-20260927'
    receipt = closure/'closure.json'
    assert not receipt.exists(), 'Preserve previous closure history'
    primary = base/'completion-original-cohort-report-01/cohort-report.json'
    posthoc = base/'completion-posthoc-compatibility-report-01/result.json'
    tests_path = base/'completion-applied-repairs-regression-01/tests.json'
    application_path = base/'completion-postcohort-repairs-01/application.json'
    presentation_path = base/'completion-report-presentation-check-01/result.json'
    presentation_review = presentation_path.with_name('visual-review.json')
    pipeline = base/'completion-frozen-report-pipeline-01/pipeline.json'
    repair_pipeline = base/'completion-repair-application-pipeline-01/pipeline.json'
    repair_retry = base/'completion-repair-regression-retry-01/retry.json'
    verifier = ROOT/'scripts/verify_applied_postcohort_repairs.py'
    registration = base/'completion-paired-cohort-01/registration.json'
    progress = registration.with_name('progress.json')
    a, b, tests, application = map(read_json, (primary, posthoc, tests_path, application_path))
    assert read_json(pipeline)['status'] == 'reports_completed_review_required'
    repair_state = read_json(repair_pipeline)
    assert repair_state['status'] == 'regression_failed_retained'
    assert next(row for row in repair_state['rows'] if row['id'] == 'apply')['exit_code'] == 0
    retry = read_json(repair_retry)
    assert retry['status'] == 'pass' and retry['exit_code'] == 0
    assert retry['tests_started_in_failed_attempt'] is False
    assert retry['production_modified_by_retry'] is False
    assert retry['failed_pipeline_sha256'] == file_sha256(repair_pipeline)
    assert retry['failed_log_sha256'] == file_sha256(repair_pipeline.with_name('regression.log'))
    assert retry['verifier_sha256'] == file_sha256(verifier)
    assert retry['source_before_sha256'] == file_sha256(repair_retry.with_name('original-verifier.py'))
    assert retry['tests_sha256'] == file_sha256(tests_path)
    assert retry['application_sha256'] == file_sha256(application_path)
    assert b['status'] == 'completed' and b['primary_outcomes_modified'] is False
    assert tests['status'] == 'pass' and tests['failures'] == tests['errors'] == 0
    assert tests['source_unchanged_during_tests'] is True
    assert tests['application_sha256'] == file_sha256(application_path)
    qa = read_json(presentation_path)
    assert qa['status'] == 'pass'
    for name, digest in qa['input_hashes'].items():
        assert file_sha256(ROOT/name) == digest
    qa_review = read_json(presentation_review)
    assert qa_review['status'] == 'pass' and qa_review['external_human_review'] is False
    for name, digest in qa_review['inputs'].items():
        assert file_sha256(presentation_path.parent/name) == digest
    assert application['primary_report_sha256'] == file_sha256(primary)
    assert application['posthoc_report_sha256'] == file_sha256(posthoc)
    assert a['registration_sha256'] == b['registration_sha256'] == file_sha256(registration)
    assert a['progress_sha256'] == b['progress_sha256'] == file_sha256(progress)
    assert len(a['rows']) == len(b['rows']) == 18
    assert Counter(r['status'] for r in a['rows']) == {'completed': 3, 'failed': 15}
    assert len(application['visual_reviews']) == 12
    for review in application['visual_reviews']:
        assert file_sha256(review['path']) == review['sha256']
        assert read_json(review['path'])['status'] == 'pass'
    for change in application['files']:
        assert file_sha256(ROOT/change['target']) == change['proposed_sha256']
        backup = application_path.parent/'original-sources'/change['target']
        assert file_sha256(backup) == change['original_sha256']
    frozen_archive = base/'completion-frozen-protocol-01/protocol-source.zip'
    assert file_sha256(frozen_archive) == '154b29606fe58b6741d69aac8c747c330f23686dabb43f35ae6302e2483dc3f4'
    handoff = base/'completion-remaining-pixel-aids-01/completed-handoff.json'
    assert read_json(handoff)['status'] == 'completed_via_explicit_handoff'
    inputs = {str(p.relative_to(ROOT)): file_sha256(p) for p in
              (Path(__file__), primary, posthoc, tests_path, application_path, presentation_path, presentation_review,
               pipeline, repair_pipeline, repair_retry, verifier, registration, progress, handoff, frozen_archive)}
    for row in read_json(progress)['rows']:
        for stage in row['stages']:
            state = Path(stage['session'])/'session.json'
            if state.exists():
                assert not read_json(state).get('active_job_id')

    passed = tests['tests_run']-tests['skipped']
    test_text = f"**{tests['tests_run']} tests: {passed} passed, {tests['skipped']} skipped; no errors.**"
    current = f"""## Latest status — September 28, review and repair work closed

- **The report on the 18 runs and the independent image review of the 12 usable final candidates are complete.** The reviewer is the Codex assistant; no external human approval is claimed.
- The original results were preserved as **3 completed / 15 failed**. Six recording failures, three tool-auditor incompatibilities and six behavior/preservation failures are shown separately. **18/18 model conversations ended normally with Opus 5.5/max.**
- In the separate compatibility review, three tool-assisted game jumps and two SVG candidates passed all relevant checks. One SVG candidate passed with normal Kiro. Because the horizontal continuation stage of the games did not run, these are not counted as completed game chains.
- The verified repairs in four production files were applied: native tool schema, model/worker status separation, recording-policy reading and the 60 Hz frame-duration calculation. {test_text}
- In the recorder diagnosis, **14/14 recordings** passed the timing limits; the old failure could not be reproduced. It is not claimed that the root cause or general recording reliability has been resolved. The sampling and acceptance thresholds were not changed.
- **There is no full three-domain Phase 3A acceptance; general KiroClops superiority is not proven.** The next engineering work is recording reliability, game scale/panel input timing and the unfinished game chain. The old failed runs will not be replaced with new ones.

[Closure and limitations](artifacts/phase3a/completion-20260927/REPORT.md) ·
[Visual report of the 18 runs](artifacts/phase3a/completion-original-cohort-report-01/index.html) ·
[Separate compatibility review](artifacts/phase3a/completion-posthoc-compatibility-report-01/REPORT.md).

The sections below describe their own historical state.

"""
    doc_paths = [ROOT/name for name in ('README.md', 'ROADMAP.md', 'PHASE3A_STATUS.md')]
    previous = {str(p.relative_to(ROOT)): file_sha256(p) for p in doc_paths}
    for path in doc_paths:
        text = path.read_text(encoding='utf-8')
        start = text.index('## Latest status — September 28, 18 runs finished')
        marker = 'The sections below describe their own historical state.'
        end = text.index(marker, start)+len(marker)
        path.write_text(text[:start]+current+text[end:].lstrip(), encoding='utf-8')

    report_path = closure/'REPORT.md'
    previous[str(report_path.relative_to(ROOT))] = file_sha256(report_path)
    old_report = report_path.read_text(encoding='utf-8')
    report = f"""# KiroClops — review, comparison and repair closeout

September 28, 2026. **This review and repair work is complete. The three-domain acceptance of Phase 3A is not complete.**

## Completed work

- 18 original Kiro runs were reported; the independent visual review of the 12 candidates with a complete final recording was verified and bound to the correct source version.
- The reviews were done by the Codex assistant using raw images and pixel aids; no external human review or manual clicking of every point is claimed.
- The last two SVG packages were completed with 6,639 and 6,655 positions, 97 review groups each and 324 image sheets each. 24 and 27 connection endpoints were additionally reviewed from mixed edge pixels. The tolerances were not changed.
- First the original report, then an explicitly separate compatibility report were produced. Only after these were closed was the fix in four production files applied; the original files were backed up.
- {test_text} No new model run or substitute attempt replacing a failed run was made.
- The first regression launcher stopped before reaching the tests because of Windows path separators. The same test suite was run after converting the file keys to a portable form; the first error log and the earlier version of the launcher are preserved in [a separate retry record](../completion-repair-regression-retry-01/retry.json). This is not a new Kiro run or a production behavior change.
- Two checks were skipped: the separately enabled headless browser integration, and a check that requires the native Heart qualification evidence, which is not in the test suite. The separate real-recording and report-playback audits are reported with their own evidence; these skips were not counted as passing tests.

## How should the result be read?

The original record stands as **3 completed / 15 failed**: tool-assisted arm 2/9, normal Kiro 1/9. 18/18 model conversations ended normally with Opus 5.5/max and native Always Allow. Of the failures, six are recording/timing, three are native auditor incompatibilities and six are real motion/preservation mismatches. A recording failure does not mean that the model crashed or that the candidate's motion failed.

The separate compatibility review accepts the **jump sub-stage** of three tool-assisted games and two tool-assisted SVG candidates. One SVG candidate of normal Kiro passes. However, the subsequent horizontal stage of the games never ran; for this reason we cannot write the total as six fully successful runs.

In the subset with a complete final recording, there is a numerical success of 5/5 for the tool-assisted arm and 1/7 for normal Kiro. These conditional rates are not evidence of general superiority because of missing recordings and game scale/input timing effects. The successful SVG repeat of normal Kiro, with a single candidate, produced a lower error and a shorter time than the matched tool-assisted repeat. No general success claim was drawn from the small sample.

## Applied fixes

1. The `Delete File / targetFile` call and **blocked** calls that request an external working directory are recognized by an exact evidence match; the blocking conditions are preserved.
2. A model conversation that ended normally is separated from a recording/validation worker that fails afterwards.
3. Browser image evidence is read according to the recording policy fixed in the experiment.
4. Verified 60 Hz jump durations are compared by frame count; the wrong rejection of an exact two-frame difference due to floating-point rounding was fixed.

The old numerical results were not rewritten. The reference scale, sampling rate and acceptance thresholds did not change. All 14/14 recordings of the diagnostic set passed, but since the original stalls could not be reproduced, the root cause of the recorder is still open.

The working notes in the original report preserve the state at the time they were written; later visual review statuses are in the linked final audit results. The cost of the repeated file verification at report closure is not Kiro's model/tool time.

## Open engineering work

- Reproducibly catch the recording failure under the same load and input conditions; fix the source of frame/input loss with evidence.
- Clarify the game body scale and the real input timing of the panel for a fair comparison.
- In a separate, pre-fixed next study, complete the game jump→horizontal chain and the interrupted motion of the panel; preserve the existing 18 failed/successful records.
- Reduce the cost of repeated evidence reads while preserving the candidate/image identity check, and make the progress of long verifications visible.

## Evidence

- [Original 18 runs, videos and code diffs](../completion-original-cohort-report-01/index.html)
- [Separate compatibility interpretation](../completion-posthoc-compatibility-report-01/REPORT.md)
- [Application and source backups](../completion-postcohort-repairs-01/application.json)
- [Production regression result](../completion-applied-repairs-regression-01/tests.json)
- [Serverless report, image and playback audit](../completion-report-presentation-check-01/result.json)
- [Recorder diagnosis](../completion-recorder-diagnostic-interpretation-01/REPORT.md)
- [Machine-readable closure](closure.json)

## Earlier working notes — historical record

The content below preserves the state at the time it was written; the current state is above.

"""
    report_path.write_text(report+old_report, encoding='utf-8')
    work_path = closure/'work.json'
    work = read_json(work_path)
    previous[str(work_path.relative_to(ROOT))] = file_sha256(work_path)
    work['historical_active_jobs_at_closure'] = work.get('active_jobs', {})
    work.update(status='completed_with_unmet_phase_acceptance', active_jobs={},
                production_frozen_for_new_qualification=False,
                updated_at=datetime.now(timezone.utc).timestamp())
    work['current_closure'] = dict(independent_reviews='12/12 pass',
        original_trials={'completed':3,'failed':15}, production_repairs='applied',
        regression={k:tests[k] for k in ('status','tests_run','failures','errors','skipped')},
        phase_acceptance='not_met', incremental_benefit='not_established',
        new_model_run=False, primary_outcomes_modified=False)
    write_json(work_path, work)
    live_path = closure/'live-closure-jobs.json'
    live = read_json(live_path)
    previous[str(live_path.relative_to(ROOT))] = file_sha256(live_path)
    live['historical_sessions'] = live.get('sessions', {})
    live.update(status='completed_with_unmet_phase_acceptance', sessions={},
                completed_at=datetime.now(timezone.utc).isoformat(),
                next='Review REPORT.md for completed work and remaining phase acceptance conditions.')
    write_json(live_path, live)
    outputs = {str(p.relative_to(ROOT)): file_sha256(p) for p in [*doc_paths,report_path,work_path,live_path]}
    write_json(receipt, dict(status='completed_with_unmet_phase_acceptance',
        created_at=datetime.now(timezone.utc).isoformat(), inputs=inputs,
        documentation_before=previous, documentation_after=outputs,
        original_trials={'completed':3,'failed':15}, independent_reviews=12,
        production_repairs=len(application['files']), regression=work['current_closure']['regression'],
        phase_acceptance='not_met', incremental_benefit='not_established',
        external_human_review=False, new_model_run=False, primary_outcomes_modified=False))
    print({'status':'completed_with_unmet_phase_acceptance','reviews':12,'tests':tests['tests_run']})


if __name__ == '__main__':
    main()
