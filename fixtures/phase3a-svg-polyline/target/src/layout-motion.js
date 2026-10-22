// Editable layout response; independent live targets, positions in CSS pixels.
export function advanceLayout(nodes, targets, dt, draggedId) {
  const blend=1-Math.exp(-2.8*dt);
  return nodes.map(n=>n.id===draggedId?{...n}:{...n,
    x:n.x+(targets[n.id].x-n.x)*blend,
    y:n.y+(targets[n.id].y-n.y)*blend,vx:0,vy:0});
}
