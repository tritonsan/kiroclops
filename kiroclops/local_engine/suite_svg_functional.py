"""Owned SVG functional rerun and independent witness, separate from pixels.

Requests and execution receipts are not attestations. The checker runs only
through the explicit run API, in a fresh child under the common native lease.
Read-only validation requires a named independent witness of the retained
actions, states, source, browser and actual owned execution. Nothing from this
private DOM oracle enters tracking, events, profiles or reference fitting.
"""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json
from . import suite_initial_acceptance as initial, suite_revision, suite_web
from .backend import _atomic, _identity, _lock
from .reliability import COMMON_CAPTURE_LOCK, runtime_doctor
from .session import _ordinary, source_hashes

VERSION = 1
MAX_SECONDS = 120
CHECKS = frozenset(('selection_add_remove', 'resize_structure', 'pointer_cancel', 'rapid_mode_changes', 'reset'))


def _need(value, message, code='incomplete_svg_functional'):
    if not value:
        raise VisualError(code, message)


def _sources():
    root = Path(__file__).resolve().parents[2]
    paths = [Path(__file__).resolve(), root / 'scripts/phase3d_svg_functional.py',
             Path(suite_web.__file__).resolve(), root / 'kiroclops/local_engine/backend.py',
             root / 'kiroclops/local_engine/environment.py', root / 'kiroclops/local_engine/reliability.py',
             root / 'kiroclops/web_local/capture.py', root / 'kiroclops/web_local/natural_capture.py']
    import importlib.util
    spec = importlib.util.find_spec('playwright')
    _need(spec is not None and spec.origin, 'Installed Playwright package is absent.')
    driver = Path(spec.origin).resolve().parent / 'driver/package'
    paths.extend((driver / 'browsers.json', driver / 'lib/coreBundle.js'))
    package = Path(spec.origin).resolve().parent
    paths.extend(package / name for name in ('async_api/__init__.py', 'async_api/_generated.py',
        '_impl/_async_base.py', '_impl/_browser_type.py', '_impl/_browser.py', '_impl/_page.py',
        '_impl/_locator.py', '_impl/_input.py', '_impl/_connection.py', '_impl/_transport.py', '_impl/_driver.py'))
    paths.append(root / 'kiroclops/integration/kiro.py')
    return {str(p): file_sha256(p) for p in paths}


def _browser_inventory(environment):
    """Small exact installed-revision inventory, without starting Playwright.

    The actual executable is identified later from the owned Node launch log.
    BrowserType.executable_path alone cannot prove the headless-shell binary.
    """
    import importlib.util
    spec = importlib.util.find_spec('playwright')
    registry = read_json(Path(spec.origin).resolve().parent / 'driver/package/browsers.json')
    if 'PLAYWRIGHT_BROWSERS_PATH' in environment:
        store = _ordinary(environment['PLAYWRIGHT_BROWSERS_PATH'])
    else:
        bundled = Path(__file__).resolve().parents[2] / '.tools/browser-local'
        store = _ordinary(bundled if bundled.is_dir() else Path(os.environ['LOCALAPPDATA']) / 'ms-playwright')
    _need(os.name == 'nt', 'This qualified functional runtime requires the registered Windows browser layout.')
    paths = []
    for browser in registry['browsers']:
        if browser['name'] not in ('chromium', 'chromium-headless-shell'):
            continue
        root = store / (browser['name'].replace('-', '_') + '-' + browser['revision'])
        relatives = ('chrome-win64/chrome.exe', 'chrome-win/chrome.exe') if browser['name'] == 'chromium' else (
            'chrome-headless-shell-win64/chrome-headless-shell.exe', 'chrome-win/headless_shell.exe')
        paths.extend(_ordinary(root / name) for name in relatives if (root / name).is_file())
    _need(paths, 'Current installed Chromium revision has no verifiable executable.')
    return {str(p): file_sha256(p) for p in paths}


def _launched_browser(stderr, request, observation):
    text = re.sub(r'\x1b\[[0-9;]*m', '', stderr)
    launching = [line.split('<launching> ', 1)[1] for line in text.splitlines() if '<launching> ' in line]
    launched = [int(v) for v in re.findall(r'<launched> pid=(\d+)', text)]
    _need(len(launching) == len(launched) == 1 and launched[0] > 0,
          'Actual owned browser launch transcript is absent, ambiguous, or foreign.')
    matching = [path for path in request['browser_inventory'] if launching[0].startswith(path + ' ')]
    _need(len(matching) == 1, 'Launched browser is outside the exact pre-execution binary inventory.')
    path = matching[0]
    _need(file_sha256(_ordinary(path)) == request['browser_inventory'][path], 'Actual browser binary changed.')
    version = observation['browser']['version']
    _need(isinstance(version, str) and version, 'Observed browser version is absent.')
    _need(re.search(r'\[pid=' + str(launched[0]) + r'\] <process did exit: exitCode=0, signal=null>', text) is not None,
          'Owned browser process did not record a clean exit.')
    from kiroclops.web_local.natural_capture import browser_arguments
    _need(all(arg in launching[0].split(' ') for arg in browser_arguments(True)),
          'Owned browser launch did not retain the shared functional launch arguments.')
    return {**observation['browser'], 'executable': path, 'executable_sha256': request['browser_inventory'][path],
            'browser_pid': launched[0], 'launch_transcript_sha256': digest(launching[0])}


def _snapshot_script():
    tree = ast.parse(inspect.getsource(suite_web._functional_browser_checks))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'snapshot')
    call = next(n for n in ast.walk(node) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == 'evaluate')
    _need(isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str),
          'Shared checker snapshot expression is unsupported; do not infer its policy.')
    return call.args[0].value


GEOMETRY = """() => [...document.querySelectorAll('#panel,#panel-handle,.bookmark,.star,[data-node-id]')]
 .map(e=>{const b=e.getBoundingClientRect();return {id:e.dataset.nodeId||e.id||e.className.baseVal||String(e.className),
 node_id:e.dataset.nodeId||null,box:[b.x,b.y,b.width,b.height]};})"""


class _Trace:
    def __init__(self):
        self.rows, self.errors, self.browser = [], [], None

    async def call(self, name, fn, *args, **kwargs):
        row = {'action': name, 'args': list(args), 'kwargs': kwargs, 'started_at': time.time()}
        try:
            value = await fn(*args, **kwargs)
            # Page.goto's Response belongs to Playwright and is not JSON. Keep
            # actual bounded response fields and return the real object to the
            # unchanged checker. Every other supported operation is JSON/None.
            saved = {'url': value.url, 'status': value.status, 'ok': value.ok} if name == 'page.goto' and value is not None else value
            row.update(finished_at=time.time(), value=saved)
            self.rows.append(row)
            return value
        except BaseException as error:
            row.update(finished_at=time.time(), error=type(error).__name__)
            self.rows.append(row)
            raise


class _Methods:
    def __init__(self, target, trace, prefix):
        self.target, self.trace, self.prefix = target, trace, prefix

    def __getattr__(self, name):
        async def invoke(*args, **kwargs):
            return await self.trace.call(self.prefix + '.' + name, getattr(self.target, name), *args, **kwargs)
        return invoke


class _Page:
    def __init__(self, target, trace):
        self.target, self.trace = target, trace
        self.mouse = _Methods(target.mouse, trace, 'mouse')

    def set_default_timeout(self, value):
        self.target.set_default_timeout(value)

    def on(self, name, callback):
        self.target.on(name, callback)
        if name == 'pageerror':
            self.target.on(name, lambda error: self.trace.errors.append(str(error)))

    def locator(self, selector):
        return _Methods(self.target.locator(selector), self.trace, 'locator:' + selector)

    async def evaluate(self, expression):
        if expression == _snapshot_script():
            value = await self.trace.call('snapshot', self.target.evaluate, expression)
            # Additional private observations only; shared predicate inputs and
            # waits are unchanged. These never cross into visual measurement.
            geometry = await self.target.evaluate(GEOMETRY)
            self.trace.rows[-1]['geometry'] = geometry
            return value
        return await self.trace.call('evaluate', self.target.evaluate, expression)

    def __getattr__(self, name):
        async def invoke(*args, **kwargs):
            return await self.trace.call('page.' + name, getattr(self.target, name), *args, **kwargs)
        return invoke


class _Browser:
    def __init__(self, target, trace):
        self.target, self.trace = target, trace

    async def new_page(self, **kwargs):
        target = await self.target.new_page(**kwargs)
        now = time.time()
        self.trace.rows.append({'action': 'browser.new_page', 'args': [], 'kwargs': kwargs,
                                'started_at': now, 'finished_at': now, 'value': None})
        return _Page(target, self.trace)

    async def close(self):
        # Do not put a Browser object in a serializable trace.
        return await self.trace.call('browser.close', self.target.close)


class _Chromium:
    def __init__(self, target, trace):
        self.target, self.trace = target, trace

    async def launch(self, **kwargs):
        browser = await self.target.launch(**kwargs)
        self.trace.browser = {'version': browser.version, 'launch_options': kwargs}
        return _Browser(browser, self.trace)


class _PlaywrightContext:
    def __init__(self, factory, trace):
        self.context, self.trace = factory(), trace

    async def __aenter__(self):
        target = await self.context.__aenter__()
        class Proxy:
            pass
        proxy = Proxy()
        proxy.chromium = _Chromium(target.chromium, self.trace)
        return proxy

    async def __aexit__(self, *args):
        return await self.context.__aexit__(*args)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _snapshot(row):
    value, geometry = row.get('value'), row.get('geometry')
    _need(isinstance(value, dict) and set(value) == {'finite', 'count', 'selected', 'pointer_capture', 'value', 'max'}
          and type(value['finite']) is bool and type(value['pointer_capture']) is bool
          and type(value['count']) is int and value['count'] >= 0
          and isinstance(value['selected'], list) and len(set(value['selected'])) == len(value['selected'])
          and all(isinstance(v, str) and v for v in value['selected'])
          and value['value'] is None and value['max'] is None,
          'SVG functional snapshot has invalid types or foreign panel fields.')
    _need(isinstance(geometry, list) and len(geometry) >= 3
          and all(isinstance(g, dict) and set(g) == {'id', 'node_id', 'box'} and isinstance(g['id'], str)
                  and (g['node_id'] is None or isinstance(g['node_id'], str) and bool(g['node_id']))
                  and isinstance(g['box'], list) and len(g['box']) == 4 and all(_finite(v) for v in g['box'])
                  and g['box'][2] >= 0 and g['box'][3] >= 0 for g in geometry),
          'Functional geometry is missing or nonfinite.')
    nodes = [g['node_id'] for g in geometry if g['node_id'] is not None]
    _need(len(nodes) == len(set(nodes)) == value['count'] and set(value['selected']).issubset(nodes),
          'Actual node geometry/count/selection identities disagree.')
    _need(value['finite'] is True, 'Observed functional state was not finite.')
    return value


def replay_trace(trace):
    """Recompute all five shared predicates from actual retained actions/states."""
    rows = trace.get('actions', [])
    expected = [('browser.new_page', [], {'viewport': {'width': 960, 'height': 640}, 'device_scale_factor': 1}),
        ('page.add_init_script', ["document.addEventListener('pointerdown',e=>window.__kcSuitePointerId=e.pointerId,true)"], {}),
        ('page.goto', None, {'wait_until': 'networkidle'}), ('locator:#reset.click', [], {}),
        ('locator:[data-node-id="b"].click', [], {}), ('locator:#add-node.click', [], {}),
        ('page.wait_for_timeout', [600], {}), ('snapshot', [_snapshot_script()], {}),
        ('locator:#remove-node.click', [], {}), ('page.wait_for_timeout', [600], {}), ('snapshot', [_snapshot_script()], {}),
        ('page.set_viewport_size', [{'width': 1280, 'height': 720}], {}), ('page.wait_for_timeout', [600], {}),
        ('snapshot', [_snapshot_script()], {}), ('locator:[data-node-id="b"].bounding_box', [], {}),
        ('mouse.move', None, {}), ('mouse.down', [], {}), ('snapshot', [_snapshot_script()], {}),
        ('evaluate', ['window.__kcSuitePointerId||1'], {}), ('locator:[data-node-id="b"].dispatch_event', None, {}),
        ('mouse.up', [], {}), ('page.wait_for_timeout', [600], {}), ('snapshot', [_snapshot_script()], {})]
    for selector in ('#add-node', '#remove-node', '#add-node'):
        expected.extend([('locator:' + selector + '.click', [], {}), ('page.wait_for_timeout', [40], {})])
    expected.extend([('page.wait_for_timeout', [800], {}), ('snapshot', [_snapshot_script()], {}),
        ('locator:#reset.click', [], {}), ('page.wait_for_timeout', [600], {}), ('snapshot', [_snapshot_script()], {}),
        ('browser.close', [], {})])
    _need(len(rows) == len(expected), 'Functional execution omitted, inserted or reordered an action.')
    previous = -math.inf
    for row, (action, args, kwargs) in zip(rows, expected):
        _need(row.get('action') == action and digest(row.get('kwargs')) == digest(kwargs)
              and (args is None or digest(row.get('args')) == digest(args)) and 'error' not in row
              and _finite(row.get('started_at')) and _finite(row.get('finished_at'))
              and previous <= row['started_at'] <= row['finished_at'], 'Functional action/chronology changed.')
        previous = row['finished_at']
    url = rows[2]['args']
    from urllib.parse import urlparse
    _need(isinstance(url, list) and len(url) == 1 and isinstance(url[0], str)
          and urlparse(url[0]).scheme == 'http' and urlparse(url[0]).hostname in ('127.0.0.1', 'localhost')
          and not urlparse(url[0]).username and not urlparse(url[0]).password,
          'Functional producer did not use the owned localhost server.')
    response = rows[2].get('value')
    _need(isinstance(response, dict) and set(response) == {'url', 'status', 'ok'}
          and response['url'] == url[0] and type(response['status']) is int and response['status'] == 200
          and response['ok'] is True, 'Actual source-bound localhost response is missing or failed.')
    box = rows[14].get('value')
    _need(isinstance(box, dict) and set(box) == {'x', 'y', 'width', 'height'}
          and all(_finite(v) for v in box.values()) and box['width'] > 0 and box['height'] > 0
          and digest(rows[15]['args']) == digest([box['x'] + box['width'] / 2, box['y'] + box['height'] / 2]),
          'Functional drag did not use its observed target box.')
    pointer = rows[18].get('value')
    _need(type(pointer) is int and pointer > 0 and digest(rows[19]['args']) == digest(['pointercancel', {'pointerId': pointer, 'bubbles': True}]),
          'Functional pointer cancellation identity changed.')
    values = [_snapshot(row) for row in rows if row['action'] == 'snapshot']
    _need(len(values) == 7, 'Every required actual state snapshot must be retained.')
    added, removed, resized, captured, released, rapid, reset = values
    checks = [
        {'id': 'selection_add_remove', 'pass': added['count'] == 4 and removed['count'] == 3
            and added['selected'] == removed['selected'] == ['b']},
        {'id': 'resize_structure', 'pass': resized['count'] == 3 and resized['selected'] == ['b']},
        {'id': 'pointer_cancel', 'pass': captured['pointer_capture'] and not released['pointer_capture'],
            'cancel_event_provenance': 'synthetic_functional_event'},
        {'id': 'rapid_mode_changes', 'pass': not rapid['pointer_capture'] and rapid['count'] == 4 and rapid['selected'] == ['b']},
        {'id': 'reset', 'pass': not reset['pointer_capture'] and reset['count'] == 3 and not reset['selected']}]
    errors = trace.get('page_errors')
    _need(isinstance(errors, list) and all(isinstance(v, str) for v in errors), 'Page error observations are absent.')
    return {'checks': checks, 'page_errors': errors, 'status': 'pass' if all(c['pass'] for c in checks) and not errors else 'fail'}


def _context(directory):
    directory = _ordinary(directory)
    plan, _ = initial.load_plan(directory)
    result = initial.load_result(directory)
    _need(result.get('complete') is True and result.get('numerical_pass') is True
          and result.get('new_model_generation') is False, 'Functional frontier needs the genuine complete unchanged result.')
    return directory, plan, result


def prepare_request(directory, output, *, runtime_root=None, browser_path=None):
    """Create an unsigned execution request without starting or preparing runtime."""
    if runtime_root is None:
        from .reliability import DEFAULT_RUNTIME_ROOT
        runtime_root = DEFAULT_RUNTIME_ROOT
    directory, plan, result = _context(directory)
    output = _ordinary(output)
    _need(not output.exists(), 'Preserve previous functional requests.', 'output_exists')
    _need(not output.is_relative_to(directory) and not output.is_relative_to(_ordinary(plan['suite']))
          and not output.is_relative_to(_ordinary(plan['authoring_parent'])), 'Keep request outside immutable evidence/source.')
    runtime = runtime_doctor(runtime_root)
    _need(runtime['status'] == 'ready', 'Existing runtime driver must be prepared before this separate run.')
    environment = {'PLAYWRIGHT_NODEJS_PATH': runtime['driver']['path']}
    if browser_path is not None:
        browser_path = _ordinary(browser_path)
        _need(browser_path.is_dir(), 'Explicit browser store does not exist.')
        environment['PLAYWRIGHT_BROWSERS_PATH'] = str(browser_path)
    request = {'kind': 'UnsignedUnchangedSVGFunctionalRequest', 'schema_version': VERSION,
        'created_at': time.time(), 'status': 'pending_execution_and_independent_witness',
        'initial_acceptance_directory': str(directory), 'plan_sha256': digest(plan), 'initial_result_sha256': digest(result),
        'source': str(_ordinary(plan['unchanged_source'])), 'source_hashes': source_hashes(plan['unchanged_source']),
        'source_sha256': plan['unchanged_source_sha256'], 'measurement_source_hashes': suite_revision.measurement_source_hashes('svg'),
        'functional_source_hashes': _sources(), 'required_checks': sorted(CHECKS), 'max_seconds': MAX_SECONDS,
        'capture_lock': str(_ordinary(COMMON_CAPTURE_LOCK)), 'runtime': runtime, 'environment': environment,
        'browser_inventory': _browser_inventory(environment),
        'new_model_generation': False, 'functional_qualified': False, 'full_behavior_acceptance': False}
    write_json(output, request)
    return request


def _request(path):
    request = read_json(path)
    directory, plan, result = _context(request['initial_acceptance_directory'])
    _need(request.get('kind') == 'UnsignedUnchangedSVGFunctionalRequest' and request.get('schema_version') == VERSION
          and request.get('status') == 'pending_execution_and_independent_witness' and request.get('plan_sha256') == digest(plan)
          and request.get('initial_result_sha256') == digest(result) and request.get('source') == plan['unchanged_source']
          and request.get('source_hashes') == source_hashes(request['source']) == plan['authoring_parent_source_hashes']
          and request.get('source_sha256') == digest(request['source_hashes']) == plan['unchanged_source_sha256']
          and request.get('measurement_source_hashes') == suite_revision.measurement_source_hashes('svg')
          and request.get('functional_source_hashes') == _sources() and request.get('required_checks') == sorted(CHECKS)
          and request.get('capture_lock') == str(_ordinary(COMMON_CAPTURE_LOCK)) and request.get('max_seconds') == MAX_SECONDS
          and request.get('new_model_generation') is False and request.get('functional_qualified') is False
          and request.get('full_behavior_acceptance') is False and _finite(request.get('created_at')),
          'Functional request/source/checker/measurement identity is stale or foreign.')
    runtime = runtime_doctor(request['runtime']['root'])
    _need(runtime == request['runtime'] and runtime['status'] == 'ready', 'Functional runtime changed.')
    env = request.get('environment', {})
    _need(set(env) in ({'PLAYWRIGHT_NODEJS_PATH'}, {'PLAYWRIGHT_NODEJS_PATH', 'PLAYWRIGHT_BROWSERS_PATH'})
          and env.get('PLAYWRIGHT_NODEJS_PATH') == runtime['driver']['path'], 'Functional browser environment is foreign.')
    if 'PLAYWRIGHT_BROWSERS_PATH' in env:
        _need(_ordinary(env['PLAYWRIGHT_BROWSERS_PATH']).is_dir(), 'Functional browser store is absent.')
    _need(request.get('browser_inventory') == _browser_inventory(env), 'Pre-execution browser binaries changed.')
    return request


class _OwnedJob:
    """Reuse the existing suspended-launch Windows job, retaining its handle.

    Only this exact job handle controls cleanup. Accounting reaches zero before
    the common lease can be released, even when the venv launcher exits first.
    """
    def __init__(self):
        from kiroclops.integration.kiro import _WindowsJob
        _need(os.name == 'nt', 'Owned SVG functional execution is Windows-qualified only.')
        self.job = _WindowsJob()
        self.c, self.w, self.k = self.job.ctypes, self.job.w, self.job.kernel
        c, w, k = self.c, self.w, self.k
        k.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)]
        k.QueryInformationJobObject.restype = w.BOOL
        k.TerminateJobObject.argtypes, k.TerminateJobObject.restype = [w.HANDLE, w.UINT], w.BOOL
        k.IsProcessInJob.argtypes, k.IsProcessInJob.restype = [w.HANDLE, w.HANDLE, c.POINTER(w.BOOL)], w.BOOL
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.GetCurrentProcess.restype = w.HANDLE
        k.DuplicateHandle.argtypes = [w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD]
        k.DuplicateHandle.restype = w.BOOL

    def assign_and_resume(self, process):
        self.job.assign_and_resume(process)

    def contains(self, process):
        _need(_identity(process['pid']) == process['identity'], 'Actual owned child creation identity changed.')
        handle = self.k.OpenProcess(0x1000, False, process['pid'])
        _need(bool(handle), 'Actual owned child cannot be queried.')
        try:
            member = self.w.BOOL()
            _need(self.k.IsProcessInJob(handle, self.job.handle, self.c.byref(member)), 'Exact job membership cannot be verified.')
            return bool(member.value)
        finally:
            self.k.CloseHandle(handle)

    def duplicate_for(self, process):
        _need(self.contains(process), 'Worker does not belong to the exact parent-owned job.')
        target = self.k.OpenProcess(0x40, False, process['pid'])  # PROCESS_DUP_HANDLE
        _need(bool(target), 'Owned worker cannot receive its exact job membership handle.')
        try:
            duplicate = self.w.HANDLE()
            _need(_identity(process['pid']) == process['identity'] and self.k.DuplicateHandle(self.k.GetCurrentProcess(),
                  self.job.handle, target, self.c.byref(duplicate), 0, False, 2), 'Exact worker job-handle transfer failed.')
            return int(duplicate.value)
        finally:
            self.k.CloseHandle(target)

    def active(self):
        c, w = self.c, self.w
        class Accounting(c.Structure):
            _fields_ = [(n, c.c_longlong) for n in ('user', 'kernel', 'period_user', 'period_kernel')] + [
                (n, w.DWORD) for n in ('faults', 'total', 'active', 'terminated')]
        value, size = Accounting(), w.DWORD()
        _need(self.k.QueryInformationJobObject(self.job.handle, 1, c.byref(value), c.sizeof(value), c.byref(size)),
              'Owned job accounting is unavailable.', 'owned_cleanup_pending')
        return int(value.active)

    def stop_and_confirm(self, output):
        # This safety wait is intentionally independent of the execution budget.
        # Never release a capture lease while this exact job retains a live owner.
        while True:
            try:
                stopped = bool(self.k.TerminateJobObject(self.job.handle, 130))
                active = self.active()
                if active == 0:
                    return
                _atomic(output / 'cleanup-pending.json', {'status': 'cleanup_pending', 'active_owned_processes': active,
                    'termination_requested': stopped, 'capture_lease_retained': True, 'updated_at': time.time()})
            except (VisualError, OSError, KeyboardInterrupt) as error:
                try:
                    _atomic(output / 'cleanup-pending.json', {'status': 'cleanup_pending', 'capture_lease_retained': True,
                                                              'message': str(error), 'updated_at': time.time()})
                except (OSError, KeyboardInterrupt):
                    pass  # Failed persistence cannot release a live owned tree.
            try:
                time.sleep(.05)
            except KeyboardInterrupt:
                pass  # Cleanup retains the lease until this exact job is empty.

    def close(self):
        self.job.close()


def _live(process):
    return isinstance(process, dict) and set(process) == {'pid', 'identity'} and type(process['pid']) is int \
        and process['pid'] > 0 and isinstance(process['identity'], str) and bool(process['identity']) \
        and _identity(process['pid']) == process['identity']


def _startup(request, output, challenge):
    """Fail before writing anything/native work without the actual live owner."""
    proof = read_json(output / 'execution-start.json')
    command = [sys.executable, '-X', 'utf8', '-m', 'scripts.phase3d_svg_functional', '_worker',
               '--request', str(output / 'request.json'), '--output', str(output)]
    _need(proof.get('kind') == 'OwnedSVGFunctionalExecution' and proof.get('schema_version') == VERSION
          and proof.get('status') == 'running' and proof.get('request_sha256') == digest(request)
          and proof.get('request_file_sha256') == file_sha256(output / 'request.json')
          and proof.get('command') == command and proof.get('challenge_sha256') == digest(challenge)
          and proof.get('functional_source_hashes') == _sources() and proof.get('environment') == request['environment']
          and proof.get('capture_lock') == request['capture_lock'] and proof.get('common_lease_acquired') is True
          and digest(proof.get('owned_job')) == digest({'kind': 'WindowsKillOnCloseJob', 'assigned_before_resume': True})
          and _live(proof.get('parent')) and _live(proof.get('child_launcher'))
          and os.getppid() in (proof['parent']['pid'], proof['child_launcher']['pid'])
          and _finite(proof.get('launched_at')) and request['created_at'] <= proof['launched_at'] <= time.time()
          and time.time() - proof['launched_at'] <= MAX_SECONDS,
          'Public worker invocation has no genuine live, source-bound owned startup.')
    _need(not any((output / name).exists() for name in ('worker-start.json', 'worker-result.json', 'observations.json', 'binding.json')),
          'A worker cannot overwrite an earlier execution.', 'output_exists')
    # A free lock proves the purported live owner did not retain the common lease.
    try:
        with _lock(Path(request['capture_lock']), timeout=0):
            raise VisualError('unowned_functional_start', 'The purported parent does not hold the common capture lease.')
    except TimeoutError:
        pass  # Existing OS lease primitive observed the retained parent lock.
    return proof


def _authorization(request, output, start, owner, authorization):
    value = read_json(output / 'worker-authorization.json')
    _need(isinstance(authorization, dict) and set(authorization) == {'challenge', 'authorization_sha256'}
          and isinstance(authorization['challenge'], str) and re.fullmatch('[0-9a-f]{64}', authorization['challenge'])
          and authorization['authorization_sha256'] == file_sha256(output / 'worker-authorization.json')
          and value.get('kind') == 'OwnedSVGFunctionalWorkerAuthorization' and value.get('schema_version') == VERSION
          and value.get('request_sha256') == digest(request) and value.get('worker_start_sha256') == digest(start)
          and value.get('execution_start_sha256') == digest(owner) and value.get('challenge_sha256') == digest(authorization['challenge'])
          and read_json(output / 'execution-start.json') == owner
          and value.get('worker') == start['process'] and value.get('job_membership_verified') is True
          and _live(owner['parent']) and _live(owner['child_launcher']) and _live(start['process'])
          and type(value.get('worker_job_handle')) is int and value['worker_job_handle'] > 0,
          'The actual live parent did not authorize this exact contained child.')
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.IsProcessInJob.argtypes, kernel.IsProcessInJob.restype = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    handle = w.HANDLE(value['worker_job_handle'])
    try:
        member = w.BOOL()
        _need(kernel.IsProcessInJob(kernel.GetCurrentProcess(), handle, ctypes.byref(member)) and member.value,
              'Worker is outside the exact parent-owned job; do not start a browser.')
    finally:
        # Parent alone retains the owning handle; its death then kills the tree.
        _need(bool(kernel.CloseHandle(handle)),
              'Temporary child job handle did not close; do not start a browser.', 'owned_job_handle_not_closed')
    return value


def worker(request_path, output, challenge, *, read_authorization):
    """Only the explicit owned child's stdin starts this private oracle."""
    request = _request(request_path)
    output = _ordinary(output)
    start = time.time()
    _need(isinstance(challenge, str) and re.fullmatch('[0-9a-f]{64}', challenge) is not None,
          'Owned startup challenge is absent.')
    _need(_ordinary(request_path) == output / 'request.json', 'Worker request must be in its owned fresh output.')
    owner = _startup(request, output, challenge)
    own = {'pid': os.getpid(), 'identity': _identity(os.getpid())}
    _need(isinstance(own['identity'], str) and own['identity'], 'Owned child kernel identity is unavailable.')
    worker_start = {'kind': 'OwnedSVGFunctionalWorkerStart', 'schema_version': VERSION,
        'request_sha256': digest(request), 'challenge_sha256': digest(challenge), 'process': own,
        'parent_pid': os.getppid(), 'started_at': start, 'source_hashes': request['source_hashes'],
        'functional_source_hashes': _sources(), 'capture_lock': request['capture_lock']}
    _atomic(output / 'worker-start.json', worker_start)
    authorization = _authorization(request, output, worker_start, owner, read_authorization())
    trace = _Trace()
    from playwright import async_api
    original = async_api.async_playwright
    async_api.async_playwright = lambda: _PlaywrightContext(original, trace)
    try:
        def guard():
            _need(time.time() - start <= MAX_SECONDS, 'Functional run reached its fixed deadline.', 'functional_timeout')
            _need(source_hashes(request['source']) == request['source_hashes'] and _sources() == request['functional_source_hashes'],
                  'Source/checker changed during functional execution.', 'source_changed')
        # This is the unchanged real checker, not an interpretation of its flags.
        result = asyncio.run(asyncio.wait_for(suite_web._functional_browser_checks(Path(request['source']), 'svg', guard, None),
                                              timeout=MAX_SECONDS))
        guard()
        observation = {'kind': 'ObservedSVGFunctionalActions', 'schema_version': VERSION, 'actions': trace.rows,
                       'page_errors': trace.errors, 'browser': trace.browser}
        _need(replay_trace(observation) == result, 'Shared checker result differs from its retained predicate observations.')
        _need(trace.browser is not None and _browser_inventory(request['environment']) == request['browser_inventory'],
              'Browser binaries changed during actual functional execution.')
        write_json(output / 'observations.json', observation)
        receipt = {'kind': 'OwnedSVGFunctionalWorkerResult', 'schema_version': VERSION, 'request_sha256': digest(request),
            'status': 'succeeded' if result['status'] == 'pass' else 'behavior_failed', 'result': result,
            'observations_sha256': digest(observation), 'challenge_sha256': digest(challenge), 'process': own,
            'started_at': start, 'finished_at': time.time(), 'source_hashes_after': source_hashes(request['source']),
            'functional_source_hashes_after': _sources(), 'browser': trace.browser, 'new_model_generation': False}
        receipt.update(authorization_sha256=digest(authorization), actual_job_membership_verified=True,
                       temporary_job_handle_closed=True)
        write_json(output / 'worker-result.json', receipt)
        return receipt
    finally:
        async_api.async_playwright = original


def run(request_path, output):
    """Actual explicit execution only; callers must schedule after other captures."""
    request_path, output = _ordinary(request_path), _ordinary(output)
    request = _request(request_path)
    _need(not output.exists(), 'Functional executions are immutable.', 'output_exists')
    directory = _ordinary(request['initial_acceptance_directory'])
    plan, _ = initial.load_plan(directory)
    _need(not any(output.is_relative_to(_ordinary(p)) or _ordinary(p).is_relative_to(output)
                  for p in (directory, plan['suite'], plan['authoring_parent'])), 'Functional execution overlaps protected evidence/source.')
    output.mkdir(parents=True)
    write_json(output / 'request.json', request)
    environment = os.environ.copy()
    for key in ('PLAYWRIGHT_BROWSERS_PATH', 'PLAYWRIGHT_NODEJS_PATH', 'KIROCLOPS_ANALYSIS_BACKEND'):
        environment.pop(key, None)
    environment.update(request['environment'])
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    environment['PYTHONIOENCODING'] = 'utf-8'
    environment['DEBUG'] = 'pw:browser'
    command = [sys.executable, '-X', 'utf8', '-m', 'scripts.phase3d_svg_functional', '_worker',
               '--request', str(output / 'request.json'), '--output', str(output)]
    challenge = secrets.token_hex(32)
    proof = {'kind': 'OwnedSVGFunctionalExecution', 'schema_version': VERSION, 'status': 'prepared',
        'request_sha256': digest(request), 'request_file_sha256': file_sha256(output / 'request.json'),
        'command': command, 'cwd': str(Path(__file__).resolve().parents[2]), 'environment': request['environment'],
        'parent': {'pid': os.getpid(), 'identity': _identity(os.getpid())}, 'challenge_sha256': digest(challenge),
        'capture_lock': request['capture_lock'], 'common_lease_acquired': False, 'max_seconds': MAX_SECONDS,
        'python_sha256': file_sha256(sys.executable), 'functional_source_hashes': _sources(),
        'prepared_at': time.time(), 'new_model_generation': False, 'functional_qualified': False}
    _atomic(output / 'execution-start.json', proof)
    with _lock(Path(request['capture_lock']), timeout=0):
        _request(output / 'request.json')
        proof.update(common_lease_acquired=True, lease_acquired_at=time.time())
        owner = _OwnedJob()
        process = None
        resumed = False
        try:
            # The shared job helper assigns before resuming this exact suspended
            # launch handle. No venv/Node/browser descendant can escape first.
            kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x4}
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       cwd=proof['cwd'], env=environment, **kwargs)
            owned = {'pid': process.pid, 'identity': _identity(process.pid)}
            _need(owned['identity'] is not None and proof['parent']['identity'] is not None,
                  'Owned process startup identity could not be established.')
            owner.assign_and_resume(process)
            resumed = True
            proof.update(status='running', child_launcher=owned, launched_at=time.time(),
                         owned_job={'kind': 'WindowsKillOnCloseJob', 'assigned_before_resume': True})
            _atomic(output / 'execution-start.json', proof)
            # First handshake permits only a source-bound startup receipt.
            process.stdin.write((challenge + '\n').encode('ascii')); process.stdin.flush()
            startup_deadline = time.monotonic() + 30
            while not (output / 'worker-start.json').exists():
                _need(process.poll() is None and time.monotonic() < startup_deadline,
                      'The owned worker did not publish its actual startup.', 'owned_startup_failed')
                time.sleep(.01)
            worker_start = read_json(output / 'worker-start.json')
            worker_process = worker_start.get('process')
            _need(worker_start.get('request_sha256') == digest(request)
                  and worker_start.get('challenge_sha256') == proof['challenge_sha256']
                  and worker_start.get('functional_source_hashes') == _sources()
                  and worker_start.get('parent_pid') in (proof['parent']['pid'], owned['pid'])
                  and _live(worker_process) and owner.contains(worker_process),
                  'The actual child is foreign, stale, or outside the owned job.')
            token = secrets.token_hex(32)
            authorization = {'kind': 'OwnedSVGFunctionalWorkerAuthorization', 'schema_version': VERSION,
                'request_sha256': digest(request), 'worker_start_sha256': digest(worker_start),
                'execution_start_sha256': digest(proof), 'challenge_sha256': digest(token), 'worker': worker_process,
                'worker_job_handle': owner.duplicate_for(worker_process), 'job_membership_verified': True,
                'authorized_at': time.time()}
            _atomic(output / 'worker-authorization.json', authorization)
            process.stdin.write((json.dumps({'challenge': token,
                'authorization_sha256': file_sha256(output / 'worker-authorization.json')}) + '\n').encode('utf-8'))
            process.stdin.flush()
            stdout, stderr = process.communicate(timeout=MAX_SECONDS + 15)
            # Normal exit must have no live venv/Node/browser descendants too.
            empty_deadline = time.monotonic() + 2
            while owner.active() != 0 and time.monotonic() < empty_deadline:
                time.sleep(.01)
            if owner.active() != 0:
                raise VisualError('owned_tree_still_live', 'Functional child exited with a live owned descendant.')
            proof.update(worker_authorization_sha256=digest(authorization), actual_worker=worker_process,
                         owned_job_active_processes_at_exit=0, owned_job_confirmed_empty_at=time.time())
        except BaseException:
            if process is not None:
                # A launch that failed assignment is still suspended and has no
                # descendants. Otherwise only the exact job handle is stopped.
                if not resumed and process.poll() is None:
                    # This exact Popen kernel handle was never authorized to
                    # execute; no guessed PID or process enumeration is used.
                    while process.poll() is None:
                        try:
                            process.terminate()
                            process.wait(timeout=.1)
                        except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
                            pass  # Retain lease; this exact unstarted handle owns no browser.
                owner.stop_and_confirm(output)
                process.wait(timeout=10)
            _atomic(output / 'execution-failure.json', {'status': 'failed', 'finished_at': time.time(),
                'owned_job_active_processes': 0, 'capture_lease_retained_until_empty': True})
            raise
        finally:
            # The accounting check/cleanup above completed before the lease or
            # owning kill-on-close handle can be released.
            owner.close()
        (output / 'stdout.log').write_bytes(stdout)
        (output / 'stderr.log').write_bytes(stderr)
        proof.update(status='succeeded' if process.returncode == 0 else 'failed', exit_code=process.returncode,
                     exited_at=time.time(), stdout_sha256=file_sha256(output / 'stdout.log'),
                     stderr_sha256=file_sha256(output / 'stderr.log'), common_lease_held_until_exit=True)
        _atomic(output / 'execution.json', proof)
    proof['lease_released_at'] = time.time()
    _atomic(output / 'execution.json', proof)
    if process.returncode == 0:
        observation = read_json(output / 'observations.json')
        write_json(output / 'browser-launch.json', _launched_browser(stderr.decode('utf-8'), request, observation))
    # Byte-bound observed execution, never an independent witness/acceptance.
    write_json(output / 'binding.json', {'kind': 'SVGFunctionalExecutionBinding', 'schema_version': VERSION,
        'files': source_hashes(output), 'functional_qualified': False, 'full_behavior_acceptance': False})
    return inspect_run(output)


def inspect_run(output):
    """Replay actual run evidence only; cannot qualify without independent witness."""
    output = _ordinary(output)
    request = _request(output / 'request.json')
    binding = read_json(output / 'binding.json')
    files = source_hashes(output)
    files.pop('binding.json', None)
    _need(binding.get('kind') == 'SVGFunctionalExecutionBinding' and binding.get('schema_version') == VERSION
          and binding.get('files') == files and binding.get('functional_qualified') is False
          and binding.get('full_behavior_acceptance') is False and not (output / 'execution-failure.json').exists(),
          'Functional execution evidence changed or failed.')
    start, proof = read_json(output / 'execution-start.json'), read_json(output / 'execution.json')
    child, result, observation = (read_json(output / n) for n in ('worker-start.json', 'worker-result.json', 'observations.json'))
    expected_command = [sys.executable, '-X', 'utf8', '-m', 'scripts.phase3d_svg_functional', '_worker',
                        '--request', str(output / 'request.json'), '--output', str(output)]
    _need(proof.get('kind') == 'OwnedSVGFunctionalExecution' and proof.get('schema_version') == VERSION
          and proof.get('status') == 'succeeded' and proof.get('exit_code') == 0
          and proof.get('command') == expected_command and proof.get('request_sha256') == digest(request)
          and proof.get('request_file_sha256') == file_sha256(output / 'request.json')
          and proof.get('functional_source_hashes') == _sources() and proof.get('python_sha256') == file_sha256(sys.executable)
          and proof.get('environment') == request['environment'] and proof.get('cwd') == str(Path(__file__).resolve().parents[2])
          and proof.get('capture_lock') == request['capture_lock'] and proof.get('max_seconds') == MAX_SECONDS
          and proof.get('common_lease_acquired') is True and proof.get('common_lease_held_until_exit') is True
          and proof.get('new_model_generation') is False and proof.get('functional_qualified') is False,
          'Owned functional execution/source/lease identity is absent or foreign.')
    _need(digest(proof.get('owned_job')) == digest({'kind': 'WindowsKillOnCloseJob', 'assigned_before_resume': True})
          and type(proof.get('owned_job_active_processes_at_exit')) is int and proof['owned_job_active_processes_at_exit'] == 0,
          'The exact owned job did not confirm all worker/browser descendants had exited.')
    _need(start == {k: v for k, v in proof.items() if k not in ('exit_code', 'exited_at', 'stdout_sha256', 'stderr_sha256',
          'common_lease_held_until_exit', 'lease_released_at', 'worker_authorization_sha256', 'actual_worker',
          'owned_job_active_processes_at_exit', 'owned_job_confirmed_empty_at')} | {'status': 'running'},
          'Before-execution process receipt changed.')
    authorization = read_json(output / 'worker-authorization.json')
    for process in (proof.get('parent'), proof.get('child_launcher'), child.get('process')):
        _need(isinstance(process, dict) and set(process) == {'pid', 'identity'} and type(process['pid']) is int
              and process['pid'] > 0 and isinstance(process['identity'], str) and process['identity'],
              'Saved owned process creation identity is absent.')
    _need(child.get('kind') == 'OwnedSVGFunctionalWorkerStart' and child.get('schema_version') == VERSION
          and child.get('parent_pid') in (proof['parent']['pid'], proof['child_launcher']['pid'])
          and child.get('request_sha256') == result.get('request_sha256') == digest(request)
          and child.get('challenge_sha256') == result.get('challenge_sha256') == proof.get('challenge_sha256')
          and child.get('capture_lock') == request['capture_lock'] and child.get('source_hashes') == request['source_hashes']
          and child.get('functional_source_hashes') == result.get('functional_source_hashes_after') == _sources()
          and result.get('source_hashes_after') == request['source_hashes'] and result.get('process') == child['process']
          and result.get('kind') == 'OwnedSVGFunctionalWorkerResult' and result.get('schema_version') == VERSION
          and result.get('status') == 'succeeded' and result.get('new_model_generation') is False,
          'Actual child/result source/request/process binding is invalid or failed.')
    _need(authorization.get('kind') == 'OwnedSVGFunctionalWorkerAuthorization' and authorization.get('schema_version') == VERSION
          and authorization.get('request_sha256') == digest(request) and authorization.get('worker_start_sha256') == digest(child)
          and authorization.get('execution_start_sha256') == digest(start) and authorization.get('job_membership_verified') is True
          and authorization.get('worker') == proof.get('actual_worker') == child['process']
          and result.get('actual_job_membership_verified') is True
          and result.get('temporary_job_handle_closed') is True
          and result.get('authorization_sha256') == proof.get('worker_authorization_sha256') == digest(authorization)
          and type(authorization.get('worker_job_handle')) is int and authorization['worker_job_handle'] > 0,
          'Exact live contained-worker authorization is absent, stale, or foreign.')
    chronology = [request['created_at'], proof.get('prepared_at'), proof.get('lease_acquired_at'), proof.get('launched_at'),
                  child.get('started_at'), authorization.get('authorized_at'), result.get('finished_at'),
                  proof.get('owned_job_confirmed_empty_at'), proof.get('exited_at'), proof.get('lease_released_at')]
    _need(all(_finite(v) for v in chronology) and chronology == sorted(chronology)
          and result.get('started_at') == child['started_at']
          and result['finished_at'] - child['started_at'] <= MAX_SECONDS and chronology[-1] <= time.time(),
          'Functional run chronology/deadline is invalid.')
    _need(observation.get('kind') == 'ObservedSVGFunctionalActions' and observation.get('schema_version') == VERSION
          and result.get('observations_sha256') == digest(observation) and replay_trace(observation) == result.get('result')
          and result['result']['status'] == 'pass' and result.get('browser') == observation.get('browser'),
          'Actual functional predicate observations failed or differ from result flags.', 'svg_functional_failure')
    _need(all(child['started_at'] <= r['started_at'] <= r['finished_at'] <= result['finished_at'] for r in observation['actions']),
          'Functional actions occurred outside the owned execution interval.')
    browser = read_json(output / 'browser-launch.json')
    _need(browser == _launched_browser((output / 'stderr.log').read_text(encoding='utf-8'), request, observation),
          'Stored browser identity differs from actual owned launch transcript.')
    from kiroclops.web_local.natural_capture import browser_arguments
    _need(isinstance(browser, dict) and set(browser) == {'version', 'executable', 'executable_sha256', 'launch_options',
          'browser_pid', 'launch_transcript_sha256'}
          and isinstance(browser['version'], str) and browser['version']
          and file_sha256(_ordinary(browser['executable'])) == browser['executable_sha256']
          and digest(browser['launch_options']) == digest({'headless': True, 'args': browser_arguments(True)}),
          'Actual functional browser/version/binary/launch policy changed.')
    _need(proof['stdout_sha256'] == file_sha256(output / 'stdout.log')
          and proof['stderr_sha256'] == file_sha256(output / 'stderr.log'), 'Owned child output changed.')
    stdout = json.loads((output / 'stdout.log').read_text(encoding='utf-8'))
    _need(stdout == {'worker_result_sha256': digest(result), 'challenge_sha256': proof['challenge_sha256']},
          'Owned stdout does not bind the actual child result/startup challenge.')
    inventory = {str(output / n): sha for n, sha in files.items()} | {str(output / 'binding.json'): file_sha256(output / 'binding.json')}
    inventory[browser['executable']] = browser['executable_sha256']
    inventory.update(_sources())
    inventory.update({str(_ordinary(request['source']) / n): sha for n, sha in request['source_hashes'].items()})
    return {'kind': 'VerifiedOwnedSVGFunctionalExecution', 'schema_version': VERSION, 'status': 'pass_pending_independent_witness',
        'directory': str(output), 'request_sha256': digest(request), 'execution_sha256': digest(proof),
        'observations_sha256': digest(observation), 'initial_acceptance_directory': request['initial_acceptance_directory'],
        'initial_result_sha256': request['initial_result_sha256'], 'source_sha256': request['source_sha256'],
        'browser': browser, 'checks': result['result']['checks'], 'finished_at': result['finished_at'],
        'input_files': inventory, 'functional_qualified': False, 'full_behavior_acceptance': False, 'new_model_generation': False}


def validate_review(output, witness, *, directory, source_sha256, initial_result_sha256, browser_versions):
    """Only genuine named independent inspection can qualify the owned run."""
    proof = inspect_run(output)
    _need(proof['initial_acceptance_directory'] == str(_ordinary(directory)) and proof['source_sha256'] == source_sha256
          and proof['initial_result_sha256'] == initial_result_sha256 and proof['browser']['version'] in browser_versions
          and set(browser_versions) == {proof['browser']['version']}, 'Functional run belongs to another source/result/browser epoch.')
    value = read_json(witness)
    _need(value.get('kind') == 'IndependentSVGFunctionalExecutionReview' and value.get('schema_version') == VERSION
          and value.get('status') == 'pass' and value.get('method') == 'independent_owned_functional_execution_inspection'
          and isinstance(value.get('reviewer'), str) and value['reviewer'].strip()
          and value.get('reviewer_role') == 'independent_functional_reviewer' and value.get('authored_source') is False
          and value.get('inspection_performed') is True and value.get('actual_owned_execution_inspected') is True
          and value.get('accepted') is True and value.get('verification_sha256') == digest(proof)
          and value.get('initial_acceptance_directory') == proof['initial_acceptance_directory']
          and value.get('source_sha256') == source_sha256 and value.get('initial_result_sha256') == initial_result_sha256
          and value.get('new_model_generation') is False and value.get('full_behavior_acceptance') is False,
          'Actual independent functional witness is absent, stale, selfreported, or foreign.')
    _need(_finite(value.get('inspected_at')) and proof['finished_at'] <= value['inspected_at'] <= time.time(),
          'Functional witness chronology is invalid.')
    checks = value.get('checks', [])
    _need(len(checks) == len(CHECKS) and {c.get('id') for c in checks} == CHECKS
          and all(c.get('verdict') == 'pass' and isinstance(c.get('evidence'), str) and c['evidence'].strip() for c in checks),
          'Every actual functional action/state predicate must be independently inspected.')
    _need(value.get('inspected_files') == proof['input_files']
          and all(file_sha256(_ordinary(p)) == sha for p, sha in proof['input_files'].items()),
          'Functional witness does not bind every actual execution/action/state/browser/source byte.')
    return {'kind': 'VerifiedSVGFunctionalInspection', 'schema_version': VERSION, 'status': 'pass',
        'execution_directory': str(_ordinary(output)), 'execution_verification': proof, 'witness': str(_ordinary(witness)),
        'witness_sha256': file_sha256(witness), 'functional_qualified': True, 'full_behavior_acceptance': False,
        'input_files': proof['input_files'] | {str(_ordinary(witness)): file_sha256(witness)}, 'new_model_generation': False}
