"""Human CLI login and data controls; agents receive a different scoped token."""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import time
from urllib.request import Request,build_opener
from urllib.error import HTTPError,URLError

from kiroclops.visual.schema import read_json,write_json,VisualError
from .credentials import save,load
from .remote import RemoteAnalysisBackend,_NoRedirect


def cognito(config,operation,body):
    if config.get('region')!='us-east-1':raise VisualError('invalid_remote_config','This beta is pinned to us-east-1')
    request=Request('https://cognito-idp.us-east-1.amazonaws.com/',data=json.dumps(body).encode(),
        headers={'Content-Type':'application/x-amz-json-1.1','X-Amz-Target':'AWSCognitoIdentityProviderService.'+operation})
    try:
        with build_opener(_NoRedirect()).open(request,timeout=30) as response:return json.load(response)
    except (HTTPError,URLError,OSError,ValueError):
        raise VisualError('cloud_login_failed','Cognito did not complete authentication; no token was logged') from None


def authenticate(config,client,username,password):
    value=cognito(config,'InitiateAuth',{'ClientId':client,'AuthFlow':'USER_PASSWORD_AUTH',
        'AuthParameters':{'USERNAME':username,'PASSWORD':password}})
    if value.get('ChallengeName')=='NEW_PASSWORD_REQUIRED':
        updated=getpass.getpass('New password: ')
        if updated!=getpass.getpass('Repeat new password: '):raise VisualError('cloud_login_failed','Passwords differ')
        value=cognito(config,'RespondToAuthChallenge',{'ClientId':client,'ChallengeName':'NEW_PASSWORD_REQUIRED','Session':value['Session'],
            'ChallengeResponses':{'USERNAME':username,'NEW_PASSWORD':updated}})
        password=updated
    if 'AuthenticationResult' not in value:raise VisualError('cloud_login_failed','This account requires an unsupported authentication challenge')
    return value['AuthenticationResult'],password


def human_backend(config,directory):
    value=load(config['human_credential_file'])
    if value['expires_at']<=time.time():raise VisualError('cloud_login_required','Log in before changing account data')
    return RemoteAnalysisBackend(directory,{**config,'scoped_agent':False},token_provider=lambda:value['access_token'])


def login(config_path,username):
    path=Path(config_path).resolve();bootstrap=read_json(path)
    password=getpass.getpass('Password: ')
    human,password=authenticate(bootstrap,bootstrap['cognito_client_id'],username,password)
    agent,_=authenticate(bootstrap,bootstrap['agent_client_id'],username,password)
    del password
    # The initial public bootstrap has no engine pin yet. Discover it through
    # authenticated HTTPS, then freeze it in the local configuration.
    from urllib.parse import urlparse
    endpoint=bootstrap['api_url'].rstrip('/')
    parsed=urlparse(endpoint)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise VisualError('invalid_remote_config','HTTPS endpoint required')
    try:
        request=Request(endpoint+'/service',headers={'Authorization':'Bearer '+human['AccessToken']})
        with build_opener(_NoRedirect()).open(request,timeout=30) as response:descriptor=json.load(response)
    except (HTTPError,URLError,OSError,ValueError):
        raise VisualError('analysis_connection_lost','Could not load the authenticated service descriptor') from None
    private=path.parent/'.kiroclops-private'
    human_file=private/'human.credential';agent_file=private/'agent.credential'
    config={**bootstrap,**descriptor,'api_url':endpoint,'capability_file':str(agent_file),
            'human_credential_file':str(human_file),'scoped_agent':True,'acknowledge_delivery':True}
    temporary=RemoteAnalysisBackend(private/'control',{**config,'scoped_agent':False},token_provider=lambda:human['AccessToken'])
    capability=temporary._call('POST','/sessions',{})
    save(human_file,{'access_token':human['AccessToken'],'expires_at':time.time()+human['ExpiresIn']})
    save(agent_file,{'access_token':agent['AccessToken'],'expires_at':min(time.time()+agent['ExpiresIn'],capability['expires_at']),
                     'service_capability':capability['capability']})
    write_json(path,config)
    return {'status':'logged_in','config':str(path),'agent_scope':capability['scope'],'expires_at':capability['expires_at'],
            'local_storage':'Windows DPAPI or POSIX mode 0600; same OS user is not a strong secret boundary'}


def logout(config,backend):
    try:
        result=backend._call('POST','/sessions/revoke',{})
        return {'status':'logged_out','agent_capabilities_revoked':result['revoked']}
    except VisualError as exc:
        if exc.code=='cloud_data_deleted':
            return {'status':'logged_out','agent_capabilities_revoked':'account_data_already_deleted'}
        return {'status':'logged_out_locally','server_revocation':'unconfirmed_until_expiry',
                'error_code':exc.code}
    finally:
        for name in ('human_credential_file','capability_file'):Path(config[name]).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    command=sub.add_parser('login');command.add_argument('--username',required=True)
    sub.add_parser('logout');sub.add_parser('usage')
    command=sub.add_parser('status');command.add_argument('job_id')
    command=sub.add_parser('delete-data');command.add_argument('--yes',action='store_true',required=True)
    args=parser.parse_args()
    try:
        if args.command=='login':result=login(args.config,args.username)
        else:
            config=read_json(args.config)
            try:backend=human_backend(config,args.config.parent/'.kiroclops-private/control')
            except VisualError:
                if args.command!='logout':raise
                # An expired human token cannot revoke remotely, but must not
                # prevent the user's local sign-out.
                for name in ('human_credential_file','capability_file'):Path(config[name]).unlink(missing_ok=True)
                print(json.dumps({'status':'logged_out_locally','server_revocation':'unconfirmed_until_expiry'}));return 0
            if args.command=='usage':result=backend._call('GET','/usage')
            elif args.command=='status':
                RemoteAnalysisBackend._job_id(args.job_id)
                value=backend._call('GET','/jobs/'+args.job_id)
                result={k:v for k,v in value.items() if k!='delivery'}
            elif args.command=='delete-data':result=backend._call('DELETE','/data')
            else:
                result=logout(config,backend)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (VisualError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'error','code':getattr(exc,'code','invalid_configuration'),'message':str(exc)},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
