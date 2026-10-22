"""Exercise the real local MCP connection without calling a model or writing code."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mcp import Client, StdioServerParameters
from kiroclops.integration.storage import write_json


async def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session',required=True,type=Path)
    p.add_argument('--action',choices=['prepare_experiment','evaluate_candidate','build_report'],default='prepare_experiment')
    a=p.parse_args()
    directory=a.session.resolve()
    params=StdioServerParameters(command=sys.executable,
        args=['-m','kiroclops.integration.mcp','--session',str(directory)],
        env={**os.environ,'PYTHONPATH':str(ROOT),'PYTHONUTF8':'1'})
    async with Client(params,read_timeout_seconds=60) as client:
        names=[t.name for t in (await client.list_tools()).tools]
        print(json.dumps({'tools':names}),flush=True)
        response=await client.call_tool('inspect_session',{})
        inspection=response.structured_content or json.loads(response.content[0].text)
        write_json(directory/'engineering'/'mcp-inspection.json',inspection)
        response=await client.call_tool(a.action,{})
        result=response.structured_content or json.loads(response.content[0].text)
        while True:
            print(json.dumps({k:result.get(k) for k in ['job_id','status','progress','candidate_id','error']},ensure_ascii=False),flush=True)
            if result.get('status') not in {'queued','running'}:
                break
            response=await client.call_tool('get_job',{'job_id':result['job_id'],'wait_seconds':20})
            result=response.structured_content or json.loads(response.content[0].text)
        write_json(directory/'engineering'/f'{a.action}.json',result)
        if result.get('status')!='succeeded':
            raise SystemExit(1)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
