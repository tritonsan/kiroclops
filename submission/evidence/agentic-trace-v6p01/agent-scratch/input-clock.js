// Input clock: remembers when the latest discrete input (click, key, pointer
// release/cancel, resize) happened, and the timestamp of the current
// animation frame, both on the performance.now() clock in milliseconds.
// The motion law uses them to start a new mode at the input instant instead
// of the previous frame boundary, so motion does not depend on where inside
// a frame the input landed.
let lastInputMs = -Infinity;
let frameMs = NaN;

const hasWindow = typeof window !== 'undefined' && typeof performance !== 'undefined';

function markInput(event) {
  const now = performance.now();
  const stamp = event && Number.isFinite(event.timeStamp) ? event.timeStamp : now;
  // Event timestamps share the performance clock in current browsers; fall
  // back to the handler time if a stamp is implausible.
  lastInputMs = stamp <= now + 1 && stamp > now - 1000 ? stamp : now;
}

if (hasWindow) {
  for (const type of ['click', 'keydown', 'pointerup', 'pointercancel']) {
    window.addEventListener(type, markInput, true);
  }
  window.addEventListener('resize', markInput);
  // Registered before the app's render loop, so each frame this runs first
  // and records the same timestamp the renderer receives.
  if (typeof requestAnimationFrame === 'function') {
    const tick = (t) => { frameMs = t; requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  }
}

// Seconds of a `step` (seconds, ending at the current frame) that elapsed
// before the latest input. 0 when the input is unknown or precedes the step.
export function inputOffsetInStep(step) {
  if (!hasWindow || !(step > 0) || !Number.isFinite(frameMs) || !Number.isFinite(lastInputMs)) return 0;
  const now = performance.now();
  if (Math.abs(now - frameMs) > 100) return 0; // stale frame clock
  const stepStart = frameMs - step * 1000;
  if (lastInputMs <= stepStart) return 0;
  const offset = Math.min(step * 1000, lastInputMs - stepStart) / 1000;
  return Math.max(0, Math.min(step, offset));
}
