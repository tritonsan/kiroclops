"""Coordinated 14-capture native runtime qualification; no model acceptance."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from kiroclops.integration import ground_runner, runner
from kiroclops.integration.isolated_runtime_v2 import (isolated_runtime, verify_runtime_receipt,
                                                       ENGINE_VERSION, GUI_SHA256)
from kiroclops.integration.storage import project_hashes, read_json, sha256, utc_now, write_json


def _frames(run):
    paths=[run/'frames'/f'{n:06d}.png' for n in range(240)]
    if set((run/'frames').glob('*.png'))!=set(paths) or any(not p.is_file() or not p.stat().st_size for p in paths):
        raise ValueError('Capture must have exactly 240 nonempty native PNGs')
    return {p.name:sha256(p) for p in paths}


def _compare(left,right):
    a,b=_frames(left),_frames(right)
    am,bm=read_json(left/'manifest.json'),read_json(right/'manifest.json')
    checks={'all_240_png_bytes_equal':a==b,'fps_60':am['fps']==bm['fps']==60,
            'frame_count_240':am['frame_count']==bm['frame_count']==240,
            'frame_timing_and_actions_equal':am['frames']==bm['frames'],
            'viewport_equal':am['viewport']==bm['viewport']}
    return {'left':str(left),'right':str(right),'checks':checks,
            'status':'pass' if all(checks.values()) else 'fail',
            'different_frames':[name for name in a if a[name]!=b[name]],
            'left_manifest_sha256':sha256(left/'manifest.json'),'right_manifest_sha256':sha256(right/'manifest.json'),
            'left_frame_sha256':a,'right_frame_sha256':b}


def _verify_versions(paths):
    rows=[]
    for path in paths:
        item=read_json(path)
        if item.get('kind') not in {'version_observation','version_cache_reuse'}:continue
        if (item.get('status')!='completed' or item.get('exit_code')!=0
                or item.get('version')!=ENGINE_VERSION or item.get('engine_sha256')!=GUI_SHA256):
            raise ValueError('Version receipt identity or completion mismatch')
        log=path.with_name(path.name.split('.isolated-runtime-v2.json')[0])
        if sha256(log)!=item['log_sha256']:raise ValueError('Version output bytes changed')
        if item['kind']=='version_cache_reuse':
            origin=item['observed_origin'];source=read_json(origin['receipt'])
            if (sha256(origin['receipt'])!=origin['receipt_sha256'] or sha256(origin['log'])!=origin['log_sha256']
                    or source.get('kind')!='version_observation' or source.get('status')!='completed'
                    or source.get('exit_code')!=0 or source.get('version')!=ENGINE_VERSION
                    or source.get('engine_sha256')!=GUI_SHA256 or source['argv']!=item['argv']
                    or source['log_sha256']!=item['log_sha256'] or Path(source['log'])!=Path(origin['log'])):
                raise ValueError('Version reuse does not bind the original successful query')
        rows.append({'path':str(path),'sha256':sha256(path),'kind':item['kind'],
                     'log_sha256':item['log_sha256'],'observed_origin':item.get('observed_origin')})
    counts=dict(Counter(row['kind'] for row in rows))
    originals=[row for row in rows if row['kind']=='version_observation']
    if counts!={'version_observation':1,'version_cache_reuse':13}:
        raise ValueError(f'Expected one actual version query and thirteen bound reuses, got {counts}')
    if any(row.get('observed_origin',{}).get('receipt')!=originals[0]['path'] for row in rows if row['kind']=='version_cache_reuse'):
        raise ValueError('Version reuses have multiple origins')
    return {'status':'pass','counts':counts,'receipts':rows}


def qualify(output):
    output=Path(output).resolve()
    if output.parent!=ROOT/'artifacts/model-trials/preflight' or output.exists():
        raise ValueError('Use a new direct child of artifacts/model-trials/preflight')
    imported=ROOT/'artifacts/model-trials/preflight/repair-import-01'
    import_receipt=read_json(imported/'result.json')
    if import_receipt['status']!='pass':raise ValueError('Import qualification has not passed')
    expected=import_receipt['source_hashes']
    projects={name:imported/name/'project' for name in ('cold','seeded')}
    if any(project_hashes(p)!=expected for p in projects.values()):raise ValueError('Imported project source changed')
    code_names=['kiroclops/integration/isolated_runtime_v2.py','kiroclops/integration/private_desktop_v1.py',
        'kiroclops/integration/import_cache_v1.py','kiroclops/integration/ground_runner.py',
        'kiroclops/integration/runner.py','kiroclops/integration/storage.py','scripts/qualify_repair_runtime_v2.py',
        'adapters/ground-godot4/capture.gd','adapters/godot4/capture.gd']
    # Adapter folder is independently recorded by each capture manifest too.
    code_names=[p for p in code_names if (ROOT/p).is_file()]
    code_hashes={p:sha256(ROOT/p) for p in code_names}
    historical={name:ROOT/'sessions/cli-ground-01/baseline-runs/calibration'/f'{name}-x0-r1' for name in ('r06','r48')}
    scenarios={}
    for name,path in historical.items():
        manifest=read_json(path/'manifest.json')
        if manifest['scenario']['split']!='calibration' or manifest['provenance']['full_source_hashes_before']!=expected or manifest['provenance']['full_source_hashes_after']!=expected:
            raise ValueError('Historical calibration source/scenario identity differs')
        scenarios[name]=manifest['scenario']
    output.mkdir()
    result={'schema_version':1,'status':'running','started_at':utc_now(),
        'scope':'Native private-desktop runtime and cache equivalence across seven capture paths. No model, heldout, threshold change or experiment acceptance.',
        'model_acceptance':False,'source_hashes':expected,'projects':{k:str(v) for k,v in projects.items()},
        'code_hashes':code_hashes,'import_qualification':{'path':str(imported/'result.json'),'sha256':sha256(imported/'result.json')},
        'cache_policy_origin':import_receipt['cache_publication'],'captures':[],'smokes':{},'pair_comparisons':{},'historical_ground_comparisons':{}}
    write_json(output/'result.json',result)
    started=time.perf_counter();all_receipts=[];runs={}
    try:
        with isolated_runtime(project_roots=list(projects.values()),expected_sources={p:expected for p in projects.values()}) as bridge:
            for name,project in projects.items():
                runs[name]={}
                for scenario_id in ('r06','r48'):
                    label='ground-'+scenario_id;run=output/name/label;t0=time.perf_counter()
                    print(json.dumps({'stage':'capture_start','project':name,'funnel':label}),flush=True)
                    ground_runner.capture_project(project,run,game='slay',scenario=scenarios[scenario_id],variant='before')
                    runs[name][label]=run
                    result['captures'].append({'project':name,'funnel':label,'run':str(run),'call_seconds':time.perf_counter()-t0})
                    write_json(output/'result.json',result)
                    print(json.dumps({'stage':'capture_complete','project':name,'funnel':label,'completed':len(result['captures'])}),flush=True)
                label='jump-h03';run=output/name/label;t0=time.perf_counter()
                print(json.dumps({'stage':'capture_start','project':name,'funnel':label}),flush=True)
                runner.capture_project(project,run,hold_ticks=3,variant='after',repeat=1,spawn_offset=0)
                runs[name][label]=run
                result['captures'].append({'project':name,'funnel':label,'run':str(run),'call_seconds':time.perf_counter()-t0})
                print(json.dumps({'stage':'smoke_start','project':name,'completed':len(result['captures'])}),flush=True)
                smoke_start=time.perf_counter();smoke=ground_runner.smoke_project(project,output/name/'smoke')
                verified=ground_runner.verify_smoke(project,output/name/'smoke',expected)
                result['smokes'][name]={'status':smoke['status'],'checks':smoke['checks'],'verifier':verified,
                                       'call_seconds':time.perf_counter()-smoke_start,
                                       'receipt_sha256':sha256(output/name/'smoke/smoke.json')}
                if smoke['status']!='passed' or verified['status']!='pass':raise ValueError('Gameplay smoke did not pass')
                for label,item in smoke['runs'].items():
                    run=Path(item['path']);runs[name]['smoke-'+label]=run
                    result['captures'].append({'project':name,'funnel':'smoke-'+label,'run':str(run),
                        'capture_wrapper_seconds':read_json(run/'manifest.json')['provenance']['wall_seconds']})
                all_receipts=list(bridge.receipts)
                write_json(output/'result.json',result)
                print(json.dumps({'stage':'project_complete','project':name,'smoke':'pass','completed':len(result['captures'])}),flush=True)
            all_receipts=list(bridge.receipts)
        if len(result['captures'])!=14:raise ValueError('Expected fourteen completed captures')
        runtime=[]
        for path in all_receipts:
            receipt=read_json(path)
            if receipt.get('kind') in {'version_observation','version_cache_reuse'}:continue
            project=Path(receipt['project'])
            if project not in projects.values():raise ValueError('Runtime proof belongs to another project')
            verified=verify_runtime_receipt(path,project=project,expected_source_hashes=expected)
            private=read_json(path.parent/'process/process.json')
            runtime.append({'path':str(path),'sha256':sha256(path),'verification':verified,
                'private_receipt_sha256':sha256(path.parent/'process/process.json'),
                'saw_owned_window':private.get('saw_owned_window'),
                'non_input_samples':len(private.get('desktop_samples',[])),
                'owned_process_termination_confirmed':private.get('owned_process_termination_confirmed'),
                'cleanup_errors':private.get('cleanup_errors')})
        result['runtime_receipts']=runtime
        result['version_cache_verification']=_verify_versions(all_receipts)
        if len(runtime)!=14 or any(row['verification']['status']!='pass' for row in runtime):
            raise ValueError('Some runtime isolation evidence is missing or invalid')
        for label in runs['cold']:
            result['pair_comparisons'][label]=_compare(runs['cold'][label],runs['seeded'][label])
        for name in projects:
            for scenario_id,path in historical.items():
                result['historical_ground_comparisons'][name+'-'+scenario_id]=_compare(runs[name]['ground-'+scenario_id],path)
        result['source_preserved']={name:project_hashes(p)==expected for name,p in projects.items()}
        result['code_unchanged']=all(sha256(ROOT/p)==digest for p,digest in code_hashes.items())
        result['import_receipt_unchanged']=sha256(imported/'result.json')==result['import_qualification']['sha256']
        result['status']='pass' if (all(result['source_preserved'].values()) and result['code_unchanged']
            and result['import_receipt_unchanged'] and all(c['status']=='pass' for c in result['pair_comparisons'].values())
            and all(c['status']=='pass' for c in result['historical_ground_comparisons'].values())) else 'fail'
        result['jump_history_limitation']='Jump equivalence is cold-versus-seeded only; no additional parent-IDE source-equivalence claim is made.'
        result['limitations']=['One cold/seeded pair per funnel; fresh process and240frames retained.',
            'This qualification is not the18ground/12jump heldout experiment, a model trial or automatic acceptance.',
            'Import cache policy and version cache change only startup work; all input timings, pixels and sources are checked.']
    except Exception as exc:
        result.update(status='error',error={'type':type(exc).__name__,'message':str(exc)})
        result['partial_runtime_receipts']=[str(p) for p in all_receipts]
        raise
    finally:
        result.update(finished_at=utc_now(),elapsed_seconds=time.perf_counter()-started)
        write_json(output/'result.json',result)
    print(json.dumps({'stage':'complete','status':result['status'],'captures':len(result['captures']),
        'pair_comparisons':len(result['pair_comparisons']),'runtime_receipts':len(result['runtime_receipts']),
        'output':str(output/'result.json'),'elapsed_seconds':result['elapsed_seconds']}),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();result=qualify(args.output)
    raise SystemExit(0 if result['status']=='pass' else 1)
