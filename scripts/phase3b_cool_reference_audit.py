"""Reassess preserved COOL outputs against a fresh, source-bound local engine."""
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone

from kiroclops.cloud.benchmark import measure
from kiroclops.cloud.benchmark_reference import validate_reference
from kiroclops.cloud.corpus import verify_corpus
from kiroclops.cloud.equivalence import compare, POLICY
from kiroclops.cloud.identity import analysis_sources, engine_identity
from kiroclops.visual.schema import digest, read_json, write_json, file_sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--cool', type=Path, required=True)
    parser.add_argument('--arm', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Fresh audit evidence directory required')
    corpus = verify_corpus(args.corpus)
    sources = analysis_sources()
    runs = {name: read_json(root/'result.json') for name, root in [('cool', args.cool), ('arm', args.arm)]}
    for name, run in runs.items():
        if run['analysis_sources'] != sources or run['corpus_sha256'] != corpus['corpus_sha256'] or not run['sources_unchanged']:
            raise ValueError(name+' result source/corpus binding mismatch')
        if len(run['rows']) != len(corpus['items']) or run['status'] == 'running':
            raise ValueError(name+' result incomplete')
    args.output.mkdir(parents=True)
    registration = {'created_at': datetime.now(timezone.utc).isoformat(), 'corpus_sha256': corpus['corpus_sha256'],
                    'analysis_sources': sources, 'comparison_policy': POLICY,
                    'input_results': {n: file_sha256(root/'result.json') for n, root in [('cool', args.cool), ('arm', args.arm)]},
                    'preexisting_historical_outputs_will_not_be_rewritten': True,
                    'no_new_aws_execution': True, 'no_pure_cool_speedup_claim': True}
    write_json(args.output/'registration.json', registration)
    engine = engine_identity(4)
    reference = {'kind': 'SourceBoundBenchmarkReference', 'schema_version': 1,
                 'corpus_sha256': corpus['corpus_sha256'], 'analysis_sources': sources,
                 'engine': engine, 'items': []}
    result = {'kind': 'CoolReferenceBindingAudit', 'schema_version': 1, 'status': 'running',
              'analysis_sources': sources, 'corpus_sha256': corpus['corpus_sha256'], 'rows': [],
              'comparison_policy': POLICY, 'speed_comparison': 'blocked_unmatched_opencv_base'}
    write_json(args.output/'result.json', result)
    for item in corpus['items']:
        root = args.corpus/item['id']
        local, timing = measure(args.corpus/item['package'], read_json(root/'selection.json'), read_json(root/'contract.json'))
        path = args.output/'reference'/item['id']/'measurement.json'
        write_json(path, local)
        reference['items'].append({'id': item['id'], 'package_sha256': item['package_sha256'],
                                   'measurement_path': item['id']+'/measurement.json',
                                   'measurement_file_sha256': file_sha256(path), 'measurement_sha256': digest(local)})
        arm = read_json(args.arm/item['id']/'measurement.json')
        cool = read_json(args.cool/item['id']/'measurement.json')
        row = {'id': item['id'], 'domain': item['domain'], 'local_timing': timing,
               'local_vs_arm': compare(local, arm), 'local_vs_cool': compare(local, cool),
               'arm_vs_cool': compare(arm, cool),
               'local_measurement_sha256': digest(local), 'arm_measurement_file_sha256': file_sha256(args.arm/item['id']/'measurement.json'),
               'cool_measurement_file_sha256': file_sha256(args.cool/item['id']/'measurement.json')}
        row['equivalent'] = all(row[k]['equivalent'] for k in ('local_vs_arm', 'local_vs_cool', 'arm_vs_cool'))
        result['rows'].append(row)
        write_json(args.output/'result.json', result)
        print(json.dumps({'id': item['id'], 'equivalent': row['equivalent']}), flush=True)
    if sources != analysis_sources():
        raise ValueError('Analysis sources changed during audit')
    verify_corpus(args.corpus)
    reference['reference_sha256'] = digest(reference)
    write_json(args.output/'reference/reference.json', reference)
    validate_reference(args.output/'reference', corpus, sources)
    result.update(status='pass' if all(r['equivalent'] for r in result['rows']) else 'failed',
                  sources_unchanged=True, reference_sha256=reference['reference_sha256'],
                  historical_code_matches_current=corpus['analysis_sources'] == sources,
                  diagnosis='Historical outputs predate the NCC tie fix; preserved COOL and standard ARM use the same corrected code.',
                  original_failed_record_preserved=True)
    write_json(args.output/'result.json', result)


if __name__ == '__main__':
    main()
