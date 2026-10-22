"""Private browser evidence. Never an input to pixel tracking/profile fitting."""
from __future__ import annotations
import math

INIT_SCRIPT = r"""(() => {
  const state={commands:[],events:[],samples:[],activeCommand:null,enabled:false};
  Object.defineProperty(window,'__kcPrivate',{value:state,writable:false});
  const now=()=> (performance.timeOrigin+performance.now())/1000;
  for(const type of ['pointerdown','pointermove','pointerup','pointercancel','click']){
    document.addEventListener(type,e=>{
      if(!state.enabled||state.activeCommand===null)return;
      state.events.push({epoch_s:now(),event:type,command:state.activeCommand,
        trusted:e.isTrusted,target:e.target.closest('[id],[data-node-id]')?.id||e.target.closest('[data-node-id]')?.dataset.nodeId||null,
        client_x:e.clientX,client_y:e.clientY,pointer_id:e.pointerId??null});
    },true);
  }
  const box=e=>{const b=e.getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height};};
  function tick(){if(state.enabled){
    const panel=document.querySelector('#panel');
    const row={epoch_s:now(),objects:{},edges:[],selected:[]};
    if(panel){
      for(const [id,selector] of [['panel','#panel'],['handle','#panel-handle svg'],['bookmark','.bookmark'],['star','.star']]){
        const e=document.querySelector(selector);if(e)row.objects[id]=box(e);
      }
      row.pointer_capture=[...document.querySelectorAll('*')].some(e=>{for(let id=1;id<=5;id++){if(e.hasPointerCapture?.(id))return true;}return false;});
    }
    for(const e of document.querySelectorAll('[data-node-id]'))row.objects[e.dataset.nodeId]=box(e);
    row.selected=[...document.querySelectorAll('[data-node-id].selected')].map(e=>e.dataset.nodeId);
    for(const e of document.querySelectorAll('[data-edge]')){
      const edge={id:e.dataset.edge,source:e.dataset.source,target:e.dataset.target,
        element:e.localName,endpoint_source:'private_svg_geometry_api'};
      try{
        if(!['line','polyline','path'].includes(e.localName))throw Error('unsupported_edge_geometry');
        const m=e.getScreenCTM(),length=e.getTotalLength();
        if(!m||!Number.isFinite(length)||length<=0)throw Error('empty_or_nonfinite_edge_geometry');
        const screen=q=>({x:m.a*q.x+m.c*q.y+m.e,y:m.b*q.x+m.d*q.y+m.f});
        edge.start=screen(e.getPointAtLength(0));edge.end=screen(e.getPointAtLength(length));
        if(![edge.start.x,edge.start.y,edge.end.x,edge.end.y].every(Number.isFinite))throw Error('nonfinite_edge_endpoint');
        edge.geometry_status='pass';
      }catch(error){edge.geometry_status='fail';edge.reason=String(error.message||error);}
      row.edges.push(edge);
    }
    state.samples.push(row);
  }requestAnimationFrame(tick);}requestAnimationFrame(tick);
})();"""


def validate_oracle(record, domain):
    """Finite functional/structural checks, separately labeled native truth."""
    samples=record.get("samples", [])
    errors=[]
    if len(samples)<10:
        errors.append("insufficient_private_samples")
    required={"panel","handle","bookmark","star"} if domain=="panel" else {"a","b","c"}
    for row in samples:
        objects=row.get("objects",{})
        if not required.issubset(objects):
            errors.append("required_object_missing")
        if any(not all(isinstance(b.get(k),(int,float)) and math.isfinite(b[k]) for k in ("x","y","width","height")) for b in objects.values()):
            errors.append("non_finite_geometry")
        if domain=="svg":
            if not 3<=len(objects)<=6:
                errors.append("node_count_outside_bounds")
            ids=[edge.get("id") for edge in row.get("edges",[])]
            if len(set(ids))!=len(ids) or len(ids)!=max(0,len(objects)-1):
                errors.append("edge_identity_or_count")
            for edge in row.get("edges",[]):
                if (edge.get('geometry_status') not in (None,'pass') or
                    any(not isinstance(edge.get(endpoint),dict) or
                        any(not isinstance(edge[endpoint].get(axis),(int,float)) or not math.isfinite(edge[endpoint][axis])
                            for axis in ('x','y')) for endpoint in ('start','end'))):
                    errors.append('edge_geometry_unobservable')
                    continue
                for key,endpoint in (("source","start"),("target","end")):
                    obj=objects.get(edge.get(key))
                    if not obj:
                        errors.append("edge_endpoint_missing")
                        continue
                    center=(obj["x"]+obj["width"]/2,obj["y"]+obj["height"]/2)
                    point=edge[endpoint]
                    if math.hypot(point["x"]-center[0],point["y"]-center[1])>2.:
                        errors.append("edge_detached")
            if not set(row.get("selected",[])).issubset(objects):
                errors.append("selected_node_missing")
    if domain=="panel" and samples and samples[-1].get("pointer_capture"):
        errors.append("pointer_capture_not_released")
    if domain=='svg' and samples:
        scenario=record.get('scenario',{})
        if len(samples[0].get('objects',{}))!=scenario.get('initial_node_count'):
            errors.append('initial_node_count_mismatch')
        if len(samples[-1].get('objects',{}))!=scenario.get('expected_final_node_count'):
            errors.append('final_node_count_mismatch')
        expected=scenario.get('expected_selected_id')
        if expected:
            if samples[-1].get('selected')!=[expected]:errors.append('final_selection_not_preserved')
            selection_seen=False
            for row in samples:
                if row.get('selected')==[expected]:selection_seen=True
                if selection_seen and expected in row.get('objects',{}) and row.get('selected')!=[expected]:
                    errors.append('selection_lost_during_structure_change')
    return {"status":"fail" if errors else "pass","checks_source":"private_dom_oracle",
            "sample_count":len(samples),"failures":sorted(set(errors)),
            "scope":"Finite visible-object/edge identity, attachment and pointer-release checks; not pixel measurement."}
