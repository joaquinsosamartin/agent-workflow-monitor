/* Host-mediated, loopback-only bridge from Claude Mod to local FastAPI.
 * No prompts, tool inputs, tool outputs, API model secrets or command lines.
 * Optional: failure leaves the native pane functioning offline.
 */
const BASE = "http://127.0.0.1:8765";
let lastError = "";
let keyCache = null;
let lastSession = "";
let seq = 0;


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
const pending = [];
const MAX_PENDING = 100;
const BATCH_SIZE = 40;
const BATCH_DELAY_MS = 500;
let flushTimer=null;
let flushing=false;
let dropped=0;
let sent=0;

export function bridgeStatus() {
  return {
    connected:!lastError && !!lastSession, sessionId:lastSession,
    error:lastError, queued:pending.length, dropped, sent,
  };
}
function queueFlush($) {
  if(flushTimer!==null||flushing||!pending.length)return;
  flushTimer=$.clock.after(BATCH_DELAY_MS,()=>{
    flushTimer=null;
    void flush($);
  });
}
async function flush($) {
  if(flushing || !pending.length)return;
  flushing=true;
  const batch=pending.splice(0,BATCH_SIZE);
  try {
    const key=await apiKey($);
    await request($,"/api/integrations/claude/events/batch",{
      method:"POST",
      headers:{"content-type":"application/json","X-Monitor-Key":key},
      body:JSON.stringify(batch),
    });
    lastSession=batch[batch.length-1].session_id;
    lastError="";
    sent+=batch.length;
  } catch(e) {
    lastError=errorText(e);
    // Bounded queue: a down backend cannot grow the Mod's memory forever.
    // Preserve event IDs on retry so the backend can ignore duplicates.
    pending.unshift(...batch);
    if(pending.length>MAX_PENDING){
      dropped+=pending.length-MAX_PENDING;
      pending.splice(0,pending.length-MAX_PENDING);
    }
  } finally {
    flushing=false;
    // On failure, wait a bounded period rather than hammering localhost.
    if(pending.length){
      flushTimer=$.clock.after(lastError?5000:BATCH_DELAY_MS,()=>{
        flushTimer=null;
        void flush($);
      });
    }
    try { $.ui.invalidate("ui.render"); } catch {}
  }
}
export async function sendSnapshot($, data) {
  // The hook only resolves session metadata and queues a small event;
  // localhost HTTP is deferred. Calling code must not await the network.
  const session_id=oneLine(await $.session.id(),128);
  if(!session_id)throw new Error("Claude no expone todavía la sesión");
  const cwd=oneLine(await $.session.cwd(),900);
  const agents=(data.agents||[]).slice(0,30).map(a=>({
    id:oneLine(a.id,128),
    title:oneLine(a.title||"Subagent",145),
    status:["pending","running","waiting","idle","completed","failed","killed"].includes(a.status)?a.status:"pending",
    model:oneLine(a.model,95),
  })).filter(a=>a.id);
  const payload={
    session_id,cwd,agents,
    status:["waiting","idle","failed"].includes(data.status)?data.status:"running",
    tool:data.lastTool?oneLine(data.lastTool.tool,90):null,
    agent_id:data.lastTool?oneLine(data.lastTool.agent,110):null,
    event_id:"mod-"+Date.now()+"-"+(++seq),
    context_tokens:Number.isInteger(data.context?.tokens)?data.context.tokens:null,
    context_window:Number.isInteger(data.context?.window)?data.context.window:null,
    hook_latency_ms:Number.isFinite(data.hookLatencyMs)?Math.max(0,Math.min(60000,data.hookLatencyMs)):null,
  };
  if(pending.length>=MAX_PENDING){pending.shift();dropped++}
  pending.push(payload);
  queueFlush($);
  return {queued:true};
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

export async function loadDelegatedExecutions($, planId) {
  if(!planId)return [];
  const result=await request($,"/api/workflows/"+encodeURIComponent(planId)+"/executions");
  return Array.isArray(result)?result:[];
}
