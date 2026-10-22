"""CLI-only second-target protocol; never opens an IDE or input desktop."""
import argparse
import json
from pathlib import Path
from kiroclops.integration.storage import IntegrationError


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    setup=commands.add_parser('setup',help='Collect fresh private-desktop native/calibration/preservation evidence; no model')
    setup.add_argument('--output',required=True,type=Path)
    q=commands.add_parser('qualify',help='Replay native/measurement/manual evidence; no model invocation')
    for name in ('runtime-receipt','integrity-receipt','calibration-root','observations','test-receipt'):
        q.add_argument('--'+name,required=True,type=Path)
    q.add_argument('--output',type=Path)
    p=commands.add_parser('prepare-cohort',help='Preregister exactly three independent chains')
    p.add_argument('cohort_id'); p.add_argument('--baseline',required=True,type=Path); p.add_argument('--qualification',type=Path)
    r=commands.add_parser('run-cohort',help='Run registered native Kiro CLI Sol/max chains')
    r.add_argument('registry',type=Path); r.add_argument('--executable')
    r=commands.add_parser('report'); r.add_argument('registry',type=Path)
    r=commands.add_parser('status-report'); r.add_argument('--qualification',required=True,type=Path)
    r.add_argument('--connection',required=True,type=Path)
    a=commands.add_parser('audit'); a.add_argument('session',type=Path)
    a.add_argument('--observations',required=True,type=Path); a.add_argument('--source-review',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command=='setup':
            from .qualification import collect_native
            result=collect_native(args.output,progress=lambda *a:print(*a,flush=True))
        elif args.command=='qualify':
            from .qualification import qualify
            result=qualify(args.runtime_receipt,args.integrity_receipt,args.calibration_root,args.observations,args.test_receipt,output=args.output)
            print(json.dumps({'status':result['status'],'gates':{k:v['status'] for k,v in result['gates'].items()}},indent=2))
            return 0 if result['status']=='pass' else 1
        elif args.command=='prepare-cohort':
            from .cohort import register_cohort
            result=register_cohort(args.cohort_id,args.baseline,qualification=args.qualification)
        elif args.command=='run-cohort':
            from .cohort import run_cohort
            result=run_cohort(args.registry,executable=args.executable,progress=lambda *a:print(*a,flush=True))
        elif args.command=='audit':
            from .audit import audit_session
            result=audit_session(args.session,args.observations,args.source_review)
        elif args.command=='status-report':
            from .reporting import build_readiness_report
            result=build_readiness_report(args.qualification,args.connection)
        else:
            from .reporting import build_cohort_report
            result=build_cohort_report(args.registry)
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        return 0
    except (IntegrationError,ValueError,KeyError,OSError) as exc:
        print(json.dumps({'status':'error','code':getattr(exc,'code','invalid_input'),'message':str(exc)},ensure_ascii=False))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
