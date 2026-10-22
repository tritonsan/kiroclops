"""Small raw-pixel samples and DOM-free annotation event tests."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from kiroclops.local_engine.intake import _ANNOTATION_HTML, prepare_intake, resume_selection
from kiroclops.visual.package import create_package, load_package
from kiroclops.visual.schema import VisualError, read_json


class IntakeConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='SVG café naïve ');self.root=Path(self.tmp.name)
        self.project=self.root/'project';(self.project/'src').mkdir(parents=True)
        (self.project/'index.html').write_text('<button id="add-node">+</button><script src="src/app.js"></script>')
        (self.project/'src/app.js').write_text("import {advanceLayout} from './layout-motion.js';advanceLayout();")
        (self.project/'src/layout-motion.js').write_text('export function advanceLayout(){}')
        image=np.zeros((64,64,3),np.uint8);image[20,40]=[33,22,11]
        paths=[]
        for i in range(4):
            path=self.root/f'{i}.png';path.write_bytes(cv2.imencode('.png',image)[1].tobytes());paths.append(path)
        self.package=self.root/'package';create_package(self.package,paths,[0.,.1,.2,.3])
        self.output=self.root/'intake'
        with patch('kiroclops.local_engine.intake.propose_objects',return_value={'proposals':[]}),\
             patch('kiroclops.local_engine.intake._qualify_automatic',return_value=(None,{'status':'needs_confirmation'})):
            prepare_intake(self.package,self.project,'Match visible nodes',self.output)
        self.manifest=load_package(self.output/'package')

    def tearDown(self):self.tmp.cleanup()

    def selection(self):
        return {'schema_version':1,'package_sha256':self.manifest['package_sha256'],
            'objects':[{'id':oid,'roi':[x,5,8,8],'anchor':[4,4],'body_height_px':8.,'frame_index':0,
              'method':'template','template_frame_sha256':self.manifest['frames'][0]['sha256'],
              'active_intervals_s':[[0.,.2]],'geometry':{'method':'contour','segmentation':'reference_fill','roi':[x-2,3,14,14]}}
              for oid,x in [('a',5),('b',30)]],
            'background_rois':[[0,40,8,8]],'connections':[{'id':'edge-a-b','from_object':'a','to_object':'b','method':'polyline',
              'stroke_rgb':[11,22,33],'stroke_tolerance':9.,'search_margin_px':80.,
              'stroke_sample':{'frame_index':1,'pixel':[40,20],'frame_sha256':self.manifest['frames'][1]['sha256']}}]}

    def test_raw_sample_full_connection_geometry_and_last_visible_frame_survive(self):
        original=self.selection();result=resume_selection(self.output,original);saved=read_json(result['selection_path'])
        self.assertEqual(original['connections'],saved['connections']);self.assertEqual(original['objects'],saved['objects'])
        self.assertEqual('ready_for_analysis',result['status']);self.assertFalse(result['benchmark_qualified'])

    def test_wrong_color_stale_frame_missing_sample_or_bad_pixel_reject(self):
        for mode in ('rgb','hash','sample','pixel'):
            with self.subTest(mode=mode):
                selection=self.selection();edge=selection['connections'][0]
                if mode=='rgb':edge['stroke_rgb'][0]+=1
                elif mode=='hash':edge['stroke_sample']['frame_sha256']='0'*64
                elif mode=='sample':edge.pop('stroke_sample')
                else:edge['stroke_sample']['pixel']=[64,20]
                with self.assertRaises(VisualError):resume_selection(self.output,selection)

    def test_lifecycle_is_selected_native_interval_not_freeform_mask(self):
        for value in ([[0.,.25]],[[.1,.2]],[[0.,.2],[.3,.4]],[[0.,.9]],[[0.,float('nan')]]):
            with self.subTest(value=value):
                selection=self.selection();selection['objects'][0]['active_intervals_s']=value
                with self.assertRaises(VisualError):resume_selection(self.output,selection)

    def test_bad_endpoints_or_geometry_reject_before_saving(self):
        for mode in ('unbound','duplicate','geometry','method'):
            with self.subTest(mode=mode):
                selection=self.selection()
                if mode=='unbound':selection['connections'][0]['to_object']='missing'
                elif mode=='duplicate':selection['connections'].append(deepcopy(selection['connections'][0]))
                elif mode=='geometry':selection['objects'][0]['geometry']['roi']=[60,0,14,14]
                else:selection['connections'][0]['method']='inferred_dom'
                with self.assertRaises(VisualError):resume_selection(self.output,selection)


@unittest.skipUnless(shutil.which('node'),'Node is required for DOM-free annotation handlers')
class AnnotationJavascriptTests(unittest.TestCase):
    def test_actual_handlers_and_getter_setter_preserve_full_selection_without_browser(self):
        data={'image_size':[320,320],'package_sha256':'package','frames':[{'time_s':i*.1,'path':f'{i}.png','sha256':f'sha-{i}'} for i in range(8)],
              'input_provenance':'unknown','request':'Observe'}
        source=_ANNOTATION_HTML.split('<script>',1)[1].split('</script>',1)[0].replace('__INTAKE_DATA__',json.dumps(data))
        script=r'''
const assert=require('assert'),vm=require('vm');const nodes={};
const ctx={clearRect(){},drawImage(){},strokeRect(){},beginPath(){},arc(){},stroke(){},getImageData(){return {data:[11,22,33,255]}}};
function node(id){return nodes[id]||(nodes[id]={value:({'#mode':'body','#object-id':'object-01','#timeline':'0','#connection-method':'line','#stroke-tolerance':'12','#search-margin':'64'})[id]||'',children:[],listeners:{},
 getContext(){return ctx},addEventListener(k,f){this.listeners[k]=f},append(x){this.children.push(x)},replaceChildren(){this.children=[]},
 setPointerCapture(){},getBoundingClientRect(){return {left:0,top:0,width:320,height:320}}});}
const document={querySelector:node,createElement:kind=>kind==='canvas'?{width:0,height:0,getContext(){return ctx}}:{append(){}}};
class Image{constructor(){this.complete=true;this.naturalWidth=320;}set src(v){this._src=v;if(this.onload)this.onload();}}
const window={};vm.runInNewContext(SOURCE,{document,window,Image,URL:{},Blob:function(){},setTimeout(){}});
function draw(mode,x,y,w,h){node('#mode').value=mode;node('#canvas').listeners.pointerdown({clientX:x,clientY:y,pointerId:1});node('#canvas').listeners.pointermove({clientX:x+w,clientY:y+h});node('#canvas').listeners.pointerup();}
draw('body',50,100,12,12);draw('geometry',45,90,90,55);node('#last-visible').value='5';node('#add').onclick();
draw('body',200,100,12,12);draw('geometry',190,90,90,55);node('#add').onclick();
node('#connection-from').value='object-01';node('#connection-to').value='object-02';node('#connection-method').value='polyline';
node('#sample-stroke').onclick();node('#canvas').listeners.pointerdown({clientX:150,clientY:110,pointerId:2});node('#add-connection').onclick();
let choice=window.kiroclopsSelection();assert.equal(choice.objects.length,2);assert.equal(choice.connections.length,1);
assert.equal(choice.objects[0].active_intervals_s[0][1],.5);assert.equal(choice.objects[1].active_intervals_s[0][1],.7000000000000001);
assert.deepEqual(choice.objects[0].roi,[50,100,12,12]);assert.deepEqual(choice.objects[0].geometry.roi,[45,90,90,55]);
assert.deepEqual(choice.connections[0].stroke_rgb,[11,22,33]);assert.deepEqual(choice.connections[0].stroke_sample.pixel,[150,110]);
assert.equal(choice.connections[0].stroke_sample.frame_sha256,'sha-0');assert.equal(choice.connections[0].stroke_tolerance,12);assert.equal(choice.connections[0].search_margin_px,64);
choice.relative_motion=[{object_id:'object-02',relative_to:'object-01'}];choice.boundaries=[{id:'wall',points:[[1,2],[3,4]]}];
window.kiroclopsSetSelection(choice);assert.deepEqual(window.kiroclopsSelection(),choice);choice.connections[0].stroke_rgb[0]=255;
assert.equal(window.kiroclopsSelection().connections[0].stroke_rgb[0],11);assert.throws(()=>window.kiroclopsSetSelection({package_sha256:'other'}));
node('#clear').onclick();assert.equal(window.kiroclopsSelection().connections.length,0);assert.equal(window.kiroclopsSelection().objects.length,0);
process.stdout.write('handlers/getter/setter PASS');
'''.replace('SOURCE',json.dumps(source))
        completed=subprocess.run([shutil.which('node'),'-'],input=script,text=True,encoding='utf-8',capture_output=True,timeout=10)
        self.assertEqual(0,completed.returncode,completed.stderr);self.assertIn('PASS',completed.stdout)


if __name__=='__main__':unittest.main()
