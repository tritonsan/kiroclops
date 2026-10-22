"""Coordinated cold/seeded headless import qualification; no GUI/model/captures."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from kiroclops.integration import import_cache_v1 as cache
from kiroclops.integration import ground_runner
from kiroclops.integration.isolated_runtime_v2 import isolated_runtime, GUI_RELATIVE
from kiroclops.integration.storage import copy_project, project_hashes, read_json, sha256, utc_now, write_json


def qualify(output):
    output=Path(output).resolve()
    if output.parent!=ROOT/'artifacts/model-trials/preflight' or output.exists():
        raise ValueError('Use a new direct child of artifacts/model-trials/preflight')
    source=ROOT/'sessions/cli-ground-01/baseline'
    state=read_json(source.parent/'session.json');expected=state['baseline_hashes']
    if project_hashes(source)!=expected:raise ValueError('Original source fingerprint changed')
    code_names=['kiroclops/integration/import_cache_v1.py','kiroclops/integration/isolated_runtime_v2.py',
                'kiroclops/integration/private_desktop_v1.py','kiroclops/integration/ground_runner.py',
                'kiroclops/integration/runner.py','kiroclops/integration/storage.py',
                'scripts/qualify_import_cache_v1.py']
    code_hashes={p:sha256(ROOT/p) for p in code_names}
    engine=ROOT/GUI_RELATIVE
    output.mkdir()
    result={'schema_version':1,'status':'running','started_at':utc_now(),
            'scope':'Import-only qualification. No GUI capture, model call, heldout motion read, or cohort acceptance.',
            'source':str(source),'source_hashes':expected,'code_hashes':code_hashes,
            'engine':str(engine),'engine_sha256':sha256(engine),'trials':[]}
    write_json(output/'result.json',result)
    print(json.dumps({'stage':'publishing_cache','output':str(output)}),flush=True)
    started=time.perf_counter()
    try:
        publish_start=time.perf_counter()
        publication=cache.publish_import_cache(source,output/'cache',engine=engine,expected_source_hashes=expected)
        result['cache_publication']=publication
        result['cache_publication_wall_seconds']=time.perf_counter()-publish_start
        write_json(output/'cache-publication.json',publication)
        print(json.dumps({'stage':'cache_published','seconds':result['cache_publication_wall_seconds'],
                          'asset_count':publication['asset_count']}),flush=True)
        for name in ('cold','seeded'):
            project=output/name/'project'
            begin=time.perf_counter()
            copy_start=time.perf_counter();copy_project(source,project);copy_seconds=time.perf_counter()-copy_start
            if project_hashes(project)!=expected:raise ValueError('Fresh project copy differs from source')
            row={'name':name,'project':str(project),'copy_seconds':copy_seconds,'seed_seconds':0}
            if name=='seeded':
                seed_start=time.perf_counter()
                row['seed_receipt']=cache.seed_import_cache(publication['cache_dir'],project,engine=engine,
                    expected_source_hashes=expected,expected_manifest_sha256=publication['manifest_sha256'])
                row['seed_seconds']=time.perf_counter()-seed_start
                write_json(output/name/'seed.json',row['seed_receipt'])
            print(json.dumps({'stage':name+'_import_start','copy_seconds':copy_seconds,'seed_seconds':row['seed_seconds']}),flush=True)
            import_start=time.perf_counter()
            with isolated_runtime(project_roots=[output],expected_sources={project:expected}):
                ground_runner.import_project(project,game='slay',runtime_dir=output/name/'runtime',timeout=180)
            row['import_wrapper_seconds']=time.perf_counter()-import_start
            row['source_preserved']=project_hashes(project)==expected
            row['copy_seed_import_sourcecheck_seconds']=time.perf_counter()-begin
            row['import_log']={'path':str(output/name/'runtime/import.log'),
                               'sha256':sha256(output/name/'runtime/import.log')}
            # Extra qualification checks are recorded separately from the normal
            # copy/seed/import/source-check operational path above.
            inspect_start=time.perf_counter()
            row['post_import_assets']=cache.inspect_import_inputs(project,engine=engine,expected_source_hashes=expected)
            row['post_import_assets_match_published_inputs']=row['post_import_assets']['cache_key']==publication['cache_key']
            row['extra_qualification_seconds']=time.perf_counter()-inspect_start
            row['status']='pass' if row['source_preserved'] and row['post_import_assets_match_published_inputs'] else 'fail'
            result['trials'].append(row)
            write_json(output/'result.json',result)
            print(json.dumps({'stage':name+'_complete','status':row['status'],
                'import_wrapper_seconds':row['import_wrapper_seconds'],
                'copy_seed_import_sourcecheck_seconds':row['copy_seed_import_sourcecheck_seconds']}),flush=True)
        cold,seeded=result['trials']
        result['observed_operational_saving_seconds']=cold['copy_seed_import_sourcecheck_seconds']-seeded['copy_seed_import_sourcecheck_seconds']
        result['cache_publication_is_separate_one_time_cost']=True
        result['source_preserved']=project_hashes(source)==expected
        result['code_unchanged']=all(sha256(ROOT/name)==digest for name,digest in code_hashes.items())
        result['cache_manifest_unchanged']=sha256(Path(publication['cache_dir'])/'manifest.json')==publication['manifest_sha256']
        result['status']='pass' if all(row['status']=='pass' for row in result['trials']) and result['source_preserved'] and result['code_unchanged'] and result['cache_manifest_unchanged'] else 'fail'
        result['limitation']='One sequential cold/seeded import pair. Order, OS cache and antivirus may affect timing; actual 240-frame image equivalence and all capture routes remain unqualified here.'
    except Exception as exc:
        result.update(status='error',error={'type':type(exc).__name__,'message':str(exc)})
        raise
    finally:
        result.update(finished_at=utc_now(),elapsed_seconds=time.perf_counter()-started)
        write_json(output/'result.json',result)
    print(json.dumps({'stage':'complete','status':result['status'],'output':str(output/'result.json'),
                      'saving_seconds':result.get('observed_operational_saving_seconds')}),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();result=qualify(args.output)
    raise SystemExit(0 if result['status']=='pass' else 1)
