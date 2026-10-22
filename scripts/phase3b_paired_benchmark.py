"""Two engines on one machine; alternating trials, with profiling outside timing."""
import argparse
import cProfile
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

from kiroclops.visual.schema import read_json, write_json, digest
from kiroclops.cloud.identity import analysis_sources, engine_identity
from kiroclops.cloud.corpus import verify_corpus
from kiroclops.cloud.equivalence import compare
from kiroclops.cloud.benchmark_reference import validate_reference, expected_measurement


def trial(args):
    from kiroclops.cloud.benchmark import measure, summarize_profile
    corpus = read_json(args.corpus / 'manifest.json')
    item = next(x for x in corpus['items'] if x['id'] == args.item)
    parent = args.corpus / item['id']
    engine = engine_identity(4)
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    measurement, timing = measure(args.corpus / item['package'], read_json(parent / 'selection.json'),
                                  read_json(parent / 'contract.json'))
    if profiler:
        profiler.disable()
    write_json(args.output, {'measurement':measurement, 'measurement_sha256':digest(measurement),
                            'timing':timing, 'engine_sha256':engine['engine_sha256'],
                            'profile':summarize_profile(profiler) if profiler else None})


def paired(args):
    if args.output.exists():
        raise ValueError('Use a fresh paired benchmark directory')
    corpus = verify_corpus(args.corpus)
    sources = analysis_sources()
    reference = validate_reference(args.reference,corpus,sources)
    args.output.mkdir(parents=True)
    envs = {}
    engines = {}
    for name, python in [('standard',args.standard),('cool',args.cool)]:
        env = dict(os.environ)
        if name == 'standard':
            env.pop('LD_LIBRARY_PATH', None)
            env.pop('VIRTUAL_ENV', None)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + (os.pathsep + env.get('PYTHONPATH','') if name == 'cool' else '')
        envs[name] = (str(python),env)
        proc = subprocess.run([str(python),'-c', 'import json;from kiroclops.cloud.identity import engine_identity;print(json.dumps(engine_identity(4)))'],
                              env=env,capture_output=True,text=True,timeout=90,check=True)
        engines[name] = json.loads(proc.stdout)
        write_json(args.output / (name + '-engine.json'),engines[name])
    identity_fields = ('opencv_version','numpy_version','python_version','architecture','threads','opencl_enabled')
    if any(engines['standard']['identity'][k] != engines['cool']['identity'][k] for k in identity_fields):
        write_json(args.output/'failure.json',{'status':'environment_mismatch','identity_fields':identity_fields})
        raise ValueError('Matched OpenCV base, Python, NumPy, architecture, threads and OpenCL are required')
    if engines['standard']['engine_sha256'] == engines['cool']['engine_sha256']:
        raise ValueError('Identical engines cannot demonstrate COOL')
    if not any('/opt/cool/' in x for x in engines['cool']['installation']['native_library_paths']):
        raise ValueError('COOL native module was not loaded from its installed environment')
    if any('/opt/cool/' in x for x in engines['standard']['installation']['native_library_paths']):
        raise ValueError('Standard environment loaded COOL')
    record = {'schema_version':1,'kind':'PairedArmCoolBenchmark','status':'running',
              'corpus_sha256':corpus['corpus_sha256'],'analysis_sources':sources,'rows':[],
              'scope':'Same c8g.xlarge, corpus and code. One warmup, five alternating timed trials per engine/package. Profiles excluded.',
              'human_acceptance':'Not a movement transfer or held-out acceptance result'}
    record['reference_sha256']=reference['reference_sha256']
    write_json(args.output/'result.json',record)
    def run(name,item,label,profile=False):
        python,env = envs[name]
        path = args.output / item['id'] / f'{name}-{label}.json'
        command = [python,str(Path(__file__).resolve()),'trial','--corpus',str(args.corpus),'--item',item['id'],'--output',str(path)]
        if profile:
            command.append('--profile')
        subprocess.run(command,env=env,timeout=240,check=True)
        return read_json(path)
    for index,item in enumerate(corpus['items']):
        row = {'id':item['id'],'domain':item['domain'],'timings':{'standard':[],'cool':[]},'stable':True}
        warm = {name:run(name,item,'warmup') for name in ('standard','cool')}
        row['cross_engine'] = compare(warm['standard']['measurement'],warm['cool']['measurement'])
        historical = read_json(args.corpus/item['id']/'historical.json')
        expected = {k:historical[k] for k in ('observations','events') if k in historical}
        row['standard_historical'] = compare(expected,{k:warm['standard']['measurement'][k] for k in expected})
        row['cool_historical'] = compare(expected,{k:warm['cool']['measurement'][k] for k in expected})
        expected_current = expected_measurement(args.reference,reference,item['id'])
        row['standard_reference'] = compare(expected_current,warm['standard']['measurement'])
        row['cool_reference'] = compare(expected_current,warm['cool']['measurement'])
        for repeat in range(5):
            order = ('standard','cool') if (index+repeat)%2 == 0 else ('cool','standard')
            for name in order:
                value = run(name,item,f'timed-{repeat+1}')
                same = value['measurement_sha256'] == warm[name]['measurement_sha256']
                row['stable'] &= same
                row['timings'][name].append({'repeat':repeat+1,'order':list(order),**value['timing'],'same_measurement':same})
        for name in ('standard','cool'):
            value = run(name,item,'profile',True)
            row['stable'] &= value['measurement_sha256'] == warm[name]['measurement_sha256']
        row['median_s'] = {name:statistics.median(x['direct_wall_s'] for x in row['timings'][name]) for name in ('standard','cool')}
        row['cool_speedup'] = row['median_s']['standard']/row['median_s']['cool']
        row['valid'] = row['stable'] and all(row[k]['equivalent'] for k in ('cross_engine','standard_reference','cool_reference'))
        record['rows'].append(row)
        write_json(args.output/'result.json',record)
        print(json.dumps({'id':item['id'],'valid':row['valid'],'median_s':row['median_s'],'cool_speedup':row['cool_speedup']}),flush=True)
        if args.sync_command:
            subprocess.run(args.sync_command,check=True,timeout=60)
    record['sources_unchanged'] = sources == analysis_sources()
    verify_corpus(args.corpus)
    validate_reference(args.reference,corpus,sources)
    record['status'] = 'pass' if record['sources_unchanged'] and all(x['valid'] for x in record['rows']) else 'failed'
    record['standard_total_median_s'] = sum(x['median_s']['standard'] for x in record['rows'])
    record['cool_total_median_s'] = sum(x['median_s']['cool'] for x in record['rows'])
    record['overall_speedup'] = record['standard_total_median_s']/record['cool_total_median_s']
    write_json(args.output/'result.json',record)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command',required=True)
    t = sub.add_parser('trial')
    t.add_argument('--corpus',type=Path,required=True);t.add_argument('--item',required=True)
    t.add_argument('--output',type=Path,required=True);t.add_argument('--profile',action='store_true')
    p = sub.add_parser('paired')
    p.add_argument('--corpus',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--standard',type=Path,required=True);p.add_argument('--cool',type=Path,required=True)
    p.add_argument('--sync-command',nargs='+')
    args = parser.parse_args()
    try:
        (trial if args.command=='trial' else paired)(args)
    except BaseException as exc:
        if args.command=='paired' and args.output.exists():
            write_json(args.output/'interruption.json',{'status':'interrupted','error':str(exc),'partial_results_are_not_complete':True})
        raise


if __name__ == '__main__':
    main()
