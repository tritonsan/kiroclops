"""Prove explicit backend configuration leaves pinned native transport intact."""
from pathlib import Path
import json
from scripts.prove_native_guard_diagnostics import ROOT,sections,sha,binding

BASE=ROOT/'artifacts/phase3b/cloud-launcher-proof-01'
OUTPUT=BASE/'native-transport-proof.json'
CURRENT=ROOT/'kiroclops/local_engine/launcher.py'
BEFORE=BASE/'launcher-before.py'
PRIOR=ROOT/'artifacts/phase3a/acceptance-reporting-01/native-transport-proof.json'
EXPECTED='762693695c8c74448bd9494fc8acca7bfcb7914bed7be0a83d5d1a15e5bcdfe3'


def build_proof():
    prior=json.loads(PRIOR.read_bytes())
    if sha(PRIOR.read_bytes())!='bf1a6eb53c67eac30fe94f47afb8a164e0199baff52fad9b89f579cd7f8f57bb':
        raise RuntimeError('Historical proof changed')
    if sha(CURRENT.read_bytes())!=EXPECTED:raise RuntimeError('Current launcher changed')
    if sha(BEFORE.read_bytes())!=prior['current_local_launcher']['sha256']:raise RuntimeError('Historical launcher reconstruction differs')
    for field in ('guard_bootstrap','current_portable_client','installed_runtime_source_proof'):
        if sha((ROOT/prior[field]['path']).read_bytes())!=prior[field]['sha256']:raise RuntimeError('Pinned dependency changed')
    runtime=json.loads((ROOT/prior['installed_runtime_source_proof']['path']).read_bytes())
    if sha(Path(runtime['source']).read_bytes())!=runtime['source_sha256']:raise RuntimeError('Installed native runtime changed')
    current=CURRENT.read_text('utf8');restored=current
    start=restored.index("    backend_mode=os.environ.get('KIROCLOPS_ANALYSIS_BACKEND'")
    end=restored.index('    command = native._shell_command',start)
    restored=restored[:start]+restored[end:]
    restored=restored.replace("              'analysis_execution':analysis_execution,\n",'')
    start=restored.index("    execution=config.get('analysis_execution'")
    end=restored.index("    if (config.get('model')",start)
    restored=restored[:start]+restored[end:]
    if restored.encode()!=BEFORE.read_bytes():raise RuntimeError('Change outside backend configuration')
    if sections(current)!=sections(restored):raise RuntimeError('Native transport changed')
    return {**prior,'kind':'CloudBackendNativeTransportCompatibility','current_local_launcher':binding(CURRENT),
        'previous_diagnostic_proof':binding(PRIOR),'previous_diagnostic_launcher':binding(BEFORE),
        'allowed_changes':['Explicit analysis backend in MCP environment','Pinned cloud configuration receipt','Verify cloud configuration before launch'],
        'historical_snapshot_reconstruction':'Removing exactly the three new configuration blocks reproduces the historical SHA-256 byte for byte.',
        'scope':'Only backend configuration changed. Native transport, model/effort, permissions and hook decisions remain pinned. No new live-canary or transfer-success claim.'}


if __name__=='__main__':
    proof=build_proof();encoded=(json.dumps(proof,indent=2)+'\n').encode()
    if OUTPUT.exists() and OUTPUT.read_bytes()!=encoded:raise RuntimeError('Never overwrite an old proof')
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    if not OUTPUT.exists():OUTPUT.write_bytes(encoded)
    print(json.dumps({'status':'pass','sha256':sha(encoded)}))
