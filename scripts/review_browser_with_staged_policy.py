"""Explicit post-hoc browser review using the tested registered-policy reader.

The live cohort, production modules on disk and primary outcomes are untouched.
Every review kit binds this wrapper, the proposed reader and its test receipt.
This command grants no review verdict and never launches Kiro or a browser.
"""
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from kiroclops.local_engine import evidence
from kiroclops.visual.schema import file_sha256, read_json
from stage_browser_evidence_policy_fix import staged_function


def load_verified_helper():
    proposal = ROOT / 'artifacts/phase3a/browser-evidence-policy-compatibility-01'
    proposed = proposal / 'evidence-proposed.py'
    tests = read_json(proposal / 'result.json')
    assert tests['status'] == 'pass' and tests['tests'] == 46
    assert tests['failures'] == tests['errors'] == tests['skipped'] == 0
    assert tests['production_modified'] is False
    assert file_sha256(evidence.__file__) == tests['production_evidence_sha256']
    assert file_sha256(proposed) == tests['proposed_evidence_sha256']
    builder = ROOT / 'scripts/stage_browser_evidence_policy_fix.py'
    assert file_sha256(builder) == tests['proposal_builder_sha256']
    path = ROOT / 'artifacts/phase3a/tools/prepare_local_final_review.py'
    spec = importlib.util.spec_from_file_location('_explicit_posthoc_browser_review', path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    original_sources = helper.helper_sources

    def bound_sources():
        sources = original_sources()
        for source in (Path(__file__), builder, proposed, proposal / 'result.json'):
            sources[source.relative_to(ROOT).as_posix()] = file_sha256(source)
        return sources

    return helper, staged_function(proposed), bound_sources


def main():
    helper, reader, sources = load_verified_helper()
    # The imported helper keeps its own reference to the function. Patch both
    # references within this isolated process; nothing is written to production.
    with ExitStack() as stack:
        stack.enter_context(patch.object(evidence, 'required_observation_bindings', reader))
        stack.enter_context(patch.object(helper, 'required_observation_bindings', reader))
        stack.enter_context(patch.object(helper, 'helper_sources', sources))
        return helper.main()


if __name__ == '__main__':
    raise SystemExit(main())
