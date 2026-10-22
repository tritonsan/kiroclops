"""Unprivileged, networkless pixel-only subprocess entry point."""
from pathlib import Path
import sys

from kiroclops.visual.schema import read_json, write_json, VisualError
from kiroclops.cloud.transport import unpack
from kiroclops.cloud.execution import measure_direct
from kiroclops.cloud.results import make_measurement
from kiroclops.cloud.identity import engine_identity


def main():
    request_path, output = Path(sys.argv[1]),Path(sys.argv[2])
    request = read_json(request_path)
    output.mkdir(exist_ok=True)
    try:
        engine_identity(4)
        unpack(request_path.parent/'input.zip',output/'package',request['package_sha256'])
        result = measure_direct(output/'package',request['selection'],request.get('contract'))
        write_json(output/'measurement.json',make_measurement(result['observations'],result['events']))
        write_json(output/'status.json',{'status':'succeeded'})
    except VisualError as exc:
        write_json(output/'status.json',{'status':'failed','error':{'code':exc.code}})
        return 1
    except Exception:
        write_json(output/'status.json',{'status':'failed','error':{'code':'analysis_runtime_error'}})
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
