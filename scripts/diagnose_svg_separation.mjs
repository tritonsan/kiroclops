// Post-hoc branch ablation of an already sealed candidate, not an experiment rerun.
// Imports the actual reviewed source and an in-memory variant omitting one call.
// It never writes application code or feeds results to Kiro.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath, pathToFileURL} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const session = path.join(root, 'artifacts/phase3a/completion-paired-cohort-01/sessions/svg-baseline-01/svg');
const input = path.join(session, 'candidates/candidate-02/source/src/layout-motion.js');
const output = path.join(root, 'artifacts/phase3a/completion-20260927/svg-baseline-01-separation-ablation.json');
if (fs.existsSync(output)) throw new Error('Fresh diagnostic output required');
const source = fs.readFileSync(input, 'utf8');
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
if (sha(source) !== 'a0f932a221db9c7b32235318900a2fbbd58b2e1f3b48823bea819fb1c109e579') throw new Error('Sealed source changed');
const call = '  separate(next, targets || {}, draggedId);';
if (source.split(call).length !== 2) throw new Error('Expected one separation call');
const full = await import(pathToFileURL(input).href);
const ablated = await import('data:text/javascript;base64,' + Buffer.from(source.replace(call, '  // Branch omitted for a post-hoc diagnostic only.')).toString('base64'));

function layout(ids, w, h) {
  const targets = {a: {x: w/2, y: h/2}};
  const others = ids.filter(id => id !== 'a');
  others.forEach((id, i) => {
    const angle = -Math.PI/2 + i * 2 * Math.PI/others.length;
    targets[id] = {x: w/2 + Math.cos(angle)*Math.min(w*.29,260),
      y: h/2 + Math.sin(angle)*Math.min(h*.31,150)};
  });
  return targets;
}
const rows = [];
for (const width of [768,1280]) {
  // Source CSS yields482px drawing height for the640px viewport.
  const height = 482, ids = ['a','b','c','d','e'];
  const before = layout(ids,width,height), target = layout([...ids,'f'],width,height);
  const nodes = ids.map(id=>({id,...before[id],vx:0,vy:0}));
  nodes.push({id:'f',x:target.f.x+(target.f.x-width/2)*.1,
    y:target.f.y+(target.f.y-height/2)*.1,vx:0,vy:0});
  let a=structuredClone(nodes), b=structuredClone(nodes);
  const samples=[];
  for (let step=1;step<=18;step++) {
    a=full.advanceLayout(a,target,1/60,null);
    b=ablated.advanceLayout(b,target,1/60,null);
    const f=a.find(n=>n.id==='f'), g=b.find(n=>n.id==='f');
    samples.push({time_s:step/60, f_full:{x:f.x,y:f.y}, f_no_separation:{x:g.x,y:g.y},
      difference_px:{x:f.x-g.x,y:f.y-g.y},distance_px:Math.hypot(f.x-g.x,f.y-g.y)});
  }
  rows.push({viewport_width:width,svg_height_assumption:height,
    f_rmse_between_variants_px:Math.sqrt(samples.reduce((sum,s)=>sum+s.distance_px**2,0)/samples.length),samples});
}
if (sha(fs.readFileSync(input)) !== sha(source)) throw new Error('Candidate changed during diagnostic');
fs.writeFileSync(output,JSON.stringify({kind:'SealedSourceBranchAblation',source_sha256:sha(source),
  candidate_sha256:'64230818161af4e2557167ed9c05a48c0265c6fe5a2a9110a9dbce865f041a1a',
  source_css_and_app_layout_read:true, live_browser_run:false, model_run:false,
  operator_candidate_edit:false, primary_outcome_modified:false,
  reference_physics_source_used:false, fixed_dt_assumption_s:1/60,
  conclusion:'Removing only the separation call changes the newborn f vertical path. This establishes the branch effect in a source-derived simulation; it does not replace independent image accuracy or prove every heldout failure.',
  producer_sha256:sha(fs.readFileSync(fileURLToPath(import.meta.url))),rows},null,2)+'\n');
console.log(JSON.stringify(rows.map(r=>({width:r.viewport_width,
  initial_f_dy_px:r.samples[0].difference_px.y,rmse_between_variants_px:r.f_rmse_between_variants_px}))));
