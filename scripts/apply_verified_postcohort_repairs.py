"""Apply the four tested repairs only after both frozen reports are complete.

The registration, original trial outcomes, captures and thresholds are never
rewritten. Source backups and an application receipt permit exact recovery.
"""
from pathlib import Path
import argparse
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256


def apply(primary,posthoc):
    primary,posthoc=Path(primary).resolve(),Path(posthoc).resolve()
    base=ROOT/'artifacts/phase3a'
    folder=base/'completion-postcohort-repairs-01'
    receipt=folder/'application.json'
    assert not receipt.exists(),'Do not overwrite repair history'
    proposal=read_json(folder/'proposal.json')
    tests=read_json(folder/'staged-tests.json')
    assert tests['status']=='pass' and tests['proposal_sha256']==file_sha256(folder/'proposal.json')
    a,b=read_json(primary/'cohort-report.json'),read_json(posthoc/'result.json')
    progress=base/'completion-paired-cohort-01/progress.json'
    registry=base/'completion-paired-cohort-01/registration.json'
    frozen=read_json(registry)
    from kiroclops.local_engine.cohort import protocol_sources
    assert protocol_sources()==frozen['source_hashes'],'Unexpected source edits'
    assert a['registration_sha256']==b['registration_sha256']==file_sha256(registry)
    assert a['progress_sha256']==b['progress_sha256']==file_sha256(progress)
    assert b['status']=='completed' and b['primary_outcomes_modified'] is False
    assert b['primary_report_sha256']==file_sha256(primary/'cohort-report.json')
    assert len(a['rows'])==len(b['rows'])==18
    assert all(row['status'] in ('failed','completed','interrupted') for row in a['rows'])
    manifest=read_json(primary/'package-manifest.json')
    for name,digest in manifest['files'].items():
        path=(primary/name).resolve()
        assert path.is_relative_to(primary) and file_sha256(path)==digest
    reviews=[]
    for row in read_json(progress)['rows']:
        for stage in row['stages']:
            session=Path(stage['session'])
            if not (session/'session.json').is_file():continue
            state=read_json(session/'session.json')
            assert not state.get('active_job_id')
            if (state.get('final') or {}).get('status')!='succeeded':continue
            reviewed=session/'independent_review.json'
            assert read_json(reviewed)['status']=='pass'
            reviews.append({'path':str(reviewed),'sha256':file_sha256(reviewed)})
    assert len(reviews)==12,'Finish every available final visual review first'
    # Prepare every payload before writing any production file.
    changes=[]
    for item in proposal['files']:
        target=(ROOT/item['target']).resolve();source=(ROOT/item['proposal']).resolve()
        assert target.is_relative_to(ROOT/'kiroclops') and source.is_relative_to(base)
        assert file_sha256(target)==item['original_sha256']
        assert file_sha256(source)==item['proposed_sha256']
        changes.append((target,target.read_bytes(),source.read_bytes()))
    additions=[]
    for name,digest in proposal['tests'].items():
        source=folder/'tests'/name;target=ROOT/'tests'/name
        assert Path(name).name==name and not target.exists()
        assert file_sha256(source)==digest
        additions.append((target,source.read_bytes()))
    backups=folder/'original-sources';assert not backups.exists()
    for target,old,new in changes:
        backup=backups/target.relative_to(ROOT);backup.parent.mkdir(parents=True,exist_ok=True)
        backup.write_bytes(old)
    try:
        for target,old,new in changes:target.write_bytes(new)
        for target,data in additions:target.write_bytes(data)
        for item in proposal['files']:
            assert file_sha256(ROOT/item['target'])==item['proposed_sha256']
    except BaseException:
        for target,old,new in changes:target.write_bytes(old)
        for target,data in additions:
            if target.exists() and target.read_bytes()==data:target.unlink()
        raise
    result={'status':'applied_regression_required','files':proposal['files'],
        'test_modules':proposal['tests'],'visual_reviews':reviews,
        'registration_sha256':file_sha256(registry),'progress_sha256':file_sha256(progress),
        'primary_report_sha256':file_sha256(primary/'cohort-report.json'),
        'posthoc_report_sha256':file_sha256(posthoc/'result.json'),
        'proposal_sha256':file_sha256(folder/'proposal.json'),
        'production_modified':True,'primary_outcomes_modified':False,
        'acceptance_thresholds_changed':False,'static_sampling_changed':False}
    write_json(receipt,result)
    print({'status':result['status'],'files':len(changes),'test_modules':len(additions)})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--primary',required=True,type=Path)
    p.add_argument('--posthoc',required=True,type=Path)
    args=p.parse_args();apply(args.primary,args.posthoc)
