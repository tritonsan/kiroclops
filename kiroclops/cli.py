from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kiroclops")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="Capture a controlled Godot experiment")
    capture.add_argument("--game", choices=["tiny", "slay"], required=True)
    capture.add_argument("--variant", choices=["reference", "before", "after"], required=True)
    capture.add_argument("--hold", type=int, required=True)
    capture.add_argument("--repeat", type=int, default=1)
    capture.add_argument("--spawn-offset", type=int, default=0)
    capture.add_argument("--output", type=Path)
    capture.add_argument("--normal-level", action="store_true")
    capture.add_argument("--no-video", action="store_true")
    capture.add_argument("--import-assets", action="store_true")
    annotate = sub.add_parser("annotate", help="Select a stable body patch and independent checkpoints")
    annotate.add_argument("run", type=Path)
    annotate.add_argument("--output", required=True, type=Path)
    annotate.add_argument("--roi", type=int, nargs=4)
    annotate.add_argument("--anchor", type=float, nargs=2)
    annotate.add_argument("--body-height", type=float)
    annotate.add_argument("--background", type=int, nargs=4, action="append")
    annotate.add_argument("--frame", type=int, default=0)
    annotate.add_argument("--controls", type=int, nargs="+", metavar="FRAME",
                          help="Click independent checkpoints in an existing annotation file")
    annotate.add_argument("--events", type=int, nargs=3, metavar=("TAKEOFF", "APEX", "LANDING"),
                          help="Independently observed event frames; used with --controls")
    analyze = sub.add_parser("analyze")
    analyze.add_argument("run", type=Path)
    analyze.add_argument("--annotation", type=Path, required=True)
    analyze.add_argument("--output", type=Path)
    fit = sub.add_parser("fit")
    fit.add_argument("analyses", nargs="+", type=Path)
    fit.add_argument("--output", type=Path, required=True)
    compare = sub.add_parser("compare")
    compare.add_argument("--reference", nargs="+", type=Path, required=True)
    compare.add_argument("--before", nargs="+", type=Path, required=True)
    compare.add_argument("--after", nargs="+", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    report = sub.add_parser("report")
    report.add_argument("runs", nargs="+", type=Path)
    report.add_argument("--output", required=True, type=Path)
    report.add_argument("--profile", type=Path)
    report.add_argument("--comparisons", nargs="*", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            from .capture import capture_run, import_project
            if args.import_assets:
                import_project(args.game, args.variant)
            result = capture_run(args.game, args.variant, args.hold, repeat=args.repeat,
                                 spawn_offset=args.spawn_offset, output_dir=args.output,
                                 normal_level=args.normal_level, video=not args.no_video)
        elif args.command == "annotate":
            from .annotation import annotate_run, annotate_controls
            if args.controls:
                result = annotate_controls(args.run, args.output, args.controls, event_frames=args.events)
            else:
                result = annotate_run(args.run, args.output, roi=args.roi,
                                      anchor_offset=args.anchor, body_height_px=args.body_height,
                                      background_rois=args.background, template_frame=args.frame,
                                      interactive=args.roi is None)
        elif args.command == "analyze":
            from .analysis import analyze_run
            result = analyze_run(args.run, args.annotation, args.output)
        elif args.command == "fit":
            from .fitting import fit_profile
            result = fit_profile(args.analyses, args.output)
        elif args.command == "compare":
            from .comparison import compare_runs
            from .common import write_json
            result = compare_runs(args.reference, args.before, args.after)
            write_json(args.output, result)
        else:
            from .report import generate_report
            result = generate_report(args.runs, args.output, profile_path=args.profile,
                                     comparison_paths=args.comparisons)
        if isinstance(result, Path):
            print(result)
        else:
            summary = {k: v for k, v in result.items() if k not in {"frames", "curve", "observations"}}
            print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"KiroClops: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
