"""Build a bounded local engineering kit; no accounts, histories or old solutions."""
import argparse
from pathlib import Path
import json
import re
import shutil
import sys
import zipfile
from urllib.parse import unquote,urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from kiroclops.visual.schema import VisualError,file_sha256,read_json,write_json
from kiroclops.local_engine.session import source_hashes
from kiroclops.portable_v2.specs import scenarios,run_name


PACKAGED_DOCUMENTS=('PHASE3A_PLAN.md','PHASE3A_STATUS.md','LOCAL_ENGINEERING.md','VISUAL_API.md',
    'PRODUCT_STRATEGY.md','ROADMAP.md','docs/LOCAL_MEDIA_SESSIONS.md','docs/CAPTURE_CLOCK_RUNTIME.md')
PACKAGED_SCRIPTS=('bootstrap_local.ps1','materialize_local_kit.py','prepare_local_web_references.py',
    'build_local_cohort_report.py','check_local_report.py','package_local.py')
MARKDOWN_LINK=re.compile(r'(?<!!)\[([^\]\n]+)\]\(([^)\n]+)\)')
DOC_NOTICE='> Kit scope: historical repository evidence is not bundled. Unavailable local links below are marked as repository-only references.\n\n'


def finalize_documentation(output):
    """Keep usable local links; label omitted historical evidence without copying it."""
    root=Path(output).resolve();changed=[];omitted=[]
    for relative in PACKAGED_DOCUMENTS:
        path=root/relative;text=path.read_text(encoding='utf-8');original=text
        def link(match):
            label,target=match.groups();target=target.strip()
            if target.startswith('<') and target.endswith('>'):target=target[1:-1]
            parsed=urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:return match.group(0)
            destination=(path.parent/unquote(parsed.path)).resolve()
            if destination.is_relative_to(root) and destination.is_file():return match.group(0)
            omitted.append({'document':relative,'label':label,'repository_target':target})
            return f'{label} (`{target}`; repository-only, not bundled)'
        text=MARKDOWN_LINK.sub(link,text)
        if text!=original:
            if not text.startswith(DOC_NOTICE):text=DOC_NOTICE+text
            path.write_text(text,encoding='utf-8');changed.append(relative)
    return {'derived_documents':changed,'omitted_repository_links':omitted,
            'historical_evidence_copied':False}


def build(output,*,include_engine=False,archive=False):
    output=Path(output).resolve()
    if output.exists():raise VisualError('output_exists','Use a fresh engineering kit directory')
    for name in ('kiroclops','fixtures','adapters','config','scripts','vendor'):
        if output.is_relative_to(ROOT/name):raise VisualError('unsafe_path','Kit cannot be built inside a source directory')
    output.mkdir(parents=True);originals={};derived=[];source_bindings={}
    def copy_file(source,relative=None):
        source=Path(source).resolve();relative=relative or source.relative_to(ROOT)
        if not source.is_relative_to(ROOT):raise VisualError('unsafe_path','Kit inputs must belong to this workspace')
        destination=(output/relative).resolve()
        if not destination.is_relative_to(output):raise VisualError('unsafe_path','Kit output escapes its directory')
        expected=file_sha256(source)
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,destination)
        if file_sha256(source)!=expected or file_sha256(destination)!=expected:raise VisualError('source_changed','Kit input changed while copying')
        source_bindings[source]=expected;originals[str(relative).replace('\\','/')]=expected
    def copy_tree(source,relative=None):
        source=Path(source);relative=relative or source.relative_to(ROOT)
        for name in source_hashes(source):copy_file(source/name,Path(relative)/name)
    def portable(value):
        if isinstance(value,dict):return {k:portable(v) for k,v in value.items()}
        if isinstance(value,list):return [portable(v) for v in value]
        if isinstance(value,str):
            normalized=value.replace('\\','/');prefix=ROOT.as_posix()+'/'
            if normalized.startswith(prefix):return '@PACKAGE_ROOT@/'+normalized[len(prefix):]
            if re.match(r'^[A-Za-z]:/',normalized) or normalized.startswith('//'):
                return '[original host path omitted]/'+Path(normalized).name
        return value
    def derive_json(source,relative):
        copy_file(source,relative);write_json(output/relative,portable(read_json(source)));derived.append(str(relative).replace('\\','/'))
    for name in ('kiroclops','fixtures/phase3a-web','fixtures/phase3a-svg','fixtures/phase3a-svg-polyline','adapters'):
        copy_tree(ROOT/name)
    for name in ('pyproject.toml','requirements-local.lock',*PACKAGED_DOCUMENTS):
        copy_file(ROOT/name)
    copy_file(ROOT/'examples/pixel_events.py')
    for name in PACKAGED_SCRIPTS:
        copy_file(ROOT/'scripts'/name)
    baseline='artifacts/phase2c-v2/setup/native-01/baseline';copy_tree(ROOT/baseline)
    for stage in ('jump','ground'):
        for split in ('calibration','validation'):
            for scenario in scenarios(stage,split):
                name=run_name(scenario)
                relative=Path('artifacts/runs')/('tiny-reference-'+name) if stage=='jump' else Path('artifacts/phase2b/reference')/name
                source=ROOT/relative
                derive_json(source/'manifest.json',relative/'manifest.json')
                for index in range(240):copy_file(source/'frames'/f'{index:06d}.png',relative/'frames'/f'{index:06d}.png')
    annotations={'tiny-jump':'config/tiny-annotation.json','tiny-ground':'config/phase2b/tiny-annotation.json',
        'heart-jump':'config/phase2c-v2/heart-jump-annotation.json','heart-ground':'config/phase2c-v2/heart-ground-annotation.json'}
    for identifier,relative in annotations.items():
        source=ROOT/relative;annotation=read_json(source);index=annotation.get('template_frame',0)
        image=Path(annotation['template_run'])/'frames'/f'{index:06d}.png'
        template=Path('package-resources/templates')/identifier
        copy_file(image,template/'frames'/f'{index:06d}.png')
        value={**annotation,'template_run':'@PACKAGE_ROOT@/'+template.as_posix(),'control_points':[],'control_events':[],
            'derived_from_annotation_sha256':file_sha256(source),'independent_review_status':'pending_new_installation',
            'portability_note':'Appearance and geometry preserved; historical acceptance is not inherited.'}
        write_json(output/(relative+'.in'),portable(value));derived.append(relative+'.in')
    for relative in ('config/sources.json','config/third-party-notices.json','config/phase2c-v2/heart-source.json'):
        derive_json(ROOT/relative,Path(relative))
    for relative in ('vendor/heart/LICENSE','vendor/tiny/UNLICENSE','vendor/tiny/readme.md'):
        copy_file(ROOT/relative)
    if include_engine:
        from kiroclops.portable_v2.runtime import ENGINE_RELATIVE,ENGINE_SHA256
        if file_sha256(ROOT/ENGINE_RELATIVE)!=ENGINE_SHA256:raise VisualError('source_changed','Pinned Godot executable differs')
        copy_file(ROOT/ENGINE_RELATIVE)
        for name in ('GODOT-LICENSE.txt','GODOT-COPYRIGHT.txt'):copy_file(ROOT/'package-resources/licenses'/name)
    (output/'README.md').write_text('''# KiroClops local engineering kit

This kit contains the local visual core, MCP/CLI, panel/SVG fixtures, clean Heart target and pixel-only Tiny reference recordings.
It is an engineering build, not a claim that the eighteen comparative model trials have passed.

Run `scripts/bootstrap_local.ps1` in PowerShell with Python 3.10 x64. FFmpeg/ffprobe and an authenticated Kiro CLI are required on PATH.
Bootstrap installs binary dependencies and project-local Chromium, and resolves the four portable annotation templates.
Godot 4.5.1 belongs at `.tools/godot4/Godot_v4.5.1-stable_win64.exe`; the manifest says whether it was included.
No Kiro account state, credentials, past conversations or previous candidate workspaces are included. Model generation consumes the user's Kiro subscription.
Use `LOCAL_ENGINEERING.md` for commands and `PHASE3A_STATUS.md` for unfinished acceptance gates.

Tiny Crate reference game source is not included; its PNGs and input receipts support measurement.
The local panel/SVG fixture implementations are included so the reference recorder can run. They stay outside the fresh model target workspace; the model receives calibration pixels, not those implementations. The library also preserves legacy adapter code for compatibility.
Reference/target third-party notices remain with the kit. The generated annotation files belong to this installation; moving an initialized kit requires a fresh unpack/bootstrap.
Portable VideoPackage and report directories themselves need no path rewriting.

Current command coverage includes `scripts/check_local_report.py` for an existing media report and `scripts/package_local.py` for repacking an initialized kit into a fresh directory. Run bootstrap before repacking so the portable annotation templates have been resolved. Repacking does not add candidate results or inherit acceptance.
`PRODUCT_STRATEGY.md` and `ROADMAP.md` are included as lightweight documentation. Historical evidence and plans outside this kit are explicitly marked as repository-only references; their files are not claimed to be included.
''',encoding='utf-8')
    documentation=finalize_documentation(output);derived.extend(documentation['derived_documents'])
    if any(file_sha256(path)!=sha for path,sha in source_bindings.items()):raise VisualError('source_changed','Kit inputs changed during packaging')
    files={p.relative_to(output).as_posix():file_sha256(p) for p in sorted(output.rglob('*')) if p.is_file()}
    result={'schema_version':1,'kind':'LocalEngineeringKit','acceptance_status':'development_pending_full_acceptance',
        'included_engine':include_engine,'reference_source_included':True,
        'reference_game_source_included':False,'reference_fixture_source_included':True,
        'reference_implementation_in_model_workspace':False,'past_candidate_workspaces_included':False,
        'account_state_included':False,
        'original_file_hashes':originals,'derived_files':derived,'files':files,
        'documentation':documentation,'included_scripts':['scripts/'+name for name in PACKAGED_SCRIPTS],
        'required_external_tools':['Python 3.10 x64','ffmpeg','ffprobe','authenticated Kiro CLI'],
        'bootstrap':'scripts/bootstrap_local.ps1','model_started':False}
    write_json(output/'kit-manifest.json',result)
    archive_path=None
    if archive:
        archive_path=output.with_suffix('.zip')
        if archive_path.exists():raise VisualError('output_exists','Archive already exists')
        with zipfile.ZipFile(archive_path,'w',zipfile.ZIP_DEFLATED,compresslevel=3) as target:
            for path in sorted(output.rglob('*')):
                if path.is_file():target.write(path,path.relative_to(output))
    return {'status':'built','directory':str(output),'archive':str(archive_path) if archive_path else None,'file_count':len(files),
            'bytes':sum(p.stat().st_size for p in output.rglob('*') if p.is_file()),'acceptance_status':result['acceptance_status']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--include-engine',action='store_true');parser.add_argument('--zip',action='store_true')
    args=parser.parse_args();print(json.dumps(build(args.output,include_engine=args.include_engine,archive=args.zip),indent=2))
