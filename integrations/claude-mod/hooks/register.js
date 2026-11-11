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
  const line=(value,color)=>color==="gray"
    ?Text({children:clean(value,110),dimColor:true})
    :Text({children:clean(value,110),...(color?{color}: {})});
  const rows=[
    Text({children:"AGENT WORKFLOW MONITOR",bold:true,color:"cyan"}),
    Text({children:"made with ❤️ by joaquinsosa.es",dimColor:true}),
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
  rows.push(Button({label:"Actualizar",onPress:()=>capture($)}));
  rows.push(Button({label:"Cerrar panel",onPress:async()=>{state.open=false;await $.ui.close({id:PANE_ID})}}));
  return Box({flexDirection:"column",paddingX:1,children:rows});
}
async function open($){
  state.open=true;
  await capture($);
  const placement=await $.ui.open({id:PANE_ID,title:"Agent Workflow",focus:true});
  return placement;
}
export function register(on){
  on("session.start",async($,e,next)=>{
    const result=await next(e);
    await $.command.register({name:"workflow",description:"Open the Agent Workflow Monitor pane"});
    await capture($);
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
