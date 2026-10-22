"""Real headless-browser annotation → fresh session → reconnect check.

Uses synthetic pixels to test the UI, never as movement success evidence.
The model is explicitly unchecked; a separate real Kiro receipt proves editing.
"""
from pathlib import Path
import argparse
import threading
import time

import cv2
import numpy as np

from kiroclops.local_engine.environment import configure_environment
from kiroclops.local_engine.intake import prepare_intake
from kiroclops.local_engine.intake_ui_server import make_server
from kiroclops.visual.package import create_package
from kiroclops.visual.schema import read_json, write_json, file_sha256
from kiroclops.web_local.specs import fixture_project


def run(output):
    if output.exists():
        raise ValueError('Use a fresh QA directory.')
    configure_environment()
    output.mkdir(parents=True)
    frames=[]
    texture=np.random.default_rng(92).integers(40,255,(20,20,3),dtype=np.uint8)
    for i in range(8):
        image=np.zeros((320,320,3),np.uint8)
        image[130:150,70+i*3:90+i*3]=texture
        path=output/f'{i}.png';path.write_bytes(cv2.imencode('.png',image)[1].tobytes());frames.append(path)
    create_package(output/'video',frames,[i*.2 for i in range(8)])
    prepare_intake(output/'video',fixture_project('panel'),'Approximate the visible panel motion.',output/'intake')
    server=make_server(output/'intake')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    errors=[];checks={}
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1200,'height':950})
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(server.url)
            page.wait_for_function('window.kiroclopsCurrentFrame?.() != null')
            page.locator('#clear').click()
            canvas=page.locator('#canvas')
            def draw(x,y,w,h):
                canvas.scroll_into_view_if_needed()
                bounds=canvas.bounding_box()
                page.mouse.move(bounds['x']+x,bounds['y']+y);page.mouse.down()
                page.mouse.move(bounds['x']+x+w,bounds['y']+y+h,steps=3);page.mouse.up()
            page.locator('#mode').select_option('body');draw(70,130,20,20)
            page.locator('#add').click()
            assert page.evaluate('window.kiroclopsSelection().objects.length')==1, page.locator('#message').inner_text()
            page.locator('#mode').select_option('background');draw(5,5,25,25)
            page.locator('#kc-launch').uncheck()
            page.locator('#kc-add-input').click()
            page.locator('#timeline').evaluate("e=>{e.value='1';e.dispatchEvent(new Event('input'))}")
            page.wait_for_function('window.kiroclopsCurrentFrame?.()?.index === 1')
            page.get_by_text('Time of this frame',exact=True).click()
            checks['native_loaded_frame_time']=page.locator('#kc-event-list input[type=number]').first.input_value()=='0.2'
            page.locator('#kc-submit').click()
            checks['unknown_mapping_blocks_submission']='Select the target object' in page.locator('#kc-status').inner_text()
            mapping=page.locator('#kc-map-list select');mapping.select_option('handle')
            page.locator('#kc-mappings').click()
            checks['mapping_survives_refresh']=page.locator('#kc-map-list select').input_value()=='handle'
            checks['actual_model_label']='claude-opus-5.5 / max' in page.locator('#kc-model').inner_text()
            page.locator('#kc-submit').click()
            page.wait_for_function("document.querySelector('#kc-status').textContent.includes('model not started')",timeout=30000)
            identifier=server.workflow.current;state=server.workflow.status(identifier)
            session=read_json(Path(state['session'])/'session.json')
            checks['real_fresh_session_created']=state['status']=='completed' and session['status']=='new'
            checks['declarations_stay_declared']=session['experiment']['input_provenance']=='user_declared'
            checks['no_invented_heldout']=session['experiment']['heldout_status']=='not_provided'
            page.locator('#kc-refresh').click()
            page.wait_for_function("document.querySelector('#kc-status').textContent.includes('model not started')")
            checks['reconnect_same_job']=server.workflow.current==identifier and len(list(server.workflow.output.glob('attempt-*')))==1
            checks['reconnect_keeps_mapping']=page.locator('#kc-map-list select').input_value()=='handle'
            checks['reconnect_keeps_timestamp']=page.locator('#kc-event-list input[type=number]').first.input_value()=='0.2'
            page.screenshot(path=str(output/'desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            checks['mobile_no_document_overflow']=page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/'mobile.png'),full_page=True)
            browser.close()
        checks['no_javascript_error']=not errors
        result={'kind':'HeadlessUserFlowCheck','status':'pass' if all(checks.values()) else 'fail',
            'checks':checks,'javascript_errors':errors,'model_started':False,'movement_acceptance':False,
            'job_id':identifier,'session':state['session'],'script_sha256':file_sha256(Path(__file__)),
            'finished_at':time.time()}
        write_json(output/'result.json',result)
        print(result)
        return result
    except Exception as exc:
        write_json(output/'result.json',{'kind':'HeadlessUserFlowCheck','status':'error',
            'checks':checks,'javascript_errors':errors,'error':str(exc),'model_started':False})
        raise
    finally:
        server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();run(args.output.resolve())
