from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.visual.package import create_package
from kiroclops.visual.schema import digest, read_json, VisualError
from kiroclops.cloud.execution import execute, backend_for
from kiroclops.cloud.remote import RemoteAnalysisBackend
from kiroclops.cloud.results import envelope, make_measurement


class FakeService:
    def __init__(self, config):
        self.config = config
        self.jobs = {}; self.uploads = 0; self.cancelled = 0; self.lost = False
        self.fail_submit_once = False; self.fail_poll_once = False

    def __call__(self, method, path, body=None, key=None):
        if path == '/packages/prepare':
            return {'verified':self.uploads > 0,'upload_url':'https://private.example/object?secret=not-for-logs'}
        if method == 'UPLOAD':
            self.uploads += 1; return {}
        if path == '/packages/complete':return {}
        if path == '/jobs':
            if key not in self.jobs:
                request=body['request']
                measurement=make_measurement({'package_sha256':request['package_sha256'],'status':'ok','objects':[]}, {'events':[]})
                self.jobs[key]={'job_id':'a'*32,'status':'succeeded','delivery':envelope(measurement,request,
                    logical_job_id=body['logical_job_id'],engine=self.config['engine'],sources=self.config['analysis_sources'],timings={'wall_s':1})}
            if self.fail_submit_once:
                self.fail_submit_once=False
                raise VisualError('analysis_connection_lost','Response lost after successful POST')
            return {k:self.jobs[key][k] for k in ('job_id','status')}
        if path.endswith('/cancel'):
            self.cancelled+=1;return {'job_id':'a'*32,'status':'cancelled'}
        if path == '/jobs/'+'a'*32:
            if self.fail_poll_once:
                self.fail_poll_once=False
                raise VisualError('analysis_connection_lost','Temporary network loss')
            return next(iter(self.jobs.values()))
        raise AssertionError((method,path))


class CloudExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='kiroclops-café test ')
        self.root=Path(self.temp.name)
        frame=self.root/'pixel.png';frame.write_bytes(cv2.imencode('.png',np.random.default_rng(41).integers(0,255,(16,16,3),dtype=np.uint8))[1].tobytes())
        self.manifest=create_package(self.root/'package',[frame,frame],[0,.1],metadata={'path':'C:\\private'})
        engine={'identity':{'version':'test','threads':4}}
        engine['engine_sha256']=digest(engine['identity'])
        self.config={'api_url':'https://api.example','engine_sha256':engine['engine_sha256'],
                     'analysis_sources':{'tracking.py':'a'*64},'engine':engine}
        self.service=FakeService(self.config)
        self.backend=RemoteAnalysisBackend(self.root/'store',self.config,transport=self.service)

    def tearDown(self):
        import time
        # The Windows venv launcher's inherited log handle can close just
        # after its recorded Python child exits. Retry only that file race.
        deadline=time.monotonic()+5
        while True:
            try:self.temp.cleanup();break
            except OSError as exc:
                if getattr(exc,'winerror',None) not in (32,267) or time.monotonic()>=deadline:raise
                time.sleep(.05)

    def test_lost_post_response_reconnects_to_same_job_and_operation(self):
        self.service.fail_submit_once=True
        with self.assertRaises(VisualError):
            execute(self.root/'package',{},self.root/'out',backend=self.backend,candidate_sha256='c'*64)
        first=read_json(self.root/'out/analysis-operation.json')
        result=execute(self.root/'package',{},self.root/'out',backend=self.backend,candidate_sha256='c'*64)
        second=read_json(self.root/'out/analysis-operation.json')
        self.assertEqual(first['logical_job_id'],second['logical_job_id'])
        self.assertEqual(1,len(self.service.jobs));self.assertEqual(1,self.service.uploads)
        self.assertEqual(0,self.service.cancelled)
        self.assertEqual(self.manifest['package_sha256'],result['package_sha256'])
        self.assertEqual('c'*64,result['candidate_sha256'])
        self.assertTrue((self.root/'store/jobs'/('a'*32)/'delivery-v2.json').exists())
        for p in (self.root/'store').rglob('*.json'):
            self.assertNotIn('secret=not-for-logs',p.read_text(encoding='utf-8'))

    def test_poll_disconnect_preserves_job_explicit_cancel_stops_it(self):
        self.service.fail_poll_once=True
        with self.assertRaises(VisualError):execute(self.root/'package',{},self.root/'out',backend=self.backend)
        self.assertEqual(0,self.service.cancelled)
        execute(self.root/'package',{},self.root/'out',backend=self.backend)
        checks=[0]
        def cancel():
            checks[0]+=1
            if checks[0]>1:raise VisualError('job_cancelled','Explicit cancellation')
        with self.assertRaises(VisualError):execute(self.root/'package',{},self.root/'out',backend=self.backend,check=cancel)
        self.assertEqual(1,self.service.cancelled)
        def immediate_cancel():raise VisualError('job_cancelled','Already cancelled before reconnect')
        with self.assertRaises(VisualError):execute(self.root/'package',{},self.root/'out',backend=self.backend,check=immediate_cancel)
        self.assertEqual(2,self.service.cancelled)

    def test_candidate_change_cannot_reuse_old_local_job(self):
        execute(self.root/'package',{},self.root/'out',backend=self.backend,candidate_sha256='a'*64)
        with self.assertRaises(VisualError) as error:
            execute(self.root/'package',{},self.root/'out',backend=self.backend,candidate_sha256='b'*64)
        self.assertEqual('operation_conflict',error.exception.code)

    def test_unknown_or_unconfigured_remote_never_falls_back(self):
        with patch.dict('os.environ',{'KIROCLOPS_ANALYSIS_BACKEND':'remote','KIROCLOPS_CLOUD_CONFIG':''}):
            with self.assertRaises(VisualError) as error:backend_for(self.root/'jobs')
        self.assertEqual('remote_not_configured',error.exception.code)
        with patch.dict('os.environ',{'KIROCLOPS_ANALYSIS_BACKEND':'surprise'}):
            with self.assertRaises(VisualError):backend_for(self.root/'jobs')

    def test_reference_retention_and_metrics_do_not_change_measurement(self):
        requests=[]
        def transport(method,path,body=None,key=None):
            if path=='/packages/prepare':requests.append(body)
            return self.service(method,path,body,key)
        backend=RemoteAnalysisBackend(self.root/'timed-store',self.config,transport=transport)
        result=execute(self.root/'package',{},self.root/'timed',backend=backend)
        self.assertEqual(requests[0]['retention_class'],'reference')
        rows=(backend.root/'transport-metrics.jsonl').read_text('utf8')
        self.assertNotIn('secret=not-for-logs',rows);self.assertNotIn(str(self.root),rows)
        import json
        metrics=[json.loads(line) for line in rows.splitlines()]
        self.assertTrue({'api','upload','submit_total','poll_and_deliver_total'}<={r['phase'] for r in metrics})
        self.assertNotIn('timings',result)
        backend.submit({'package_path':str(self.root/'package'),'selection':{},'candidate_sha256':'c'*64})
        self.assertEqual(requests[-1]['retention_class'],'candidate')

    def test_unsigned_object_host_and_unpinned_engine_rejected(self):
        with self.assertRaises(VisualError):self.backend._signed_url('https://other.example/object')
        config=dict(self.config);config.pop('engine_sha256')
        with self.assertRaises(VisualError):RemoteAnalysisBackend(self.root/'other',config)

    def test_cleanup_ack_failure_keeps_valid_local_result_and_retries(self):
        attempts=[]
        original=self.service
        def service(method,path,body=None,key=None):
            if path.endswith('/ack'):
                attempts.append(body)
                if len(attempts)==1:raise VisualError('analysis_connection_lost','Retry cleanup')
                return {'candidate_cleanup':'complete_or_not_required'}
            return original(method,path,body,key)
        backend=RemoteAnalysisBackend(self.root/'ack-store',{**self.config,'acknowledge_delivery':True},transport=service)
        submitted=backend.submit({'package_path':str(self.root/'package'),'selection':{},'contract':{}})
        first=backend.get_job(submitted['job_id'])
        self.assertEqual(first['status'],'succeeded');self.assertEqual(first['cloud_cleanup'],'pending_acknowledgement')
        second=backend.get_job(submitted['job_id'])
        self.assertEqual(second['result_sha256'],first['result_sha256'])
        self.assertEqual(second['cloud_cleanup'],'complete_or_not_required')
        self.assertEqual(attempts[0],attempts[1])

    def test_local_worker_receipt_is_required_but_historical_v1_stays_readable(self):
        from kiroclops.local_engine.backend import LocalAnalysisBackend, _atomic, _read, _alive
        import time
        local=LocalAnalysisBackend(self.root/'local')
        selection={'schema_version':1,'objects':[{'id':'box','roi':[0,0,4,4],'anchor':[2,2],
                   'method':'template','frame_index':0,'min_score':0,'min_margin':0,'search_radius':2}], 'background_rois':[]}
        submitted=local.submit({'package_path':str(self.root/'package'),'selection':selection,'contract':{}})
        done=local.get_job(submitted['job_id'],wait_seconds=30)
        self.assertEqual('succeeded',done['status'],done)
        directory=Path(done['job_directory'])
        # A terminal result is durable before Python closes worker.log. Wait
        # for this owned child to exit before deliberately editing its evidence
        # and deleting the Windows temporary directory.
        worker=_read(directory/'state.json').get('worker')
        deadline=time.monotonic()+5
        while _alive(worker) and time.monotonic()<deadline:time.sleep(.025)
        self.assertFalse(_alive(worker),'Completed worker did not exit')
        delivery=read_json(directory/'delivery-v2.json')
        self.assertEqual(2,delivery['schema_version'])
        # Historical v1 readers keep their original result format and digest.
        original=_read(directory/'state.json');legacy=dict(original)
        legacy.pop('delivery_schema_version');legacy.pop('delivery_sha256')
        _atomic(directory/'state.json',legacy)
        self.assertEqual('succeeded',local.get_job(submitted['job_id'])['status'])
        _atomic(directory/'state.json',original)
        (directory/'delivery-v2.json').unlink()
        rejected=local.get_job(submitted['job_id'])
        self.assertEqual('failed',rejected['status'])
        self.assertEqual('result_integrity_failure',rejected['error']['code'])


if __name__=='__main__':unittest.main()
