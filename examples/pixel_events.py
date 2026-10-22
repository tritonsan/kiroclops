"""Standalone pixel event extraction. No game, browser, MCP or Kiro dependency."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiroclops.visual.schema import read_json,write_json
from kiroclops.visual.tracking import analyze_package
from kiroclops.visual.events import extract_events


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package',type=Path);parser.add_argument('--selection',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
    selection=read_json(args.selection)
    observed=analyze_package(args.package,selection,{'min_valid_ratio':.95,'require_static_camera':True})
    events=extract_events(observed,selection)
    write_json(args.output,{'observations':observed,'events':events})
    print(json.dumps({'measurement_status':observed['status'],'objects':len(observed['objects']),
        'events':len(events['events']),'relations':len(events['relations']),'output':str(args.output)},indent=2))


if __name__=='__main__':main()
