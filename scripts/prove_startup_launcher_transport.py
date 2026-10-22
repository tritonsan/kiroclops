"""Bind pre-prompt recovery to unchanged native client, guard and output protocol."""
import ast
from pathlib import Path
from scripts.prove_native_guard_diagnostics import ROOT, sha, binding
from kiroclops.visual.schema import read_json, write_json


def section(text, name):
    return next(ast.get_source_segment(text,n) for n in ast.parse(text).body
                if getattr(n,'name',None)==name)


def run():
    base=ROOT/'artifacts/phase3b/startup-transport-proof-01'
    if base.exists():raise ValueError('Preserve earlier source proofs')
    current=ROOT/'kiroclops/local_engine/launcher.py'
    historical=ROOT/'artifacts/phase3b/cloud-launcher-proof-01/launcher-before.py'
    prior_path=ROOT/'artifacts/phase3b/cloud-launcher-proof-01/native-transport-proof.json'
    prior=read_json(prior_path)
    assert sha(prior_path.read_bytes())=='a15047b492fb54640e35e4000a5abc26064d2c5223a6b07385154ba4a2af65e8'
    before=historical.read_text('utf8');after=current.read_text('utf8')
    assert sha(historical.read_bytes())==prior['previous_diagnostic_launcher']['sha256']
    restored=after
    for name in ('preflight','_run_native'):
        old,new=section(before,name),section(after,name)
        assert restored.count(new)==1
        restored=restored.replace(new,old)
    # Both source bodies are independently frozen by the previous proof.
    # No guard/client/model/configuration code changed with this startup edit.
    assert sha(restored.encode('utf8'))==prior['current_local_launcher']['sha256']
    assert prior['current_local_launcher']['sha256']=='762693695c8c74448bd9494fc8acca7bfcb7914bed7be0a83d5d1a15e5bcdfe3'
    unchanged={}
    for name in ('LocalACPClient','guard','configure','load_config'):
        assert section(after,name)==section(restored,name)
        unchanged[name]=sha(section(after,name).encode('utf8'))
    for field in ('guard_bootstrap','current_portable_client','installed_runtime_source_proof'):
        assert sha((ROOT/prior[field]['path']).read_bytes())==prior[field]['sha256']
    runtime=read_json(ROOT/prior['installed_runtime_source_proof']['path'])
    assert sha(Path(runtime['source']).read_bytes())==runtime['source_sha256']
    base.mkdir()
    (base/'launcher-before.py').write_bytes(restored.encode('utf8'))
    value={**prior,'kind':'PrePromptStartupNativeTransportCompatibility','status':'pass',
        'current_local_launcher':binding(current),'previous_cloud_proof':binding(prior_path),
        'previous_cloud_launcher':binding(base/'launcher-before.py'),
        'startup_helper':binding(ROOT/'kiroclops/local_engine/startup.py'),
        'unchanged_native_sections':unchanged,'live_model_call':False,
        'allowed_changes':['Direct initialize/tools-list before native connection','At most two native connection attempts before any prompt','Preserve each failed startup journal; no retry after generation'],
        'source_reconstruction':'Restoring exactly preflight and _run_native from the pinned source recreates the previous cloud launcher byte for byte.',
        'scope':'Native output decoding compatibility only. Guard, native client, model, approval and post-turn code are unchanged. Recovery safety is tested separately; no whole-run acceptance, missing-hook exemption or permission expansion.'}
    write_json(base/'native-transport-proof.json',value)
    print({'status':'pass','launcher_sha256':sha(current.read_bytes()),
           'proof_sha256':sha((base/'native-transport-proof.json').read_bytes())})


if __name__=='__main__':run()
