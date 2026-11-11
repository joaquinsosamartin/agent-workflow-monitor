import asyncio
import os
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator
from typing import Literal
from .db import Store
from .process_watch import ProcessRegistry

STATIC = Path(__file__).resolve().parent/'static'
DATA_DIR=Path(os.getenv('AGENT_MONITOR_DATA', str(Path.home()/'.agent-monitor')))
store = Store(DATA_DIR/'monitor.db')
process_registry = ProcessRegistry()
listeners: dict[str, set[asyncio.Queue]] = {}

class RunIn(BaseModel):
    title: str=Field(min_length=1,max_length=200)
    project: str=Field(default='',max_length=200)
    id: str|None=None

class NodeIn(BaseModel):
    title: str=Field(min_length=1,max_length=200)
    parent_id: str|None=None
    agent: str=''
    provider: str=''
    position: int=0
    id: str|None=None

class UsageIn(BaseModel):
    session_id: str=Field(min_length=1)
    usage_key: str=Field(min_length=1)
    model: str=''
    input_tokens: int=Field(default=0,ge=0)
    output_tokens: int=Field(default=0,ge=0)
    cached_input_tokens: int=Field(default=0,ge=0)
    reasoning_tokens: int=Field(default=0,ge=0)
    cost_usd: str|None=None
    cost_source: Literal['reported','estimated','unavailable']='unavailable'
    @model_validator(mode='after')
    def validate_cost(self):
        from decimal import Decimal, InvalidOperation
        if self.cost_usd is not None:
            try:
                if not Decimal(self.cost_usd).is_finite() or Decimal(self.cost_usd)<0: raise ValueError()
            except (InvalidOperation,ValueError): raise ValueError('Invalid cost_usd')
        if self.cost_source != 'unavailable' and self.cost_usd is None: raise ValueError('cost_usd required for reported/estimated')
        return self

class EventIn(BaseModel):
    type: Literal['node.status','run.status','usage','log']
    node_id: str|None=None
    status: Literal['pending','running','idle','completed','failed','blocked','cancelled']|None=None
    agent: str|None=None
    provider: str|None=None
    message: str=''
    external_id: str|None=None
    usage: UsageIn|None=None
    @model_validator(mode='after')
    def valid(self):
        if self.type in ('node.status','run.status') and not self.status: raise ValueError('status required')
        if self.type=='node.status' and not self.node_id: raise ValueError('node_id required')
        if self.type=='usage' and not self.usage: raise ValueError('usage required')
        return self

KEY_FILE=DATA_DIR/'api.key'
if not KEY_FILE.exists():
    import secrets
    KEY_FILE.write_text(secrets.token_urlsafe(32),encoding='utf-8')
    try: os.chmod(KEY_FILE,0o600)
    except OSError: pass
KEY=KEY_FILE.read_text(encoding='utf-8').strip()

def authorize(x_monitor_key: str|None=Header(default=None)):
    import hmac
    if not x_monitor_key or not hmac.compare_digest(x_monitor_key,KEY):
        raise HTTPException(401,'Invalid monitor key')

app=FastAPI(title='Agent Workflow Monitor',version='0.2.0')

# Local transcript observation is read-only and requires no provider hooks.
# Disable with AGENT_MONITOR_OBSERVE=0.
from .observer import SessionObserver
observer = SessionObserver(store, poll_seconds=float(os.getenv('AGENT_MONITOR_POLL_SECONDS', '2')),
                           max_age_hours=float(os.getenv('AGENT_MONITOR_LOOKBACK_HOURS', '12')))

@app.on_event('startup')
async def observer_start():
    if os.getenv('AGENT_MONITOR_OBSERVE', '1').lower() not in ('0','false','no'):
        observer.start()

@app.on_event('shutdown')
async def observer_shutdown():
    observer.stop()

@app.get('/api/observer')
def observer_status():
    from .observer import discovery_roots
    import time
    sources = []
    for provider, root in discovery_roots():
        count = 0
        recent = 0
        newest = None
        if root.exists():
            try:
                for file in root.rglob('*.jsonl'):
                    if not file.is_file() or (provider == 'codex' and not file.name.startswith('rollout-')):
                        continue
                    count += 1
                    mtime = file.stat().st_mtime
                    if time.time() - mtime < observer.max_age_seconds:
                        recent += 1
                    if newest is None or mtime > newest:
                        newest = mtime
            except OSError:
                pass
        sources.append({
            'provider':provider,'path':str(root),'exists':root.is_dir(),
            'jsonl_count':count,'recent_count':recent,'last_modified_epoch':newest
        })
    with store.lock:
        discovered = [dict(item) for item in store.conn.execute(
            "SELECT provider,COUNT(DISTINCT run_id) AS sessions,"
            " COUNT(*) AS files FROM observer_files GROUP BY provider"
        )]
    failures = [
        {'provider':stat.get('provider'),'filename':Path(path).name,
         'error':stat.get('error')}
        for path,stat in observer.file_stats.items() if stat.get('error')
    ]
    return {'enabled':observer.thread is not None and observer.thread.is_alive(),
            'sources':sources,
            'poll_seconds':observer.poll_seconds,
            'lookback_hours':observer.max_age_seconds/3600,
            'last_scan':dict(observer.scan_summary),
            'ingested':discovered,
            'failing_files':failures[:20],
            'error_count':len(observer.last_errors)}

@app.post('/api/observer/scan',dependencies=[Depends(authorize)])
def observer_scan():
    return {'files_scanned': observer.scan_once()}


@app.get('/')
def index(): return FileResponse(STATIC/'index.html')

@app.get('/api/bootstrap')
def bootstrap(): return {'key':KEY,'version':'0.1.0'}

def classified_runs():
    runs = store.run_list()
    with store.lock:
        observer_files = [dict(r) for r in store.conn.execute(
            "SELECT path,provider,run_id FROM observer_files"
        )]
    process_state = process_registry.classify(runs, observer_files)
    return runs, process_state

@app.get('/api/runs')
def get_runs():
    runs, _ = classified_runs()
    return runs

@app.get('/api/processes')
def get_processes():
    runs, processes = classified_runs()
    return {
        "verified_sessions": processes["verified_sessions"],
        "detected_processes": len(processes["processes"]),
        "processes_by_provider": processes["processes_by_provider"],
        "process_relationships": processes["process_relationships"],
        "unmatched_processes": processes["unmatched_processes"],
        "checked_at": processes["checked_at"],
        "errors": processes["errors"],
        "classification": {
            status: sum(r["presence"] == status for r in runs)
            for status in ("active", "waiting", "recent_unverified", "inactive")
        }
    }

@app.post('/api/runs',dependencies=[Depends(authorize)])
def create_run(data:RunIn):
    try: return {'id':store.add_run(data.title,data.project,data.id)}
    except Exception as ex:
        if isinstance(ex,ValueError): raise HTTPException(400,str(ex))
        raise HTTPException(409,'Run ID already exists')

@app.post('/api/runs/{run_id}/nodes',dependencies=[Depends(authorize)])
def create_node(run_id:str, data:NodeIn):
    try: return {'id':store.add_node(run_id,data.title,data.parent_id,data.agent,data.provider,data.position,data.id)}
    except ValueError as ex: raise HTTPException(400,str(ex))
    except Exception: raise HTTPException(409,'Node ID already exists')

@app.get('/api/runs/{run_id}')
def get_run(run_id:str):
    result=store.snapshot(run_id)
    if result is None: raise HTTPException(404,'Run not found')
    return result

@app.post('/api/runs/{run_id}/events',dependencies=[Depends(authorize)])
async def publish(run_id:str,data:EventIn):
    event=data.model_dump(exclude_none=True)
    event['run_id']=run_id
    try: seq=store.save_event(event)
    except ValueError as ex: raise HTTPException(400,str(ex))
    if seq:
        for q in list(listeners.get(run_id,())):
            if not q.full(): q.put_nowait({'seq':seq,'event':event})
    return {'sequence':seq,'duplicate':seq is None}

@app.get('/api/runs/{run_id}/events')
async def event_stream(run_id:str,request:Request,after:int|None=None):
    if store.snapshot(run_id) is None: raise HTTPException(404,'Run not found')
    # New subscribers only need fresh events. Replaying the entire database
    # flooded the browser with hundreds of redundant updates on selection.
    if after is None:
        recent = store.recent_events(run_id, 1)
        after = recent[0]['seq'] if recent else 0
    q:asyncio.Queue=asyncio.Queue(maxsize=256)
    listeners.setdefault(run_id,set()).add(q)
    async def stream():
        last=after
        try:
            while True:
                records=store.event_list(run_id,last,200)
                if records:
                    for record in records:
                        last=record['seq']
                        yield f'id: {last}\nevent: update\ndata: {record["payload"]}\n\n'
                    continue
                if await request.is_disconnected(): break
                try: await asyncio.wait_for(q.get(),timeout=12)
                except asyncio.TimeoutError: yield ': keepalive\n\n'
        finally:
            listeners.get(run_id,set()).discard(q)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.get('/api/runs/{run_id}/history')
def history(run_id:str,after:int=0,limit:int=40):
    if store.snapshot(run_id) is None: raise HTTPException(404,'Run not found')
    if after: return store.event_list(run_id,after,min(max(limit,1),200))
    return store.recent_events(run_id,limit)

@app.post('/api/demo',dependencies=[Depends(authorize)])
def demo():
    rid=store.add_run('Customer API · Authentication & Authorization','POPS / Demo')
    steps=[('Análisis y arquitectura','Claude · Architect','claude',['Revisar requisitos','Definir contratos','Plan de implementación']),('Implementación backend','Codex · Developer','codex',['Entidades y DTOs','Endpoints y handlers','Persistencia']),('Pruebas y validaciones','Codex · QA','codex',['Compilación','Tests unitarios','Calidad']),('Revisión y entrega','Claude · Reviewer','claude',['Revisión de arquitectura','Seguridad','Pull Request'])]
    result=[]
    for i,(title,agent,provider,children) in enumerate(steps):
        parent=store.add_node(rid,title,agent=agent,provider=provider,position=i)
        subs=[store.add_node(rid,t,parent,agent,provider,j) for j,t in enumerate(children)]
        result.append((parent,subs))
    for i,(parent,subs) in enumerate(result):
        if i>1: break
        store.save_event({'run_id':rid,'type':'node.status','node_id':parent,'status':'completed' if i==0 else 'running'})
        for j,sub in enumerate(subs):
            if i==1 and j>1: break
            store.save_event({'run_id':rid,'type':'node.status','node_id':sub,'status':'completed' if i==0 or j==0 else 'running'})
            if i==0 or j==0:
                store.save_event({'run_id':rid,'type':'usage','node_id':sub,'agent':steps[i][1],'provider':steps[i][2],
                    'usage':{'session_id':f'demo-{i}-{j}','usage_key':'total','model':'demo-model',
                    'input_tokens':2700+j*900,'output_tokens':700+j*130,'cached_input_tokens':300,
                    'cost_usd':str(round(.011+j*.006,4)),'cost_source':'estimated'}})
    store.save_event({'run_id':rid,'type':'run.status','status':'running'})
    return {'id':rid}
