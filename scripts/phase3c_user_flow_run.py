"""One real Kiro CLI run submitted through the local UI, desktop untouched.

Reuses validated object annotations only on identical reference pixel hashes.
It gives the model no prior solution, new probe results or reference code.
"""
from pathlib import Path
import argparse
import threading
import time

from kiroclops.common import ROOT
from kiroclops.visual.schema import read_json, write_json, file_sha256, digest
from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.intake import prepare_intake, resume_selection
from kiroclops.local_engine.intake_ui_server import make_server
from kiroclops.web_local.specs import fixture_project


def run(output):
    if output.exists():raise ValueError('Use a new directory; a failed run is preserved.')
    configure_environment()
    reference=ROOT/'artifacts/phase3b/kiro-integration-01/svg/session-reference-media'
    bundle=read_json(reference/'bundle.json');selection=read_json(reference/'selection.json')
    prepared=prepare_intake(reference/'package',fixture_project('svg'),bundle['goal'],output/'intake')
    # resume_selection checks package identity and exact template frame hashes.
    resume_selection(output/'intake',selection)
    write_json(output/'registration.json',{'kind':'RealLocalUiKiroRun','model':'claude-opus-5.5',
        'effort':'max','approval':'native_session_allow_always','analysis_backend':'local',
        'max_candidates':3,'stage_budget_s':1800,'operator_solution_provided':False,
        'prior_reference_selection_reused':True,'reference_package_sha256':prepared['package_sha256'],
        'reference_selection_sha256':digest(selection),'reference_source_provided':False,
        'new_probe_feedback_provided':False,'script_sha256':file_sha256(Path(__file__)),
        'initial_nodes':3,'old_session_changed':False,'registered_at':time.time()})
    server=make_server(output/'intake')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1200,'height':950})
            page.goto(server.url)
            page.wait_for_function('window.kiroclopsSelection?.().objects.length === 4')
            page.locator('#kc-nodes').fill('3')
            page.locator('#kc-launch').check()
            page.locator('#kc-submit').click()
            deadline=time.monotonic()+20
            while server.workflow.current is None and time.monotonic()<deadline:
                page.wait_for_timeout(100)
            identifier=server.workflow.current
            if identifier is None:raise ValueError(page.locator('#kc-status').inner_text())
            browser.close()  # Model continues without a page or foreground app.
        print('UI submitted; real Opus5.5/max CLI continues in background.',flush=True)
        last=None
        while True:
            state=server.workflow.status(identifier)
            view=(state.get('status'),state.get('step'),state.get('progress',{}))
            if view!=last:
                print({'status':state['status'],'step':state.get('step'),'progress':state.get('progress')},flush=True)
                last=view
            if state['status'] not in ('queued','running'):break
            server.workflow.finished.wait(20)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1200,'height':950});page.goto(server.url)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(output/'final-ui.png'),full_page=True)
            ui={'same_job_after_reconnect':server.workflow.current==identifier,
                'report_link_visible':page.locator('#kc-report').is_visible(),
                'acceptance_axis_count':page.locator('#kc-acceptance>p').count(),
                'displayed_status':page.locator('#kc-status').inner_text()}
            browser.close()
        result={'kind':'RealLocalUiKiroRunResult','status':state['status'],'job_id':identifier,
            'session':state.get('session'),'ui':ui,'acceptance':state.get('acceptance'),
            'launch_status':state.get('launch_result',{}).get('status'),'error':state.get('error'),
            'operator_code_changes':False,'finished_at':time.time()}
        if state.get('session'):
            from kiroclops.local_engine.review import audit_native_run
            audit=audit_native_run(Path(state['session']))
            write_json(output/'native-audit.json',audit)
            result['native_audit_status']=audit['status']
        write_json(output/'result.json',result)
        print({k:v for k,v in result.items() if k not in ('acceptance','ui')},flush=True)
        return result
    finally:
        server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();run(args.output.resolve())
