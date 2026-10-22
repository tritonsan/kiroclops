"""Render the submission diagrams to PNG and PDF, refusing any layout problem.

Each HTML page declares its boxes and edges; ``diagram.js`` draws the edges and
measures the result: every glyph inside its box and the canvas, no text under
16 px, no overlapping boxes, badges or labels, no edge through an unrelated
box, no touching edges, group labels clear of their contents. A page with any
problem is written only as ``<name>.draft.png`` with ``<name>.audit.json`` and
the script exits non-zero.

    .venv-local\\Scripts\\python.exe submission\\diagrams\\build_diagrams.py [architecture agent-loop]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGES = ('architecture', 'agent-loop')


def build(page, name):
    html = HERE/f'{name}.html'
    page.goto(html.as_uri())
    page.wait_for_function('document.fonts && document.fonts.status === "loaded"')
    page.evaluate('() => Promise.all(Array.from(document.images).map(i => i.complete ? 1 : new Promise(r => { i.onload = r; i.onerror = r; })))')
    width, height = page.evaluate('() => [document.getElementById("canvas").clientWidth, document.getElementById("canvas").clientHeight]')
    page.set_viewport_size({'width': width, 'height': height})
    page.evaluate('() => window.renderEdges()')
    broken = page.evaluate('() => Array.from(document.images).filter(i => !i.naturalWidth).map(i => i.getAttribute("src"))')
    problems = page.evaluate('() => window.audit()')
    problems += [{'kind': 'image_missing', 'src': src} for src in broken]
    (HERE/f'{name}.audit.json').write_text(json.dumps({'page': name, 'size': [width, height], 'problems': problems}, indent=2), encoding='utf-8')
    canvas = page.locator('#canvas')
    if problems:
        canvas.screenshot(path=str(HERE/f'{name}.draft.png'))
        return problems
    draft = HERE/f'{name}.draft.png'
    if draft.exists():
        draft.unlink()
    canvas.screenshot(path=str(HERE/f'{name}.png'))
    page.emulate_media(media='screen')
    page.pdf(path=str(HERE/f'{name}.pdf'), width=f'{width}px', height=f'{height}px', print_background=True,
             margin={'top': '0', 'right': '0', 'bottom': '0', 'left': '0'}, page_ranges='1')
    return problems


def main(argv=None):
    names = (argv or sys.argv[1:]) or list(PAGES)
    failed = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 900}, device_scale_factor=2)
        for name in names:
            problems = build(page, name)
            status = 'ok' if not problems else f'{len(problems)} problem(s)'
            print(f'{name}: {status}')
            for problem in problems[:40]:
                print('   ', json.dumps(problem))
            failed += bool(problems)
        browser.close()
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
