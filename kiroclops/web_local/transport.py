"""Validate bounded CDP PNG delivery permutations without changing observations.

Chromium153 creates observation metadata before independent PNG encoding jobs.
Their replies can arrive in a different order. The observation timestamps are
not frame-presentation timestamps; transport validation cannot measure that lag.
"""
from __future__ import annotations

import base64
import hashlib
import math

POLICY_VERSION = 'chromium153-png-delivery-v1'
SUPPORTED_BROWSER_VERSION = '153.0.8010.12'
SOURCE_PROOF = ('https://chromium.googlesource.com/chromium/src/+/refs/tags/'
                '153.0.8010.12/content/browser/devtools/protocol/page_handler.cc')
MAX_REORDER_S = .120
MAX_HOST_DELIVERY_LAG_S = 1.0
SOURCE_CLOCK_SEMANTICS = {
    'policy_version': POLICY_VERSION,
    'browser_observation': 'Browser consumer metadata creation time before PNG encoding; original CDP metadata.timestamp',
    'observation_timestamp_interval': 'Zero-width identifies the supplied browser observation timestamp, not pixel presentation accuracy',
    'pixel_presentation_time_uncertainty_s': None,
    'pixel_presentation_time_accuracy_claimed': False,
    'capture_to_metadata_latency_measured': False,
    'begin_frame': 'Actual host UTC request/response bracket; midpoint and half-width remain explicit',
    'source_proof': SOURCE_PROOF,
    'verified_browser_version': SUPPORTED_BROWSER_VERSION,
}


class TransportOrderError(RuntimeError):
    code = 'capture_timing_failure'


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _png_sha(frame):
    try:
        return hashlib.sha256(base64.b64decode(frame['data'], validate=True)).hexdigest()
    except (KeyError, TypeError, ValueError) as exc:
        raise TransportOrderError('Transport receipt has missing or invalid original PNG bytes') from exc


def normalize_compositor_delivery(frames, *, segment_start_s, segment_end_s,
                                  browser_version, delivery_end_s=None):
    """Return all original frame objects plus an auditable raw-index permutation.

    Only the CDP compositor-source subsequence can be permuted. BeginFrame and
    other host-bracket source order remains strict. No duplicate is discarded,
    no timestamp is rewritten, and no image content determines ordering.
    """
    if browser_version != SUPPORTED_BROWSER_VERSION:
        raise TransportOrderError('Unverified Chromium build for PNG delivery-order policy')
    if not _number(segment_start_s) or not _number(segment_end_s) or segment_end_s <= segment_start_s:
        raise TransportOrderError('Invalid actual capture segment bounds')
    delivery_end_s = segment_end_s if delivery_end_s is None else delivery_end_s
    if not _number(delivery_end_s) or delivery_end_s < segment_end_s:
        raise TransportOrderError('Invalid actual transport delivery bounds')
    for source in ('cdp_begin_frame_bracket', 'cdp_screenshot_bracket'):
        previous = None
        for frame in frames:
            if frame.get('timestamp_source') != source:
                continue
            stamp = frame.get('epoch_s');interval = frame.get('interval_epoch_s')
            if (not _number(stamp) or not isinstance(interval, list) or len(interval) != 2
                    or not all(_number(x) for x in interval) or not interval[0] <= stamp <= interval[1]
                    or (previous is not None and interval[0] <= previous)):
                raise TransportOrderError('Host-bracket source order cannot be normalized')
            previous = interval[1]

    entries = [];seen = set();last_index = None;last_receive = None
    latest_stamp = None;maximum_inversion = 0.0
    for raw_index, frame in enumerate(frames):
        source = frame.get('timestamp_source')
        if source not in ('cdp_compositor', 'cdp_begin_frame_bracket', 'cdp_screenshot_bracket'):
            raise TransportOrderError('Unknown frame transport source')
        if source != 'cdp_compositor':
            continue
        stamp = frame.get('epoch_s');receive = frame.get('receive_epoch_s')
        receive_index = frame.get('receive_index');metadata = frame.get('metadata')
        if (not _number(stamp) or not _number(receive) or not isinstance(metadata, dict)
                or not _number(metadata.get('timestamp')) or metadata['timestamp'] != stamp
                or frame.get('interval_epoch_s') != [stamp, stamp]):
            raise TransportOrderError('Browser observation timestamp does not match original CDP metadata')
        if (type(receive_index) is not int or receive_index < 0
                or (last_index is not None and receive_index <= last_index)
                or (last_receive is not None and receive < last_receive)):
            raise TransportOrderError('Actual CDP receipt index/time order is invalid')
        if not segment_start_s <= stamp <= segment_end_s or not stamp <= receive <= delivery_end_s:
            raise TransportOrderError('Browser observation lies outside its observation/delivery segment or after its receipt')
        if receive - stamp > MAX_HOST_DELIVERY_LAG_S:
            raise TransportOrderError('Browser observation delivery exceeds existing one-second host-lag bound')
        if stamp in seen:
            raise TransportOrderError('Equal browser observation timestamps are ambiguous and cannot be deduplicated')
        inversion = max(0.0, latest_stamp - stamp) if latest_stamp is not None else 0.0
        if inversion > MAX_REORDER_S:
            raise TransportOrderError('CDP PNG delivery permutation exceeds120ms')
        maximum_inversion = max(maximum_inversion, inversion)
        latest_stamp = max(latest_stamp, stamp) if latest_stamp is not None else stamp
        seen.add(stamp);last_index = receive_index;last_receive = receive
        entries.append({'raw_index':raw_index, 'receive_index':receive_index,
                        'epoch_s':stamp, 'receive_epoch_s':receive,
                        'png_sha256':_png_sha(frame)})

    ordered = sorted(entries, key=lambda row:row['epoch_s'])
    output = list(frames);permutation = list(range(len(frames)))
    for old, new in zip(entries, ordered):
        output[old['raw_index']] = frames[new['raw_index']]
        permutation[old['raw_index']] = new['raw_index']
    for normalized_index, raw_index in enumerate(permutation):
        assert output[normalized_index] is frames[raw_index]
    receipt = {'kind':'CDPTransportOrderReceipt', 'schema_version':1,
        'policy_version':POLICY_VERSION, 'browser_version':browser_version,
        'source_clock_semantics':dict(SOURCE_CLOCK_SEMANTICS),
        'segment_epoch_s':[segment_start_s,segment_end_s],
        'segment_bounds_semantics':'Browser observation production interval; delivery may finish after screencast stop',
        'delivery_interval_epoch_s':[segment_start_s,delivery_end_s],
        'maximum_allowed_reorder_s':MAX_REORDER_S,
        'maximum_host_delivery_lag_s':MAX_HOST_DELIVERY_LAG_S,
        'maximum_observed_inversion_s':maximum_inversion,
        'raw_frame_count':len(frames), 'normalized_frame_count':len(output),
        'compositor_observation_count':len(entries),
        'permuted':permutation != list(range(len(frames))),
        'normalized_index_to_raw_index':permutation,
        'raw_compositor_observations':entries,
        'ordered_compositor_receive_indices':[e['receive_index'] for e in ordered],
        'pngs_dropped':0, 'timestamps_changed':0,
        'status':'validated_transport_order',
        'note':'Transport ordering only; timing, input ambiguity and pixel observability remain separate gates.'}
    return output, receipt
