"""Bind the assistant's actual raw-image inspection, preserving disagreements."""
from pathlib import Path
from datetime import datetime,timezone
from copy import deepcopy
from kiroclops.visual.schema import read_json,write_json,file_sha256,digest

ROOT=Path(__file__).resolve().parents[1]


def bind():
    base=ROOT/'artifacts/phase3b'
    for domain in ('svg','panel'):
        old=base/'visual-closeout-01'/domain;new=base/'visual-closeout-02'/domain
        a,b=read_json(old/'preparation.json'),read_json(new/'preparation.json')
        assert a['fingerprint']==b['fingerprint']
        assert a['requests']==b['requests']
        assert a['candidate_sha256']==b['candidate_sha256']
        for e in b['requests']:
            assert read_json(old/e['directory']/'request.json')==read_json(new/e['directory']/'request.json')
        source=read_json(old/'completed-source-review.json')
        assert source['source_hashes']==b['fingerprint']['selected_source_hashes']
        assert not (new/'completed-source-review.json').exists()
        write_json(new/'completed-source-review.json',source)
        if domain=='svg':
            assert read_json(old/'check-result.json')['visual_status']=='pass'
            for e in b['requests']:
                relative=e['directory'];marks=read_json(old/'completed-annotations'/relative/'annotation.json')
                assert read_json(old/relative/'review-result.json')['status']=='pass'
                marks['reuse_provenance']={'prior_annotation':str(old/'completed-annotations'/relative/'annotation.json'),
                    'prior_annotation_sha256':file_sha256(old/'completed-annotations'/relative/'annotation.json'),
                    'prior_validator_sha256':file_sha256(old/relative/'review-result.json'),
                    'exact_request_and_raw_identity_checked':True,'new_image_inspection_claimed':False,
                    'reason':'New frozen checker version supports the exact historical cloud launcher. Image, selection, role, event and candidate bindings are identical.'}
                write_json(new/'completed-annotations'/relative/'annotation.json',marks)
        else:
            aid=base/'visual-closeout-01/panel-raw'
            paths=list((aid/'raw').glob('position-*.png'))+list((aid/'event-boards').glob('*.png'))
            paths += [aid/'raw'/f'record-{i:04d}'/'geometry-01.png' for i in range(4)]
            assert len(paths)==19
            inspection={'reviewer':'Codex assistant','at':datetime.now(timezone.utc).isoformat(),'external_human_review':False,
                'method':'Actual view_image inspection of all listed raw glyph, representative event and raw/overlay panel geometry sheets. Computer-assisted foreground mass and outline measurements; not individual manual clicks.',
                'viewed':[{'file':p.relative_to(aid).as_posix(),'sha256':file_sha256(p)} for p in paths],
                'findings':['Bookmark, handle and star glyphs complete and distinguishable on control sheets.',
                    'Final recording shows spring overshoot and independently delayed child movement.',
                    'Resolve record roles from the frozen preparation, never from ordinal order. The BEFORE target has additional raw rest-band departures and different event occurrence counts; type/ordinal association is not enough to certify those timings.',
                    'Independent painted panel outline yields several pair-geometry disagreements. Original tolerance remains unchanged.'],
                'inspection_scope':'Actual assistant engineering review; not external human review. Failed or ambiguous checks remain failed.'}
            write_json(aid/'visual-inspection-progress.json',inspection)
            rows={}
            for p in read_json(aid/'raw/manifest.json')['proposals']:
                assert file_sha256(p['path'])==p['sha256']
                raw=read_json(p['path'])
                for row in raw['rows']:rows[raw['record_id'],row['index']]=row
            req=read_json(new/'positions/panel/request.json');points=[]
            for t in req['tasks']:
                row=rows[t['record_id'],t['index']];assert row['image_sha256']==t['image_sha256']
                xy=row['points'][t['object_id']]
                points.append({k:t[k] for k in ('record_id','object_id','index','image_sha256')}|{'x':xy['x'],'y':xy['y']})
            common={'status':'completed','reviewer':'Codex assistant','external_human_review':False,
                'inspection_journal_sha256':file_sha256(aid/'visual-inspection-progress.json')}
            write_json(new/'completed-annotations/positions/panel/annotation.json',common|{
                'request_sha256':digest(req),'method':'independent_raw_image_marking','points':points,
                'method_detail':'Independent absolute foreground color-mass from raw PNGs; all listed crops viewed. Tracker/DOM values are never observations.'})
            diagnosis=read_json(aid/'unsigned-numeric-diagnosis.json')
            ambiguous={r['record']:{(v['type'],tuple(v['objects'])) for v in r['event_counts'] if v['required']!=v['independent']}
                for r in read_json(aid/'drafts/summary.json')['records']}
            for record in b['fingerprint']['records']:
                name=record['id']
                for scope in ('events','relationships'):
                    relative=f'records/{name}/{scope}'
                    draft=read_json(aid/'drafts'/name/(scope+'.json'))
                    request=read_json(new/relative/'request.json');assert digest(request)==draft['request_sha256']
                    numerical={c['id']:c['pass'] for c in diagnosis['results'][relative]['checks']}
                    checks=[]
                    for c in draft['checks']:
                        ordinal_ambiguous=scope=='events' and (c['type'],tuple(c['object_ids'])) in ambiguous[name]
                        checks.append({'id':c['id'],'verdict':'pass' if numerical[c['id']] and not ordinal_ambiguous else 'fail',
                            'frame_sha256s':c['frame_sha256s'],
                            ('observed_interval_s' if scope=='events' else 'observed'):c['independent_observation'],
                            'evidence':'Independently segmented raw pixel sequence, actual glyph/event/geometry sheets viewed. Numerical disagreement and extra event occurrence ambiguity are preserved; no tolerance relaxation.'})
                    write_json(new/'completed-annotations'/relative/'annotation.json',common|{
                        'request_sha256':digest(request),'method':'independent_raw_sequence_review','checks':checks})
            write_json(new/'raw-review-binding.json',{'status':'inspection_completed_with_disagreements',
                'positions':len(points),'inspected_sheets':19,'ordinal_ambiguities':{k:[list(v) for v in s] for k,s in ambiguous.items() if s},
                'original_diagnosis_sha256':file_sha256(aid/'unsigned-numeric-diagnosis.json'),
                'thresholds_changed':False,'new_model_run':False})
    game=base/'visual-closeout-02/game-limited'
    data=read_json(game/'result.json')
    paths=[game/s['file'] for r in data['records'] for s in r['sheets']]
    assert len(paths)==4
    write_json(game/'visual-inspection.json',{'reviewer':'Codex assistant','status':'limited_controls_inspected',
        'at':datetime.now(timezone.utc).isoformat(),'external_human_review':False,
        'viewed':[{'file':p.relative_to(game).as_posix(),'sha256':file_sha256(p)} for p in paths],
        'raw_result_sha256':file_sha256(game/'result.json'),
        'findings':['All distinct hat/head glyphs actually viewed; anchor tracks the same head feature.',
            'Independent raw positions exactly match tracked controls; median and max error0px on this limited clip.',
            'Reference has10, initial target16, selected evaluation13 and final13 distinct images; the full20 distinct-image gate is not met.'],
        'full_review_accepted':False,'thresholds_changed':False})
    print('Bindings completed; frozen checks still required; panel disagreements retained.')


if __name__=='__main__':bind()
