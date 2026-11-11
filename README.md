# Agent Workflow Monitor — MVP 0.1

Local-first agent execution monitor using **Python, FastAPI, SQLite (WAL), and Server-Sent Events (SSE)**.

An interactive vertical workflow timeline shows steps and substeps, the responsible Codex/Claude agent, status, token usage and USD costs. Supports a local demo and programmatic event ingestion.

## Quick start (Windows)

Requirements: Python 3.12+ and Git.

```powershell
git clone https://github.com/joaquinsosamartin/agent-workflow-monitor.git
cd agent-workflow-monitor
.\scripts\start-windows.ps1
```

Open **http://127.0.0.1:8765** and press **Crear ejecución de demostración**.

Cross-platform alternative:

```bash
python -m pip install -r requirements.txt
python -m monitor serve --no-browser
```

## Build standalone Windows EXE

On Windows:

```powershell
.\scripts\build-windows.ps1
.\dist\agent-monitor.exe
```

Windows builds must be produced on Windows. The executable is created with PyInstaller and includes the backend and HTML UI. No Python installation is needed to **run** that EXE. This repository contains source code and the Windows build script; **no precompiled EXE is included**.

Data: `%USERPROFILE%\.agent-monitor\monitor.db` and `api.key`. Override with `AGENT_MONITOR_DATA`. Listens only on `127.0.0.1:8765`.

## API

- `GET /api/runs` — list runs
- `POST /api/runs` — create run
- `POST /api/runs/{run_id}/nodes` — add step or substep
- `POST /api/runs/{run_id}/events` — push status, usage or log event
- `GET /api/runs/{run_id}` — snapshot
- `GET /api/runs/{run_id}/events` — SSE updates
- `GET /api/runs/{run_id}/history` — persisted event history
- `POST /api/demo` — create a populated demonstration

Writes require header `X-Monitor-Key`, read from `~/.agent-monitor/api.key`. Run and workflow IDs are UUIDs by default. The static UI is only served on localhost.

Example for event ingestion after creating run/node via the API:

```json
{
  "type": "usage",
  "node_id": "YOUR_NODE_ID",
  "provider": "codex",
  "agent": "Codex · Developer",
  "usage": {
    "session_id": "session-001",
    "usage_key": "turn-1",
    "input_tokens": 2400,
    "output_tokens": 800,
    "cached_input_tokens": 450,
    "cost_source": "unavailable"
  }
}
```

## Monitor supervised CLIs

After creating a workflow run and a node, start the monitor server and invoke:

```powershell
python -m monitor run --provider codex --run-id YOUR_RUN_ID --node-id YOUR_NODE_ID --agent "Codex" -- codex exec --json "Inspect the project"
python -m monitor run --provider claude --run-id YOUR_RUN_ID --node-id YOUR_NODE_ID --agent "Claude" -- claude -p "Inspect the project" --output-format stream-json --verbose
```

This initial integration is a **best-effort event adapter**. Claude result events may include reported `total_cost_usd`; Codex usage records are stored without inventing a dollar value. Agent event schemas vary by CLI version and must be tested in your environment.

## Attribution and costs

Usage is keyed by `(run_id, session_id, usage_key)` and upserted idempotently. Do not sum overlapping session totals and per-turn usage. Cached input tokens may already be included in input counts, so do not double count them. A cost can be **reported**, **estimated**, or **unavailable**. A subscription's effective billed cost may differ from token pricing.

Substep-level metrics are reliable only when the invocation or event can be attributed to that substep. The monitor does not invent fine-grained attribution from session totals.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Roadmap

1. **Orca integration:** embedded side panel consuming the same SSE/API contract.
2. **Claude Code Mod:** native panel/hooks adapter where supported.
3. **Codex integration:** hooks/CLI/app-server bridge and external dashboard.
4. More accurate subagent tracing, session discovery and duration analytics.

Current limitations: no native Orca/Deck plugin, Claude Mod or Codex plugin yet; no automatic discovery of running CLI sessions. This is an initial local MVP, not a production-hardened multi-user service.


## Automatic local session discovery (v0.2)

**Enabled by default when Agent Monitor starts.** No hooks, modifications to Claude/Codex settings, or changes to how you launch the CLIs are required.

The collector reads **only new complete JSONL lines**, remembering byte offsets in SQLite:

- Claude CLI: \`%USERPROFILE%\.claude\projects\...\*.jsonl\`, plus subagent JSONL files.
- Codex CLI: \`%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl\`.
- Custom roots: \`CLAUDE_CONFIG_DIR\` (parent of \`projects\`) and \`CODEX_HOME\` (parent of \`sessions\`).

**Restart the server** after pulling updates:

\`\`\`powershell
cd C:\git\agent-workflow-monitor
git pull
# Ctrl+C in the old monitor terminal, then:
.\.venv\Scripts\python.exe -m monitor serve
\`\`\`

To see the observer state, open:

\`\`\`text
http://127.0.0.1:8765/api/observer
\`\`\`

Then open Claude Code / Codex CLI as usual and send a prompt. New sessions appear in the left-hand list automatically, without wrapping the CLI process. Sessions with no transcript activity for two minutes are marked **idle**, not completed (idle is an inference from file inactivity).

You can trigger an immediate scan with \`POST /api/observer/scan\` using the API key header. The poll interval defaults to 2 seconds; newly discovered records are visible on the dashboard within a few seconds.

**Optional environment settings**:

- \`AGENT_MONITOR_OBSERVE=0\` — turn off discovery.
- \`AGENT_MONITOR_POLL_SECONDS=5\` — alter scan frequency.
- \`AGENT_MONITOR_LOOKBACK_HOURS=24\` — discover files modified in the past 24 hours.

The default lookback is **12 hours** and at most 250 recent transcript files are scanned per provider per cycle. Already discovered files resume from stored offsets across restarts.

**Privacy:** input prompts, code, tool arguments, tool results, and message bodies are not stored. We retain the source session identifier, project name, tool name, status, usage counters, event metadata, and agent identifier. Tool call identifiers are kept locally in SQLite. The dashboard listens on loopback only. Avoid exposing the API to a network without additional authentication.

**Accuracy and limitations:**

- Claude transcript assistant messages can report model token counts. Costs are **unavailable**, because the transcript does not provide a verified price per message. Some message records represent updates of the same message ID; usage is upserted rather than summed as separate records.
- Codex \`token_count.info.total_token_usage\` is cumulative for a session, so it replaces the previous snapshot instead of being added to it. Cache tokens are a subset of input tokens.
- Costs per substep require actual correlated usage data. The observer does not allocate session totals to tool calls without evidence.
- Formats of local JSONL transcripts are provider-internal and may change. Claude subagent support is best-effort.
- Tool calls become substeps of a session; a Claude subagent has its own child node, which can contain nested tools. Up to 100 tool nodes are collected per parent.
- Automatic task plans, explicit phase names from a SDD workflow, Codex multi-agent attribution, and historical cost estimates require future integrations.
- This does not install a Claude Mod, Codex Plugin, or Orca sidebar plugin. Those remain independent planned adapters of the shared Python core.

Test the observer with synthetic transcript files:

\`\`\`powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
\`\`\`

## Session and agent titles, active/inactive groups, timestamps

The sidebar shows **active sessions first**, highlighted on the left in green, followed by a collapsible inactive group. Each session displays its task title, project, responsible agent, most recently observed operation and update timestamp. Steps/substeps and the activity history also show local date and time (day/month/year, hour:minute). The activity history includes the 40 most recent events.

Session titles are derived from the **first user instruction** in a Claude or Codex transcript, shortened to 105 characters, or from a neutral fallback if unavailable. Claude delegated Task/Agent tool descriptions are displayed as agent operations; the detailed delegated prompt is not retained. This feature stores a short fragment of the user's instruction in local SQLite, unlike the previous metadata-only observer. Do not monitor sensitive projects unless this local retention is acceptable. No content leaves the computer via the monitor.

Session inactivity is inferred from transcript modification time. An inactive session may still have an open process or be waiting for human input.

For changes to appear after `git pull`, restart the FastAPI process and refresh the browser (Ctrl+F5). Previous discovered transcripts are processed incrementally, so titles of **already ingested** sessions may retain fallback values; newly created sessions acquire descriptive titles automatically.

## Less noisy workflow UI (2026-11-11)

- All agent actions/substeps now **start collapsed**. Click a workflow node to reveal them, and click nested nodes to expand deeper operations.
- The overall event history is hidden until you select **Ver historial de acciones**.
- SSE notifications are debounced for 2 seconds. Unchanged session lists are not reconstructed. The SSE endpoint sends only *new* events when no cursor is provided; historical events remain accessible through `/history`.
- Previously discovered sessions are reconciled even when their transcript files fall outside the discovery lookback, preventing stale `running` statuses from persisting forever.
- **Active means recent transcript activity, not verified live OS processes.** The observer waits up to 2 minutes without transcript modification before marking an observed session idle. Claude/Codex sessions may outnumber currently open windows because one CLI can have multiple conversation IDs, session resumes, and past sessions in SQLite. Demo runs and manually created runs are also stored independently.
- The SQLite history is deliberately retained; no existing sessions are deleted by this update.

After `git pull`, restart the monitor process and hard-refresh the browser. For an exact live-process count, a separate process-to-session association mechanism will be needed.

## Process presence detection (v0.3)

`psutil` enumerates Claude/Codex CLI processes locally (read-only). Use:

```text
http://127.0.0.1:8765/api/processes
```

The sidebar distinguishes **verified linked processes** (green when the transcript changed within 2 minutes, amber when the process exists but the transcript is idle), **recent unverified activity** (blue), and a collapsible **inactive/historical** section. It displays the total CLI processes detected and how many sessions could actually be linked.

A correlation is marked *verified* only when the process command line explicitly contains an exact session UUID in a supported `resume <uuid>` / `--resume <uuid>` form and this UUID matches a local Claude/Codex transcript file. Many newly launched CLI processes do **not** expose their session IDs in the command line: these remain **unverified**, even though they may be live. We do not attach a process merely because it shares a working directory or project name.

Process count and session count are different: terminal wrappers, resumed sessions, previous conversation IDs and subagents can cause discrepancies. Also, corporate security software can restrict process inspection. A verified "waiting" process is still running but is not emitting new transcript lines.

Use `git pull`, install the changed dependencies with
` .\.venv\Scripts\python.exe -m pip install -r requirements.txt `,
stop the old monitor process and run it again. This update is intentionally read-only and retains all historical SQLite records.

The Orca adapter, Claude Mod, and Codex plugin are still future integrations; this release only enriches the common local monitor.

## Diagnóstico: Codex visible como proceso pero no como sesión

En Windows, el panel ahora distingue **procesos CLI** y **sesiones observadas**. Un Codex ejecutándose normalmente puede no llevar el ID de sesión en su comando; aparecerá bajo «Procesos sin sesión vinculada» hasta que se encuentre y se correlacione su registro JSONL. No presupone que todos los procesos sean sesiones independientes.

- `GET /api/processes`: totales separados para Codex y Claude, PIDs de procesos sin sesión y relaciones de ascendencia de procesos (son pistas, no pruebas de delegación).
- `GET /api/observer`: rutas comprobadas, existencia de cada directorio, número de JSONL y número de archivos recientes.
- Se observan `%USERPROFILE%\.codex\sessions` y, si `CODEX_HOME` no está configurado, `%APPDATA%\codex\sessions`. No se accede a WSL de forma automática.
- Si Codex está en WSL o utiliza otro `CODEX_HOME`, inicia Agent Monitor en ese entorno o configura una ruta de sesiones accesible por Windows; no dupliques directorios de datos ni copies archivos de autenticación.
- Los títulos y la jerarquía de Claude/Codex siguen dependiendo de los eventos realmente observables; la ascendencia entre procesos no implica que un Claude sea un subagente de un Codex.

Para actualizar sin GitHub Actions:

```powershell
cd C:\git\agent-workflow-monitor
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m monitor serve
```

Después abre `http://127.0.0.1:8765/api/processes` y `http://127.0.0.1:8765/api/observer`. No publiques datos completos de rutas o PID si no quieres compartir detalles de tu equipo.

## Codex sessions not visible on Windows — diagnostic and fix

If `/api/processes` reports Codex processes but the left sidebar shows only Claude:

1. A Codex `task_complete` is **completion of a turn, not the entire session**. The observer now keeps the run available and repairs old incorrect states without deleting history.
2. A malformed line in one rollout file no longer stops discovery of subsequent lines or files.
3. A recognized `session_meta` header creates a visible Codex session immediately, with the project inferred from its Windows working directory.
4. The observer endpoint now exposes `last_scan.by_provider`, `ingested` (distinct sessions and files), and `failing_files`. It does not expose the raw transcript.
5. Unmatched Codex/Claude processes appear **above** historical sessions. Claude subprocesses with a known parent CLI process are visually grouped; these links are process relationships only, not authenticated task delegation.

Update locally (no GitHub Actions are configured):

```powershell
cd C:\git\agent-workflow-monitor
git pull
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m monitor serve
```

Restart the old server before using the new version, and refresh the browser with Ctrl+F5. Then inspect `http://127.0.0.1:8765/api/observer` and `http://127.0.0.1:8765/api/processes`.

**Remaining limitation:** starting Codex normally does not expose its session UUID in the process command line. The process may be visible even if its JSONL is not current or its session cannot be linked; this cannot be safely resolved by matching similar timestamps alone. Showing “Codex orchestrates these two Claude agents” requires correlated launch events/trace IDs or explicit instrumentation.

## Árbol de orquestadores y agentes (11/11/2026)

El lateral incluye ahora **PROCESS TREE**, por encima del historial de sesiones:

- Codex se muestra como proceso detectado, incluso cuando todavía no puede vincularse a un JSONL.
- Las relaciones de proceso hijo/descendiente se reflejan como árbol y se pueden contraer.
- Si un Claude no tiene un padre detectable, puedes pulsar **Asociar manualmente a Codex**, escoger el PID del orquestador y confirmar. Se mostrará como **Vinculación confirmada manualmente**.
- El botón **Desvincular de Codex** elimina esa relación manual.
- La asociación queda vinculada a PID y timestamp de creación de **ambos** procesos, evitando reutilización accidental del PID en una sesión posterior.
- Una asociación manual es tu declaración sobre la orquestación; no demuestra que el registro de tokens de Claude pertenezca al Codex padre. Solo se agregará consumo cuando exista atribución verificable.
- El árbol se obtiene de `GET /api/topology`; `POST /api/topology/links` y `DELETE /api/topology/links/{child_pid}` guardan y eliminan asociaciones autorizadas con `X-Monitor-Key`.

Las sesiones de conversación siguen estando disponibles debajo. El nuevo árbol **no sustituye** su historial ni sus eventos. No hay workflows de GitHub Actions.

### Actualizar y verificar en Windows

Detén el proceso Agent Monitor actual y ejecuta:

```powershell
cd C:\git\agent-workflow-monitor
git pull
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m monitor serve
```

Actualiza el navegador con `Ctrl+F5` y consulta también `http://127.0.0.1:8765/api/topology`. Si los nodos Codex están presentes pero no sus sesiones, revisa el JSON de `/api/observer` para diagnosticar registros JSONL.

## HTTP requests remaining Pending (Windows)

Expected: `GET /api/runs/{id}/events` stays open, because it is Server-Sent Events (SSE).

Unexpected: a second `/api/dashboard`, `/api/topology`, or a `POST /api/topology/links` remaining pending indefinitely. The November 11 fix addresses this by:

- Refreshing Windows process information on a background thread; HTTP requests return the last cached process snapshot without waiting for `psutil.process_iter`.
- Caching the PID ancestry map instead of walking Windows processes synchronously on every HTTP request.
- Returning an HTTP 503 with a logged timeout after 5 seconds for a dashboard/topology/link operation blocked by another lock.
- Aborting browser JSON requests after 8 seconds (SSE connections are deliberately unaffected).

After updating, restart Agent Monitor (not just refresh the tab), then measure responses:

```powershell
cd C:\git\agent-workflow-monitor
git pull
.\.venv\Scripts\python.exe -m monitor serve
```

```powershell
foreach ($url in @('/api/health','/api/dashboard','/api/dashboard','/api/topology')) {
  try {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $reply = Invoke-WebRequest "http://127.0.0.1:8765$url" -TimeoutSec 8 -UseBasicParsing
    $watch.Stop()
    "$url - HTTP $($reply.StatusCode) - $($watch.ElapsedMilliseconds) ms"
  } catch { "$url - ERROR: $($_.Exception.Message)" }
}
```

If `/api/health` is fast but dashboard returns 503, provide the error from the server terminal; this narrows the issue to the database/observer instead of the HTTP event loop. No GitHub Actions workflow is configured or triggered.
