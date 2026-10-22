import { advanceMotion } from './panel-motion.js';
const panel=document.querySelector('#panel'),track=document.querySelector('#track'),handle=document.querySelector('#panel-handle');
const title=document.querySelector('.panel-title'),content=document.querySelector('.panel-content');
const initialState=x=>({x,velocity:0,titleY:24,titleVelocity:0,contentY:40,contentVelocity:0});
let state=initialState(0),target=0,open=false,drag=null,last=performance.now();
const limits=()=>({min:12,max:Math.max(12,track.clientWidth-panel.offsetWidth-76)});
function setOpen(value){open=value;target=value?limits().max:limits().min;}
document.querySelector('#open-panel').onclick=()=>setOpen(true);
document.querySelector('#close-panel').onclick=()=>setOpen(false);
document.querySelector('#reset').onclick=()=>{drag=null;open=false;state=initialState(limits().min);target=state.x;};
handle.addEventListener('pointerdown',e=>{handle.setPointerCapture(e.pointerId);drag={pointer:e.pointerId,offset:e.clientX-track.getBoundingClientRect().left-state.x};});
handle.addEventListener('pointermove',e=>{if(!drag||drag.pointer!==e.pointerId)return;const b=limits();state={...state,x:Math.max(b.min,Math.min(b.max,e.clientX-track.getBoundingClientRect().left-drag.offset)),velocity:0};});
function release(e){if(!drag||drag.pointer!==e.pointerId)return;drag=null;if(handle.hasPointerCapture(e.pointerId))handle.releasePointerCapture(e.pointerId);setOpen(state.x>(limits().min+limits().max)/2);}
handle.addEventListener('pointerup',release);handle.addEventListener('pointercancel',release);
handle.addEventListener('keydown',e=>{if(e.key==='ArrowRight')setOpen(true);if(e.key==='ArrowLeft')setOpen(false);});
window.addEventListener('resize',()=>{if(!drag)setOpen(open);});
state.x=limits().min;target=state.x;
function render(now){
  const dt=Math.min(.050,Math.max(0,(now-last)/1000));last=now;
  const next=advanceMotion(state,target,dt,{open,dragging:!!drag});
  if(!['x','velocity','titleY','titleVelocity','contentY','contentVelocity'].every(k=>Number.isFinite(next[k])))throw new Error('Motion controller must return finite position and velocity for panel, title and content');
  state=drag?{...next,x:state.x,velocity:0}:next;
  panel.style.transform=`translateX(${state.x}px)`;
  title.style.transform=`translateY(${state.titleY}px)`;
  content.style.transform=`translateY(${state.contentY}px)`;
  handle.setAttribute('aria-valuemin',String(limits().min));
  handle.setAttribute('aria-valuemax',String(limits().max));
  handle.setAttribute('aria-valuenow',String(Math.round(state.x)));
  requestAnimationFrame(render);
}
requestAnimationFrame(render);
