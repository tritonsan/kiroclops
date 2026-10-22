"""Seven real captures on a new external copy; no model or heldout analysis."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import struct
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from kiroclops.integration import ground_runner, runner
from kiroclops.integration import isolated_runtime_v3 as runtime
from kiroclops.integration.import_cache_v1 import seed_import_cache
from kiroclops.integration.storage import copy_project, project_hashes, read_json, sha256, utc_now, write_json
from kiroclops.integration.repair_workflow_v2 import verify_protocol


def frames(run):
    paths=[run/'frames'/f'{i:06d}.png' for i in range(240)]
    if set((run/'frames').glob('*.png'))!=set(paths):raise ValueError('Not exactly 240 PNGs')
    for path in paths:
        header=path.read_bytes()[:24]
        if len(header)!=24 or header[:8]!=b'\x89PNG\r\n\x1a\n' or struct.unpack('>II',header[16:24])!=(640,360):
            raise ValueError('Invalid native PNG header')
    return {p.name:sha256(p) for p in paths}


def timed(run):
    manifest=read_json(run/'manifest.json')
    bridgepath=next(run.glob('engine.isolated-runtime-*/bridge.json'))
    bridge=read_json(bridgepath); private=read_json(bridgepath.parent/'process/process.json')
    seconds=lambda a,b:(datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds()
    first=(run/'frames/000000.png').stat().st_mtime
    last=(run/'frames/000239.png').stat().st_mtime
    return {'manifest_wrapper_seconds':manifest['provenance']['wall_seconds'],
        'wrapper_measurement_caveat':'Legacy jump manifest excludes later FFmpeg; ground includes it.',
        'private_bridge_seconds':seconds(bridge['started_at'],bridge['finished_at']),
        'resumed_to_proof_seconds':seconds(private['resumed_at'],private['finished_at']),
        'resumed_to_first_png_mtime_seconds':first-datetime.fromisoformat(private['resumed_at']).timestamp(),
        'first_to_last_png_mtime_seconds':last-first,
        'png_timestamp_caveat':'Filesystem mtimes bound a production span, not CPU/GPU/encoding stage clocks.',
        'bridge':str(bridgepath),'bridge_sha256':sha256(bridgepath)}


def qualify(output):
    output=Path(output).resolve()
    if output.parent!=ROOT/'artifacts/model-trials/preflight' or output.exists():raise ValueError('New direct preflight output required')
    historypath=ROOT/'artifacts/model-trials/preflight/repair-runtime-01/result.json'
    history=read_json(historypath)
    importedpath=ROOT/'artifacts/model-trials/preflight/repair-import-01/result.json'
    imported=read_json(importedpath)
    if history['status']!='pass' or imported['status']!='pass':raise ValueError('Earlier qualification did not pass')
    expected=history['source_hashes']; source=Path(imported['source'])
    if expected!=imported['source_hashes'] or project_hashes(source)!=expected:raise ValueError('Baseline source mismatch')
    oldruns={r['funnel']:Path(r['run']) for r in history['captures'] if r['project']=='seeded'}
    if len(oldruns)!=7:raise ValueError('Seven original capture paths required')
    bound={}
    for label,run in oldruns.items():
        manifest=read_json(run/'manifest.json')
        if (manifest['provenance']['full_source_hashes_before']!=expected or manifest['provenance']['full_source_hashes_after']!=expected
                or manifest['fps']!=60 or manifest['frame_count']!=240):raise ValueError('Historical source/timing mismatch')
        bound[label]={'path':str(run),'manifest_sha256':sha256(run/'manifest.json'),'frames':frames(run),'timing':timed(run)}
    original=verify_protocol(ROOT/'sessions/cli-repair-v2-01')
    codefiles=['kiroclops/integration/isolated_runtime_v3.py','kiroclops/integration/isolated_runtime_v2.py',
        'kiroclops/integration/private_desktop_v1.py','kiroclops/integration/import_cache_v1.py',
        'kiroclops/integration/runner.py','kiroclops/integration/ground_runner.py','scripts/qualify_isolated_runtime_v3.py']
    codes={name:sha256(ROOT/name) for name in codefiles}
    output.mkdir(); started=time.perf_counter()
    record={'schema_version':1,'status':'running','started_at':utc_now(),'source_hashes':expected,
        'policy':runtime.PRESENTATION_POLICY,'code_hashes':codes,'old_frozen_49_source_hashes':original['source_hashes'],
        'historical_result':{'path':str(historypath),'sha256':sha256(historypath)},
        'historical_runs':bound,'captures':[],'acceptance':False,
        'scope':'Private no-VSync rendering qualification only; no controller/threshold/model or heldout change.'}
    write_json(output/'result.json',record)
    try:
        project=copy_project(source,output/'project')
        if project_hashes(project)!=expected:raise ValueError('Copied source differs')
        cache=imported['cache_publication']
        record['cache_seed']=seed_import_cache(cache['cache_dir'],project,engine=ROOT/runtime.GUI_RELATIVE,
            expected_source_hashes=expected,expected_manifest_sha256=cache['manifest_sha256'])
        runs={}
        with runtime.isolated_runtime(project_roots=[output],expected_sources={project:expected}) as bridge:
            t=time.perf_counter(); ground_runner.import_project(project,game='slay',runtime_dir=output/'import-runtime')
            record['import_seconds']=time.perf_counter()-t
            print(json.dumps({'stage':'import_complete','seconds':record['import_seconds']}),flush=True)
            for label in ['ground-r06','ground-r48','jump-h03']:
                run=output/'captures'/label;t=time.perf_counter()
                print(json.dumps({'stage':'capture_start','label':label}),flush=True)
                if label.startswith('ground-'):
                    scenario=read_json(oldruns[label]/'manifest.json')['scenario']
                    ground_runner.capture_project(project,run,game='slay',scenario=scenario,variant='before')
                else:runner.capture_project(project,run,hold_ticks=3,variant='after',repeat=1,spawn_offset=0)
                runs[label]=run
                record['captures'].append({'label':label,'run':str(run),'full_call_seconds':time.perf_counter()-t})
                write_json(output/'result.json',record)
                print(json.dumps({'stage':'capture_complete','label':label,'seconds':record['captures'][-1]['full_call_seconds']}),flush=True)
            print(json.dumps({'stage':'four_smokes_start'}),flush=True)
            t=time.perf_counter();smoke=ground_runner.smoke_project(project,output/'captures/smoke')
            smoke_replay=ground_runner.verify_smoke(project,output/'captures/smoke',expected)
            record['smoke']={'result_status':smoke['status'],'verification':smoke_replay,'full_call_seconds':time.perf_counter()-t}
            if smoke['status']!='passed' or smoke_replay['status']!='pass':raise ValueError('Four smoke paths failed')
            for label,item in smoke['runs'].items():
                runs['smoke-'+label]=Path(item['path'])
                record['captures'].append({'label':'smoke-'+label,'run':item['path']})
            receipts=list(bridge.receipts)
        comparisons={};runtimechecks=[]
        for label,run in runs.items():
            old=oldruns[label];a,b=read_json(run/'manifest.json'),read_json(old/'manifest.json')
            newframes=frames(run)
            checks={'all_240_png_bytes_equal':newframes==bound[label]['frames'],
                'frame_records_equal':a['frames']==b['frames'], 'fps60':a['fps']==b['fps']==60,
                'native_viewport':a['viewport']==b['viewport']==[640,360],
                'frame_count240':a['frame_count']==b['frame_count']==240,
                'scenario_unchanged':a['scenario']==b['scenario'],
                'source_unchanged':a['provenance']['full_source_hashes_before']==a['provenance']['full_source_hashes_after']==expected,
                'historical_bytes_unchanged':sha256(old/'manifest.json')==bound[label]['manifest_sha256'] and frames(old)==bound[label]['frames']}
            proofpath=run/('engine.'+runtime.VERSION)/'bridge.json'
            verification=runtime.verify_runtime_receipt(proofpath,project=project,expected_source_hashes=expected)
            process=read_json(proofpath.parent/'process/process.json')
            runtimechecks.append({'path':str(proofpath),'verification':verification,'desktop_name':process['desktop_name'],
                'pid':process['process_identity']['pid'],'creation_filetime_100ns':process['process_identity']['creation_filetime_100ns'],
                'owned_window':process['saw_owned_window'],'non_input_samples':len(process['desktop_samples']),
                'terminated':process['owned_process_termination_confirmed']})
            comparisons[label]={'status':'pass' if all(checks.values()) else 'fail','checks':checks,
                'different_frames':[k for k in newframes if newframes[k]!=bound[label]['frames'][k]],
                'new_manifest_sha256':sha256(run/'manifest.json'),'new_frames':newframes,'timing':timed(run),
                'old_timing':bound[label]['timing']}
        versions=[]
        for path in receipts:
            r=read_json(path)
            if r.get('kind') not in {'version_observation','version_cache_reuse'}:continue
            log=path.with_name(path.name.split('.'+runtime.VERSION+'.json')[0])
            if (r['status']!='completed' or r['exit_code']!=0 or r['version']!=runtime.ENGINE_VERSION
                    or r['engine_sha256']!=runtime.GUI_SHA256 or sha256(log)!=r['log_sha256']):raise ValueError('Version proof differs')
            if r['kind']=='version_cache_reuse':
                origin=r['observed_origin'];o=read_json(origin['receipt'])
                if (sha256(origin['receipt'])!=origin['receipt_sha256'] or sha256(origin['log'])!=origin['log_sha256']
                        or o['kind']!='version_observation' or o['status']!='completed' or o['log_sha256']!=r['log_sha256']):raise ValueError('Version origin differs')
            versions.append({'path':str(path),'sha256':sha256(path),'kind':r['kind']})
        count=dict(Counter(v['kind'] for v in versions))
        if count!={'version_observation':1,'version_cache_reuse':6}:raise ValueError('Wrong version query counts')
        oldnow=verify_protocol(ROOT/'sessions/cli-repair-v2-01')
        record.update(comparisons=comparisons,runtime=runtimechecks,versions=versions,version_counts=count,
            source_preserved=project_hashes(project)==project_hashes(source)==expected,
            code_preserved=all(sha256(ROOT/k)==v for k,v in codes.items()),
            old_protocol_unchanged=oldnow['source_hashes']==record['old_frozen_49_source_hashes'])
        record['status']='pass' if (len(comparisons)==7 and all(c['status']=='pass' for c in comparisons.values())
            and all(v['verification']['status']=='pass' for v in runtimechecks)
            and len({v['desktop_name'] for v in runtimechecks})==7
            and len({(v['pid'],v['creation_filetime_100ns']) for v in runtimechecks})==7
            and record['source_preserved'] and record['code_preserved'] and record['old_protocol_unchanged']) else 'fail'
        record['limitations']=['One fresh capture per path, compared with historical completed v2 images. Not a controlled repeated performance benchmark.',
            'Wall timings include host scheduling and GPU driver effects; no stage-specific CPU attribution.',
            'No acceptance, model source change, heldout measurement or shortened capture.']
    except BaseException as error:
        record.update(status='error',error={'type':type(error).__name__,'message':str(error)})
        raise
    finally:
        record.update(finished_at=utc_now(),elapsed_seconds=time.perf_counter()-started)
        write_json(output/'result.json',record)
    print(json.dumps({'stage':'complete','status':record['status'],'captures':len(record['captures']),'elapsed_seconds':record['elapsed_seconds']}),flush=True)
    return record

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();result=qualify(args.output)
    raise SystemExit(0 if result['status']=='pass' else 1)
