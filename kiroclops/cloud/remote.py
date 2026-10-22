"""Explicit remote AnalysisBackend; transport failures never cancel jobs."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import json
from pathlib import Path
import re
import time
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import uuid

from kiroclops.visual.package import load_package
from kiroclops.visual.schema import VisualError, digest, read_json, write_json
from .corpus import rebind
from .results import request_binding, validate_delivery, legacy_result
from .transport import pack, MAX_ARCHIVE_BYTES


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise HTTPError(request.full_url, code, 'Authenticated API redirects are prohibited', headers, fp)


class RemoteAnalysisBackend:
    def __init__(self, root, config, *, transport=None, token_provider=None):
        self.root = Path(root).resolve()
        self.config = deepcopy(config)
        base = config.get('api_url', '').rstrip('/')
        parsed = urlparse(base)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise VisualError('invalid_remote_config', 'An HTTPS API endpoint is required')
        if not config.get('engine_sha256') or not config.get('analysis_sources'):
            raise VisualError('invalid_remote_config', 'Pin the service engine and analysis sources before execution')
        self.base = base
        self.transport = transport
        self.token_provider = token_provider or self._token
        (self.root / 'operations').mkdir(parents=True, exist_ok=True)
        (self.root / 'jobs').mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _timed(self,phase):
        started=time.monotonic();cpu=time.process_time();finished=False
        try:
            yield
            finished=True
        finally:
            row={'phase':phase,'wall_seconds':time.monotonic()-started,'process_cpu_seconds':time.process_time()-cpu,
                 'finished_at':time.time(),'completed':finished,'scope':'client_process; nested phases must not be added twice'}
            # Diagnostics are outside immutable measurement/receipt hashes.
            # Never include URL, body, token, exception text or user paths.
            try:
                with (self.root/'transport-metrics.jsonl').open('a',encoding='utf8') as stream:
                    stream.write(json.dumps(row,allow_nan=False)+'\n')
            except OSError:pass  # Missing metrics are reported as unavailable.

    def _token(self):
        # Login stores a scoped capability separately from the MCP-visible
        # configuration. Same-user OS processes are not a secret boundary.
        path = self.config.get('capability_file')
        if not path:
            raise VisualError('cloud_login_required', 'A scoped service capability is required')
        try:
            from .credentials import load
            value = load(path)
        except (OSError, ValueError):
            raise VisualError('cloud_login_required', 'Service capability is unavailable') from None
        if value.get('expires_at', 0) <= time.time() or not value.get('access_token'):
            raise VisualError('cloud_login_required', 'Service capability expired')
        return value['access_token']

    def _call(self, method, path, body=None, key=None):
        with self._timed('api'):
            return self._call_once(method,path,body,key)

    def _call_once(self, method, path, body=None, key=None):
        if self.transport:
            return self.transport(method, path, body, key)
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None
        headers = {'Authorization': 'Bearer ' + self.token_provider(), 'Content-Type': 'application/json'}
        if self.config.get('scoped_agent'):
            from .credentials import load
            capability=load(self.config['capability_file']).get('service_capability')
            if not capability:raise VisualError('cloud_login_required','A session-scoped service capability is required')
            headers['X-KiroClops-Capability']=capability
        if key:
            headers['Idempotency-Key'] = key
        try:
            with build_opener(_NoRedirect()).open(Request(self.base + path, data=encoded, headers=headers, method=method), timeout=30) as response:
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise VisualError('invalid_remote_response', 'API response exceeds the byte budget')
                return json.loads(data)
        except HTTPError as exc:
            # Do not include response bodies/URLs: they can contain signed URLs
            # and tokens. No credentials are written to the job ledger.
            code = {401: 'cloud_login_required', 403: 'cloud_access_denied', 404: 'cloud_not_found',
                    409: 'cloud_conflict', 410:'cloud_data_deleted', 413: 'cloud_quota_exceeded', 429: 'cloud_quota_exceeded'}.get(exc.code, 'analysis_connection_lost')
            raise VisualError(code, f'Cloud API returned HTTP {exc.code}; existing job is preserved') from None
        except (URLError, TimeoutError, OSError) as exc:
            raise VisualError('analysis_connection_lost', 'Cloud connection interrupted; reconnect using the saved operation') from None
        except (json.JSONDecodeError, UnicodeError):
            raise VisualError('invalid_remote_response', 'Cloud API returned invalid JSON') from None

    def _signed_url(self, url):
        parsed = urlparse(url)
        host = parsed.hostname or ''
        allowed = self.config.get('object_hosts', [])
        if parsed.scheme != 'https' or parsed.username or parsed.password or host not in allowed:
            raise VisualError('invalid_remote_response', 'Object URL is outside the configured storage hosts')
        return url

    def _upload(self, url, file):
        with self._timed('upload'):
            return self._upload_bytes(url,file)

    def _upload_bytes(self, url, file):
        if self.transport:
            return self.transport('UPLOAD', '/object', {'local_file': str(file)}, None)
        # PNG packages are bounded. Avoid including any bearer token in this
        # separate S3 request, and never return its URL to CLI/MCP callers.
        from http.client import HTTPSConnection
        parsed = urlparse(self._signed_url(url))
        connection = HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=90)
        try:
            target = parsed.path + ('?' + parsed.query if parsed.query else '')
            connection.putrequest('PUT', target)
            connection.putheader('Content-Length', str(Path(file).stat().st_size))
            connection.endheaders()
            with Path(file).open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    connection.send(block)
            response = connection.getresponse()
            response.read(1024 * 1024)
            if response.status not in (200, 201, 204):
                raise VisualError('analysis_connection_lost', 'Package upload was not acknowledged; retry the saved operation')
        except (OSError, TimeoutError):
            raise VisualError('analysis_connection_lost', 'Package upload interrupted; retry the saved operation') from None
        finally:
            connection.close()

    def submit(self, request, idempotency_key=None):
        with self._timed('submit_total'):
            return self._submit(request,idempotency_key)

    def _submit(self, request, idempotency_key=None):
        from kiroclops.local_engine.backend import _lock
        if request.get('operation', 'analyze') != 'analyze':
            raise VisualError('unsupported_operation', 'Only pixel analysis can run remotely')
        logical = idempotency_key or uuid.uuid4().hex
        if not isinstance(logical, str) or not 1 <= len(logical) <= 256:
            raise VisualError('invalid_idempotency_key', 'Invalid logical operation ID')
        op_path = self.root / 'operations' / (digest(logical) + '.json')
        manifest = load_package(request['package_path'])
        original = request_binding(manifest['package_sha256'], request['selection'], request.get('contract'), request.get('candidate_sha256'))
        with _lock(self.root / 'submit.lock'):
            if op_path.exists():
                operation = read_json(op_path)
                if operation['original_request'] != original:
                    raise VisualError('idempotency_conflict', 'Operation ID belongs to another input or candidate')
            else:
                operation = {'schema_version':1, 'logical_job_id':logical, 'original_request':original,
                             'source_package':str(Path(request['package_path']).resolve()), 'job_id':None, 'status':'prepared'}
                write_json(op_path, operation)  # before the first outbound request
            if operation['job_id']:
                return {'job_id':operation['job_id'], 'status':operation['status']}
            archive = self.root / 'operations' / (digest(logical) + '.zip')
            if 'package_receipt' not in operation:
                # A crash after archive creation but before receipt persistence
                # restarts only this owned staging file, not the source package.
                archive.unlink(missing_ok=True)
                operation['package_receipt'] = pack(request['package_path'], archive)
                write_json(op_path, operation)
            package_receipt = operation['package_receipt']
            from kiroclops.visual.schema import file_sha256
            if file_sha256(archive) != package_receipt['archive_sha256']:
                raise VisualError('source_changed', 'Staged upload archive changed')
            remote = rebind(original, original['package_sha256'], package_receipt['package_sha256'])
            response = self._call('POST', '/packages/prepare', {
                **package_receipt, 'retention_class':request.get('retention_class',
                    'candidate' if original['candidate_sha256'] is not None else 'reference'),
                'index_for_similarity':False}, logical + '-package')
            if response.get('verified') is not True:
                self._upload(response['upload_url'], archive)
                self._call('POST', '/packages/complete', {'package_sha256':package_receipt['package_sha256'],
                           'archive_sha256':package_receipt['archive_sha256']}, logical + '-complete')
            response = self._call('POST', '/jobs', {'schema_version':2, 'logical_job_id':logical,
                'request':remote, 'engine_sha256':self.config['engine_sha256'],
                'analysis_sources_sha256':digest(self.config['analysis_sources'])}, logical)
            job_id = self._job_id(response['job_id'])
            operation.update(job_id=job_id, status=response['status'], remote_request=remote)
            write_json(op_path, operation)
            write_json(self.root / 'jobs' / job_id / 'operation.json', operation)
            return {'job_id':job_id, 'status':response['status']}

    @staticmethod
    def _job_id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
            raise VisualError('invalid_job', 'Invalid remote job ID')
        return value

    def get_job(self, job_id, wait_seconds=0):
        with self._timed('poll_and_deliver_total'):
            return self._get_job(job_id,wait_seconds)

    def _get_job(self, job_id, wait_seconds=0):
        job_id = self._job_id(job_id)
        if not isinstance(wait_seconds,(int,float)) or not 0 <= wait_seconds <= 60:
            raise VisualError('invalid_request', 'wait_seconds must be 0..60')
        directory = self.root / 'jobs' / job_id
        operation = read_json(directory / 'operation.json')
        deadline = time.monotonic() + wait_seconds
        while True:
            status = self._call('GET', '/jobs/' + job_id)
            if status.get('job_id') != job_id:
                raise VisualError('result_binding_mismatch', 'Status belongs to another job')
            if status['status'] in ('succeeded','failed','cancelled','interrupted') or time.monotonic() >= deadline:
                break
            time.sleep(min(.5, max(0., deadline-time.monotonic())))
        state = {'schema_version':1, 'kind':'AnalysisJob', 'job_id':job_id, 'status':status['status'],
                 'package_sha256':operation['original_request']['package_sha256'],
                 'candidate_sha256':operation['original_request']['candidate_sha256'],
                 'request_sha256':digest(operation['original_request']), 'result':None,
                 'error':({'code':str(status['error'].get('code','remote_failure'))[:80],
                           'message':'Remote analysis did not complete; consult the bounded service receipt'} if status.get('error') else None),
                 'progress':status.get('progress'),
                 'job_directory':str(directory), 'backend':'remote'}
        if status['status']=='succeeded':
            delivery = status.get('delivery')
            if not isinstance(delivery,dict):
                raise VisualError('invalid_remote_response','Completed job has no verified delivery')
            remote_result = legacy_result(delivery,operation['remote_request'],logical_job_id=operation['logical_job_id'],
                expected_engine_sha256=self.config['engine_sha256'],expected_sources=self.config['analysis_sources'])
            local_result = rebind(remote_result, operation['remote_request']['package_sha256'], operation['original_request']['package_sha256'])
            local_result['request_sha256'] = digest(operation['original_request'])
            if 'observation_sha256' in local_result['events']:
                local_result['events']['observation_sha256'] = digest(local_result['observations'])
            write_json(directory / 'delivery-v2.json', delivery)
            write_json(directory / 'result.json', local_result)
            write_json(directory / 'binding-receipt.json', {'policy':'portable-metadata-rebase-v1',
                'remote_measurement_sha256':delivery['measurement_sha256'],
                'original_request_sha256':digest(operation['original_request']),
                'remote_request_sha256':digest(operation['remote_request']), 'local_result_sha256':digest(local_result)})
            state.update(result=local_result,result_sha256=digest(local_result),result_path=str(directory/'result.json'))
        write_json(directory / 'state.json', {k:v for k,v in state.items() if k != 'result'})
        if state['status']=='succeeded' and self.config.get('acknowledge_delivery'):
            try:
                ack=self._call('POST','/jobs/'+job_id+'/ack',{'measurement_sha256':delivery['measurement_sha256'],
                    'receipt_sha256':delivery['receipt_sha256']},'ack-'+job_id)
                state['cloud_cleanup']=ack.get('candidate_cleanup')
            except VisualError as exc:
                # Local evidence was already validated and persisted. A failed
                # cleanup acknowledgement must not relabel a valid measurement.
                state['cloud_cleanup']='pending_acknowledgement'
                state['cleanup_error_code']=exc.code
            write_json(directory / 'state.json',{k:v for k,v in state.items() if k!='result'})
        return state

    def cancel_job(self, job_id):
        job_id = self._job_id(job_id)
        read_json(self.root / 'jobs' / job_id / 'operation.json')
        return self._call('POST','/jobs/'+job_id+'/cancel',{},'cancel-'+job_id)
