"""Command line: `plugin-auditor` (or its short alias `cpa`)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .inventory import Component, Source, default_home, scan, shadowed_skills
from .usage import Usage, attribute, read_events

NEW_DAYS = 7  # items younger than this are "too new to judge"
INVOCABLE = ("skill", "command", "agent", "mcp")


# ---------- small helpers ----------

def _short(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _day(ts: str) -> str:
    return ts[:10] if ts else "never"


def _age_days(ts: str, now: datetime) -> int | None:
    if not ts:
        return None
    try:
        return (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
    except ValueError:
        return None


def _label(s: Source) -> str:
    if s.kind == "plugin":
        return s.id.split(" (")[0]
    if s.kind == "synced":
        return s.id
    if s.scope == "user":
        return "(loose, user)"
    return f"(loose, {_short(s.project)})"


def _scope(s: Source) -> str:
    where = s.scope + (f" {_short(s.project)}" if s.project else "")
    if s.kind == "plugin" and s.enabled is False:
        where += " [disabled]"
    elif s.kind == "plugin" and s.enabled is None:
        where += " [not enabled]"
    return where


def _invoked_as(s: Source, c: Component) -> str:
    if s.kind in ("plugin", "synced") and c.kind in ("skill", "command", "agent"):
        return f"{s.name}:{c.name}"
    return c.name


def _home(args) -> Path:
    return Path(args.home).expanduser() if args.home else default_home()


def _int_between(lo: int, hi: int):
    def parse(text: str) -> int:
        try:
            n = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number")
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return n
    return parse


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in rows + [headers]) for i in range(len(headers))]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths])] + [line(r) for r in rows])


# ---------- verdicts ----------

def component_verdict(c: Component, u: Usage, now: datetime) -> str:
    if c.disabled and not u.count:
        return "disabled"
    if c.kind == "hook":
        return "active" if u.count else "never fired"
    if c.kind == "mod":
        if not u.count:
            return "unknown (UI mod)"
        return "invoked (inferred)" if set(u.by_kind) == {"signal"} else "invoked"
    if u.count:
        return "invoked"
    age = _age_days(c.added, now)
    if age is not None and age < NEW_DAYS:
        return f"UNUSED (added {age}d ago)"
    return "UNUSED"


def source_verdict(comps: list[dict]) -> str:
    if not comps:
        return "empty?"
    if all(c["verdict"] == "disabled" for c in comps):
        return "disabled"
    comps = [c for c in comps if c["verdict"] != "disabled"]
    inv = [c for c in comps if c["kind"] in INVOCABLE]
    idle = [c for c in inv if not c["uses"]]
    mods = [c for c in comps if c["kind"] == "mod"]
    hooks = [c for c in comps if c["kind"] == "hook"]
    idle_note = f"; {len(idle)}/{len(inv)} idle" if idle and inv else ""

    if any(c["uses"] for c in inv) or any(c["uses"] for c in mods):
        return "invoked" + (f" ({len(idle)}/{len(inv)} idle)" if idle else "")
    if any(c["uses"] for c in hooks):
        return "active (hooks only)" + idle_note
    if mods:
        return "unknown (UI mod)" + idle_note
    if hooks and not inv:
        return "never fired"
    if all(c["verdict"].startswith("UNUSED (added") for c in inv):
        return inv[0]["verdict"]
    return "UNUSED"


# ---------- data ----------

def auto_risk(args, progress=None) -> str:
    """Scan new or changed skills with SkillSpector, unless --no-risk. Returns a status line."""
    from .risk import find_skillspector, scan_all, stale_folders

    if getattr(args, "no_risk", False):
        return "off (--no-risk)"
    if not find_skillspector():
        return "SkillSpector not installed"
    home = _home(args)
    paths = [c.path for s in scan(home) for c in s.components if c.kind == "skill"]
    if not stale_folders(home, paths):
        return "up to date"
    say = progress or (lambda m: print(m, file=sys.stderr, flush=True))
    scan_all(home, paths, progress=say)
    return "up to date"


def build(args, cache=None, risk_status: str = "") -> dict:
    home = _home(args)
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=args.since)).isoformat() if args.since else ""

    sources = scan(home)
    from .risk import RiskCache, cached_risk
    risk_cache = RiskCache(home).load()
    events, sessions = read_events(home, since, cache)
    cutoffs = {w: (now - timedelta(days=n)).isoformat() for w, n in (("7d", 7), ("30d", 30))}
    att = attribute(events, sources, cutoffs)

    items = []
    for s in sources:
        comps = []
        for i, c in enumerate(s.components):
            u = att.components[(s.id, i)]
            comps.append({
                "kind": c.kind,
                "name": c.name,
                "invoked_as": _invoked_as(s, c),
                "path": c.path,
                "added": c.added,
                "modified": c.modified,
                "context_tokens": c.context_tokens,
                "uses": u.count,
                "by_kind": u.by_kind,
                "by_detail": u.by_detail,
                "last_used": u.last,
                "used_in_projects": sorted(u.projects),
                "verdict": component_verdict(c, u, now),
                "disabled": c.disabled,
                "windows": u.windows,
                "risk": (lambda rk: rk.payload() if rk else None)(
                    cached_risk(home, c.path, risk_cache) if c.kind == "skill" and risk_cache else None),
            })
        total = att.source(s.id)
        invoked = sum(c["uses"] for c in comps if c["kind"] in INVOCABLE or c["kind"] == "mod")
        idle_tokens = sum(c["context_tokens"] for c in comps
                          if c["kind"] in INVOCABLE and not c["uses"] and not c["disabled"])
        items.append({
            "id": s.id,
            "label": _label(s),
            "kind": s.kind,
            "scope": s.scope,
            "scope_label": _scope(s),
            "project": s.project,
            "enabled": s.enabled,
            "version": s.version,
            "installed_at": s.installed_at,
            "last_updated": s.last_updated,
            "install_path": s.install_path,
            "context_tokens": s.context_tokens,
            "idle_tokens": idle_tokens,
            "invocations": invoked,
            "hook_runs": total.by_kind.get("hook", 0),
            "windows": total.windows,
            "last_used": total.last,
            "used_in_projects": sorted(total.projects),
            "verdict": source_verdict(comps),
            "components": comps,
        })

    newest = sorted(att.log, key=lambda pair: pair[0].timestamp, reverse=True)
    recent = [p for p in newest if p[0].kind != "hook"][:60] + [p for p in newest if p[0].kind == "hook"][:30]
    recent.sort(key=lambda pair: pair[0].timestamp, reverse=True)
    labels = {s.id: _label(s) for s in sources}
    return {
        "version": __version__,
        "generated_at": now.isoformat(),
        "since": since,
        "recent": [
            {"time": ev.timestamp, "kind": ev.kind, "event": ev.event,
             "name": ev.event if ev.kind == "hook" else ev.name,
             "source": ", ".join(labels.get(o, o) for o in owners), "project": ev.project, "session": ev.session}
            for ev, owners in recent
        ],
        "sessions": {"count": sessions.count, "first": sessions.first, "last": sessions.last,
                     "by_project": sessions.by_project,
                     "windows": {w: sessions.since(c) for w, c in cutoffs.items()}},
        "items": items,
        "unattributed": {
            k: {"uses": v.count, "last_used": v.last, "by_detail": v.by_detail, "windows": v.windows}
            for k, v in sorted(att.unattributed.items(), key=lambda kv: -kv[1].count)
        },
        "shadowed_skills": shadowed_skills(sources),
        "hook": _hook_status(home),
        "risk_status": risk_status,
        "auditor_tokens": _auditor_tokens(risk_cache),
    }


def _auditor_tokens(risk_cache: dict) -> dict:
    """LLM tokens this tool has spent. The auditor itself calls no model; only SkillSpector could."""
    scans = [rk for _, rk in risk_cache.values()]
    return {"llm_tokens": sum(rk.llm_tokens for rk in scans), "scans": len(scans)}


def _hook_status(home: Path) -> str:
    from .hookconfig import is_stale, status
    from .usage import db_events

    try:
        installed = status(home / "settings.json")
    except SystemExit:  # a broken settings.json must not stop a read-only report
        return "unknown (settings.json is not valid JSON)"
    logged = len(db_events(home))
    if installed and is_stale(home / "settings.json"):
        return "installed but BROKEN: its python or script moved. Run `cpa install-hook` again"
    if installed:
        return f"installed, {logged} events logged"
    if logged:
        return f"not installed ({logged} events logged earlier)"
    return "not installed (run `cpa install-hook` to keep history beyond transcript retention)"


# ---------- views ----------

def _contents(comps: list[dict]) -> str:
    counts: dict[str, int] = {}
    for c in comps:
        counts[c["kind"]] = counts.get(c["kind"], 0) + 1
    return ", ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in sorted(counts.items())) or "nothing found"


def view_report(data: dict, with_usage: bool = True) -> None:
    items = sorted(data["items"], key=lambda i: (i["kind"] != "plugin", i["invocations"] > 0, -i["idle_tokens"]))
    rows = []
    for i in items:
        row = [i["label"], i["scope_label"], _contents(i["components"]), str(i["context_tokens"] or "")]
        if with_usage:
            row += [str(i["invocations"]), str(i["hook_runs"] or ""), _day(i["last_used"]), i["verdict"]]
        rows.append(row)
    headers = ["Source", "Scope", "Contents", "~Tokens"]
    if with_usage:
        headers += ["Invoked", "Hook runs", "Last used", "Verdict"]
    print(_table(rows, headers))
    if not with_usage:
        return

    idle = [(i, c) for i in data["items"] for c in i["components"]
            if c["kind"] in ("skill", "command", "agent") and not c["uses"] and not c["disabled"]]
    if idle:
        tokens = sum(c["context_tokens"] for _, c in idle)
        print(f"\n{len(idle)} skills, commands, and agents were never invoked. "
              f"They add about {tokens} tokens to every session where they load. See `cpa skills`.")
    _print_unattributed(data, kinds=None)
    if data["shadowed_skills"]:
        print("\nSkill names defined in more than one place:")
        for name, where in data["shadowed_skills"].items():
            print(f"  {name}: {', '.join(_short(w) for w in where)}")


def view_skills(data: dict) -> None:
    rows = []
    for i in data["items"]:
        for c in i["components"]:
            if c["kind"] not in ("skill", "command", "agent"):
                continue
            by = c["by_kind"]
            claude = by.get("skill", 0) + by.get("agent", 0)
            user = by.get("command", 0)
            risk = c.get("risk")
            rows.append([c["invoked_as"], c["kind"], i["label"], i["scope_label"], _day(c["added"]),
                         str(c["context_tokens"]), str(claude or ""), str(user or ""),
                         _day(c["last_used"]), c["verdict"],
                         ("error" if risk.get("error") else f"{risk['score']} {risk['recommendation']}") if risk else ""])
    rows.sort(key=lambda r: (-(int(r[6] or 0) + int(r[7] or 0)), r[2], r[0]))
    headers = ["Name", "Kind", "Source", "Scope", "Added", "~Tokens", "By Claude", "By you", "Last used", "Verdict", "Risk"]
    has_risk = any(r[-1] for r in rows)
    if not has_risk:
        headers, rows = headers[:-1], [r[:-1] for r in rows]
    print(_table(rows, headers))
    if not has_risk:
        print("\nRun `cpa risk` to add a security risk column (needs NVIDIA SkillSpector).")
    _print_unattributed(data, kinds=("skill", "command", "agent"))


def view_risk(data: dict, args) -> int:
    from .risk import scan_all

    home = _home(args)
    skills = [(i, c) for i in data["items"] for c in i["components"] if c["kind"] == "skill"]
    print("SkillSpector runs in static mode (--no-llm). It still looks up package names on OSV.dev.\n")
    try:
        results = scan_all(home, [c["path"] for _, c in skills], refresh=args.refresh)
    except FileNotFoundError as exc:
        print(exc)
        return 1
    from .risk import skill_folder

    rows = []
    for i, c in skills:
        rk = results.get(str(skill_folder(c["path"])))
        if not rk:
            continue
        if rk.error:
            rows.append([c["invoked_as"], i["label"], "", "error", "", "", rk.error[:60], c["verdict"]])
            continue
        top = rk.top(1)
        finding = f"{top[0]['id']} {top[0]['pattern']} ({top[0]['file']}:{top[0]['line']})" if top else ""
        coverage = "partial" if rk.status == "partial" or rk.coverage_gaps else "full"
        rows.append([c["invoked_as"], i["label"], str(rk.score), rk.recommendation,
                     str(rk.issues - rk.coverage_gaps or ""), coverage, finding[:60], c["verdict"]])
    rows.sort(key=lambda r: -int(r[2] or -1))
    print()
    print(_table(rows, ["Skill", "Source", "Score", "Recommendation", "Findings", "Coverage", "Top finding", "Usage"]))
    print("\nScores are SkillSpector's own, and heuristic: signals to review, not verdicts.")
    print("Coverage 'partial': the scanner could not inspect every referenced file (AE rules), which raises the score.")
    print("Details for one skill: skillspector scan <skill folder> --no-llm")
    return 0


def view_mcp(data: dict) -> None:
    rows = []
    for i in data["items"]:
        for c in i["components"]:
            if c["kind"] != "mcp" and not (c["kind"] == "mod" and c["by_kind"].get("mcp")):
                continue
            tools = sorted(c["by_detail"].items(), key=lambda kv: -kv[1])
            top = ", ".join(f"{t} x{n}" for t, n in tools[:3])
            name = c["name"] if c["kind"] == "mcp" else f"{i['label'].split('@')[0]} (mod tools)"
            rows.append([name, i["label"], i["scope_label"], str(c["uses"]), str(len(tools)),
                         _day(c["last_used"]), top, c["verdict"]])
    for label, u in data["unattributed"].items():
        if label.startswith("claude.ai connector:"):
            tools = sorted(u["by_detail"].items(), key=lambda kv: -kv[1])
            top = ", ".join(f"{t} x{n}" for t, n in tools[:3])
            rows.append([label.split(": ", 1)[1], "claude.ai account", "account",
                         str(u["uses"]), str(len(tools)), _day(u["last_used"]), top, "invoked"])
    rows.sort(key=lambda r: -int(r[3]))
    print(_table(rows, ["Server", "Source", "Scope", "Calls", "Tools used", "Last used", "Top tools", "Verdict"]))
    print("\nclaude.ai connectors are listed only once they have been used. Their full list is not on disk.")
    _print_unattributed(data, kinds=("mcp",))


def removed_items(data: dict) -> list[dict]:
    """Usage that matches nothing installed now: removed plugins, deleted skills, old MCP servers.

    claude.ai connectors are not here: they live in the account, not on disk, and are shown as MCP servers.
    """
    out = []
    for label, v in data["unattributed"].items():
        if label.startswith("claude.ai connector:"):
            continue
        kind, _, name = label.partition(": ")
        plugin = name.split(":", 1)[0] if ":" in name and kind in ("skill", "command", "agent") else ""
        out.append({"label": label, "kind": kind, "name": name, "plugin": plugin, **v})
    return out


def _print_unattributed(data: dict, kinds: tuple[str, ...] | None) -> None:
    rows = [x for x in removed_items(data) if kinds is None or x["kind"] in kinds]
    if not rows:
        return
    print("\nRemoved: used before, but not installed now (or loaded once with --plugin-dir):")
    for x in rows[:15]:
        where = f" (plugin {x['plugin']})" if x["plugin"] else ""
        print(f"  {x['kind']}: {x['name']}{where}: {x['uses']} uses, last {_day(x['last_used'])}")


def _filter(data: dict, text: str) -> dict:
    t = text.lower()
    data["items"] = [i for i in data["items"] if t in i["label"].lower() or t in i["id"].lower()]
    names = {c["name"] for i in data["items"] for c in i["components"]}
    data["recent"] = [e for e in data["recent"] if t in e["source"].lower()]
    data["unattributed"] = {k: v for k, v in data["unattributed"].items() if t in k.lower()}
    data["shadowed_skills"] = {n: w for n, w in data["shadowed_skills"].items() if n in names}
    return data


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="plugin-auditor", description="Audit Claude Code plugins and skills: what, where, and how much used.")
    p.add_argument("view", nargs="?", default="report",
                   choices=["report", "skills", "mcp", "risk", "scan", "serve", "install-hook", "uninstall-hook", "demo"],
                   help="report: per plugin (default); skills: per skill/command/agent; mcp: per MCP server; "
                        "scan: inventory only; serve: live dashboard in the browser; "
                        "install-hook / uninstall-hook: log usage to SQLite as it happens; "
                        "risk: security scan of each skill with NVIDIA SkillSpector; "
                        "demo: the dashboard on fictional data (--export FILE writes a static demo page)")
    p.add_argument("--source", metavar="TEXT", help="only sources whose name contains TEXT")
    p.add_argument("--since", type=_int_between(1, 36500), metavar="DAYS", help="only count usage from the last DAYS days")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    p.add_argument("--home", help="Claude config folder (default ~/.claude)")
    p.add_argument("--refresh", action="store_true", help="with `risk`, rescan even unchanged skills")
    p.add_argument("--no-risk", action="store_true",
                   help="don't scan new or changed skills with SkillSpector (it runs automatically when installed)")
    p.add_argument("--settings", help="settings file for install-hook (default <home>/settings.json)")
    p.add_argument("--export", metavar="FILE", help="with `demo`, write a self-contained HTML demo instead of serving")
    p.add_argument("--port", type=_int_between(1, 65535), default=8765, help="port for `serve` (default 8765)")
    p.add_argument("--no-open", action="store_true", help="with `serve`, don't open the browser")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)
    if args.home and args.view not in ("demo",) and not Path(args.home).expanduser().is_dir():
        p.error(f"--home {args.home}: not a folder")

    if args.view in ("install-hook", "uninstall-hook"):
        from . import hookconfig
        home = _home(args)
        target = Path(args.settings).expanduser() if args.settings else home / "settings.json"
        (hookconfig.install if args.view == "install-hook" else hookconfig.uninstall)(target)
        return 0

    if args.view == "demo":
        from .demo import export_demo, serve_demo
        return export_demo(args, Path(args.export)) if args.export else serve_demo(args)

    if args.view == "serve":
        from .serve import serve
        return serve(args)

    risk_status = "" if args.view == "risk" else auto_risk(args)
    data = build(args, risk_status=risk_status)
    if args.source:
        data = _filter(data, args.source)
    if args.json:
        json.dump(data, sys.stdout, indent=2, default=list)
        print()
        return 0

    ses = data["sessions"]
    window = f", last {args.since} days" if args.since else ""
    print(f"plugin-auditor {data['version']}")
    print(f"Transcripts: {ses['count']} sessions, {_day(ses['first'])} to {_day(ses['last'])}{window}")
    print(f"Usage hook: {data['hook']}")
    if data.get("risk_status"):
        print(f"Risk scan: {data['risk_status']}")
    tok = data.get("auditor_tokens") or {}
    print(f"Tokens used by this auditor: {tok.get('llm_tokens', 0):,} "
          f"(local files only; {tok.get('scans', 0)} SkillSpector scans, no LLM)")
    print()
    if args.view == "risk":
        return view_risk(data, args)
    {"report": view_report, "skills": view_skills, "mcp": view_mcp,
     "scan": lambda d: view_report(d, with_usage=False)}[args.view](data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
