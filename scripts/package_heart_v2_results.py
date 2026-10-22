"""Package terminal Heart v2 reports and exact playable copies without launching games.

This engineering helper lives outside the frozen model protocol. Every available
selected final candidate must already have independent-review.json. Acceptance
comes from the existing v2 report/audit replay, never from this packager. Existing
session reports are preserved as snapshots before the report builder refreshes
them; earlier cohort reports are never overwritten.
"""
from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kiroclops.integration.storage import copy_project, project_hashes, read_json, sha256, utc_now
from scripts.heart_v2_feedback_story import Evidence, build as build_feedback, digest, require, terminal_metadata

VERSION = 'heart-v2-results-package-v1'


def regular(path, *, within=None):
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        if component.exists() or component.is_symlink():
            info = component.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                    'Linked/reparse package paths are forbidden')
            if component.is_file():
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                        'Only independent regular package files are allowed')
    if within is not None:
        require(path.is_relative_to(Path(within).resolve()), 'Package path escaped its allowed root')
    return path


def files(directory):
    directory = regular(directory)
    result = []
    for current, directories, names in os.walk(directory, followlinks=False):
        for name in directories:
            regular(Path(current) / name, within=directory)
        for name in names:
            result.append(regular(Path(current) / name, within=directory))
    return sorted(result)


def tree_hashes(directory):
    return {path.relative_to(directory).as_posix(): sha256(path) for path in files(directory)}


def copy_tree(source, destination):
    source, destination = regular(source), regular(destination)
    require(not destination.exists(), 'Never replace an existing report snapshot')
    before = tree_hashes(source)
    destination.mkdir(parents=True)
    for relative, expected in before.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
        require(sha256(target) == expected, 'Copied report bytes differ')
    require(tree_hashes(source) == before and tree_hashes(destination) == before,
            'Report changed during copy')
    return before


def write_new(path, value):
    path = regular(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        if isinstance(value, str):
            stream.write(value)
        else:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.links += [value for key, value in attrs if key in ('href', 'src', 'poster') and value]


def validate_links(directory):
    """Static links plus the existing viewer's JSON video URLs; no verdict imports."""
    directory = regular(directory)
    count = 0
    for path in files(directory):
        if path.suffix.lower() != '.html':
            continue
        source = path.read_text(encoding='utf-8')
        parser = Links()
        parser.feed(source)
        values = list(parser.links)
        for match in re.finditer(r'const runs=(.*?);const roles=', source, flags=re.S):
            rows = json.loads(match.group(1))
            require(isinstance(rows, list), 'Dynamic video rows are malformed')
            for row in rows:
                require(isinstance(row.get('videos'), dict), 'Dynamic video map is missing')
                values.extend(row['videos'].values())
        for value in values:
            require(isinstance(value, str), 'Link is not text')
            if value.startswith('#'):
                continue
            parsed = urlsplit(value)
            decoded = unquote(parsed.path)
            require(not parsed.scheme and not parsed.netloc and decoded and '\\' not in decoded
                    and not Path(decoded).is_absolute(), 'Only portable relative local report links are allowed')
            target = regular((path.parent / decoded).resolve(), within=directory)
            require(target.is_file(), f'Missing local link from {path.name}: {value}')
            count += 1
    return {'status': 'pass', 'local_links_checked': count, 'dynamic_video_urls_checked': True}


def playable(directory, state, destination, status, evidence):
    """Copy exactly the selected source; only write a launcher beside the copy."""
    cid = state.get('selected_candidate_id')
    require(isinstance(cid, str) and re.fullmatch(r'candidate-\d+', cid), 'Selected candidate identity is invalid')
    candidate = evidence.data(directory / 'candidates' / cid / 'candidate.json')
    source = directory / 'candidates' / cid / 'project'
    require(candidate.get('candidate_id') == cid and candidate.get('session_id') == state['session_id']
            and Path(candidate['project_path']).resolve() == source.resolve()
            and digest(candidate['source_hashes']) == candidate['source_sha256']
            and project_hashes(source) == candidate['source_hashes'], 'Selected playable source binding differs')
    require(not destination.exists(), 'Playable bundle already exists')
    destination.mkdir(parents=True)
    project = copy_project(source, destination / 'project')
    require(project_hashes(source) == project_hashes(project) == candidate['source_hashes'],
            'Playable copy differs from the selected source')
    copied = tree_hashes(project)
    for relative, value in copied.items():
        require(sha256(source / relative) == value, 'Copied generated sidecar differs from its source')
    license_path = project / 'LICENSE'
    require(license_path.is_file() and 'Permission is hereby granted' in license_path.read_text(encoding='utf-8'),
            'Original MIT license is missing')
    engine_relative = os.path.relpath(evidence.root / '.tools/godot4/Godot_v4.5.1-stable_win64.exe', destination).replace('\\', '/')
    require("'" not in engine_relative, 'Unexpected quote in the engine path')
    launcher = """param([string]$GodotPath = '')
$ErrorActionPreference = 'Stop'
if (-not $GodotPath) {
    $GodotPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '%s')).Path
}
$gameProject = Join-Path $PSScriptRoot 'project'
if (-not (Test-Path -LiteralPath $GodotPath -PathType Leaf)) { throw 'Godot 4.5.1 executable is missing.' }
& $GodotPath --headless --editor --import --path $gameProject
if ($LASTEXITCODE -ne 0) { throw 'Godot asset import failed.' }
& $GodotPath --path $gameProject
""" % engine_relative
    write_new(destination / 'PLAY.ps1', launcher)
    final = state.get('final') or {}
    note = (f'# {state["session_id"]}\n\n'
            f'Independent chain acceptance: **{status}**. Ground-stage numerical status: **{final.get("numerical_status", "not_run")}**. '
            f'Automatic integrity: **{final.get("automatic_integrity_status", "not_run")}**.\n\n'
            'A failed or pending result is preserved for inspection and is not a fully accepted version.\n\n'
            'Run `PLAY.ps1` yourself in PowerShell to import the assets and open the game. '
            'The packaging operation never launches Godot. The supplied Godot 4.5.1 executable is located relative '
            'to this workspace; if moving this bundle, use `PLAY.ps1 -GodotPath <path>`.\n\n'
            'Controls: A/D or left/right arrows; jump with Space, W, up arrow or J; Enter to start.\n\n'
            f'Selected candidate: `{cid}`. Source digest: `{candidate["source_sha256"]}`. '
            'The original MIT license is preserved at `project/LICENSE`.\n')
    write_new(destination / 'README.md', note)
    provenance = {'version': VERSION, 'created_at': utc_now(), 'session': str(directory),
                  'candidate_id': cid, 'source_sha256': candidate['source_sha256'],
                  'source_hashes': candidate['source_hashes'], 'copied_files_sha256': copied,
                  'copy_matches_selected_source': True, 'game_launched_by_packager': False,
                  'independent_chain_acceptance': status,
                  'numerical_status': final.get('numerical_status', 'not_run'),
                  'automatic_integrity_status': final.get('automatic_integrity_status', 'not_run'),
                  'license_sha256': sha256(license_path), 'launcher_sha256': sha256(destination / 'PLAY.ps1')}
    write_new(destination / 'provenance.json', provenance)
    return provenance


def aggregate(values):
    return 'pass' if values and all(value == 'pass' for value in values) else 'fail' if any(
        value in {'fail', 'not_run'} for value in values) else 'pending'


def landing(summary, story, rows):
    esc = html.escape
    body = [f'<h1>KiroClops — Heart: new integrity check</h1><p>GPT‑5.6 Sol / max · CLI · Always Allow</p>',
            f'<p><strong>Numerical + automatic check: {summary["numerical_and_integrity_passed"]}/3 chains. '
            f'Independent acceptance: {summary["independent_passed"]}/3 chains.</strong></p>',
            '<p>The new check tests, in both directions, whether the wall jump overlaps with the short jump allowance '
            'after leaving the ledge. Candidate error, tool feedback and the subsequent code edit are recorded separately.</p>',
            '<p><a href="report/index.html">All videos and detailed comparisons</a> · '
            '<a href="feedback-story.json">Trial flow bound to the source and the conversation record</a> · '
            '<a href="package-summary.json">Package summary</a></p>',
            '<table><thead><tr><th>Chain</th><th>Numerical</th><th>Integrity</th><th>Independent acceptance</th>'
            '<th>Local reports</th><th>Playable copy</th></tr></thead><tbody>']
    for row in rows:
        reports = ' · '.join(f'<a href="{esc(value["report"], quote=True)}">{esc(stage)}</a>'
                             for stage, value in row['stages'].items() if value.get('report'))
        play = ('<a href="' + esc(row['playable_readme'], quote=True) + '">README</a> · <a href="'
                + esc(row['playable_launcher'], quote=True) + '">PLAY.ps1</a>') if row.get('playable_readme') else 'Not produced'
        body.append('<tr>' + ''.join(f'<td>{esc(str(row[key]))}</td>' for key in
                                    ('chain_id', 'numerical_status', 'automatic_integrity_status', 'independent_acceptance'))
                    + f'<td>{reports}</td><td>{play}</td></tr>')
    body.append('</tbody></table><h2>Attempts after feedback</h2>')
    successes = story['summary']['wall_feedback_edit_success_stages']
    body.append(f'<p>In {successes} stages, the sequence of recorded wall/ledge error feedback → completed local code '
                'edit → eligible next candidate was observed. This ordering does not prove the internal reasoning of the model; '
                'an independent tool-log audit is required for code authorship and unattended operation.</p>')
    for stage in story['stages']:
        body.append(f'<h3>{esc(stage["session_id"])}</h3>')
        candidates = stage.get('candidates', [])
        if not candidates:
            body.append(f'<p>Not run, or no candidate was evaluated: {esc(str(stage.get("reason", stage.get("story"))))}</p>')
            continue
        body.append('<ul>')
        for candidate in candidates:
            failures = ', '.join(candidate.get('failure_case_ids', [])) or candidate.get('failure_class') or 'not reported'
            selected = ' · selected version' if candidate['selected'] else ''
            body.append(f'<li>{esc(candidate["candidate_id"])}: eligibility={str(candidate["eligible"]).lower()}, '
                        f'integrity={esc(str(candidate.get("automatic_integrity_status")))}{selected}; '
                        f'error: {esc(str(failures))}.</li>')
        body.append('</ul>')
    body.append('<p>The first candidate being eligible does not show that a fix was made in response to error feedback. '
                'Failed and pending results are preserved. This package does not launch the engine; the game opens only when '
                'the user runs PLAY.ps1.</p>')
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' \
           '<title>KiroClops Heart v2 results</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;line-height:1.55}' \
           'table{width:100%;border-collapse:collapse}th,td{border:1px solid #ccd2da;padding:10px;text-align:left}a{color:#145aaf}' \
           'h2{margin-top:36px}h3{font-size:18px}li{overflow-wrap:anywhere}</style><body>' + ''.join(body) + '</body></html>\n'


def package(registry_path):
    from kiroclops.portable_v2 import reporting
    from kiroclops.portable_v2.cohort import verify_registry
    registry_path = regular(Path(registry_path).resolve(), within=ROOT)
    initial = read_json(registry_path)
    require(initial.get('protocol_id') == 'heart-transfer-chain-v2', 'Only the Heart v2 protocol can be packaged')
    evidence = Evidence(initial['root'])
    require(evidence.root == ROOT, 'Package root must be this workspace')
    evidence.bind(Path(__file__))
    registry = evidence.data(registry_path)
    run = evidence.data(ROOT / 'artifacts/phase2c-v2/cohorts' / registry['cohort_id'] / 'run.json')
    require(run.get('registry_sha256') == sha256(registry_path), 'Run and registry differ')
    prepared = terminal_metadata(registry, run, evidence)
    for _, _, _, directory, state, _ in prepared:
        if state and state.get('selected_candidate_id'):
            review = directory / 'final/independent-review.json'
            require(review.is_file(), f'Independent review is still missing: {directory.name}')
            evidence.bind(review)
    require(verify_registry(registry_path) == registry, 'Frozen registry failed verification')
    output = regular(ROOT / 'artifacts/phase2c-v2/reports' / (registry['cohort_id'] + '-delivery'))
    native_report = regular(ROOT / 'artifacts/phase2c-v2/reports' / registry['cohort_id'])
    require(not output.exists() and not native_report.exists(), 'Preserve existing report packages; this is a single fresh build')
    story = build_feedback(registry_path)
    for path, expected in story['evidence_files'].items():
        bound = evidence.bind(path)
        require(evidence.files[str(bound)] == expected, 'Feedback story evidence changed before packaging')
    output.mkdir(parents=True)
    write_new(output / 'packaging-start.json', {'version': VERSION, 'started_at': utc_now(),
                                               'registry_sha256': sha256(registry_path), 'game_launch_authorized': False})
    snapshots = {}
    for _, _, _, directory, state, _ in prepared:
        if state and (directory / 'report').is_dir():
            snapshots[directory.name] = copy_tree(directory / 'report', output / 'pre-report-snapshots' / directory.name)
    # This is the only native report-builder invocation. It runs report/audit
    # replay and video encoding, not Kiro, Godot, or a game launch.
    path = reporting.build_cohort_report(registry_path)
    require(path.resolve() == native_report / 'index.html', 'Report builder wrote to an unexpected location')
    copy_tree(native_report, output / 'report')
    core_summary = read_json(output / 'report/summary.json')
    require(core_summary.get('cohort_id') == registry['cohort_id'] and len(core_summary.get('chains', [])) == 3,
            'Cohort report summary is incomplete')
    actual = {(chain, stage): (directory, state) for chain, stage, _, directory, state, _ in prepared}
    rows = []
    for registered, reviewed in zip(registry['chains'], core_summary['chains']):
        require(registered['chain_id'] == reviewed['chain_id'], 'Cohort report chain order differs')
        stages = {}
        for stage in ('jump', 'ground'):
            directory, state = actual[registered['chain_id'], stage]
            final = (state or {}).get('final') or {}
            review = reviewed['stages'][stage]
            stages[stage] = {'numerical_status': final.get('numerical_status', 'not_run'),
                             'automatic_integrity_status': final.get('automatic_integrity_status', 'not_run'),
                             'independent_acceptance': review['status'],
                             'report': 'report/' + review['report'] if review.get('report') else None}
            if state and (directory / 'final/independent-review.json').is_file():
                destination = output / 'evidence' / directory.name / 'independent-review.json'
                write_new(destination, (directory / 'final/independent-review.json').read_text(encoding='utf-8'))
        row = {'chain_id': registered['chain_id'], 'stages': stages,
               'numerical_status': aggregate([value['numerical_status'] for value in stages.values()]),
               'automatic_integrity_status': aggregate([value['automatic_integrity_status'] for value in stages.values()]),
               'independent_acceptance': reviewed['status']}
        directory, state = actual[registered['chain_id'], 'ground']
        if state and state.get('selected_candidate_id'):
            destination = output / 'playable' / registered['chain_id']
            playable(directory, state, destination, row['independent_acceptance'], evidence)
            row['playable_readme'] = (destination / 'README.md').relative_to(output).as_posix()
            row['playable_launcher'] = (destination / 'PLAY.ps1').relative_to(output).as_posix()
        rows.append(row)
    summary = {'version': VERSION, 'cohort_id': registry['cohort_id'], 'created_at': utc_now(),
               'required_chains': 3, 'independent_passed': core_summary['passed'],
               'numerical_and_integrity_passed': sum(row['numerical_status'] == row['automatic_integrity_status'] == 'pass' for row in rows),
               'chains': rows, 'original_session_report_snapshots': snapshots,
               'acceptance_source': 'portable_v2.reporting with its independent audit replay',
               'games_launched': False, 'model_calls': 0}
    write_new(output / 'feedback-story.json', story)
    write_new(output / 'package-summary.json', summary)
    write_new(output / 'index.html', landing(summary, story, rows))
    links = validate_links(output)
    evidence.verify()
    require(verify_registry(registry_path) == registry, 'Frozen registry changed during packaging')
    write_new(output / 'package-validation.json', {'version': VERSION, 'completed_at': utc_now(),
               'status': 'pass', 'links': links, 'input_evidence': evidence.files,
               'output_files_sha256': tree_hashes(output), 'games_launched': False, 'model_calls': 0})
    return {'status': 'complete', 'index': str(output / 'index.html'),
            'independent_passed': summary['independent_passed'], 'required_chains': 3,
            'numerical_and_integrity_passed': summary['numerical_and_integrity_passed'], 'links': links}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(package(args.registry), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({'status': 'unavailable', 'reason': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
