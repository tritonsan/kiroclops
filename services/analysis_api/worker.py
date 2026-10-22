"""One EC2 control process; analysis runs without network or IAM credentials."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time
import uuid

from kiroclops.visual.schema import digest, file_sha256, read_json, write_json
from kiroclops.cloud.identity import analysis_sources, engine_identity
from kiroclops.cloud.results import envelope, validate_delivery
from .api import Service, S3Objects, Queue, TERMINAL
from .store import DynamoStore, Conflict, key


ROOT = Path(__file__).resolve().parents[2]
WORK = Path('/srv/kiroclops-jobs')


def sources():
    result = analysis_sources(ROOT)
    for name in ('kiroclops/__init__.py','kiroclops/cloud/identity.py','kiroclops/cloud/results.py',
                 'kiroclops/cloud/transport.py','kiroclops/cloud/corpus.py','kiroclops/cloud/execution.py',
                 'services/analysis_api/runner.py'):
        result[name] = file_sha256(ROOT/name)
    return dict(sorted(result.items()))


class Worker:
    def __init__(self):
        self.store = DynamoStore(os.environ['TABLE'])
        self.objects = S3Objects(os.environ['BUCKET'])
        self.queue = Queue(os.environ['QUEUE'])
        self.service = Service(self.store,self.objects,self.queue)
        self.engine = engine_identity(4)
        self.sources = sources()
        self.owner = uuid.uuid4().hex
        if any(WORK.iterdir()):raise ValueError('Boot cleanup did not empty the worker directory')
        disk_key='system#worker#disk';previous=self.store.get(disk_key)
        self.store.transact([(disk_key,previous,{'verified_empty_at':time.time(),'worker_owner':self.owner})])
        descriptor = {'schema_version':2,'engine_sha256':self.engine['engine_sha256'],'analysis_sources':self.sources,
                      'object_hosts':[os.environ['BUCKET']+'.s3.us-east-1.amazonaws.com'],
                      'engine_name':'standard-arm','threads':4,'beta_readiness':'internal-operational-probe','acknowledge_delivery':True,
                      'deployment_revision':int(os.environ.get('DEPLOYMENT_REVISION','1')),'ready_at':time.time()}
        self.put_json('service/descriptor.json',descriptor)
        self.put_json('service/engine.json',self.engine)

    def put_json(self,path,value):
        self.objects.client.put_object(Bucket=self.objects.bucket,Key=path,Body=json.dumps(value,allow_nan=False).encode(),ContentType='application/json')

    def claim(self,tenant,job_id):
        job_key = key(tenant,'job',job_id)
        job = self.store.get(job_key)
        if not job or job['status'] in TERMINAL:
            return None
        if job['status']=='running' and job['lease_until']>time.time():
            return False
        if job.get('cancel_requested'):
            self.service.finish(tenant,job,'cancelled',None)
            return None
        if job.get('attempts',0)>=1:
            self.service.finish(tenant,job,'interrupted',None,{'code':'worker_lease_exhausted'})
            return None
        updated = {**job,'status':'running','lease_owner':self.owner,'lease_until':time.time()+30,'attempts':job.get('attempts',0)+1,
                   'started_at':job.get('started_at',time.time())}
        self.store.transact([(job_key,job,updated)])
        return updated

    def heartbeat(self,tenant,job_id):
        job_key = key(tenant,'job',job_id)
        for _ in range(5):
            account=self.store.get(key(tenant,'account'))
            if not account or account.get('data_state') in ('deleting','deleted'):return False
            current = self.store.get(job_key)
            if current['status']!='running' or current.get('lease_owner')!=self.owner or current.get('cancel_requested'):
                return False
            try:
                self.store.transact([(job_key,current,{**current,'lease_until':time.time()+30})])
                return True
            except Conflict:
                continue
        return False

    def finish(self,tenant,job_id,status,delivery_key=None,error=None):
        for _ in range(8):
            current = self.store.get(key(tenant,'job',job_id))
            if not current or current['status'] in TERMINAL or current.get('lease_owner')!=self.owner:
                return
            try:
                self.service.finish(tenant,current,'cancelled' if current.get('cancel_requested') else status,delivery_key,error)
                return
            except Conflict:
                continue
        raise Conflict()

    def run(self,tenant,job,message):
        job_id = job['job_id']
        directory = WORK/job_id
        # Only this owned job's directory may be recycled after a lost lease.
        if directory.parent.resolve()!=WORK.resolve() or len(job_id)!=32:
            raise ValueError('Invalid workspace')
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(mode=0o711,parents=True)
        output = directory/'output'
        output.mkdir(mode=0o700)
        account = pwd.getpwnam('kiroanalysis')
        os.chown(output,account.pw_uid,account.pw_gid)
        package_key = key(tenant,'package',job['request']['package_sha256'])
        package = self.store.get(package_key)
        started = time.monotonic()
        unit = 'kiro-analysis-'+job_id
        process = None
        try:
            if (not package or package['status'] not in ('received','verified') or package['expires_at']<=time.time()
                    or package.get('generation',1)!=job.get('package_generation',1)):
                self.finish(tenant,job_id,'failed',error={'code':'package_unavailable'})
                return
            if job['engine_sha256']!=self.engine['engine_sha256'] or job['analysis_sources']!=self.sources or sources()!=self.sources:
                self.finish(tenant,job_id,'failed',error={'code':'engine_changed'})
                return
            source_key = package.get('object_key') or package['staging_key']
            self.objects.client.download_file(self.objects.bucket,source_key,str(directory/'input.zip'))
            if (directory/'input.zip').stat().st_size!=package['bytes'] or file_sha256(directory/'input.zip')!=package['archive_sha256']:
                self.finish(tenant,job_id,'failed',error={'code':'package_integrity_failure'})
                return
            write_json(directory/'request.json',job['request'])
            for name in ('input.zip','request.json'):
                os.chmod(directory/name,0o444)
            command = ['systemd-run','--quiet','--wait','--collect','--pipe','--unit='+unit,
                '-p','User=kiroanalysis','-p','Group=kiroanalysis','-p','PrivateNetwork=yes',
                '-p','ProtectSystem=strict','-p','ProtectHome=yes','-p','PrivateTmp=yes',
                '-p','NoNewPrivileges=yes','-p','RestrictSUIDSGID=yes','-p','ProtectKernelTunables=yes',
                '-p','ProtectKernelModules=yes','-p','ProtectControlGroups=yes','-p','MemoryMax=2G',
                '-p','CPUQuota=400%','-p','RuntimeMaxSec=120','-p','TasksMax=64',
                '-p','ReadWritePaths='+str(output),'-p','RestrictAddressFamilies=AF_UNIX',
                '--setenv=PYTHONPATH='+str(ROOT),'--setenv=OPENCV_IO_MAX_IMAGE_PIXELS=2073600',
                sys.executable,'-m','services.analysis_api.runner',str(directory/'request.json'),str(output)]
            process = subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                       env={'PATH':'/usr/sbin:/usr/bin:/bin'})
            while process.poll() is None:
                if not self.heartbeat(tenant,job_id):
                    subprocess.run(['systemctl','stop',unit],capture_output=True,timeout=15)
                    process.wait(timeout=15)
                    self.finish(tenant,job_id,'cancelled')
                    return
                self.queue.client.change_message_visibility(QueueUrl=self.queue.url,ReceiptHandle=message['ReceiptHandle'],VisibilityTimeout=45)
                time.sleep(.5)
            from kiroclops.visual.schema import safe_path
            status_path=safe_path(output,'status.json')
            if status_path.exists() and status_path.stat().st_size>65536:raise ValueError('Oversized status file')
            state = read_json(status_path) if status_path.exists() else {'status':'failed','error':{'code':'worker_process_terminated'}}
            if state['status']!='succeeded' or process.returncode!=0:
                self.finish(tenant,job_id,'failed',error=state.get('error',{'code':'analysis_runtime_error'}))
                return
            if sources()!=self.sources or file_sha256(directory/'input.zip')!=package['archive_sha256']:
                self.finish(tenant,job_id,'failed',error={'code':'source_changed'})
                return
            from kiroclops.visual.schema import safe_path
            measurement_path=safe_path(output,'measurement.json')
            if measurement_path.stat().st_size>6*1024*1024:raise ValueError('Measurement exceeds result budget')
            measurement = read_json(measurement_path)
            delivery = envelope(measurement,job['request'],logical_job_id=job['logical_job_id'],engine=self.engine,sources=self.sources,
                                timings={'worker_wall_seconds':time.monotonic()-started},origin={'kind':'fresh','backend':'aws-standard-arm','ami_id':os.environ.get('AMI_ID')})
            validate_delivery(delivery,job['request'],logical_job_id=job['logical_job_id'],expected_engine_sha256=self.engine['engine_sha256'],expected_sources=self.sources)
            # Copy verified local bytes; the original signed upload URL cannot
            # mutate this content-addressed object after validation.
            verified_key = f"users/{tenant}/packages/{package['package_sha256']}.zip"
            self.objects.client.upload_file(str(directory/'input.zip'),self.objects.bucket,verified_key)
            current_package = self.store.get(package_key)
            current_account=self.store.get(key(tenant,'account'))
            if (not current_package or current_package['status']=='deleted' or not current_account
                    or current_account.get('data_state') in ('deleting','deleted')):
                self.objects.client.delete_object(Bucket=self.objects.bucket,Key=verified_key)
                self.finish(tenant,job_id,'cancelled')
                return
            self.store.transact([(package_key,current_package,{**current_package,'status':'verified','object_key':verified_key})])
            path = f'users/{tenant}/deliveries/{job_id}.json'
            self.put_json(path,delivery)
            self.finish(tenant,job_id,'succeeded',path)
            self.objects.client.delete_object(Bucket=self.objects.bucket,Key=package['staging_key'])
        except Exception:
            self.finish(tenant,job_id,'interrupted',error={'code':'worker_failure'})
        finally:
            if process and process.poll() is None:
                subprocess.run(['systemctl','stop',unit],capture_output=True,timeout=15)
                process.wait(timeout=15)
            shutil.rmtree(directory)
            for _ in range(8):
                job_key=key(tenant,'job',job_id);current=self.store.get(job_key)
                if not current or current.get('lease_owner')!=self.owner:break
                try:
                    self.store.transact([(job_key,current,{**current,'disk_cleaned_at':time.time()})])
                    break
                except Conflict:continue

    def loop(self):
        idle = time.monotonic()
        while time.monotonic()-idle < 300:
            budget=self.store.get('system#budget#worker')
            if not budget or time.time()-budget.get('last_accounted_at',0)>600 or not budget.get('compute_allowed',False):break
            messages = self.queue.client.receive_message(QueueUrl=self.queue.url,MaxNumberOfMessages=1,WaitTimeSeconds=10,VisibilityTimeout=45).get('Messages',[])
            for message in messages:
                value = json.loads(message['Body'])
                try:
                    job = self.claim(value['tenant'],value['job_id'])
                    if job is False:
                        continue
                    if job:
                        idle = time.monotonic()
                        self.run(value['tenant'],job,message)
                    self.queue.client.delete_message(QueueUrl=self.queue.url,ReceiptHandle=message['ReceiptHandle'])
                except Conflict:
                    continue
        self.put_json('service/stopped.json',{'reason':'idle','time':time.time()})
        subprocess.run(['shutdown','-h','now'],check=True)


if __name__=='__main__':
    WORK.mkdir(exist_ok=True,mode=0o711)
    # A stopped or killed VM can leave an interrupted workspace. No result is
    # taken from it; expired leases are reported separately by the controller.
    for stale in WORK.iterdir():
        if stale.resolve().parent!=WORK.resolve():raise ValueError('Unexpected stale workspace')
        if stale.is_dir() and not stale.is_symlink():shutil.rmtree(stale)
    Worker().loop()
