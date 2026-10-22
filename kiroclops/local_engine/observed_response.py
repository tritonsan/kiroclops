"""Prospective SVG response policy shared by fixture and media entry points.

Fixed whole-reference spatial scales avoid repeatedly renormalizing a settled
tail. Real input clocks and lifecycle acceptance remain separately visible.
"""
from .evaluation import _compare_web
from .comparison_diagnostics import explain_comparison
from .media_session import LIMITS, _spatial_observations
from ..visual.schema import VisualError


def compare_media(reference, before, candidate, binding, *, limits=None, require_improvement=False):
    # Media jobs supply a full BehaviorContract (intent/provenance/status plus
    # these thresholds). Compare the required numeric fields, as the legacy
    # media path does; metadata is not an alternative set of limits.
    if binding['scenario']['domain']!='svg' or (limits is not None and
            any(limits.get(key)!=value for key,value in LIMITS.items())):
        raise VisualError('invalid_contract','Observed response v2 is qualified separately for SVG with fixed limits')
    items=(reference,before,candidate)
    if any(item['scenario'].get('analysis_policy')!='observed_response_v2' for item in items):
        raise VisualError('invalid_contract','Analysis policies cannot be mixed')
    spatial,evidence=_spatial_observations([item['observations'] for item in items],binding)
    prepared=[{**item,'observations':obs} for item,obs in zip(items,spatial)]
    result=_compare_web(*prepared,improvement=require_improvement)
    from .geometry_acceptance import svg_preservation
    source_geometry=svg_preservation(spatial[0],spatial[0],reference.get('selection',{}).get('connections',[]))
    result['reference_svg_geometry_preservation']=source_geometry
    if not source_geometry['pass']:
        result['pass']=False
        result['failure_class']='reference_invalid' if source_geometry['measurement_status']=='ok' else 'insufficient_observability'
    if source_geometry['measurement_status']!='ok':result['measurement_pass']=False
    old=[r['before_equal_window_rmse'] for r in result['improvement'] if r['before_equal_window_rmse'] is not None]
    before_score=sum(old)/len(old) if old else None
    gains=[{**r,'applicable':True} for r in result['improvement']]
    result.update(windows=[result['pixel_comparison']],before_score=before_score,
        improvement_fraction=1-result['score']/before_score if before_score and result['score'] is not None else None,
        improvement_gates=gains,improvement_required=require_improvement,
        improvement_pass=bool(gains) and all(r['pass'] for r in gains),
        input_provenance=binding['input_provenance'],claim=binding['claim'],
        measurement_status='ok' if result['measurement_pass'] else 'insufficient_observability',
        spatial_alignment_evidence=evidence,analysis_policy='observed_response_v2',
        interpretation='Prospective policy; shared observable boundaries, full-reference scales, no time warp or threshold change.')
    result['diagnostic']=explain_comparison(result,reference['observations']['inputs']['events'],candidate['observations']['inputs']['events'])
    return result
