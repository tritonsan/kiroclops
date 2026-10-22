"""Unsigned event proposals from independently marked/segmented raw anchors.

This review aid does not read the EventSet under review or import its detector.
It scans complete pixel-anchor series and produces candidate intervals for a
reviewer to inspect against raw sequences. It never emits pass or a receipt.
"""
from pathlib import Path
import argparse
import sys
import statistics

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from kiroclops.visual.schema import read_json,write_json,file_sha256


def candidates(rows,objects,rest_duration=.25):
    t=np.asarray([r['time_s'] for r in rows]);p=np.asarray([[r['points'][objects[0]][k] for k in ('x','y')] for r in rows])
    if len(objects)==2:p-=np.asarray([[r['points'][objects[1]][k] for k in ('x','y')] for r in rows])
    axis=int(np.argmax(np.ptp(p,axis=0)));out=[];cursor=0;search_from=0
    def emit(kind,a,b,**extra):
        out.append({'type':kind,'object_ids':objects,'indices':[int(a),int(b)],
            'observed_interval_s':[float(t[a]),float(t[b])],**extra})
    while cursor<len(t)-1:
        departures=np.where(np.linalg.norm(p[search_from:]-p[cursor],axis=1)>1.5)[0]
        if not len(departures):break
        start=search_from+int(departures[0]);emit('motion_start',max(cursor,start-1),start)
        quiet=None;quiet_end=None
        for i in range(start+1,len(t)):
            end=int(np.searchsorted(t,t[i]+rest_duration))
            if end>=len(t):break
            if np.linalg.norm(np.ptp(p[i:end+1],axis=0))<=1.5:
                quiet,quiet_end=i,end;break
        finish=quiet_end if quiet_end is not None else len(t)-1
        # Independent sampled extrema: require a visible >1.5px departure from
        # the last extreme before calling a sign change, not every AA flicker.
        direction=np.sign(p[start,axis]-p[max(cursor,start-1),axis]);peak=start;turn=None
        for i in range(start+1,finish+1):
            delta=p[i,axis]-p[i-1,axis]
            if delta==0:continue
            if direction==0:direction=np.sign(delta);peak=i
            if np.sign(delta)==direction:
                if direction*(p[i,axis]-p[peak,axis])>=0:peak=i
                turn=None
            else:
                if turn is None:turn=i
                if direction*(p[peak,axis]-p[i,axis])>1.5:
                    emit('reversal',turn-1,turn);direction=np.sign(delta);peak=i;turn=None
        if quiet is None:break
        emit('stop',quiet,min(quiet+1,len(t)-1),support_indices=[quiet,quiet_end])
        emit('settled',quiet,min(quiet+1,len(t)-1),support_indices=[quiet,quiet_end])
        endpoint=np.median(p[quiet:quiet_end+1],axis=0);vector=endpoint-p[start-1];length=np.linalg.norm(vector)
        if length>1.5:
            projected=(p[start-1:quiet+1]-endpoint)@(vector/length)
            excursion=np.where(projected>1.5)[0]
            if len(excursion):
                first=start-1+int(excursion[0]);emit('overshoot',max(start-1,first-1),first,
                    observed_endpoint_px=endpoint.tolist(),observed_amplitude_px=float(projected.max()))
        # A later departure is relative to the same observed rest band's first
        # position, but only after its supporting quiet interval is visible.
        cursor=quiet;search_from=quiet_end+1
    return out


def main():
    parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path)
    parser.add_argument('--source',default='raw-points.json');parser.add_argument('--output',default='event-proposals.json')
    parser.add_argument('--rest-duration',type=float,default=.25);args=parser.parse_args()
    for path in sorted(args.directory.glob('*/'+args.source)):
        data=read_json(path);events=[]
        for ids in (['bookmark'],['handle'],['star'],['bookmark','handle'],['star','handle']):
            events.extend(candidates(data['rows'],ids,args.rest_duration))
        result={'kind':'UnsignedIndependentEventProposals','status':'unreviewed','raw_points_sha256':file_sha256(path),
            'helper_sha256':file_sha256(__file__),'events':sorted(events,key=lambda e:(e['observed_interval_s'][0],e['object_ids'],e['type'])),
            'limitation':'Raw color-bound candidate times need visual review. Small raster excursions or input-interrupted movement may remain ambiguous.'}
        write_json(path.parent/args.output,result);print(path.parent.name,len(events))


if __name__=='__main__':main()
