"""Diagnostic only: correlate each PNG with a visible real-RAF counter.

No target positions, layout parameters or velocities are used for measurement.
The image/clock association is tested in copied analyses; ordinary recordings
and their original CDP clocks remain intact. This is not an acceptance runner.
"""
from pathlib import Path
from copy import deepcopy
import argparse
import sys
import math

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cv2
from kiroclops.visual.schema import read_json,write_json,file_sha256
from kiroclops.local_engine.evaluation import _capture_web,_compare_web,_lifecycle_bindings
from kiroclops.local_engine.environment import configure_environment
from kiroclops.web_local.specs import scenarios,fixture_project

CLOCK_SCRIPT=r'''(()=>{
  const rows=[];let id=0,canvas=null,cells=[];const ns='http://www.w3.org/2000/svg';
  window.__kcPrivate.visual_clock={rows,policy:'svg-raf-pixel-tag-probe-v1'};
  function tick(raf){
    if(!canvas && document.documentElement){canvas=document.createElementNS(ns,'svg');
      canvas.setAttribute('width','84');canvas.setAttribute('height','6');canvas.setAttribute('aria-hidden','true');
      canvas.style.cssText='position:fixed;bottom:0;right:0;width:84px;height:6px;z-index:2147483647;pointer-events:none';
      for(let i=0;i<28;i++)for(let row=0;row<2;row++){const r=document.createElementNS(ns,'rect');
        r.setAttribute('x',i*3);r.setAttribute('y',row*3);r.setAttribute('width',3);r.setAttribute('height',3);
        canvas.append(r);cells.push(r);}
      document.documentElement.append(canvas);}
    if(canvas){id++;if(id>65535)throw Error('visual_clock_overflow');
      const before=(performance.timeOrigin+performance.now())/1000;
      const bits=[1,0,1,0];for(let i=15;i>=0;i--)bits.push((id>>i)&1);
      const crc=(id^(id>>8)^0xa7)&255;for(let i=7;i>=0;i--)bits.push((crc>>i)&1);
      bits.forEach((bit,i)=>{for(let row=0;row<2;row++)cells[i*2+row].style.fill=(bit^row)?'#fafafa':'#050505';});
      const row={id,callback_epoch_s:before,raf_epoch_s:(performance.timeOrigin+raf)/1000,
        callback_end_epoch_s:(performance.timeOrigin+performance.now())/1000,after_task_epoch_s:null};
      rows.push(row);setTimeout(()=>{row.after_task_epoch_s=(performance.timeOrigin+performance.now())/1000;},0);
    }requestAnimationFrame(tick);
  }requestAnimationFrame(tick);
})();'''


def decode(image):
    patch=image[-6:,-84:,:3];bits=[]
    if patch.shape!=(6,84,3):raise ValueError('Missing visual clock')
    for i in range(28):
        cells=[patch[row*3:(row+1)*3,i*3:(i+1)*3] for row in range(2)]
        vals=[1 if (cell>235).all() else 0 if (cell<20).all() else None for cell in cells]
        if None in vals or vals[0]==vals[1]:
            raise ValueError('Ambiguous visual clock cell')
        bits.append(vals[0])
    if bits[:4]!=[1,0,1,0]:raise ValueError('Missing visual clock preamble')
    def number(values):
        n=0
        for bit in values:n=n*2+bit
        return n
    ident=number(bits[4:20]);crc=number(bits[20:])
    if crc!=((ident^(ident>>8)^0xa7)&255):raise ValueError('Visual clock checksum mismatch')
    return ident


def adjusted(record, capture):
    record=deepcopy(record);package=Path(record['package']);manifest=read_json(package/'manifest.json')
    private=read_json(Path(capture)/'private/oracle.json');clock={r['id']:r for r in private['visual_clock']['rows']}
    origin=private['start_epoch_s'];times={};associations=[];prior=-math.inf;ids=set()
    for frame in manifest['frames']:
        image=cv2.imread(str(package/frame['path']));ident=decode(image)
        row=clock[ident];end=row['after_task_epoch_s']
        if end is None or not row['callback_epoch_s']<=end or end-row['callback_epoch_s']>.04:
            raise ValueError('Unbounded rendering-batch clock interval')
        stamp=(row['callback_epoch_s']+end)/2-origin
        if stamp<=prior or ident in ids:raise ValueError('Duplicate or unordered visible RAF counter')
        prior=stamp;ids.add(ident);times[frame['time_s']]=stamp
        associations.append({'index':frame['index'],'sha256':frame['sha256'],'visible_counter':ident,
            'original_cdp_time_s':frame['time_s'],'observed_callback_time_s':stamp,
            'metadata_minus_callback_s':frame['time_s']-stamp,
            'rendering_batch_interval_s':[row['callback_epoch_s']-origin,end-origin],
            'callback_duration_s':row['callback_end_epoch_s']-row['callback_epoch_s']})
    def rewrite(item):
        if isinstance(item,list):
            for child in item:rewrite(child)
        elif isinstance(item,dict):
            if 'frames' in item:
                for frame in item['frames']:frame['time_s']=times[frame['time_s']]
            if 'active_intervals_s' in item:item['active_intervals_s']=[[times[a],times[b]] for a,b in item['active_intervals_s']]
            for key,child in item.items():
                if key not in ('frames','active_intervals_s','inputs','lifecycle_bindings','visibility_brackets'):rewrite(child)
    rewrite(record['observations']);rewrite(record['selection'])
    frames=[{'time_s':times[f['time_s']]} for f in manifest['frames']]
    record['selection']['lifecycle_bindings']=_lifecycle_bindings(record['selection'],
        {'frames':frames,'inputs':record['observations']['inputs']})
    record['diagnostic_only']=True
    return record,associations


def run(output):
    from kiroclops.web_local import capture
    from kiroclops.local_engine.session import _lock
    from scripts.validate_panel_recorder import implementation_hashes
    output=Path(output)
    if output.exists():raise ValueError('Fresh diagnostic output required')
    output.mkdir(parents=True);configure_environment()
    original=capture.INIT_SCRIPT;capture.INIT_SCRIPT=original+'\n'+CLOCK_SCRIPT
    recipes=[next(s for s in scenarios('svg','calibration') if s['recipe']=='four-to-three'),
             next(s for s in scenarios('svg','validation') if s['recipe']=='five-to-six-drag' and s['viewport'][0]==768)]
    write_json(output/'protocol.json',{'kind':'PixelClockEngineeringProbe','acceptance':False,
        'script_sha256':file_sha256(__file__),'implementation_hashes':implementation_hashes(ROOT),
        'in_memory_recorder_instrumentation':True,'overlay':{'width':84,'height':6,'position':'bottom_right'},
        'source_geometry_used_for_measurement':False,'time_meaning':'Observed recorder RAF callback, not display presentation',
        'recipes':recipes,'repeats':2,'retries':0})
    rows=[]
    try:
        for scenario in recipes:
            scenario=deepcopy(scenario);scenario.update(split='qualification',analysis_policy='observed_response_v2',frame_driver='natural_browser_raf_v1')
            records=[];stamped=[]
            for repeat in (1,2):
                print(scenario['id'],repeat,flush=True);path=output/scenario['id']/str(repeat)
                with _lock(ROOT/'artifacts/local-engine/capture.lock',timeout=30):
                    record=_capture_web(fixture_project('svg','reference'),path,scenario,lambda:None,lambda *a:None,lambda:False,'reference',
                        reference_selection=records[0]['selection'] if records else None)
                write_json(path/'record.json',record);new,association=adjusted(record,path)
                write_json(path/'pixel-clock-association.json',association);write_json(path/'diagnostic-record.json',new)
                records.append(record);stamped.append(new)
            raw=_compare_web(records[0],records[0],records[1],improvement=False)
            visible=_compare_web(stamped[0],stamped[0],stamped[1],improvement=False)
            row={'scenario':scenario['id'],'raw_pass':raw['pass'],'pixel_clock_pass':visible['pass'],
                 'raw':raw,'pixel_clock':visible}
            write_json(output/scenario['id']/'comparison.json',row);rows.append(row)
            print({k:row[k] for k in ('scenario','raw_pass','pixel_clock_pass')},flush=True)
    finally:capture.INIT_SCRIPT=original
    write_json(output/'result.json',{'kind':'PixelClockEngineeringProbe','acceptance':False,'rows':rows})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    run(p.parse_args().output)
