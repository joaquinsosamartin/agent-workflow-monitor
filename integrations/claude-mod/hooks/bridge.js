/* Host-mediated, loopback-only bridge from Claude Mod to local FastAPI.
 * No prompts, tool inputs, tool outputs, API model secrets or command lines.
 * Optional: failure leaves the native pane functioning offline.
 */
const BASE = "http://127.0.0.1:8765";
let lastError = "";
let keyCache = null;
let lastSession = "";
let seq = 0;
let sending = null;

function oneLine(s, limit=120) {
  return String(s ?? "").replace(/\s+/g," ").slice(0,limit).trim();
}
function errorText(e) { return oneLine(e?.message || e,150); }

async function request($, path, init={}) {
  if(typeof $.http?.fetch !== "function") {
    throw new Error("Claude Mod: $.http.fetch no disponible");
  }
  const result = await $.http.fetch(BASE + path, init);
  if(!result?.ok)throw new Error("HTTP "+(result?.status??"?")+" desde "+path);
  return result.text ? JSON.parse(result.text) : {};
}
async function apiKey($) {
  if(keyCache)return keyCache;
  const bootstrap=await request($,"/api/bootstrap");
  if(typeof bootstrap.key !== "string"||!bootstrap.key)throw new Error("Monitor sin clave local");
  keyCache=bootstrap.key;
  return keyCache;
}
export function bridgeStatus() {
  return {connected:!lastError && !!lastSession, sessionId:lastSession, error:lastError};
}
export async function sendSnapshot($, data) {
  // Never overlap HTTP calls and never make Claude's tool execution depend
  // on a backend response; callers catch and report local errors.
  if(sending)return sending;
  sending=(async()=>{
    const session_id=oneLine(await $.session.id(),128);
    if(!session_id)throw new Error("Claude no expone todavía la sesión");
    const cwd=oneLine(await $.session.cwd(),900);
    const key=await apiKey($);
    const agents=(data.agents || []).slice(0,30).map(a=>({
      id:oneLine(a.id,128),
      title:oneLine(a.title||"Subagent",145),
      status:["pending","running","waiting","idle","completed","failed","killed"].includes(a.status)?a.status:"pending",
      model:oneLine(a.model,95),
    })).filter(a=>a.id);
    const payload={
      session_id, cwd, agents,
      status: data.status==="waiting"?"waiting":data.status==="idle"?"idle":data.status==="failed"?"failed":"running",
      tool: data.lastTool ? oneLine(data.lastTool.tool,90) : null,
      agent_id: data.lastTool ? oneLine(data.lastTool.agent,110) : null,
      event_id: "mod-"+Date.now()+"-"+(++seq),
      context_tokens: Number.isInteger(data.context?.tokens)?data.context.tokens:null,
      context_window: Number.isInteger(data.context?.window)?data.context.window:null,
    };
    const response=await request($,"/api/integrations/claude/events",{
      method:"POST",
      headers:{"content-type":"application/json","X-Monitor-Key":key},
      body:JSON.stringify(payload)
    });
    lastSession=session_id;
    lastError="";
    return response;
  })().catch(e=>{
    lastError=errorText(e);
    throw e;
  }).finally(()=>{sending=null});
  return sending;
}

export async function listSharedWorkflows($) {
  const result=await request($,"/api/workflows");
  return Array.isArray(result)?result:[];
}
export async function loadSharedWorkflow($, planId) {
  if(!planId)return null;
  const key=encodeURIComponent(planId);
  return await request($,"/api/workflows/"+key);
}

export async function sendClaudeTask($, entry) {
  const session_id=oneLine(await $.session.id(),128);
  if(!session_id)throw new Error("Session ID unavailable");
  const key=await apiKey($);
  return request($,"/api/integrations/claude/task-events",{
    method:"POST",
    headers:{"content-type":"application/json","X-Monitor-Key":key},
    body:JSON.stringify({
      plan_id:oneLine(entry.planId,128),
      session_id,
      task_id:oneLine(entry.taskId,128),
      action:entry.action,
      subject:entry.subject?oneLine(entry.subject,195):null,
      status:entry.status||null,
      owner:entry.owner?oneLine(entry.owner,128):null,
      event_id:oneLine(entry.eventId,128),
    }),
  });
}
