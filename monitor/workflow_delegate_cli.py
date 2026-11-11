"""Supervised local phase execution with explicit orchestrator attribution.

Usage:
  python -m monitor delegate --plan my-plan --phase review --provider claude
    --agent qa --orchestrator-provider codex -- claude -p "Review code"

No prompts or command arguments are ever sent to the monitor API.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4


def local_key() -> str:
    root = Path(os.getenv("AGENT_MONITOR_DATA", str(Path.home() / ".agent-monitor")))
    return (root / "api.key").read_text(encoding="utf-8").strip()


def send(port: int, path: str, payload: dict, key: str):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type":"application/json", "X-Monitor-Key":key},
        method="POST",
    )
    with urllib.request.urlopen(req,timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def delegate(args) -> int:
    command=list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ValueError("Command required after --")
    key=local_key()
    execution_id=str(uuid4())
    # The subprocess does not inherit our API key as an environment variable.
    # stdout/stderr are inherited; do not read/store prompts or output.
    child=subprocess.Popen(command)
    started=False
    try:
        start_payload={
            "execution_id":execution_id,
            "provider":args.provider,
            "agent_id":args.agent,
            "session_id":args.session,
            "orchestrator_provider":args.orchestrator_provider,
            "orchestrator_session_id":args.orchestrator_session,
            "pid":child.pid,
        }
        send(args.port, f"/api/workflows/{args.plan}/phases/{args.phase}/executions",
             start_payload,key)
        started=True
        exit_code=child.wait()
    except KeyboardInterrupt:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        exit_code=130
    except BaseException:
        # Fail closed: do not silently run an untracked delegated agent.
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    finally:
        if started:
            actual_exit=exit_code if "exit_code" in locals() else (
                child.returncode if child.returncode is not None else 1
            )
            try:
                send(args.port,f"/api/workflows/executions/{execution_id}/finish",
                     {"exit_code":actual_exit,"event_id":str(uuid4())},key)
            except (OSError,ValueError,urllib.error.HTTPError) as exc:
                print(f"Agent Monitor: could not report completion: {exc}",
                      file=sys.stderr)
    return exit_code
