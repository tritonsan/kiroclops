"""Session provisioning and pixel-only baseline commands. No controller patching."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .session import Session, prepare_session
from .storage import IntegrationError


def wait_for_job(session,submitted):
    while True:
        result=session.get_job(submitted['job_id'],wait_seconds=20)
        if result['status'] not in {'queued','running'}:
            return result


def main(argv=None):
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='action',required=True)
    create=sub.add_parser('create')
    create.add_argument('--name')
    create.add_argument('--mode',choices=['tools','baseline'],default='tools')
    create.add_argument('--no-configure',action='store_true')
    create.add_argument('--experiment', choices=['legacy-jump-v1', 'ground-motion-v1'], default='legacy-jump-v1')
    for name in ('prepare','capture','finish','report','inspect'):
        command=sub.add_parser(name)
        command.add_argument('--session',required=True,type=Path)
    args=p.parse_args(argv)
    session=None
    try:
        if args.action=='create':
            result={'session':str(prepare_session(args.name,mode=args.mode,configure=not args.no_configure,
                                                 experiment_id=args.experiment))}
        else:
            session=Session(args.session)
            baseline=session.read()['mode']=='baseline'
            if args.action=='inspect':
                result={'session_id':session.read()['session_id'],'status':session.read()['status']} if baseline else session.inspect()
            else:
                action={'prepare':'prepare','capture':'evaluate','finish':'finalize','report':'report'}[args.action]
                job=wait_for_job(session,session.submit(action))
                result=job
                if baseline and args.action in {'prepare','capture'}:
                    # No motion profile, numerical feedback or tracking traces.
                    label=job.get('candidate_id') or 'baseline'
                    result={'job_id':job['job_id'],'status':job['status'],'candidate_id':job.get('candidate_id'),
                            'raw_reference':str(session.directory/'target/.kiroclops-reference'),
                            'raw_evidence':str(session.directory/'target/.kiroclops-evidence'/label)}
                    if job['status']=='failed':
                        result['error']={'code':job.get('error',{}).get('code'),
                                         'message':'Execution failed; inspect candidate engine log in exported evidence if present.'}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if result.get('status') in {'failed','cancelled','error','interrupted'} else 0
    except (IntegrationError,OSError,ValueError) as exc:
        print(json.dumps({'status':'error','error':{'code':getattr(exc,'code','invalid_request'),'message':str(exc)}},ensure_ascii=False))
        return 1
    finally:
        if session:
            session.close()


if __name__=='__main__':
    raise SystemExit(main())
