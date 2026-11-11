import {sendSnapshot,bridgeStatus,listSharedWorkflows,loadSharedWorkflow,loadDelegatedExecutions,sendClaudeTask} from "./bridge.js";
/* Claude Code Mod — local, read-only workflow view.
 * All hooks pass through Claude's ordinary behavior; no tools are intercepted.
 * This version draws its own Pane and shows only data exposed by the Mods API.
 * Contract: Claude Code 2.1.287+, validate with your installed types.
 */
const PANE_ID = "agent-workflow-monitor";
const MAX_TOOLS = 12;
let state = {
  open: false, startedAt: Date.now(), toolCount: 0, lastTool: "",
  tools: [], agents: [], usage: null, status: "idle", error: null, expandedAgents: {},
  sharedPlans: [], selectedPlan: null, planData: null, delegatedExecutions: [], expandedPhases: {},
  lastHookLatencyMs: null,
};

function clean(text, max=85) {
  const value=String(text ?? "").replace(/\s+/g," ").trim();
  return value.length>max ? value.slice(0,max-1)+"…" : value;
}
function short(n) {
  if(!Number.isFinite(Number(n))) return "—";
  if(n>=1e6)return (n/1e6).toFixed(1)+"M";
  if(n>=1e3)return (n/1e3).toFixed(1)+"K";
  return String(n);
}
function agentItems(items) {
  if(!Array.isArray(items))return [];
  return items.slice(0,30).map((a,i)=>({
    id:clean(a.agentId??a.id??String(i),32),
    title:clean(a.description??a.name??a.agentType??a.type??"Subagent"),
    status:clean(a.status??"unknown",24),
    model:clean(a.model??"",35),
  }));
}
async function capture($) {
  try {
    const [usage,agents]=await Promise.all([
      $.session.usage(), $.agent.list()
    ]);
    // Context tokens measure model context, NOT billable token usage.
    state.usage=usage?.context??null;
    state.agents=agentItems(agents);
    state.error=null;
  } catch(err) {
    state.error=clean(err?.message??err,110);
  }
  $.ui.invalidate("ui.render");
}
function publish($, latestTool=null) {
  // Best-effort, asynchronous telemetry. It must never prevent Claude from
  // answering or executing tools if the local monitor is not running.
  void sendSnapshot($,{
    status:state.status,
    agents:state.agents,
    context:state.usage,
    lastTool:latestTool,
    hookLatencyMs:state.lastHookLatencyMs,
  }).catch(()=>$.ui.invalidate("ui.render"));
}
async function loadPlans($) {
  try {
    state.sharedPlans = await listSharedWorkflows($);
    if(state.selectedPlan){
      const [plan,executions]=await Promise.all([
        loadSharedWorkflow($,state.selectedPlan),
        loadDelegatedExecutions($,state.selectedPlan)
      ]);
      state.planData=plan;
      state.delegatedExecutions=executions;
    }
  }catch(err){
    // The native Claude pane remains usable even when FastAPI is offline.
    state.error="Plan compartido: "+clean(err?.message??err,100);
  }
  $.ui.invalidate("ui.render");
}
function phaseStatus(s) {
  return ({pending:"○",running:"◌",completed:"✓",failed:"!",blocked:"⊘",
           cancelled:"×",waiting:"◐"})[s]||"·";
}
function addWorkflowRows($,rows,line,Box,Text,Button) {
  rows.push(line("── PLAN COMPARTIDO ──","cyan"));
  if(!state.sharedPlans.length){
    rows.push(line("No hay planes registrados en el monitor local","gray"));
  }else{
    for(const plan of state.sharedPlans.slice(0,8)){
      const selected=plan.id===state.selectedPlan;
      rows.push(Box({flexDirection:"row",children:[
        Text({children:(selected?"▾ ":"▸ ")+clean(plan.title,46)}),
        Button({label:selected?"Activo":"Ver",onPress:async()=>{
          state.selectedPlan=selected?null:plan.id;
          if(state.selectedPlan){
            await loadPlans($);
          }else{
            state.planData=null;
            state.delegatedExecutions=[];
          }
          $.ui.invalidate("ui.render");
        }})
      ]}));
    }
  }
  if(!state.planData?.phases)return;
  const phases=state.planData.phases;
  const byParent=parent=>phases.filter(p=>(p.parent_id||null)===parent);
  function drawPhase(p,depth=0) {
    const children=byParent(p.id);
    const executions=state.delegatedExecutions.filter(ex=>ex.phase_id===p.id);
    const expanded=!!state.expandedPhases[p.id];
    const prefix=" ".repeat(Math.min(depth*2,8));
    const text=prefix+phaseStatus(p.status)+" "+clean(p.title,55);
    const person="  "+p.provider+(p.agent_id?" · "+clean(p.agent_id,30):"");
    if(children.length||executions.length){
      rows.push(Box({flexDirection:"row",children:[
        Text({children:text}),
        Button({label:expanded?"−":"+",onPress:()=>{
          state.expandedPhases[p.id]=!expanded;
          $.ui.invalidate("ui.render");
        }})
      ]}));
    }else rows.push(line(text));
    rows.push(line(prefix+person,"gray"));
    if(expanded){
      for(const execution of executions.slice(-5).reverse()){
        rows.push(line(prefix+"  "+phaseStatus(execution.status)+" "+
          clean(execution.provider,20)+" · "+clean(execution.agent_id,28),"gray"));
        if(execution.orchestrator_provider){
          rows.push(line(prefix+"    Orquestador: "+execution.orchestrator_provider+
            (execution.orchestrator_session_id?" · sesión vinculada":""),"gray"));
        }
        rows.push(line(prefix+"    "+clean(execution.started_at,32)+
          (execution.finished_at?" → "+clean(execution.finished_at,32):" · en curso"),"gray"));
      }
      const history=(state.planData?.history||[]).filter(h=>h.phase_id===p.id);
      if(history.length){
        rows.push(line(prefix+"  Relevos y cambios: "+history.length,"gray"));
        for(const event of history.slice(-4).reverse()){
          const changed=event.previous_provider!==event.next_provider||
                        event.previous_agent_id!==event.next_agent_id;
          rows.push(line(prefix+"  "+(changed?"↳ ":"· ")+
            clean(event.previous_provider,12)+" → "+
            clean(event.next_provider,12)+" · "+clean(event.next_status,20),"gray"));
        }
      }
      children.slice(0,50).forEach(x=>drawPhase(x,depth+1));
    }
  }
  byParent(null).slice(0,50).forEach(p=>drawPhase(p));
}
function draw($, e) {
  const {Box,Text,Button}=$.ui.resolve(e);
  const line=(value,color)=>color==="gray"
    ?Text({children:clean(value,110),dimColor:true})
    :Text({children:clean(value,110),...(color?{color}: {})});
  const rows=[
    Text({children:"AGENT WORKFLOW MONITOR",bold:true,color:"cyan"}),
    Text({children:"made with ❤️ by joaquinsosa.es",dimColor:true}),
    line("Estado: "+state.status+"  ·  Herramientas: "+state.toolCount),
    line("Monitor local: "+(bridgeStatus().connected?"conectado":bridgeStatus().error?"no disponible":"sin sincronizar"),bridgeStatus().connected?"green":"gray"),
    line("Telemetría: "+bridgeStatus().queued+" pendientes · "+bridgeStatus().sent+" enviados · "+bridgeStatus().dropped+" descartados","gray"),
    line("Hook observado: "+(state.lastHookLatencyMs==null?"—":state.lastHookLatencyMs+" ms"),"gray"),
  ];
  if(state.usage?.window) {
    rows.push(line("Contexto: "+short(state.usage.tokens??0)+" / "+short(state.usage.window)+" · "+(state.usage.percent??"—")+"%","cyan"));
    rows.push(line("Consumo facturado: consultar panel local (no confundir con contexto)"));
  } else {
    rows.push(line("Tokens: aún no disponibles","gray"));
  }
  addWorkflowRows($,rows,line,Box,Text,Button);
  rows.push(line("── AGENTES DE ESTA SESIÓN ──","cyan"));
  if(!state.agents.length)rows.push(line("Sin subagentes registrados","gray"));
  for(const a of state.agents.slice(0,10)){
    const glyph=a.status==="completed"?"✓":a.status==="running"?"◌":a.status==="failed"?"!":"·";
    rows.push(Box({flexDirection:"row",children:[
      Text({children:glyph+" "+a.title+" ["+a.status+"]"}),
      Button({
        label:state.expandedAgents[a.id]?"−":"+",
        onPress:()=>{
          state.expandedAgents[a.id]=!state.expandedAgents[a.id];
          $.ui.invalidate("ui.render");
        }
      })
    ]}));
    if(state.expandedAgents[a.id]){
      rows.push(line("   Agent ID: "+a.id,"gray"));
      if(a.model)rows.push(line("   Model: "+a.model,"gray"));
      const activity=state.tools.filter(x=>x.agent===a.id).slice(-4).reverse();
      for(const item of activity) rows.push(line("   "+item.time+" · "+item.tool,"gray"));
      if(!activity.length)rows.push(line("   Sin acciones registradas","gray"));
    }
  }
  rows.push(line("── ÚLTIMAS ACCIONES ──","cyan"));
  if(!state.tools.length)rows.push(line("Sin herramientas observadas","gray"));
  for(const entry of state.tools.slice(-MAX_TOOLS).reverse()){
    rows.push(line(entry.time+"  "+entry.agent+" · "+entry.tool));
  }
  if(state.error)rows.push(line("Diagnóstico: "+state.error,"yellow"));
  if(bridgeStatus().error)rows.push(line("Puente: "+bridgeStatus().error,"yellow"));
  rows.push(Button({label:"Actualizar",onPress:async()=>{await capture($);publish($);await loadPlans($)}}));
  rows.push(Button({label:"Cerrar panel",onPress:async()=>{state.open=false;await $.ui.close({id:PANE_ID})}}));
  return Box({flexDirection:"column",paddingX:1,children:rows});
}
async function open($){
  state.open=true;
  await capture($);
  publish($);
  await loadPlans($);
  const placement=await $.ui.open({id:PANE_ID,title:"Agent Workflow",focus:true});
  return placement;
}
function recordTaskChange($,e,outcome) {
  // Only completed TaskCreate/TaskUpdate calls; no inferred progress from
  // arbitrary tool invocations or file edits, and never affect Codex phases.
  if(!state.selectedPlan||outcome?.isError||outcome?.deny||outcome?.result?.success===false)return;
  const action=e.tool==="TaskCreate"?"created":e.tool==="TaskUpdate"?"updated":null;
  if(!action)return;
  const body=outcome?.result;
  const taskId=action==="created"
    ?(body?.task?.id??body?.taskId??body?.id)
    :e.taskId;
  if(typeof taskId!=="string"&&typeof taskId!=="number"){
    state.error="TaskCreate: Claude no devolvió un ID estructurado";
    return;
  }
  const taskIdString=String(taskId);
  if(!taskIdString||taskIdString.length>128)return;
  const subject=typeof e.subject==="string"?clean(e.subject,195):null;
  // A successful TaskUpdate may omit a status and only change the owner.
  const status=["pending","in_progress","completed","deleted"].includes(e.status)
    ?e.status:null;
  const owner=typeof e.owner==="string"?clean(e.owner,128):null;
  const eventId=String(e.tool_use_id||(
    action+":"+taskIdString+":"+state.toolCount)).slice(0,128);
  const planId=state.selectedPlan;
  void sendClaudeTask($,{
    planId, taskId:taskIdString,action,subject,status,owner,
    eventId:"claude-tool:"+eventId,
  }).then(async()=>{
    if(state.selectedPlan===planId)await loadPlans($);
  }).catch(err=>{
    state.error="Sincronización de tareas: "+clean(err?.message??err,95);
    if(state.open)$.ui.invalidate("ui.render");
  });
}
export function register(on){
  on("session.start",async($,e,next)=>{
    const result=await next(e);
    await $.command.register({name:"workflow",description:"Open the Agent Workflow Monitor pane"});
    void capture($).catch(err=>{
      state.error="Sesión: "+clean(err?.message??err,100);
    });
    return result;
  });
  on("command.run",{command:"workflow"},async($,e)=>{
    const placement=await open($);
    return {text:placement?.isPlaced===false
      ?"Agent Workflow Monitor is not available on this surface. Use Claude Code CLI or Desktop Code."
      :"Agent Workflow Monitor opened. Details are displayed in its pane."};
  });
  on("turn.start",async($,e,next)=>{
    if(!e.agentId)state.status="running";
    const result=await next(e);
    if(state.open)$.ui.invalidate("ui.render");
    return result;
  });
  on("turn.complete",async($,e,next)=>{
    const result=await next(e);
    const observationStarted=Date.now();
    if(!e.agentId)state.status="waiting";
    const lastTool=state.tools[state.tools.length-1]??null;
    // Never wait for session usage, agent discovery or localhost I/O in
    // the Claude turn lifecycle. Failures remain visible in the pane.
    void capture($).then(()=>{
      state.lastHookLatencyMs=Date.now()-observationStarted;
      publish($,lastTool);
    }).catch(err=>{
      state.error="Captura: "+clean(err?.message??err,100);
    });
    return result;
  });
  on("agent.spawn",async($,e,next)=>{
    const result=await next(e);
    void capture($).then(()=>publish($)).catch(err=>{
      state.error="Agentes: "+clean(err?.message??err,100);
    });
    return result;
  });
  on("tool.call",async($,e,next)=>{
    const observationStarted=Date.now();
    state.toolCount++;
    const item={
      time:new Date().toLocaleTimeString("es-ES",{hour:"2-digit",minute:"2-digit"}),
      tool:clean(e.tool??"Tool",45),
      agent:clean(e.agentId??"Claude",25),
    };
    state.lastTool=item.tool;
    state.tools=[...state.tools,item].slice(-MAX_TOOLS);
    if(state.open)$.ui.invalidate("ui.render");
    state.lastHookLatencyMs=Date.now()-observationStarted;
    const outcome=await next(e);
    if(e.tool==="TaskCreate"||e.tool==="TaskUpdate"){
      try {recordTaskChange($,e,outcome)}
      catch(err){state.error="Task sync: "+clean(err?.message??err,100)}
    }
    return outcome;
  });
  on("ui.render",{component:"Pane",requestId:PANE_ID},($,e,next)=>draw($,e));
  on("ui.close",{id:PANE_ID},($,e,next)=>{
    state.open=false;
    return next(e);
  });
}
