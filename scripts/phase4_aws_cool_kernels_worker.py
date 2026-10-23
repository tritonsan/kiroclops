"""Per-kernel and pipeline-stage timing of one OpenCV engine on the COOL Graviton4 instance.

Phase 4 AWS addition after the V6 freeze: a post-hoc measurement that changes no recorded
result. ``phase4_aws_cool_kernels.py`` boots a bounded COOL worker; during boot, before the
worker service starts, this script runs once per engine and round, alternately with the COOL
build (the worker's own environment) and the standard ``opencv-python`` 5.0.0 wheel
(a second virtual environment on the same instance):

* kernels: the OpenCV calls the KiroClops measurement uses, on real frames of a V6 panel
  calibration recording, timed at 1 and 4 threads (median of 7 batches after warm-up);
* pipeline: wall time of ``measure_direct`` on three recordings, plus one cProfile run that
  splits the time between OpenCV calls, NumPy and the remaining Python code.

    python phase4_aws_cool_kernels_worker.py --engine cool --round 1 --input DIR --out FILE
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import platform
import pstats
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

BATCHES = 7
BATCH_MIN_S = .03
THREADS = (1, 4)
PIPELINE_RUNS = 3
BUILD_KEYS = ('Version control', 'Baseline', 'Dispatched code generation', 'CPU/HW features', 'Parallel framework',
              'KleidiCV', 'kleidicv', 'NEON', 'Build type', 'C++ Compiler', 'Lapack', 'Other third-party libraries')


def timed(fn):
    for _ in range(5):
        fn()
    calls = 1
    while True:
        started = time.perf_counter()
        for _ in range(calls):
            fn()
        if time.perf_counter()-started >= BATCH_MIN_S or calls >= 1 << 16:
            break
        calls *= 2
    samples = []
    for _ in range(BATCHES):
        started = time.perf_counter()
        for _ in range(calls):
            fn()
        samples.append((time.perf_counter()-started)/calls)
    return {'median_s': statistics.median(samples), 'min_s': min(samples), 'max_s': max(samples), 'calls_per_batch': calls}


def kernels(frames_dir):
    paths = sorted(frames_dir.glob('*.png'))
    buffers = [np.frombuffer(p.read_bytes(), np.uint8) for p in paths]
    colour = [cv2.imdecode(b, cv2.IMREAD_COLOR) for b in buffers]
    gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in colour]
    height, width = gray[0].shape
    cy, cx = height//2, width//2
    window = np.ascontiguousarray(colour[0][cy-100:cy+100, cx-100:cx+100])
    template = np.ascontiguousarray(colour[1][cy-24:cy+24, cx-24:cx+24])
    big_template = np.ascontiguousarray(colour[1][cy-32:cy+32, cx-32:cx+32])
    corners = cv2.goodFeaturesToTrack(gray[0], 100, .01, 5)
    if corners is None or len(corners) < 10:
        corners = np.array([[[x, y]] for x in range(40, width-40, max(1, (width-80)//10))
                            for y in range(40, height-40, max(1, (height-80)//10))], np.float32)[:100]
    edges = cv2.Canny(gray[0], 50, 150)
    _, mask = cv2.threshold(gray[0], 128, 255, cv2.THRESH_BINARY)
    side = min(512, height, width)
    crop_a = np.float32(gray[0][:side, :side])
    crop_b = np.float32(gray[1][:side, :side])
    return {
        'imdecode_png_frame': lambda: cv2.imdecode(buffers[0], cv2.IMREAD_COLOR),
        'cvtColor_bgr2gray_frame': lambda: cv2.cvtColor(colour[0], cv2.COLOR_BGR2GRAY),
        'matchTemplate_ccoeff_normed_200px_window_48px': lambda: cv2.matchTemplate(window, template, cv2.TM_CCOEFF_NORMED),
        'matchTemplate_ccoeff_normed_full_frame_64px': lambda: cv2.matchTemplate(colour[0], big_template, cv2.TM_CCOEFF_NORMED),
        'resize_cubic_x8_48px_patch': lambda: cv2.resize(template, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC),
        'goodFeaturesToTrack_frame': lambda: cv2.goodFeaturesToTrack(gray[0], 200, .01, 5),
        'calcOpticalFlowPyrLK_100_points': lambda: cv2.calcOpticalFlowPyrLK(gray[0], gray[1], corners, None, winSize=(21, 21), maxLevel=3),
        'canny_frame': lambda: cv2.Canny(gray[0], 50, 150),
        'findContours_edges': lambda: cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE),
        'connectedComponentsWithStats_mask': lambda: cv2.connectedComponentsWithStats(mask),
        'gaussianBlur_5x5_frame': lambda: cv2.GaussianBlur(colour[0], (5, 5), 0),
        'integral_frame': lambda: cv2.integral(gray[0]),
        'absdiff_frame': lambda: cv2.absdiff(colour[0], colour[1]),
        'phaseCorrelate_crop': lambda: cv2.phaseCorrelate(crop_a, crop_b),
    }, {'frame_shape': list(colour[0].shape), 'frames_loaded': len(paths)}


CV2_NAMES = set(dir(cv2))


def classify(name):
    # cProfile names OpenCV functions '<matchTemplate>' and OpenCV methods "<method 'x' of 'cv2.Y' objects>".
    inner = name[1:-1] if name.startswith('<') and name.endswith('>') else name
    if inner in CV2_NAMES or 'cv2.' in name:
        return 'opencv'
    if 'numpy' in name:
        return 'numpy'
    return 'python'


def pipeline(input_dir):
    from kiroclops.cloud.execution import measure_direct
    rows = []
    for item in json.loads((input_dir/'packages.json').read_text(encoding='utf-8')):
        package = input_dir/item['package']
        selection = json.loads((input_dir/item['selection']).read_text(encoding='utf-8'))
        contract = json.loads((input_dir/item['contract']).read_text(encoding='utf-8')) if item.get('contract') else None
        measure_direct(package, selection, contract)  # warm-up
        walls = []
        for _ in range(PIPELINE_RUNS):
            started = time.perf_counter()
            result = measure_direct(package, selection, contract)
            walls.append(time.perf_counter()-started)
        profiler = cProfile.Profile()
        profiler.enable()
        measure_direct(package, selection, contract)
        profiler.disable()
        stats = pstats.Stats(profiler)
        totals = {'opencv': 0., 'numpy': 0., 'python': 0.}
        functions = {}
        for (filename, line, function), (_, _, tottime, _, _) in stats.stats.items():
            label = function if filename == '~' else f'{Path(filename).name}:{function}'
            if filename != '~' and 'numpy' in filename:
                bucket = 'numpy'
            elif filename == '~':
                bucket = classify(function)
            else:
                bucket = 'python'
            totals[bucket] += tottime
            if bucket == 'opencv':
                functions[label] = functions.get(label, 0.)+tottime
        profiled = sum(totals.values())
        top = sorted(functions.items(), key=lambda kv: -kv[1])[:12]
        rows.append({'name': item['name'], 'frames': len(result['observations'].get('objects', [{}])[0].get('frames', [])),
                     'wall_s': walls, 'wall_median_s': statistics.median(walls),
                     'profiled_total_s': profiled, 'share': {k: v/profiled for k, v in totals.items()},
                     'top_opencv_tottime_s': top,
                     'measurement_sha256': hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()})
    return rows


def environment():
    info = cv2.getBuildInformation()
    lines = [line.strip() for line in info.splitlines() if any(key in line for key in BUILD_KEYS)]
    cpu = ''
    try:
        cpu = next((l.split(':', 1)[1].strip() for l in Path('/proc/cpuinfo').read_text().splitlines()
                    if l.lower().startswith(('model name', 'cpu part'))), '')
    except OSError:
        pass
    return {'opencv_version': cv2.__version__, 'cv2_file': cv2.__file__, 'numpy_version': np.__version__,
            'python': sys.version.split()[0], 'machine': platform.machine(), 'cpu': cpu,
            'use_optimized': cv2.useOptimized(), 'default_threads': cv2.getNumThreads(),
            'build_information_sha256': hashlib.sha256(info.encode()).hexdigest(), 'build_lines': lines}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--engine', required=True, choices=('cool', 'stock'))
    parser.add_argument('--round', type=int, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    env = environment()
    expected = {'cool': '/opt/cool', 'stock': '/stock/'}[args.engine]
    report = {'kind': 'CoolKernelBenchmarkRun', 'schema_version': 1, 'engine': args.engine, 'round': args.round,
              'environment': env, 'engine_check': expected in env['cv2_file'] or (args.engine == 'cool' and '5.1' in env['opencv_version'])}
    calls, frame_info = kernels(args.input/'frames')
    report['frames'] = frame_info
    report['kernels'] = {}
    for threads in THREADS:
        cv2.setNumThreads(threads)
        report['kernels'][str(threads)] = {name: timed(fn) for name, fn in calls.items()}
    cv2.setNumThreads(4)
    report['pipeline'] = pipeline(args.input)
    args.out.write_text(json.dumps(report, indent=1), encoding='utf-8')
    print(json.dumps({'engine': args.engine, 'round': args.round, 'opencv': env['opencv_version'],
                      'pipeline_s': [round(r['wall_median_s'], 3) for r in report['pipeline']]}), flush=True)


if __name__ == '__main__':
    main()
