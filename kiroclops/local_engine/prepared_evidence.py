"""Bind benchmark calibration caches to completed preparation and actual pixels.

These are local integrity receipts, not a sandbox against arbitrary filesystem
access. They prevent stale, swapped or edited caches from scoring another run.
"""
from pathlib import Path
import math

from kiroclops.visual.schema import VisualError,digest,file_sha256,read_json,write_json,safe_path
from kiroclops.visual.package import load_package


def _fail(message):
    raise VisualError('measurement_binding_changed',message)


def _source_binding(state):
    return {key:state.get(key) for key in ('session_id','domain','mode','baseline','reference','baseline_hashes','reference_hashes','experiment')}


def _inside(path,root):
    path=Path(path).resolve();root=Path(root).resolve()
    if not path.is_relative_to(root):_fail('Prepared evidence must stay within its own preparation output.')
    return path


def _observations(observations,manifest):
    if observations.get('package_sha256')!=manifest['package_sha256'] or digest(observations.get('inputs'))!=digest(manifest['inputs']):
        _fail('Prepared observations refer to another recording or input timeline.')
    objects=observations.get('objects',[])
    if not objects:_fail('Prepared observations contain no object identities.')
    if len({obj.get('id') for obj in objects})!=len(objects):_fail('Prepared object identities repeat.')
    for obj in objects:
        frames=obj.get('frames',[])
        if len(frames)!=len(manifest['frames']):_fail('Prepared observation frame count differs from the recording.')
        for index,(frame,recorded) in enumerate(zip(frames,manifest['frames'])):
            time=frame.get('time_s')
            if frame.get('index')!=index or type(time) not in (int,float) or not math.isfinite(time) or abs(time-recorded['time_s'])>1e-9:
                _fail('Prepared observation frame identities or timestamps differ.')


def _index_signature(path,output):
    path=_inside(path,output);rows=read_json(path)
    if not isinstance(rows,list) or not rows:_fail('Prepared index must contain actual recording rows.')
    result=[]
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('scenario'),dict):_fail('Prepared recording scenario is missing.')
        package=_inside(row.get('package',''),output);manifest=load_package(package)
        files={};observations=row.get('observations');events=row.get('events')
        analysis_path=row.get('analysis')
        if analysis_path:
            analysis_path=_inside(analysis_path,output);analysis=read_json(analysis_path)
            files[analysis_path.relative_to(output).as_posix()]=file_sha256(analysis_path)
            if 'observations' in analysis:
                if observations is not None and digest(observations)!=digest(analysis['observations']):_fail('Index and analysis observations differ.')
                if events is not None and digest(events)!=digest(analysis.get('events')):_fail('Index and analysis events differ.')
                observations=analysis['observations'];events=analysis.get('events')
            else:
                # Game's pure metric projection links the common pixel result.
                observed=analysis_path.parent/'observations.json'
                if not observed.is_file():_fail('Prepared game metrics lack their common pixel observations.')
                observations=read_json(observed);files[observed.relative_to(output).as_posix()]=file_sha256(observed)
                provenance=analysis.get('provenance',{})
                if provenance.get('package_sha256')!=manifest['package_sha256'] or provenance.get('observation_sha256')!=digest(observations):
                    _fail('Prepared game metrics do not bind the recorded pixel observations.')
            selection=analysis_path.parent/'selection.json'
            if selection.is_file():
                files[selection.relative_to(output).as_posix()]=file_sha256(selection)
                if 'selection' in row and digest(row['selection'])!=digest(read_json(selection)):_fail('Prepared selection and analysis selection differ.')
        if observations is not None:_observations(observations,manifest)
        result.append({'scenario_sha256':digest(row['scenario']),'package':package.relative_to(output).as_posix(),
            'package_sha256':manifest['package_sha256'],'observations_sha256':digest(observations) if observations is not None else None,
            'events_sha256':digest(events) if events is not None else None,'linked_files':files})
    return {'file_sha256':file_sha256(path),'records':result}


def store_prepared(root,job,state,prepared,indexes,extras=None):
    """Write the private index and return the hash to include in public job result."""
    root=Path(root).resolve();output=root/'measurements'/job['job_id']
    expected={name:_inside(path,output) for name,path in indexes.items()}
    if not expected:_fail('No prepared record indexes were provided.')
    write_json(root/'prepared-private.json',prepared)
    receipt={'schema_version':1,'kind':'PreparedCalibrationEvidence','prepare_job_id':job['job_id'],
        'source_binding_sha256':digest(_source_binding(state)),
        'prepared_private_sha256':file_sha256(root/'prepared-private.json'),
        'indexes':{name:{'path':path.relative_to(root).as_posix(),'signature':_index_signature(path,output)} for name,path in expected.items()},
        'extras':{name:{'path':_inside(path,output).relative_to(root).as_posix(),'sha256':file_sha256(path)} for name,path in (extras or {}).items()}}
    write_json(root/'prepared-evidence.json',receipt)
    return file_sha256(root/'prepared-evidence.json')


def load_prepared(root,state):
    """Validate before rendering/scoring a candidate, including neutral captures."""
    root=Path(root).resolve();receipt=read_json(root/'prepared-evidence.json')
    prepare_id=state.get('prepare_job_id')
    if (not prepare_id or receipt.get('kind')!='PreparedCalibrationEvidence' or receipt.get('schema_version')!=1
            or receipt.get('prepare_job_id')!=prepare_id or receipt.get('source_binding_sha256')!=digest(_source_binding(state))):
        _fail('Prepared evidence belongs to another session, source binding or preparation job.')
    job=read_json(root/'jobs'/prepare_id/'job.json');result=read_json(root/'jobs'/prepare_id/'result.json')
    if (job.get('status')!='succeeded' or job.get('operation')!='prepare' or job.get('job_id')!=prepare_id
            or job.get('result_sha256')!=digest(result) or result.get('prepared_evidence_sha256')!=file_sha256(root/'prepared-evidence.json')):
        _fail('Prepared evidence is not bound to the completed prepare result.')
    if receipt.get('prepared_private_sha256')!=file_sha256(root/'prepared-private.json'):_fail('Prepared index paths or metadata changed.')
    output=root/'measurements'/prepare_id
    if not receipt.get('indexes'):_fail('Prepared indexes are missing.')
    for item in receipt['indexes'].values():
        path=safe_path(root,item['path'])
        if _index_signature(path,output)!=item['signature']:_fail('A prepared recording, analysis or image package changed.')
    for item in receipt.get('extras',{}).values():
        path=_inside(safe_path(root,item['path']),output)
        if file_sha256(path)!=item['sha256']:_fail('Prepared profile changed.')
    return read_json(root/'prepared-private.json')
