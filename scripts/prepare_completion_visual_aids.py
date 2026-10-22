"""Prepare unsigned independent raw-pixel aids after native capture finishes.

Never marks a point as reviewed and never writes an acceptance receipt.
"""
from pathlib import Path
import sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'artifacts/phase3a/tools')]
from kiroclops.visual.schema import read_json,write_json,file_sha256,VisualError
from kiroclops.local_engine.qualification import analyzer_sources
import prepare_fresh_web_review_v2 as preparation
import review_panel_raw09 as panel_raw
import prepare_panel_scope_confirmed as panel_scope
import panel_completion_boards as panel_boards
import raw_svg_holdout_review_aid as svg_raw
import compare_svg_clock_review as svg_compare
import svg_clock_event_boards as svg_boards

def run(corpus,output,pipeline):
    corpus,output,pipeline=map(lambda p:Path(p).resolve(),(corpus,output,pipeline))
    if output.exists():raise ValueError('Preserve prior visual evidence')
    deadline=time.monotonic()+1800
    while True:
        state=read_json(pipeline)
        if state['status'] in ('measuring_references','automated_gates_pass_independent_review_pending'):break
        if state['status'] in ('failed','interrupted'):raise VisualError('pipeline_failed','Automated prerequisite failed; no new review preparation')
        if time.monotonic()>deadline:raise VisualError('capture_wait_timeout','Capture stage did not finish')
        time.sleep(5)
    frozen=analyzer_sources()
    print('Prepare16-record independent raw review',flush=True)
    preparation.prepare(corpus,file_sha256(corpus/'manifest.json'),output)
    prep=read_json(output/'preparation.json')
    records=[r for r in prep['records'] if r['domain']=='panel']
    write_json(output/'panel/inventory.json',{'records':records})
    requested=read_json(output/'svg/positions/request.json')['records'];records=[]
    for item in requested:
        row=next(r for r in prep['records'] if r['domain']=='svg' and r['package_sha256']==item['package_sha256'] and r['observations_sha256']==item['observations_sha256'])
        records.append({**row,'record_id':item['id'],'record_path':str(output/'svg/records'/row['id'])})
    write_json(output/'svg/inventory.json',{'records':records})
    panel_raw.run(output,output/'panel/raw')
    panel_scope.run(output,output/'panel/raw',output/'panel/drafts')
    panel_boards.R=output/'panel';panel_boards.RAW=output/'panel/raw';panel_boards.run()
    svg_raw.run(output/'svg');svg_compare.run(output/'svg');svg_boards.run(output/'svg')
    if analyzer_sources()!=frozen:raise VisualError('source_changed','Analyzer changed during unsigned preparation')
    write_json(output/'aids-result.json',{'status':'unsigned_needs_visual_review','records':16,
        'independent_review_signed':False,'source_hashes':frozen,'helper_sha256':file_sha256(__file__),
        'svg_numeric_agreement':read_json(output/'svg/agreement.json'),'panel_drafts':read_json(output/'panel/drafts/summary.json')})
    print('Unsigned aids ready; actual visual review remains required',flush=True)
if __name__=='__main__':run(*sys.argv[1:])
