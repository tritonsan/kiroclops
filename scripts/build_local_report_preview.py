"""Build an explicitly non-model report preview from preserved engineering captures."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.session import create_session
from kiroclops.local_engine.reporting import build_report
from kiroclops.visual.schema import read_json,write_json


def main():
    root=Path(__file__).resolve().parents[1]
    directory=root/'sessions/local-report-preview-01'
    old=root/'artifacts/phase3a/web-corpus-01'
    measured=root/'artifacts/phase3a/web-corpus-02'
    create_session(directory,'svg',old/'private-projects/svg-slow',old/'private-projects/svg-correct-first',
        'Engineering report preview: preserved reference and intentionally slow SVG control. No Kiro-generated candidate; acceptance remains pending.')
    captures=read_json(measured/'capture-index.json')
    reference=next(x['record'] for x in captures['references'] if x['domain']=='svg' and x['scenario_index']==0)
    before=next(x['record'] for x in captures['cases'] if x['case']['id']=='svg-slow')
    reference={**reference,'analysis':str(measured/'analysis/references/svg/0/analysis.json')}
    before={**before,'analysis':str(measured/'analysis/svg-slow/analysis.json')}
    output=directory/'measurements/engineering-controls'
    write_json(output/'reference.json',[reference]);write_json(output/'before.json',[before])
    write_json(output/'comparison.json',read_json(measured/'analysis/svg-slow/result.json'))
    result=build_report(directory,encode=True)
    write_json(root/'artifacts/phase3a/report-preview-01.json',result)
    print(result)


if __name__=='__main__':main()
