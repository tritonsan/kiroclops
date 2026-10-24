"""Scaling benchmark of one OpenCV engine on one instance: kernels by image size and thread count,
the measurement pipeline by thread count, and the throughput of the whole instance.

Phase 4 AWS addition after the V6 freeze: a post-hoc measurement that changes no recorded
result. ``phase4_aws_cool_scaling.py`` boots a bounded worker of a chosen instance type;
during boot, before the worker service starts, this script runs once per engine and round:
COOL from the worker's own environment and the standard ``opencv-python`` 5.0.0 wheel from a
second virtual environment on the COOL image, or the standard wheel alone on a standard
Ubuntu worker (Arm or x86).

* kernels: the OpenCV calls of ``phase4_aws_cool_kernels_worker.py`` on real 960 x 640 frames
  of a V6 panel calibration recording, and the frame-size dependent ones on 1920 x 1080 and
  3840 x 2160 frames tiled from the same recording, each at several thread counts;
* pipeline: wall time of ``measure_direct`` on three recordings at several thread counts;
* throughput: one process per vCPU with one OpenCV thread each, all measuring the three
  recordings in turn from a common start; analyses per hour of the whole instance.

    python phase4_aws_cool_scaling_worker.py --engine cool --round 1 --input DIR --out FILE
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

BATCHES = 7
BATCH_MIN_S = .03
WARMUP_S = .5
PIPELINE_RUNS = 2
THROUGHPUT_PASSES = 2
THROUGHPUT_DELAY_S = 45
SIZES = {'1080p': (1080, 1920), '2160p': (2160, 3840)}
BUILD_KEYS = ('Version control', 'Baseline', 'Dispatched code generation', 'CPU/HW features', 'Parallel framework',
              'KleidiCV', 'kleidicv', 'NEON', 'Build type', 'C++ Compiler', 'Lapack', 'Other third-party libraries')
LSCPU_KEYS = ('Architecture', 'Model name', 'Vendor ID', 'Thread(s) per core', 'Core(s) per socket', 'Socket(s)',
              'CPU(s)', 'L1d', 'L2', 'L3', 'NUMA node(s)', 'CPU max MHz', 'BogoMIPS')
CPU_FEATURES = ('sve', 'sve2', 'i8mm', 'bf16', 'asimddp', 'avx2', 'avx512f', 'avx512bw', 'avx512_vnni', 'avx512_bf16',
                'amx_tile', 'amx_bf16')


def timed(fn):
    started = time.perf_counter()
    for _ in range(5):
        fn()
        if time.perf_counter()-started >= WARMUP_S:
            break
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


def kernel_set(buffer, colour, window_kernels):
    """OpenCV calls of the tracker on one decoded frame pair; ``buffer`` is the PNG of the first frame."""
    gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in colour]
    height, width = gray[0].shape
    cy, cx = height//2, width//2
    big_template = np.ascontiguousarray(colour[1][cy-32:cy+32, cx-32:cx+32])
    corners = cv2.goodFeaturesToTrack(gray[0], 100, .01, 5)
    if corners is None or len(corners) < 10:
        corners = np.array([[[x, y]] for x in range(40, width-40, max(1, (width-80)//10))
                            for y in range(40, height-40, max(1, (height-80)//10))], np.float32)[:100]
    edges = cv2.Canny(gray[0], 50, 150)
    _, mask = cv2.threshold(gray[0], 128, 255, cv2.THRESH_BINARY)
    side = 1 << (min(height, width, 2048).bit_length()-1)  # 512, 1024 and 2048 for the three sizes
    crop_a = np.float32(gray[0][:side, :side])
    crop_b = np.float32(gray[1][:side, :side])
    calls = {
        'imdecode_png_frame': lambda: cv2.imdecode(buffer, cv2.IMREAD_COLOR),
        'cvtColor_bgr2gray_frame': lambda: cv2.cvtColor(colour[0], cv2.COLOR_BGR2GRAY),
        'matchTemplate_ccoeff_normed_full_frame_64px': lambda: cv2.matchTemplate(colour[0], big_template, cv2.TM_CCOEFF_NORMED),
        'goodFeaturesToTrack_frame': lambda: cv2.goodFeaturesToTrack(gray[0], 200, .01, 5),
        'calcOpticalFlowPyrLK_100_points': lambda: cv2.calcOpticalFlowPyrLK(gray[0], gray[1], corners, None, winSize=(21, 21), maxLevel=3),
        'canny_frame': lambda: cv2.Canny(gray[0], 50, 150),
        'findContours_edges': lambda: cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE),
        'connectedComponentsWithStats_mask': lambda: cv2.connectedComponentsWithStats(mask),
        'gaussianBlur_5x5_frame': lambda: cv2.GaussianBlur(colour[0], (5, 5), 0),
        'integral_frame': lambda: cv2.integral(gray[0]),
        'absdiff_frame': lambda: cv2.absdiff(colour[0], colour[1]),
        'phaseCorrelate_crop': lambda: cv2.phaseCorrelate(crop_a, crop_b),
    }
    if window_kernels:
        window = np.ascontiguousarray(colour[0][cy-100:cy+100, cx-100:cx+100])
        template = np.ascontiguousarray(colour[1][cy-24:cy+24, cx-24:cx+24])
        calls['matchTemplate_ccoeff_normed_200px_window_48px'] = lambda: cv2.matchTemplate(window, template, cv2.TM_CCOEFF_NORMED)
        calls['resize_cubic_x8_48px_patch'] = lambda: cv2.resize(template, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC)
    return calls, {'shape': list(colour[0].shape), 'phase_correlate_side': side, 'png_bytes': int(buffer.size)}


def frame_sets(input_dir):
    """Native frames as recorded, and the tiled 1080p and 2160p frames staged with the input."""
    sets = {}
    paths = sorted((input_dir/'frames').glob('*.png'))[:2]
    for name, files in [('native', paths)]+[(size, [input_dir/'tiles'/f'{size}-{i}.png' for i in (0, 1)]) for size in SIZES]:
        buffers = [np.frombuffer(p.read_bytes(), np.uint8) for p in files]
        sets[name] = (buffers[0], [cv2.imdecode(b, cv2.IMREAD_COLOR) for b in buffers])
    return sets


def recordings(input_dir):
    items = []
    for item in json.loads((input_dir/'packages.json').read_text(encoding='utf-8')):
        selection = json.loads((input_dir/item['selection']).read_text(encoding='utf-8'))
        contract = json.loads((input_dir/item['contract']).read_text(encoding='utf-8')) if item.get('contract') else None
        items.append((item['name'], input_dir/item['package'], selection, contract))
    return items


def digest(result):
    return hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()


def pipeline(input_dir, thread_counts):
    from kiroclops.cloud.execution import measure_direct
    items = recordings(input_dir)
    for _, package, selection, contract in items:
        measure_direct(package, selection, contract)  # warm-up
    rows = []
    for threads in thread_counts:
        cv2.setNumThreads(threads)
        for name, package, selection, contract in items:
            walls, hashes = [], set()
            for _ in range(PIPELINE_RUNS):
                started = time.perf_counter()
                result = measure_direct(package, selection, contract)
                walls.append(time.perf_counter()-started)
                hashes.add(digest(result))
            rows.append({'threads': threads, 'name': name, 'wall_s': walls, 'wall_median_s': statistics.median(walls),
                         'measurement_sha256': sorted(hashes)})
    return rows


def child(input_dir, passes, start_at, offset):
    """One throughput process: one OpenCV thread, the three recordings in turn from a common start."""
    from kiroclops.cloud.execution import measure_direct
    cv2.setNumThreads(1)
    items = recordings(input_dir)
    for _, package, selection, contract in items:
        measure_direct(package, selection, contract)  # imports and first-call costs before the start
    while time.time() < start_at:
        time.sleep(min(.05, max(0., start_at-time.time())))
    started = time.time()
    order = items[offset % len(items):]+items[:offset % len(items)]
    walls = []
    for _ in range(passes):
        for name, package, selection, contract in order:
            began = time.perf_counter()
            measure_direct(package, selection, contract)
            walls.append([name, time.perf_counter()-began])
    print(json.dumps({'started': started, 'finished': time.time(), 'walls': walls}), flush=True)


def throughput(input_dir, processes):
    start_at = time.time()+THROUGHPUT_DELAY_S
    command = [sys.executable, str(Path(__file__).resolve()), '--child', '--input', str(input_dir),
               '--passes', str(THROUGHPUT_PASSES), '--start-at', repr(start_at)]
    children = [subprocess.Popen(command+['--offset', str(i)], stdout=subprocess.PIPE, text=True) for i in range(processes)]
    outputs = []
    for process in children:
        stdout, _ = process.communicate(timeout=1500)
        outputs.append(json.loads(stdout.strip().splitlines()[-1]))
    first = min(o['started'] for o in outputs)
    last = max(o['finished'] for o in outputs)
    analyses = sum(len(o['walls']) for o in outputs)
    per_recording = {}
    for o in outputs:
        for name, wall in o['walls']:
            per_recording.setdefault(name, []).append(wall)
    return {'processes': processes, 'threads_per_process': 1, 'passes': THROUGHPUT_PASSES, 'analyses': analyses,
            'wall_s': last-first, 'analyses_per_hour': analyses/(last-first)*3600,
            'latest_start_after_signal_s': max(o['started'] for o in outputs)-start_at,
            'per_recording_median_s': {k: statistics.median(v) for k, v in per_recording.items()}}


def instance_metadata():
    import urllib.request
    base = 'http://169.254.169.254/latest/'
    try:
        token = urllib.request.urlopen(urllib.request.Request(base+'api/token', method='PUT', headers={
            'X-aws-ec2-metadata-token-ttl-seconds': '60'}), timeout=2).read().decode()
        get = lambda path: urllib.request.urlopen(urllib.request.Request(base+'meta-data/'+path, headers={
            'X-aws-ec2-metadata-token': token}), timeout=2).read().decode()
        return {'instance_type': get('instance-type'), 'availability_zone': get('placement/availability-zone'), 'ami_id': get('ami-id')}
    except OSError as exc:
        return {'unavailable': type(exc).__name__}


def environment():
    info = cv2.getBuildInformation()
    lines = [line.strip() for line in info.splitlines() if any(key in line for key in BUILD_KEYS)]
    lscpu, features = {}, []
    try:
        for line in subprocess.run(['lscpu'], capture_output=True, text=True, timeout=30).stdout.splitlines():
            key, _, value = line.partition(':')
            if key.strip() in LSCPU_KEYS or key.strip().startswith(('L1d', 'L2', 'L3')):
                lscpu[key.strip()] = value.strip()
        flags = next((l.split(':', 1)[1].split() for l in Path('/proc/cpuinfo').read_text().splitlines()
                      if l.lower().startswith(('flags', 'features'))), [])
        features = [f for f in CPU_FEATURES if f in flags]
    except (OSError, subprocess.SubprocessError):
        pass
    return {'opencv_version': cv2.__version__, 'cv2_file': cv2.__file__, 'numpy_version': np.__version__,
            'python': sys.version.split()[0], 'machine': platform.machine(), 'cpu_count': os.cpu_count(),
            'lscpu': lscpu, 'cpu_features': features, 'use_optimized': cv2.useOptimized(), 'default_threads': cv2.getNumThreads(),
            'build_information_sha256': hashlib.sha256(info.encode()).hexdigest(), 'build_lines': lines,
            'instance': instance_metadata()}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--engine', choices=('cool', 'stock'))
    parser.add_argument('--round', type=int)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--passes', type=int, default=THROUGHPUT_PASSES, help=argparse.SUPPRESS)
    parser.add_argument('--start-at', type=float, help=argparse.SUPPRESS)
    parser.add_argument('--offset', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--cpus', type=int, help=argparse.SUPPRESS)  # smaller sweep for a local smoke test
    args = parser.parse_args()
    if args.child:
        child(args.input, args.passes, args.start_at, args.offset)
        return
    env = environment()
    cool_build = '/opt/cool' in env['cv2_file']
    cpus = args.cpus or os.cpu_count() or 1
    sweep = [t for t in (1, 2, 4, 8, 16, 32, 64) if t <= cpus]
    short = sorted({1, min(4, cpus), cpus})
    report = {'kind': 'CoolScalingRun', 'schema_version': 1, 'engine': args.engine, 'round': args.round, 'environment': env,
              'engine_check': cool_build if args.engine == 'cool' else (not cool_build and env['opencv_version'].startswith('5.0.0')),
              'thread_counts': {'native_kernels': sweep, 'tiled_kernels': short, 'pipeline': short}}
    report['frames'], report['kernels'] = {}, {}
    for name, (buffer, colour) in frame_sets(args.input).items():
        calls, info = kernel_set(buffer, colour, window_kernels=name == 'native')
        report['frames'][name] = info
        report['kernels'][name] = {}
        for threads in (sweep if name == 'native' else short):
            cv2.setNumThreads(threads)
            report['kernels'][name][str(threads)] = {kernel: timed(fn) for kernel, fn in calls.items()}
    report['pipeline'] = pipeline(args.input, short)
    report['throughput'] = throughput(args.input, cpus)
    args.out.write_text(json.dumps(report, indent=1), encoding='utf-8')
    print(json.dumps({'engine': args.engine, 'round': args.round, 'opencv': env['opencv_version'], 'cpus': cpus,
                      'analyses_per_hour': round(report['throughput']['analyses_per_hour'])}), flush=True)


if __name__ == '__main__':
    main()
