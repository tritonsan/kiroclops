"""Bind new sessions to the complete, recorded and qualified capture policy.

SVG admission: svg-visible-clock-review-01, 16 recordings, separately checked
raw positions, events, lifecycles and geometry. This does not admit an untagged
SVG transport clock or alter an existing legacy recording.
"""
from kiroclops.visual.schema import VisualError
from kiroclops.web_local.specs import SVG_CAPTURE_POLICY

LEGACY_DRIVER='controlled_begin_frame_v1'
PANEL_DRIVER='natural_browser_raf_v1'


def reference_capture_policy(domain,manifest):
    metadata=manifest.get('metadata',{})
    scenario=metadata.get('scenario',{}) if isinstance(metadata,dict) else {}
    if not isinstance(scenario,dict):raise VisualError('invalid_capture_metadata','Reference capture scenario must be an object')
    driver=scenario.get('frame_driver',LEGACY_DRIVER)
    extras=any(scenario.get(k) is not None for k in ('analysis_policy','visual_frame_clock'))
    frames=manifest.get('frames',[])
    tagged=any(f.get('timestamp_source')=='svg_visible_raf_batch' for f in frames)
    if domain=='svg' and all(scenario.get(k)==v for k,v in SVG_CAPTURE_POLICY.items()):
        if frames and all(f.get('timestamp_source')=='svg_visible_raf_batch' for f in frames):
            return dict(SVG_CAPTURE_POLICY)
        raise VisualError('unqualified_capture_mode','SVG reference frames must carry the qualified visible clock')
    if not extras and not tagged:
        if driver==LEGACY_DRIVER:return {'frame_driver':driver}
        if driver==PANEL_DRIVER and domain=='web_panel':return {'frame_driver':driver}
    raise VisualError('unqualified_capture_mode','Reference recorder mode is not qualified for this application domain')


def reference_capture_driver(domain,manifest):
    return reference_capture_policy(domain,manifest)['frame_driver']
