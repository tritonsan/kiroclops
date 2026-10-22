"""Compare independently segmented anchors using the frozen event definition.

No old review, video, analysis, threshold or candidate is changed. This unsigned
diagnosis distinguishes event-definition mismatch from irreducible timing
uncertainty; it cannot turn a failed historical review into acceptance.
"""
from collections import defaultdict
from pathlib import Path
import argparse

from kiroclops.visual.raw_event_review import candidates, relative_timing
from kiroclops.visual.schema import read_json, write_json, digest, file_sha256


def run(review, raw, output):
    review, raw, output = map(lambda p: Path(p).resolve(), (review, raw, output))
    if output.exists():
        raise ValueError('Preserve earlier diagnoses; use a fresh output.')
    prepared = read_json(review / 'preparation.json')
    assert digest(prepared) == read_json(review / 'preparation-binding.json')['sha256']
    raw_manifest = read_json(raw / 'manifest.json')
    raw_hashes = {row['record']: row['sha256'] for row in raw_manifest['proposals']}
    records = []
    for rec in prepared['fingerprint']['records']:
        name = rec['id']
        if file_sha256(raw / name / 'raw-proposals.json') != raw_hashes.get(name):
            raise ValueError('Independent pixel aid changed: ' + name)
        data = read_json(raw / name / 'raw-proposals.json')
        rows = data['rows']
        events_file = review / 'records' / name / 'events/events.json'
        observed_file = review / 'records' / name / 'events/observations.json'
        measured = read_json(events_file)
        observations = read_json(observed_file)
        assert digest(observations) == rec['observations_sha256']
        policy = measured['motion_start_policy']['version']
        # Definition/settings are public measurement scope, not the detector's
        # observed times or coordinates. The historical aid hardcoded .25s
        # while these frozen results actually declare .2s rest support.
        options = measured['options']
        selection = observations['selection']
        lifetimes = {o['id']: o['active_intervals_s'] for o in selection['objects'] if 'active_intervals_s' in o}
        valid_scope = {o['id']: {f['index']: f.get('valid') is True for f in o['frames']}
                       for o in observations['objects']}
        scoped = [{**r, 'points': {oid: (p if valid_scope[oid].get(r['index'], False) else {'x': None, 'y': None})
                                  for oid, p in r['points'].items()}} for r in rows]
        proposed = []
        for ids in (['bookmark'], ['handle'], ['star'], ['bookmark', 'handle'], ['star', 'handle']):
            proposed.extend(candidates(scoped, ids, options['rest_duration_s'], active_intervals_s=lifetimes,
                                       max_gap_s=options['max_gap_s'], rest_radius_px=options['rest_radius_px'], policy=policy))
        proposed.sort(key=lambda e: (e['observed_interval_s'][0], e['object_ids'], e['type']))
        pools = defaultdict(list)
        for event in proposed:
            pools[event['type'], tuple(event['object_ids'])].append(event)
        counts = defaultdict(int)
        event_checks = []
        request = read_json(review / 'records' / name / 'events/request.json')
        for task in request['tasks']:
            value = task['measurement']; key = value['type'], tuple(value['object_ids'])
            ordinal = counts[key]; counts[key] += 1
            choices = pools[key]
            actual = choices[ordinal]['observed_interval_s'] if ordinal < len(choices) else None
            expected = value['time_interval_s']
            within = actual is not None and max(abs(a-b) for a, b in zip(actual, expected)) <= request['event_tolerance_s']
            event_checks.append({'id': task['id'], 'type': key[0], 'objects': list(key[1]), 'occurrence': ordinal,
                                 'expected': expected, 'independent': actual, 'within_frozen_tolerance': within})
        count_checks = [{'type': key[0], 'objects': list(key[1]), 'expected': count, 'independent': len(pools[key]),
                         'match': count == len(pools[key])} for key, count in counts.items()]
        relation_checks = []
        request = read_json(review / 'records' / name / 'relationships/request.json')
        for task in request['tasks']:
            value = task['measurement']
            if value['type'] != 'relative_event_timing':
                continue
            intervals = []
            for oid in value['object_ids']:
                ids = [oid] + ([value['relative_to_object_id']] if value.get('relative_to_object_id') else [])
                choices = pools[value['event_type'], tuple(ids)]
                intervals.append(choices[value['occurrence']]['observed_interval_s'] if value['occurrence'] < len(choices) else None)
            cadence = [b['time_s']-a['time_s'] for oid in value['object_ids']
                       for a, b in zip(observations['objects'][next(i for i, o in enumerate(observations['objects']) if o['id']==oid)]['frames'],
                                      observations['objects'][next(i for i, o in enumerate(observations['objects']) if o['id']==oid)]['frames'][1:])]
            actual = relative_timing(*intervals, cadence, policy=policy) if all(intervals) else None
            aligned = actual is not None and actual['order'] == value['order'] and max(abs(a-b) for a, b in zip(actual['lag_interval_s'], value['lag_interval_s'])) <= request['event_tolerance_s']
            unresolved = value.get('timing_status') != 'resolved' or actual is not None and actual['timing_status'] != 'resolved'
            relation_checks.append({'id': task['id'], 'objects': value['object_ids'], 'event': value['event_type'],
                                    'definition_aligned': aligned, 'measurement_unresolved': unresolved,
                                    'expected_order': value['order'], 'expected_lag': value['lag_interval_s'], 'independent': actual})
        row = {'id': name, 'role': rec['role'], 'package_sha256': rec['package_sha256'], 'predicate_version': policy,
               'raw_sha256': file_sha256(raw / name / 'raw-proposals.json'),
               'public_event_options': options,
               'observation_scope_sha256': file_sha256(observed_file), 'production_coordinates_used': False,
               'event_checks': event_checks, 'count_checks': count_checks, 'timing_relations': relation_checks,
               'event_time_disagreements': sum(not c['within_frozen_tolerance'] for c in event_checks),
               'occurrence_count_disagreements': sum(not c['match'] for c in count_checks),
               'relation_disagreements': sum(not c['definition_aligned'] for c in relation_checks),
               'unresolved_relations': sum(c['measurement_unresolved'] for c in relation_checks)}
        records.append(row)
    result = {'kind': 'UnsignedEventDefinitionDiagnosis', 'status': 'diagnostic_only', 'records': records,
              'frozen_review_sha256': file_sha256(review / 'preparation.json'),
              'raw_manifest_sha256': file_sha256(raw / 'manifest.json'),
              'helper_sha256': file_sha256(Path(__file__)),
              'raw_event_review_sha256': file_sha256(Path(__file__).resolve().parents[1] / 'kiroclops/visual/raw_event_review.py'),
              'historical_acceptance_changed': False, 'thresholds_changed': False,
              'limitation': 'Definition-aligned interval aids still require visual inspection; >100ms uncertainty remains unresolved.'}
    write_json(output / 'result.json', result)
    print([{'record': r['id'], 'events': r['event_time_disagreements'], 'counts': r['occurrence_count_disagreements'],
            'relations': r['relation_disagreements'], 'unresolved': r['unresolved_relations']} for r in records])
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--review', type=Path, required=True); p.add_argument('--raw', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(); run(a.review, a.raw, a.output)
