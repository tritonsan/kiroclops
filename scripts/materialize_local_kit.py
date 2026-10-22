"""Resolve portable annotation templates into this installation, preserving inputs."""
from pathlib import Path
import json


def materialize(root):
    root=Path(root).resolve();changed=[]
    def resolve(value):
        if isinstance(value,dict):return {k:resolve(v) for k,v in value.items()}
        if isinstance(value,list):return [resolve(v) for v in value]
        if isinstance(value,str) and value.startswith('@PACKAGE_ROOT@/'):
            relative=value[len('@PACKAGE_ROOT@/'):];path=(root/relative).resolve()
            if not path.is_relative_to(root):raise ValueError('Package path escapes installation')
            return str(path)
        return value
    for template in sorted((root/'config').rglob('*.json.in')):
        target=template.with_suffix('');value=resolve(json.loads(template.read_text('utf-8')))
        if target.exists() and json.loads(target.read_text('utf-8'))!=value:
            raise ValueError(f'Existing annotation differs: {target.name}; use a fresh kit directory')
        target.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');changed.append(target.relative_to(root).as_posix())
    return {'status':'ready','annotations':changed}


if __name__=='__main__':print(json.dumps(materialize(Path(__file__).resolve().parents[1]),indent=2))
