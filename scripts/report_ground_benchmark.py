"""Build the separate Phase 2B offline comparison package from recorded evidence."""
from pathlib import Path
import argparse
import html
import json
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.integration.ground_benchmark import build_benchmark
from kiroclops.integration.ground_reporting import generate_ground_report
from kiroclops.integration.storage import write_json


def build():
    result=build_benchmark()
    output=ROOT/'artifacts/phase2b-report'
    if output.is_symlink() or output.resolve()!=output.absolute():
        raise ValueError('Output must be the ordinary Phase2B report directory')
    output.mkdir(parents=True,exist_ok=True)
    cards=[]
    escape=lambda value:html.escape(str(value),quote=True)
    for row in result['sessions']:
        session=ROOT/'sessions'/row['session_id']
        link='Not run yet.'
        if (session/'session.json').exists():
            page=generate_ground_report(session)
            destination=output/'runs'/row['session_id']
            if destination.is_symlink() or destination.resolve()!=destination.absolute():
                raise ValueError('Session report destination redirects')
            shutil.copytree(page.parent,destination,dirs_exist_ok=True)
            link=f'<a href="runs/{escape(row["session_id"])}/index.html">Open the videos and the code diff →</a>'
        status=row.get('demo_status') if row['surface']=='ide' else row['status']
        error=row.get('final_error',{}).get('mean_rmse_body')
        error='—' if error is None else f'{error:.4f} body'
        gates=''.join(f'<li>{escape(k)}: <b>{escape(v)}</b></li>' for k,v in row.get('gates',{}).items())
        cards.append(f'<article><h2>{escape(row["session_id"])}</h2><p class="badge">{escape(status)}</p>'
            f'<p>Final horizontal error: <b>{error}</b><br>Candidates: {row.get("candidate_count",0)} / 3</p>'
            f'<p>{link}</p><details><summary>Acceptance checks</summary><ul>{gates}</ul></details></article>')
    write_json(output/'benchmark.json',result)
    data=escape(json.dumps(result,ensure_ascii=False,indent=2))
    page=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>KiroClops · Two behaviors, one character</title><style>
:root{{color-scheme:dark}}body{{background:#10141e;color:#e9edf5;font:16px system-ui;margin:0}}main{{max-width:1200px;margin:auto;padding:36px}}h1{{font-size:42px;line-height:1.15}}p{{line-height:1.6}}a{{color:#91c8ff}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}}article,section{{background:#1b2435;padding:24px;border-radius:14px;margin-bottom:20px}}.muted{{color:#b3bdcd}}.badge{{display:inline-block;background:#2e425c;padding:6px 12px;border-radius:8px}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}@media(max-width:700px){{.grid{{grid-template-columns:1fr}}main{{padding:18px}}h1{{font-size:32px}}}}
</style><body><main><p class="muted">KIROCLOPS / PHASE 2B</p><h1>Two behaviors.<br>One playable character.</h1>
<p>Can Kiro transfer the running and braking measured from the footage into the code of the target game while preserving the earlier jump?</p>
<section><h2>Recorded result</h2><p>CLI: <b>{result['cli']['successes']} / 3</b> passed all conditions.<br>IDE demonstration: <b>{'verified' if result['ide']['demonstrated'] else 'not yet verified'}</b>.</p>
<p>Each final candidate is evaluated with 18 held-out horizontal comparisons, 12 jump comparisons and game checks. The independent visual measurement audit is kept separate from the numerical result.</p>
<p class="muted">Model: Claude Opus 5 / high. The native IDE export does not definitively prove the historical thinking level of each generation. No new run is chosen in place of failed runs; a comparison without tools is outside the scope of this phase.</p></section>
<div class="grid">{''.join(cards)}</div><section><h2>Experiment record</h2><p><a href="benchmark.json">Machine-readable result</a></p><details><summary>All evidence statuses</summary><pre>{data}</pre></details></section>
<p class="muted">This package works offline with local video and data files. Normal gameplay footage is not used in place of the controlled motion measurement.</p></main></body></html>'''
    (output/'index.html').write_text(page,encoding='utf-8')
    return output/'index.html'


if __name__=='__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(build())
