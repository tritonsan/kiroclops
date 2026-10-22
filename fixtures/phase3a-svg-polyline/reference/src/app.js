import {advanceLayout} from './layout-motion.js';
const svg=document.querySelector('#diagram'),nodesGroup=document.querySelector('#nodes'),edgesGroup=document.querySelector('#edges');
const ns='http://www.w3.org/2000/svg';
const ideas=[['a','Core idea','#4dd0e1'],['b','Research','#ffd166'],['c','Prototype','#e68bc4'],['d','Feedback','#bfa5ff'],['e','Publish','#7ed3a0'],['f','Learn','#f3926c']];
// Route colors identify real link types; they do not encode coordinates or test state.
const routeColors={b:'#61bff7',c:'#f5a171',d:'#96d9a6',e:'#c8a4f4',f:'#eabe62'};
function routedPoints(core,node){
  const side=node.id.charCodeAt(0)%2 ? 1 : -1;
  const lane=core.x+side*(112+8*(node.id.charCodeAt(0)-98));
  const portY=({b:-16,c:0,d:16,e:-8,f:8})[node.id]??0;
  // Keep a real center attachment inside the card; visible side ports are separate.
  return [[core.x,core.y],[core.x,core.y+portY],[lane,core.y+portY],[lane,node.y],[node.x,node.y]].map(p=>p.join(',')).join(' ');
}
let nodes=[],drag=null,selected=null,last=performance.now();
const initialCount=Math.max(3,Math.min(6,Number(new URLSearchParams(location.search).get('nodes'))||3));
function layout(){const w=svg.clientWidth,h=svg.clientHeight,cx=w/2,cy=h/2;const result={a:{x:cx,y:cy}};const others=nodes.filter(n=>n.id!=='a');others.forEach((n,i)=>{const angle=-Math.PI/2+i*2*Math.PI/others.length;result[n.id]={x:cx+Math.cos(angle)*Math.min(w*.29,260),y:cy+Math.sin(angle)*Math.min(h*.31,150)};});return result;}
function make(tag,attrs){const e=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v));return e;}
function redrawStructure(){nodesGroup.replaceChildren();edgesGroup.replaceChildren();for(const node of nodes){const core=nodes.find(n=>n.id==='a');if(node.id!=='a')edgesGroup.append(make('polyline',{'data-edge':`a-${node.id}`,'data-source':'a','data-target':node.id,'class':'edge','style':`stroke:${routeColors[node.id]}`,points:routedPoints(core,node)}));const [id,label,color]=ideas.find(i=>i[0]===node.id);const g=make('g',{'data-node-id':id,'class':'node'+(selected===id?' selected':''),'transform':`translate(${node.x} ${node.y})`,'tabindex':'0','role':'button','aria-label':label});g.append(make('rect',{x:-69,y:-29,width:138,height:58,rx:11}));const icon=make('path',{fill:color,d:'M-54 -13h22v26h-22z M-51 -10v6h6v-6z','fill-rule':'evenodd'});g.append(icon);const text=make('text',{x:-22,y:5});text.textContent=label;g.append(text);g.addEventListener('pointerdown',e=>{selected=id;g.setPointerCapture(e.pointerId);const box=svg.getBoundingClientRect(),current=nodes.find(n=>n.id===id);drag={id,pointer:e.pointerId,dx:e.clientX-box.left-current.x,dy:e.clientY-box.top-current.y};});g.addEventListener('pointermove',e=>{if(!drag||drag.id!==id||drag.pointer!==e.pointerId)return;const box=svg.getBoundingClientRect(),current=nodes.find(n=>n.id===id);current.x=Math.max(75,Math.min(svg.clientWidth-75,e.clientX-box.left-drag.dx));current.y=Math.max(35,Math.min(svg.clientHeight-35,e.clientY-box.top-drag.dy));current.vx=0;current.vy=0;});const release=e=>{if(drag&&drag.id===id&&drag.pointer===e.pointerId){drag=null;if(g.hasPointerCapture(e.pointerId))g.releasePointerCapture(e.pointerId);}};g.addEventListener('pointerup',release);g.addEventListener('pointercancel',release);nodesGroup.append(g);}}
function reset(){drag=null;selected=null;nodes=ideas.slice(0,initialCount).map(([id])=>({id,x:svg.clientWidth/2,y:svg.clientHeight/2,vx:0,vy:0}));const targets=layout();nodes=nodes.map(n=>({...n,...targets[n.id]}));redrawStructure();}
document.querySelector('#reset').onclick=reset;
document.querySelector('#add-node').onclick=()=>{if(nodes.length>=6)return;const [id]=ideas[nodes.length];nodes.push({id,x:svg.clientWidth/2,y:svg.clientHeight/2,vx:0,vy:0});const fresh=nodes[nodes.length-1],goal=layout()[id];fresh.x=goal.x+(goal.x-svg.clientWidth/2)*.1;fresh.y=goal.y+(goal.y-svg.clientHeight/2)*.1;drag=null;redrawStructure();};
document.querySelector('#remove-node').onclick=()=>{if(nodes.length<=3)return;const old=nodes.pop();if(selected===old.id)selected=null;drag=null;redrawStructure();};
function render(now){const dt=Math.min(.05,Math.max(0,(now-last)/1000));last=now;nodes=advanceLayout(nodes,layout(),dt,drag?.id??null);for(const n of nodes){const g=nodesGroup.querySelector(`[data-node-id="${n.id}"]`);if(g){g.setAttribute('transform',`translate(${n.x} ${n.y})`);g.classList.toggle('selected',selected===n.id);}}for(const line of edgesGroup.children){const a=nodes.find(n=>n.id===line.dataset.source),b=nodes.find(n=>n.id===line.dataset.target);if(a&&b){line.setAttribute('points',routedPoints(a,b));}}requestAnimationFrame(render);}
reset();requestAnimationFrame(render);
