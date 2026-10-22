"""Real COOL correctness and timing; not a matched-version speed comparison."""
import argparse
import cProfile
import json
from pathlib import Path
import statistics
import subprocess
from kiroclops.visual.schema import read_json,write_json,digest
from kiroclops.cloud.corpus import verify_corpus
from kiroclops.cloud.identity import engine_identity,analysis_sources
from kiroclops.cloud.benchmark import measure,summarize_profile
from kiroclops.cloud.equivalence import compare
from kiroclops.cloud.benchmark_reference import validate_reference, expected_measurement


def main():
    p=argparse.ArgumentParser();p.add_argument('--corpus',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--sync-command',nargs='+');args=p.parse_args()
    if args.output.exists():raise ValueError('Fresh output required')
    corpus=verify_corpus(args.corpus);sources=analysis_sources();engine=engine_identity(4)
    reference=validate_reference(args.reference,corpus,sources)
    if not any('/opt/cool/' in x for x in engine['installation']['native_library_paths']):raise ValueError('Not the actual COOL installation')
    write_json(args.output/'engine.json',engine)
    record={'kind':'CoolCorrectnessValidation','schema_version':1,'status':'running','rows':[],
            'corpus_sha256':corpus['corpus_sha256'],'analysis_sources':sources,
            'speed_comparison_status':'blocked_unmatched_opencv_base','software_origin':'Marketplace COOL AMI',
            'no_speedup_claim':True,'scope':'12 calibration packages, one warmup and five timing repetitions, independent profile per package.'}
    record['reference_sha256']=reference['reference_sha256']
    write_json(args.output/'result.json',record)
    for item in corpus['items']:
        root=args.corpus/item['id'];selection=read_json(root/'selection.json');contract=read_json(root/'contract.json')
        warm,timing=measure(args.corpus/item['package'],selection,contract)
        historical=read_json(root/'historical.json')
        historical={k:historical[k] for k in ('observations','events') if k in historical}
        historical_check=compare(historical,{k:warm[k] for k in historical})
        check=compare(expected_measurement(args.reference,reference,item['id']),warm)
        write_json(args.output/item['id']/'measurement.json',warm)
        row={'id':item['id'],'domain':item['domain'],'reference_equivalence':check,'historical_equivalence':historical_check,'warmup_s':timing['direct_wall_s'],'timings':[]}
        for i in range(5):
            current,timing=measure(args.corpus/item['package'],selection,contract)
            row['timings'].append({'repeat':i+1,**timing,'stable':digest(current)==digest(warm)})
        profile=cProfile.Profile();profile.enable();profiled,_=measure(args.corpus/item['package'],selection,contract);profile.disable()
        write_json(args.output/item['id']/'profile.json',summarize_profile(profile))
        row['profile_stable']=digest(profiled)==digest(warm)
        row['median_s']=statistics.median(x['direct_wall_s'] for x in row['timings'])
        row['valid']=check['equivalent'] and row['profile_stable'] and all(x['stable'] for x in row['timings'])
        record['rows'].append(row);write_json(args.output/'result.json',record)
        print(json.dumps({'id':item['id'],'valid':row['valid'],'median_s':row['median_s']}),flush=True)
        if args.sync_command:subprocess.run(args.sync_command,timeout=60,check=True)
    verify_corpus(args.corpus)
    validate_reference(args.reference,corpus,sources)
    record['sources_unchanged']=sources==analysis_sources()
    record['status']='pass' if record['sources_unchanged'] and all(x['valid'] for x in record['rows']) else 'failed'
    write_json(args.output/'result.json',record)


if __name__=='__main__':main()
