// Private reference implementation; pixels/input receipts are its public data.
function spring(position,velocity,target,dt,stiffness,damping){
  const steps=Math.max(1,Math.ceil(dt/.004)),h=dt/steps;
  for(let i=0;i<steps;i++){velocity+=(stiffness*(target-position)-damping*velocity)*h;position+=velocity*h;}
  return {position,velocity};
}
export function advanceMotion(state, target, dt, input) {
  let x=state.x,velocity=state.velocity;
  const steps=Math.max(1,Math.ceil(dt/.004)),h=dt/steps;
  for(let i=0;i<steps;i++){velocity+=(155*(target-x)-17*velocity)*h;x+=velocity*h;}
  const changed=state.sequenceOpen!==undefined&&state.sequenceOpen!==input.open;
  const elapsed=(changed?0:(state.sequenceElapsed||0))+dt;
  const titleDelay=input.open ? .08 : .12,contentDelay=input.open ? .16 : .04;
  const active=delay=>Math.max(0,Math.min(dt,elapsed-delay));
  const title=spring(state.titleY,state.titleVelocity,input.open?0:24,active(titleDelay),220,20);
  const content=spring(state.contentY,state.contentVelocity,input.open?0:40,active(contentDelay),190,18);
  return {...state,x,velocity,titleY:title.position,titleVelocity:title.velocity,
    contentY:content.position,contentVelocity:content.velocity,sequenceOpen:input.open,sequenceElapsed:elapsed};
}
