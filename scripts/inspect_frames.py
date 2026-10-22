"""Pixel-coordinate contact sheets for independent visual annotation.

This tool only displays raw pixels and coordinate rulers. It does not read
analysis outputs, run tracking, or create ground-truth coordinates.
"""
import argparse
from pathlib import Path
import sys

import cv2
import numpy as np


def contact_sheet(run, frames, crop, output, scale=8, columns=5):
    run = Path(run)
    x1, y1, x2, y2 = crop
    panels = []
    for index in frames:
        image = cv2.imread(str(run / "frames" / f"{index:06d}.png"))
        if image is None:
            raise ValueError(f"Missing frame {index}")
        region = cv2.resize(image[y1:y2, x1:x2], None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        panel = cv2.copyMakeBorder(region, 30, 0, 38, 0, cv2.BORDER_CONSTANT, value=(238,238,238))
        cv2.putText(panel, f"f{index:03d}", (2,21), cv2.FONT_HERSHEY_SIMPLEX, .5, (0,0,0), 1)
        for row in range(y2-y1):
            yy = 30+row*scale
            if row % max(1, 8//scale) == 0:
                cv2.putText(panel, str(y1+row), (1, yy+min(8,scale)), cv2.FONT_HERSHEY_SIMPLEX,.26,(0,0,0),1)
            cv2.line(panel, (38, yy), (panel.shape[1], yy), (60,60,60), 1)
        for col in range(x2-x1):
            xx = 38+col*scale
            cv2.line(panel, (xx,30), (xx,panel.shape[0]), (60,60,60), 1)
        panels.append(panel)
    while len(panels) % columns:
        panels.append(np.full_like(panels[0],238))
    result = cv2.vconcat([cv2.hconcat(panels[i:i+columns]) for i in range(0,len(panels),columns)])
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    if not cv2.imwrite(str(output),result):
        raise OSError("Cannot write contact sheet")


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('run', type=Path)
    p.add_argument('--frames', nargs='+', type=int, required=True)
    p.add_argument('--crop', nargs=4, type=int, required=True)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--scale', default=8, type=int)
    p.add_argument('--columns', default=5, type=int)
    a=p.parse_args()
    contact_sheet(a.run,a.frames,a.crop,a.output,a.scale,a.columns)
