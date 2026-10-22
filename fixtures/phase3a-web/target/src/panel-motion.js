// Editable motion law. CSS pixels and seconds; called during real interaction.
// input.open is the requested mode; input.dragging means the pointer owns x.
// Keep finite x/velocity, titleY/titleVelocity and contentY/contentVelocity.
// Additional state fields persist between calls. Rendering is in app.js.
export function advanceMotion(state,target,dt,input){
  const alpha=1-Math.exp(-3*dt);
  const x=state.x+(target-state.x)*alpha;
  const titleY=state.titleY+((input.open?0:24)-state.titleY)*alpha;
  const contentY=state.contentY+((input.open?0:40)-state.contentY)*alpha;
  return {...state,x,velocity:(x-state.x)/Math.max(dt,1e-6),
    titleY,titleVelocity:(titleY-state.titleY)/Math.max(dt,1e-6),
    contentY,contentVelocity:(contentY-state.contentY)/Math.max(dt,1e-6)};
}
