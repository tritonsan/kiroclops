"""Small prospective checks of sealed Kiro code; no editing/model feedback.

This is not the 3A acceptance matrix. All recipes and source identities are
written before capture. Failures remain failures and no candidate is replaced.
Run as: python -m scripts.phase3c_interaction_probes --output <fresh directory>
"""
from pathlib import Path
from copy import deepcopy
import argparse
import time

from kiroclops.common import ROOT
from kiroclops.visual.schema import read_json, write_json, digest, file_sha256, VisualError
from kiroclops.visual.package import load_package
from kiroclops.local_engine.session import source_hashes
from kiroclops.integration.storage import copy_project
from kiroclops.local_engine.environment import configure_environment

SESSIONS = {
    'game': ROOT/'artifacts/phase3b/kiro-integration-01/game/session',
    'panel': ROOT/'artifacts/phase3b/panel-kiro-recovery-02/session',
    'svg': ROOT/'artifacts/phase3b/kiro-integration-01/svg/session',
}


def register(output, domains=('game','panel','svg')):
    from kiroclops.web_local.specs import capture_scenarios, QUALIFIED_CAPTURE_POLICY, fixture_project
    from kiroclops.portable_v2.specs import scenarios
    from kiroclops.cloud.identity import service_sources, engine_identity
    if output.exists():
        raise VisualError('output_exists', 'Use a fresh probe directory; prior outcomes are never overwritten.')
    sources = {}
    for domain, directory in SESSIONS.items():
        state = read_json(directory/'session.json')
        selected = state['selected_candidate_id']
        row = next(c for c in state['candidates'] if c['candidate_id'] == selected)
        candidate = directory/'candidates'/selected/'source'
        if digest(source_hashes(candidate)) != row['source_sha256']:
            raise VisualError('source_changed', 'Sealed candidate differs: '+domain)
        if source_hashes(directory/'baseline') != state['baseline_hashes']:
            raise VisualError('source_changed', 'Baseline differs: '+domain)
        sources[domain] = {'session':str(directory), 'candidate_id':selected,
            'candidate':str(candidate), 'candidate_sha256':row['source_sha256'],
            'baseline':str(directory/'baseline'), 'baseline_sha256':digest(state['baseline_hashes']),
            'training_scenarios':state['experiment']['calibration'],
            'reference_source':str(fixture_project(domain,'reference')) if domain!='game' else None,
            'reference_sha256':digest(source_hashes(fixture_project(domain,'reference'))) if domain!='game' else None}
    recipes = {'game':[s for s in scenarios('jump','validation') if s['repeat']==1]}
    choices = {'panel':[('interrupt',768),('drag-release',1280)],
               'svg':[('three-to-five',768),('six-to-four',1280)]}
    for domain, names in choices.items():
        all_recipes = capture_scenarios(domain,'validation',QUALIFIED_CAPTURE_POLICY)
        recipes[domain] = [next(s for s in all_recipes if s['recipe']==name and s['viewport'][0]==width and s['repeat']==1)
                           for name,width in names]
    plan = {'kind':'SealedInteractionProbeRegistration','schema_version':1,'registered_at':time.time(),
        'sources':{d:sources[d] for d in domains},'recipes':{d:recipes[d] for d in domains},
        'analysis_sources':service_sources(),'engine':engine_identity(),
        'execution_sources':{str(p.relative_to(ROOT)).replace('\\','/'):file_sha256(p) for p in
            [ROOT/'kiroclops/local_engine/evaluation.py',ROOT/'kiroclops/local_engine/game.py',
             ROOT/'kiroclops/portable_v2/measurement.py',ROOT/'kiroclops/portable_v2/runtime.py',
             ROOT/'kiroclops/web_local/capture.py',ROOT/'kiroclops/web_local/natural_capture.py',ROOT/'kiroclops/web_local/specs.py']},
        'script_sha256':file_sha256(Path(__file__)), 'thresholds':'Unchanged domain comparator gates',
        'model_run':False,'candidate_editing':False,'full_3a_matrix':False,
        'scope':'4 jump, 2 panel and 2 SVG cases; old sessions retain their original no-heldout status.'}
    write_json(output/'registration.json',plan)
    write_json(output/'registration-binding.json',{'sha256':digest(plan)})
    return plan


def run(output,domains=('game','panel','svg')):
    configure_environment()
    plan = register(output,domains)
    required=sum(len(s) for s in plan['recipes'].values())
    rows = []
    def check():
        from kiroclops.cloud.identity import service_sources
        if service_sources()!=plan['analysis_sources']:
            raise VisualError('source_changed','Analysis code changed after probe registration.')
        if any(file_sha256(ROOT/p)!=sha for p,sha in plan['execution_sources'].items()):
            raise VisualError('source_changed','Capture/comparison code changed after registration.')
    def progress(*args):
        pass
    for domain in domains:
        sources=plan['sources'][domain]
        projects={role:Path(sources[key]) for role,key in [('before','baseline'),('after','candidate')]}
        if domain=='game':
            from kiroclops.portable_v2 import runtime
            for role,project in list(projects.items()):
                copied=output/domain/'projects'/role
                copy_project(project,copied)
                runtime.import_project(copied,output/domain/'imports'/role)
                projects[role]=copied
        else:
            projects['reference']=Path(sources['reference_source'])
        for scenario in plan['recipes'][domain]:
            name=scenario['id']+('-x'+str(scenario['spawn_offset']) if domain=='game' else '')
            destination=output/domain/name
            started=time.time()
            print(domain,name,'started',flush=True)
            try:
                check()
                records={}
                if domain=='game':
                    from kiroclops.local_engine.game import analyze_game_run, _canonical_record
                    from kiroclops.portable_v2.measurement import _jump_row, _reliable
                    from kiroclops.portable_v2.specs import run_name
                    raw_reference=ROOT/'artifacts/runs'/('tiny-reference-'+run_name(scenario))
                    records['reference']=analyze_game_run(raw_reference,'jump',destination/'reference')
                    for role in ('before','after'):
                        recipe={k:deepcopy(v) for k,v in scenario.items() if k not in ('mechanic','fps')}
                        recipe['variant']=role
                        raw=destination/role/'raw'
                        runtime.capture_project(projects[role],raw,recipe)
                        records[role]=analyze_game_run(raw,'jump',destination/role/'measurement',
                            candidate_sha256=sources['baseline_sha256' if role=='before' else 'candidate_sha256'])
                    analyses=[]
                    for role in ('reference','before','after'):
                        record=_canonical_record(records[role],scenario,'jump')
                        analysis=read_json(record['analysis']);_reliable(analysis);analyses.append(analysis)
                    comparison=_jump_row(*analyses,True)
                else:
                    from kiroclops.local_engine.evaluation import _capture_web, _compare_web
                    for role in ('reference','before','after'):
                        records[role]=_capture_web(projects[role],destination/role,scenario,check,progress,None,
                            'reference' if role=='reference' else 'target',
                            reference_selection=records.get('reference',{}).get('selection'))
                    comparison=_compare_web(records['reference'],records['before'],records['after'],improvement=True)
                evidence=[]
                for role,record in records.items():
                    manifest=load_package(record['package'],verify='hashes')
                    evidence.append({'role':role,'package':record['package'],'package_sha256':manifest['package_sha256'],
                        'analysis':record['analysis'],'analysis_sha256':file_sha256(record['analysis']),
                        'unique_images':len({f['sha256'] for f in manifest['frames']}),'frame_count':len(manifest['frames'])})
                result={'domain':domain,'scenario':scenario,'status':'pass' if comparison['pass'] else 'fail',
                        'comparison':comparison,'records':evidence,'started_at':started,'finished_at':time.time(),
                        'independent_visual_review':'pending','candidate_modified':False}
            except Exception as exc:
                result={'domain':domain,'scenario':scenario,'status':'invalid','started_at':started,'finished_at':time.time(),
                        'error':{'code':getattr(exc,'code','probe_error'),'message':str(exc)},'candidate_modified':False}
            write_json(destination/'result.json',result)
            rows.append({'domain':domain,'scenario':scenario,'status':result['status'],'path':str(destination/'result.json')})
            write_json(output/'progress.json',{'completed':len(rows),'required':required,'rows':rows})
            print(domain,name,result['status'],result.get('error',''),flush=True)
        for role in ('before','after'):
            key='baseline' if role=='before' else 'candidate'
            if digest(source_hashes(sources[key]))!=sources[key+'_sha256']:
                raise VisualError('source_changed','Sealed '+role+' source changed during probes.')
    summary={'kind':'SealedInteractionProbeResults','registration_sha256':digest(plan),'rows':rows,
        'passed':sum(r['status']=='pass' for r in rows),'failed':sum(r['status']=='fail' for r in rows),
        'invalid':sum(r['status']=='invalid' for r in rows),'required':required,'full_3a_acceptance':False,
        'independent_visual_review':'pending','old_acceptance_changed':False}
    write_json(output/'result.json',summary)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--domains',nargs='+',choices=('game','panel','svg'),default=['game','panel','svg'])
    args=parser.parse_args()
    result=run(args.output.resolve(),tuple(dict.fromkeys(args.domains)))
    print({k:result[k] for k in ('passed','failed','invalid','required')})
