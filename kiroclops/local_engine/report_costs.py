"""Presentation-only cost scopes; never run or validate a pixel measurement.

Missing historical timing is unknown, not zero. A gate's elapsed time is the
cost of that bound reference preparation, not an estimate of all engineering
work or the latency of an arbitrary new user video.
"""
from pathlib import Path
import math

from kiroclops.visual.schema import VisualError, file_sha256, read_json


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _duration(record):
    start,end=record.get('started_at'),record.get('finished_at')
    return end-start if _number(start) and _number(end) and end>=start else None


def _bound_json(path,expected):
    path=Path(path)
    if not path.is_file() or file_sha256(path)!=expected:
        raise VisualError('report_cost_evidence_changed','Cost metadata differs from its bound evidence.')
    value=read_json(path)
    if file_sha256(path)!=expected:
        raise VisualError('report_cost_evidence_changed','Cost metadata changed while being read.')
    return value


def shared_reference_costs(manifest_path=None,manifest_sha256=None,gate_binding=None,*,qualification_sha256=None):
    """Read only bound top-level metadata, never any heldout measurement files."""
    result={'kind':'SharedPrerequisiteCost','counting_scope':'Once per shared reference corpus, outside each stage budget.',
        'reference_manifest_sha256':manifest_sha256,'reference_gate_sha256':None,
        'reference_capture_wall_seconds':None,'reference_analysis_and_self_check_wall_seconds':None,
        'qualification_24_wall_seconds':None,'qualification_sha256':qualification_sha256,
        'total_first_time_engineering_wall_seconds':None,'first_time_user_video_latency_seconds':None,
        'status':'unrecorded','notes':[
            'Shared prerequisite cost is not multiplied by eighteen trials or subtracted from either arm.',
            'Unknown capture, earlier failed preparations, engineering and review time are not zero.',
            'Cached preparation is not first-time user-video analysis latency.',
            'A historical prerequisite duration is metadata, not a new acceptance or authorship claim.']}
    if manifest_path is None and gate_binding is None:return result
    _bound_json(manifest_path,manifest_sha256)
    result['status']='partial_timing_recorded'
    if gate_binding is not None:
        proof=_bound_json(gate_binding['path'],gate_binding['sha256'])
        if proof.get('manifest_sha256')!=manifest_sha256 or gate_binding.get('manifest_sha256')!=manifest_sha256:
            raise VisualError('report_cost_evidence_changed','Reference timing belongs to another raw corpus.')
        result.update(reference_gate_sha256=gate_binding['sha256'],
            reference_analysis_and_self_check_wall_seconds=_duration(proof),
            reference_gate_record_count=proof.get('count'),reference_gate_recorded_status=proof.get('status'))
    # Current capture manifests/24-control qualification do not record a complete
    # wall-time interval. Do not infer it from file mtimes or video duration.
    return result


def session_costs(directory,state):
    directory=Path(directory);experiment=state.get('experiment',{})
    shared=shared_reference_costs(experiment.get('web_reference_bundle'),
        experiment.get('web_reference_bundle_sha256'),experiment.get('web_reference_cache'))
    prepared=None;job_sha=None
    if state.get('prepare_job_id'):
        path=directory/'jobs'/state['prepare_job_id']/'job.json'
        if path.is_file():
            job=read_json(path)
            if job.get('job_id')==state['prepare_job_id'] and job.get('operation')=='prepare':
                prepared=_duration(job);job_sha=file_sha256(path)
    return {'kind':'SessionCostScopes','shared_prerequisites':shared,
        'prepare_job_wall_seconds':prepared,'prepare_job_metadata_sha256':job_sha,
        'prepare_job_scope':'Includes reference loading/copy/replay and target preparation; not fresh-video latency.',
        'reference_measurement_policy':'preregistered_reuse' if 'web_reference_cache' in experiment else 'not_cached_by_this_protocol',
        'model_stage_budget_seconds':state.get('budget',{}).get('max_seconds'),
        'budget_clock_origin':state.get('clock_origin'),
        'budget_scope':'Authoring and tool activity share the fixed stage budget; its recorded clock origin is separate from model inference time.',
        'unknown_is_zero':False}


def media_measurement_origin(record):
    origin=record.get('measurement_origin')
    if not isinstance(origin,dict):
        return {'status':'not_recorded','scope':'No reuse receipt in this historical record; fresh-worker execution is not inferred.'}
    allowed=('kind','domain','split','scenario_id','gate_sha256','raw_reference_manifest_sha256',
        'reference_package_sha256','original_analysis_sha256','original_selection_sha256',
        'observations_sha256','events_sha256','source_hashes_sha256',
        'measurement_executed','worker_started','model_success_claimed')
    return {key:origin[key] for key in allowed if key in origin}


def cohort_costs(registry):
    binding=registry.get('web_reference_bundle') or {}
    return {'kind':'CohortCostScopes','shared_prerequisites':shared_reference_costs(
            binding.get('manifest'),binding.get('manifest_sha256'),binding.get('observability'),
            qualification_sha256=registry.get('qualification_sha256')),
        'group_elapsed_scope':'Sum of finished stage wall times, including session preparation, native setup, authoring/tools and final native audit. Excludes shared preregistration prerequisites and later independent review.',
        'group_elapsed_is_model_only':False,'shared_prerequisites_counted_once':True,
        'first_time_video_latency_claimed':False,'budget':registry.get('budget')}
