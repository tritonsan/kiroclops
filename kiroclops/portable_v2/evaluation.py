"""Seven-tool job implementation: capture/measure/check, never write movement code."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import shutil
import re
import time
from kiroclops.integration.storage import read_json, write_json, project_hashes, sha256, IntegrationError
from .specs import CONFIG, scenarios, run_name, digest
from . import measurement


def _stage(state):
    return state['experiment_spec']['stage']


def _source(path):
    return project_hashes(Path(path))


def _calibration_media(directory, references, baseline_runs):
    """Export only this stage's calibration pixels inside Kiro's allowed workspace."""
    destination = directory/'target/.kiroclops-reference'
    index = {'scope': 'calibration only; native rendered pixels', 'runs': []}
    for stage, paths in references.items():
        for role, group in (('reference',paths), ('before',baseline_runs[stage])):
            for source_path in group:
                data=read_json(source_path)
                if data['scenario']['split'] != 'calibration':
                    raise IntegrationError('wrong_split','Cannot export held-out evidence into a model workspace')
                if data['scenario']['repeat'] != 1:
                    continue
                name=run_name(dict(data['scenario'],id=data['scenario'].get('id') or f"h{data['scenario']['hold_ticks']:02}"))
                target=destination/stage/role/name
                target.mkdir(parents=True,exist_ok=False)
                (target/'frames').mkdir()
                source=Path(data['run_id'])
                hashes={}
                for i in range(240):
                    rel=f'frames/{i:06d}.png'
                    shutil.copy2(source/rel,target/rel); hashes[rel]=sha256(target/rel)
                safe={k:v for k,v in data.items() if k not in ('run_id','provenance','annotation_validation')}
                write_json(target/'measurement.json',safe)
                index['runs'].append({'mechanic':stage,'role':role,'path':str(target),'scenario':data['scenario'],
                                      'body_height_px':data['body_height_px'],'raw_frame_hashes':hashes})
    write_json(destination/'index.json',index)
    return str(destination/'index.json')


def _matrix(project, stage, split, output, variant, *, cancel, check, progress):
    from .runtime import capture_project, import_project
    output = Path(output)
    sources = _source(project)
    check()
    import_project(Path(project),output/'import',cancel=cancel)
    if _source(project)!=sources:
        raise IntegrationError('source_changed','Import changed immutable candidate sources')
    results = []
    for i, scenario in enumerate(scenarios(stage, split)):
        check()
        progress(f'{stage} {split}: {run_name(scenario)}', i, len(scenarios(stage, split)))
        run = output / run_name(scenario)
        if run.exists():
            raise IntegrationError('output_exists', 'Fresh capture output required; incomplete observations cannot be reused')
        actual = {k:v for k,v in scenario.items() if k not in ('mechanic', 'fps')}
        actual['variant'] = variant
        capture_project(Path(project), run, actual, cancel=cancel)
        if _source(project) != sources:
            raise IntegrationError('source_changed', 'Candidate source changed during capture')
        measurement.analyze(run, stage)
        results.append(str(run/'analysis.json'))
    return results


def _refs(directory, stage, split, *, check):
    # Separate directories per job prevent a later job overwriting earlier evidence.
    return measurement.reference_runs(stage, split, Path(directory)/stage/split, check)


def _public_integrity(result):
    # Engine quantities, seed values and backend paths never reach profile fitting or Kiro.
    result=deepcopy(result)
    if result.get('mismatches'):
        grouped={}
        for mismatch in result['mismatches']:
            case=mismatch.get('case_id','unknown')
            item=grouped.setdefault(case,{'case_id':case,'mismatch_count':0,'fields':set(),'checks':set(),'steps':[]})
            item['mismatch_count']+=1
            item['fields'].update(mismatch.get('fields',[]))
            if mismatch.get('check'): item['checks'].add(mismatch['check'])
            if type(mismatch.get('step')) is int: item['steps'].append(mismatch['step'])
        result['mismatches']=[{'case_id':v['case_id'],'mismatch_count':v['mismatch_count'],
            'fields':sorted(v['fields']),'checks':sorted(v['checks']),
            'step_range':[min(v['steps']),max(v['steps'])] if v['steps'] else None} for v in grouped.values()]
    def compact(value):
        if isinstance(value, dict):
            return {k: compact(v) for k,v in value.items() if k in {
                'status', 'pass', 'name', 'case_id', 'id', 'reason', 'error', 'code', 'message',
                'cases', 'checks', 'failures', 'mismatches', 'expected_behavior', 'actual_behavior',
                'mismatch_count', 'failed_cases', 'case_count', 'air_cases', 'air_steps',
                'step', 'step_range', 'fields', 'check', 'passed', 'required', 'scope', 'stage', 'summary'}}
        if isinstance(value, list):
            return [compact(v) for v in value]
        return value
    return compact(result)


def _integrity(project, baseline, output, stage, *, cancel, check):
    from .integrity import run_checks
    check()
    result = run_checks(Path(project), Path(baseline), Path(output), stage, cancel=cancel)
    check()
    if result.get('status') not in ('pass', 'fail'):
        lines=[]
        for log in sorted((Path(output)/'candidate').rglob('engine.log')):
            for line in log.read_text(encoding='utf-8',errors='replace').splitlines():
                if re.search(r'SCRIPT ERROR|Parse Error|Compile Error|res://.*\.gd:',line,re.I):
                    # Keep Godot res:// file/line diagnostics. Private absolute backend paths
                    # and raw preservation telemetry do not belong in model feedback.
                    line=re.sub(r'(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\"\']+', '<private-path>',line)
                    lines.append(line[:240])
        if lines:
            raise IntegrationError('candidate_compilation_error','Candidate GDScript failed: '+' | '.join(lines[:12]))
        raise IntegrationError('integrity_tool_failure',f"Preservation fixture did not produce valid observations ({result.get('code','unknown')}): {result.get('message','runtime failure')}")
    write_json(Path(output)/'comparison.json', result)
    return result


def _original(state, directory):
    if _stage(state) == 'jump':
        return Path(directory)/'baseline'
    original = state.get('original_baseline') or {}
    path = Path(original.get('project', ''))
    if not path.is_dir() or not original.get('source_hashes') or _source(path) != original['source_hashes']:
        raise IntegrationError('parent_integrity', 'Original Heart jump improvement baseline is missing or changed')
    return path


def _prepare(directory, job, state, *, cancel, check, progress):
    stage = _stage(state)
    output = directory/'measurements'/job['job_id']
    progress('Recomputing reference profile from calibration pixels')
    refs = _refs(output/'reference', stage, 'calibration', check=check)
    profile = measurement.fit_reference(stage, refs, output/'profile.json')
    baseline = _matrix(directory/'baseline', stage, 'calibration', output/'before', 'before',
                       cancel=cancel, check=check, progress=progress)
    preservation = _integrity(directory/'baseline', directory/'baseline', output/'integrity', stage,
                              cancel=cancel, check=check)
    if preservation['status'] != 'pass':
        raise IntegrationError('baseline_integrity_failure', 'The clean baseline failed preservation qualification')
    references = {stage: refs}
    baseline_runs = {stage: baseline}
    if stage == 'ground':
        references['jump'] = _refs(output/'reference', 'jump', 'calibration', check=check)
        baseline_runs['jump'] = _matrix(directory/'baseline', 'jump', 'calibration', output/'parent-jump', 'before',
                                       cancel=cancel, check=check, progress=progress)
        # Parent preserves the transferred jump; report these reference comparisons without
        # requiring another 50% gain against an already improved parent.
    annotation = read_json(CONFIG/f'heart-{stage}-annotation.json')
    media = _calibration_media(directory, references, baseline_runs)
    comparison = measurement.compare_motion(stage, refs, baseline, baseline, 'calibration', baseline_diagnostic=True)
    clean_profile = {k:v for k,v in profile.items() if k != 'provenance'}
    return {'reference': references, 'baseline_runs': baseline_runs,
            'reference_profile': clean_profile, 'profile_path': str(output/'profile.json'),
            'profile_sha256': sha256(output/'profile.json'),
            'target_body_height_px': annotation['body_height_px'],
            'calibration_media_index': media,
            'baseline_comparison': measurement.public_comparison(comparison),
            'motion_diagnostics': measurement.diagnostics(stage, refs, baseline, profile),
            'automatic_integrity_status': 'pass', 'integrity': _public_integrity(preservation),
            'measurement_status': 'pending', 'units': 'body heights, seconds; fixed 60 physics steps per second',
            'runtime_budget_hint': read_json(CONFIG/'qualification.json').get('runtime_budget_hint', {}) if (CONFIG/'qualification.json').is_file() else {}}


def _evaluate(directory, job, state, *, cancel, check, progress):
    stage = _stage(state)
    candidate = read_json(directory/'candidates'/job['candidate_id']/'candidate.json')
    project = Path(candidate['project_path'])
    output = directory/'measurements'/job['job_id']
    preservation = _integrity(project, directory/'baseline', output/'integrity', stage, cancel=cancel, check=check)
    if preservation['status'] != 'pass':
        return {'eligible': False, 'mean_rmse_body': None, 'automatic_integrity_status': 'fail',
                'failure_class': 'gameplay_integrity_violation', 'integrity': _public_integrity(preservation),
                'motion_calibration': 'not_run', 'jump_calibration': 'not_run', 'measurement_status': 'pending'}
    after = _matrix(project, stage, 'calibration', output/'after', 'after', cancel=cancel, check=check, progress=progress)
    reference, before = state['reference'][stage], state['baseline_runs'][stage]
    comparison = measurement.compare_motion(stage, reference, before, after, 'calibration')
    jump = None
    jump_runs = None
    if stage == 'ground':
        jump_runs = _matrix(project, 'jump', 'calibration', output/'after-jump', 'after',
                            cancel=cancel, check=check, progress=progress)
        jump = measurement.compare_motion('jump', state['reference']['jump'], state['baseline_runs']['jump'], jump_runs, 'calibration')
    profile = read_json(state['preparation']['profile_path'])
    result = {'eligible': comparison['pass'] and (jump is None or jump['pass']),
              'mean_rmse_body': comparison['mean_rmse_body'], 'automatic_integrity_status': 'pass',
              'integrity': _public_integrity(preservation), 'calibration': measurement.public_comparison(comparison),
              'jump_preservation': measurement.public_comparison(jump) if jump else 'not_applicable',
              'calibration_runs': after, 'jump_calibration_runs': jump_runs,
              'motion_diagnostics': measurement.diagnostics(stage, reference, after, profile),
              'measurement_status': 'pending', 'candidate_source_sha256': candidate['source_sha256']}
    result['failure_class'] = None if result['eligible'] else 'motion_mismatch'
    write_json(output/'comparison.json', result)
    return result


def _finalize(directory, job, state, *, cancel, check, progress):
    if not state.get('sealed_at') or state.get('selected_candidate_id') != job['candidate_id']:
        raise IntegrationError('validation_unsealed', 'Hold-outs require a sealed candidate')
    stage = _stage(state)
    candidate = read_json(directory/'candidates'/job['candidate_id']/'candidate.json')
    project = Path(candidate['project_path'])
    output = directory/'final'
    preservation = _integrity(project, directory/'baseline', output/'integrity', stage, cancel=cancel, check=check)
    if preservation['status'] != 'pass':
        return {'numerical_status': 'not_run', 'automatic_integrity_status': 'fail',
                'measurement_status': 'pending', 'integrity': _public_integrity(preservation)}
    reports = {}
    comparisons = {}
    for mechanic in (('jump',) if stage == 'jump' else ('ground', 'jump')):
        refs = _refs(output/'reference', mechanic, 'validation', check=check)
        baseline_project = _original(state, directory) if mechanic == 'jump' else directory/'baseline'
        before = _matrix(baseline_project, mechanic, 'validation', output/mechanic/'before', 'before',
                         cancel=cancel, check=check, progress=progress)
        after = _matrix(project, mechanic, 'validation', output/mechanic/'after', 'after',
                        cancel=cancel, check=check, progress=progress)
        comparisons[mechanic] = measurement.compare_motion(mechanic, refs, before, after, 'validation')
        reports[mechanic] = {'reference': refs, 'before': before, 'after': after}
    # The same code snapshot is run in the real level, not a replacement controller.
    from .runtime import capture_project
    normal_scenario = {'id': 'normal-combined', 'split': 'smoke', 'repeat': 1, 'spawn_offset': 0,
        'warmup_ticks': 60, 'frame_count': 240, 'variant': 'after',
        'events': [{'tick': 30, 'action': 'right', 'pressed': True},
                   {'tick': 36, 'action': 'jump', 'pressed': True},
                   {'tick': 60, 'action': 'jump', 'pressed': False},
                   {'tick': 78, 'action': 'right', 'pressed': False}]}
    check()
    normal = capture_project(project, output/'normal', normal_scenario, normal_level=True, cancel=cancel)
    slope_scenario=dict(normal_scenario,id='normal-slope',events=[e for e in normal_scenario['events'] if e['action']!='jump'])
    check()
    slope=capture_project(project,output/'slope',slope_scenario,normal_level=True,cancel=cancel)
    normal_status = 'pass' if normal.get('gameplay_status') == slope.get('gameplay_status') == 'pass' else 'fail'
    numerics = all(value['pass'] for value in comparisons.values())
    record = {'schema_version': 5, 'session_id': directory.name, 'candidate_id': job['candidate_id'],
              'source_sha256': candidate['source_sha256'], 'comparisons': comparisons, 'runs': reports,
              'normal_capture': str(output/'normal'), 'slope_capture':str(output/'slope'), 'normal_gameplay_status': normal_status,
              'numerical_status': 'pass' if numerics else 'fail',
              'automatic_integrity_status': 'pass' if normal_status == 'pass' else 'fail',
              'measurement_status': 'pending', 'source_review_status': 'pending', 'status': 'pending',
              'integrity': _public_integrity(preservation)}
    write_json(output/'comparison.json', record)
    return {'numerical_status': record['numerical_status'], 'automatic_integrity_status': record['automatic_integrity_status'],
            'measurement_status': 'pending', 'source_review_status': 'pending',
            'comparison_path': str(output/'comparison.json'), 'candidate_source_sha256': candidate['source_sha256'],
            'comparisons': {k: measurement.public_comparison(v) for k,v in comparisons.items()},
            'normal_gameplay_status': normal_status, 'integrity': record['integrity']}


def run_job(directory, job_id, *, cancel, check, progress):
    directory = Path(directory)
    state = read_json(directory/'session.json')
    job = read_json(directory/'jobs'/f'{job_id}.json')
    if job['action'] == 'report':
        from .reporting import build_session_report
        return {'report_path': str(build_session_report(directory))}
    function = {'prepare': _prepare, 'evaluate': _evaluate, 'finalize': _finalize}[job['action']]
    return function(directory, job, state, cancel=cancel, check=check, progress=progress)
