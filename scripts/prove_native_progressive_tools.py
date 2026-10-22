"""Read-only proof of the pinned KAS progressive disclosure implementation.

This inspects installed bytes. It never starts Kiro or a model and does not
claim runtime attestation, authorization, or tool-boundary enforcement.
"""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
PRIOR = ROOT / 'artifacts/phase3a/native-hook-sol24-installed-source-proof.json'
OUTPUT = ROOT / 'artifacts/phase3d/native-progressive-tool-source-proof.json'
SOURCE = Path.home()/Path('AppData/Local/Kiro-Cli/kas/2.27.0-2943e5ce601c8f80b0b4240176f70606c25c931dfc0d9c74c98f5e4e054d5475/node_modules/@kiro/agent/dist/server/acp-server.js')
SOURCE_SHA256 = '79a1a743ee7236a71bba9c6c68342deccfcffea4d61361eae0254288339b19c8'
# Exact, independently inspected sections of the installed minified bundle.
SECTIONS = {
    'passthrough_output': ('ty=class{', 'function hvt('),
    'tool_load': ('G$e={id:"tool_load"', 'async function hwc('),
    'progressive_call': ('var tPr={id:"tool_call"', ';P1r();Tkr();gve();'),
    'deferred_hook_dispatch': ('async dispatchAsDeferred(', 'concludedToolUses=new WeakMap'),
    'model_arguments_projection': ('emitAction(t,r){t.execution.emit({...r,rawInput:this.deferredModelArgs', 'get streamErrorActionType(){'),
    'mcp_wrapper': ('n0t=class e extends Kt', 'async callToolWithElicitation('),
    'server_dispatch': ('dispatch:async(Je,pt)=>{let Lt=Wt(ie)', 'onLoadedToolVanished:'),
}


def build_proof():
    data = SOURCE.read_bytes()
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise ValueError('Installed KAS source differs from the inspected bundle')
    fragments = {}
    for name, (start, end) in SECTIONS.items():
        needle, stop = start.encode(), end.encode()
        if data.count(needle) != 1:
            raise ValueError(f'Ambiguous inspected section: {name}')
        begin = data.index(needle)
        finish = data.index(stop, begin + len(needle))
        fragment = data[begin:finish]
        fragments[name] = {'byte_offset': begin, 'byte_length': len(fragment),
                           'sha256': hashlib.sha256(fragment).hexdigest()}
    return {'kind': 'PinnedKASProgressiveToolSourceReview', 'schema_version': 1,
            'status': 'pass', 'cli_version': 'kiro-cli-chat 2.27.0', 'kas_version': '0.66.22',
            'source': str(SOURCE), 'source_sha256': SOURCE_SHA256,
            'fragments': fragments, 'live_model_run': False,
            'reviewed_behavior': {
                'tool_load_is_catalog_disclosure_with_passthrough_output': True,
                'tool_load_concludes_without_normal_start': True,
                'tool_call_dispatches_exact_server_tool_id_and_inner_arguments': True,
                'deferred_dispatch_runs_underlying_pre_tool_hook': True,
                'native_events_project_original_model_wrapper_arguments': True,
                'mcp_wrapper_keeps_server_tool_identity_and_emits_pending_running_terminal': True},
            'limits': 'Static pinned source review only. Recorded exact native envelopes and independently matched hook decisions remain required. No runtime signature or launcher equivalence claim.'}


if __name__ == '__main__':
    proof = build_proof()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'pass', 'output': str(OUTPUT),
                      'sha256': hashlib.sha256(OUTPUT.read_bytes()).hexdigest()}))
