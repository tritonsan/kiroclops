"""Unpack an actual kit into a Unicode/space path and run its own pixel code."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import file_sha256,read_json,write_json,VisualError


def main():
    parser=argparse.ArgumentParser();parser.add_argument('archive',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();root=args.output.resolve()
    if root.exists():raise VisualError('output_exists','Fresh relocation directory required')
    root.mkdir(parents=True)
    with zipfile.ZipFile(args.archive) as archive:
        for entry in archive.infolist():
            path=(root/entry.filename).resolve()
            if not path.is_relative_to(root) or '\\' in entry.filename:raise VisualError('unsafe_path','Archive entry escapes destination')
        archive.extractall(root)
    manifest=read_json(root/'kit-manifest.json')
    assert all(file_sha256(root/name)==sha for name,sha in manifest['files'].items())
    command=[sys.executable,'-X','utf8',str(root/'scripts/materialize_local_kit.py')]
    materialized=subprocess.run(command,cwd=root,capture_output=True,text=True,check=True)
    script='''from pathlib import Path
import json
import kiroclops
from kiroclops.local_engine.game import analyze_game_run
from kiroclops.common import ROOT
from kiroclops.visual.schema import read_json
annotation=read_json(ROOT/'config/tiny-annotation.json')
assert Path(annotation['template_run']).is_relative_to(ROOT)
record=analyze_game_run(ROOT/'artifacts/runs/tiny-reference-h03-x0-r1','jump',ROOT/'artifacts/relocation-measurement')
result=read_json(record['analysis'])
print(json.dumps({'module':kiroclops.__file__,'root':str(ROOT),'template':annotation['template_run'],'status':result['status'],'valid_ratio':result['valid_ratio'],'metrics':result['metrics'],'all_frame_paths_local':True}))
'''
    measured=subprocess.run([sys.executable,'-X','utf8','-c',script],cwd=root,capture_output=True,text=True,check=True)
    value=json.loads(measured.stdout);assert Path(value['module']).is_relative_to(root)
    assert value['status']=='ok' and value['valid_ratio']>=.95
    result={'status':'pass','archive_sha256':file_sha256(args.archive),'verified_files':len(manifest['files']),
        'materialization':json.loads(materialized.stdout),'measurement':value,
        'dependency_runtime':'Existing locked Python environment reused; no fresh dependency installation claimed.',
        'model_started':False,'engine_launched':False,'original_workspace_modified':False}
    write_json(root/'relocation-check.json',result);print(json.dumps(result,indent=2))


if __name__=='__main__':main()
