"""Install the KiroClops power for the Kiro IDE from this checkout.

Post-freeze addition (25 October 2026). Renders ``integrations/kiro-power/kiroclops`` with
this checkout's code path and Python interpreter into a local power folder. Add that folder
in Kiro as a custom power (Powers, add from a local folder), or pass ``--activate`` to
replace an already installed custom power named ``kiroclops``: its folder is backed up
first, Kiro's custom-power registry entry is pointed at the rendered folder, and the server
is registered in Kiro's user MCP settings with its tools auto-approved (Kiro does not apply
auto-approval to servers declared by a power); the power then carries the instructions and
the tile only.

The power runs ``kiroclops.local_engine.workspace_mcp``: after one request in a supported
project, Kiro sets up the KiroClops session itself. Run this with the Python environment
that has the KiroClops dependencies and ``mcp`` (``pip install -e .[kiro]``).

    python scripts/install_kiro_power.py
    python scripts/install_kiro_power.py --sessions D:/kiroclops-sessions --activate
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'integrations'/'kiro-power'/'kiroclops'


def local_data():
    return Path(os.environ.get('LOCALAPPDATA', Path.home()/'.local'/'share'))/'KiroClops'


def render(destination, sessions):
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(SOURCE, destination, ignore=shutil.ignore_patterns('mcp.template.json'))
    values = {'python': sys.executable, 'code': str(ROOT), 'sessions': str(sessions), 'path': os.environ.get('PATH', '')}
    # The launcher puts this checkout first on sys.path, whatever folder Kiro starts it in; a
    # working directory that holds another kiroclops package would otherwise shadow this one.
    launcher = destination/'server.py'
    launcher.write_text('import runpy\nimport sys\n\n'
                        f'sys.path.insert(0, {str(ROOT)!r})\n'
                        "runpy.run_module('kiroclops.local_engine.workspace_mcp', run_name='__main__', alter_sys=True)\n",
                        encoding='utf-8')
    template = json.loads((SOURCE/'mcp.template.json').read_text(encoding='utf-8'))
    server = template['mcpServers']['kiroclops']
    server['command'] = values['python']
    server['args'] = ['-B', str(launcher)]
    server['env'] = {key: value.format(**values) for key, value in server['env'].items()}
    (destination/'mcp.json').write_text(json.dumps(template, indent=2)+'\n', encoding='utf-8')
    return destination


def register_user_server(server):
    """Register the server in Kiro's user MCP settings, where auto-approval applies.

    Kiro deletes ``autoApprove`` from servers that a power declares, so a session would ask
    for approval on every one of its many job polls. The power keeps the instructions and the
    tile; the server is a user-level entry, and no power-declared copy is left to duplicate it.
    """
    settings = Path.home()/'.kiro'/'settings'/'mcp.json'
    value = json.loads(settings.read_text(encoding='utf-8')) if settings.exists() else {}
    if settings.exists():
        shutil.copy2(settings, local_data()/'power-backups'/f"mcp-settings-{time.strftime('%Y%m%d-%H%M%S')}.json")
    value.setdefault('mcpServers', {})['kiroclops'] = {'command': server['command'], 'args': server['args'], 'env': server['env'],
                                                      'disabled': False, 'autoApprove': server['autoApprove']}
    powers = value.get('powers', {}).get('mcpServers', {})
    for name in [n for n in powers if n.startswith('power-kiroclops-')]:
        del powers[name]
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(value, indent=2)+'\n', encoding='utf-8')
    return settings


def activate(rendered):
    powers = Path.home()/'.kiro'/'powers'
    installed = powers/'installed'/'kiroclops'
    if not installed.exists():
        raise SystemExit('No installed kiroclops power: add the rendered folder in Kiro as a custom power instead.')
    backup = installed.with_name(f"kiroclops-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copytree(installed, local_data()/'power-backups'/backup.name)
    server = json.loads((rendered/'mcp.json').read_text(encoding='utf-8'))['mcpServers']['kiroclops']
    (rendered/'mcp.json').unlink()  # the server is registered at user level instead
    register_user_server(server)
    shutil.rmtree(installed)
    shutil.copytree(rendered, installed)
    registry = powers/'registries'/'user-added.json'
    if registry.exists():
        value = json.loads(registry.read_text(encoding='utf-8'))
        manifest = json.loads((rendered/'plugin.json').read_text(encoding='utf-8'))
        icon = 'data:image/png;base64,' + base64.b64encode((rendered/'icon.png').read_bytes()).decode('ascii')
        for power in value.get('powers', []):
            if power.get('name') == 'kiroclops':
                # Kiro reads a custom power's tile (name, author, icon) from this registry entry.
                power.update(source={'type': 'local', 'path': str(rendered)}, description=manifest['description'],
                             displayName='KiroClops', author=manifest['author']['name'], iconUrl=icon,
                             repositoryUrl='https://github.com/tritonsan/kiroclops')
        registry.write_text(json.dumps(value, indent=2)+'\n', encoding='utf-8')
    return installed


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', type=Path, default=local_data()/'power'/'kiroclops', help='rendered power folder')
    parser.add_argument('--sessions', type=Path, default=local_data()/'sessions', help='where KiroClops keeps its sessions')
    parser.add_argument('--activate', action='store_true', help='replace the installed kiroclops custom power (backed up first)')
    args = parser.parse_args()
    try:
        import mcp  # noqa: F401
    except ImportError:
        raise SystemExit('This Python environment has no mcp package: pip install -e .[kiro]')
    args.sessions.mkdir(parents=True, exist_ok=True)
    rendered = render(args.output.resolve(), args.sessions.resolve())
    result = {'power': str(rendered), 'python': sys.executable, 'code': str(ROOT), 'sessions': str(args.sessions.resolve())}
    if args.activate:
        result['installed'] = str(activate(rendered))
    print(json.dumps(result, indent=1))
    print('Restart the Kiro IDE, open a copy of fixtures/phase3a-web/target or fixtures/phase3a-svg/target, '
          'and ask Kiro to use KiroClops to make it move like the reference.')


if __name__ == '__main__':
    main()
