"""Reuse preregistered reference measurements; never cache a model candidate.

A complete passing32-record gate is established by cohort registration. This
reader validates that immutable gate once per matrix, then opens measurements
only for the requested split. Baseline calibration copies raw images only.
There is no process-global validation cache and no fallback from stale evidence.
"""
from copy import deepcopy
from pathlib import Path
import shutil

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from kiroclops.visual.package import copy_package, load_package

ROOT=Path(__file__).resolve().parents[2]
CACHE_FIELD='web_reference_cache'
MEASUREMENT_CONTRACT={'min_valid_ratio':.95,'require_static_camera':True,
                      'event_options':{'connection_tolerance_px':2.,'rest_duration_s':.25}}


def _require(condition,message,code='reference_cache_changed'):
    if not condition:raise VisualError(code,message)


def _sources():
    from .qualification import analyzer_sources
    result=analyzer_sources()
    for name in ('web_reference.py','session.py'):
        path=ROOT/'kiroclops/local_engine'/name
        result[path.relative_to(ROOT).as_posix()]=file_sha256(path)
    return result


def _bound(path,expected):
    path=Path(path).resolve()
    _require(path.is_file() and file_sha256(path)==expected,'Missing or changed bound reference evidence.')
    return path


def _gate(state,domain,split):
    """Validate immutable whole-gate metadata without opening heldout analyses."""
    from kiroclops.web_local.specs import scenarios, capture_scenarios
    experiment=state.get('experiment',{})
    binding=experiment.get(CACHE_FIELD)
    _require(isinstance(binding,dict),'Configured reference cache needs its preregistered gate binding.')
    _require(binding.get('status')=='pass' and binding.get('records')==32,
             'A complete32-record passing gate is required.','reference_not_qualified')
    manifest=_bound(experiment.get('web_reference_bundle',''),experiment.get('web_reference_bundle_sha256'))
    _require(binding.get('manifest_sha256')==file_sha256(manifest),'Cache gate and raw corpus differ.')
    gate_path=_bound(binding.get('path',''),binding.get('sha256'))
    proof=read_json(gate_path);raw_manifest=read_json(manifest)
    _require(proof.get('kind')=='ReferenceObservabilityGateV2' and proof.get('status')=='pass'
        and proof.get('fatal_error') is None and proof.get('count')==proof.get('passed')
        ==proof.get('actual_comparator_self_consistency_passed')==proof.get('required_count')==32,
        'Failed or incomplete reference evidence cannot become an accepted cache.','reference_not_qualified')
    _require(proof.get('manifest_sha256')==binding['manifest_sha256'],'Gate uses another raw corpus.')
    current=_sources();recorded=proof.get('source_hashes',{})
    _require(recorded and recorded==proof.get('completed_source_hashes') and proof.get('source_binding_status')=='pass'
        and all(recorded.get(name)==sha for name,sha in current.items()),'Reference measurements used another source version.')
    for name,sha in recorded.items():
        path=(ROOT/name).resolve()
        _require(path.is_relative_to(ROOT),'Reference source binding escapes the project.')
        _bound(path,sha)
    policy=experiment.get('capture_policy')
    _require(raw_manifest.get('capture_policy')==policy,'Session and reference capture policy differ.')
    expected={(d,s,r['id']):r for d in ('panel','svg') for s in ('calibration','validation')
              for r in (scenarios(d,s) if policy is None else capture_scenarios(d,s,policy))}
    _require(len(expected)==32,'Current reference scenario matrix is not the qualified32-record protocol.')
    rows=proof.get('records',[]);keys=[(r.get('domain'),r.get('split'),r.get('scenario_id')) for r in rows]
    _require(len(keys)==len(set(keys))==32 and set(keys)==set(expected),
             'Duplicate/missing gate rows cannot replace required references.','reference_not_qualified')
    raw_rows=raw_manifest.get('records',[])
    raw_keys=[(r.get('domain'),r.get('split'),r.get('scenario',{}).get('id')) for r in raw_rows]
    _require(len(raw_keys)==len(set(raw_keys))==32 and set(raw_keys)==set(expected),'Raw reference matrix changed.')
    raw_by_key=dict(zip(raw_keys,raw_rows))
    for key,row in zip(keys,rows):
        _require(row.get('status')==row.get('camera_status')==row.get('temporal_support_status')
            ==row.get('comparator_self_consistency_status')=='pass' and not row.get('errors'),
            'A required reference has an unresolved failure.','reference_not_qualified')
        _require(row.get('reference_pack_sha256')==binding['manifest_sha256']
            and row.get('reference_package_sha256')==raw_by_key[key].get('package_sha256')
            and row.get('scenario_sha256')==digest(expected[key])
            and raw_by_key[key].get('scenario')==expected[key],
            'A gate row is bound to a different raw package/scenario.')
    return manifest,gate_path,proof,{key:row for key,row in zip(keys,rows) if key[:2]==(domain,split)},current


def _measurement(record,row,check):
    from .prepared_evidence import _observations
    from .reference_acceptance import selection_matches_package
    from .web_reference import check_reference_comparison_coverage
    check()
    files={}
    for name,sha in row.get('evidence_hashes',{}).items():
        path=_bound(name,sha);files[path]=sha
    analysis_path=_bound(row['analysis_path'],row['analysis_sha256']);files[analysis_path]=row['analysis_sha256']
    selection_path=analysis_path.parent/'selection.json'
    selection_sha=row.get('evidence_hashes',{}).get(str(selection_path))
    _bound(selection_path,selection_sha);files[selection_path]=selection_sha
    analysis=read_json(analysis_path);selection=read_json(selection_path)
    observations,events=analysis.get('observations',{}),analysis.get('events',{})
    package=load_package(record['package'])
    _require(package['package_sha256']==row['reference_package_sha256']==record['reference_package_sha256'],
             'Measured reference package changed.')
    _require(analysis.get('kind')=='AnalysisResult' and analysis.get('measurement_status')=='ok'
        and analysis.get('package_sha256')==package['package_sha256']
        and analysis.get('candidate_sha256')==record['source_sha256'],
        'Cached result is not the original successful reference measurement.')
    _require(selection==observations.get('selection') and selection_matches_package(selection,package['package_sha256'])
        and digest(observations)==row['observations_sha256'] and digest(events)==row['events_sha256']
        and events.get('observation_sha256')==digest(observations) and events.get('package_sha256')==package['package_sha256'],
        'Cached selection, observations or events belong to another measurement.')
    _observations(observations,package)  # Every frame/index/actual time and input receipt.
    semantic={'operation':'analyze','package_sha256':package['package_sha256'],'selection':selection,
              'contract':MEASUREMENT_CONTRACT,'candidate_sha256':record['source_sha256']}
    _require(analysis.get('request_sha256')==digest(semantic),'Cached worker request uses another contract/source/selection.')
    measured={**record,'selection':selection,'measurement_status':analysis['measurement_status'],
              'observations':observations,'events':events}
    comparator_path=_bound(row['self_comparison_path'],row['self_comparison_sha256'])
    files[comparator_path]=row['self_comparison_sha256']
    replay=check_reference_comparison_coverage(measured)
    _require(replay.get('status')=='pass' and replay==read_json(comparator_path),
             'Current comparator does not reproduce the qualified reference result.','reference_not_qualified')
    check()
    return measured,analysis_path,selection_path,files


def reuse_reference_matrix(state,output,domain,split,check,progress,cancel,*,analyze=True):
    """Copy only the requested raw/reference-measurement split into a fresh job."""
    # These checks precede every filesystem access to the configured cache.
    _require(split in ('calibration','validation') and domain in ('panel','svg'),'Unsupported reference split/domain.')
    if split=='validation':
        _require(state.get('sealed_at') is not None,'Heldout reference cache requires a sealed candidate.','not_sealed')
    if split=='calibration' and state.get('mode')=='baseline':
        _require(not analyze,'Baseline preparation cannot access reference measurements.','reference_cache_mode_violation')
    def guard():
        check()
        cancelled=cancel() if callable(cancel) else cancel.is_set() if cancel is not None else False
        if cancelled:raise VisualError('job_cancelled','Reference copy/reuse was cancelled.')
    guard()
    manifest,gate_path,proof,selected,before=_gate(state,domain,split)
    manifest_sha=file_sha256(manifest);gate_sha=file_sha256(gate_path)
    from .web_reference import load_reference_split
    from kiroclops.web_local.specs import scenarios, capture_scenarios
    policy=state.get('experiment',{}).get('capture_policy')
    raw=load_reference_split(manifest,domain,split,expected_source_hashes=state['reference_hashes'],
                             expected_scenarios=scenarios(domain,split) if policy is None else capture_scenarios(domain,split,policy))
    _require(len(raw)==len(selected) and {r['scenario']['id'] for r in raw}=={key[2] for key in selected},
             'Requested reference split is incomplete.')
    output=Path(output).resolve()
    _require(not output.exists(),'Reference reuse output must be a fresh job directory.','output_exists')
    output.mkdir(parents=True)
    rows=[];evidence={manifest:manifest_sha,gate_path:gate_sha};copied_files={}
    for index,record in enumerate(raw):
        guard();identifier=record['scenario']['id'];row=selected[domain,split,identifier]
        progress(f'frozen reference {domain} {split}: {identifier}',index,len(raw))
        directory=output/identifier;package=directory/'package'
        current_manifest=copy_package(record['package'],package)
        _require(current_manifest['package_sha256']==row['reference_package_sha256'],'Copied reference differs from the gate.')
        local={**record,'package':str(package)}
        if analyze:
            measured,analysis_path,selection_path,files=_measurement(record,row,guard)
            evidence.update(files)
            measurement_dir=directory/'measurement';measurement_dir.mkdir()
            for source in (analysis_path,selection_path):
                destination=measurement_dir/source.name;shutil.copyfile(source,destination)
                _require(file_sha256(destination)==files[source],'Reference measurement copy changed.')
                copied_files[destination]=files[source]
            origin={'kind':'PreregisteredReferenceMeasurementReuse','domain':domain,'split':split,'scenario_id':identifier,
                'gate_sha256':gate_sha,'raw_reference_manifest_sha256':manifest_sha,
                'reference_package_sha256':current_manifest['package_sha256'],
                'original_analysis_sha256':row['analysis_sha256'],'original_selection_sha256':files[selection_path],
                'observations_sha256':row['observations_sha256'],'events_sha256':row['events_sha256'],
                'source_hashes_sha256':digest(proof['source_hashes']),
                'measurement_executed':False,'worker_started':False,'model_success_claimed':False}
            write_json(measurement_dir/'reuse-origin.json',origin)
            copied_files[measurement_dir/'reuse-origin.json']=file_sha256(measurement_dir/'reuse-origin.json')
            # The authoritative origin is also inline in the index. Existing
            # PreparedCalibrationEvidence hashes that complete index and binds
            # both unchanged analysis/selection files to the preparation job.
            local.update({k:measured[k] for k in ('selection','measurement_status','observations','events')})
            local.update(analysis=str(measurement_dir/'analysis.json'),measurement_origin=origin)
        rows.append(local)
    guard()
    _require(_sources()==before,'Analysis sources changed while reusing reference measurements.')
    for path,sha in {**evidence,**copied_files}.items():_bound(path,sha)
    for record,row in zip(raw,rows):
        expected=record['reference_package_sha256']
        _require(load_package(record['package'])['package_sha256']==load_package(row['package'])['package_sha256']==expected,
                 'Raw reference or local copy changed during matrix preparation.')
    # Extra source entries in the accepted producer map are checked again too.
    for name,sha in proof['source_hashes'].items():_bound(ROOT/name,sha)
    guard()
    write_json(output/'reference-origin.json',{'kind':'ReferenceMatrixReuse','domain':domain,'split':split,
        'gate_sha256':gate_sha,'raw_manifest_sha256':manifest_sha,'records':len(rows),
        'measurement_accessed':bool(analyze),'measurement_executed':False,'worker_started':False,
        'source_binding_status':'pass','scope':'Immutable reference evidence only; candidate and target measurements are not cached.'})
    progress(f'frozen reference {domain} {split}: complete',len(rows),len(rows))
    return rows
