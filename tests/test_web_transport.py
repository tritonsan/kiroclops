"""Delivery permutations preserve exact observations; corrupt clocks still fail."""
import base64
from copy import deepcopy
import unittest

from kiroclops.web_local.capture import CaptureError, merge_frame_receipts
from kiroclops.web_local.transport import (normalize_compositor_delivery,
    TransportOrderError, SUPPORTED_BROWSER_VERSION, SOURCE_CLOCK_SEMANTICS)


def packet(stamp,receive_index,receive=None,data=b'raw-pixel-payload'):
    return {'timestamp_source':'cdp_compositor','epoch_s':stamp,
        'receive_epoch_s':receive if receive is not None else 100.5+receive_index*.01,
        'receive_index':receive_index,'metadata':{'timestamp':stamp},
        'interval_epoch_s':[stamp,stamp],'data':base64.b64encode(data).decode()}


def normalize(frames,**kwargs):
    return normalize_compositor_delivery(frames,segment_start_s=kwargs.get('start',100.),
        segment_end_s=kwargs.get('end',102.),delivery_end_s=kwargs.get('delivery_end'),
        browser_version=kwargs.get('version',SUPPORTED_BROWSER_VERSION))


class TransportOrderTests(unittest.TestCase):
    def test_actual_recorded_226_227_timestamps_need_explicit_transport_policy(self):
        # Exact timing reduction of refs06 interrupt-w1280-r2, original raw
        # receipt SHA d35e1e35507815ce5b2c7a164d7603e9e6c4a576a8e6705f0705f8cef573b992.
        # Payload below is a synthetic identical-pixel test fixture; separate
        # artifact replay checks the original PNGs without modifying evidence.
        frames=[packet(1790024108.538461,226,1790024108.5669768),
                packet(1790024108.517784,227,1790024108.5689287)]
        original=deepcopy(frames)
        with self.assertRaises(CaptureError):merge_frame_receipts(frames)
        result,receipt=normalize(frames,start=1790024106.,end=1790024110.)
        self.assertEqual(result,[frames[1],frames[0]])
        self.assertEqual(merge_frame_receipts(result),result)
        self.assertEqual(frames,original)
        self.assertIs(result[0],frames[1]);self.assertIs(result[1],frames[0])
        self.assertEqual(receipt['normalized_index_to_raw_index'],[1,0])
        self.assertEqual(receipt['ordered_compositor_receive_indices'],[227,226])
        self.assertEqual(receipt['timestamps_changed'],0);self.assertEqual(receipt['pngs_dropped'],0)

    def test_in_order_is_identity(self):
        frames=[packet(100.1,0),packet(100.2,1)]
        result,receipt=normalize(frames)
        self.assertEqual(result,frames);self.assertFalse(receipt['permuted'])

    def test_begin_frame_subsequence_is_not_permuted(self):
        a=packet(100.2,0);b=packet(100.1,1)
        bracket={'timestamp_source':'cdp_begin_frame_bracket','epoch_s':100.15,
                 'interval_epoch_s':[100.14,100.16],'data':'YWJj'}
        result,receipt=normalize([a,bracket,b])
        self.assertEqual(result,[b,bracket,a]);self.assertIs(result[1],bracket)
        self.assertEqual(receipt['normalized_index_to_raw_index'],[2,1,0])

    def test_equal_time_identical_pixels_always_fail(self):
        with self.assertRaisesRegex(TransportOrderError,'Equal'):
            normalize([packet(100.1,0),packet(100.1,1)])

    def test_equal_time_different_pixels_also_fail(self):
        with self.assertRaisesRegex(TransportOrderError,'Equal'):
            normalize([packet(100.1,0,data=b'a'),packet(100.1,1,data=b'b')])

    def test_reorder_bound_uses_maximum_prior_time_not_only_adjacent(self):
        with self.assertRaisesRegex(TransportOrderError,'120ms'):
            normalize([packet(100.3,0),packet(100.21,1),packet(100.11,2)])

    def test_large_reorder_is_not_a_repair(self):
        with self.assertRaises(TransportOrderError):normalize([packet(100.4,0),packet(100.1,1)])

    def test_forged_metadata_cannot_normalize(self):
        frame=packet(100.1,0);frame['metadata']['timestamp']=100.101
        with self.assertRaisesRegex(TransportOrderError,'metadata'):normalize([frame])

    def test_missing_metadata_cannot_normalize(self):
        frame=packet(100.1,0);frame.pop('metadata')
        with self.assertRaises(TransportOrderError):normalize([frame])

    def test_nonfinite_or_boolean_timestamp_is_invalid(self):
        for value in [float('inf'),float('nan'),True,'100.1']:
            with self.subTest(value=value),self.assertRaises(TransportOrderError):normalize([packet(value,0)])

    def test_future_observation_rejected(self):
        with self.assertRaisesRegex(TransportOrderError,'after its receipt'):
            normalize([packet(100.2,0,100.199)])

    def test_existing_host_lag_bound_is_retained(self):
        with self.assertRaisesRegex(TransportOrderError,'one-second'):
            normalize([packet(100.1,0,101.101)])

    def test_real_frame_encoding_can_arrive_after_observation_stream_stops(self):
        frames=[packet(100.4,0,100.55)]
        result,receipt=normalize(frames,end=100.5,delivery_end=100.6)
        self.assertEqual(result,frames)
        self.assertEqual(receipt['segment_epoch_s'],[100.,100.5])
        self.assertEqual(receipt['delivery_interval_epoch_s'],[100.,100.6])

    def test_longer_delivery_interval_does_not_extend_observation_clock(self):
        with self.assertRaises(TransportOrderError):
            normalize([packet(100.51,0,100.55)],end=100.5,delivery_end=100.6)
        with self.assertRaises(TransportOrderError):
            normalize([packet(100.49,0,100.61)],end=100.5,delivery_end=100.6)

    def test_delivery_end_cannot_precede_stream_end(self):
        with self.assertRaisesRegex(TransportOrderError,'delivery bounds'):
            normalize([packet(100.1,0)],delivery_end=101.9)

    def test_segment_bounds_rejected(self):
        for frame in [packet(99.9,0),packet(100.1,0,102.1)]:
            with self.assertRaises(TransportOrderError):normalize([frame])

    def test_backward_or_duplicate_receive_index_is_invalid(self):
        for index in [0,-1,True]:
            with self.subTest(index=index),self.assertRaises(TransportOrderError):
                normalize([packet(100.1,0),packet(100.2,index)])

    def test_backward_receive_time_is_invalid(self):
        with self.assertRaisesRegex(TransportOrderError,'receipt index/time'):
            normalize([packet(100.1,0,100.6),packet(100.2,1,100.59)])

    def test_missing_receive_index_cannot_be_inferred(self):
        frame=packet(100.1,0);frame.pop('receive_index')
        with self.assertRaises(TransportOrderError):normalize([frame])

    def test_modified_source_interval_is_invalid(self):
        frame=packet(100.1,0);frame['interval_epoch_s']=[100.09,100.11]
        with self.assertRaises(TransportOrderError):normalize([frame])

    def test_backward_host_brackets_are_never_sorted(self):
        frames=[{'timestamp_source':'cdp_begin_frame_bracket','epoch_s':t,
                 'interval_epoch_s':[t-.001,t+.001]} for t in [100.2,100.1]]
        with self.assertRaisesRegex(TransportOrderError,'Host-bracket'):normalize(frames)

    def test_overlapping_host_brackets_are_never_normalized(self):
        frames=[{'timestamp_source':'cdp_begin_frame_bracket','epoch_s':t,
                 'interval_epoch_s':[t-.01,t+.01]} for t in [100.1,100.11]]
        with self.assertRaises(TransportOrderError):normalize(frames)

    def test_unverified_build_is_explicit_failure(self):
        with self.assertRaisesRegex(TransportOrderError,'Unverified Chromium'):
            normalize([packet(100.1,0)],version='154.0.0.0')

    def test_unknown_presentation_uncertainty_not_claimed_zero(self):
        self.assertIsNone(SOURCE_CLOCK_SEMANTICS['pixel_presentation_time_uncertainty_s'])
        self.assertFalse(SOURCE_CLOCK_SEMANTICS['pixel_presentation_time_accuracy_claimed'])
        self.assertFalse(SOURCE_CLOCK_SEMANTICS['capture_to_metadata_latency_measured'])

    def test_unknown_source_and_invalid_png_fail(self):
        a=packet(100.1,0);a['timestamp_source']='invented'
        b=packet(100.1,0);b['data']='bad base64 ?'
        for frame in [a,b]:
            with self.assertRaises(TransportOrderError):normalize([frame])


if __name__=='__main__':unittest.main()
