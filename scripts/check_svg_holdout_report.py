"""Headless interaction check for the standalone supplemental report."""
from pathlib import Path
import argparse
import json
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.local_engine.environment import configure_environment
from kiroclops.visual.schema import read_json, write_json, file_sha256


def run(report, output):
    from playwright.sync_api import sync_playwright

    report, output = Path(report).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Preserve previous verification output')
    output.mkdir(parents=True)
    configure_environment()
    errors, requests, cases, videos = [], [], [], []
    data = read_json(report / 'report.json')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1080})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('requestfailed', lambda request: requests.append({'url':request.url,'error':request.failure}))
            page.goto((report / 'index.html').as_uri(), wait_until='load')
            assert page.locator('#rows tr').count() == 12
            assert page.locator('#scenario option').count() == 12
            for index, case in enumerate(data['cases']):
                page.select_option('#scenario', str(index))
                page.wait_for_function("[...document.querySelectorAll('#images img')].length === 3 && [...document.querySelectorAll('#images img')].every(i=>i.complete && i.naturalWidth>0)")
                assert page.locator('#curve path').count() == 3
                assert page.input_value('#object') == 'c'
                initial = page.locator('#clock').inner_text()
                page.click('#next')
                assert page.locator('#clock').inner_text() != initial
                cases.append({'scenario_id': case['id'], 'three_images_loaded': True, 'curve_series': 3, 'frame_step': 'pass'})
            page.select_option('#scenario', '0')
            initial = page.locator('#clock').inner_text()
            page.click('#play')
            page.wait_for_function("initial => document.querySelector('#clock').textContent !== initial", arg=initial)
            page.click('#pause')
            paused = page.locator('#clock').inner_text()
            page.wait_for_timeout(100)
            assert page.locator('#clock').inner_text() == paused
            page.locator('#scrub').evaluate("e => {e.value = '0.6';e.dispatchEvent(new Event('input'))}")
            page.wait_for_function("[...document.querySelectorAll('#images img')].every(i=>i.complete && i.naturalWidth>0)")
            page.screenshot(path=str(output / 'report.png'), full_page=True)
            assert not errors, errors
            # Rapid playback can replace an in-flight local frame request.
            # Only an explicitly aborted request for an existing packaged PNG
            # is benign; absent media and all other transport errors still fail.
            from urllib.parse import urlparse, unquote
            unexpected=[]
            for request in requests:
                parsed=urlparse(request['url']);raw=unquote(parsed.path)
                if sys.platform=='win32' and raw.startswith('/'):
                    raw=raw[1:]
                target=Path(raw).resolve()
                if not (request['error']=='net::ERR_ABORTED' and parsed.scheme=='file'
                        and target.is_relative_to(report/'media') and target.suffix=='.png' and target.is_file()):
                    unexpected.append(request)
            assert not unexpected, unexpected
        finally:
            browser.close()
    probe = shutil.which('ffprobe')
    if not probe:
        raise ValueError('ffprobe required to verify presentation video streams')
    for case in data['cases']:
        for role, media in case['media'].items():
            path = report / media['video']
            result = subprocess.run([probe, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                'stream=codec_name,width,height:format=duration', '-of', 'json', str(path)], check=True,
                capture_output=True, text=True)
            value = json.loads(result.stdout)
            stream = value['streams'][0]
            assert stream['codec_name'] == 'h264' and float(value['format']['duration']) > 0
            videos.append({'scenario_id': case['id'], 'role': role, 'sha256': file_sha256(path), **stream})
    result = {'status': 'pass', 'report_page_sha256': file_sha256(report / 'index.html'),
        'standalone_file_url': True, 'browser_headless': True, 'scenarios': cases,
        'play_pause_scrub': 'pass', 'videos': videos, 'page_errors': errors, 'failed_requests': unexpected,
        'superseded_existing_frame_requests': requests,
        'scope': 'Report UI and video packaging verification; not independent measurement acceptance.'}
    write_json(output / 'result.json', result)
    print({'status': 'pass', 'scenarios': len(cases), 'videos': len(videos)}, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.report, args.output)
