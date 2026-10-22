"""Read-only timing correlation, without geometry or a causal verdict."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import read_json, write_json, file_sha256


def main():
    base = ROOT/'artifacts/phase3a/completion-paired-cohort-01/sessions/svg-baseline-03/svg'
    record = base/'measurements/b6608339cb61494794aa9b386ce29db6/before/three-to-five-w1280-r1'
    files = [record/'capture-record.json', record/'private/frame-receipts.json',
             record/'private/visual-clock.json', record/'private/progress-runtime.json',
             record/'private/received-evidence.json',
             base/'jobs/b6608339cb61494794aa9b386ce29db6/job.json']
    capture, frames, clock, progress, received, job = map(read_json, files)
    stamps = capture['timestamps']
    index = max(range(1, len(stamps)), key=lambda i: stamps[i]-stamps[i-1])
    # Use timestamps, visual-clock counters and transport receipts only.
    # Application geometry from private functional checks is not read.
    around = []
    for i in range(max(0, index-2), min(len(stamps), index+2)):
        frame, batch = frames['frames'][i], clock['associations'][i]
        around.append({'index': i, 'timestamp_s': stamps[i], 'counter': batch['counter'],
            'cdp_epoch_s': frame['epoch_s'], 'received_epoch_s': frame['receive_epoch_s'],
            'delivery_lag_s': frame['receive_epoch_s']-frame['epoch_s'],
            'rendering_batch_interval_epoch_s': batch['rendering_batch_interval_epoch_s']})
    left, right = frames['frames'][index-1]['epoch_s'], frames['frames'][index]['epoch_s']
    overlaps = [r for r in progress['callback_records']
        if r['callback_start_epoch_s'] <= right and r['callback_end_epoch_s'] >= left]
    out = ROOT/'artifacts/phase3a/completion-20260927/svg-baseline-03-transport-correlation.json'
    assert not out.exists()
    value = {'kind': 'ReadOnlyTransportCorrelation', 'trial': 'svg-baseline-03',
        'gap_index': index, 'gap_s': stamps[index]-stamps[index-1], 'nearby_frames': around,
        'overlapping_progress_callbacks': overlaps, 'delivery_backpressure': received['delivery_backpressure'],
        'job_json_bytes': files[-1].stat().st_size, 'job_status': job['status'],
        'progress_implementation': 'Single background thread; durable job update uses lock/read/fsync/replace.',
        'observed': 'Delayed delivery and skipped visible RAF counters overlap a durable progress callback.',
        'causality_established': False, 'interpretation':
            'Overlap cannot distinguish host scheduling, transport/backpressure or filesystem effects. '
            'The job JSON is small; large JSON serialization is not supported by this evidence. '
            'Direct fixture probes have no durable session progress callback and cannot by themselves rule it out.',
        'private_geometry_used': False, 'thresholds_changed': False, 'primary_outcome_modified': False,
        'evidence_hashes': {str(p.relative_to(ROOT)): file_sha256(p) for p in files},
        'producer_sha256': file_sha256(__file__)}
    write_json(out, value)
    print({'gap_ms': value['gap_s']*1000, 'overlapping_callbacks': len(overlaps),
           'causality_established': False})


if __name__ == '__main__':
    main()
