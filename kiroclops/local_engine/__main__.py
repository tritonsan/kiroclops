"""Local KiroClops engineering CLI; no cloud services or desktop takeover."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import sys

from kiroclops.visual.schema import VisualError, read_json, write_json


def declared_clicks(values):
    """Explicit CLI declarations; never infer or upgrade input provenance."""
    from kiroclops.visual.schema import finite,validate_inputs
    events=[]
    for value in values:
        if ':' not in value:raise VisualError('invalid_input_timeline','Use --click seconds:selector')
        seconds,selector=value.split(':',1)
        if not selector.strip():raise VisualError('invalid_input_timeline','A click selector is required')
        events.append({'time_s':finite(float(seconds),'declared click time',minimum=0),
                       'action':'click','selector':selector,'source':'explicit_cli_user_declaration'})
    return validate_inputs({'kind':'InputTimeline','schema_version':1,'provenance':'user_declared','events':events})


def declared_actions(values):
    """Explicit observed/declarative game inputs, with no hidden rule inference."""
    from kiroclops.visual.schema import finite,validate_inputs
    events=[]
    for value in values:
        parts=value.split(':')
        if len(parts)!=3 or parts[1] not in ('left','right','jump') or parts[2] not in ('press','release'):
            raise VisualError('invalid_input_timeline','Use --action seconds:left|right|jump:press|release')
        try:stamp=finite(float(parts[0]),'declared action time',minimum=0)
        except ValueError as exc:raise VisualError('invalid_input_timeline','An action time must be a finite nonnegative number') from exc
        events.append({'time_s':stamp,'action':parts[1],'pressed':parts[2]=='press','source':'explicit_cli_user_declaration'})
    return validate_inputs({'kind':'InputTimeline','schema_version':1,'provenance':'user_declared','events':events})


def doctor():
    from .environment import configure_environment
    environment=configure_environment()
    dependencies = {}
    for name in ('opencv-python', 'numpy', 'mcp', 'playwright'):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    executables = {name: shutil.which(name) for name in ('ffmpeg', 'ffprobe', 'kiro-cli')}
    root=Path(__file__).resolve().parents[2]
    chromium=None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as driver:
            path=Path(driver.chromium.executable_path)
            chromium={'path':str(path),'installed':path.is_file()}
    except (ImportError,RuntimeError,OSError) as exc:
        chromium={'installed':False,'error':str(exc)}
    from kiroclops.portable_v2.runtime import ENGINE_RELATIVE,ENGINE_SHA256
    from kiroclops.visual.schema import file_sha256
    from kiroclops.web_local.clock_runtime import capability_check
    capture_clock=capability_check()
    engine=root/ENGINE_RELATIVE
    godot={'path':str(engine),'installed':engine.is_file(),'verified':engine.is_file() and file_sha256(engine)==ENGINE_SHA256}
    ready=all(dependencies.values()) and all(executables.values()) and chromium['installed'] and godot['verified'] and capture_clock['status']=='ready'
    return {'status': 'ready' if ready else 'missing_dependency',
            'python': sys.version.split()[0], 'dependencies': dependencies, 'executables': executables,
            'chromium':chromium,'godot':godot,'environment':environment,'capture_clock':capture_clock,
            'model_started': False, 'cloud_used': False}


def main(argv=None):
    from .environment import configure_environment
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    for name in ('import-video', 'import-legacy'):
        command = commands.add_parser(name)
        command.add_argument('source', type=Path)
        command.add_argument('--output', type=Path, required=True)
        if name == 'import-video':
            command.add_argument('--inputs', type=Path)
        else:
            command.add_argument('--annotation', type=Path)
    for name in ('inspect', 'propose'):
        command = commands.add_parser(name)
        command.add_argument('package', type=Path)
        command.add_argument('--output', type=Path)
    analyze = commands.add_parser('analyze')
    analyze.add_argument('package', type=Path)
    analyze.add_argument('--selection', type=Path, required=True)
    analyze.add_argument('--contract', type=Path)
    analyze.add_argument('--output', type=Path, required=True)
    compare = commands.add_parser('compare')
    compare.add_argument('--reference', type=Path, required=True)
    compare.add_argument('--candidate', type=Path, required=True)
    compare.add_argument('--contract', type=Path, required=True)
    compare.add_argument('--output', type=Path, required=True)
    submit = commands.add_parser('submit')
    submit.add_argument('--store', type=Path, required=True)
    submit.add_argument('--request', type=Path, required=True)
    submit.add_argument('--key')
    for name in ('job', 'cancel'):
        command = commands.add_parser(name)
        command.add_argument('--store', type=Path, required=True)
        command.add_argument('job_id')
    prepare = commands.add_parser('prepare-session')
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--domain', choices=['panel', 'svg', 'game'], required=True)
    prepare.add_argument('--baseline', type=Path)
    prepare.add_argument('--reference', type=Path)
    prepare.add_argument('--request', required=True)
    prepare.add_argument('--mode', choices=['tools','baseline'], default='tools')
    prepare.add_argument('--stage', choices=['jump','ground'], default='jump')
    prepare.add_argument('--configure-kiro', action='store_true')
    trial = commands.add_parser('trial')
    # 'remeasure' (V6 plan WP2.3) is a runner action after a finalization failed in recording.
    trial.add_argument('operation', choices=['inspect','prepare','evaluate','finalize','report','job','cancel','remeasure'])
    trial.add_argument('--session', type=Path, required=True)
    trial.add_argument('--candidate')
    trial.add_argument('--async', dest='asynchronous', action='store_true', help='Submit a durable job and return its ID immediately')
    trial.add_argument('--job-id', help='ID returned by this session when submitting a job')
    trial.add_argument('--wait-seconds', type=float, help='For job polling only; wait between 0 and 60 seconds')
    report = commands.add_parser('report')
    report.add_argument('--session', type=Path, required=True)
    report.add_argument('--output', type=Path)
    review = commands.add_parser('review-prepare')
    review.add_argument('package', type=Path)
    review.add_argument('--observations', type=Path, required=True)
    review.add_argument('--tolerance', type=float, required=True)
    review.add_argument('--output', type=Path, required=True)
    reviewed = commands.add_parser('review-check')
    reviewed.add_argument('directory', type=Path)
    reviewed.add_argument('--marks', type=Path, required=True)
    intake = commands.add_parser('start')
    intake.add_argument('--video', type=Path, required=True)
    intake.add_argument('--project', type=Path, required=True)
    intake.add_argument('--request', required=True)
    intake.add_argument('--output', type=Path, required=True)
    intake.add_argument('--ui', action='store_true', help='Continue selection, binding and optional Kiro CLI launch in one loopback UI')
    input_options=intake.add_mutually_exclusive_group()
    input_options.add_argument('--inputs', type=Path)
    input_options.add_argument('--click',action='append',default=[],metavar='SECONDS:SELECTOR',help='Explicit reference click, e.g. 0.4:#open-panel; repeat for several clicks')
    input_options.add_argument('--action',action='append',default=[],metavar='SECONDS:ACTION:PRESS_OR_RELEASE',help='Explicit game input, e.g. 0.5:jump:press then 0.55:jump:release')
    selected = commands.add_parser('select')
    selected.add_argument('directory', type=Path)
    selected.add_argument('--selection', type=Path, required=True)
    transfer = commands.add_parser('transfer',help='Bind a selected local video to its supported target recorder')
    transfer.add_argument('directory',type=Path)
    transfer.add_argument('--output',type=Path,required=True)
    transfer.add_argument('--object',action='append',default=[],metavar='REFERENCE_ID=TARGET_ID')
    transfer.add_argument('--initial-nodes',type=int)
    transfer.add_argument('--selected-node')
    transfer.add_argument('--game-stage',choices=('jump','ground'))
    transfer.add_argument('--body-height',type=float,help='Explicit fixed reference character height in natural pixels')
    ui = commands.add_parser('ui', help='Resume the combined local selection and Kiro workflow')
    ui.add_argument('directory', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'doctor':
            result = doctor()
        elif args.command == 'import-video':
            from kiroclops.visual.package import import_video
            result = import_video(args.source, args.output, inputs=read_json(args.inputs) if args.inputs else None)
        elif args.command == 'import-legacy':
            from kiroclops.visual.compat import import_legacy_run, selection_from_legacy
            result = import_legacy_run(args.source, args.output)
            if args.annotation:
                write_json(args.output / 'selection.json', selection_from_legacy(args.source, args.annotation))
        elif args.command == 'inspect':
            from kiroclops.visual.package import load_package, timing_quality
            result = load_package(args.package)
            result = {k: v for k, v in result.items() if k != 'frames'} | {'frame_count': len(result['frames']), 'timing': timing_quality(result)}
        elif args.command == 'propose':
            from kiroclops.visual.selection import propose_objects
            result = propose_objects(args.package)
        elif args.command == 'analyze':
            from kiroclops.cloud.execution import measure_direct
            selection = read_json(args.selection)
            contract = read_json(args.contract) if args.contract else None
            measured = measure_direct(args.package, selection, contract)
            observations, events = measured['observations'], measured['events']
            result = {'schema_version': 1, 'kind': 'AnalysisResult', 'observations': observations, 'events': events,
                      'independent_review_status': 'pending'}
        elif args.command == 'compare':
            from kiroclops.visual.comparison import compare_observations
            reference, candidate = read_json(args.reference), read_json(args.candidate)
            result = compare_observations(reference.get('observations', reference), candidate.get('observations', candidate), read_json(args.contract))
        elif args.command == 'prepare-session':
            from .session import create_session
            baseline, reference = args.baseline, args.reference
            if args.domain != 'game':
                from kiroclops.web_local.specs import fixture_project, scenarios
                baseline = baseline or fixture_project(args.domain,'target')
                reference = reference or fixture_project(args.domain,'reference')
                experiment = {'calibration':scenarios(args.domain,'calibration'), 'validation':scenarios(args.domain,'validation')}
            else:
                if not baseline or not reference:
                    raise VisualError('missing_source', 'Game session requires explicit baseline and reference roots')
                experiment = {'stage':args.stage}
            session_domain = 'web_panel' if args.domain == 'panel' else args.domain
            result = create_session(args.output,session_domain,baseline,reference,args.request,mode=args.mode,experiment=experiment)
            if args.configure_kiro:
                from .launcher import configure
                configure(args.output)
            result = {'status':'prepared','session':str(args.output),'domain':args.domain,'mode':args.mode,'model_started':False}
        elif args.command == 'start':
            from .intake import prepare_intake
            inputs=read_json(args.inputs) if args.inputs else declared_clicks(args.click) if args.click else declared_actions(args.action) if args.action else None
            result=prepare_intake(args.video,args.project,args.request,args.output,inputs=inputs)
            if args.ui:
                from .intake_ui_server import serve
                serve(args.output)
        elif args.command == 'ui':
            from .intake_ui_server import serve
            serve(args.directory)
            return 0
        elif args.command == 'select':
            from .intake import resume_selection
            result=resume_selection(args.directory,read_json(args.selection))
        elif args.command == 'transfer':
            from .media_session import binding_preset,create_media_session
            mappings={}
            for item in args.object:
                if '=' not in item:raise VisualError('invalid_binding','Use --object reference_id=target_id')
                source,target=item.split('=',1)
                if not source or not target or source in mappings:raise VisualError('invalid_binding','Object bindings must be unique nonempty IDs')
                mappings[source]=target
            binding=binding_preset(args.directory,object_map=mappings,initial_node_count=args.initial_nodes,
                                   expected_selected_id=args.selected_node,game_stage=args.game_stage,body_height=args.body_height)
            result=create_media_session(args.directory,args.output,binding)
        elif args.command == 'report':
            from .reporting import build_report
            result=build_report(args.session, output=args.output)
        elif args.command == 'review-prepare':
            from .review import prepare_review
            result=prepare_review(args.package,read_json(args.observations),args.output,tolerance_px=args.tolerance)
        elif args.command == 'review-check':
            from .review import evaluate_review
            result=evaluate_review(args.directory,read_json(args.marks))
        elif args.command == 'trial':
            job_operations = ('prepare', 'evaluate', 'finalize', 'report', 'remeasure')
            if args.asynchronous and args.operation not in job_operations:
                raise VisualError('invalid_request', '--async is only valid for prepare/evaluate/finalize/report/remeasure')
            if (args.operation in ('job', 'cancel')) != (args.job_id is not None):
                raise VisualError('invalid_request', '--job-id is required for job/cancel and invalid for other operations')
            if args.candidate is not None and args.operation != 'finalize':
                raise VisualError('invalid_request', '--candidate is only valid for finalize')
            if args.wait_seconds is not None and (args.operation != 'job' or not math.isfinite(args.wait_seconds) or not 0 <= args.wait_seconds <= 60):
                raise VisualError('invalid_request', '--wait-seconds is only valid for job and must be between 0 and 60')
            from .session import Session
            session = Session(args.session)
            if args.operation == 'inspect':
                result = session.inspect()
            elif args.operation == 'job':
                result = session.get_job_feedback(args.job_id, wait_seconds=args.wait_seconds if args.wait_seconds is not None else 0)
            elif args.operation == 'cancel':
                result = session.cancel_job(args.job_id)
            else:
                submitted = session.submit(args.operation, candidate_id=args.candidate)
                result = submitted
                if not args.asynchronous and submitted.get('job_id') is not None:
                    while True:
                        job = session.get_job_feedback(submitted['job_id'], wait_seconds=10)
                        if job['status'] not in ('queued','running'):
                            result=job;break
        else:
            from kiroclops.cloud.execution import backend_for_store
            backend = backend_for_store(args.store)
            if args.command == 'submit':
                result = backend.submit(read_json(args.request), idempotency_key=args.key)
            elif args.command == 'job':
                result = backend.get_job(args.job_id)
            else:
                result = backend.cancel_job(args.job_id)
        if getattr(args, 'output', None) and args.command not in ('import-video', 'import-legacy','prepare-session','report','review-prepare','start','transfer'):
            write_json(args.output, result)
        # Large frame traces stay in the requested artifact instead of flooding a terminal.
        shown = ({'status': result.get('status', 'written'), 'output': str(args.output)}
                 if args.command in ('analyze', 'compare') else result)
        if args.command in ('import-video', 'import-legacy'):
            shown = {'status': 'imported', 'output': str(args.output), 'frames': len(result['frames']), 'package_sha256': result['package_sha256']}
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return 1 if result.get('status') in ('error','failed','cancelled','interrupted','timed_out','missing_dependency') else 0
    except (VisualError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'status': 'error', 'code': getattr(exc, 'code', 'invalid_request'), 'message': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
