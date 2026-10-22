"""Stage an exact-frame comparison repair without touching frozen production."""
from pathlib import Path
import difflib
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiroclops.visual.schema import file_sha256, write_json


HELPER = '''def _jump_frame_spans(report):
    """Use observed event indices on the verified60Hz lattice, not rounded UTC."""
    events, frames = report.get('events', {}), report.get('frames', [])
    values = [events.get(key) for key in ('takeoff_frame', 'apex_frame', 'landing_frame')]
    if (report.get('fps') != 60 or len(frames) != 240 or
            any(type(v) is not int for v in values) or
            not 0 <= values[0] < values[1] < values[2] < len(frames)):
        raise IntegrationError('tracking_failure', 'Jump duration lacks ordered observed frame events')
    times = []
    for index in values:
        frame = frames[index]
        if (frame.get('frame_index') != index or frame.get('valid') is not True or
                type(frame.get('time_s')) not in (int, float) or not math.isfinite(frame['time_s']) or
                abs(frame['time_s'] - index/60) > 1e-5):
            raise IntegrationError('capture_timing_failure', 'Jump event frame is not on the verified60Hz lattice')
        times.append(frame['time_s'])
    spans = (values[1]-values[0], values[2]-values[1])
    for field, duration, left, right in zip(('rise_s', 'fall_s'), spans, times, times[1:]):
        metric = report.get('metrics', {}).get(field)
        if (type(metric) not in (int, float) or not math.isfinite(metric) or
                abs(metric-(right-left)) > 1e-9 or abs(metric-duration/60) > 2e-5):
            raise IntegrationError('tracking_failure', 'Jump duration disagrees with its observed frame events')
    return spans


'''


def main():
    source = ROOT/'kiroclops/portable_v2/measurement.py'
    output = ROOT/'artifacts/phase3a/jump-frame-timing-compatibility-01'
    assert not output.exists()
    original = source.read_text('utf-8')
    text = original.replace('def _jump_row(reference, before, after, require_improvement):',
        HELPER+'def _jump_row(reference, before, after, require_improvement):',1)
    old = "    rise, fall = abs(nm['rise_s']-rm['rise_s']), abs(nm['fall_s']-rm['fall_s'])"
    new = "    reference_spans, candidate_spans = _jump_frame_spans(reference), _jump_frame_spans(after)\n    rise_ticks, fall_ticks = [abs(a-b) for a,b in zip(reference_spans, candidate_spans)]\n    rise, fall = rise_ticks/60, fall_ticks/60"
    assert text.count(old)==1
    text=text.replace(old,new,1)
    old="'height': height <= .1, 'rise': rise <= max(2/60, .1*rm['rise_s']),\n             'fall': fall <= max(2/60, .1*rm['fall_s'])"
    new="'height': height <= .1, 'rise': 10*rise_ticks <= max(20, reference_spans[0]),\n             'fall': 10*fall_ticks <= max(20, reference_spans[1])"
    assert text.count(old)==1
    text=text.replace(old,new,1)
    output.mkdir(parents=True)
    proposed=output/'measurement-proposed.py';proposed.write_text(text,'utf-8')
    (output/'measurement.patch').write_text(''.join(difflib.unified_diff(original.splitlines(True),text.splitlines(True),
        fromfile='kiroclops/portable_v2/measurement.py',tofile='kiroclops/portable_v2/measurement.py')),'utf-8')
    write_json(output/'proposal.json',{'status':'staged_tests_pending','original_source_sha256':file_sha256(source),
        'proposed_source_sha256':file_sha256(proposed),'builder_sha256':file_sha256(__file__),
        'production_modified':False,'primary_outcomes_modified':False,
        'scope':'Exactly2 frames or10%of reference frame span. No tolerance relaxation. Rounded timestamps retain their original evidence; malformed or contradictory event/metric bindings fail closed.'})
    print({'status':'staged','production_modified':False})


if __name__=='__main__':
    main()
