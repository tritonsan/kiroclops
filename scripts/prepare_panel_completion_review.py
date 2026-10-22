"""Prepare current event/relationship review; retain exact reviewed positions."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest
from kiroclops.visual.events import extract_events
from kiroclops.local_engine.review import prepare_scope_review,evaluate_review

def run():
    base=ROOT/'artifacts/phase3a';previous=base/'panel-recorder-independent-review-01'
    output=base/'completion-panel-review-01'
    if output.exists():raise ValueError('Preserve previous review output')
    inventory=read_json(previous/'inventory.json');position=previous/'positions'
    checked=evaluate_review(position,read_json(position/'annotation.json'),persist=False)
    if checked!=read_json(position/'review-result.json') or checked['status']!='pass':raise ValueError('Prior image markings do not replay')
    positions={'directory':str(position),'result_sha256':file_sha256(position/'review-result.json'),
               'request_sha256':digest(read_json(position/'request.json'))}
    rows=[];bridge=output/'scope-adapter'
    for entry in inventory['records']:
        if file_sha256(entry['record_path'])!=entry['record_sha256']:raise ValueError('Original record changed')
        record=read_json(entry['record_path']);name=entry['id']
        updated=extract_events(record['observations'],record['selection'],{'event_options':{'rest_duration_s':.25}})
        if updated!=read_json(base/'confirmed-rest-validation-01/events'/(name+'.json')):raise ValueError('Event regression evidence changed')
        record={**record,'events':updated}
        write_json(output/'records'/(name+'.json'),record)
        folder=bridge/'inputs'/name
        write_json(folder/'analysis.json',{'observations':record['observations']});write_json(folder/'selection.json',record['selection'])
        row={'id':name,'domain':'panel','role':entry['role'],'package':entry['package'],
             'package_sha256':record['observations']['package_sha256'],'observations_sha256':digest(record['observations']),
             'analysis_path':str(folder/'analysis.json'),'selection_sha256':digest(record['selection'])}
        for scope in ('events','relationships'):
            dest=output/'scopes'/name/scope
            prepare_scope_review(entry['package'],record['observations'],updated,dest,scope=scope,candidate_sha256=inventory['candidate_sha256'])
            write_json(bridge/'panel/records'/name/scope/'request.json',read_json(dest/'request.json'))
        rows.append(row);print(name,flush=True)
    write_json(bridge/'preparation.json',{'records':rows,'status':'unsigned'})
    write_json(output/'inventory.json',{'status':'pending','records':rows,'position_reviews':[positions],
        'candidate_sha256':inventory['candidate_sha256'],'required_observations':inventory['required_observations'],
        'position_reuse':'Exact pixel and observation identity; existing independently reviewed marks replayed unchanged.'})

if __name__=='__main__':run()
