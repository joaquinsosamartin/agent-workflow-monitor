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
