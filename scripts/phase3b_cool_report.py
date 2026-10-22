"""Report paired feasibility without treating engine speed as product acceptance."""
import argparse
from pathlib import Path
from statistics import median
import json
from kiroclops.visual.schema import read_json, write_json


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--probe',type=Path,required=True)
    args=parser.parse_args()
    parent=args.probe/'download/paired'
    result=read_json(parent/'result.json')
    if result['status']=='running' or len(result['rows'])!=12:
        raise ValueError('Incomplete benchmark cannot be reported as complete')
    standard=read_json(parent/'standard-engine.json')
    cool=read_json(parent/'cool-engine.json')
    records=result['rows']
    report={'status':result['status'],'equivalent_packages':sum(x['valid'] for x in records),
            'total_packages':12,'standard_total_median_s':result['standard_total_median_s'],
            'cool_total_median_s':result['cool_total_median_s'],'overall_speedup':result['overall_speedup'],
            'analysis_time_reduction_percent':100*(1-result['cool_total_median_s']/result['standard_total_median_s']),
            'domain_speedup':{domain:sum(x['median_s']['standard'] for x in records if x['domain']==domain)/sum(x['median_s']['cool'] for x in records if x['domain']==domain) for domain in ('game','panel','svg')},
            'engine_identity':{name:{k:engine['identity'][k] for k in ('opencv_version','numpy_version','python_version','threads','architecture')}
                               | {'engine_sha256':engine['engine_sha256']} for name,engine in [('standard',standard),('cool',cool)]},
            'not_claimed':['upload latency speedup','COOL setup-free local client','held-out movement acceptance','external hosted license approval'],
            'cool_service_eligible':result['status']=='pass'}
    write_json(args.probe/'summary.json',report)
    lines=['# COOL real-workload feasibility','',f"Status: **{report['status']}**, equivalent and stable packages: **{report['equivalent_packages']}/12**.",'',
           'The same code, Python/NumPy/OpenCV base and four threads were used on a single c8g.xlarge. For each package/engine there was one warm-up and five alternating timings; profiles are separate from timing.','',
           '| Package | Standard median s | COOL median s | Standard / COOL | Accuracy |',
           '|---|---:|---:|---:|---|']
    for x in records:
        lines.append(f"| {x['id']} | {x['median_s']['standard']:.3f} | {x['median_s']['cool']:.3f} | {x['cool_speedup']:.3f}× | {x['valid']} |")
    lines += ['',f"Total of the 12 package median times: {report['standard_total_median_s']:.3f} → {report['cool_total_median_s']:.3f} s. Ratio: **{report['overall_speedup']:.3f}×**; change in analysis time: **{report['analysis_time_reduction_percent']:.2f}% reduction**.",'',
              'This ratio does not include upload, queue, engine startup or local application recording. It is not claimed that the overall user session is sped up by the same ratio.',
              'The Windows local measurement carries an operating-system/Python difference; the COOL effect is assessed only with the two matched engines on the same ARM machine.',
              'The earlier local disk and ROI improvements are in the code shared by both engines; they are not counted again as COOL gain.',
              'Categorical event/identity/validity equality and the predetermined numerical engine equivalence have been preserved. Held-out validation, motion transfer or external-user acceptance is not considered to have been performed.']
    (args.probe/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
