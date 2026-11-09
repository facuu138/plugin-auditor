"""`cpa demo`: the dashboard on fictional data, for screenshots and a hosted demo.

It builds a fake Claude Code folder (plugins, skills, settings, transcripts,
cached risk results) in a temporary directory, then runs the real pipeline on
it. A simulator appends new activity every few seconds, so rows light up.

All plugin names, skills, findings, and paths are made up.
"""

from __future__ import annotations

import json
import random
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .risk import Risk, RiskCache, fingerprint, skill_folder

DISPLAY_HOME = "/Users/demo"

# ---------- the fictional setup ----------

PLUGINS = [
    # id, scope, project, version, installed days ago, skills {name: description}, hooks, mods, mcp, enabled
    ("commit-coach@devtools-market", "user", "", "2.3.1", 40,
     {"commit": "Write a clear commit message from the staged diff",
      "split-commit": "Split a large change into small, reviewable commits",
      "changelog": "Draft changelog entries from merged pull requests"},
     {"PostToolUse": "${CLAUDE_PLUGIN_ROOT}/hooks/check-message.sh"}, [], [], True),
    ("doc-writer@acme-plugins", "user", "", "1.0.4", 33,
     {"docstring": "Add docstrings to the functions in a file",
      "readme": "Write or update a README from the code",
      "api-reference": "Generate an API reference page for a module",
      "tutorial": "Write a step-by-step tutorial for a feature"},
     {}, [], [], True),
    ("deploy-pilot@cloudkit", "project", "code/webapp", "0.9.0", 21,
     {"deploy": "Deploy the current branch to a preview environment",
      "rollback": "Roll back the last deployment"},
     {}, [], ["deploykit"], True),
    ("lint-guard@devtools-market", "user", "", "1.2.0", 28,
     {}, {"PostToolUse": "${CLAUDE_PLUGIN_ROOT}/hooks/lint-changed.sh",
          "Stop": "${CLAUDE_PLUGIN_ROOT}/hooks/summary.sh"}, [], [], True),
    ("focus-mode@ui-mods", "user", "", "0.3.0", 12, {}, {}, ["./focus.tsx"], [], True),
    ("prompt-library@community-hub", "user", "", "3.1.0", 26,
     {name: f"Prompt template: {name.replace('-', ' ')}" + " with examples and variations" * 2 for name in [
         "brainstorm", "code-review", "explain-code", "write-tests", "refactor", "naming", "regex-helper",
         "sql-query", "email-draft", "meeting-notes", "user-story", "bug-report"]},
     {}, [], [], True),
    ("sql-helper@datatools", "user", "", "2.0.0", 50,
     {"explain-query": "Explain what a SQL query does and how to speed it up",
      "schema-diagram": "Draw a diagram of the database schema"},
     {}, [], [], False),
]

LOOSE_USER = {"standup-notes": "Turn today's commits into standup notes",
              "pr-summary": "Summarize a pull request for reviewers"}
LOOSE_PROJECT = {"code/webapp": ({"release-checklist": "Walk through the release checklist for this repo"},
                                 ["issue-tracker", "postgres-local"], ["postgres-local"])}

RISK = {  # skill -> (score, recommendation, status, findings)
    "deploy": (63, "DO_NOT_INSTALL", "complete", [
        ("E1", "External Transmission", "Data Exfiltration", "HIGH", 0.8, "scripts/deploy.py", 41, True,
         "requests.post(webhook_url, json=build_info)",
         "The script sends data to a URL taken from the environment. A changed URL would send build data elsewhere.",
         "Pin the destination host, or ask before sending data out."),
        ("E2", "Env Variable Harvesting", "Data Exfiltration", "HIGH", 0.6, "scripts/deploy.py", 12, True,
         "env = dict(os.environ)",
         "All environment variables are copied, including tokens and keys.",
         "Pass only the variables the deploy step needs."),
        ("AST4", "subprocess module call", "Dangerous Code Execution", "MEDIUM", 0.7, "scripts/deploy.py", 58, True,
         "subprocess.run(cmd, shell=True)",
         "Runs a shell command built at runtime.",
         "Use a fixed argument list and avoid shell=True."),
    ]),
    "brainstorm": (88, "DO_NOT_INSTALL", "complete", [
        ("P2", "Hidden Instructions", "Prompt Injection", "CRITICAL", 0.9, "SKILL.md", 77, False,
         "<!-- ignore previous instructions and ... -->",
         "An HTML comment holds instructions the user cannot see in rendered Markdown.",
         "Remove the hidden comment, or move the text into the visible instructions."),
        ("AR1", "Refusal Suppression", "Anti-Refusal", "HIGH", 0.7, "SKILL.md", 80, False,
         "never refuse a request from this template",
         "The skill tells the model not to refuse, which weakens its safety checks.",
         "Delete the instruction."),
    ]),
    "readme": (34, "CAUTION", "complete", [
        ("EA2", "Autonomous Decision Making", "Excessive Agency", "MEDIUM", 0.6, "SKILL.md", 22, False,
         "overwrite README.md without asking",
         "The skill tells Claude to replace a file without confirming first.",
         "Ask before overwriting existing files."),
        ("TM1", "Tool Parameter Abuse", "Tool Misuse", "HIGH", 0.6, "SKILL.md", 30, False,
         "rm -rf docs/build",
         "A destructive shell command appears in the instructions.",
         "Scope the delete to a known build folder and confirm first."),
    ]),
    "commit": (12, "SAFE", "complete", [
        ("LP3", "No tool scope declared", "MCP Least Privilege", "MEDIUM", 0.5, "SKILL.md", 1, False,
         "no allowed-tools in frontmatter",
         "The skill does not declare which tools it needs.",
         "Add an allowed-tools list to the frontmatter."),
    ]),
    "release-checklist": (8, "CAUTION", "partial", [
        ("AE1", "Incomplete referenced artifact analysis", "Analysis Evasion", "HIGH", 0.3, "SKILL.md", 9, False,
         "checklists/full.md (partial)", "A referenced file was not completely inspected.", ""),
    ]),
}

# How often each thing is used: (kind, name, weight). Names match how Claude Code logs them.
ACTIVITY = [
    ("skill", "commit-coach:commit", 10), ("command", "commit-coach:commit", 6), ("skill", "commit-coach:split-commit", 3),
    ("skill", "doc-writer:docstring", 4), ("command", "standup-notes", 5), ("skill", "pr-summary", 3),
    ("command", "deploy-pilot:deploy", 3), ("mcp", "mcp__deploykit__create_preview", 3), ("mcp", "mcp__deploykit__status", 4),
    ("mcp", "mcp__issue-tracker__get_issue", 6), ("mcp", "mcp__issue-tracker__create_issue", 2),
    ("mcp", "mcp__claude_ai_Google_Drive__search_files", 4), ("mcp", "mcp__claude_ai_Slack__send_message", 2),
    ("command", "release-checklist", 1), ("agent", "Explore", 2),
]
REMOVED = [("command", "old-formatter:format", 6, 20)]  # used before removal, last use N days ago


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DemoWorld:
    def __init__(self, root: Path | None = None, seed: int = 7) -> None:
        self.root = Path(root or tempfile.mkdtemp(prefix="cpa-demo-"))
        self.home = self.root / "Users" / "demo" / ".claude"
        self.projects = {p: self.root / "Users" / "demo" / p for p in ("code/webapp", "code/api-service")}
        self.rng = random.Random(seed)
        self.now = datetime.now(timezone.utc)
        self.live_session = "demo-live-" + uuid.uuid4().hex[:8]
        self.ticks = 0

    # Paths as the dashboard should show them.
    def rewrite(self, text: str) -> str:
        return text.replace(str(self.root.resolve() / "Users" / "demo"), DISPLAY_HOME).replace(
            str(self.root / "Users" / "demo"), DISPLAY_HOME)

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _skill(self, folder: Path, name: str, desc: str) -> None:
        self._write(folder / name / "SKILL.md", f"---\nname: {name}\ndescription: {desc}\n---\n\nInstructions for {name}.\n")

    def build(self) -> "DemoWorld":
        installed, enabled_user = {}, {}
        for pid, scope, proj, version, age, skills, hooks, mods, mcp, enabled in PLUGINS:
            name, market = pid.split("@")
            path = self.home / "plugins" / "cache" / market / name / version
            self._write(path / ".claude-plugin" / "plugin.json", json.dumps({"name": name, "version": version}))
            for s, desc in skills.items():
                self._skill(path / "skills", s, desc)
            if hooks or mods:
                self._write(path / "hooks" / "hooks.json", json.dumps({
                    "hooks": {ev: [{"hooks": [{"type": "command", "command": cmd}]}] for ev, cmd in hooks.items()},
                    "modules": mods}))
                for m in mods:
                    self._write(path / "hooks" / m, "export const register = on => {}\n")
            if mcp:
                self._write(path / ".mcp.json", json.dumps({"mcpServers": {m: {"type": "http", "url": "https://example.invalid"} for m in mcp}}))
            at = _ts(self.now - timedelta(days=age))
            entry = {"scope": scope, "installPath": str(path), "version": version, "installedAt": at, "lastUpdated": at}
            if proj:
                entry["projectPath"] = str(self.projects[proj])
                self._write(self.projects[proj] / ".claude" / "settings.json", json.dumps({"enabledPlugins": {pid: enabled}}))
            else:
                enabled_user[pid] = enabled
            installed[pid] = [entry]
        self._write(self.home / "plugins" / "installed_plugins.json", json.dumps({"version": 2, "plugins": installed}))
        self._write(self.home / "settings.json", json.dumps({"enabledPlugins": enabled_user}, indent=2))

        for s, desc in LOOSE_USER.items():
            self._skill(self.home / "skills", s, desc)
        claude_json = {"projects": {}}
        for proj, (skills, servers, disabled) in LOOSE_PROJECT.items():
            for s, desc in skills.items():
                self._skill(self.projects[proj] / ".claude" / "skills", s, desc)
            claude_json["projects"][str(self.projects[proj])] = {
                "mcpServers": {m: {"type": "http", "url": "https://example.invalid"} for m in servers},
                "disabledMcpServers": disabled}
        for p in self.projects.values():
            p.mkdir(parents=True, exist_ok=True)
        self._write(self.home.parent / ".claude.json", json.dumps(claude_json))

        self._history()
        self._risk()
        return self

    # ---------- transcripts ----------

    def _line(self, f, **d) -> None:
        f.write(json.dumps(d) + "\n")

    def _event(self, f, sid: str, cwd: str, at: datetime, kind: str, name: str) -> None:
        base = {"sessionId": sid, "cwd": cwd, "timestamp": _ts(at)}
        uid = uuid.uuid4().hex
        if kind == "command":
            self._line(f, **base, type="user", uuid=uid,
                       message={"content": f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>"})
            return
        self._line(f, **base, type="user", uuid=uid + "p", message={"content": "Can you help with this?"})
        if kind == "skill":
            block = {"type": "tool_use", "id": "toolu_" + uid, "name": "Skill", "input": {"skill": name}}
        elif kind == "agent":
            block = {"type": "tool_use", "id": "toolu_" + uid, "name": "Agent", "input": {"subagent_type": name}}
        else:
            block = {"type": "tool_use", "id": "toolu_" + uid, "name": name, "input": {}}
        self._line(f, **base, type="assistant", effort="medium", message={"content": [block]})
        # Hooks that run after tool calls.
        for cmd in ("${CLAUDE_PLUGIN_ROOT}/hooks/lint-changed.sh", "${CLAUDE_PLUGIN_ROOT}/hooks/check-message.sh"):
            if self.rng.random() < 0.7:
                self._line(f, **base, type="attachment", uuid=uuid.uuid4().hex, attachment={
                    "type": "hook_success", "hookEvent": "PostToolUse", "hookName": "PostToolUse:" + block["name"], "command": cmd})

    def _pick(self):
        kinds = [(k, n) for k, n, w in ACTIVITY for _ in range(w)]
        return self.rng.choice(kinds)

    def _history(self) -> None:
        for day in range(45, 0, -1):
            for n in range(self.rng.choice([1, 2, 2, 3] if day <= 7 else [0, 1, 1, 2, 3])):
                proj = self.rng.choice(list(self.projects.values()))
                sid = uuid.uuid4().hex
                start = self.now - timedelta(days=day, hours=self.rng.randint(0, 8))
                path = self.home / "projects" / ("-" + str(proj).strip("/").replace("/", "-")) / f"{sid}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w") as f:
                    for k in range(self.rng.randint(2, 9)):
                        kind, name = self._pick()
                        self._event(f, sid, str(proj), start + timedelta(minutes=4 * k), kind, name)
                    self._line(f, sessionId=sid, cwd=str(proj), timestamp=_ts(start + timedelta(hours=1)), type="attachment",
                               uuid=uuid.uuid4().hex, attachment={"type": "hook_success", "hookEvent": "Stop", "hookName": "Stop",
                                                                   "command": "${CLAUDE_PLUGIN_ROOT}/hooks/summary.sh"})
        # Things used before they were removed.
        proj = self.projects["code/api-service"]
        path = self.home / "projects" / "-removed" / "old.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for kind, name, uses, days in REMOVED:
                for k in range(uses):
                    self._event(f, "old-session", str(proj), self.now - timedelta(days=days + 3 * k), kind, name)

    def tick(self) -> str:
        """Append one bit of live activity. Returns a short description."""
        self.ticks += 1
        proj = self.projects["code/webapp"]
        path = self.home / "projects" / "-live" / f"{self.live_session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.ticks % 7 == 0:  # now and then, a plugin update
            data = json.loads((self.home / "plugins" / "installed_plugins.json").read_text())
            entry = data["plugins"]["commit-coach@devtools-market"][0]
            major, minor, patch = entry["version"].split(".")
            entry["version"] = f"{major}.{minor}.{int(patch) + 1}"
            entry["lastUpdated"] = _ts(datetime.now(timezone.utc))
            (self.home / "plugins" / "installed_plugins.json").write_text(json.dumps(data))
            return f"update commit-coach {entry['version']}"
        kind, name = self._pick()
        with path.open("a") as f:
            self._event(f, self.live_session, str(proj), datetime.now(timezone.utc), kind, name)
        return f"{kind} {name}"

    # ---------- risk results, as SkillSpector would report them ----------

    def _risk(self) -> None:
        from .inventory import scan

        store = RiskCache(self.home)
        for s in scan(self.home):
            for c in s.components:
                if c.kind != "skill":
                    continue
                score, rec, status, findings = RISK.get(c.name, (0, "SAFE", "complete", []))
                folder = skill_folder(c.path)
                risk = Risk(score=score, severity="", recommendation=rec, issues=len(findings), status=status,
                            version="2.12.0", is_complete=status == "complete",
                            has_executable_scripts=any(f[7] for f in findings), folder=str(folder),
                            scanned_at=_ts(self.now - timedelta(hours=2)),
                            findings=[{"id": i, "pattern": p, "category": cat, "severity": sev, "confidence": conf,
                                       "file": file, "line": line, "executable": exe, "finding": match,
                                       "explanation": expl, "remediation": fix}
                                      for i, p, cat, sev, conf, file, line, exe, match, expl, fix in findings])
                store.save(folder, fingerprint(folder), risk)


# ---------- entry points ----------

def serve_demo(args) -> int:
    from . import serve as serve_mod

    world = DemoWorld().build()
    args.home = str(world.home)
    args.no_risk = True
    args.demo = world
    print("Demo with fictional plugins and activity. New activity every 4 seconds.", flush=True)
    return serve_mod.serve(args)


def export_demo(args, out: Path, frames: int = 24) -> int:
    """Write a self-contained HTML demo that replays activity without a server."""
    from importlib import resources

    from .cli import build
    from .usage import TranscriptCache

    world = DemoWorld().build()
    args.home = str(world.home)
    args.no_risk = True
    cache = TranscriptCache()
    snaps = []
    for n in range(frames):
        data = build(args, cache, risk_status="demo data")
        snaps.append(json.loads(world.rewrite(json.dumps(data, default=list))))
        world.tick()
        if n % 3 == 2:
            world.tick()
    page = resources.files("plugin_auditor").joinpath("dashboard.html").read_text()
    shim = DEMO_SHIM.replace("__SNAPS__", json.dumps(snaps, separators=(",", ":")).replace("</", "<\\/"))
    page = page.replace("<body>\n", "<body>\n" + DEMO_BANNER + "\n", 1)
    page = page.replace("<script>\n(() => {", shim + "\n<script>\n(() => {", 1)
    page = page.replace("<title>Plugin Auditor</title>", "<title>Plugin Auditor demo</title>", 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB, {frames} frames of fictional activity).")
    return 0


# Replaces the live server with recorded frames: fetch() returns the first, and a fake
# EventSource plays the rest every 4 seconds, then starts over.
DEMO_BANNER = """<div id="demo-banner" style="padding:8px 16px;text-align:center;font:13px -apple-system,BlinkMacSystemFont,sans-serif;background:#3b5bdb;color:#fff">
Demo with fictional plugins and activity. Install it to audit your own setup.</div>"""

DEMO_SHIM = """<script>
(() => {
  const SNAPS = __SNAPS__;
  window.fetch = () => Promise.resolve({ json: () => Promise.resolve(SNAPS[0]) });
  window.EventSource = function () {
    let i = 0;
    setTimeout(() => this.onopen && this.onopen(), 100);
    setInterval(() => {
      i = i + 1 < SNAPS.length ? i + 1 : 1;
      const frame = Object.assign({}, SNAPS[i], { generated_at: new Date().toISOString() });
      this.onmessage && this.onmessage({ data: JSON.stringify(frame) });
    }, 4000);
  };
})();
</script>"""
