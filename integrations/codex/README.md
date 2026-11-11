# Codex integration — tracked workflow delegation

This is an **opt-in Codex skill**, not a transparent hook for every Codex
process. It records explicit agent launches through the local monitor
wrapper and shares the same plan/phase model with Claude Code and Orca.

## Install as a personal Codex skill on Windows

From `C:\git\agent-workflow-monitor`:

```powershell
$target = Join-Path $HOME ".agents\skills\agent-workflow-delegate"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item ".\integrations\codex\skills\agent-workflow-delegate\SKILL.md" $target -Force
```

Start a fresh Codex session. The skill should be available when Codex
is asked to delegate a tracked phase. Its instructions do not silently
intercept arbitrary subprocess launches.

## Track a Claude phase that Codex delegates

Create a plan first (see `examples/multi-agent-workflow.json`) and run
the FastAPI server. Then, from a PowerShell terminal or a Codex task
that has permission to run commands:

```powershell
$python = "C:\git\agent-workflow-monitor\.venv\Scripts\python.exe"
$monitor = "C:\git\agent-workflow-monitor\entry.py"

& $python $monitor delegate `
  --plan pops-customer-api `
  --phase qa `
  --provider claude `
  --agent qa-reviewer `
  --orchestrator-provider codex `
  -- claude -p "Review API security and report findings"
```

Only include `--orchestrator-session` or `--session` when the actual
session IDs are known. Do not infer them from timestamps.

The monitor stores:
- Who orchestrated and executed the phase
- The real subprocess PID, if launched
- Execution start/end time and exit status
- Current phase owner and append-only handoff history

It does **not** store:
- The prompt, command line, tool output, or environment secrets
- Inferred costs or unverified token attribution

## Inspect results

- `GET /api/workflows/pops-customer-api` — plan and ownership history
- `GET /api/workflows/pops-customer-api/executions` — delegated runs
- Claude Code Mod `/workflow` — the same plan, including delegation
  history under its expanded phase

An exit code of zero is a successful *process execution*, not proof that
the phase's acceptance criteria are satisfied. If the agent was reassigned
while the delegated process was running, its late completion is retained
as an execution event but cannot overwrite the new phase owner.

If the local monitor is unavailable when launching, the wrapper terminates
the child rather than silently executing it without a trace.

No GitHub Actions workflows are used.
