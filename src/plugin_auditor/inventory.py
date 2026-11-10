"""Inventory: what is installed, where, and what each item contains.

Reads only files on disk. Never writes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Component:
    kind: str  # skill | command | agent | hook | mcp | mod
    name: str
    path: str = ""
    description: str = ""
    # For hooks: the event (e.g. PostToolUse) and the raw command string.
    event: str = ""
    command: str = ""
    status_message: str = ""  # transcripts log this instead of the command when set
    # When this item appeared: plugin install date, or file creation time for loose items.
    added: str = ""
    # When its file last changed. Used to light up updated items in the dashboard.
    modified: str = ""
    # Turned off: a disabled MCP server, or any item of a disabled plugin. Not loaded, so not "unused".
    disabled: bool = False
    # For UI mods: names they register as tools or slash commands (best effort).
    registers: set[str] = field(default_factory=set)

    @property
    def context_tokens(self) -> int:
        """Rough tokens this item adds to every session's system prompt."""
        if self.kind in ("skill", "command", "agent"):
            return max(1, (len(self.name) + len(self.description)) // 4)
        return 0


@dataclass
class Source:
    """A plugin, or a bundle of loose items at one scope (e.g. user skills)."""

    id: str  # "brag@brag" or "loose:user" or "loose:project:/path"
    kind: str  # plugin | loose
    name: str  # plugin name used as namespace ("brag")
    scope: str  # user | project | local | managed | unknown
    project: str = ""
    version: str = ""
    installed_at: str = ""
    install_path: str = ""
    last_updated: str = ""
    manifest_name: str = ""
    enabled: bool | None = None
    components: list[Component] = field(default_factory=list)

    @property
    def context_tokens(self) -> int:
        return sum(c.context_tokens for c in self.components)


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _modified(path: Path) -> str:
    from datetime import datetime, timezone

    try:
        ts = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _created(path: Path) -> str:
    """File creation time (birth time on macOS, else mtime), ISO 8601 UTC."""
    from datetime import datetime, timezone

    try:
        st = path.stat()
    except OSError:
        return ""
    ts = getattr(st, "st_birthtime", None) or st.st_mtime
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


_REG = re.compile(r"""(?:tool|command)\.register\(\s*\{\s*name:\s*['"`]([\w:-]+)['"`]""")
_STR = re.compile(r"""['"`]([a-z][\w-]{1,40})['"`]""")


def _mod_registers(mod: Path) -> set[str]:
    """Literal names a mod registers, plus short string literals as candidates.

    Mods register commands and tools at runtime, so this is a heuristic.
    `register` names are certain; other literals only match commands that
    nothing else explains.
    """
    names: set[str] = set()
    for f in [mod] + sorted(mod.parent.glob("*.ts*")):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        names |= {"=" + n for n in _REG.findall(text)}
        names |= set(_STR.findall(text))
    return names


_FM = re.compile(r"^---\s*\n(.*?)\n---", re.S)


def _frontmatter(path: Path) -> dict[str, str]:
    """Minimal YAML frontmatter reader: top-level `key: value` lines only."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    m = _FM.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if kv:
            key = kv.group(1)
            out[key] = kv.group(2).strip().strip("\"'")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def _skills_in(root: Path) -> list[Component]:
    comps = []
    if not root.is_dir():
        return comps
    for md in sorted(root.glob("*/SKILL.md")):
        if md.parent.name.startswith(".") or md.parent.name == "synced":
            continue
        fm = _frontmatter(md)
        comps.append(
            Component("skill", fm.get("name") or md.parent.name, str(md), fm.get("description", ""),
                      added=_created(md), modified=_modified(md))
        )
    return comps


def _mds_in(root: Path, kind: str) -> list[Component]:
    comps = []
    if not root.is_dir():
        return comps
    for md in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in md.relative_to(root).parts):
            continue
        fm = _frontmatter(md)
        name = fm.get("name") or md.relative_to(root).with_suffix("").as_posix().replace("/", ":")
        comps.append(Component(kind, name, str(md), fm.get("description", ""), added=_created(md),
                               modified=_modified(md)))
    return comps


def _hooks_from(hooks: dict, origin: str) -> list[Component]:
    comps = []
    for event, groups in (hooks or {}).items():
        for group in groups or []:
            for h in group.get("hooks", []) or []:
                cmd = h.get("command") or h.get("prompt") or h.get("url") or ""
                if "cpa_usage_hook" in cmd:
                    continue  # the auditor's own logging hook
                matcher = group.get("matcher", "")
                label = f"{event}" + (f"[{matcher}]" if matcher else "")
                comps.append(Component("hook", label, origin, event=event, command=cmd,
                                       status_message=h.get("statusMessage", "")))
    return comps


def _paths(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def plugin_components(install: Path) -> list[Component]:
    """Everything a plugin provides. Honors custom paths in plugin.json."""
    comps: list[Component] = []
    manifest = _load_json(install / ".claude-plugin" / "plugin.json") or _load_json(install / "plugin.json")

    for d in ["skills"] + _paths(manifest.get("skills")):
        comps += _skills_in(install / d)
    for d in ["commands"] + _paths(manifest.get("commands")):
        comps += _mds_in(install / d, "command")
    for d in ["agents"] + _paths(manifest.get("agents")):
        comps += _mds_in(install / d, "agent")

    hook_files = ["hooks/hooks.json"] + _paths(manifest.get("hooks"))
    for rel in dict.fromkeys(hook_files):
        f = (install / rel).resolve()
        hj = _load_json(f)
        comps += _hooks_from(hj.get("hooks", {}), str(f))
        for mod in hj.get("modules", []) or []:
            comps.append(Component("mod", mod, str(f), registers=_mod_registers(f.parent / mod)))
    if isinstance(manifest.get("hooks"), dict):
        comps += _hooks_from(manifest["hooks"].get("hooks", manifest["hooks"]), "plugin.json")

    servers = dict(_load_json(install / ".mcp.json").get("mcpServers", {}))
    mcp = manifest.get("mcpServers")
    if isinstance(mcp, dict):
        servers.update(mcp)
    for rel in _paths(mcp):
        servers.update(_load_json(install / rel).get("mcpServers", {}))
    for name in servers:
        comps.append(Component("mcp", name, str(install)))

    # The same file can be reached twice (default folder and a manifest path).
    seen, unique = set(), []
    for c in comps:
        key = (c.kind, c.name, c.path, c.event, c.command)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def _settings_files(claude_home: Path, projects: list[str]) -> list[tuple[str, str, Path]]:
    """(scope, project, path) for every settings file we know about."""
    files = [("user", "", claude_home / "settings.json")]
    for p in projects:
        files.append(("project", p, Path(p) / ".claude" / "settings.json"))
        files.append(("local", p, Path(p) / ".claude" / "settings.local.json"))
    return files


def known_projects(claude_home: Path) -> list[str]:
    """Project folders seen in installed_plugins.json or in transcripts' cwd."""
    found: set[str] = set()
    data = _load_json(claude_home / "plugins" / "installed_plugins.json")
    for entries in data.get("plugins", {}).values():
        for e in entries:
            if e.get("projectPath"):
                found.add(e["projectPath"])
    proj_dir = claude_home / "projects"
    if proj_dir.is_dir():
        for f in proj_dir.glob("*/*.jsonl"):
            try:
                with f.open() as fh:
                    for _, line in zip(range(50), fh):
                        m = re.search(r'"cwd":"([^"]+)"', line)
                        if m:
                            found.add(m.group(1))
                            break
            except OSError:
                pass
    return sorted(p for p in found if Path(p).is_dir())


def default_home() -> Path:
    """Claude Code's config folder: $CLAUDE_CONFIG_DIR if set, else ~/.claude."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def claude_json_path(claude_home: Path) -> Path:
    """MCP servers file: inside a custom config folder if it holds one, else ~/.claude.json."""
    inside = claude_home / ".claude.json"
    return inside if inside.exists() else claude_home.parent / ".claude.json"


def scan(claude_home: Path | None = None) -> list[Source]:
    claude_home = claude_home or default_home()
    projects = known_projects(claude_home)
    sources: list[Source] = []

    # Which plugins are enabled, and at which scope.
    enabled: dict[tuple[str, str], bool] = {}  # (plugin_id, project) -> bool
    settings_hooks: list[tuple[str, str, Path, dict]] = []
    for scope, proj, path in _settings_files(claude_home, projects):
        s = _load_json(path)
        for pid, on in (s.get("enabledPlugins") or {}).items():
            enabled[(pid, proj)] = bool(on)
        if s.get("hooks"):
            settings_hooks.append((scope, proj, path, s["hooks"]))

    # Plugins.
    data = _load_json(claude_home / "plugins" / "installed_plugins.json")
    for pid, entries in data.get("plugins", {}).items():
        for e in entries:
            install = Path(e.get("installPath", ""))
            proj = e.get("projectPath", "")
            manifest = _load_json(install / ".claude-plugin" / "plugin.json") or _load_json(install / "plugin.json")
            comps = plugin_components(install) if install.is_dir() else []
            for c in comps:
                c.added = e.get("installedAt", "")
            on = enabled.get((pid, proj), enabled.get((pid, "")))
            sources.append(
                Source(
                    id=pid if not proj else f"{pid} ({proj})",
                    kind="plugin",
                    name=pid.split("@")[0],
                    scope=e.get("scope", "unknown"),
                    project=proj,
                    version=e.get("version", ""),
                    installed_at=e.get("installedAt", ""),
                    last_updated=e.get("lastUpdated", ""),
                    install_path=str(install),
                    manifest_name=manifest.get("name", ""),
                    enabled=on,
                    components=comps,
                )
            )

    # Loose skills, commands, and agents at user scope.
    user = Source("loose:user", "loose", "", "user", install_path=str(claude_home))
    user.components = (
        _skills_in(claude_home / "skills")
        + _mds_in(claude_home / "commands", "command")
        + _mds_in(claude_home / "agents", "agent")
    )
    sources.append(user)

    # Skills synced from claude.ai (invoked as "anthropic-skills:<name>").
    synced = Source("claude.ai synced skills", "synced", "anthropic-skills", "user",
                    install_path=str(claude_home / "skills" / "synced"))
    for bucket in sorted((claude_home / "skills" / "synced").glob("*/")):
        if not bucket.name.startswith("."):
            synced.components += _skills_in(bucket)
    sources.append(synced)

    # Loose items per project.
    for p in projects:
        base = Path(p) / ".claude"
        if base.resolve() == claude_home.resolve():
            continue  # the home folder: its .claude is the user scope
        src = Source(f"loose:project:{p}", "loose", "", "project", project=p, install_path=str(base))
        src.components = (
            _skills_in(base / "skills") + _mds_in(base / "commands", "command") + _mds_in(base / "agents", "agent")
        )
        for path in (Path(p) / ".mcp.json",):
            for name in _load_json(path).get("mcpServers", {}):
                src.components.append(Component("mcp", name, str(path)))
        sources.append(src)

    # Hooks defined directly in settings files.
    for scope, proj, path, hooks in settings_hooks:
        target = next(
            (s for s in sources if s.kind == "loose" and s.scope == ("user" if scope == "user" else "project") and s.project == proj),
            None,
        )
        if target:
            target.components += _hooks_from(hooks, str(path))

    # User and per-project MCP servers from ~/.claude.json.
    cj = _load_json(claude_json_path(claude_home))
    for name in cj.get("mcpServers", {}) or {}:
        user.components.append(Component("mcp", name, "~/.claude.json"))
    for proj, cfg in (cj.get("projects") or {}).items():
        target = next((s for s in sources if s.id == f"loose:project:{proj}"), None)
        for name in (cfg or {}).get("mcpServers", {}) or {}:
            if target:
                target.components.append(Component("mcp", name, "~/.claude.json (local)"))

    # MCP servers turned off per project (in ~/.claude.json or the project's settings files).
    for p in projects:
        off = set()
        cfg = (cj.get("projects") or {}).get(p) or {}
        off |= set(cfg.get("disabledMcpServers") or []) | set(cfg.get("disabledMcpjsonServers") or [])
        for f in (Path(p) / ".claude" / "settings.json", Path(p) / ".claude" / "settings.local.json"):
            off |= set(_load_json(f).get("disabledMcpjsonServers") or [])
        src = next((s for s in sources if s.id == f"loose:project:{p}"), None)
        for c in (src.components if src else []):
            if c.kind == "mcp" and c.name in off:
                c.disabled = True

    # Everything in a plugin that is not turned on is off: Claude does not load it, so it is neither unused nor a token cost.
    for s in sources:
        if s.kind == "plugin" and s.enabled is not True:
            for c in s.components:
                c.disabled = True

    return [s for s in sources if s.kind == "plugin" or s.components]


def shadowed_skills(sources: list[Source]) -> dict[str, list[str]]:
    """Unnamespaced skill names defined in more than one place."""
    seen: dict[str, list[str]] = {}
    for s in sources:
        if s.kind != "loose":
            continue
        for c in s.components:
            if c.kind == "skill":
                seen.setdefault(c.name, []).append(s.id)
    return {k: v for k, v in seen.items() if len(v) > 1}
