"""Engineering-only real stdio handshake and clean baseline recording check."""
import asyncio
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp import Client, StdioServerParameters


async def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action',choices=['prepare_experiment','evaluate_candidate','build_report'],default='prepare_experiment')
    args=parser.parse_args()
    directory = ROOT / 'sessions/engineering-smoke-01'
    params = StdioServerParameters(command=sys.executable,
        args=['-m','kiroclops.integration.mcp','--session',str(directory)],
        env={**os.environ,'PYTHONPATH':str(ROOT),'PYTHONUTF8':'1'})
    async with Client(params,read_timeout_seconds=60) as client:
        listed=await client.list_tools()
        print(json.dumps({'tools':[t.name for t in listed.tools]}),flush=True)
        inspect=await client.call_tool('inspect_session',{})
        print(inspect.model_dump_json(),flush=True)
        response=await client.call_tool(args.action,{})
        result=response.structured_content or json.loads(response.content[0].text)
        print(json.dumps(result),flush=True)
        while result.get('status') in {'queued','running'}:
            response=await client.call_tool('get_job',{'job_id':result['job_id'],'wait_seconds':20})
            result=response.structured_content or json.loads(response.content[0].text)
            print(json.dumps(result,ensure_ascii=False),flush=True)
        if result.get('status') != 'succeeded':
            raise SystemExit(1)


if __name__=='__main__':
    asyncio.run(main())
