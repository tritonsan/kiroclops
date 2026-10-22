"""Run the Phase 3A game ground stages that never started, with the cohort code.

The three KiroClops-arm jump stages passed motion, preservation and visual
boundary validation; only the frozen native auditor rejected them, a defect
repaired after the cohort. Their ground continuation runs here with the exact
preregistered protocol code (the 192 archived files), the same qualification,
model, effort, budget and goal text, in an isolated runtime copy. The primary
verdict uses that frozen code; the repaired auditor is replayed separately and
labeled post-hoc. The normal-Kiro arm is not continued: its jump behavior
failed. Original cohort files are only read.

Usage (from the repository root, with the .venv-local interpreter):
  python scripts/run_postrepair_ground.py build
  python scripts/run_postrepair_ground.py relocate
  python scripts/run_postrepair_ground.py supplement
  python scripts/run_postrepair_ground.py precheck
  python scripts/run_postrepair_ground.py dryrun --trial game-tools-01
  python scripts/run_postrepair_ground.py kiro-preflight
  python scripts/run_postrepair_ground.py register
  python scripts/run_postrepair_ground.py run --trial game-tools-01
  python scripts/run_postrepair_ground.py supplementary-register --trial game-tools-03
  python scripts/run_postrepair_ground.py supplementary-run --trial game-tools-03

The supplementary commands are post-hoc and never counted: they give a stage
whose registered run failed at MCP startup, before its prompt, one separately
recorded attempt (ground/supplementary-01). The registered failure stays.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import os
import platform
import uuid
import zipfile

from postrepair_quiet import ConcurrencyMonitor, quiet_gate

MAIN = Path(__file__).resolve().parents[1]
BASE = MAIN/'artifacts/phase3a'
COHORT = BASE/'completion-paired-cohort-01'
STUDY = BASE/'postrepair-completion-01'
# The repository disk (5400 rpm) is kept saturated by another reader (web
# amendment 02), so the frozen runtime and every ground session run from the
# NVMe system disk. Records stay in GROUND; sessions are archived there.
WORK_ROOT = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'kc3a-runtime/postrepair-sessions'
RUNTIME_ON_REPOSITORY_DISK = STUDY/'runtime/cohort-code'
RUNTIME = WORK_ROOT/'cohort-code'
GROUND = STUDY/'ground'
# The frozen runtime gives Godot TMP/TEMP inside each run directory. dbghelp.dll,
# which Godot imports, hangs in loader initialization once TMP reaches 261
# characters (ground/environment-diagnosis-01). Under %LOCALAPPDATA% a session
# resolves to the longer MSIX package path, so sessions use a short root on the
# same NVMe disk. The deepest TMP below a ground session (finalize preservation,
# wall-coyote import clean retry) adds 149 characters to the session directory.
SESSIONS = Path('C:/kc3a/ground')
TMP_LIMIT_CHARS, TMP_SUFFIX_CHARS, TMP_MARGIN_CHARS = 260, 149, 20
DIAGNOSIS = GROUND/'environment-diagnosis-01/diagnosis.json'
STAGE_QUIET_POLICY = {'interval_s': 60, 'consecutive_clear_checks': 11, 'maximum_wait_s': 8*3600}
SUPPLEMENTARY = GROUND/'supplementary-01'
# Entry modules of the processes a stage starts from the repository-disk venv:
# the MCP server (which Kiro must reach within its fixed 30 s connect limit),
# the guard hook (10 s per tool call) and the job workers.
WARM_ENTRIES = ('kiroclops.local_engine.mcp', 'kiroclops.local_engine.guard_diagnostics',
                'kiroclops.local_engine.launcher', 'kiroclops.local_engine.session', 'kiroclops.local_engine.backend')
KEEP_WARM_INTERVAL_S = 20
COHORT_TOOLS_STAGES = tuple(f'{domain}-tools-{repeat:02}/{stage}' for domain, stage in
                            (('game', 'jump'), ('panel', 'panel'), ('svg', 'svg')) for repeat in (1, 2, 3))
ARCHIVE = BASE/'completion-frozen-protocol-01'
TRIALS = {'game-tools-01': 1, 'game-tools-02': 2, 'game-tools-03': 3}
# Runtime resources the frozen code reads relative to its own root.
RESOURCES = ('adapters', 'config', 'vendor', '.tools/godot4', 'artifacts/runs', 'artifacts/phase2b/reference')


def _sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def build():
    """Materialize the archived cohort protocol code plus read-only resources."""
    if RUNTIME_ON_REPOSITORY_DISK.exists():
        raise SystemExit('Runtime copy already exists; never replace it')
    receipt = json.loads((ARCHIVE/'receipt.json').read_text(encoding='utf-8'))
    for relative in RESOURCES:
        source = MAIN/relative
        if source.is_dir():
            shutil.copytree(source, RUNTIME_ON_REPOSITORY_DISK/relative, ignore=shutil.ignore_patterns('__pycache__', '.godot'))
    archive = zipfile.ZipFile(ARCHIVE/'protocol-source.zip')
    for name in archive.namelist():
        if name == 'registration.json':
            continue
        data = archive.read(name)
        if hashlib.sha256(data).hexdigest() != receipt['source_hashes'][name]:
            raise SystemExit(f'Archive entry differs from its receipt: {name}')
        target = RUNTIME_ON_REPOSITORY_DISK/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (RUNTIME_ON_REPOSITORY_DISK/'.runtime').mkdir(exist_ok=True)
    (RUNTIME_ON_REPOSITORY_DISK/'artifacts/local-engine').mkdir(parents=True, exist_ok=True)
    print({'runtime': str(RUNTIME_ON_REPOSITORY_DISK), 'archived_files': len(receipt['source_hashes'])}, flush=True)
    return 0


def _tree_hashes(root):
    return {p.relative_to(root).as_posix(): _sha(p) for p in sorted(Path(root).rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts}


def relocate():
    """Byte-identical copy of the built runtime on the NVMe disk, verified file by file."""
    if RUNTIME.exists():
        raise SystemExit('NVMe runtime copy already exists; never replace it')
    started = time.time()
    shutil.copytree(RUNTIME_ON_REPOSITORY_DISK, RUNTIME, ignore=shutil.ignore_patterns('__pycache__'))
    source, copy = _tree_hashes(RUNTIME_ON_REPOSITORY_DISK), _tree_hashes(RUNTIME)
    if source != copy:
        raise SystemExit('NVMe runtime copy differs from the repository-disk runtime')
    receipt = {'kind': 'GroundRuntimeRelocation', 'source': str(RUNTIME_ON_REPOSITORY_DISK), 'copy': str(RUNTIME),
               'files': len(copy), 'tree_sha256': hashlib.sha256(json.dumps(copy, sort_keys=True).encode()).hexdigest(),
               'seconds': round(time.time()-started, 1),
               'reason': 'The repository disk is saturated by another reader; Godot, Python sources and sessions run from NVMe.'}
    GROUND.mkdir(parents=True, exist_ok=True)
    (GROUND/'runtime-relocation.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print({k: receipt[k] for k in ('files', 'tree_sha256', 'seconds')}, flush=True)
    return 0


GATE_TOOL = 'artifacts/phase3a/tools/check_qualified_reference_observability.py'


def supplement():
    """Add the one gate-bound file the 192-file archive lacks: the tool that produced the cohort's reference gate.

    validate_reference_gate requires every source the gate recorded, including
    this tool, at its recorded hash. The main-tree copy still has that hash.
    """
    receipt_path = GROUND/'runtime-supplement.json'
    if receipt_path.exists():
        raise SystemExit('Runtime supplement already recorded; never replace it')
    cohort = json.loads((COHORT/'registration.json').read_text(encoding='utf-8'))
    gate = Path(cohort['web_reference_bundle']['observability']['path'])
    expected = json.loads(gate.read_text(encoding='utf-8'))['source_hashes'][GATE_TOOL]
    source = MAIN/GATE_TOOL
    if _sha(source) != expected:
        raise SystemExit('Main-tree gate tool differs from the hash the cohort gate recorded')
    added = []
    for root in (RUNTIME_ON_REPOSITORY_DISK, RUNTIME):
        target = root/GATE_TOOL
        if target.exists():
            raise SystemExit(f'{target} already exists')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if _sha(target) != expected:
            raise SystemExit(f'Copied gate tool differs: {target}')
        added.append(str(target))
    receipt = {'kind': 'GroundRuntimeSupplement', 'file': GATE_TOOL, 'sha256': expected, 'source': str(source),
               'gate': str(gate), 'added_to': added, 'recorded_at': time.time(),
               'reason': ('The cohort reference gate binds the tool that produced it. The 192-file protocol archive does '
                          'not contain it, so validate_reference_gate rejected the frozen runtime until it was added. '
                          'It is not one of the protocol sources.')}
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(receipt, flush=True)
    return 0


def _archive(session_dir, destination):
    """Stored zip of a working session plus a per-file SHA-256 manifest."""
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination/'session-archive.zip'
    files = {}
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in sorted(p for p in Path(session_dir).rglob('*') if p.is_file()):
            relative = path.relative_to(Path(session_dir).parent).as_posix()
            files[relative] = _sha(path)
            archive.write(path, relative)
    manifest = {'kind': 'PostRepairSessionArchive', 'working_directory': str(session_dir), 'files': files,
                'count': len(files), 'zip': str(archive_path), 'zip_sha256': _sha(archive_path)}
    (destination/'session-archive-manifest.json').write_text(json.dumps(manifest, indent=1), encoding='utf-8')
    return {'zip': str(archive_path), 'zip_sha256': manifest['zip_sha256'], 'files': len(files)}


def _session_guard(directory):
    """Refuse a session directory whose deepest Godot TMP would come near the dbghelp limit."""
    chars = len(str(Path(directory).resolve()))
    if chars + TMP_SUFFIX_CHARS > TMP_LIMIT_CHARS - TMP_MARGIN_CHARS:
        raise SystemExit(f'Session path {directory} ({chars} chars) leaves Godot TMP within '
                         f'{TMP_MARGIN_CHARS} chars of {TMP_LIMIT_CHARS}; use a shorter root')
    return chars


def _tmp_budget(directory):
    """Measured deepest Godot TMP below a finished session (the runtime creates environment/tmp per run)."""
    root = Path(directory).resolve()
    deepest = max((Path(base)/'tmp' for base, names, _ in os.walk(root)
                   if Path(base).name == 'environment' and 'tmp' in names), key=lambda p: len(str(p)), default=None)
    return {'session_chars': len(str(root)), 'deepest_tmp_chars': len(str(deepest)) if deepest else None,
            'deepest_tmp': str(deepest) if deepest else None, 'limit_chars': TMP_LIMIT_CHARS}


def _use_runtime():
    sys.path.insert(0, str(RUNTIME))
    import kiroclops
    loaded = Path(kiroclops.__file__).resolve().parent
    if loaded != (RUNTIME/'kiroclops').resolve():
        raise SystemExit(f'kiroclops loaded from {loaded}, not the frozen runtime')


# The interpreter's packages stay on the repository disk (the venv is not
# relocatable without changing sys.executable). They are read once before a
# stage so the MCP server and job workers do not page them in from that disk.
PREWARM_SKIP = {'playwright', 'pip', 'setuptools', 'pkg_resources', '__pycache__'}


def _prewarm_packages():
    root = Path(sys.prefix)/'Lib/site-packages'
    started, count, size = time.time(), 0, 0
    for entry in sorted(root.iterdir()):
        if entry.name in PREWARM_SKIP or entry.name.endswith('.dist-info'):
            continue
        for path in ([entry] if entry.is_file() else sorted(entry.rglob('*'))):
            if path.is_file() and '__pycache__' not in path.parts:
                with open(path, 'rb') as handle:
                    while chunk := handle.read(1 << 20):
                        size += len(chunk)
                count += 1
    return {'root': str(root), 'files': count, 'bytes': size, 'seconds': round(time.time()-started, 1)}


def _warm_files():
    """Files the stage's Python processes read at startup: what WARM_ENTRIES load (sources, cached
    bytecode, extension modules), every file of the site packages they come from, path and metadata files.

    A child interpreter imports the frozen runtime without writing bytecode; nothing else runs.
    """
    code = ('import importlib,json,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\n'
            'for n in sys.argv[2:]:importlib.import_module(n)\n'
            'site=Path(sys.prefix,"Lib","site-packages").resolve();files=set();tops=set()\n'
            'for m in list(sys.modules.values()):\n'
            ' for a in ("__file__","__cached__"):\n'
            '  v=getattr(m,a,None)\n'
            '  if v and Path(v).resolve().is_file():\n'
            '   p=Path(v).resolve();files.add(str(p))\n'
            '   if p.is_relative_to(site):tops.add(p.relative_to(site).parts[0])\n'
            'print(json.dumps({"files":sorted(files),"tops":sorted(tops),"site":str(site)}))')
    done = subprocess.run([sys.executable, '-B', '-c', code, str(RUNTIME), *WARM_ENTRIES], capture_output=True,
                          text=True, encoding='utf-8', cwd=str(RUNTIME), timeout=600,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if done.returncode:
        raise SystemExit(f'Warm-set import failed: {done.stderr[-1000:]}')
    value = json.loads(done.stdout.strip().splitlines()[-1])
    site = Path(value['site'])
    files = set(value['files'])
    for top in value['tops']:
        path = site/top
        files.update(str(p) for p in (sorted(path.rglob('*')) if path.is_dir() else [path]) if p.is_file())
    files.update(str(p) for p in site.glob('*.pth'))
    files.update(str(p) for p in site.glob('*.dist-info/METADATA'))
    files.update(str(Path(sys.prefix)/name) for name in ('pyvenv.cfg', 'Scripts/python.exe'))
    return sorted(f for f in files if Path(f).is_file())


class KeepWarm:
    """Re-read a fixed file set in the background so the OS file cache keeps it; never touches a stage file.

    Supplementary attempts only: the frozen MCP server must answer Kiro within
    30 s, and its imports otherwise page in from the saturated repository disk
    right after the launcher's registration check has streamed gigabytes from it.
    """

    def __init__(self, files, interval_s=KEEP_WARM_INTERVAL_S):
        import threading
        self.files, self.interval = list(files), interval_s
        self.directories = sorted({str(Path(name).parent) for name in self.files})
        self._stop = threading.Event()
        self._rounds, self._first, self._slowest, self._errors = 0, None, 0.0, []
        self._started = time.time()
        self._thread = threading.Thread(target=self._run, name='postrepair-keep-warm', daemon=True)
        self._thread.start()

    def _round(self):
        started = time.time()
        for name in self.directories:
            try:
                os.listdir(name)
            except OSError as exc:
                self._errors.append(f'{name}: {exc}')
        for name in self.files:
            try:
                with open(name, 'rb') as handle:
                    while handle.read(1 << 20):
                        pass
            except OSError as exc:
                self._errors.append(f'{name}: {exc}')
        return time.time()-started

    def _run(self):
        while True:
            seconds = self._round()
            self._rounds += 1
            self._first = seconds if self._first is None else self._first
            self._slowest = max(self._slowest, seconds)
            if self._stop.wait(self.interval):
                return

    def close(self):
        self._stop.set()
        self._thread.join(timeout=300)
        return {'files': len(self.files), 'directories': len(self.directories), 'interval_s': self.interval,
                'started_at': self._started, 'stopped_at': time.time(), 'rounds': self._rounds,
                'first_round_s': round(self._first, 2) if self._first is not None else None,
                'slowest_round_s': round(self._slowest, 2), 'errors': self._errors[-5:]}


def _model_gate():
    """The launcher's own inventory check, run before a stage starts; a stage never starts without the model."""
    from kiroclops.integration import kiro as native
    from kiroclops.local_engine.launcher import MODEL
    executable = shutil.which('kiro-cli')
    if not executable:
        raise SystemExit('kiro-cli missing; the stage was not started')
    done = subprocess.run([executable, 'chat', '--agent-engine', 'v3', '--list-models', '--format', 'json'],
                          capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
                          cwd=str(WORK_ROOT), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    models = native._parse_inventory(done.stdout) if done.returncode == 0 else {'models': []}
    version = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.strip()
    available = MODEL in {m.get('model_id') for m in models['models']}
    record = {'checked_at': time.time(), 'model': MODEL, 'available': available, 'cli_version': version,
              'returncode': done.returncode}
    if not available:
        raise SystemExit(f'{MODEL} is not in the Kiro model inventory ({done.stderr[-300:]}); the stage was not started')
    return record


def kiro_preflight():
    """Unregistered handshake on a scratch ground session: agent, model, effort, MCP tools and hook; no prompt."""
    _use_runtime()
    from kiroclops.local_engine.cohort import GOALS
    from kiroclops.local_engine.launcher import configure, preflight
    from kiroclops.local_engine.session import create_session
    from kiroclops.visual.schema import read_json, write_json
    cohort = read_json(COHORT/'registration.json')
    runs = sorted((GROUND/'kiro-preflight').glob('run-[0-9][0-9].json')) if (GROUND/'kiro-preflight').is_dir() else []
    name = f'run-{len(runs)+1:02}'
    directory = SESSIONS/'kiro-preflight'/name/'ground'
    _session_guard(directory)
    record = {'kind': 'GroundKiroPreflight', 'counted': False, 'prompt_sent': False, 'session': str(directory),
              'started_at': time.time(), 'model_gate': _model_gate(), 'prewarm': _prewarm_packages()}
    experiment = {'stage': 'ground', 'original_baseline': cohort['game_baseline'], 'experiment_id': 'local-heart-ground'}
    create_session(directory, 'game', _jump_candidate('game-tools-01'), RUNTIME/'config/phase2c-v2', GOALS['ground'],
                   mode='tools', experiment=experiment)
    configure(directory)
    try:
        ready = preflight(directory, connect=True)
        record.update(status='pass', cli_version=ready['cli_version'], handshake=ready.get('preflight'))
    except Exception as exc:
        record.update(status='fail', error={'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)})
    record['finished_at'] = time.time()
    (GROUND/'kiro-preflight').mkdir(parents=True, exist_ok=True)
    write_json(GROUND/'kiro-preflight'/f'{name}.json', record)
    print({k: record.get(k) for k in ('status', 'cli_version', 'error')}, record['prewarm']['seconds'], flush=True)
    return 0 if record['status'] == 'pass' else 1


def _repaired_audit(session):
    """Replay the post-cohort repaired auditor from the main tree, read-only."""
    code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
            'from kiroclops.local_engine.review import audit_native_run;'
            'from kiroclops.visual.schema import VisualError\n'
            'try:\n r=audit_native_run(sys.argv[2]);print(json.dumps({"status":r["status"],"evidence_hashes":r["evidence_hashes"],'
            '"tool_calls":len(r["tool_outcomes"]["calls"])}))\n'
            'except VisualError as e:\n print(json.dumps({"status":"fail","code":e.code,"message":str(e)}))')
    done = subprocess.run([sys.executable, '-B', '-c', code, str(MAIN), str(session)], capture_output=True,
                          text=True, encoding='utf-8', cwd=str(MAIN), timeout=600)
    try:
        value = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        value = {'status': 'error', 'stdout': done.stdout[-2000:], 'stderr': done.stderr[-2000:]}
    value.update(auditor='main-tree repaired review.py', auditor_sha256=_sha(MAIN/'kiroclops/local_engine/review.py'))
    return value


def precheck():
    _use_runtime()
    from kiroclops.local_engine.cohort import (protocol_sources, reference_sources, validate_qualification,
                                               web_reference_binding)
    from kiroclops.local_engine.session import source_hashes
    from kiroclops.visual.schema import digest, file_sha256, read_json, write_json
    cohort = read_json(COHORT/'registration.json')
    sources = protocol_sources()
    result = {'kind': 'GroundContinuationPrecheck', 'checked_at': time.time(), 'runtime': str(RUNTIME),
              'protocol_sources_match_cohort': sources == cohort['source_hashes'],
              'protocol_source_differences': sorted(k for k in set(sources) | set(cohort['source_hashes'])
                                                    if sources.get(k) != cohort['source_hashes'].get(k)),
              'reference_sources_match_cohort': reference_sources() == cohort['reference_hashes']}
    validate_qualification(cohort['qualification'])
    result['qualification'] = 'valid'
    shared = cohort['web_reference_bundle']
    result['web_reference_binding_valid'] = web_reference_binding(shared['manifest'], shared['observability']['path']) == shared
    result['game_baseline_unchanged'] = source_hashes(cohort['game_baseline']) == cohort['game_baseline_hashes']
    jumps = []
    for trial in TRIALS:
        session = COHORT/'sessions'/trial/'jump'
        state = read_json(session/'session.json')
        selected = state['selected_candidate_id']
        candidate = next(c for c in state['candidates'] if c['candidate_id'] == selected)
        source = session/'candidates'/selected/'source'
        final = read_json(session/'jobs'/state['final']['job_id']/'result.json')
        jumps.append({'trial': trial, 'session': str(session), 'session_json_sha256': file_sha256(session/'session.json'),
                      'selected_candidate_id': selected, 'candidate_source_sha256': candidate['source_sha256'],
                      'candidate_source_verified': digest(source_hashes(source)) == candidate['source_sha256'],
                      'final_pass': final.get('pass') is True, 'final_passed': final.get('passed'),
                      'preservation': final.get('preservation', {}).get('status'),
                      'visual_wall': final.get('visual_wall_status'),
                      'original_native_verdict': 'fail (frozen auditor)',
                      'repaired_native_audit': _repaired_audit(session)})
    result['jump_prerequisites'] = jumps
    result['ready'] = (result['protocol_sources_match_cohort'] and result['reference_sources_match_cohort']
                       and result['web_reference_binding_valid'] and result['game_baseline_unchanged']
                       and all(j['candidate_source_verified'] and j['final_pass'] and j['preservation'] == 'pass'
                               and j['visual_wall'] == 'pass' and j['repaired_native_audit']['status'] == 'pass'
                               for j in jumps))
    GROUND.mkdir(parents=True, exist_ok=True)
    write_json(GROUND/'precheck.json', result)
    print({k: result[k] for k in ('protocol_sources_match_cohort', 'reference_sources_match_cohort',
                                  'web_reference_binding_valid', 'game_baseline_unchanged', 'ready')},
          [(j['trial'], j['repaired_native_audit']['status']) for j in jumps], flush=True)
    return 0 if result['ready'] else 1


def _jump_candidate(trial):
    from kiroclops.visual.schema import read_json
    session = COHORT/'sessions'/trial/'jump'
    state = read_json(session/'session.json')
    return session/'candidates'/state['selected_candidate_id']/'source'


def dryrun(trial):
    """Exercise ground prepare and finalize without a model, on an unregistered scratch session.

    The unchanged jump candidate stands in for a ground candidate; its motion is
    expected to fail. Only environment completeness is checked.
    """
    _use_runtime()
    from kiroclops.local_engine.backend import TERMINAL, _atomic, _digest
    from kiroclops.local_engine.cohort import GOALS
    from kiroclops.local_engine.session import JOB_BINDING_FIELDS, Session, copy_project, create_session
    from kiroclops.visual.schema import VisualError, read_json, write_json
    cohort = read_json(COHORT/'registration.json')
    # Run 1 is ground/dryrun/<trial>; later runs (after an infrastructure failure) are <trial>-rNN.
    names = [trial] + [f'{trial}-r{n:02}' for n in range(2, 10)]
    name = next(n for n in names if not (GROUND/'dryrun'/n).exists())
    if name != trial and not read_json(GROUND/'dryrun'/names[names.index(name)-1]/'dryrun.json').get('steps'):
        raise SystemExit('The previous dry run left no step record; inspect it first')
    directory = SESSIONS/'dryrun'/name/'ground'
    record_dir = GROUND/'dryrun'/name
    if directory.exists():
        raise SystemExit('This dry run session already exists; never replace it')
    _session_guard(directory)
    record_dir.mkdir(parents=True)
    quiet = quiet_gate(f'{trial} dryrun')
    monitor = ConcurrencyMonitor([WORK_ROOT, SESSIONS])
    experiment = {'stage': 'ground', 'original_baseline': cohort['game_baseline'], 'experiment_id': 'local-heart-ground'}
    create_session(directory, 'game', _jump_candidate(trial), RUNTIME/'config/phase2c-v2', GOALS['ground'],
                   mode='tools', experiment=experiment)
    session = Session(directory)
    log = {'kind': 'GroundEnvironmentDryRun', 'trial': trial, 'model_started': False, 'counted': False,
           'quiet_gate': quiet, 'session': str(directory), 'runtime': str(RUNTIME), 'steps': []}

    def poll(job_id):
        # r05: one poll saw jobs/<id>/job.json as missing while the worker kept
        # rewriting it. Only this model-free observer retries; the job is untouched.
        for attempt in range(10):
            try:
                return Session(directory).get_job(job_id, wait_seconds=30)
            except VisualError as exc:
                if exc.code != 'job_not_found' or not (directory/'jobs'/job_id).is_dir() or attempt == 9:
                    raise
                log.setdefault('transient_poll_errors', []).append({'job_id': job_id, 'at': time.time(),
                                                                   'code': exc.code})
                time.sleep(1)

    def wait(job_id, name):
        started = time.time()
        last = None
        while True:
            job = poll(job_id)
            phase = (job.get('progress') or {}).get('phase')
            if phase != last or job['status'] in TERMINAL:
                print(f'  dryrun {name}: {job["status"]} {job.get("progress")}', flush=True)
                last = phase
            if job['status'] in TERMINAL:
                log['steps'].append({'operation': name, 'status': job['status'], 'error': job.get('error'),
                                     'seconds': time.time()-started})
                write_json(record_dir/'dryrun.json', log)
                return job

    prepare = session.submit('prepare')
    job = wait(prepare['job_id'], 'prepare')
    if job['status'] == 'succeeded':
        # Seal the unchanged source directly; submit() would refuse an ineligible candidate.
        state = session._load()
        candidate_id = 'candidate-01'
        snapshot = directory/'candidates'/candidate_id/'source'
        hashes = copy_project(directory/'target', snapshot)
        job_id = uuid.uuid4().hex
        job_dir = directory/'jobs'/job_id
        job_dir.mkdir(parents=True)
        record = {'schema_version': 1, 'job_id': job_id, 'operation': 'finalize', 'status': 'queued',
                  'created_at': time.time(), 'started_at': None, 'finished_at': None, 'worker': None,
                  'progress': {'phase': 'queued', 'completed': 0, 'total': 1}, 'candidate_id': candidate_id,
                  'source_path': str(snapshot), 'source_hashes': hashes, 'source_sha256': _digest(hashes),
                  'error': None, 'result': None, 'result_applied': False}
        _atomic(job_dir/'job.json', record)
        _atomic(job_dir/'binding.json', {'sha256': _digest({k: record[k] for k in JOB_BINDING_FIELDS})})
        state['candidates'].append({'candidate_id': candidate_id, 'created_at': time.time(), 'job_id': job_id,
                                    'status': 'queued', 'eligible': False, 'score': None,
                                    'source_sha256': record['source_sha256'], 'error': None})
        state.update(selected_candidate_id=candidate_id, sealed_at=time.time(), status='sealed',
                     final_job_id=job_id, active_job_id=job_id)
        session._save(state)
        session._spawn(job_id)
        job = wait(job_id, 'finalize')
        if job['status'] == 'succeeded':
            result = job['result']
            log['finalize_summary'] = {'pass': result.get('pass'), 'passed': result.get('passed'),
                                       'required': result.get('required'),
                                       'preservation': result.get('preservation', {}).get('status'),
                                       'jump_preservation_pass': (result.get('jump_preservation') or {}).get('pass'),
                                       'visual_wall': result.get('visual_wall_status')}
    log['environment_complete'] = all(step['status'] == 'succeeded' for step in log['steps']) and len(log['steps']) == 2
    log['concurrency'] = monitor.close()
    log['path_budget'] = _tmp_budget(directory)
    log['archive'] = _archive(directory, record_dir)
    write_json(record_dir/'dryrun.json', log)
    print({'environment_complete': log['environment_complete'], 'finalize': log.get('finalize_summary')}, flush=True)
    return 0 if log['environment_complete'] else 1


def register():
    _use_runtime()
    from kiroclops.local_engine.cohort import protocol_sources, reference_sources, verify_registration
    from kiroclops.visual.schema import file_sha256, read_json, write_json
    path = GROUND/'registration.json'
    if path.exists():
        raise SystemExit('Ground continuation already registered; never replace it')
    precheck_result = read_json(GROUND/'precheck.json')
    if not precheck_result.get('ready'):
        raise SystemExit('Precheck did not pass')
    if not any(read_json(p).get('environment_complete') for p in (GROUND/'dryrun').glob('*/dryrun.json')):
        raise SystemExit('No model-free dry run completed prepare and finalize')
    if not any(read_json(p).get('status') == 'pass' for p in (GROUND/'kiro-preflight').glob('run-*.json')):
        raise SystemExit('No Kiro handshake preflight passed')
    cohort = read_json(COHORT/'registration.json')
    rows, sessions = [], []
    for trial, repeat in TRIALS.items():
        session = str((SESSIONS/'sessions'/trial/'ground').resolve())
        _session_guard(session)
        sessions.append(session)
        rows.append({'id': trial, 'domain': 'game', 'mode': 'tools', 'repeat': repeat, 'status': 'registered',
                     'stages': [{'stage': 'ground', 'session': session, 'status': 'registered'}]})
    registry = deepcopy(cohort)
    registry.update(registered_at=time.time(), trials=len(rows), sessions=sessions, rows=rows,
                    source_hashes=protocol_sources(), reference_hashes=reference_sources(),
                    platform=platform.platform(), cohort_platform=cohort.get('platform'),
                    comparison_limits=[
                        'Continuation of three preregistered KiroClops-arm game chains after their jump stages passed.',
                        'The normal-Kiro arm is not continued because its jump behavior failed; no paired ground comparison exists.',
                        'Each ground stage runs once. A failed trial remains in the denominator.'],
                    completion_of={'cohort_registration': str(COHORT/'registration.json'),
                                   'cohort_registration_sha256': file_sha256(COHORT/'registration.json'),
                                   'cohort_progress_sha256': file_sha256(COHORT/'progress.json'),
                                   'precheck_sha256': file_sha256(GROUND/'precheck.json'),
                                   'dryruns': {p.parent.name: {'path': str(p), 'sha256': file_sha256(p),
                                                               'environment_complete': read_json(p).get('environment_complete')}
                                               for p in sorted((GROUND/'dryrun').glob('*/dryrun.json'))},
                                   'kiro_preflights': {p.stem: {'path': str(p), 'sha256': file_sha256(p),
                                                                'status': read_json(p).get('status')}
                                                       for p in sorted((GROUND/'kiro-preflight').glob('run-*.json'))},
                                   'inherited_jump_stages': {j['trial']: {k: j[k] for k in (
                                       'session', 'selected_candidate_id', 'candidate_source_sha256', 'final_pass',
                                       'preservation', 'visual_wall', 'original_native_verdict')}
                                       | {'repaired_native_audit': j['repaired_native_audit']['status']}
                                       for j in precheck_result['jump_prerequisites']},
                                   'runtime': str(RUNTIME),
                                   'runtime_relocation': str(GROUND/'runtime-relocation.json'),
                                   'runtime_relocation_sha256': file_sha256(GROUND/'runtime-relocation.json'),
                                   'runtime_supplement': str(GROUND/'runtime-supplement.json'),
                                   'runtime_supplement_sha256': file_sha256(GROUND/'runtime-supplement.json'),
                                   'session_root': str(SESSIONS/'sessions'),
                                   'session_path_diagnosis': str(DIAGNOSIS),
                                   'session_path_diagnosis_sha256': file_sha256(DIAGNOSIS),
                                   'session_path_limit': {'tmp_limit_chars': TMP_LIMIT_CHARS,
                                                          'deepest_tmp_suffix_chars': TMP_SUFFIX_CHARS,
                                                          'margin_chars': TMP_MARGIN_CHARS},
                                   'stage_quiet_policy': dict(STAGE_QUIET_POLICY),
                                   'concurrency_note': ('Another tool (Codex, Phase 3B) runs KiroClops sessions on this '
                                                        'machine with its own capture lock. A stage starts after ten '
                                                        'quiet minutes; workloads that start later are recorded by the '
                                                        'concurrency monitor, never stopped.'),
                                   'environment': ('Runtime and Godot on the NVMe system disk; the repository disk is '
                                                   'saturated by another reader. Sessions use the short NVMe root '
                                                   'C:/kc3a/ground because Godot TMP paths of 261+ characters hang '
                                                   'dbghelp.dll loader initialization (environment-diagnosis-01). Each '
                                                   'finished session is archived under ground/sessions/<trial>/. The '
                                                   'capture lock is the runtime-local default, as create_session sets it.')})
    GROUND.mkdir(parents=True, exist_ok=True)
    write_json(path, registry)
    write_json(GROUND/'progress.json', {'registration_sha256': file_sha256(path), 'rows': deepcopy(rows)})
    verify_registration(path)
    print({'registered': str(path), 'sha256': file_sha256(path), 'trials': len(rows)}, flush=True)
    return 0


def run(trial):
    """One preregistered ground stage, evaluated exactly like cohort.run_cohort."""
    return _run_stage(GROUND/'registration.json', trial, GROUND/'sessions')


def _run_stage(registration, trial, archive_root, *, warm_files=None):
    _use_runtime()
    from kiroclops.local_engine.cohort import (_evidenced_authoring_timeout, _evidenced_prompt_throttle,
                                               _prepare_stage, _stage_result, _verify_postturn_report,
                                               verify_registration)
    from kiroclops.local_engine.launcher import run as launch
    from kiroclops.local_engine.session import Session
    from kiroclops.visual.schema import VisualError, file_sha256, read_json, write_json
    # As in cohort.run_cohort: the launcher repeats the full verification before any prompt.
    registry = verify_registration(registration, full=False)
    progress_path = Path(registration).parent/'progress.json'
    progress = read_json(progress_path)
    row = next(r for r in progress['rows'] if r['id'] == trial)
    stage = row['stages'][0]
    if stage['status'] != 'registered':
        raise SystemExit(f'{trial} ground stage is {stage["status"]}; never rerun a started stage')
    if progress.get('continuation_blocked'):
        raise SystemExit(f'Continuation blocked by unconfirmed cleanup: {progress["continuation_blocked"]}')
    previous = _jump_candidate(trial)
    _session_guard(stage['session'])
    # A counted stage starts only after ten quiet minutes: Codex runs Phase 3B
    # batches on this machine, and dry run r05 overlapped one that started later.
    quiet = quiet_gate(f'{trial} ground', STAGE_QUIET_POLICY)
    model_gate = _model_gate()
    prewarm = _prewarm_packages()
    monitor = ConcurrencyMonitor([WORK_ROOT, SESSIONS])
    # Supplementary attempts keep the startup files cached until the launcher returns.
    warm = KeepWarm(warm_files) if warm_files else None
    row['status'] = 'running'
    stage.update(status='running', started_at=time.time(), quiet_gate=quiet, model_gate=model_gate, prewarm=prewarm)
    write_json(progress_path, progress)
    launched, state, native_status, error = {}, {}, 'fail', None
    try:
        _prepare_stage(registry, row, stage, previous)
        launched = launch(stage['session'], registration)
        if launched.get('terminal_cleanup', {}).get('status') != 'pass':
            raise VisualError('terminal_cleanup_unconfirmed', 'Owned execution cleanup is not confirmed')
        state = Session(stage['session']).inspect()
        if _evidenced_prompt_throttle(stage['session'], launched):
            raise VisualError('upstream_model_throttle', 'Kiro model service throttled the exact selected model before generation')
        if _evidenced_authoring_timeout(stage['session'], launched):
            raise VisualError('native_authoring_timeout', 'Kiro generated with the selected model until the bounded authoring deadline')
        from kiroclops.local_engine.review import audit_native_run
        audit_native_run(stage['session']);native_status = 'pass'
        report = launched.get('postturn_report', {})
        if report.get('status') != 'pass':
            raise VisualError(report.get('code', 'postturn_report_incomplete'),
                              report.get('message', 'The native turn ended without a completed local report'))
        measured = _stage_result(stage['session'])
        if not isinstance(measured, dict):
            raise VisualError('invalid_result', 'No source-bound final result exists for the completed report')
        _verify_postturn_report({'session': stage['session'], 'postturn_report': report}, measured,
                                state.get('selected_candidate_id'))
    except (VisualError, OSError, ValueError, KeyError) as exc:
        error = {'code': getattr(exc, 'code', 'trial_execution_error'), 'message': str(exc)}
        launch_path = Path(stage['session'])/'kiro/launch.json'
        if not launched and launch_path.is_file():
            launched = read_json(launch_path)
        if launched and launched.get('terminal_cleanup', {}).get('status') != 'pass':
            error = {'code': 'terminal_cleanup_unconfirmed', 'message': 'Terminal process cleanup has not been confirmed'}
        if (Path(stage['session'])/'session.json').exists():
            try:
                state = (Session(stage['session'])._load() if error['code'] == 'terminal_cleanup_unconfirmed'
                         else Session(stage['session']).inspect())
            except (VisualError, OSError, ValueError, KeyError):
                pass
    keep_warm = warm.close() if warm else None
    captured_pass = bool(state.get('final') and state['final'].get('pass') is True)
    report = launched.get('postturn_report', {})
    passed = (error is None and captured_pass and launched.get('status') == 'completed' and native_status == 'pass'
              and report.get('status') == 'pass')
    stage.update(status='completed' if passed else 'failed', launch_status=launched.get('status', 'launch_failed'),
                 numerical_pass=passed, captured_numerical_pass=captured_pass, native_validation_status=native_status,
                 error=error, postturn_report=report, independent_review_status='pending', source_review_status='pending',
                 candidate_count=state.get('budget', {}).get('used_candidates', len(state.get('candidates', []))),
                 finished_at=time.time(), final=state.get('final'), terminal_cleanup=launched.get('terminal_cleanup'),
                 concurrency=monitor.close())
    if keep_warm:
        stage['keep_warm'] = keep_warm
    if launched.get('status') in ('completed', 'finalization_failed') and (Path(stage['session'])/'kiro/launch.json').is_file():
        stage['posthoc_repaired_native_audit'] = _repaired_audit(stage['session'])
    row['status'] = 'completed' if passed else 'failed'
    if error and error['code'] == 'terminal_cleanup_unconfirmed':
        launch_path = Path(stage['session'])/'kiro/launch.json'
        progress['continuation_blocked'] = {'code': 'terminal_cleanup_unconfirmed', 'trial_id': trial,
                                            'session': stage['session'],
                                            'launch_sha256': file_sha256(launch_path) if launch_path.is_file() else None}
    write_json(progress_path, progress)
    if Path(stage['session']).is_dir():
        stage['path_budget'] = _tmp_budget(stage['session'])
        stage['archive'] = _archive(stage['session'], Path(archive_root)/trial)
        write_json(progress_path, progress)
    print({'trial': trial, 'status': stage['status'], 'captured_numerical_pass': captured_pass,
           'native_validation_status': native_status, 'error': error,
           'posthoc_repaired_native_audit': stage.get('posthoc_repaired_native_audit', {}).get('status')}, flush=True)
    return 0


def _handshake_evidence(session):
    """MCP connection timing of one recorded launch: ACP request times and, when kept, Kiro's own MCP log."""
    from datetime import datetime, timezone
    import re
    session = Path(session)
    launch_path = session/'kiro/launch.json'
    launch = json.loads(launch_path.read_text(encoding='utf-8')) if launch_path.is_file() else {}
    run_dir = session/'kiro/run'
    if not run_dir.is_dir():  # a handshake-only preflight records its client under kiro/preflight-<ns>
        run_dir = next(iter(sorted((session/'kiro').glob('preflight-*'))), run_dir)
    sent = {}
    for line in open(run_dir/'outbound.jsonl', encoding='utf-8'):
        row = json.loads(line)
        if row.get('phase') == 'attempt':
            sent.setdefault(row['message'].get('method'), datetime.fromisoformat(row['at']).timestamp())
    new, hooks = sent.get('session/new'), sent.get('_kiro/hooks/list')
    record = {'session': str(session), 'launch_status': launch.get('status'), 'prompt_sent': launch.get('prompt_sent'),
              'session_new_to_hooks_list_s': round(hooks-new, 1) if new and hooks else None}
    # Kiro names each agent-server log directory by its UTC start; the target path identifies the session.
    target = re.sub(r'\\+', '/', str(session/'target')).lower()
    logs = Path.home()/'.kiro/logs'
    for directory in sorted(logs.iterdir()) if logs.is_dir() else []:
        try:
            opened = datetime.strptime(directory.name, '%Y%m%dT%H%M%S%f').replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if not new or not new-30 <= opened <= new+30 or not (directory/'mcp.log').is_file():
            continue
        if target not in re.sub(r'\\+', '/', (directory/'kiro.log').read_text(encoding='utf-8', errors='replace')).lower():
            continue
        for line in (directory/'mcp.log').read_text(encoding='utf-8').splitlines():
            event = json.loads(line)
            if event.get('serverName') == 'kiroclops' and event.get('message') in ('Connected with transport',
                                                                                   'Failed to connect'):
                record['kiro_mcp_log'] = {'log': str(directory/'mcp.log'),
                                          **{k: event.get(k) for k in ('timestamp', 'message', 'era', 'eraProbeBudgetMs',
                                                                       'connectDurationMs', 'error')}}
    return record


def supplementary_register(trial):
    """Register one post-hoc, uncounted attempt for a stage whose registered run failed at MCP startup."""
    _use_runtime()
    from kiroclops.local_engine.cohort import protocol_sources, reference_sources, verify_registration
    from kiroclops.visual.schema import file_sha256, read_json, write_json
    path = SUPPLEMENTARY/'registration.json'
    if path.exists():
        raise SystemExit('Supplementary attempt already registered; never replace it')
    ground = GROUND/'registration.json'
    verify_registration(ground, full=False)
    progress = read_json(GROUND/'progress.json')
    stage = next(r for r in progress['rows'] if r['id'] == trial)['stages'][0]
    launch = read_json(Path(stage['session'])/'kiro/launch.json')
    if (stage['status'] != 'failed' or launch.get('status') != 'mcp_startup_failed'
            or launch.get('prompt_sent') is not False or stage.get('candidate_count')):
        raise SystemExit('Only a registered stage that failed at MCP startup, before its prompt, gets a supplementary attempt')
    registry = read_json(ground)
    sources, references = protocol_sources(), reference_sources()
    if sources != registry['source_hashes'] or references != registry['reference_hashes']:
        raise SystemExit('The frozen runtime no longer matches the ground registration')
    session = str((SESSIONS/'supplementary-01'/trial/'ground').resolve())
    _session_guard(session)
    if Path(session).exists():
        raise SystemExit('The supplementary session already exists; never replace it')
    SUPPLEMENTARY.mkdir(parents=True, exist_ok=True)
    files = _warm_files()
    warm_path = SUPPLEMENTARY/'keep-warm-files.json'
    write_json(warm_path, {'kind': 'GroundKeepWarmFiles', 'entries': list(WARM_ENTRIES), 'interpreter': sys.executable,
                           'files': files, 'count': len(files), 'bytes': sum(Path(f).stat().st_size for f in files),
                           'on_repository_disk': sum(1 for f in files if Path(f).drive.upper() == MAIN.drive.upper())})
    original = next(r for r in registry['rows'] if r['id'] == trial)
    row = {**deepcopy(original), 'status': 'registered',
           'stages': [{'stage': 'ground', 'session': session, 'status': 'registered'}]}
    evidence = {'ground': {t: _handshake_evidence(r['stages'][0]['session']) for t, r in
                           ((r['id'], r) for r in progress['rows'])},
                'kiro_preflights': {p.stem: _handshake_evidence(read_json(p)['session'])
                                    for p in sorted((GROUND/'kiro-preflight').glob('run-*.json'))},
                'cohort_tools_arm': {name: _handshake_evidence(COHORT/'sessions'/name) for name in COHORT_TOOLS_STAGES}}
    registry.update(
        registered_at=time.time(), trials=1, sessions=[session], rows=[row], source_hashes=sources,
        reference_hashes=references, platform=platform.platform(),
        comparison_limits=[
            f'Post-hoc supplementary attempt, written after the registered {trial} ground stage failed at MCP startup '
            'before its prompt.',
            'Not counted: the registered failure stays in the ground denominator and is never replaced by this attempt.',
            'Same frozen protocol code, qualification, model, effort, budget, goal and inherited jump candidate. The only '
            'change is an infrastructure mitigation: the files the stage processes read at Python startup are kept in '
            'the OS file cache from stage preparation until the launcher returns.',
            'One attempt; its outcome is reported whatever it is.'],
        supplementary_of={
            'trial': trial, 'ground_registration': str(ground), 'ground_registration_sha256': file_sha256(ground),
            'ground_progress_sha256': file_sha256(GROUND/'progress.json'),
            'registered_outcome': {**{k: stage.get(k) for k in ('status', 'launch_status', 'error', 'candidate_count',
                                                                'started_at', 'finished_at')},
                                   'prompt_sent': launch.get('prompt_sent'), 'launch_error': launch.get('error'),
                                   'concurrency_seen': sorted({w['command'] for w in stage.get('concurrency', {})
                                                               .get('foreign_workloads_seen', [])})},
            'handshake_evidence': evidence,
            'cause': ('Supported, not proven. Kiro waits at most 30 s (fixed in the frozen launcher) for the KiroClops '
                      'MCP server. That server starts from the virtual environment on the repository disk, which other '
                      'readers keep saturated, and the launcher streams the registered evidence (about 3.6 GB) from the '
                      'same disk just before the handshake. The connection was already slow in ground stages 01 and 02 '
                      'and, in stage 03, exceeded the limit while a Codex Phase 3C process ran. See handshake_evidence.'),
            'mitigation': {'kind': 'keep_warm', 'files': str(warm_path), 'files_sha256': file_sha256(warm_path),
                           'interval_s': KEEP_WARM_INTERVAL_S, 'entries': list(WARM_ENTRIES),
                           'window': 'from stage preparation until the launcher returns'},
            'stage_quiet_policy': dict(STAGE_QUIET_POLICY)})
    write_json(path, registry)
    write_json(SUPPLEMENTARY/'progress.json', {'registration_sha256': file_sha256(path), 'rows': [deepcopy(row)]})
    # The launcher repeats the full verification before any prompt.
    verify_registration(path, full=False)
    print({'registered': str(path), 'sha256': file_sha256(path), 'session': session, 'warm_files': len(files),
           'handshakes': {k: v.get('session_new_to_hooks_list_s') for k, v in evidence['ground'].items()}}, flush=True)
    return 0


def supplementary_run(trial):
    """The registered supplementary attempt, evaluated exactly like a ground stage, with the startup files kept warm."""
    _use_runtime()
    from kiroclops.visual.schema import file_sha256, read_json
    registry = read_json(SUPPLEMENTARY/'registration.json')
    mitigation = registry['supplementary_of']['mitigation']
    if file_sha256(mitigation['files']) != mitigation['files_sha256']:
        raise SystemExit('The registered keep-warm file list changed')
    return _run_stage(SUPPLEMENTARY/'registration.json', trial, SUPPLEMENTARY/'sessions',
                      warm_files=read_json(mitigation['files'])['files'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('build', 'relocate', 'supplement', 'precheck', 'dryrun', 'kiro-preflight',
                                            'register', 'run', 'supplementary-register', 'supplementary-run'))
    parser.add_argument('--trial', choices=tuple(TRIALS))
    args = parser.parse_args()
    trial_commands = {'dryrun': dryrun, 'run': run, 'supplementary-register': supplementary_register,
                      'supplementary-run': supplementary_run}
    if args.command in trial_commands:
        if not args.trial:
            raise SystemExit('--trial is required')
        return trial_commands[args.command](args.trial)
    return {'build': build, 'relocate': relocate, 'supplement': supplement, 'precheck': precheck,
            'kiro-preflight': kiro_preflight, 'register': register}[args.command]()


if __name__ == '__main__':
    raise SystemExit(main())
