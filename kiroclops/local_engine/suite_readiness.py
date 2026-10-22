"""Image-bound pre-prompt admission, separate from final behavior acceptance."""
from pathlib import Path

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, safe_path, write_json
from .media_suite import load_suite, record_from_suite, runtime_environment
from .session import Session


def _not_ready(message):
    raise VisualError('environment_not_ready', message)


def _warmups(path, suite=None, authoring_parent=None):
    path=Path(path);value=read_json(path)
    policy=value.get('policy') or {}
    attempts=value.get('attempts') or []
    if (value.get('kind')!='LocalWarmupReadiness' or value.get('status')!='pass'
            or value.get('evaluated') is not False or value.get('model_started') is not False
            or policy.get('maximum_attempts')!=6 or policy.get('required_consecutive_clean')!=2
            or not 2<=len(attempts)<=6 or value.get('consecutive_clean')!=2):
        _not_ready('Warmup qualification receipt is incomplete or uses another policy.')
    for index,attempt in enumerate(attempts,1):
        if (attempt.get('attempt')!=index
                or read_json(path.parent/f'attempt-{index:02}-result.json')!=attempt):
            _not_ready('Warmup attempt identity changed.')
        if index>len(attempts)-2:
            result=attempt.get('capture_result') or {};timing=result.get('timing') or {}
            actual=read_json(Path(attempt['directory'])/'record.json')
            from kiroclops.visual.package import load_package
            package=load_package(actual['package'],verify='hashes')
            if (attempt.get('status')!='pass' or result.get('status') not in ('pass','ok')
                    or timing.get('status')!='pass' or timing.get('errors',[])!=[]
                    or result.get('record_sha256')!=digest(actual)
                    or actual.get('measurement_status')!='ok'):
                _not_ready('Two consecutive real, unchanged clean captures are required.')
            if suite is not None:
                from .suite_evidence import _source_expectation
                from .evidence import _record_source
                if (actual.get('source_sha256')!=digest(suite['authoring_parent_source_hashes'])
                        or not any(digest(actual.get('scenario'))==digest(c['scenario'])
                                   for c in suite['cases'] if c['role']=='calibration')):
                    _not_ready('Clean warmups must use this stage recipe and exact authoring parent.')
                expected=_source_expectation(Path(authoring_parent),suite['authoring_parent_source_hashes'],suite['domain'])
                _record_source(actual,package,expected)
    return value


def _raw_review(path, suite_directory, suite):
    if suite.get('schema_version') == 2 and suite.get('stage') in ('panel', 'svg'):
        from .realistic_review import validate_review, calibration_review
        from .realistic_policy import build_policy
        from .evidence import observations
        from kiroclops.visual.package import load_package
        path=Path(path)
        result=validate_review(path)
        if result.get('phase')!='calibration':
            _not_ready('Only calibration pixel review may admit a prospective model stage.')
        from . import realistic_policy_v3
        frozen=suite.get('acceptance_policy')
        if realistic_policy_v3.is_v3(frozen):
            # V3 also re-measures every reference r1/r2 retest floor from the
            # suite's own frozen recordings; nothing is copied from the policy.
            references={}
            for case in suite['cases']:
                record=record_from_suite(suite_directory,case)
                selection=record.get('selection')
                if isinstance(selection,(str,Path)):selection=read_json(selection)
                references[case['case_id']]={'scenario':record['scenario'],'observations':observations(record),
                                             'selection':selection}
            actual_policy=realistic_policy_v3.build_policy(suite['stage'],calibration_review(path),references)
        else:
            actual_policy=build_policy(suite['stage'],calibration_review(path))
        if actual_policy != frozen:
            _not_ready('Frozen pixel floors do not come from this complete inspected calibration receipt.')
        floors=(actual_policy['base_policy']['floors_px'] if realistic_policy_v3.is_v3(actual_policy)
                else actual_policy['floors_px'])
        expected_initial={(role,c['case_id']) for c in suite['cases'] if c['role']=='calibration'
                          for role in ('original','before')}
        initial=suite.get('initial_records',[])
        if {(c['role'],c['case_id']) for c in initial} != expected_initial:
            _not_ready('Both exact calibration starting sources require raw review.')
        required=set()
        object_ids=set()
        for case in suite['cases']:
            if case['role'] != 'calibration':continue
            record=record_from_suite(suite_directory,case)
            observed=observations(record)
            object_ids.update(obj['id'] for obj in observed['objects'])
            required.add(('reference',case['case_id'],case['package_sha256'],digest(observed),record['source_sha256']))
        for case in initial:
            record=record_from_suite(suite_directory,case)
            required.add((case['role'],case['case_id'],load_package(record['package'],verify='hashes')['package_sha256'],
                          digest(observations(record)),record['source_sha256']))
        core=[r for r in result.get('records',[]) if r.get('purpose','primary')!='measurement_qualification']
        extra=[r for r in result.get('records',[]) if r.get('purpose')=='measurement_qualification']
        actual={(r['role'],r['case_id'],r['package_sha256'],r['observations_sha256'],r['source_sha256']) for r in core}
        if len(core) != len(required) or actual != required:
            _not_ready('Prospective raw review must cover every exact reference/original/before calibration record.')
        # Static six-node qualification can supply e/f pixel precision before
        # the model. It is never a behavioral scenario or held-out movement.
        if extra:
            groups={}
            core_ids={row[1] for row in required}
            for row in extra:
                if row.get('split_role')!='calibration' or row['case_id'] in core_ids:
                    _not_ready('Geometry-only qualification must be separate from behavioral/held-out cases.')
                groups.setdefault(row['case_id'],[]).append(row['role'])
            if any(sorted(roles)!=['before','original','reference'] for roles in groups.values()):
                _not_ready('Each geometry-only qualification needs exactly three independently bound roles.')
            indexed={row['id']:row for row in read_json(path/'records.json')}
            for row in extra:
                snapshot=indexed[row['record_id']]
                if (snapshot.get('purpose')!='measurement_qualification'
                        or snapshot.get('qualification_kind')!='static_geometry'
                        or snapshot.get('behavior_fit_eligible') is not False):
                    _not_ready('Extra calibration is admitted only as explicit static geometry, outside behavior fitting.')
                observed=observations(snapshot)
                if row['role']=='reference':object_ids.update(obj['id'] for obj in observed['objects'])
        if set(floors) != object_ids:
            _not_ready('Every selected reference object needs its own inspected calibration floor.')
        return {'directory':str(path.resolve()),'attestation_sha256':file_sha256(path/'attestation.json'),
                'result_sha256':digest(result),'required_record_count':len(required)+len(extra),
                'acceptance_profile':actual_policy.get('id','realistic-behavior-v1'),
                'acceptance_policy_sha256':actual_policy['policy_sha256'],
                'acceptance_policy_document_sha256':digest(actual_policy),
                'calibration_review_sha256':digest(calibration_review(path))}
    if suite.get('domain') in ('game', 'svg'):
        from .suite_generic_raw_review import validate_review
        return validate_review(path, suite_directory, suite)
    # The v3 admission sidecar is deliberately separate from accepting the
    # whole raw review. It cannot waive scored event or position failures.
    from . import suite_raw_review_v3
    if read_json(Path(path)/'result.json').get('version') == suite_raw_review_v3.VERSION:
        return suite_raw_review_v3.validate_admission(path, suite_directory, suite)
    from .evidence import observations
    from .suite_raw_review import _public_contract
    path=Path(path);result=read_json(path/'result.json')
    receipt=read_json(path/'evidence-receipt.json');attestation=read_json(path/'attestation.json')
    if (result.get('numeric_status')!='pass' or result.get('unsupported')
            or result.get('public_event_contract')!=_public_contract(suite.get('event_contract'))
            or attestation.get('kind')!='ProspectiveRawVisualReviewAttestation'
            or attestation.get('status')!='pass' or attestation.get('images_inspected') is not True
            or attestation.get('accepted') is not True
            or attestation.get('result_sha256')!=digest(result)
            or receipt.get('result_sha256')!=digest(result)
            or attestation.get('evidence_receipt_sha256')!=file_sha256(path/'evidence-receipt.json')
            or attestation.get('inspected_sheets')!=result.get('sheets') or not result.get('sheets')):
        _not_ready('Raw review is pending, failed or uses a different frozen event definition.')
    for name,expected in {**receipt['source_files'],**receipt['code_sources']}.items():
        if file_sha256(name)!=expected:_not_ready('Raw review source pixels, measurements or helper code changed.')
    for name,expected in receipt['generated_files'].items():
        if file_sha256(safe_path(path,name))!=expected:_not_ready('Raw contact sheet or independent result changed.')
    required=set()
    for case in suite['cases']:
        if case['role']=='calibration':
            record=record_from_suite(suite_directory,case)
            required.add(('reference',case['package_sha256'],digest(observations(record))))
    initial=suite.get('initial_records',[])
    for case in initial:
        record=record_from_suite(suite_directory,case)
        from kiroclops.visual.package import load_package
        required.add((case['role'],load_package(record['package'],verify='hashes')['package_sha256'],digest(observations(record))))
    expected_initial={(role,c['case_id']) for c in suite['cases'] if c['role']=='calibration' for role in ('original','before')}
    if {(c['role'],c['case_id']) for c in initial}!=expected_initial:
        _not_ready('Both independently bound initial calibration sources must be reviewed.')
    actual={(r['role'],r['package_sha256'],r['observations_sha256']) for r in result.get('records',[])}
    if actual!=required:_not_ready('Raw review does not bind every calibration reference and both starting measurements.')
    return {'directory':str(path.resolve()),'attestation_sha256':file_sha256(path/'attestation.json'),
            'result_sha256':digest(result),'required_record_count':len(required)}


def _measurements(suite_directory,suite):
    from .suite_revision import measurement_source_hashes,verify_measurement_record
    from kiroclops.cloud.identity import engine_identity
    stages={c['stage'] for c in suite['cases']}
    code={stage:measurement_source_hashes(stage) for stage in stages}
    engine=engine_identity()['engine_sha256']
    cases={c['case_id']:c for c in suite['cases']}
    for item in suite['cases']+suite.get('initial_records',[])+suite.get('baseline_records',[]):
        stage=cases[item['case_id']]['stage'];record=record_from_suite(suite_directory,item)
        verify_measurement_record(record,stage,expected_sources=code[stage],expected_engine_sha256=engine)
    expected={(role,c['case_id']) for c in suite['cases'] if c['role']!='calibration' for role in ('before','original')}
    if {(c['role'],c['case_id']) for c in suite.get('baseline_records',[])}!=expected:
        _not_ready('Freeze both private regression/validation baseline recordings before the model budget starts.')


def certify(directory, raw_review):
    """Create a new append-only admission receipt after actual visual inspection."""
    directory=Path(directory);state=Session(directory)._load();suite=load_suite(state['reference'])
    if state['started_at'] is not None or state['candidates']:
        _not_ready('Admission cannot be added after the model prompt.')
    readiness=read_json(directory/'environment-readiness.json')
    if readiness.get('measurement_revision_required') is True:
        _not_ready('Historical measurements still require a code/engine-bound revision.')
    _measurements(state['reference'],suite)
    runtime_environment(state['experiment'])
    warmup_path=(readiness.get('warmups') or {}).get('receipt_path') or readiness.get('warmup_receipt')
    _warmups(warmup_path,suite,state['baseline'])
    raw=_raw_review(raw_review,state['reference'],suite)
    receipt={'kind':'ReferenceSuiteAdmission','schema_version':1,'status':'pass',
        'suite_sha256':digest(suite),'warmup_receipt':str(warmup_path),
        'warmup_sha256':file_sha256(warmup_path),'raw_review':raw,'model_started':False,
        'behavior_acceptance_granted':False,'thresholds_changed':False}
    output=directory/'admission.json'
    if output.exists():raise VisualError('output_exists','Keep previous admission decisions unchanged.')
    write_json(output,receipt)
    return receipt


def validate(directory, suite):
    directory=Path(directory);state=Session(directory)._load();receipt=read_json(directory/'admission.json')
    if read_json(directory/'environment-readiness.json').get('measurement_revision_required') is True:
        _not_ready('The preparation has unstamped historical measurements.')
    _measurements(state['reference'],suite)
    if (receipt.get('kind')!='ReferenceSuiteAdmission' or receipt.get('status')!='pass'
            or receipt.get('suite_sha256')!=digest(suite)
            or receipt.get('model_started') is not False or receipt.get('behavior_acceptance_granted') is not False
            or receipt.get('warmup_sha256')!=file_sha256(receipt['warmup_receipt'])):
        _not_ready('The frozen admission receipt changed.')
    runtime_environment(state['experiment']);_warmups(receipt['warmup_receipt'],suite,state['baseline'])
    if _raw_review(receipt['raw_review']['directory'],state['reference'],suite)!=receipt['raw_review']:
        _not_ready('Calibration visual review no longer belongs to this suite.')
    return receipt
