"""Write one new static launcher extension receipt; never run Kiro or a capture."""
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiroclops.local_engine.launcher_policy_proof import ROOT, OUTPUT, build_proof
from kiroclops.visual.schema import VisualError, digest, write_json


def main():
    output = ROOT / OUTPUT
    if output.exists():
        raise VisualError('output_exists', 'Preserve the existing static launcher extension receipt.')
    proof = build_proof()
    write_json(output, proof)
    print({'status': proof['status'], 'output': str(output), 'proof_semantic_sha256': digest(proof),
           'native_canary': False, 'model_started': False})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
