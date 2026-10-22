"""Check every supplemental report scenario in a moved, offline directory."""
from pathlib import Path
import argparse
import re
import shutil
import sys
import tempfile
from urllib.parse import unquote
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiroclops.local_engine.environment import configure_environment
from kiroclops.visual.schema import read_json, write_json


def run(report, output):
    report, output = Path(report).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    configure_environment()
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory(prefix='KiroClops moved report café ') as tmp:
        moved = Path(tmp) / 'offline report naïve'
        shutil.copytree(report, moved)
        for path in moved.rglob('*.json'):
            assert not re.search(r'(?<![A-Za-z0-9_])[A-Za-z]:[\\/]', path.read_text('utf-8')), path.name
        data = read_json(moved / 'report.json')
        assert len(data['groups']) == 12
        videos = [m['video'] for g in data['groups'] for m in g['roles'].values()]
        assert len(videos) == 36
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1080})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto((moved / 'index.html').as_uri())
            page.screenshot(path=str(output / 'summary.png'))
            groups = []
            for index, group in enumerate(data['groups']):
                page.locator('#scenario').select_option(str(index))
                page.wait_for_function("Array.from(document.querySelectorAll('#media img')).length===3 && Array.from(document.querySelectorAll('#media img')).every(i=>i.complete&&i.naturalWidth>0)")
                before = page.locator('#time').inner_text()
                page.locator('#next').click()
                after = page.locator('#time').inner_text()
                assert before != after, group['id']
                page.locator('#slider').evaluate("e=>{e.value=.15;e.dispatchEvent(new Event('input'))}")
                page.wait_for_function("Array.from(document.querySelectorAll('#media img')).every(i=>i.complete&&i.naturalWidth>0)")
                assert page.locator('#overlay-reference circle').count() == 3
                for href in page.locator('a').evaluate_all("a=>a.map(n=>n.getAttribute('href'))"):
                    target = (moved / unquote(href)).resolve()
                    assert target.is_relative_to(moved) and target.is_file(), href
                groups.append({'id': group['id'], 'frame_advanced': True, 'images_loaded': 3})
            page.locator('#media').screenshot(path=str(output / 'comparison.png'))
            before_play = page.locator('#time').inner_text()
            page.locator('#play').click()
            page.wait_for_timeout(150)
            page.locator('#play').click()
            assert page.locator('#time').inner_text() != before_play
            dimensions = []
            for relative in videos:
                dimensions.append(page.evaluate("url=>new Promise((resolve,reject)=>{let v=document.createElement('video');v.onloadedmetadata=()=>resolve({width:v.videoWidth,height:v.videoHeight,duration:v.duration});v.onerror=()=>reject('Video failed: '+url);v.src=url})", (moved / relative).as_uri()))
            assert all(v['width'] > 0 and v['duration'] > 0 for v in dimensions)
            assert not errors, errors
            browser.close()
    result = {'status': 'pass', 'scenarios': groups, 'videos': dimensions, 'javascript_errors': errors,
              'local_links_pass': True, 'frame_step_pass': True, 'play_pause_pass': True,
              'moved_to_unicode_space_directory': True, 'server_required': False,
              'absolute_windows_paths_absent': True,
              'independent_measurement_review_performed': False}
    write_json(output / 'result.json', result)
    return {'status': result['status'], 'scenarios': len(groups), 'videos': len(dimensions)}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    print(run(args.report, args.output), flush=True)
