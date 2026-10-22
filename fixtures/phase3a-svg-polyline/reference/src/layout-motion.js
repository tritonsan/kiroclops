// Private reference implementation. Only rendered images are measurement data.
export function advanceLayout(nodes, targets, dt, draggedId) {
  return nodes.map(node=>{
    if(node.id===draggedId)return {...node};
    let {x,y,vx,vy}=node;const target=targets[node.id];
    const steps=Math.max(1,Math.ceil(dt/.004)),h=dt/steps;
    for(let i=0;i<steps;i++){vx+=(145*(target.x-x)-16*vx)*h;vy+=(145*(target.y-y)-16*vy)*h;x+=vx*h;y+=vy*h;}
    return {...node,x,y,vx,vy};
  });
}
