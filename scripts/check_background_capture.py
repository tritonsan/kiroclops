"""Render a disposable baseline offscreen and compare all 240 natural PNGs."""
from pathlib import Path
import argparse
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.common import ROOT
from kiroclops.integration.background_capture_v1 import install_disposable_project
from kiroclops.integration.ground_runner import capture_project, import_project
from kiroclops.integration.storage import copy_project, project_hashes, read_json, sha256, utc_now, write_json


OBSERVER = '''extends Node
var samples: Array = []
var output_path: String
func _ready() -> void:
    var config = JSON.parse_string(FileAccess.get_file_as_string(OS.get_environment("KIROCLOPS_CONFIG")))
    output_path = str(config["output_dir"]).path_join("window-observer.json")
    RenderingServer.frame_post_draw.connect(_observe)
    _observe()
func _observe() -> void:
    var p = DisplayServer.window_get_position()
    samples.append({"no_focus": DisplayServer.window_get_flag(DisplayServer.WINDOW_FLAG_NO_FOCUS),
        "focused": DisplayServer.window_is_focused(), "position": [p.x, p.y],
        "mode": DisplayServer.window_get_mode(), "display_server": DisplayServer.get_name()})
    var out = FileAccess.open(output_path, FileAccess.WRITE)
    out.store_string(JSON.stringify({"samples": samples}))
    out.close()
'''


def check(output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Preflight output must be new; previous evidence is preserved')
    source = ROOT / 'sessions/cli-ground-01/baseline'
    previous = ROOT / 'sessions/cli-ground-01/baseline-runs/calibration/r06-x0-r1'
    manifest = read_json(previous / 'manifest.json')
    before = project_hashes(source)
    if before != manifest['provenance']['full_source_hashes_before']:
        raise ValueError('The historical baseline source no longer matches its capture')
    output.mkdir(parents=True)
    project = copy_project(source, output / 'project')
    startup = install_disposable_project(project, disposable_root=output, expected_source_hashes=before)
    write_json(output / 'startup.json', startup)
    (project / 'background_observer.gd').write_text(OBSERVER, encoding='utf-8')
    settings = project / 'project.godot'
    contents = settings.read_text(encoding='utf-8')
    if '[autoload]' not in contents:
        raise ValueError('Expected the pinned project autoload section')
    after_header = contents.index('[autoload]') + len('[autoload]')
    section_end = contents.find('\n[', after_header)
    if section_end < 0:
        section_end = len(contents)
    contents = contents[:section_end] + '\nKiroClopsWindowObserver="*res://background_observer.gd"\n' + contents[section_end:]
    settings.write_text(contents, encoding='utf-8')
    started = time.monotonic()
    import_project(project, game='slay', runtime_dir=output / 'runtime/import')
    run = capture_project(project, output / 'capture', game='slay', scenario=manifest['scenario'], variant='before')
    observer = read_json(run / 'window-observer.json')
    samples = observer['samples']
    window_pass = (len(samples) >= 240 and all(s['no_focus'] is True and s['focused'] is False
                   and s['position'][0] < -9000 and s['position'][1] < -9000
                   and s['mode'] == 0 and s['display_server'] != 'headless' for s in samples))
    different, images = [], {}
    for index in range(240):
        name = f'frames/{index:06d}.png'
        old, new = sha256(previous / name), sha256(run / name)
        images[name] = {'reference': old, 'offscreen': new}
        if old != new:
            different.append(index)
    result = {
        'schema_version': 1, 'recorded_at': utc_now(),
        'preflight_script_sha256': sha256(Path(__file__)),
        'status': 'pass' if not different and window_pass and project_hashes(source) == before else 'fail',
        'source': str(source), 'source_hashes': before, 'reference': str(previous),
        'reference_manifest_sha256': sha256(previous / 'manifest.json'),
        'capture_manifest_sha256': sha256(run / 'manifest.json'),
        'frame_count': 240, 'viewport': [640, 360], 'different_frames': different,
        'frame_hashes': images, 'elapsed_seconds': time.monotonic() - started,
        'window_status': 'pass' if window_pass else 'fail', 'window_samples': len(samples),
        'window_observer_sha256': sha256(run / 'window-observer.json'),
        'window_behavior': 'Own Godot window reports no_focus, unfocused, offscreen and real renderer on every observed draw.',
        'limitation': 'This receipt inspects only the launched Godot window; no other application is inspected.',
    }
    write_json(output / 'result.json', result)
    print(json.dumps({k: result[k] for k in ('status', 'frame_count', 'different_frames', 'elapsed_seconds')}, indent=2))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    raise SystemExit(check(parser.parse_args().output))
