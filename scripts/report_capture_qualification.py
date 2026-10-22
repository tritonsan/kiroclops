"""Read-only timing/qualification report; private timing is diagnostic only."""
from pathlib import Path
import argparse,html,statistics,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json,file_sha256


def summarize(folder):
    result=read_json(folder/'result.json');protocol=read_json(folder/'protocol.json');latencies=[];phases=[];callback_gaps=[]
    for path in sorted((folder/'captures').glob('*/capture-record.json')):
        capture=read_json(path)
        latencies.extend((e['time_s']-e['planned_time_s'])*1000 for e in capture['inputs']['events'])
        oracle=read_json(path.parent/'private/oracle.json')
        times=[s['epoch_s'] for s in oracle['samples']]
        callback_gaps.extend((b-a)*1000 for a,b in zip(times,times[1:]))
        for event in oracle['events']:
            prior=[t for t in times if t<event['epoch_s']]
            if event['event']=='click' and prior:phases.append((event['epoch_s']-prior[-1])*1000)
    return {'name':folder.name,'status':result['status'],'passed':sum(p['pass'] for p in result['pairs']),
        'required':6,'completed_pairs':len(result['pairs']),'errors':result['errors'],
        'click_lateness_median_ms':statistics.median(latencies) if latencies else None,
        'click_phase_min_max_ms':[min(phases),max(phases)] if phases else None,
        'callback_gap_max_ms':max(callback_gaps) if callback_gaps else None,
        'result_sha256':file_sha256(folder/'result.json'),'protocol_sha256':file_sha256(folder/'protocol.json'),
        'source_hashes':protocol['source_hashes'],'diagnostic_inputs':
            {str(p.relative_to(folder)):file_sha256(p) for p in (folder/'captures').glob('*/private/oracle.json')}}


def run(output,grid=None,reassessment=None):
    root=Path(__file__).resolve().parents[1]/'artifacts/phase3a';output=Path(output)
    if output.exists():raise ValueError('Fresh report required')
    names=['click-hover-qualification-02','natural-browser-qualification-01',
        'natural-browser-raf-qualification-01','natural-browser-raf-qualification-02']
    rows=[summarize(root/name) for name in names]
    assert all(r['source_hashes']==rows[0]['source_hashes'] for r in rows),'Reference source changed'
    grid_result=read_json(Path(grid)/'result.json') if grid else None
    previous_grid=read_json(root/'natural-browser-grid-01/result.json') if grid else None
    candidate_result=read_json(Path(reassessment)/'result.json') if reassessment else None
    output.mkdir(parents=True)
    write_json(output/'summary.json',{'rows':rows,'limits_changed':False,'historical_verdicts_changed':False,
        'model_started':False,'private_geometry_used':False,'private_timestamps_diagnostic_only':True,
        'reference_grid':grid_result,'previous_reference_grid':previous_grid,'sealed_candidate_reassessment':candidate_result,
        'scope':'Single wide interrupted reference; not full panel or phase acceptance',
        'causal_certainty':'Frame/input phase is implicated by paired implementation changes; other timing effects not excluded.'})
    def n(v):return '—' if v is None else f'{v:.2f}'
    table=''.join(f'<tr><td><a href="../{html.escape(r["name"])}/result.json">{html.escape(r["name"])}</a></td>'
        f'<td>{r["passed"]}/6</td><td>{len(r["errors"])}</td><td>{n(r["click_lateness_median_ms"])}</td>'
        f'<td>{"–".join(n(v) for v in r["click_phase_min_max_ms"])}</td><td>{n(r["callback_gap_max_ms"])}</td></tr>' for r in rows)
    current=''
    if grid_result:
        current+=f'<h2>Broad reference qualification: {grid_result["passed"]}/{grid_result["required"]}</h2><p>Two recordings in each of seven scenarios; calibration, early close, drag-and-drop and open–close–open. The thresholds did not change.</p><ul>'
        current+=''.join(f'<li>{html.escape(r["scenario"])}: {"PASS" if r["pass"] else "FAIL"}</li>' for r in grid_result['rows'])+'</ul>'
        current+=f'<p>The previous broad experiment was {previous_grid["passed"]}/{previous_grid["required"]}: in five recordings the image gap around the first input exceeded the limit. When needed, the final fix takes a new real image before the click. The earlier failures are preserved.</p>'
    if candidate_result:
        c=candidate_result.get('comparison',{})
        current+=f'<h2>Additional calibration with the unchanged Kiro code: {html.escape(candidate_result["status"].upper())}</h2>'
        current+='<p>There is no new model run or operator patch. The old acceptance decisions are preserved; this result is not held-out validation or full phase acceptance.</p>'
        if c:current+=f'<p>Mean curve error {c["score"]:.6f}; reduction relative to baseline {c["improvement_fraction"]*100:.2f}%.</p>'
        current+=f'<p><a href="../{html.escape(Path(reassessment).name)}/index.html">Additional measurement of the sealed candidate</a></p>'
    page=f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>KiroClops · Recording diagnosis</title>
<style>body{{font:16px/1.6 system-ui;background:#101827;color:#e4eafa;max-width:1200px;margin:40px auto;padding:0 24px}}a{{color:#8eccff}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:12px;border-bottom:1px solid #364254;text-align:left}}code{{background:#263349}}</style>
<h1>KiroClops · Recording repeatability</h1>
{current}
<p>The same unchanged reference, 1280 pixel width, open–close–open. Each experiment contains four new recordings and six predetermined comparisons. The thresholds are the same; failed recordings are preserved.</p>
<table><tr><th>Experiment</th><th>Passing pairs</th><th>Recording errors</th><th>Median click latency (ms)</th><th>Click position within the frame (ms)</th><th>Longest callback gap (ms)</th></tr>{table}</table>
<h2>What changed?</h2><p>The natural browser flow removes frame-driving with BeginFrame. The native input is sent when a real animation callback completes. The screenshot and the input preparation run in sequence; if the image before the click is stale, a new real PNG is taken. The clock or motion code is not changed; the images and timestamps come from real recordings. The acceptance thresholds are the same.</p>
<h2>Limit of the evidence</h2><p>The position within the frame and the callback gap come from private diagnostic records; they are not passed to the image measurement or to Kiro. They are not pixel presentation-time accuracy. The success counts are development attempts at a single problem; they are not a general success rate. The whole panel, target transfer and independent visual acceptance are additionally required.</p>
<p><a href="summary.json">Source digests and all diagnostic results</a> · <a href="../panel-feedback-validation-01/index.html">Earlier Kiro run: 10/12, unchanged</a></p></html>'''
    (output/'index.html').write_text(page,encoding='utf8');print([(r['name'],r['passed']) for r in rows])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--grid',type=Path);p.add_argument('--reassessment',type=Path)
    args=p.parse_args();run(args.output,args.grid,args.reassessment)
