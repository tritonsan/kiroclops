"""Preserve a configuration-readable historical launcher; no runtime policy claim."""
import ast
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'kiroclops/local_engine/launcher.py'
ARCHIVE=ROOT/'artifacts/phase3d/historical-launcher-59866911/launcher.py'
OUTPUT=ARCHIVE.with_name('source-proof.json')
EXPECTED='59866911eca2be15f4a3b73107f445fa3e4e1bd82b81f2d828b4a218958d1138'


def build_proof(data):
    if hashlib.sha256(data).hexdigest()!=EXPECTED:raise ValueError('The historical launcher bytes differ.')
    tree=ast.parse(data.decode('utf-8'))
    nodes={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    return {'kind':'HistoricalLauncherConfigurationSourceArchive','schema_version':1,'status':'pass',
            'archive':'artifacts/phase3d/historical-launcher-59866911/launcher.py','archive_sha256':EXPECTED,
            'model':'claude-opus-5.5','effort':'max','approval_mode':'native_session_allow_always',
            'reviewed_sections':{n:hashlib.sha256(ast.dump(nodes[n],include_attributes=False).encode()).hexdigest()
                                 for n in ('configure','load_config','_run_native')},
            'live_model_run':False,'native_canary':False,'runtime_output_policy_rebound':False,
            'scope':'Byte-exact offline archive. configure, load_config and _run_native were read to preserve historical model/configuration and generated-control checks. This permits reading configuration only; native outcome, source authorship and behavior acceptance require their separate evidence. No launcher execution authorization or runtime-output-policy equivalence.'}


if __name__=='__main__':
    if ARCHIVE.exists():data=ARCHIVE.read_bytes()
    else:
        data=SOURCE.read_bytes();build_proof(data)
        ARCHIVE.parent.mkdir(parents=True,exist_ok=True);ARCHIVE.write_bytes(data)
    proof=build_proof(data)
    if OUTPUT.exists() and json.loads(OUTPUT.read_text())!=proof:raise ValueError('Preserve the earlier archive proof.')
    OUTPUT.write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'archived','proof_sha256':hashlib.sha256(OUTPUT.read_bytes()).hexdigest()}))
