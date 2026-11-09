"""Install or remove the usage-logging hook in a Claude Code settings file."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER = "cpa_usage_hook"
TOOL_MATCHER = "Skill|Agent|Task|mcp__.*"


def hook_command() -> str:
    script = Path(__file__).with_name("cpa_usage_hook.py").resolve()
    # -I: isolated (ignores env vars and user site), -S: skip site import. Both speed up start.
    return f'"{sys.executable}" -I -S "{script}"'


def _entries() -> dict:
    cmd = {"type": "command", "command": hook_command(), "timeout": 5}
    return {
        "PreToolUse": {"matcher": TOOL_MATCHER, "hooks": [dict(cmd)]},
        "UserPromptSubmit": {"hooks": [dict(cmd)]},
    }


def _is_ours(group: dict) -> bool:
    return any(MARKER in (h.get("command") or "") for h in group.get("hooks", []) or [])


def _strip(settings: dict) -> int:
    """Remove our hook groups. Returns how many were removed."""
    removed = 0
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        groups = hooks[event] or []
        kept = [g for g in groups if not _is_ours(g)]
        removed += len(groups) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks and "hooks" in settings:
        del settings["hooks"]
    return removed


def _write(path: Path, settings: dict) -> Path | None:
    backup = None
    if path.exists():
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
        backup = path.with_name(f"{path.name}.cpa-backup-{stamp}")
        n = 1
        while backup.exists():  # never overwrite an earlier backup
            n += 1
            backup = path.with_name(f"{path.name}.cpa-backup-{stamp}-{n}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".cpa-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written settings file
    return backup


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError:
        sys.exit(f"{path} is not valid JSON. Fix it first; nothing was changed.")


def install(path: Path) -> None:
    settings = _load(path)
    _strip(settings)  # replace an older install, e.g. after moving the repo
    hooks = settings.setdefault("hooks", {})
    for event, group in _entries().items():
        hooks.setdefault(event, []).append(group)
    backup = _write(path, settings)
    print(f"Installed the usage hook in {path}")
    if backup:
        print(f"Backup of the previous file: {backup}")
    print(f"Events: PreToolUse ({TOOL_MATCHER}) and UserPromptSubmit (slash commands only).")
    print("It prints nothing and logs to ~/.claude/plugin-auditor/usage.db.")
    print("Run /hooks in Claude Code to confirm, or start a new session.")


def uninstall(path: Path) -> None:
    settings = _load(path)
    removed = _strip(settings)
    if not removed:
        print(f"No usage hook found in {path}. Nothing changed.")
        return
    backup = _write(path, settings)
    print(f"Removed {removed} hook entries from {path}. Backup: {backup}")
    print("The log database is kept. Delete ~/.claude/plugin-auditor/ to remove it.")


def status(path: Path) -> bool:
    settings = _load(path)
    return any(_is_ours(g) for groups in (settings.get("hooks") or {}).values() for g in groups or [])


def is_stale(path: Path) -> bool:
    """True when an installed hook points at a python or script that no longer exists."""
    for groups in (_load(path).get("hooks") or {}).values():
        for g in groups or []:
            if not _is_ours(g):
                continue
            for h in g.get("hooks", []) or []:
                try:
                    parts = shlex.split(h.get("command") or "")
                except ValueError:
                    return True
                if not all(Path(p).exists() for p in (parts[0], parts[-1]) if parts):
                    return True
    return False
