"""Headless file:// smoke of the standalone report, including relocated media."""
from pathlib import Path
import argparse
import json
import shutil
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.environment import configure_environment
from kiroclops.visual.schema import write_json


def main():
    parser=argparse.ArgumentParser();parser.add_argument('report',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False);configure_environment()
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory(prefix='KiroClops moved report café ') as temporary:
        moved=Path(temporary)/'local report naïve';shutil.copytree(args.report,moved)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1100})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto((moved/'index.html').as_uri());page.locator('#image-reference').wait_for()
            page.wait_for_function("Array.from(document.querySelectorAll('#media img')).every(i=>i.complete&&i.naturalWidth>0)")
            before=page.locator('#time').inner_text();page.locator('#next').click();after=page.locator('#time').inner_text()
            assert before!=after,'Frame control did not advance'
            assert page.locator('#overlay-reference circle').count()>0,'No visible measured anchors'
            page.locator('#slider').evaluate("e=>{e.value=.5;e.dispatchEvent(new Event('input'))}")
            page.wait_for_function("Array.from(document.querySelectorAll('#media img')).every(i=>i.complete&&i.naturalWidth>0)")
            page.screenshot(path=str(args.output/'report.png'),full_page=True)
            videos=page.locator('a').evaluate_all("nodes=>nodes.filter(n=>n.href.endsWith('.mp4')).map(n=>n.href)")
            dimensions=[]
            for url in videos:
                dimensions.append(page.evaluate("url=>new Promise((resolve,reject)=>{let v=document.createElement('video');v.onloadedmetadata=()=>resolve({width:v.videoWidth,height:v.videoHeight,duration:v.duration});v.onerror=()=>reject('Local media failed');v.src=url})",url))
            assert dimensions and all(v['width']>0 and v['duration']>0 for v in dimensions)
            assert not errors,errors
            result={'status':'pass','moved_to_unicode_space_directory':True,'file_protocol':True,'server_required':False,
                    'javascript_errors':errors,'frame_step':{'before':before,'after':after},'videos':dimensions,
                    'visible_anchor_overlays':page.locator('#overlay-reference circle').count(),'model_run':False}
            write_json(args.output/'result.json',result);print(json.dumps(result,indent=2));browser.close()


if __name__=='__main__':main()
