"""V6 knowingly changed the launcher and session that the 2.27 native proof pins.

PHASE4_V6_PLAN.md WP3 adds probes, budget refusal, window inspection and guard
hardening to ``kiroclops/local_engine/launcher.py`` and ``session.py``. Both are
pinned by ``launcher_policy_proof.SOURCE_PINS``, so the pinned 2.27 chain cannot hold
for the live tree, and V6 runs are reported as native-uncertified.

The chain still has to hold for the frozen V5b sources that produced the accepted
evidence. ``tests/test_v5b_snapshot_native_chain.py`` runs the snapshot's own copies
of the affected tests there. In the live tree those tests are skipped only while the
pinned divergence is exactly this V6 one; any other pinned change still fails them.
"""
from pathlib import Path

from kiroclops.local_engine import launcher_policy_proof as proof
from kiroclops.visual.schema import file_sha256

ROOT = Path(__file__).resolve().parents[1]
V6_CHANGED_PINS = frozenset({'kiroclops/local_engine/launcher.py', 'kiroclops/local_engine/session.py'})
# Pinned evidence (the staged 2.27 proofs); a fresh clone has none of it (V6 plan WP4.3).
EVIDENCE = tuple(sorted(path for path in proof.SOURCE_PINS if not path.startswith('kiroclops/')))
REASON = ('V6 changed the launcher and session pinned by the 2.27 native proof (PHASE4_V6_PLAN.md WP3); '
          'the pinned chain is verified on the V5b snapshot by tests/test_v5b_snapshot_native_chain.py')


def changed_pins(root=ROOT):
    """Pinned launcher-proof sources whose bytes differ from their pins in ``root``."""
    changed = set()
    for relative, expected in proof.SOURCE_PINS.items():
        path = Path(root) / relative
        if not path.is_file() or file_sha256(path) != expected:
            changed.add(relative)
    return frozenset(changed)


V6_DIVERGENCE = changed_pins() == V6_CHANGED_PINS
