"""Observe only this bounded probe and retain partial outcomes."""
import argparse
import json
from pathlib import Path
import time
import sys
from scripts.phase3b_aws_probe import status
from kiroclops.visual.schema import read_json, write_json


def main():
    sys.stdout.reconfigure(line_buffering=True)
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    seen=set();started=time.monotonic()
    while time.monotonic()-started<4800:
        status(args.output)
        state=read_json(args.output/'state.json')
        if (args.output/'instance.json').exists():
            instance=read_json(args.output/'instance.json')
            volumes=[x['Ebs']['VolumeId'] for x in instance.get('BlockDeviceMappings',[]) if 'Ebs' in x]
            if volumes:
                write_json(args.output/'observed-volumes.json',{'instance_id':state['InstanceId'],
                           'volume_ids':volumes,'source':'Recorded running EC2 describe-instances response'})
        blocker=args.output/'download/matching-blocker.json'
        if blocker.exists():
            print(json.dumps(read_json(blocker)),flush=True)
            return 0
        report=args.output/('download/cool-validation/result.json' if state.get('cool_only') else 'download/paired/result.json')
        if report.exists():
            result=read_json(report)
            for row in result['rows']:
                if row['id'] not in seen:
                    seen.add(row['id'])
                    print(json.dumps({'package':row['id'],'valid':row['valid'],'median_s':row['median_s'],'speedup':row.get('cool_speedup')}),flush=True)
            if result['status']!='running':
                print(json.dumps({'benchmark_status':result['status'],'completed_packages':len(seen)}),flush=True)
                return 0
        state=read_json(args.output/'state.json')
        if state.get('instance_state') in ('terminated','stopped'):
            print(json.dumps({'status':'probe_ended_before_complete_result','completed_packages':len(seen)}),flush=True)
            return 1
        time.sleep(40)
    raise TimeoutError('Bounded probe did not deliver a complete result; do not claim success')


if __name__=='__main__':
    raise SystemExit(main())
