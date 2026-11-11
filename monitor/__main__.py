import argparse
import json
import subprocess
import sys
import threading
import urllib.request
from uuid import uuid4
from .adapters import normalize

def post(url,key,payload):
    req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Monitor-Key':key},method='POST')
    with urllib.request.urlopen(req,timeout=8) as response: return json.load(response)

def run_agent(args):
    from .app import KEY
    base=f'http://127.0.0.1:{args.port}/api/runs/{args.run_id}/events'
    session=args.session or str(uuid4())
    post(base,KEY,{'type':'node.status','node_id':args.node_id,'status':'running','agent':args.agent,'provider':args.provider})
    command=args.command
    if command and command[0]=='--': command=command[1:]
    if not command: raise SystemExit('Agent command required after --')
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=None,text=True,encoding='utf-8',errors='replace',bufsize=1)
    try:
        for idx,line in enumerate(process.stdout):
            sys.stdout.write(line);sys.stdout.flush()
            try: payload=json.loads(line)
            except json.JSONDecodeError: continue
            for event in normalize(args.provider,payload,session,idx):
                event['node_id']=args.node_id;event['agent']=args.agent;event['provider']=args.provider
                post(base,KEY,event)
        code=process.wait()
    except BaseException:
        process.terminate();process.wait();raise
    post(base,KEY,{'type':'node.status','node_id':args.node_id,'status':'completed' if code==0 else 'failed'})
    return code

def main():
    p=argparse.ArgumentParser(description='Local Agent Workflow Monitor')
    sub=p.add_subparsers(dest='cmd')
    serve=sub.add_parser('serve',help='Start local web UI')
    serve.add_argument('--port',type=int,default=8765)
    serve.add_argument('--no-browser',action='store_true')
    wrapped=sub.add_parser('run',help='Wrap a Codex/Claude CLI call to collect usage')
    wrapped.add_argument('--provider',choices=['codex','claude'],required=True)
    wrapped.add_argument('--run-id',required=True)
    wrapped.add_argument('--node-id',required=True)
    wrapped.add_argument('--agent',default='')
    wrapped.add_argument('--session',default=None)
    wrapped.add_argument('--port',type=int,default=8765)
    wrapped.add_argument('command',nargs=argparse.REMAINDER)
    args=p.parse_args()
    if args.cmd=='run': sys.exit(run_agent(args))
    import uvicorn
    if args.cmd is None: args=p.parse_args(['serve'])
    if not args.no_browser:
        import webbrowser
        threading.Timer(1.2,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    print(f'Agent Monitor: http://127.0.0.1:{args.port}')
    uvicorn.run('monitor.app:app',host='127.0.0.1',port=args.port,log_level='warning')
if __name__=='__main__': main()
