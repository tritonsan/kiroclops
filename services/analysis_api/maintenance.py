"""Independent scheduled cleanup and worker controller, no user image decoding."""
from __future__ import annotations

import json
import os
import time
import uuid

from .store import DynamoStore,Conflict,key
from .api import Service,S3Objects,Queue
from .operations import observe,action
from .lifecycle import erase


def handler(event,context):
    import boto3
    store=DynamoStore(os.environ['TABLE'])
    objects=S3Objects(os.environ['BUCKET']);queue=Queue(os.environ['QUEUE'])
    service=Service(store,objects,queue)
    lock_key='system#controller#lease';previous=store.get(lock_key);now=time.time()
    if previous and previous['expires_at']>now:return {'status':'busy'}
    lock={'owner':uuid.uuid4().hex,'expires_at':now+100}
    try:store.transact([(lock_key,previous,lock)])
    except Conflict:return {'status':'busy'}
    try:
        ec2=boto3.client('ec2');instance_id=os.environ['WORKER_ID']
        instance=ec2.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]
        budget_key='system#budget#worker';old=store.get(budget_key)
        seed=old
        if seed is None:
            seed=objects.read_json('service/budget-seed.json')
            seed.update(last_accounted_at=instance['LaunchTime'].timestamp(),observed_state='pending')
        budget=observe(seed,instance['State']['Name'],now,instance['LaunchTime'].timestamp())
        store.transact([(budget_key,old,budget)])
        pending=False;cleanup_pending=False
        for _,registered in store.list_prefix('system#tenant#'):
            tenant=registered['tenant'];account=store.get(key(tenant,'account'))
            if not account:continue
            if account.get('data_state') in ('deleting','deleted'):
                deletion=erase(service,tenant)
                cleanup_pending|=deletion.get('waiting_for_worker_disk',False)
                continue
            for row_key,row in list(store.list_prefix(key(tenant,'package'))):
                if (row.get('expires_at',now+1)<=now or row.get('cleanup_pending') or
                    (row.get('retire_when_idle') and not store.get(key(tenant,'active')))):
                    from .lifecycle import retire_package
                    retire_package(service,tenant,row_key,row)
            for row_key,row in list(store.list_prefix(key(tenant,'job'))):
                if row.get('created_at',now)+30*86400<=now and row['status'] in ('succeeded','failed','interrupted','cancelled'):
                    if row.get('delivery_key'):objects.delete(row['delivery_key'])
                    from .storage import release
                    storage=release(service,tenant,row)
                    store.transact([(row_key,row,None)]+([storage] if storage else []))
            for kind in ('cache','capability'):
                for row_key,row in list(store.list_prefix(key(tenant,kind))):
                    if row['expires_at']<=now:store.transact([(row_key,row,None)])
            active=store.get(key(tenant,'active'))
            if active:
                job=store.get(key(tenant,'job',active['job_id']))
                if job and job['status']=='queued':
                    pending=True
                    # Durable outbox repair: a lost/expired SQS message cannot
                    # strand the only copy of a logical job.
                    queue.send(tenant,job['job_id'],wake=False)
                elif job and job['status']=='running' and job.get('lease_until',0)<now:
                    service.finish(tenant,job,'interrupted',None,{'code':'worker_lease_expired'})
        operation=action(instance['State']['Name'],pending,budget)
        # Data erasure remains available when new computation is disallowed.
        # Boot erases abandoned directories before analysis can start.
        if cleanup_pending and instance['State']['Name']=='stopped':operation='start'
        if operation=='start':
            try:ec2.start_instances(InstanceIds=[instance_id])
            except ec2.exceptions.ClientError as exc:
                if exc.response['Error']['Code']!='IncorrectInstanceState':raise
        elif operation=='stop':
            try:ec2.stop_instances(InstanceIds=[instance_id])
            except ec2.exceptions.ClientError as exc:
                if exc.response['Error']['Code']!='IncorrectInstanceState':raise
        return {'status':'ok','worker_action':operation,'compute_allowed':budget['compute_allowed']}
    finally:
        current=store.get(lock_key)
        if current and current['owner']==lock['owner']:
            try:store.transact([(lock_key,current,None)])
            except Conflict:pass
