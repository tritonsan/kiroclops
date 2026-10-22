"""Run exactly three preregistered service integrations, without coaching."""
from pathlib import Path
import subprocess
import sys
import time

from kiroclops.visual.schema import read_json,write_json,file_sha256
from services.analysis_api.deploy import relogin_probe


def main():
    root=Path('artifacts/phase3b/kiro-integration-01').resolve()
    registered=read_json(root/'registration.json')
    output=root/'run-result.json'
    if output.exists():raise ValueError('Do not replace prior integration runs')
    rows=[]
    for domain in registered['domains']:
        for path,expected in registered['sources'].items():
            if file_sha256(path)!=expected:raise ValueError('Registered implementation changed: '+path)
        directory=root/domain;session=directory/'session'
        if (session/'kiro/launch.json').exists():raise ValueError('Conversation already started')
        print(domain+': renewing bounded service capability',flush=True)
        relogin_probe('artifacts/phase3b/service-probe-02','owner')
        print(domain+': starting exact Opus 5.5/max CLI task',flush=True)
        started=time.time()
        with (directory/'launcher-output.log').open('w',encoding='utf8') as stream:
            result=subprocess.run([sys.executable,'-X','utf8','-m','kiroclops.local_engine.launcher',
                '--session',str(session),'--user-video'],stdout=stream,stderr=subprocess.STDOUT,
                timeout=2400,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        launch=read_json(session/'kiro/launch.json') if (session/'kiro/launch.json').exists() else {}
        state=read_json(session/'session.json')
        rows.append({'domain':domain,'process_returncode':result.returncode,'launcher_status':launch.get('status'),
            'prompt_sent':launch.get('prompt_sent',False),'duration_seconds':time.time()-started,
            'session_status':state['status'],'candidate_count':len(state['candidates']),
            'selected_candidate':state['selected_candidate_id'],'final':state.get('final'),
            'postturn_report':launch.get('postturn_report')})
        write_json(output,{'status':'running' if len(rows)<3 else 'completed','rows':rows,
            'registration_sha256':file_sha256(root/'registration.json'),'external_users':0,'comparative_claim':False})
        print(domain+': '+str(launch.get('status'))+' / '+state['status'],flush=True)
    for path,expected in registered['sources'].items():
        if file_sha256(path)!=expected:raise ValueError('Implementation changed during integrations')


if __name__=='__main__':main()
