"""Prepare raw-image review tasks for a completed engineering corpus; never signs."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json
from kiroclops.local_engine.review import prepare_review_group


def main():
    parser=argparse.ArgumentParser();parser.add_argument('corpus',type=Path);parser.add_argument('--domain',choices=['panel','svg'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--include-event-neighbors',action='store_true',help='Optional additional position marks; event timing has its own independent review')
    args=parser.parse_args()
    index=read_json(args.corpus/'capture-index.json');records=[]
    for row in index['references']:
        if row['domain']!=args.domain:continue
        analysis=read_json(args.corpus/f'analysis/references/{args.domain}/{row["scenario_index"]}/analysis.json')
        records.append({'package':row['record']['package'],'observations':analysis['observations'],
            'events':analysis['events'] if args.include_event_neighbors else {},'role':'reference'})
    for row in index['cases']:
        if row['case']['domain']!=args.domain:continue
        analysis=read_json(args.corpus/f'analysis/{row["case"]["id"]}/analysis.json')
        records.append({'package':row['record']['package'],'observations':analysis['observations'],
            'events':analysis['events'] if args.include_event_neighbors else {},'role':'baseline' if row['case']['mutation']=='baseline' else 'qualification_candidate'})
    result=prepare_review_group(records,args.output,tolerance_px=1.5)
    write_json(args.output/'origin-records.json',[{'package':str(r['package']),'role':r['role']} for r in records])
    print(result)


if __name__=='__main__':main()
