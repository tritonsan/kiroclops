"""Operator-only invitation without email delivery or plaintext token output."""
import argparse
import getpass
import json
from pathlib import Path
import re

from scripts.phase3b_aws_probe import aws
from kiroclops.visual.schema import digest,read_json,write_json
from kiroclops.local_engine.backend import _lock


def invite(deployment,username,output):
    deployment,output=Path(deployment).resolve(),Path(output).resolve()
    if not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}',username):raise ValueError('Use a simple Cognito username')
    if output.exists():raise ValueError('Do not overwrite an invitation receipt')
    state=read_json(deployment/'state.json')
    if state.get('purpose')!='invited-beta-standard-opencv':raise ValueError('Use a reusable standard-OpenCV beta deployment')
    with _lock(deployment/'invitation.lock'):
        registered=aws('dynamodb','query',table_name=state['Table'],key_condition_expression='pk = :p AND begins_with(sk, :s)',
            expression_attribute_values={':p':{'S':'system'},':s':{'S':'system#tenant#'}},consistent_read=True)
        if len(registered.get('Items',[]))>=5:raise ValueError('Five invitations already exist')
        counter_key={'pk':{'S':'system'},'sk':{'S':'system#invitation-count#'}}
        counter=aws('dynamodb','get-item',table_name=state['Table'],key=counter_key,consistent_read=True).get('Item')
        previous=counter['value']['S'] if counter else None
        count=json.loads(previous)['count'] if previous else len(registered.get('Items',[]))
        if count>=5:raise ValueError('Five invitations already reserved')
        password=getpass.getpass('Temporary password (share through your own secure channel): ')
        if password!=getpass.getpass('Repeat temporary password: '):raise ValueError('Passwords differ')
        # Password is never retained in files, tool output or an email. Cognito
        # requires the invited person to set a new password on first login.
        user=aws('cognito-idp','admin-create-user',user_pool_id=state['Pool'],username=username,
                 temporary_password=password,message_action='SUPPRESS')['User']
        del password
        sub=next(p['Value'] for p in user['Attributes'] if p['Name']=='sub');tenant=digest(sub)[:32]
        # An unregistered Cognito user has no service access. If registration
        # fails, the account remains closed and the operator must reconcile it.
        writes=[]
        for name,value in ((f'user#{tenant}#account#',{'enabled':True,'storage_bytes':0,'kind':'invited_external_beta'}),
                           (f'system#tenant#{tenant}',{'tenant':tenant})):
            partition='user#'+tenant if name.startswith('user#') else 'system'
            writes.append({'Put':{'TableName':state['Table'],'ConditionExpression':'attribute_not_exists(pk)',
                'Item':{'pk':{'S':partition},'sk':{'S':name},'value':{'S':json.dumps(value,sort_keys=True,separators=(',',':'))}}}})
        reservation={'TableName':state['Table'],'ConditionExpression':'attribute_not_exists(pk)' if previous is None else '#v = :old',
            'Item':{**counter_key,'value':{'S':json.dumps({'count':count+1},separators=(',',':'))}}}
        if previous is not None:reservation.update(ExpressionAttributeNames={'#v':'value'},ExpressionAttributeValues={':old':{'S':previous}})
        writes.append({'Put':reservation})
        aws('dynamodb','transact-write-items',transact_items=writes)
        write_json(output,{'api_url':state['ApiUrl'],'cognito_client_id':state['Client'],
            'agent_client_id':state['AgentClient'],'region':'us-east-1'})
        write_json(output.with_suffix('.invitation.json'),{'username':username,'tenant':tenant,
            'email_sent':False,'temporary_password_retained':False,'external_test_completed':False})
    return {'status':'invited','bootstrap_config':str(output),'email_sent':False}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--deployment',required=True);p.add_argument('--username',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(json.dumps(invite(a.deployment,a.username,a.output)))
