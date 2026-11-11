/* Claude Code Mod — local, read-only workflow view.
 * All hooks pass through Claude's ordinary behavior; no tools are intercepted.
 * This version draws its own Pane and shows only data exposed by the Mods API.
 * Contract: Claude Code 2.1.287+, validate with your installed types.
 */
const PANE_ID = "agent-workflow-monitor";
const MAX_TOOLS = 12;
let state = {
  open: false, startedAt: Date.now(), toolCount: 0, lastTool: "",
  tools: [], agents: [], usage: null, status: "idle", error: null,
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
function draw($, e) {
  const {Box,Text,Button}=$.ui.resolve(e);
  const line=(value,color)=>Text({children:clean(value,110),color});
  const rows=[
    Text({children:"AGENT WORKFLOW MONITOR",bold:true,color:"cyan"}),
    line("Estado: "+state.status+"  ·  Herramientas: "+state.toolCount),
  ];
  if(state.usage?.window) {
    rows.push(line("Contexto: "+short(state.usage.tokens??0)+" / "+short(state.usage.window)+" · "+(state.usage.percent??"—")+"%","cyan"));
    rows.push(line("Consumo facturado: consultar panel local (no confundir con contexto)"));
  } else {
    rows.push(line("Tokens: aún no disponibles","gray"));
  }
  rows.push(line("── AGENTES ──","cyan"));
  if(!state.agents.length)rows.push(line("Sin subagentes registrados","gray"));
  for(const a of state.agents.slice(0,10)){
    rows.push(line("◌ "+a.title+" ["+a.status+"]"));
    if(a.model)rows.push(line("  "+a.model,"gray"));
  }
  rows.push(line("── ÚLTIMAS ACCIONES ──","cyan"));
  if(!state.tools.length)rows.push(line("Sin herramientas observadas","gray"));
  for(const entry of state.tools.slice(-MAX_TOOLS).reverse()){
    rows.push(line(entry.time+"  "+entry.agent+" · "+entry.tool));
  }
  if(state.error)rows.push(line("Diagnóstico: "+state.error,"yellow"));
  rows.push(Button({label:"Actualizar",onPress:()=>capture($)}));
  rows.push(Button({label:"Cerrar panel",onPress:async()=>{state.open=false;await $.ui.close({id:PANE_ID})}}));
  return Box({flexDirection:"column",paddingX:1,children:rows});
}
async function open($){
  state.open=true;
  await capture($);
  return $.ui.open({id:PANE_ID,title:"Agent Workflow",focus:true});
}
export function register(on){
  on("session.start",async($,e,next)=>{
    const result=await next(e);
    await $.command.register({name:"workflow",description:"Open the Agent Workflow Monitor pane"});
    await capture($);
    return result;
  });
  on("command.run",{command:"workflow"},async($,e)=>{
    await open($);
    return {text:"Agent Workflow Monitor opened. Details are displayed in its pane."};
  });
  on("turn.start",async($,e,next)=>{
    if(!e.agentId)state.status="running";
    const result=await next(e);
    if(state.open)$.ui.invalidate("ui.render");
    return result;
  });
  on("turn.complete",async($,e,next)=>{
    const result=await next(e);
    if(!e.agentId)state.status="waiting";
    await capture($);
    return result;
  });
  on("agent.spawn",async($,e,next)=>{
    const result=await next(e);
    await capture($);
    return result;
  });
  on("tool.call",async($,e,next)=>{
    state.toolCount++;
    const item={
      time:new Date().toLocaleTimeString("es-ES",{hour:"2-digit",minute:"2-digit"}),
      tool:clean(e.tool??"Tool",45),
      agent:clean(e.agentId??"Claude",25),
    };
    state.lastTool=item.tool;
    state.tools=[...state.tools,item].slice(-MAX_TOOLS);
    if(state.open)$.ui.invalidate("ui.render");
    return next(e);
  });
  on("ui.render",{component:"Pane",requestId:PANE_ID},($,e,next)=>draw($,e));
  on("ui.close",{id:PANE_ID},($,e,next)=>{
    state.open=false;
    return next(e);
  });
}
