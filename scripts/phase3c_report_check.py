"""Headless QA of the shipped, self-contained evidence viewer."""
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import argparse
import threading

from kiroclops.local_engine.environment import configure_environment
from kiroclops.visual.schema import write_json, file_sha256


def run(directory):
    configure_environment()
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(directory),**kwargs)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    errors=[];cases=[];checks={}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1360,'height':950})
            page.on('pageerror',lambda e:errors.append(str(e)))
            base=f'http://127.0.0.1:{server.server_port}/'
            page.goto(base+'index.html')
            count=page.locator('#scenario option').count()
            for i in range(count):
                page.locator('#scenario').select_option(str(i))
                videos=page.locator('video').count()
                if videos:page.wait_for_function("[...document.querySelectorAll('video')].every(v=>v.readyState>=2&&v.videoWidth>0)")
                cases.append({'index':i,'videos':videos,'plot_paths':page.locator('#plot path').count(),
                    'evidence_status':page.request.get(base+page.locator('#evidence').get_attribute('href')).status})
            checks['ten_cases']=count==10
            checks['twenty_four_videos']=sum(c['videos'] for c in cases)==24
            checks['all_evidence_links']=all(c['evidence_status']==200 for c in cases)
            checks['invalid_cases_have_no_video']=sum(c['videos']==0 for c in cases)==2
            page.locator('#scenario').select_option('0')
            page.wait_for_function("[...document.querySelectorAll('video')].every(v=>v.readyState>=2)")
            page.locator('#next').click()
            checks['reference_frame_advance']=float(page.locator('#timeline').input_value())>0
            page.locator('#play').click();page.wait_for_timeout(450)
            checks['joint_native_playback']=page.evaluate("[...document.querySelectorAll('video')].every(v=>!v.paused&&v.currentTime>.5)")
            page.locator('#pause').click()
            checks['joint_pause']=page.evaluate("[...document.querySelectorAll('video')].every(v=>v.paused)")
            checks['nested_real_run_report']=page.request.get(base+'user-flow/index.html').status==200
            checks['code_diffs']=all(page.request.get(base+d+'.diff').status==200 for d in ('game','panel','svg'))
            page.screenshot(path=str(directory/'desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            checks['mobile_no_document_overflow']=page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(directory/'mobile.png'),full_page=True)
            browser.close()
        checks['no_javascript_error']=not errors
        result={'kind':'EvidenceViewerQA','status':'pass' if all(checks.values()) else 'fail',
            'checks':checks,'cases':cases,'javascript_errors':errors,'script_sha256':file_sha256(Path(__file__))}
        write_json(directory/'viewer-qa.json',result)
        print(result)
        return result
    finally:
        server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path)
    a=p.parse_args();run(a.directory.resolve())
