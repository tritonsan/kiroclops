"""Reuse exact inspected corrupt pixels after replaying fresh fault results."""
from pathlib import Path
import argparse
import copy
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json,file_sha256,VisualError
from kiroclops.visual.tracking import analyze_package
from kiroclops.local_engine.cohort import _validate_sample


def rebind(old_path,new_path):
    old_path=Path(old_path).resolve();new_path=Path(new_path).resolve()
    old=read_json(old_path);new=read_json(new_path);byid={r['id']:r for r in old['samples']}
    for row in new['samples']:
        prior=byid[row['id']];_validate_sample(prior,old_path.parent)
        a=read_json(Path(prior['package_dir'])/'manifest.json');b=read_json(Path(row['package_dir'])/'manifest.json')
        if a!=b:raise VisualError('stale_review','Raw fault manifest differs')
        for frame in a['frames']:
            left=Path(prior['package_dir'])/frame['path'];right=Path(row['package_dir'])/frame['path']
            if left.exists()!=right.exists() or (left.exists() and file_sha256(left)!=file_sha256(right)):
                raise VisualError('stale_review','Actual corrupt or missing PNG differs')
        selection=read_json(row['selection_path']);prior_selection=read_json(prior['selection_path'])
        # The independently inspected current panel now has a separate painted
        # outer outline. All original identity ROIs and fault patches must match.
        normalized=copy.deepcopy(selection)
        for obj in normalized['objects']:obj.pop('geometry',None)
        if normalized!=prior_selection:raise VisualError('stale_review','Identity point or faulty selection changed')
        if file_sha256(row['contract_path'])!=file_sha256(prior['contract_path']):
            raise VisualError('stale_review','Fault thresholds changed')
        actual=read_json(row['result_path']);previous=read_json(prior['result_path'])
        if actual!=previous:raise VisualError('stale_review','The freshly recorded rejection differs')
        if row['id']=='error-wrong-selection':
            try:analyze_package(row['package_dir'],selection)
            except VisualError as exc:
                if exc.code!=actual['error']['code']:raise
            else:raise VisualError('invalid_review','Wrong texture selection unexpectedly succeeded')
        receipt=read_json(prior['independent_review_path'])
        receipt.update(source_manifest_sha256=file_sha256(Path(row['reference_package_dir'])/'manifest.json'),
            package_manifest_sha256=file_sha256(Path(row['package_dir'])/'manifest.json'),
            selection_sha256=file_sha256(row['selection_path']),result_sha256=file_sha256(row['result_path']),
            reuse_provenance={'prior_review_sha256':file_sha256(prior['independent_review_path']),
                'rule':'Every actual PNG and missing-file state is identical; fault definitions and fresh rejection replayed.',
                'selection_delta':'Added separately reviewed painted-panel contour; original identity and corrupted selections unchanged.'})
        destination=Path(row['package_dir']).parent/'independent-review.json'
        if destination.exists():raise VisualError('output_exists','Never replace independent review')
        write_json(destination,receipt)
        row.update(independent_review_path=str(destination),independent_review_sha256=file_sha256(destination),independent_review_status='pass')
        _validate_sample(row,new_path.parent)
    new.update(independent_review_status='pass',status='pass')
    destination=new_path.parent/'reviewed-manifest.json'
    if destination.exists():raise VisualError('output_exists','Never replace reviewed corpus')
    write_json(destination,new);return destination


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('old',type=Path);parser.add_argument('new',type=Path)
    args=parser.parse_args();print(rebind(args.old,args.new))
