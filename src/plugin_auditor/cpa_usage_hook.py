"""Claude Code hook: log skill, agent, MCP, and slash-command use to SQLite.

Registered for PreToolUse (matcher Skill|Agent|Task|mcp__.*) and UserPromptSubmit.

Rules, so the hook costs nothing:
- Print nothing. For UserPromptSubmit, stdout would be added to Claude's context.
- Always exit 0, even on errors. A logging hook must never block Claude.
- Standard library only, so Python starts fast.
- Never store prompt text: only the slash command's name.

The file name contains "cpa_usage_hook", which the auditor uses to ignore its own runs.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    key TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    session TEXT,
    project TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL
)
"""


def db_path() -> str:
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.environ.get("CPA_DB") or os.path.join(home, "plugin-auditor", "usage.db")


def event_from(data: dict):
    """(kind, name, key suffix) for a hook input, or None to skip."""
    hook = data.get("hook_event_name", "")
    if hook == "PreToolUse":
        tool = data.get("tool_name", "")
        inp = data.get("tool_input") or {}
        ref = data.get("tool_use_id") or ""
        if tool == "Skill" and inp.get("skill"):
            return "skill", str(inp["skill"]), ref
        if tool in ("Agent", "Task") and inp.get("subagent_type"):
            return "agent", str(inp["subagent_type"]), ref
        if tool.startswith("mcp__"):
            return "mcp", tool, ref
        return None
    if hook == "UserPromptSubmit":
        prompt = (data.get("prompt") or "").lstrip()
        if prompt.startswith("/") and len(prompt) > 1:
            name = prompt[1:].split(None, 1)[0]
            return "command", name, ""
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
        found = event_from(data)
        if not found:
            return
        kind, name, ref = found
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        session = data.get("session_id", "")
        key = ref or f"{session}:{ts}:{kind}:{name}"
        path = db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        con = sqlite3.connect(path, timeout=2)
        try:
            con.execute(SCHEMA)
            con.execute(
                "INSERT OR IGNORE INTO events (key, ts, session, project, kind, name) VALUES (?, ?, ?, ?, ?, ?)",
                (key, ts, session, data.get("cwd", ""), kind, name),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass  # never block or slow Claude because of logging


if __name__ == "__main__":
    main()
    sys.exit(0)
