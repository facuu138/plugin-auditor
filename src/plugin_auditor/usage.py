"""Usage: what actually ran, read from Claude Code transcripts.

Transcript format is internal to Claude Code and can change between
versions. Keep all format knowledge in this module.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .inventory import Source, default_home

BUILTIN_COMMANDS = {
    "add-dir", "agents", "bug", "clear", "compact", "config", "context", "cost", "doctor", "exit",
    "effort", "export", "help", "hooks", "ide", "init", "login", "logout", "mcp", "memory", "model",
    "permissions", "plugin", "plugins", "pr-comments", "release-notes", "reload-plugins", "resume",
    "review", "rewind", "status", "statusline", "terminal-setup", "usage", "vim", "fast", "skills",
}

_CMD = re.compile(r"<command-name>/?([^<]+)</command-name>")
_ARGS = re.compile(r"<command-args>([^<]*)</command-args>")

# Behavioral signatures: things a UI mod leaves in transcripts without logging
# a call. Each maps a signal name to the plugin names known to cause it.
# Credit from a signature is inferred, not observed. Contributions welcome.
SIGNATURES: dict[str, list[str]] = {
    # think-hard: a prompt starting with "+" runs that one turn at max effort.
    "max-effort turn": ["think-hard"],
}


@dataclass
class Event:
    kind: str  # skill | command | mcp | agent | hook | signal
    name: str  # skill name, command name, mcp tool, agent type, or hook command
    session: str
    project: str
    timestamp: str
    event: str = ""  # hook event, e.g. PostToolUse
    key: str = ""  # unique id, used to drop duplicates across files


@dataclass
class Sessions:
    count: int = 0
    by_project: dict[str, int] = field(default_factory=dict)
    first: str = ""
    last: str = ""
    starts: list[str] = field(default_factory=list)  # first timestamp of each session

    def since(self, cutoff: str) -> int:
        return sum(1 for ts in self.starts if ts and ts >= cutoff)


def _iter_lines(path: Path):
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def parse_file(path: Path) -> tuple[list[Event], dict[str, tuple[str, str]]]:
    """Events in one transcript, and {session: (project, first timestamp)}."""
    events: list[Event] = []
    sessions: dict[str, tuple[str, str]] = {}
    # Turns, for effort signatures: [session, project, start ts, efforts, effort set by /effort]
    turns: list[list] = []
    effort_cmd: dict[str, str] = {}  # session -> level last set with /effort
    for d in _iter_lines(path):
        ts = d.get("timestamp", "")
        sid = d.get("sessionId", path.stem)
        cwd = d.get("cwd", "")
        if sid and cwd and sid not in sessions:
            sessions[sid] = (cwd, ts)
        msg = d.get("message") or {}
        content = msg.get("content")

        # Turn boundaries: a real user message (not a tool result, not a subagent).
        if d.get("type") == "user" and not d.get("isSidechain") and not d.get("isMeta"):
            is_prompt = isinstance(content, str) or (
                isinstance(content, list)
                and any(isinstance(b, dict) and b.get("type") == "text" for b in content)
                and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            )
            text = content if isinstance(content, str) else ""
            if is_prompt and "<local-command-" not in text:
                turns.append([sid, cwd, ts, set(), effort_cmd.get(sid, "")])
        if d.get("type") == "assistant" and not d.get("isSidechain") and d.get("effort") and turns:
            if turns[-1][0] == sid:
                turns[-1][3].add(d["effort"])

        # Tool calls made by the model.
        if d.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                inp = block.get("input") or {}
                key = block.get("id") or f"{sid}:{ts}:{name}"
                if name == "Skill" and inp.get("skill"):
                    events.append(Event("skill", inp["skill"], sid, cwd, ts, key=key))
                elif name in ("Agent", "Task") and inp.get("subagent_type"):
                    events.append(Event("agent", inp["subagent_type"], sid, cwd, ts, key=key))
                elif name.startswith("mcp__"):
                    events.append(Event("mcp", name, sid, cwd, ts, key=key))

        # Slash commands typed by the user.
        if d.get("type") == "user":
            text = content if isinstance(content, str) else ""
            if isinstance(content, list):
                text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            m = _CMD.search(text or "")
            if m:
                name = m.group(1).strip()
                events.append(Event("command", name, sid, cwd, ts, key=d.get("uuid") or f"{sid}:{ts}"))
                if name == "effort":
                    a = _ARGS.search(text)
                    effort_cmd[sid] = (a.group(1).strip().lower() if a else "")

        # Hook runs.
        att = d.get("attachment") or {}
        if (str(att.get("type", "")).startswith("hook") and att.get("command")
                and "cpa_usage_hook" not in att["command"]):  # our own logging hook
            events.append(Event("hook", att["command"], sid, cwd, ts, event=att.get("hookEvent", ""),
                                key=d.get("uuid") or f"{sid}:{ts}:{att.get('hookName')}"))
    events += _effort_signals(turns)
    return events, sessions


def _effort_signals(turns: list[list]) -> list[Event]:
    """Max-effort turns that the session's own effort setting does not explain.

    A turn counts when it ran at max effort, the user had not set `/effort max`,
    and the session is not at max effort throughout (a max default).
    """
    out: list[Event] = []
    by_session: dict[str, list[list]] = {}
    for turn in turns:
        if turn[3]:
            by_session.setdefault(turn[0], []).append(turn)
    for sid, ts_turns in by_session.items():
        if all("max" in t[3] for t in ts_turns):
            continue  # max is the session default, not a per-turn boost
        for sid_, cwd, ts, efforts, set_by_cmd in ts_turns:
            if "max" in efforts and set_by_cmd != "max":
                out.append(Event("signal", "max-effort turn", sid_, cwd, ts, key=f"{sid_}:{ts}:max-effort"))
    return out


class TranscriptCache:
    """Re-parse only transcripts whose size or mtime changed."""

    def __init__(self) -> None:
        self._files: dict[Path, tuple[tuple[float, int], list[Event], dict]] = {}

    def get(self, path: Path) -> tuple[list[Event], dict]:
        try:
            st = path.stat()
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            return [], {}
        hit = self._files.get(path)
        if hit and hit[0] == stamp:
            return hit[1], hit[2]
        events, sessions = parse_file(path)
        self._files[path] = (stamp, events, sessions)
        return events, sessions


def transcript_files(claude_home: Path) -> list[Path]:
    return sorted((claude_home / "projects").rglob("*.jsonl"))


def db_path(claude_home: Path) -> Path:
    return claude_home / "plugin-auditor" / "usage.db"


def db_events(claude_home: Path) -> list[Event]:
    """Events logged by the usage hook. Empty if the hook was never installed."""
    path = db_path(claude_home)
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute("SELECT key, ts, session, project, kind, name FROM events").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [Event(kind, name, session or "", project or "", ts, key=key)
            for key, ts, session, project, kind, name in rows]


def read_events(claude_home: Path | None = None, since: str = "",
                cache: TranscriptCache | None = None) -> tuple[list[Event], Sessions]:
    claude_home = claude_home or default_home()
    events: list[Event] = []
    seen: set[str] = set()
    sessions: dict[str, tuple[str, str]] = {}
    logged: dict[str, tuple[str, str]] = {}  # sessions known only from the hook log

    for path in transcript_files(claude_home):
        evs, ses = cache.get(path) if cache else parse_file(path)
        for sid, (proj, ts) in ses.items():
            if since and ts and ts < since:
                continue
            if sid not in sessions or (ts and ts < sessions[sid][1]):
                sessions[sid] = (proj, ts)
        for ev in evs:
            if since and ev.timestamp and ev.timestamp < since:
                continue
            if ev.key in seen:
                continue
            seen.add(ev.key)
            events.append(ev)

    # The hook log fills in sessions whose transcripts are gone (retention).
    # While a transcript exists it is the richer source, so hook rows for it are skipped.
    for ev in db_events(claude_home):
        if ev.session in sessions or ev.key in seen:
            continue
        if since and ev.timestamp and ev.timestamp < since:
            continue
        seen.add(ev.key)
        events.append(ev)
        logged.setdefault(ev.session, (ev.project, ev.timestamp))

    for sid, (proj, ts) in logged.items():
        sessions[sid] = (proj, ts)

    s = Sessions(count=len(sessions))
    for proj, ts in sessions.values():
        s.starts.append(ts)
        s.by_project[proj] = s.by_project.get(proj, 0) + 1
        if ts:
            s.first = min(s.first or ts, ts)
            s.last = max(s.last, ts)
    return events, s


@dataclass
class Usage:
    count: int = 0
    last: str = ""
    projects: set[str] = field(default_factory=set)
    by_kind: dict[str, int] = field(default_factory=dict)  # skill | command | mcp | agent | hook
    by_detail: dict[str, int] = field(default_factory=dict)  # e.g. MCP tool names
    windows: dict[str, dict[str, int]] = field(default_factory=dict)  # "7d" -> {kind: count}

    def add(self, ev: Event, detail: str = "", hits: tuple[str, ...] = ()) -> None:
        for w in hits:
            bucket = self.windows.setdefault(w, {})
            bucket[ev.kind] = bucket.get(ev.kind, 0) + 1
        self.count += 1
        self.last = max(self.last, ev.timestamp)
        if ev.project:
            self.projects.add(ev.project)
        self.by_kind[ev.kind] = self.by_kind.get(ev.kind, 0) + 1
        if detail:
            self.by_detail[detail] = self.by_detail.get(detail, 0) + 1


@dataclass
class Attribution:
    # (source id, component index) -> usage of that component
    components: dict[tuple[str, int], Usage]
    # label -> usage that nothing installed explains
    unattributed: dict[str, Usage]
    # every event with what it was credited to, oldest first
    log: list[tuple[Event, list[str]]] = field(default_factory=list)  # credited source ids, or a display label

    def source(self, source_id: str) -> Usage:
        total = Usage()
        for (sid, _), u in self.components.items():
            if sid != source_id:
                continue
            total.count += u.count
            total.last = max(total.last, u.last)
            total.projects |= u.projects
            for k, v in u.by_kind.items():
                total.by_kind[k] = total.by_kind.get(k, 0) + v
            for w, kinds in u.windows.items():
                bucket = total.windows.setdefault(w, {})
                for k, v in kinds.items():
                    bucket[k] = bucket.get(k, 0) + v
        return total


def _mcp_server(tool: str) -> str:
    # mcp__<server>__<tool>; server names may contain single underscores.
    parts = tool.split("__")
    return parts[1] if len(parts) >= 3 else ""


_BUILTIN_AGENTS = {"general-purpose", "Explore", "Plan", "claude", "fork", "statusline-setup", "claude-code-guide",
                   "web-fetch"}

Ref = tuple[Source, int]  # a component: its source and index


def attribute(events: list[Event], sources: list[Source], windows: dict[str, str] | None = None) -> Attribution:
    """Credit each event to the component(s) that provide it.

    `windows` maps a name (e.g. "7d") to a cutoff timestamp; usage is also counted per window.
    """
    windows = windows or {}
    comp_usage: dict[tuple[str, int], Usage] = {
        (s.id, i): Usage() for s in sources for i in range(len(s.components))
    }
    unattributed: dict[str, Usage] = {}
    log: list[tuple[Event, list[str]]] = []

    namespaced: dict[tuple[str, str, str], list[Ref]] = {}  # (namespace, group, name)
    bare: dict[tuple[str, str], list[Ref]] = {}  # (group, name)
    hooks: dict[tuple[str, str], list[Ref]] = {}
    mcp: dict[str, list[Ref]] = {}
    mods: list[Ref] = []
    for s in sources:
        namespaces = {n for n in (s.name, s.manifest_name) if n} if s.kind in ("plugin", "synced") else set()
        for i, c in enumerate(s.components):
            ref = (s, i)
            if c.kind in ("skill", "command", "agent"):
                group = "agent" if c.kind == "agent" else "invocable"
                bare.setdefault((group, c.name), []).append(ref)
                for ns in namespaces:
                    namespaced.setdefault((ns, group, c.name), []).append(ref)
            elif c.kind == "hook":
                hooks.setdefault((c.event, c.command), []).append(ref)
                if c.status_message:
                    hooks.setdefault((c.event, c.status_message), []).append(ref)
            elif c.kind == "mcp":
                mcp.setdefault(c.name, []).append(ref)
                for ns in namespaces:
                    mcp.setdefault(f"plugin_{ns}_{c.name}", []).append(ref)
            elif c.kind == "mod":
                mods.append(ref)
                for ns in namespaces:  # tools a mod registers appear as mcp__<plugin>__<tool>
                    mcp.setdefault(ns, []).append(ref)

    def pick(cands: list[Ref], ev: Event) -> list[Ref]:
        """Prefer components whose scope covers the event's project."""
        if len(cands) <= 1:
            return cands
        local = [r for r in cands if r[0].project and ev.project.startswith(r[0].project)]
        if local:
            return local
        loose = [r for r in cands if r[0].kind == "loose"]  # a loose skill shadows a plugin's bare name
        return loose or [r for r in cands if not r[0].project] or cands

    for ev in events:
        hits = tuple(w for w, cutoff in windows.items() if ev.timestamp and ev.timestamp >= cutoff)
        refs: list[Ref] = []
        detail = ""
        label = f"{ev.kind}: {ev.name}"
        if ev.kind in ("skill", "command", "agent"):
            group = "agent" if ev.kind == "agent" else "invocable"
            if ":" in ev.name:
                ns, short = ev.name.split(":", 1)
                refs = namespaced.get((ns, group, short), [])
            else:
                refs = bare.get((group, ev.name), [])
            if not refs and ev.kind == "command":
                if ev.name in BUILTIN_COMMANDS:
                    continue
                refs = [m for m in mods if "=" + ev.name in m[0].components[m[1]].registers] or [
                    m for m in mods if ev.name in m[0].components[m[1]].registers
                ]
            if not refs and ev.kind == "agent" and ev.name in _BUILTIN_AGENTS:
                continue
            detail = "typed by user" if ev.kind == "command" else "called by Claude"
        elif ev.kind == "mcp":
            server = _mcp_server(ev.name)
            refs = mcp.get(server, [])
            detail = ev.name.split("__", 2)[-1]
            if not refs and server.startswith("claude_ai_"):
                name = server[len("claude_ai_"):].replace("_", " ")
                unattributed.setdefault(f"claude.ai connector: {name}", Usage()).add(ev, detail, hits)
                log.append((ev, [f"claude.ai {name}"]))
                continue
            label = f"mcp: {server}"
        elif ev.kind == "hook":
            refs = hooks.get((ev.event, ev.name), [])
            label = f"hook: {ev.event} {ev.name}"
        elif ev.kind == "signal":
            names = SIGNATURES.get(ev.name, [])
            refs = [m for m in mods if m[0].name in names]
            detail = f"inferred: {ev.name}"
            label = f"signal: {ev.name}"

        refs = pick(refs, ev)
        if not refs:
            unattributed.setdefault(label, Usage()).add(ev, detail, hits)
            log.append((ev, ["(not installed)"]))
            continue
        credited: set[str] = set()
        for s, i in refs:
            if s.id in credited:  # e.g. one hook command listed under several matchers
                continue
            credited.add(s.id)
            comp_usage[(s.id, i)].add(ev, detail, hits)
        log.append((ev, list(dict.fromkeys(r[0].id for r in refs))))
    return Attribution(comp_usage, unattributed, log)
