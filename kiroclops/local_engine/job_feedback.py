"""Bound agent responses, retaining complete feedback in the allowed workspace."""
from copy import deepcopy
import json
from pathlib import Path

from kiroclops.visual.schema import VisualError,digest,file_sha256,read_json,write_json

# Kiro 2.27 offloads tool output above ~30,000 characters; stay well below it.
MAX_RESPONSE_CHARS=20000
# An offloaded output lands outside the target and the author sees only a short
# preview, so inspections stay well below the limit too.
INSPECT_MAX_CHARS=24000
# A window needs attention when it fails, nears its limit or got worse than the
# unchanged starting version by a visible margin.
NEAR_LIMIT_FRACTION=.8
REGRESSION_FRACTION=1.25
# V6 plan WP3.1: a window moved against the previous candidate when its position
# error changed by more than this fraction (and by more than CHANGE_FLOOR).
CHANGE_FRACTION=.1
CHANGE_FLOOR=.005
CHANGE_ROWS=8


def _size(value):
    # Conservative: ASCII-escaped, indented JSON, even if the transport is compact.
    return len(json.dumps(value,ensure_ascii=True,indent=2,allow_nan=False))


def _pick(value,keys):
    return {k:deepcopy(value[k]) for k in keys if k in value}


def _round(value):
    return float(f'{value:.4g}') if isinstance(value,float) else value


def _round_deep(value):
    if isinstance(value,list):return [_round_deep(v) for v in value]
    if isinstance(value,dict):return {k:_round_deep(v) for k,v in value.items()}
    return _round(value)


def _windows(comparison):
    """Per-window calibration feedback: each production gate joined with its measured window.

    Suite comparisons carry ``per_window_gates`` (current and starting-version
    position error) and ``pixel_comparison.object_windows`` (errors, limits and
    gates), keyed by object, window and segment.
    """
    measured={(w.get('reference_id'),w.get('window_id'),w.get('segment_index')):w
              for w in (comparison.get('pixel_comparison') or {}).get('object_windows',[])}
    rows=[]
    for gate in comparison.get('per_window_gates') or []:
        window=measured.get((gate.get('object_id'),gate.get('window_id'),gate.get('segment_index')),{})
        limits=window.get('limits') or {}
        row={'object':gate.get('object_id'),'window':gate.get('window_id'),'segment':gate.get('segment_index'),
             'pass':gate.get('pass'),'classification':gate.get('classification'),
             'position_error':gate.get('after_rmse'),'position_error_before_change':gate.get('before_rmse'),
             'position_limit':limits.get('rmse_limit'),'endpoint_error':window.get('endpoint_error'),
             'endpoint_limit':limits.get('endpoint_limit'),'size_error':window.get('size_rmse'),
             'size_limit':limits.get('size_rmse_limit'),
             'failed_gates':sorted(k for k,ok in (window.get('gates') or {}).items() if ok is False) or None,
             'relative_to':window.get('relative_to_reference'),'time_window_s':window.get('time_window_s'),
             'scale_px':window.get('scale_px'),
             # V6 plan WP3.1: direction, timing and shape of the error (feedback only).
             'mean_offset':window.get('mean_offset'),'lag_s':window.get('lag_s'),
             'position_error_at_lag':window.get('lag_position_rmse'),'trajectory':window.get('trajectory')}
        error,before,limit=row['position_error'],row['position_error_before_change'],row['position_limit']
        if all(isinstance(v,(int,float)) for v in (error,limit)) and limit>0:
            row['limit_fraction']=error/limit
            row['near_limit']=error>=NEAR_LIMIT_FRACTION*limit
        if all(isinstance(v,(int,float)) for v in (error,before)):
            row['worse_than_before']=error>REGRESSION_FRACTION*before and error-before>.005
        rows.append({k:_round_deep(v) for k,v in row.items() if v is not None})
    return rows


def _window_errors(result):
    """Position error per (case, object, window, segment) of one evaluate result."""
    errors={}
    for comparison in (result or {}).get('comparisons',[]):
        for row in _windows(comparison):
            if isinstance(row.get('position_error'),(int,float)):
                errors[(comparison.get('case_id'),row.get('object'),row.get('window'),row.get('segment'))]=row['position_error']
    return errors


def change_vs_previous(result,previous):
    """Per-window position error change against the previous evaluated candidate (V6 plan WP3.1)."""
    before,now=_window_errors(previous.get('result')),_window_errors(result)
    rows=[]
    for key in sorted(set(before)&set(now),key=lambda k:tuple(str(v) for v in k)):
        change=now[key]-before[key]
        rows.append({'case_id':key[0],'object':key[1],'window':key[2],'segment':key[3],'position_error':_round(now[key]),
                     'previous_position_error':_round(before[key]),'change':_round(change),
                     'moved':abs(change)>max(CHANGE_FRACTION*before[key],CHANGE_FLOOR)})
    worsened=sorted((r for r in rows if r['moved'] and r['change']>0),key=lambda r:-r['change'])
    improved=sorted((r for r in rows if r['moved'] and r['change']<0),key=lambda r:r['change'])
    listed=worsened[:CHANGE_ROWS]+improved[:CHANGE_ROWS]
    for row in listed:row.pop('moved')
    return {'previous_candidate_id':previous.get('candidate_id'),'windows_compared':len(rows),
            'worsened':len(worsened),'improved':len(improved),'unchanged':len(rows)-len(worsened)-len(improved),
            'rows':listed,'rows_not_listed':len(worsened)+len(improved)-len(listed),
            'meaning':('change is position_error minus the previous candidate\'s on the same window, in fractions of '
                       f'scale_px; a window moved when it changed by more than {int(CHANGE_FRACTION*100)}% '
                       '(and 0.005); listed: the largest worsenings, then the largest improvements.')}


def _attention(row):
    return row.get('pass') is False or row.get('near_limit') is True or row.get('worse_than_before') is True


def _scenario_summary(value):
    keep=('id','case_id','recipe','split','repeat','viewport','duration_s','events')
    return {k:deepcopy(value[k]) for k in keep if k in value and not (k=='case_id' and value.get('id')==value[k])}


def _shrink(value):
    """Replace every repeated full scenario object with its identity, viewport and inputs."""
    if isinstance(value,dict):
        if {'events','viewport'}<=set(value) and ('id' in value or 'case_id' in value):
            return _scenario_summary(value)
        return {k:_shrink(v) for k,v in value.items()}
    if isinstance(value,list):
        return [_shrink(v) for v in value]
    return deepcopy(value)


def _export(target,folder,payload,error):
    root=Path(target).resolve();path=(root/'.kiroclops-evidence'/folder/(digest(payload)+'.json')).resolve()
    if not path.is_relative_to(root):raise VisualError('unsafe_path',error)
    if path.exists():
        if read_json(path)!=payload:raise VisualError('feedback_changed','Exported feedback was modified')
    else:write_json(path,payload)
    return path


def compact_inspection(value,target):
    """Keep inspect_session below Kiro's offload limit; the complete inspection stays in the workspace."""
    if not isinstance(value,dict) or value.get('status')=='error' or _size(value)<=INSPECT_MAX_CHARS:
        return deepcopy(value)
    path=_export(target,'inspect',value,'Inspection output escaped the target workspace')
    compact={k:_shrink(v) for k,v in value.items()}
    compact['full_inspection']={'path':str(path),'sha256':file_sha256(path)}
    compact['inspection_is_summary']=True
    compact['instruction']=('Repeated scenario objects are shortened to id, recipe, split, viewport, duration and input '
                            'events. The complete inspection is full_inspection.path inside the target; read selected '
                            'sections only and keep each displayed section under 8000 characters.')
    planning=compact.get('calibration_planning')
    if _size(compact)>INSPECT_MAX_CHARS and isinstance(planning,dict) and isinstance(planning.get('cases'),list):
        planning['case_count']=len(planning['cases'])
        planning['cases']=[{k:case.get(k) for k in ('scenario_id','behavior_tags','status') if k in case}
                           for case in planning['cases']]
    experiment=compact.get('experiment')
    if _size(compact)>INSPECT_MAX_CHARS and isinstance(experiment,dict) and isinstance(experiment.get('calibration'),list):
        experiment['calibration']=[s.get('id',s.get('case_id')) if isinstance(s,dict) else s for s in experiment['calibration']]
    if _size(compact)>INSPECT_MAX_CHARS:
        raise VisualError('feedback_too_large','The shortened inspection still exceeds the bounded response size')
    return compact


def compact_job_feedback(public,target,previous=None):
    """Only export a result already released by Session.get_job's sealing guard.

    No source/private result is opened here. Final results withheld from an
    active authoring turn remain withheld. Numeric gates and full evidence
    are never truncated on disk; the inline view explicitly declares omission.
    ``previous`` ({candidate_id, result}) is the nearest earlier evaluated
    candidate; its windows give the change table (V6 plan WP3.1).
    """
    changes=None
    if isinstance(public.get('result'),dict) and isinstance(previous,dict) and isinstance(previous.get('result'),dict):
        changes=change_vs_previous(public['result'],previous)
    whole={**public,'change_vs_previous_candidate':changes} if changes else public
    if not isinstance(public.get('result'),dict) or _size(whole)<=MAX_RESPONSE_CHARS:
        return deepcopy(whole)
    job_id=public.get('job_id')
    if not isinstance(job_id,str) or len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id):
        raise VisualError('invalid_job_id','A bounded result needs its actual job identity')
    result=public['result']
    payload={'kind':'PublicCalibrationJobFeedback','job_id':job_id,'operation':public.get('operation'),
             'candidate_id':public.get('candidate_id'),'source_sha256':public.get('source_sha256'),
             'result':deepcopy(result)}
    path=_export(target,'jobs/'+job_id,payload,'Feedback output escaped the target workspace')
    summary=_pick(result,('status','eligible','pass','score','calibration_pass','acceptance_status',
        'heldout_status','independent_review_status','source_review_status','benchmark_qualified',
        'reference_kind','input_provenance','finalization_limit','neutral_capture','reason'))
    summary['full_feedback']={'path':str(path),'sha256':file_sha256(path),'result_sha256':digest(result),
                              'job_id':job_id,'candidate_id':public.get('candidate_id'),
                              'source_sha256':public.get('source_sha256')}
    summary['feedback_is_summary']=True
    summary['instruction']='Read selected JSON sections from full_feedback.path with ordinary workspace file/shell tools; cap each displayed section at 8000 characters to avoid another native output offload. This allowed workspace file preserves complete numerical feedback. Do not re-track images merely because this inline view is shortened.'
    for key in ('reference_media','before_media','candidate_media'):
        if key in result:summary[key]=deepcopy(result[key])
    if 'profile' in result:
        profile=result['profile']
        summary['profile']=_pick(profile,('inference_limit','motion_basis','full_pixel_evidence'))
        summary['profile']['profiles']=[{
            'scenario':_pick(p.get('scenario',{}),('id','domain','viewport','duration_s')),
            'objects':[_pick(o,('id','valid_ratio','frame_count','units','invalid_ranges')) for o in p.get('objects',[])],
            'inputs':deepcopy(p.get('inputs',{})),
            'event_count':len(p.get('events',[])),'unobservable_count':p.get('unobservable_count'),
        } for p in profile.get('profiles',[])]
    comparisons=[]
    for c in result.get('comparisons',[]):
        item=_pick(c,('case_id','role','purpose','pass','score','before_score','improvement_fraction','improvement_pass',
                      'measurement_status','failure_class','required_order_status','interpretation'))
        legacy=[_pick(r,('reference_id','relative_to_reference','window_id','segment_index',
            'pass','valid_time_ratio','position_rmse','scale_px','endpoint_error','gates','limits'))
            for w in c.get('windows',[]) for r in w.get('object_windows',[])]
        windows=_windows(c)
        if legacy or not windows:item['object_windows']=legacy
        if windows:
            listed=[w for w in windows if _attention(w)]
            item['windows']=sorted(listed,key=lambda w:(w.get('pass') is not False,not w.get('worse_than_before'),
                                                          -(w.get('limit_fraction') or 0)))
            item['windows_total']=len(windows)
            item['windows_not_listed']=len(windows)-len(listed)
        item['failed_dynamics']=[deepcopy(r) for r in c.get('dynamics',[]) if r.get('observable') and not r.get('pass')]
        item['failed_improvement']=[deepcopy(r) for r in c.get('improvement_gates',[]) if c.get('improvement_required') and not r.get('pass')]
        comparisons.append(item)
    if comparisons:
        summary['comparisons']=comparisons
        rows=[w for item in comparisons for w in item.get('windows',[])]
        if any('windows' in item for item in comparisons):
            summary['window_feedback']={
                'failing':sum(w.get('pass') is False for w in rows),
                'worse_than_before':sum(w.get('worse_than_before') is True for w in rows),
                'near_limit':sum(w.get('near_limit') is True for w in rows),
                'meaning':('position/endpoint/size errors are fractions of scale_px; position_error_before_change '
                           'is the unchanged starting version on the same clip; listed windows fail, are within '
                           f'{int(NEAR_LIMIT_FRACTION*100)}% of their limit, or got worse than before. mean_offset is '
                           'the average signed candidate-minus-reference offset [x, y] in fractions of scale_px; '
                           'lag_s is the candidate clock shift that best aligns it with the reference (positive: the '
                           'candidate moves later) and position_error_at_lag the error at that shift; trajectory holds '
                           '16 reference and candidate positions in pixels across the window.')}
    response={**deepcopy(public),'result':summary}
    if changes:
        response['change_vs_previous_candidate']=deepcopy(changes)

    # Every omission is explicit, and the immutable full result remains available.
    # Windows that fail or got worse than before matter most to the author, so
    # profiles and descriptive window fields are dropped first. V6: the
    # candidate's own frame packages are the author's visual evidence and are
    # never dropped; reference/before media paths go first.
    def drop_context():
        summary.pop('profile',None)
        for key in ('reference_media','before_media'):summary.pop(key,None)
        media=summary.get('candidate_media')
        if isinstance(media,list) and media:
            # Keep the candidate frames reachable through one root and the
            # case list instead of one long path per case.
            def parent(path):
                index=max(path.rfind('/'),path.rfind('\\'))
                return path[:index+1] if index>=0 else ''
            parents={parent(str(m.get('package'))) for m in media if isinstance(m,dict)}
            summary['candidate_media']={'count':len(media),
                'root':parents.pop() if len(parents)==1 else None,
                'layout':'<root><scenario_id>/ holds each case package (manifest.json and frames)',
                'scenario_ids':[m.get('scenario_id') for m in media if isinstance(m,dict)]}
        for item in comparisons:
            if 'object_windows' in item:
                item['object_window_count']=len(item['object_windows'])
                item['object_windows']=[r for r in item['object_windows'] if r.get('pass') is False]
            for key in ('failed_dynamics','failed_improvement'):
                if not item.get(key):item.pop(key,None)
        summary['additional_fields_omitted']=True

    def drop_trajectories():
        for item in comparisons:
            for w in item.get('windows',[]):w.pop('trajectory',None)

    def drop_details():
        for item in comparisons:
            for w in item.get('windows',[]):
                for key in ('scale_px','size_error','size_limit','classification','segment'):w.pop(key,None)
        table=response.get('change_vs_previous_candidate')
        if table:
            # Keep the two largest worsenings and the two largest improvements.
            kept=[r for r in table['rows'] if r['change']>0][:2]+[r for r in table['rows'] if r['change']<0][:2]
            table['rows_not_listed']+=len(table['rows'])-len(kept)
            table['rows']=kept

    def keep(predicate):
        def step():
            for item in comparisons:
                if 'windows' in item:
                    kept=[w for w in item['windows'] if predicate(w)]
                    item['windows_not_listed']+=len(item['windows'])-len(kept)
                    item['windows']=kept
        return step

    def fold_clean_cases():
        clean=[item for item in comparisons if item.get('pass') is True and not item.get('windows')
               and not item.get('failed_dynamics') and not item.get('failed_improvement') and not item.get('object_windows')]
        if clean:
            summary['passing_cases_without_listed_windows']=[item.get('case_id') for item in clean]
            summary['comparisons']=comparisons[:]=[item for item in comparisons if item not in clean]

    def minimal_rows():
        keys=('object','window','pass','position_error','position_error_before_change','position_limit',
              'failed_gates','worse_than_before','mean_offset','lag_s')
        for item in comparisons:
            item.pop('purpose',None);item.pop('role',None)
            item['windows']=[{k:w[k] for k in keys if k in w} for w in item.get('windows',[])]
        table=response.get('change_vs_previous_candidate')
        if table:
            table['rows_not_listed']+=len(table['rows']);table['rows']=[];table.pop('meaning',None)

    def worst_windows_only():
        rows=sorted(((item,w) for item in comparisons for w in item.get('windows',[])),
                    key=lambda pair:(pair[1].get('pass') is not False,
                                     -(pair[1].get('position_error') or 0)/(pair[1].get('position_limit') or 1)))
        while rows and _size(response)>MAX_RESPONSE_CHARS:
            item,w=rows.pop()
            item['windows'].remove(w);item['windows_not_listed']=item.get('windows_not_listed',0)+1

    for step in (drop_context,drop_trajectories,drop_details,
                 keep(lambda w:w.get('pass') is False or w.get('worse_than_before') is True),
                 keep(lambda w:w.get('pass') is False),fold_clean_cases,minimal_rows,worst_windows_only):
        if _size(response)<=MAX_RESPONSE_CHARS:break
        step()
    if _size(response)>MAX_RESPONSE_CHARS:
        summary['comparison_count']=len(comparisons);summary.pop('comparisons',None)
    if _size(response)>MAX_RESPONSE_CHARS:
        raise VisualError('feedback_too_large','Even result identity exceeds the bounded response size')
    return response
