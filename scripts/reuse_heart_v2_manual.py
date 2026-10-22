"""Operator-only, exact-PNG manual reuse for the six heart-v2-01 stages.

No model or engine runs. No label comes from tracked or engine coordinates.
Only after a stage has finished, historical independently marked pixels can be
rebound to equal native PNGs at the same input scenario and frame. Missing
observations stay pending and get raw-only inspection sheets.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.integration.storage import read_json, sha256, write_json, utc_now
from kiroclops.portable_v2.review import contact_sheet, validate_observations
from kiroclops.portable_v2.session import ordinary, digest

COHORT = 'heart-v2-01'
STAGES = {f'heart-{COHORT}-{n:02}-{stage}': stage for n in (1, 2, 3) for stage in ('jump', 'ground')}
EVENTS = ('takeoff_frame', 'apex_frame', 'landing_frame')
REQUIRED_ROLES = {'reference', 'before', 'after'}
SHEET_FRAMES = (0, 28, 30, 32, 36, 40, 44, 48, 56, 64, 72, 80, 88, 96)


def scoped(path):
    path = ordinary(path)
    if not path.is_relative_to(ROOT):
        raise ValueError('Evidence must remain in the project workspace')
    return path


class RawEvidence:
    """Cache only repeated immutable metadata/hashes during one operator call."""
    def __init__(self):
        self.files = {}
        self.manifests = {}

    def bind(self, path):
        path = scoped(path)
        key = str(path)
        if key not in self.files:
            self.files[key] = sha256(path)
        return self.files[key]

    def frame(self, run, index):
        if type(index) is not int or not 0 <= index < 240:
            raise ValueError('Manual frame index must be within the recorded 240 frames')
        return self.bind(scoped(run) / 'frames' / f'{index:06d}.png')

    def manifest(self, run):
        run = scoped(run)
        if str(run) not in self.manifests:
            path = run / 'manifest.json'; self.bind(path)
            self.manifests[str(run)] = read_json(path)
        return self.manifests[str(run)]

    def key(self, run):
        manifest = self.manifest(run)
        scenario = manifest['scenario']
        # Repeats may share pixels, but their hashes count only once. Input
        # timing/geometry and the semantic frame are never shifted or fitted.
        identity = {'game': manifest['game'], 'viewport': manifest['viewport'], 'fps': manifest['fps'],
            'frame_count': manifest['frame_count'], 'id': scenario.get('id',
                f"h{scenario['hold_ticks']:02}" if 'hold_ticks' in scenario else None),
            'spawn_offset': scenario.get('spawn_offset', 0),
            'input': {key: scenario[key] for key in ('events', 'hold_ticks', 'start_tick') if key in scenario}}
        return json.dumps(identity, sort_keys=True, separators=(',', ':'))

    def unchanged(self):
        if any(sha256(path) != expected for path, expected in self.files.items()):
            raise ValueError('Raw/manual evidence changed while reuse was being prepared')


def source_pool(paths, evidence):
    pool = defaultdict(lambda: {'points': [], 'events': []})
    issues = []
    for path in paths:
        path = scoped(path); file_hash = evidence.bind(path)
        record = read_json(path)
        for mechanic, roles in record.get('groups', {}).items():
            if mechanic not in ('jump', 'ground'):
                raise ValueError('Unknown manual source mechanic')
            for role, values in roles.items():
                for kind in ('points', 'events'):
                    for index, value in enumerate(values.get(kind, [])):
                        try:
                            if value.get('source') not in ('human', 'assistant_visual'):
                                raise ValueError('Only explicitly independent human/visual labels can be reused')
                            run = scoped(value['run']); key = evidence.key(run)
                            if kind == 'points':
                                if (type(value.get('x')) not in (int, float) or type(value.get('y')) not in (int, float)
                                        or not all(math.isfinite(value[k]) for k in ('x', 'y'))):
                                    raise ValueError('Invalid manual coordinates')
                                if evidence.frame(run, value['frame_index']) != value.get('frame_sha256'):
                                    raise ValueError('Original marked PNG changed')
                                if not set(value.get('categories', [])) <= {'brake', 'reverse', 'flip'}:
                                    raise ValueError('Unknown original manual category')
                            else:
                                if any(type(value.get(k)) is not int or not 0 <= value[k] < 240 for k in EVENTS):
                                    raise ValueError('Invalid original manual event frame')
                                needed = {str(i) for name in EVENTS for i in range(max(0, value[name]-2), min(240, value[name]+3))}
                                bound = value.get('frame_hashes', {})
                                if not needed <= set(bound):
                                    raise ValueError('Original event lacks a complete +/-2 frame neighborhood')
                                if any(evidence.frame(run, int(i)) != expected for i, expected in bound.items()):
                                    raise ValueError('Original event neighborhood PNG changed')
                            pool[mechanic, key][kind].append({'value': deepcopy(value), 'origin': {
                                'observations_path': str(path), 'observations_sha256': file_hash,
                                'mechanic': mechanic, 'role': role, 'kind': kind, 'index': index,
                                'run': str(run), 'rule': 'Same input/frame and exact complete PNG; no coordinate transformation'}})
                        except (OSError, ValueError, KeyError, TypeError) as error:
                            issues.append({'source': str(path), 'mechanic': mechanic, 'role': role,
                                           'kind': kind, 'index': index, 'reason': str(error)})
    return pool, issues


def reuse_group(analysis_paths, mechanic, pool, evidence):
    matches, events, misses = [], [], []
    unique_runs = []
    for path in analysis_paths:
        path = scoped(path); evidence.bind(path)
        # Only the run directory and its manifest binding are extracted here.
        # Frame positions, measured events, metrics and motor data author no mark.
        metadata = read_json(path)
        run = scoped(metadata['run_id'])
        if metadata.get('provenance', {}).get('manifest_sha256') != evidence.bind(run / 'manifest.json'):
            raise ValueError('Target analysis manifest linkage changed')
        unique_runs.append(run)
        candidates = pool.get((mechanic, evidence.key(run)), {'points': [], 'events': []})
        if not any(candidates.values()):
            misses.append({'run': str(run), 'kind': 'scenario', 'reason': 'No independently marked matching input scenario'})
        for kind in ('points', 'events'):
            for source in candidates[kind]:
                value = source['value']
                if kind == 'points':
                    matched = evidence.frame(run, value['frame_index']) == value['frame_sha256']
                else:
                    # Check every supplied bound frame, including all mandatory
                    # +/-2 neighborhoods. No event time is inferred or shifted.
                    matched = all(evidence.frame(run, int(i)) == h for i, h in value['frame_hashes'].items())
                if not matched:
                    misses.append({'run': str(run), 'kind': kind, 'frame_index': value.get('frame_index'),
                        'event_frames': {k: value[k] for k in EVENTS} if kind == 'events' else None,
                        'source': source['origin'], 'reason': 'Native PNG differs; fresh visual observation required'})
                    continue
                copied = deepcopy(value)
                copied['run'] = str(run)
                copied['reuse_origin'] = {**source['origin'], 'prior_reuse_origin': value.get('reuse_origin')}
                (matches if kind == 'points' else events).append(copied)
    # Conflicting historical coordinates are not resolved with tracker output.
    labels = defaultdict(set)
    for point in matches:
        labels[point['frame_sha256']].add((point['x'], point['y']))
    conflicts = {h for h, values in labels.items() if len(values) != 1}
    selected, seen = [], set()
    for point in sorted(matches, key=lambda p: (-len(p.get('categories', [])), p['run'], p['frame_index'])):
        h = point['frame_sha256']
        if h not in conflicts and h not in seen:
            selected.append(point); seen.add(h)
    event_labels = defaultdict(set)
    for event in events:
        event_labels[event['run']].add(tuple(event[k] for k in EVENTS))
    event_seen, selected_events = set(), []
    for event in events:
        if len(event_labels[event['run']]) != 1:
            continue
        # Identical repeated neighborhoods do not manufacture more event proof.
        key = (tuple(event[k] for k in EVENTS), tuple(sorted(event['frame_hashes'].items())))
        if key not in event_seen:
            event_seen.add(key); selected_events.append(event)
    group = {'points': selected, 'events': selected_events,
        'analysis_files': {str(scoped(p)): evidence.bind(p) for p in analysis_paths},
        'method': 'Operator-only exact raw-PNG reuse; original coordinates, event ticks and categories unchanged.'}
    checked = validate_observations(analysis_paths, group, minimum=20, require_events=mechanic == 'jump',
        required_categories=('brake', 'reverse', 'flip') if mechanic == 'ground' else ())
    gaps = {'status': 'ready_for_review' if checked['status'] == 'pass' else 'pending',
        'distinct_points': len(seen), 'minimum': 20, 'points_missing': max(0, 20-len(seen)),
        'event_count': len(selected_events), 'events_missing': mechanic == 'jump' and not selected_events,
        'missing_categories': sorted(set(('brake', 'reverse', 'flip') if mechanic == 'ground' else ()) - set(checked['categories'])),
        'conflicting_pixel_hashes': sorted(conflicts),
        'conflicting_event_runs': sorted(run for run, values in event_labels.items() if len(values) != 1),
        'replay_status': checked['status'], 'median_error_px': checked['median_error_px'],
        'event_replay': checked['events'], 'replay_errors': checked['errors'], 'unmatched': misses}
    return group, checked, gaps, unique_runs


def prepare(session, output, *, sources=None, sheets=True):
    directory = scoped(session)
    if directory.parent != ROOT / 'sessions' or directory.name not in STAGES:
        raise ValueError('Only the six heart-v2-01 sessions are supported')
    output = scoped(output)
    if not output.is_relative_to(ROOT / 'artifacts/phase2c-v2') or output.exists():
        raise ValueError('Use a fresh operator output directory under artifacts/phase2c-v2')
    output.mkdir(parents=True)
    state_path = directory / 'session.json'
    pending = {'status': 'pending', 'session_id': directory.name, 'created_at': utc_now(),
               'scope': 'Operator-only manual reuse; never a source audit or full acceptance.'}
    if not state_path.is_file():
        pending['reason'] = 'Future stage has not been created; no frames inspected'
        write_json(output / 'summary.json', pending); return pending
    state = read_json(state_path)
    launch_path = directory / 'kiro/launch.json'
    launch = read_json(launch_path) if launch_path.is_file() else {}
    if (state.get('status') != 'finished' or state.get('active_job_id') or not state.get('sealed_at')
            or launch.get('status') != 'completed'):
        pending['reason'] = 'Stage is not sealed and terminal; live/held-out frames were not inspected'
        write_json(output / 'summary.json', pending); return pending
    stage = STAGES[directory.name]
    if state.get('session_id') != directory.name or state.get('experiment_id') != f'heart-{stage}-v2':
        raise ValueError('Stage identity differs from the supported experiment')
    evidence = RawEvidence()
    evidence.bind(state_path); evidence.bind(launch_path)
    cid = state['selected_candidate_id']
    if not isinstance(cid, str) or not re.fullmatch(r'candidate-\d+', cid):
        raise ValueError('Selected candidate ID is invalid')
    candidate_path = directory / 'candidates' / cid / 'candidate.json'
    evidence.bind(candidate_path); candidate = read_json(candidate_path)
    comparison_path = directory / 'final/comparison.json'
    comparison_hash = evidence.bind(comparison_path); comparison = read_json(comparison_path)
    if (candidate.get('candidate_id') != cid or candidate.get('source_sha256') != digest(candidate['source_hashes'])
            or comparison.get('session_id') != directory.name or comparison.get('candidate_id') != cid
            or comparison.get('source_sha256') != candidate['source_sha256']):
        raise ValueError('Selected candidate/comparison metadata binding differs')
    mechanics = {'jump'} if stage == 'jump' else {'jump', 'ground'}
    if set(comparison.get('runs', {})) != mechanics:
        raise ValueError('Final mechanic groups are incomplete')
    source_paths = sources or [ROOT / f'artifacts/phase2c/cohorts/heart-02/manual/{n:02}-{kind}-observations.json'
                              for n in (1, 2, 3) for kind in ('jump', 'ground')]
    pool, issues = source_pool(source_paths, evidence)
    result = {'schema_version': 1, 'candidate_id': cid, 'source_sha256': candidate['source_sha256'],
        'comparison_sha256': comparison_hash, 'created_at': utc_now(), 'groups': {}, 'replay': {},
        'scope': pending['scope'], 'method': 'Independent historical marks; same input/frame; exact raw PNG equality only.',
        'reuse_source_files': {str(scoped(p)): evidence.bind(p) for p in source_paths}}
    gaps, sheet_paths = {}, []
    for mechanic in sorted(mechanics):
        if set(comparison['runs'][mechanic]) != REQUIRED_ROLES:
            raise ValueError('Final role groups are incomplete')
        result['groups'][mechanic], result['replay'][mechanic], gaps[mechanic] = {}, {}, {}
        for role, paths in comparison['runs'][mechanic].items():
            if len(paths) != (12 if mechanic == 'jump' else 18) or len(set(paths)) != len(paths):
                raise ValueError('Final run matrix is incomplete or duplicated')
            group, checked, missing, runs = reuse_group(paths, mechanic, pool, evidence)
            result['groups'][mechanic][role] = group
            result['replay'][mechanic][role] = checked
            gaps[mechanic][role] = missing
            if sheets and missing['status'] == 'pending':
                represented = set()
                for run in runs:
                    key = evidence.key(run)
                    if key in represented:
                        continue
                    represented.add(key)
                    # One representative per scenario/input; never use tracked
                    # apex/landing coordinates to pick or crop an inspection.
                    manifest = evidence.manifest(run)
                    scenario = manifest['scenario']
                    name = scenario.get('id', f"h{scenario['hold_ticks']:02}" if 'hold_ticks' in scenario else 'jump')
                    offset = manifest['scenario'].get('spawn_offset', 0)
                    if not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or type(offset) is not int:
                        raise ValueError('Unsafe scenario identifier')
                    for page, start in enumerate(range(0, len(SHEET_FRAMES), 12)):
                        file = output / 'sheets' / f'{mechanic}-{role}-{name}-x{offset}-run{len(represented):02}-{page:02}.png'
                        indices = list(SHEET_FRAMES[start:start+12])
                        for index in indices: evidence.frame(run, index)
                        contact_sheet(run, indices, file, crop=(0, 0, *manifest['viewport']), scale=2, columns=3)
                        sheet_paths.append(str(file))
    evidence.unchanged()
    result['reuse_evidence_files'] = evidence.files
    write_json(output / 'observations.json', result)
    summary = {**pending, 'status': 'ready_for_review' if all(v['status'] == 'ready_for_review' for g in gaps.values() for v in g.values()) else 'pending',
        'candidate_id': cid, 'source_sha256': candidate['source_sha256'], 'groups': gaps,
        'source_observation_issues': issues, 'raw_contact_sheets': sheet_paths,
        'observations_path': str(output / 'observations.json'), 'observations_sha256': sha256(output / 'observations.json'),
        'script_sha256': sha256(Path(__file__)), 'no_model_or_engine': True,
        'limitation': 'Unmatched/ambiguous marks require new human or assistant visual inspection; ready_for_review is not independent acceptance.'}
    write_json(output / 'summary.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source', action='append', type=Path)
    parser.add_argument('--no-sheets', action='store_true')
    args = parser.parse_args(argv)
    result = prepare(args.session, args.output, sources=args.source, sheets=not args.no_sheets)
    print(json.dumps({k: result.get(k) for k in ('status', 'session_id', 'candidate_id', 'reason', 'observations_path')}, ensure_ascii=False))
    print(str(args.output.resolve() / 'summary.json'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
