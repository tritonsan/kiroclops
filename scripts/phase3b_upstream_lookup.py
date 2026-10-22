"""Record the public source-revision lookup; never guess a performance base."""
import json
from pathlib import Path
import urllib.request
import urllib.error
from datetime import datetime, timezone

from kiroclops.visual.schema import write_json


def main():
    prefix = '9a6e0d1bff'
    url = 'https://api.github.com/repos/opencv/opencv/commits/'+prefix
    request = urllib.request.Request(url, headers={'User-Agent': 'KiroClops-internal-feasibility'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, value = response.status, json.load(response)
    except urllib.error.HTTPError as error:
        status, value = error.code, json.loads(error.read().decode())
    record = {'checked_at': datetime.now(timezone.utc).isoformat(), 'repository': 'opencv/opencv',
              'endpoint': url, 'prefix_from_vendor_build': prefix,
              'vendor_build_control': '4.13.0-153-g9a6e0d1bff-dirty',
              'http_status': status, 'upstream_resolved': status == 200,
              'source_status': 'unresolved_public_revision' if status != 200 else 'upstream_commit_found',
              'response': {'message': value.get('message'), 'sha': value.get('sha'), 'html_url': value.get('html_url')},
              'no_arbitrary_commit_selected': True,
              'limit': 'A dirty vendor tree also needs its changes identified; a version label alone does not isolate COOL effects.'}
    write_json(Path('artifacts/phase3b/cool-probe-06/upstream-lookup.json'), record)
    print({'http_status': status, 'resolved': record['upstream_resolved'], 'message': value.get('message')})


if __name__ == '__main__':
    main()
