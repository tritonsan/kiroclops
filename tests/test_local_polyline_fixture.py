"""Plain SVG fixture integration and separate private geometry receipts."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from kiroclops.local_engine.intake import detect_project
from kiroclops.web_local.oracle import INIT_SCRIPT, validate_oracle

ROOT = Path(__file__).resolve().parents[1]


def private_record():
    objects = {name: {'x': x, 'y': y, 'width': 100., 'height': 50.}
               for name, x, y in [('a', 200., 200.), ('b', 350., 100.), ('c', 350., 300.)]}
    edges = [{'id': 'a-'+name, 'source': 'a', 'target': name, 'element': 'polyline',
              'geometry_status': 'pass', 'endpoint_source': 'private_svg_geometry_api',
              'start': {'x': 250., 'y': 225.},
              'end': {'x': objects[name]['x']+50., 'y': objects[name]['y']+25.}} for name in ('b','c')]
    return {'scenario': {'initial_node_count': 3, 'expected_final_node_count': 3},
            'samples': [deepcopy({'objects': objects, 'edges': edges, 'selected': []}) for _ in range(12)]}


class PolylineFixtureTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the JavaScript-only route geometry harness')
    def test_actual_routes_keep_interior_attachment_and_separated_visible_ports(self):
        source = (ROOT/'fixtures/phase3a-svg-polyline/reference/src/app.js').read_text(encoding='utf-8')
        start = source.index('function routedPoints(')
        route = source[start:source.index('\n}\n', start)+2]
        # Execute the real renderer function, without DOM/rendering. Check the
        # geometric invariant at translated origins and with every supported node.
        harness = route + r"""
const assert=require('node:assert/strict');
const expectedPorts={b:-16,c:0,d:16,e:-8,f:8};
const parse=value=>value.split(' ').map(pair=>pair.split(',').map(Number));
for(const core of [{x:480,y:260},{x:118,y:77},{x:734.5,y:313.25}]){
  let sides={left:[],right:[]};
  for(const id of ['b','c','d','e','f']){
    const node={id,x:core.x+180,y:core.y+130};
    const points=parse(routedPoints(core,node));
    assert.equal(points.length,5);
    assert.deepEqual(points[0],[core.x,core.y]);
    assert.deepEqual(points[4],[node.x,node.y]);
    // The first vertical segment lies wholly inside the real138x58 card.
    assert.equal(points[1][0],core.x);
    assert.ok(Math.abs(points[1][1]-core.y)<29);
    assert.equal(points[1][1]-core.y,expectedPorts[id]);
    // The visible horizontal segment crosses a side border, inside its flat
    // portion (rounded corners start18px from the center vertically).
    assert.equal(points[1][1],points[2][1]);
    assert.ok(Math.abs(points[2][0]-core.x)>69);
    assert.ok(Math.abs(points[1][1]-core.y)<=18);
    sides[points[2][0]<core.x?'left':'right'].push(points[1][1]);
    assert.ok(points.flat().every(Number.isFinite));
  }
  for(const ys of Object.values(sides))for(let i=0;i<ys.length;i++)for(let j=i+1;j<ys.length;j++){
    assert.ok(Math.abs(ys[i]-ys[j])>=8); // Distinct3px strokes cannot overpaint the same entry.
  }
}
process.stdout.write('separate visible port geometry pass');
"""
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        completed = subprocess.run([shutil.which('node')], input=harness, text=True,
                                   encoding='utf-8', capture_output=True, timeout=10, **options)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn('geometry pass', completed.stdout)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the JavaScript-only route geometry harness')
    def test_registered_detached_control_still_ends_twelve_pixels_outside_the_card(self):
        source = (ROOT/'fixtures/phase3a-svg-polyline/reference/src/app.js').read_text(encoding='utf-8')
        old = '[lane,node.y],[node.x,node.y]'
        self.assertEqual(1, source.count(old))
        start = source.index('function routedPoints(')
        route = source[start:source.index('\n}\n', start)+2]
        changed = route.replace(old, "[lane,node.y],[node.id==='b' ? node.x+Math.sign(lane-node.x)*81 : node.x,node.y]")
        # This is the existing capture helper's actual source mutation, not a
        # new fault or threshold. It must remain visibly outside the69px halfwidth.
        harness = 'const original=(new Function('+json.dumps(route+';return routedPoints;')+'))();\n'
        harness += 'const detached=(new Function('+json.dumps(changed+';return routedPoints;')+'))();\n'
        harness += r"""
const assert=require('node:assert/strict');
const parse=value=>value.split(' ').map(pair=>pair.split(',').map(Number));
const core={x:480,y:260};
for(const id of ['b','c','d','e','f']){
  const node={id,x:480,y:110};
  const good=parse(original(core,node)),bad=parse(detached(core,node));
  if(id==='b'){
    assert.deepEqual(good.slice(0,-1),bad.slice(0,-1));
    assert.equal(Math.abs(bad.at(-1)[0]-node.x)-69,12);
    assert.equal(bad.at(-1)[1],node.y);
  }else assert.deepEqual(good,bad);
}
process.stdout.write('existing detached mutation pass');
"""
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        completed = subprocess.run([shutil.which('node')], input=harness, text=True,
                                   encoding='utf-8', capture_output=True, timeout=10, **options)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn('mutation pass', completed.stdout)

    def test_actual_entry_graph_uses_existing_svg_adapter_and_only_motion_differs(self):
        base = ROOT/'fixtures/phase3a-svg-polyline'
        for variant in ('reference','target'):
            detected = detect_project(base/variant)
            self.assertEqual('svg', detected['domain'])
            self.assertEqual('advanceLayout', detected['controller_export'])
            self.assertEqual({'#add-node','#remove-node','#reset'},
                             {row['selector'] for row in detected['recommended_capture_actions']})
        for name in ('index.html','style.css','src/app.js'):
            self.assertEqual((base/'reference'/name).read_bytes(), (base/'target'/name).read_bytes())
        self.assertNotEqual((base/'reference/src/layout-motion.js').read_bytes(),
                            (base/'target/src/layout-motion.js').read_bytes())

    def test_separate_native_polyline_attachment_is_not_pixel_acceptance(self):
        result = validate_oracle(private_record(), 'svg')
        self.assertEqual('pass', result['status'])
        self.assertEqual('private_dom_oracle', result['checks_source'])
        bad = private_record(); bad['samples'][3]['edges'][0]['end']['x'] += 12
        self.assertIn('edge_detached', validate_oracle(bad, 'svg')['failures'])

    def test_unsupported_or_nonfinite_geometry_fails_explicitly(self):
        for replacement in ({'geometry_status':'fail','reason':'unsupported_edge_geometry'},
                            {'geometry_status':'pass','start':{'x':float('nan'),'y':0},'end':{'x':1,'y':2}}):
            value = private_record(); value['samples'][2]['edges'][0].update(replacement)
            self.assertIn('edge_geometry_unobservable', validate_oracle(value, 'svg')['failures'])

    def test_previous_line_receipts_keep_their_native_semantics(self):
        value = private_record()
        for row in value['samples']:
            for edge in row['edges']:
                for key in ('element','geometry_status','endpoint_source'):
                    edge.pop(key)
        self.assertEqual('pass', validate_oracle(value, 'svg')['status'])

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the JavaScript-only oracle harness')
    def test_script_samples_geometry_endpoints_and_screen_transform_without_attribute_guessing(self):
        # No browser, rendering or pixels: exercise the actual private script
        # against geometry objects. Real-render qualification is separate.
        harness = r"""
const assert=require('node:assert/strict');
global.window={};global.performance={timeOrigin:1000,now:()=>10};let callbacks=[];
global.requestAnimationFrame=f=>callbacks.push(f);
let calls=[];
const edge={localName:'polyline',dataset:{edge:'a-b',source:'a',target:'b'},
 getScreenCTM:()=>({a:2,b:0,c:0,d:3,e:7,f:11}),
 getTotalLength:()=>200,getPointAtLength:t=>{calls.push(t);return t===0?{x:10,y:20}:{x:90,y:80};},
 getAttribute:()=>{throw Error('Attribute guessing is forbidden');}};
global.document={addEventListener(){},querySelector:()=>null,querySelectorAll:s=>s==='[data-edge]'?[edge]:[]};
"""
        ending = r"""
window.__kcPrivate.enabled=true;callbacks.shift()();
let result=window.__kcPrivate.samples[0].edges[0];
assert.deepEqual(calls,[0,200]);assert.deepEqual(result.start,{x:27,y:71});assert.deepEqual(result.end,{x:187,y:251});
assert.equal(result.endpoint_source,'private_svg_geometry_api');assert.equal(result.geometry_status,'pass');
edge.localName='rect';callbacks.shift()();assert.equal(window.__kcPrivate.samples[1].edges[0].geometry_status,'fail');
process.stdout.write('geometry API harness pass');
"""
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        completed = subprocess.run([shutil.which('node')], input=harness+INIT_SCRIPT+ending,
                                   text=True, encoding='utf-8', capture_output=True, timeout=10, **options)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn('harness pass', completed.stdout)


if __name__ == '__main__':
    unittest.main()
