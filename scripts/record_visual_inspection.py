"""Record files already displayed and inspected by the calling reviewer.

This command records provenance only; it cannot approve an experiment.
"""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json,file_sha256,safe_path
p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('files',nargs='+');a=p.parse_args()
r=a.root;dest=r/'visual-inspection-progress.json'
value=read_json(dest) if dest.exists() else {'status':'in_progress','reviewer':'Codex assistant','external_human_review':False}
seen={row['file']:row for row in value.get('viewed',[])}
for name in a.files:
    path=safe_path(r,name);seen[name]={'file':name,'sha256':file_sha256(path)}
value['viewed']=list(seen.values());write_json(dest,value);print({'inspected_files':len(seen)})
