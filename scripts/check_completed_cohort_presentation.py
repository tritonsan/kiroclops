"""Check the completed portable report in hidden Chromium, without a server."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256
from playwright.sync_api import sync_playwright


def main():
    base = ROOT/'artifacts/phase3a'
    report = base/'completion-original-cohort-report-01'
    out = base/'completion-report-presentation-check-01'
    assert not out.exists()
    assert (report/'package-manifest.json').exists()
    data = read_json(report/'cohort-report.json')
    assert len(data['rows']) == 18
    row = next(r for r in data['rows'] if r['id'] == 'svg-tools-03')
    stage = row['stages'][0]
    detail = report/stage['report_link']
    files = [report/'index.html', report/'cohort-report.json', report/'package-manifest.json', detail]
    hashes = {str(p.relative_to(ROOT)):file_sha256(p) for p in files}
    errors, requests = [], []
    out.mkdir()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440,'height':1000})
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('request', lambda r: requests.append(r.url))
            page.goto((report/'index.html').as_uri(), wait_until='load')
            assert page.locator('h1').inner_text() == 'KiroClops · 18 registered trials'
            assert page.locator('table').count() == 2
            assert page.locator('table').nth(0).locator('tbody tr').count() == 6
            assert page.locator('table').nth(1).locator('tbody tr').count() == 18
            page.screenshot(path=str(out/'cohort-top.png'))
            page.locator('table').nth(1).screenshot(path=str(out/'all-trials.png'))
            page.goto(detail.as_uri(), wait_until='load')
            page.wait_for_function("[...document.querySelectorAll('#media img')].length === 3 && [...document.querySelectorAll('#media img')].every(i=>i.complete && i.naturalWidth>0)")
            scenario_count = page.locator('#scenario option').count()
            assert scenario_count > 0
            before = page.locator('#time').inner_text()
            page.locator('#next').click()
            page.wait_for_function('previous=>document.getElementById("time").textContent!==previous', arg=before)
            after = page.locator('#time').inner_text()
            page.locator('#play').click()
            page.wait_for_function('previous=>document.getElementById("time").textContent!==previous', arg=after)
            page.locator('#play').click()
            page.wait_for_function("[...document.querySelectorAll('#media img')].every(i=>i.complete && i.naturalWidth>0)")
            page.locator('#media').screenshot(path=str(out/'svg-reference-before-after.png'))
            video = page.locator('#media a').filter(has_text='Open presentation video').first.get_attribute('href')
            assert video
            page.evaluate("src=>{const v=document.createElement('video');v.id='qa-video';v.preload='metadata';v.src=src;document.body.append(v)}", video)
            page.wait_for_function("document.getElementById('qa-video').readyState>=1")
            duration = page.locator('#qa-video').evaluate('(v)=>v.duration')
            assert duration > 0
            assert not errors, errors
            assert all(url.startswith('file:') for url in requests), 'Unexpected external request'
        finally:
            browser.close()
    assert all(file_sha256(ROOT/name)==sha for name,sha in hashes.items())
    result = dict(status='pass',headless=True,server_required=False,
        trial_rows=18,domain_mode_groups=6,detail_scenarios=scenario_count,
        next_frame_changes_time=True,playback_advances=True,local_images_loaded=3,
        video_metadata_duration_s=duration,page_errors=errors,external_requests=0,
        input_hashes=hashes,report_files_modified=False,new_model_run=False)
    write_json(out/'result.json',result)
    print({k:result[k] for k in ('status','trial_rows','detail_scenarios','page_errors')})


if __name__ == '__main__':
    main()
