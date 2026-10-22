"""Three-domain orchestration; only images cross the AnalysisBackend boundary."""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import shutil
import statistics
import time

from kiroclops.visual.schema import VisualError, digest, read_json, write_json, file_sha256
from kiroclops.visual.package import load_package, copy_package


def _analysis(package, selection, output, check, progress, *, contract=None, candidate_sha256=None):
    from kiroclops.cloud.execution import execute
    return execute(package, selection, output, contract=contract or {'min_valid_ratio': .95},
                   candidate_sha256=candidate_sha256, check=check, progress=progress)


def _canonical_domain(domain):
    return 'panel' if domain in ('web_panel', 'web-panel', 'web', 'panel') else domain


def _capture_web(project, output, scenario, check, progress, cancel, variant, *, reference_selection=None, analyze=True):
    from kiroclops.web_local.capture import capture_run
    check()
    captured = capture_run(project, output, scenario, variant=variant, cancel=cancel, progress=progress)
    if captured['timing']['status'] != 'pass':
        raise VisualError('capture_timing_failure', str(captured['timing'].get('errors')))
    record = {'scenario': scenario, 'package': str(captured['package_dir']), 'timing': captured['timing'],
              'functional': captured['functional'], 'source_sha256': digest(captured['source_hashes'])}
    return _measure_record(record, Path(output)/'measurement', check, progress, reference_selection) if analyze else record


def _lifecycle_bindings(selection, manifest):
    """Bind visibility edges to actual reference inputs; clip edges stay censored."""
    times = [frame['time_s'] for frame in manifest['frames']]
    events = manifest['inputs']['events']
    bindings = {}
    for obj in selection['objects']:
        intervals = []
        for start, end in obj.get('active_intervals_s', [[times[0], times[-1]]]):
            edges = []
            for side, value, selector in [('start', start, '#add-node'), ('end', end, '#remove-node')]:
                if abs(value - times[0 if side == 'start' else -1]) <= 1e-9:
                    edges.append({'kind': 'clip_start' if side == 'start' else 'clip_end'})
                    continue
                options = [(i, event) for i, event in enumerate(events) if event['action'] == 'click' and event.get('selector') == selector]
                if not options:
                    raise VisualError('insufficient_observability', 'Reference visibility transition has no matching verified input.')
                index, event = min(options, key=lambda row: abs(row[1]['time_s'] - value))
                ordinal = sum(e['action'] == event['action'] and e.get('selector') == selector for e in events[:index])
                edges.append({'kind': 'input', 'action': event['action'], 'selector': selector,
                              'ordinal': ordinal, 'offset_s': value - event['time_s']})
            intervals.append({'start': edges[0], 'end': edges[1]})
        bindings[obj['id']] = intervals
    return bindings


def _map_lifecycle(bindings, manifest):
    times = [frame['time_s'] for frame in manifest['frames']]
    events = manifest['inputs']['events']
    result = {}
    for object_id, intervals in bindings.items():
        values = []
        for interval in intervals:
            mapped = []
            for side in ('start', 'end'):
                edge = interval[side]
                if edge['kind'] in ('clip_start', 'clip_end'):
                    value = times[0 if edge['kind'] == 'clip_start' else -1]
                else:
                    matching = [e for e in events if e['action'] == edge['action'] and e.get('selector') == edge['selector']]
                    if edge['ordinal'] >= len(matching):
                        raise VisualError('capture_timing_failure', 'Candidate is missing a lifecycle-defining verified input.')
                    value = matching[edge['ordinal']]['time_s'] + edge['offset_s']
                mapped.append(value)
            if mapped[1] <= mapped[0]:
                raise VisualError('capture_timing_failure', 'Mapped object lifetime has no positive duration.')
            values.append(mapped)
        result[object_id] = values
    return result


def _measure_record(record, output, check, progress, reference_selection=None):
    from kiroclops.web_local.selection import select_package_from_pixels
    manifest = load_package(record['package'])
    try:
        selection = select_package_from_pixels(record['package'], _canonical_domain(record['scenario']['domain']))
    except ValueError as exc:
        raise VisualError('tracking_failure', str(exc)) from exc
    # Candidate pixels choose the ROI, never the required identities or lifetimes.
    if reference_selection:
        bindings = reference_selection['lifecycle_bindings']
        expected_intervals = _map_lifecycle(bindings, manifest)
        lookup = {o['id']: o for o in selection['objects']}
        for expected in reference_selection['objects']:
            if expected['id'] not in lookup:
                raise VisualError('tracking_failure', f"Expected visible object missing: {expected['id']}")
            if record['scenario'].get('analysis_policy')!='observed_response_v2':
                lookup[expected['id']]['active_intervals_s'] = expected_intervals[expected['id']]
        if reference_selection.get('connections'):
            selection['connections'] = deepcopy(reference_selection['connections'])
        if reference_selection.get('relative_motion'):
            selection['relative_motion']=deepcopy(reference_selection['relative_motion'])
    else:
        bindings = _lifecycle_bindings(selection, manifest)
    selection['lifecycle_bindings'] = deepcopy(bindings)
    selection['input_origin_s'] = manifest['inputs']['events'][0]['time_s'] if manifest['inputs']['events'] else 0
    write_json(Path(output)/'selection.json', selection)
    result = _analysis(record['package'], selection, output, check, progress,
                       contract={'min_valid_ratio': .95, 'require_static_camera': True,
                                 'event_options':{'connection_tolerance_px':2.,'rest_duration_s':.25}},
                       candidate_sha256=record.get('source_sha256'))
    return {**record, 'analysis': str(Path(output)/'analysis.json'), 'selection': selection,
            'measurement_status': result.get('measurement_status'), 'observations': result['observations'],
            'events': result['events']}


def _motion_origin(observations):
    events = observations.get('inputs', {}).get('events', [])
    clicks = [e for e in events if e['action'] == 'click']
    index = next((i for i,e in enumerate(clicks) if e.get('selector') in ('#open-panel','#add-node','#remove-node')), 0)
    if not clicks:
        raise VisualError('insufficient_observability', 'Recorded initiating input is missing')
    return clicks[index]['time_s'], index


def _event_anchor(events, index):
    event=events[index]
    key=(event['action'],event.get('selector'))
    return {'mode':'input','action':key[0],'selector':key[1],
            'ordinal':sum((e['action'],e.get('selector'))==key for e in events[:index])}


def _contract(reference, candidate=None):
    from kiroclops.visual.comparison import relative_object
    observations = reference['observations']
    observed_policy=reference['scenario'].get('analysis_policy')=='observed_response_v2'
    # A natural camera has no image exactly at an interrupting click. Preserve
    # the real input origin, but end each response at shared observed support.
    # This only changes temporal boundaries, not panel scales or tolerances.
    panel_observed_boundary=(reference['scenario'].get('domain')=='panel'
        and reference['scenario'].get('frame_driver')=='natural_browser_raf_v1')
    origin, input_index = _motion_origin(observations)
    pairs = []
    last_motion = origin
    last_available = -float('inf')
    mapped = None
    if candidate and reference.get('selection', {}).get('lifecycle_bindings'):
        candidate_obs = candidate['observations']
        mapped = _map_lifecycle(reference['selection']['lifecycle_bindings'],
                                {'frames': candidate_obs['objects'][0]['frames'], 'inputs': candidate_obs['inputs']})
        candidate_origin, _ = _motion_origin(candidate_obs)
    relative={item['object_id']:item['relative_to'] for item in reference.get('selection',{}).get('relative_motion',[])}
    objects={obj['id']:obj for obj in observations['objects']}
    for obj in observations['objects']:
        measured=relative_object(obj,objects[relative[obj['id']]]) if obj['id'] in relative else obj
        frames = [f for f in measured['frames'] if f['valid']]
        if len(frames) < 2:
            raise VisualError('tracking_failure', 'Reference object has insufficient observations')
        xs, ys = [f['x'] for f in frames], [f['y'] for f in frames]
        distance = math.hypot(max(xs)-min(xs), max(ys)-min(ys))
        static = distance <= (1.5 if observed_policy else 2)
        scale = 1. if static else distance
        for a,b in zip(frames, frames[1:]):
            if math.hypot(b['x']-a['x'], b['y']-a['y']) > .5:
                last_motion = max(last_motion, b['time_s'])
        last_available = max(last_available, frames[-1]['time_s'])
        pair = {'reference_id': obj['id'], 'candidate_id': obj['id'], 'scale_px': scale,
                      'rmse_limit': 1.5 if static else .03, 'size_rmse_limit': 2 if static else .03,
                      'endpoint_limit': 2 if static else max(2 / scale, .01), 'static': static}
        if observed_policy:
            pair.update(size_rmse_limit=1.5 if static else .03,endpoint_limit=1.5 if static else .01)
        if obj['id'] in relative:
            pair.update(relative_to_reference=relative[obj['id']],relative_to_candidate=relative[obj['id']])
        if mapped and obj['id'] in mapped:
            pair['candidate_active_intervals_s'] = [[a-candidate_origin, b-candidate_origin] for a,b in mapped[obj['id']]]
        pairs.append(pair)
    end = max(.2, min(last_available - origin - .02, last_motion - origin + .30))
    # Each response is anchored to its own actual command in BOTH recordings.
    # A later input's scheduling jitter must not look like animation latency.
    events=observations['inputs']['events']
    def response_input(i,e):
        if not observed_policy:return e['action'] in ('click','drag_start','drag_end')
        # Pointer-down does not prescribe a displacement. The first actual
        # pointer move anchors the drag trajectory; a variable down-to-move
        # pause must not translate every subsequent movement sample. The
        # preceding click window still covers any response to pointer-down.
        return e['action'] in ('click','drag_end') or (e['action']=='drag_move' and i>0 and events[i-1]['action']=='drag_start')
    relevant=[i for i,e in enumerate(events) if response_input(i,e)
              and origin-1e-9<=e['time_s']<origin+end-1e-9]
    if not relevant or abs(events[relevant[0]]['time_s']-origin)>1e-9:
        raise VisualError('insufficient_observability','Initiating interaction cannot be bound')
    windows=[]
    for index,event_index in enumerate(relevant):
        next_index=relevant[index+1] if index+1<len(relevant) else None
        duration=(events[next_index]['time_s'] if next_index is not None else origin+end)-events[event_index]['time_s']
        if duration<=0:raise VisualError('capture_timing_failure','Response inputs must be ordered distinctly')
        window={'id':f'motion-{index}','start_s':0.,'end_s':duration,
                'alignment':_event_anchor(events,event_index)}
        if next_index is not None:window['end_alignment']=_event_anchor(events,next_index)
        windows.append(window)
    # A node removed by the very first command still needs a measured identity
    # and position before removal, followed by the existing absence checks.
    initial_only=[obj['id'] for obj in observations['objects'] if obj.get('active_intervals_s') and
                  all(b<=origin+1e-9 for a,b in obj['active_intervals_s'])]
    if observed_policy:
        initial_only=[obj['id'] for obj in observations['objects'] if obj.get('active_intervals_s')
                      and obj['active_intervals_s'][0][0]<origin]
    if initial_only:
        first=max(objects[oid]['frames'][0]['time_s'] for oid in initial_only)
        start=max(first-origin,-.25)
        if start>=0:raise VisualError('insufficient_observability','Removed initial objects have no observed baseline')
        anchor=_event_anchor(events,relevant[0])
        windows.insert(0,{'id':'initial-preservation','start_s':start,'end_s':0.,
                          'alignment':anchor,'end_alignment':anchor,'object_ids':initial_only})
    timeline = [f['time_s'] for f in observations['objects'][0]['frames']]
    lifecycle_tolerance = min(.1, max(.05, 1.5*statistics.median(b-a for a,b in zip(timeline,timeline[1:]))))
    return {'schema_version': 1, 'kind': 'BehaviorContract', 'min_valid_ratio': .95,
            **({'sampling_policy':'observed_pre_input_v1'} if panel_observed_boundary else {}),
            **({'sampling_policy':'observed_response_v2','scale_policy':'whole_reference_object_motion'} if observed_policy else {}),
            'alignment': {'mode': 'input', 'action': 'click', 'event_index': input_index},
            'max_interpolation_gap_s': .12, 'object_pairs': pairs,
            'windows': windows,
            'lifecycle_tolerance_s': lifecycle_tolerance,
            'event_options': {'rest_duration_s': .25, 'connection_tolerance_px': 2.}, 'required_event_types': [],
            'require_final_no_overlap': reference['scenario'].get('domain')=='svg',
            'require_connections': bool(observations.get('connections')),
            'required_connections': [c['id'] for c in observations.get('connections', [])]}


def _distance(point, other):
    return math.hypot(point[0]-other[0], point[1]-other[1])


def _segment_samples(obj, start, end):
    return [f for f in obj['frames'] if start-1e-9 <= f['time_s'] <= end+1e-9 and f.get('valid')]


def _settle_time(frames, endpoint, band, origin):
    if len(frames) < 2:
        return None
    last_bad = -1
    for index, frame in enumerate(frames):
        if _distance((frame['x'], frame['y']), endpoint) > band:
            last_bad = index
    first = last_bad + 1
    if first >= len(frames) or frames[-1]['time_s'] - frames[first]['time_s'] < .25-1e-9:
        return None
    if any(b['time_s']-a['time_s'] > .12+1e-9 for a,b in zip(frames[first:], frames[first+1:])):
        return None
    return frames[first]['time_s'] - origin


def _overshoot(frames, start_point, endpoint):
    displacement = (endpoint[0]-start_point[0], endpoint[1]-start_point[1])
    distance = math.hypot(*displacement)
    if distance <= 1e-9:
        return None
    unit = (displacement[0]/distance, displacement[1]/distance)
    return max(0., max((f['x']-endpoint[0])*unit[0] + (f['y']-endpoint[1])*unit[1] for f in frames))


def _mapped_event_time(relative, reference, candidate, reference_origin, candidate_origin):
    """Segment clock reset at known actions; never rescale a motion's duration."""
    absolute = relative + reference_origin
    ref_events = reference['inputs']['events']
    candidate_events = candidate['inputs']['events']
    for index, event in enumerate(ref_events):
        if abs(event['time_s']-absolute) > 1e-9:
            continue
        key = (event['action'], event.get('selector'))
        ordinal = sum((e['action'], e.get('selector')) == key for e in ref_events[:index])
        matches = [e for e in candidate_events if (e['action'], e.get('selector')) == key]
        if ordinal >= len(matches):
            raise VisualError('capture_timing_failure', 'Candidate lacks a required interaction event.')
        return matches[ordinal]['time_s']
    return candidate_origin + relative


def _dynamic_checks(reference, candidate, contract):
    """Fixed 250 ms settling-band and overshoot gates, from observed geometry."""
    refs, targets = reference['observations'], candidate['observations']
    ref_lookup = {obj['id']: obj for obj in refs['objects']}
    target_lookup = {obj['id']: obj for obj in targets['objects']}
    from kiroclops.visual.comparison import window_timing,relative_object
    checks = []
    for pair in contract['object_pairs']:
        if pair['static']:
            continue
        obj = ref_lookup[pair['reference_id']]
        target_obj=target_lookup.get(pair['candidate_id'])
        if pair.get('relative_to_reference'):
            obj=relative_object(obj,ref_lookup[pair['relative_to_reference']])
            target_obj=relative_object(target_obj,target_lookup[pair['relative_to_candidate']]) if target_obj and pair['relative_to_candidate'] in target_lookup else None
        for window in contract['windows']:
            if window.get('object_ids') is not None and obj['id'] not in window['object_ids']:
                continue
            timing=window_timing(refs,targets,contract,window)
            reference_origin,candidate_origin=timing['reference_origin_s'],timing['candidate_origin_s']
            start,end=[value+reference_origin for value in timing['time_window_s']]
            # Presence boundaries censor dynamics. Interrupted or newly appearing
            # segments without an observable 250ms tail cannot prove settling.
            intervals = obj.get('active_intervals_s', [[start, end]])
            for interval in intervals:
                left, right = max(start, interval[0]), min(end, interval[1])
                if right <= left:
                    continue
                row = {'object_id': obj['id'], 'window_id': window['id'], 'reference_interval_s': [left,right],
                       'observable': False, 'pass': False, 'settle_band_px': max(2., .01*pair['scale_px']),
                       'overshoot_limit_px': .02*pair['scale_px'],'input_timing':timing}
                if not timing['endpoint_required']:
                    row['reason']='reference_interrupted_or_unsettled'
                    checks.append(row)
                    continue
                frames = _segment_samples(obj, left, right)
                if len(frames) < 2 or frames[-1]['time_s']-frames[0]['time_s'] < .25:
                    row['reason'] = 'reference_interval_too_short'
                    checks.append(row)
                    continue
                tail = [f for f in frames if f['time_s'] >= frames[-1]['time_s']-.25-1e-9]
                endpoint = (statistics.median(f['x'] for f in tail), statistics.median(f['y'] for f in tail))
                start_point = (frames[0]['x'], frames[0]['y'])
                if _distance(start_point, endpoint) <= row['settle_band_px']:
                    row['reason'] = 'no_directional_displacement_in_segment'
                    checks.append(row)
                    continue
                reference_settle = _settle_time(frames, endpoint, row['settle_band_px'], left)
                if reference_settle is None:
                    row['reason'] = 'reference_interrupted_or_unsettled'
                    checks.append(row)
                    continue
                row['observable'] = True
                target = target_obj
                candidate_start = candidate_origin+left-reference_origin
                candidate_end = candidate_origin+right-reference_origin
                observed = _segment_samples(target, candidate_start, candidate_end) if target else []
                candidate_settle = _settle_time(observed, endpoint, row['settle_band_px'], candidate_start)
                reference_overshoot = _overshoot(frames, start_point, endpoint)
                candidate_overshoot = _overshoot(observed, start_point, endpoint) if observed else None
                timing_limit = max(.1, .1*reference_settle)
                gates = {'settled_event_observed': candidate_settle is not None,
                         'settling_time': candidate_settle is not None and abs(candidate_settle-reference_settle) <= timing_limit+1e-9,
                         'overshoot': candidate_overshoot is not None and abs(candidate_overshoot-reference_overshoot) <= row['overshoot_limit_px']+1e-9}
                row.update(reference_settling_s=reference_settle, candidate_settling_s=candidate_settle,
                           reference_overshoot_px=reference_overshoot, candidate_overshoot_px=candidate_overshoot,
                           settling_time_limit_s=timing_limit, endpoint_px=list(endpoint), gates=gates, **{'pass': all(gates.values())})
                checks.append(row)
    return checks


def _compare_web(reference, before, candidate, *, improvement):
    from kiroclops.visual.comparison import compare_observations
    contract = _contract(reference, candidate)
    old = compare_observations(reference['observations'], before['observations'], _contract(reference, before))
    new = compare_observations(reference['observations'], candidate['observations'], contract)
    moving = {p['reference_id'] for p in contract['object_pairs'] if not p['static']}
    gains = []
    for object_id in sorted(moving):
        old_values = [r['position_rmse'] for r in old['object_windows'] if r['reference_id'] == object_id]
        new_values = [r['position_rmse'] for r in new['object_windows'] if r['reference_id'] == object_id]
        # Equal weight per actual motion window; a zero-error static window is
        # not individually required to improve by fifty percent.
        old_error = statistics.mean(old_values) if old_values and all(v is not None for v in old_values) else None
        new_error = statistics.mean(new_values) if new_values and all(v is not None for v in new_values) else None
        gain = 1-new_error/old_error if old_error is not None and old_error > 1e-9 and new_error is not None else None
        gains.append({'object_id': object_id, 'before_equal_window_rmse': old_error, 'after_equal_window_rmse': new_error,
                      'improvement_fraction': gain,
                      'pass': gain is not None and gain >= .5})
    errors = [g['after_equal_window_rmse'] for g in gains if g['after_equal_window_rmse'] is not None]
    functional = candidate['functional'].get('status') == 'pass'
    # Good per-frame tracking cannot certify an unsampled response interval.
    # Improvement also requires a measurable baseline; a large error on a
    # handful of baseline pixels is not evidence for a fifty-percent gain.
    baseline_measured = before['measurement_status'] == 'ok' and old['measurement_status'] == 'pass'
    measured = (candidate['measurement_status'] == 'ok' and reference['measurement_status'] == 'ok'
                and new['measurement_status'] == 'pass' and (not improvement or baseline_measured))
    dynamics = _dynamic_checks(reference, candidate, contract)
    relative_timing=_relative_timing_checks(reference,candidate,contract)
    svg_geometry=None
    if contract['require_final_no_overlap']:
        from .geometry_acceptance import svg_preservation
        svg_geometry=svg_preservation(reference['observations'],candidate['observations'],
                                      reference.get('selection',{}).get('connections',[]))
        measured = measured and svg_geometry['measurement_status']=='ok'
    failure_class = svg_geometry['failure_class'] if svg_geometry else None
    baseline_coverage = [r for r in old['object_windows'] if r['gates']['coverage'] is False]
    if any(r['gates']['coverage'] is False for r in new['object_windows']):
        failure_class = 'comparison_coverage_failure'
    elif improvement and baseline_coverage:
        failure_class = 'baseline_comparison_coverage_failure'
    elif not measured and failure_class is None:
        failure_class = 'comparison_measurement_failure'
    passed = bool(new['success'] and functional and measured and all(row['pass'] for row in dynamics if row['observable'])
                  and all(row['pass'] for row in relative_timing)
                  and (svg_geometry is None or svg_geometry['pass'])
                  and (not improvement or gains and all(g['pass'] for g in gains)))
    return {'scenario_id': reference['scenario']['id'], 'pass': passed, 'score': statistics.mean(errors) if errors else None,
            'pixel_comparison': new, 'improvement': gains, 'functional': candidate['functional'],
            'measurement_pass': measured, 'baseline_measurement_pass': baseline_measured,
            'baseline_coverage_failures': baseline_coverage,
            'contract': contract, 'dynamics': dynamics,'relative_timing':relative_timing,
            'svg_geometry_preservation':svg_geometry,
            'failure_class':failure_class}


def _relative_timing_checks(reference,candidate,contract):
    from kiroclops.visual.events import extract_events
    specs=reference.get('selection',{}).get('relative_motion',[])
    if len(specs)<2:return []
    eventsets=[extract_events(item['observations'],selection={**item.get('selection',{}),'relative_motion':specs},contract=contract)
               for item in (reference,candidate)]
    def mapped(eventset):
        return {(tuple(r['object_ids']),r['relative_to_object_id'],r['occurrence']):r
                for r in eventset['relations'] if r['type']=='relative_event_timing' and
                r['event_type']=='motion_start' and r['relative_to_object_id'] is not None}
    refs,targets=[mapped(item) for item in eventsets];rows=[]
    if not refs:return [{'pass':False,'reason':'reference_relative_onsets_unobservable'}]
    for key in sorted(set(refs)|set(targets)):
        ref,target=refs.get(key),targets.get(key)
        row={'object_ids':list(key[0]),'relative_to_object_id':key[1],'occurrence':key[2],
             'reference':ref,'candidate':target,'lag_tolerance_s':.1,'pass':False}
        if ref and target:
            a,b=ref['lag_interval_s'];c,d=target['lag_interval_s'];distance=max(0.,c-b,a-d)
            # A label is a quantized summary, not a second measurement. Two
            # overlapping lag intervals may straddle the 50 ms order boundary.
            # Accept that ambiguity only with actual interval overlap; opposite
            # resolved orders and missing/unresolved onsets still fail.
            orders={ref['order'],target['order']}
            same_order=len(orders)==1 and orders <= {'before','after','overlapping_uncertainty'}
            compatible_ambiguity=(orders in ({'before','overlapping_uncertainty'},
                                             {'after','overlapping_uncertainty'})
                                 and distance<=1e-9)
            row['order_comparison']='same' if same_order else 'overlapping_lag_intervals' if compatible_ambiguity else 'incompatible'
            row.update(lag_interval_distance_s=distance,gates={
                'resolved_onsets':ref['timing_status']=='resolved' and target['timing_status']=='resolved',
                'observed_order':same_order or compatible_ambiguity,
                'lag':distance<=.1+1e-9})
            row['pass']=all(row['gates'].values())
        else:row['reason']='relative_onset_count_mismatch'
        rows.append(row)
    return rows


def _export_calibration(directory, records, role):
    root = Path(directory) / 'target/.kiroclops-evidence/calibration' / role
    exported = []
    for record in records:
        destination = root / record['scenario']['id']
        if destination.exists():
            if load_package(destination)['package_sha256'] != load_package(record['package'])['package_sha256']:
                raise VisualError('evidence_conflict', 'Existing calibration evidence belongs to a different recording.')
        else:
            copy_package(record['package'], destination)
        exported.append({'scenario_id': record['scenario']['id'], 'package': str(destination)})
    return exported


# V6 plan WP2.1: a host timing stall is a property of one recording, not of the
# candidate. Such a scenario is re-recorded from the same sealed sources; the
# failed attempt's evidence stays beside the canonical output and every attempt
# is listed on the row. Measurement failures are never retried.
CAPTURE_ATTEMPTS = 3
RETRYABLE_CAPTURE_CODES = ('capture_timing_failure', 'machine_not_quiet')
RETRY_PAUSE_S = 3.


def _capture_web_retrying(project, output, scenario, check, progress, cancel, variant, *, reference_selection=None,
                          analyze=True, attempts=CAPTURE_ATTEMPTS, note=lambda text: None):
    import time
    output = Path(output)
    history = []
    for attempt in range(1, attempts+1):
        try:
            row = _capture_web(project, output, scenario, check, progress, cancel, variant,
                               reference_selection=reference_selection, analyze=analyze)
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if code not in RETRYABLE_CAPTURE_CODES or attempt >= attempts:
                if history:
                    raise VisualError(code or 'capture_failed',
                                      f'{exc} (attempt {attempt} of {attempts}; earlier attempts: '
                                      + '; '.join(f"{h['attempt']}={h['code']}" for h in history) + ')') from exc
                raise
            preserved = output.with_name(f'{output.name}~failed-attempt{attempt}')
            if output.exists():
                output.rename(preserved)
            history.append({'attempt': attempt, 'code': code, 'message': str(exc),
                            'evidence': str(preserved) if preserved.exists() else None})
            note(f'{scenario["id"]}: re-recording after {code} (attempt {attempt+1} of {attempts})')
            check()
            time.sleep(RETRY_PAUSE_S)
            continue
        row['recording_retries'] = history+[{'attempt': attempt, 'status': 'ok'}]
        return row
    raise VisualError('capture_failed', 'Unreachable capture retry state')


def _web_matrix(project, root, domain, split, check, progress, cancel, variant, reference=None, *, analyze=True, capture_policy=None, case_ids=None):
    from kiroclops.web_local.specs import scenarios, capture_scenarios
    rows = []
    recipes = scenarios(domain, split) if capture_policy is None else capture_scenarios(domain, split, capture_policy)
    selected = list(enumerate(recipes))
    if case_ids is not None:
        unknown = [c for c in case_ids if c not in {s['id'] for s in recipes}]
        if unknown:
            raise VisualError('invalid_request', 'Unknown calibration cases: ' + ', '.join(unknown))
        selected = [(i, s) for i, s in selected if s['id'] in case_ids]
    for position, (index, scenario) in enumerate(selected):
        check()
        progress(f'{variant} {domain} {split}: {scenario["id"]}', position, len(selected))
        ref = reference[index]['selection'] if reference else None
        rows.append(_capture_web_retrying(project, Path(root) / scenario['id'], scenario, check, progress, cancel, variant,
                                          reference_selection=ref, analyze=analyze,
                                          note=lambda text, position=position: progress(f'{variant} {domain} {split}: {text}', position, len(selected))))
    return rows


def _profile(records, *, full_output=None):
    from .feedback import motion_feedback
    return motion_feedback(records,full_output=full_output)


def _reference_web_matrix(state, output, domain, split, check, progress, cancel, *, analyze=True):
    """Replay the same preregistered raw reference pixels in both comparison arms."""
    experiment = state.get('experiment', {})
    if 'web_reference_cache' in experiment:
        from .reference_cache import reuse_reference_matrix
        return reuse_reference_matrix(state,output,domain,split,check,progress,cancel,analyze=analyze)
    bundle = experiment.get('web_reference_bundle')
    if not bundle:
        return _web_matrix(state['reference'], output, domain, split, check, progress, cancel,
                           'reference', analyze=analyze, capture_policy=experiment.get('capture_policy'))
    from .web_reference import load_reference_split
    from kiroclops.web_local.specs import scenarios, capture_scenarios
    expected_sha = experiment.get('web_reference_bundle_sha256')
    if not expected_sha or file_sha256(bundle) != expected_sha:
        raise VisualError('reference_bundle_changed', 'The frozen reference manifest differs from the session binding.')
    if split == 'validation' and state.get('sealed_at') is None:
        raise VisualError('not_sealed', 'Held-out reference recordings require a sealed candidate.')
    check()
    policy=experiment.get('capture_policy')
    recipes=scenarios(domain,split) if policy is None else capture_scenarios(domain,split,policy)
    raw = load_reference_split(bundle, domain, split, expected_source_hashes=state['reference_hashes'],
                               expected_scenarios=recipes)
    rows = []
    for index, record in enumerate(raw):
        check()
        progress(f'frozen reference {domain} {split}: {record["scenario"]["id"]}', index, len(raw))
        destination = Path(output) / record['scenario']['id']
        copy_package(record['package'], destination / 'package')
        local = {**record, 'package': str(destination / 'package')}
        rows.append(_measure_record(local, destination / 'measurement', check, progress) if analyze else local)
    if file_sha256(bundle) != expected_sha:
        raise VisualError('reference_bundle_changed', 'The frozen reference manifest changed while preparing its local copy.')
    return rows


def _run_web(directory, job, state, check, progress, cancel):
    from .prepared_evidence import store_prepared,load_prepared
    root = Path(directory)
    output = root / 'measurements' / job['job_id']
    domain, mode = _canonical_domain(state['domain']), state['mode']
    capture_policy=state.get('experiment',{}).get('capture_policy')
    operation = job.get('operation', job.get('action'))
    if operation == 'prepare':
        use_analysis = mode == 'tools'
        refs = _reference_web_matrix(state, output/'reference', domain, 'calibration', check, progress, cancel, analyze=use_analysis)
        before = _web_matrix(state['baseline'], output/'before', domain, 'calibration', check, progress, cancel, 'before', refs if use_analysis else None, analyze=use_analysis,capture_policy=capture_policy)
        if use_analysis and any(r['measurement_status'] != 'ok' for r in refs+before):
            raise VisualError('tracking_failure', 'Calibration is not qualified for model-guided work')
        if any(r['functional']['status'] != 'pass' for r in refs+before):
            raise VisualError('preservation_violation', 'Reference or initial target has invalid functionality')
        write_json(output/'reference.json', refs)
        write_json(output/'before.json', before)
        public = {'status': 'ready', 'reference_media': _export_calibration(root,refs,'reference'),
                  'before_media': _export_calibration(root,before,'before')}
        if mode == 'tools':
            profile = _profile(refs,full_output=root/'target/.kiroclops-evidence/calibration/full-measurements.json')
            profile_path = root/'target/.kiroclops-evidence/calibration/profile.json'
            write_json(profile_path, profile)
            public.update(profile=profile, profile_path=str(profile_path))
        prepared={'reference':str(output/'reference.json'),'before':str(output/'before.json')}
        public['prepared_evidence_sha256']=store_prepared(root,job,state,prepared,
            {'reference':output/'reference.json','before':output/'before.json'})
        return public
    if operation == 'report':
        from .reporting import build_report
        return build_report(root)
    prepared = load_prepared(root,state)
    source = job['source_path']
    if operation == 'evaluate':
        if mode == 'baseline':
            # Same source/runtime/capture limits, without charging the normal
            # Kiro arm for tracking, model fitting or pixel comparison work.
            candidates = _web_matrix(source, output/'candidate', domain, 'calibration', check, progress, cancel, 'candidate', analyze=False,capture_policy=capture_policy)
            write_json(output/'candidate.json', candidates)
            media = _export_candidate(root, candidates, job['candidate_id'])
            return {'eligible': all(c['functional']['status'] == 'pass' for c in candidates), 'score': None,
                    'neutral_capture': True, 'candidate_media': media,
                    'message': 'Recording completed; select your candidate from actual images.'}
        refs, before = read_json(prepared['reference']), read_json(prepared['before'])
        requested = job.get('case_ids')
        candidates = _web_matrix(source, output/'candidate', domain, 'calibration', check, progress, cancel, 'candidate', refs,
                                 capture_policy=capture_policy, case_ids=requested)
        if requested is not None:
            # V6 plan WP3.2: a partial probe compares only the named calibration cases.
            refs = [r for r in refs if r['scenario']['id'] in requested]
            before = [b for b in before if b['scenario']['id'] in requested]
        comparisons = [_compare_web(r,b,c,improvement=False) for r,b,c in zip(refs,before,candidates)]
        write_json(output/'candidate.json', candidates)
        write_json(output/'comparison.json', comparisons)
        media = _export_candidate(root, candidates, ('probes/' if requested is not None else '')+job['candidate_id'])
        scores = [c['score'] for c in comparisons if c['score'] is not None]
        result = {'eligible': all(c['pass'] for c in comparisons), 'score': statistics.mean(scores) if len(scores)==len(comparisons) else None,
                  'comparisons': comparisons, 'candidate_media': media, 'independent_review_status': 'pending'}
        if requested is not None:
            result.update(eligible=False, partial=True, partial_cases=list(requested),
                          message='Partial probe: only the named calibration cases were measured; it cannot be finalized.')
        return result
    if operation == 'finalize':
        refs = _reference_web_matrix(state,output/'reference',domain,'validation',check,progress,cancel)
        before = _web_matrix(state['baseline'],output/'before',domain,'validation',check,progress,cancel,'before',refs,capture_policy=capture_policy)
        final = _web_matrix(source,output/'candidate',domain,'validation',check,progress,cancel,'final',refs,capture_policy=capture_policy)
        comparisons = [_compare_web(r,b,c,improvement=True) for r,b,c in zip(refs,before,final)]
        for name, rows in (('reference',refs),('before',before),('final',final),('comparison',comparisons)):
            write_json(output/f'{name}.json', rows)
        return {'pass': len(comparisons)==12 and all(c['pass'] for c in comparisons),
                'passed': sum(c['pass'] for c in comparisons), 'required': 12,
                'comparisons': comparisons, 'independent_review_status': 'pending', 'source_review_status': 'pending',
                'numerical_acceptance_only': True}
    raise VisualError('unsupported_operation', operation)


def _export_candidate(directory, records, candidate_id):
    exported = []
    for record in records:
        destination = Path(directory)/'target/.kiroclops-evidence/candidates'/candidate_id/record['scenario']['id']
        copy_package(record['package'], destination)
        exported.append({'scenario_id': record['scenario']['id'], 'package': str(destination)})
    return exported


def run_job(directory, job, check, progress, cancel):
    state = read_json(Path(directory)/'session.json')
    if state.get('experiment',{}).get('reference_mode')=='media_suite':
        from .media_suite import run_suite_job
        result = run_suite_job(directory,job,state,check,progress,cancel)
    elif state.get('experiment',{}).get('reference_mode')=='media_bundle':
        from .media_session import run_media_job
        result = run_media_job(directory,job,state,check,progress,cancel)
    elif state['domain'] in ('web_panel','web-panel','web','panel','svg'):
        result = _run_web(directory,job,state,check,progress,cancel)
    elif state['domain'] == 'game':
        from .game import run_game_job
        result = run_game_job(directory,job,state,check,progress,cancel)
    else:
        raise VisualError('unsupported_target', state['domain'])
    from .calibration_planning import advice_for_job
    return advice_for_job(state, job, Path(directory)/'measurements'/job['job_id'], result)
