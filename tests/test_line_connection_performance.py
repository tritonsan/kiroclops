"""Differential regression against the pre-optimization pixel kernel.

The legacy helper is copied unchanged from tracking.py SHA
1b41124aa5cc03c195a58711c763c0d3df6e688f6725b2576ec725031bd9dcc3.
It is an independent frozen oracle here, never used by production.
"""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.visual.tracking import _line_connection, _number
from kiroclops.visual.tracking import analyze_package
from kiroclops.visual.package import create_package


def _legacy_line_connection(gray, spec, frames):
    """Visible straight segments only. Endpoints are measured, never snapped."""
    a, b = frames[spec['from_object']], frames[spec['to_object']]
    if not a['valid'] or not b['valid']:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'node_unobservable'}
    pa = np.array([a['left']+a['width']/2, a['top']+a['height']/2], float)
    pb = np.array([b['left']+b['width']/2, b['top']+b['height']/2], float)
    direction = pb-pa
    length = float(np.linalg.norm(direction))
    if length < 5:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'ambiguous_node_geometry'}
    direction /= length
    edges = cv2.Canny(gray, spec.get('canny_low', 50), spec.get('canny_high', 150))
    corridor = _number(spec.get('corridor_px', 8), 'connection corridor', 1, 100)
    yy, xx = np.indices(gray.shape)
    along = (xx-pa[0])*direction[0]+(yy-pa[1])*direction[1]
    across = abs((xx-pa[0])*direction[1]-(yy-pa[1])*direction[0])
    region = (across <= corridor) & (along >= 0) & (along <= length)
    for frame in frames.values():
        if not frame.get('valid'):
            continue
        x, y, w, h = [int(round(frame[k])) for k in ('left', 'top', 'width', 'height')]
        edges[max(0, y):y+h, max(0, x):x+w] = 0
    # Preserve actual support outside the search region. Cutting a continuing
    # stroke at the corridor boundary does not reveal a physical endpoint.
    outside_support = (edges > 0) & ~region
    for frame in frames.values():
        if not frame.get('valid'):
            continue
        x, y, w, h = [int(round(frame[k])) for k in ('left', 'top', 'width', 'height')]
        # Canny/three-pixel grouping can leave a node-outline halo. It is an
        # occluding border, not evidence that a connector continues outside
        # the corridor. This mask is diagnostic only; endpoints are unchanged.
        outside_support[max(0,y-3):y+h+3,max(0,x-3):x+w+3] = False
    edges[~region] = 0
    # Canny's two sides of a three-pixel stroke are one visible connection.
    # Group nearby edge support, but measure endpoints from original edge
    # pixels only. Hough segments alone can stop halfway along a sound line.
    support = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(support, connectivity=8)
    if count <= 1:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'visible_connection_not_found'}
    candidates = []
    for label in range(1, count):
        left, top, width, height, area = stats[label]
        if area < 6:
            continue
        local_labels=labels[top:top+height,left:left+width]
        local_edges=edges[top:top+height,left:left+width]
        ys, xs = np.nonzero((local_labels == label) & (local_edges > 0))
        if len(xs) < 6:
            continue
        xs=xs+left;ys=ys+top
        points = np.column_stack((xs, ys)).astype(float)
        vx, vy, _, _ = cv2.fitLine(points.astype(np.float32), cv2.DIST_L2, 0, .01, .01).reshape(-1)
        fitted = np.array([vx, vy], float)
        if abs(float(np.dot(fitted, direction))) < .97:
            continue
        if np.dot(fitted, direction) < 0:
            fitted *= -1
        projected = points @ fitted
        # Averaging the observed opposing stroke edges locates its visible
        # center, without extending it toward any node boundary.
        p = points[projected <= projected.min()+1e-5].mean(axis=0)
        q = points[projected >= projected.max()-1e-5].mean(axis=0)
        size = float(np.linalg.norm(q-p))
        if size < 6:
            continue
        clipped = False
        for endpoint in (p, q):
            ex, ey = endpoint
            x0, x1 = max(0, int(ex)-3), min(gray.shape[1], int(ex)+5)
            y0, y1 = max(0, int(ey)-3), min(gray.shape[0], int(ey)+5)
            oy, ox = np.nonzero(outside_support[y0:y1, x0:x1])
            if len(ox) and np.any((ox+x0-ex)**2+(oy+y0-ey)**2 <= 9):
                clipped = True
                break
        midpoint = (p+q)/2
        lateral = abs(float((midpoint[0]-pa[0])*direction[1]-(midpoint[1]-pa[1])*direction[0]))
        candidates.append((size-.5*lateral, p.tolist(), q.tolist(), size, clipped))
    if not candidates:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'unsupported_connection_geometry'}
    candidates.sort(key=lambda row: row[0], reverse=True)
    best = candidates[0]
    if best[4]:
        return {'valid': False, 'start': None, 'end': None, 'reason': 'endpoint_clipped_by_search_region'}
    if any(abs(other[0]-best[0]) < 1 and
           min(np.linalg.norm(np.array(other[1])-best[1]), np.linalg.norm(np.array(other[2])-best[2])) > 4
           for other in candidates[1:]):
        return {'valid': False, 'start': None, 'end': None, 'reason': 'ambiguous_visible_connection'}
    return {'valid': True, 'start': best[1], 'end': best[2], 'score': min(1., best[3]/length),
            'method': 'canny_connected_visible_support', 'reason': None}


class LineConnectionPerformanceTests(unittest.TestCase):
    def frames(self, a=(24, 50, 20, 22), b=(184, 50, 20, 22), extra=()):
        return {name: {'valid': True, 'left': box[0], 'top': box[1], 'width': box[2], 'height': box[3]}
                for name, box in [('a', a), ('b', b), *extra]}

    def compare(self, gray, frames, *, corridor=8, reverse=False, cache=None):
        spec={'from_object':'b' if reverse else 'a','to_object':'a' if reverse else 'b',
              'corridor_px':corridor}
        expected=_legacy_line_connection(gray,spec,frames)
        # The historical V1 rule stays byte-compatible with its frozen oracle.
        actual=_line_connection(gray,spec,frames,edge_cache=cache,endpoint_method='extreme_v1')
        self.assertEqual(actual,expected)
        return actual

    def test_horizontal_vertical_diagonal_and_reversed_are_exact(self):
        for a,b in [((24,50,20,22),(184,50,20,22)),
                    ((80,10,20,22),(80,135,20,22)),
                    ((12,15,21,23),(181,127,20,22)),
                    ((14,141,20,22),(181,11,21,21))]:
            for reverse in (False,True):
                with self.subTest(a=a,b=b,reverse=reverse):
                    gray=np.zeros((181,241),np.uint8)
                    cv2.line(gray,(int(a[0]+a[2]/2),int(a[1]+a[3]/2)),
                             (int(b[0]+b[2]/2),int(b[1]+b[3]/2)),210,3)
                    self.compare(gray,self.frames(a,b),reverse=reverse)

    def test_image_borders_subpixel_geometry_and_occluding_nodes_are_exact(self):
        for a,b in [((0,0,17,19),(217,154,24,27)),((0,159,19,22),(220,0,21,19)),
                    ((11.25,18.75,19.5,21.5),(175.75,127.25,20.5,22.5))]:
            for corridor in (1,8,30,100):
                with self.subTest(a=a,b=b,corridor=corridor):
                    gray=np.zeros((181,241),np.uint8)
                    cv2.line(gray,(int(a[0]+a[2]/2),int(a[1]+a[3]/2)),
                             (int(b[0]+b[2]/2),int(b[1]+b[3]/2)),255,3)
                    frames=self.frames(a,b,extra=[('occluder',(108,76,14,20))])
                    self.compare(gray,frames,corridor=corridor)

    def test_clipped_endpoint_is_not_converted_to_a_visible_endpoint(self):
        gray=np.zeros((160,260),np.uint8)
        cv2.line(gray,(30,25),(230,130),255,3)
        frames=self.frames((20,40,20,20),(220,90,20,20))
        result=self.compare(gray,frames,corridor=8)
        self.assertFalse(result['valid'])
        self.assertEqual(result['reason'],'endpoint_clipped_by_search_region')

    def test_parallel_ambiguous_lines_remain_ambiguous(self):
        gray=np.zeros((140,260),np.uint8)
        cv2.line(gray,(50,55),(209,55),255,1)
        cv2.line(gray,(50,65),(209,65),255,1)
        result=self.compare(gray,self.frames((25,50,20,20),(215,50,20,20)),corridor=12)
        self.assertFalse(result['valid'])
        self.assertEqual(result['reason'],'ambiguous_visible_connection')

    def test_empty_invalid_and_near_coincident_geometry_are_exact(self):
        gray=np.zeros((83,127),np.uint8)
        frames=self.frames((0,0,10,10),(100,60,10,10))
        self.compare(gray,frames)
        frames['a']['valid']=False
        self.compare(gray,frames)
        self.compare(gray,self.frames((21,21,10,10),(22,23,10,10)))

    def test_seeded_stroke_and_noise_differential_preserves_every_field(self):
        random=np.random.default_rng(320491)
        for index in range(80):
            height=int(random.integers(63,181));width=int(random.integers(97,251))
            gray=np.zeros((height,width),np.uint8)
            a=(float(random.integers(0,width//3)),float(random.integers(0,height-18)),13,17)
            b=(float(random.integers(2*width//3,width-14)),float(random.integers(0,height-18)),13,17)
            cv2.line(gray,(int(a[0]+6),int(a[1]+8)),(int(b[0]+6),int(b[1]+8)),
                     int(random.integers(100,256)),int(random.integers(1,5)))
            for _ in range(5):
                x1,y1=int(random.integers(width)),int(random.integers(height))
                x2,y2=int(random.integers(width)),int(random.integers(height))
                cv2.line(gray,(x1,y1),(x2,y2),int(random.integers(50,256)),1)
            with self.subTest(index=index):
                self.compare(gray,self.frames(a,b),corridor=int(random.integers(1,24)),reverse=bool(index%2))

    def test_per_frame_cache_reuses_full_canny_without_mutation_or_threshold_mixing(self):
        gray=np.zeros((131,241),np.uint8)
        cv2.line(gray,(30,61),(210,61),255,3)
        original=gray.copy();frames=self.frames()
        cache={};real_canny=cv2.Canny
        with patch('kiroclops.visual.tracking.cv2.Canny',wraps=real_canny) as canny:
            self.compare(gray,frames,cache=cache)
            # Each compare calls its frozen oracle once plus the production kernel.
            self.compare(gray,frames,reverse=True,cache=cache)
            self.assertEqual(canny.call_count,3)
            saved={key:value.copy() for key,value in cache.items()}
            spec={'from_object':'a','to_object':'b','canny_low':60,'canny_high':180}
            expected=_legacy_line_connection(gray,spec,frames)
            self.assertEqual(_line_connection(gray,spec,frames,edge_cache=cache,endpoint_method='extreme_v1'),expected)
            self.assertEqual(canny.call_count,5)
        np.testing.assert_array_equal(gray,original)
        for key,value in saved.items():
            np.testing.assert_array_equal(cache[key],value)

    def test_real_package_cache_does_not_replay_a_removed_connection(self):
        random=np.random.default_rng(409)
        image=np.zeros((100,180,3),np.uint8)
        image[40:60,20:44]=random.integers(30,255,(20,24,3),dtype=np.uint8)
        image[40:60,130:154]=random.integers(30,255,(20,24,3),dtype=np.uint8)
        linked=image.copy()
        cv2.line(linked,(49,50),(124,50),(230,230,230),3)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);paths=[]
            for i,frame in enumerate((linked,image)):
                path=root/f'{i}.png';path.write_bytes(cv2.imencode('.png',frame)[1].tobytes());paths.append(path)
            create_package(root/'package',paths,[0.,.04])
            selection={'schema_version':1,'objects':[
                {'id':'a','roi':[20,40,24,20],'anchor':[12,10]},
                {'id':'b','roi':[130,40,24,20],'anchor':[12,10]}],
                'connections':[{'id':'a-b','from_object':'a','to_object':'b','method':'line'}]}
            result=analyze_package(root/'package',selection)
            self.assertTrue(all(obj['valid_ratio']==1 for obj in result['objects']))
            self.assertEqual([frame['valid'] for frame in result['connections'][0]['frames']],[True,False])
            self.assertEqual(result['status'],'rejected')


def _scene(dx=0, ring=False):
    """Antialiased 3 px stroke along the node-centre axis, ending at both boxes."""
    image=np.full((240,260,3),(37,25,16),np.uint8)
    a=(60,20,80,40);b=(60+dx,150,80,40)
    ca=(a[0]+a[2]/2,a[1]+a[3]/2);cb=(b[0]+b[2]/2,b[1]+b[3]/2)
    t0=(a[1]+a[3]-ca[1])/(cb[1]-ca[1]);t1=(b[1]-1-ca[1])/(cb[1]-ca[1])
    start=(ca[0]+t0*(cb[0]-ca[0]),a[1]+a[3]);end=(ca[0]+t1*(cb[0]-ca[0]),b[1]-1)
    cv2.line(image,tuple(int(round(v)) for v in start),tuple(int(round(v)) for v in end),(176,149,128),3,cv2.LINE_AA)
    for box,focus in ((a,False),(b,ring)):
        x,y,w,h=box
        cv2.rectangle(image,(x,y),(x+w-1,y+h-1),(79,57,38),-1)
        cv2.rectangle(image,(x-1,y-1),(x+w,y+h),(120,120,120),1)
        if focus:
            cv2.rectangle(image,(x-2,y-2),(x+w+1,y+h+1),(16,16,16),1)
            cv2.rectangle(image,(x-3,y-3),(x+w+2,y+h+2),(195,195,195),1)
    frames={'a':{'valid':True,'left':a[0],'top':a[1],'width':a[2],'height':a[3]},
            'b':{'valid':True,'left':b[0],'top':b[1],'width':b[2],'height':b[3]}}
    return cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),frames,start,end


class AxisBandEndpointTests(unittest.TestCase):
    """V3 endpoint rule: the stroke axis at the furthest visible extent of its band."""
    SPEC={'from_object':'a','to_object':'b','corridor_px':8}

    def test_default_is_v2_and_unknown_methods_are_rejected(self):
        gray,frames,_,_=_scene()
        self.assertEqual('canny_axis_band_visible_support_v2',_line_connection(gray,self.SPEC,frames)['method'])
        self.assertEqual('canny_connected_visible_support',
                         _line_connection(gray,{**self.SPEC,'endpoint_method':'extreme_v1'},frames)['method'])
        with self.assertRaises(Exception):
            _line_connection(gray,self.SPEC,frames,endpoint_method='snap_to_border')

    def test_diagonal_and_ring_ends_lie_on_the_stroke_axis(self):
        for label,kwargs in (('vertical',{}),('ring',{'ring':True}),('diagonal',{'dx':25}),
                             ('diagonal-ring',{'dx':25,'ring':True})):
            with self.subTest(label=label):
                gray,frames,start,end=_scene(**kwargs)
                result=_line_connection(gray,self.SPEC,frames)
                self.assertTrue(result['valid'],result)
                self.assertLess(np.hypot(*np.subtract(result['start'],start)),1.)
                self.assertLess(np.hypot(*np.subtract(result['end'],end)),1.5)

    def test_v1_border_drift_is_what_v2_removes(self):
        gray,frames,start,_=_scene(dx=25)
        old=_line_connection(gray,self.SPEC,frames,endpoint_method='extreme_v1')
        new=_line_connection(gray,self.SPEC,frames)
        self.assertGreater(np.hypot(*np.subtract(old['start'],start)),2.)
        self.assertLess(np.hypot(*np.subtract(new['start'],start)),1.)

    def test_cache_and_full_image_agree_for_v2(self):
        for kwargs in ({},{'ring':True},{'dx':25},{'dx':-30,'ring':True}):
            with self.subTest(**kwargs):
                gray,frames,_,_=_scene(**kwargs)
                cache={}
                self.assertEqual(_line_connection(gray,self.SPEC,frames),
                                 _line_connection(gray,self.SPEC,frames,edge_cache=cache))
                self.assertEqual(_line_connection(gray,self.SPEC,frames),
                                 _line_connection(gray,self.SPEC,frames,edge_cache=cache))


if __name__=='__main__':
    unittest.main()
