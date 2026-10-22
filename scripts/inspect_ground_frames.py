"""Native-pixel rulers for independent ground-motion observations.

Reads PNG pixels only. Does not load tracker output or propose coordinates.
"""
from pathlib import Path
import argparse
import cv2
import numpy as np


def sheet(run, frames, crop, output, scale=10, columns=2):
    x0, y0, x1, y1 = crop
    panels = []
    for index in frames:
        image = cv2.imread(str(Path(run) / 'frames' / f'{index:06d}.png'))
        if image is None:
            raise ValueError(f'Missing frame {index}')
        pixels = cv2.resize(image[y0:y1, x0:x1], None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_NEAREST)
        panel = cv2.copyMakeBorder(pixels, 43, 0, 35, 0, cv2.BORDER_CONSTANT,
                                   value=(245, 245, 245))
        cv2.putText(panel, f'f{index:03d} native pixels; origin {x0},{y0}', (3, 16), 0, .4, (0,0,0), 1)
        for col in range(x1-x0):
            x = 35+col*scale
            cv2.line(panel, (x,43), (x,panel.shape[0]), (65,65,65), 1)
            if (x0+col) % 5 == 0:
                cv2.putText(panel, str(x0+col), (x,36), 0, .28, (0,0,0), 1)
        for row in range(y1-y0):
            y = 43+row*scale
            cv2.line(panel, (35,y), (panel.shape[1],y), (65,65,65), 1)
            if scale >= 10 or (y0+row) % 2 == 0:
                cv2.putText(panel, str(y0+row), (0,y+min(9,scale)), 0, .28, (0,0,0), 1)
        panels.append(panel)
    while len(panels) % columns:
        panels.append(np.full_like(panels[0],245))
    result=cv2.vconcat([cv2.hconcat(panels[i:i+columns]) for i in range(0,len(panels),columns)])
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    if not cv2.imwrite(str(output),result):
        raise OSError('Cannot write observation sheet')


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('run',type=Path)
    p.add_argument('--frames',nargs='+',type=int,required=True)
    p.add_argument('--crop',nargs=4,type=int,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--scale',type=int,default=10)
    p.add_argument('--columns',type=int,default=2)
    a=p.parse_args()
    sheet(a.run,a.frames,a.crop,a.output,a.scale,a.columns)
