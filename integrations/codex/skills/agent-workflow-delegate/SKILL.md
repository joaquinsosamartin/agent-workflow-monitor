---
name: agent-workflow-delegate
description: Track explicit Codex or Claude task delegation using the local Agent Workflow Monitor, with phase IDs, provider handoffs, and execution outcomes.
---

# Agent Workflow Monitor — Codex orchestration

Use this skill only when the user wants to track delegated work in a
**specific, already-created Agent Workflow Monitor plan**.

## Rules

- Never invent plan IDs, phase IDs, provider identities, agent names or sessions.
- Keep the orchestrator (e.g. Codex) separate from the delegated executor
  (e.g. Claude). A phase can be handed off without copying it.
- Do not infer that a shell child process is a Claude agent solely from its PID.
- Record work through the local executable wrapper only when you are actually
  launching that delegated command. Do not claim it tracks previous processes.
- Never send prompts, command arguments, environment variables or credentials
  to the monitor API. The wrapper stores execution identity, provider, agent,
  optional known session ID, PID, start, exit status and UTC timestamps.
- If the monitor server is unavailable, do not continue a new delegated
  execution untracked. Explain the failure rather than silently bypassing it.
- Never mark acceptance criteria fulfilled solely because a process exited
  successfully; a successful process exit is execution status, not proof of
  feature acceptance or merged code.
- Do not use GitHub Actions.

## Start the monitor

From the cloned project repository, launch:

```powershell
.\.venv\Scripts\python.exe -m monitor serve
```

## Track a real delegated Claude phase

Example syntax (substitute the real IDs and actual command):

```powershell
.\.venv\Scripts\python.exe -m monitor delegate `
  --plan pops-customer-api --phase qa `
  --provider claude --agent qa-reviewer `
  --orchestrator-provider codex `
  --orchestrator-session YOUR_KNOWN_CODEX_SESSION_ID `
  -- claude -p "Review the changes and report any findings"
```

If no Codex session ID is known, omit `--orchestrator-session`.
If the delegated Claude session ID is known, pass it via `--session`.
Do not guess either identifier.

The wrapper waits for the child and records its exit code. The selected phase becomes `running` on launch. A successful exit completes the execution record but leaves the phase `waiting` for explicit acceptance; a failed exit marks the phase `failed` if its ownership has not changed.

## Inspect the result

`GET http://127.0.0.1:8765/api/workflows/pops-customer-api` returns
the phase status and audit events.

`GET http://127.0.0.1:8765/api/workflows/pops-customer-api/executions`
returns the delegated execution timeline.

Only use a verified workflow ID and agent session. The Claude Mod can
display the same plan via `/workflow`.
