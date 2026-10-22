"""Fetch the exact public Heart tree through GitHub's raw endpoint; never float refs."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
from urllib.request import urlopen, Request

ROOT = Path(__file__).resolve().parents[1]
COMMIT = 'cfcc75db398bf429eece175b4181ab468e716d2c'
REPO = 'https://github.com/uheartbeast/Heart-Platformer-Godot-4'
FILES = '''.gitattributes .gitignore DefaultMovementData.tres Events.gd FasterMovementData.tres LICENSE
PlayerMovementData.gd SlowMovementData.tres black_9slice.png black_9slice.png.import black_9slice.tres
default_theme.tres hazard_area.tscn heart.gd heart.png heart.png.import heart.tscn icon.svg icon.svg.import
level_completed.gd level_completed.tscn level_one.tscn level_tile_map.tscn level_transition.gd
level_transition.tscn level_two.tscn pixel_font.ttf pixel_font.ttf.import player.gd player.png
player.png.import player.tscn project.godot slopes.png slopes.png.import slopes_flipped.png
slopes_flipped.png.import spikes.png spikes.png.import spikes.tscn start_menu.gd start_menu.tscn
tileset.png tileset.png.import victory_screen.gd victory_screen.tscn white_9slice.png
white_9slice.png.import white_9slice.tres world.gd world.tscn'''.split()


def main():
    destination = ROOT / 'vendor/heart'
    lock = ROOT / 'config/phase2c/heart-source.json'
    if lock.exists():
        record = json.loads(lock.read_text(encoding='utf-8'))
        assert record['commit'] == COMMIT
        assert all(hashlib.sha256((destination/name).read_bytes()).hexdigest() == value
                   for name, value in record['files'].items()), 'Pinned Heart tree differs'
        print(json.dumps({'status': 'verified', 'commit': COMMIT, 'files': len(record['files'])}))
        return
    destination.mkdir(parents=True, exist_ok=True)

    def fetch(name):
        url = f'https://raw.githubusercontent.com/uheartbeast/Heart-Platformer-Godot-4/{COMMIT}/{name}'
        with urlopen(Request(url, headers={'User-Agent': 'KiroClops-source-provisioner'}), timeout=25) as response:
            data = response.read()
        if not data:
            raise ValueError(f'Empty source: {name}')
        path = destination / name
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError(f'Existing source differs: {name}')
        else:
            path.write_bytes(data)
        return name, hashlib.sha256(data).hexdigest()

    with ThreadPoolExecutor(max_workers=6) as pool:
        hashes = dict(pool.map(fetch, FILES))
    # The official root tree is flat. Imported/generated runtime cache paths
    # are deliberately absent; raw source sidecars remain exactly upstream.
    record = {'schema_version': 1, 'target_id': 'heart', 'url': REPO,
              'commit': COMMIT, 'commit_evidence_url': REPO + '/commit/' + COMMIT + '.patch',
              'tree_url': REPO + '/tree/' + COMMIT, 'acquisition': 'pinned GitHub raw tree',
              'files': dict(sorted(hashes.items())), 'license': 'MIT',
              'license_sha256': hashes['LICENSE'], 'engine_required': '4.5.1.stable',
              'project_declared_version': '4.0', 'native_viewport': [320, 180]}
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'status': 'pinned', 'commit': COMMIT, 'files': len(hashes)}))


if __name__ == '__main__':
    main()
