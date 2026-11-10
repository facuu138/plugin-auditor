"""End-to-end test against a fake ~/.claude folder."""

import json
from pathlib import Path

import pytest

from plugin_auditor.inventory import scan
from datetime import datetime, timezone

from plugin_auditor.cli import component_verdict, source_verdict
from plugin_auditor.usage import attribute, read_events


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def skill(name: str, desc: str) -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\nBody\n"


def make_home(tmp: Path) -> tuple[Path, Path]:
    home = tmp / ".claude"
    proj = tmp / "repo"
    proj.mkdir()
    used = home / "plugins/cache/m/used/1.0"
    idle = home / "plugins/cache/m/idle/1.0"
    hooky = home / "plugins/cache/m/hooky/1.0"
    write(used / "skills/greet/SKILL.md", skill("greet", "Say hello"))
    write(idle / "skills/nap/SKILL.md", skill("nap", "Never called " * 20))
    write(hooky / "hooks/hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/stop.sh"}]}]}}))
    mixed = home / "plugins/cache/m/mixed/1.0"
    write(mixed / ".claude-plugin/plugin.json", json.dumps({"name": "mixy", "hooks": ["./hooks/extra.json"]}))
    write(mixed / "hooks/extra.json", json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "node start.js",
                                                "statusMessage": "Starting mixy..."}]}]},
        "modules": ["./ui.tsx"]}))
    write(mixed / "hooks/ui.tsx", "await $.tool.register({ name: 'peek' })")
    write(mixed / "skills/idle/SKILL.md", skill("idle", "never used"))
    write(home / "plugins/installed_plugins.json", json.dumps({"version": 2, "plugins": {
        "used@m": [{"scope": "user", "installPath": str(used), "installedAt": "2026-01-01T00:00:00Z"}],
        "idle@m": [{"scope": "project", "projectPath": str(proj), "installPath": str(idle),
                    "installedAt": "2026-01-01T00:00:00Z"}],
        "hooky@m": [{"scope": "user", "installPath": str(hooky), "installedAt": "2026-01-01T00:00:00Z"}],
        "mixed@m": [{"scope": "user", "installPath": str(mixed), "installedAt": "2026-01-01T00:00:00Z"}],
    }}))
    write(home / "settings.json", json.dumps({"enabledPlugins": {"used@m": True, "hooky@m": True}}))
    write(proj / ".claude/settings.json", json.dumps({"enabledPlugins": {"idle@m": False}}))
    write(proj / ".claude/skills/local/SKILL.md", skill("local", "A repo skill"))
    write(home / "skills/.trash/x/old/SKILL.md", skill("old", "deleted"))

    base = {"sessionId": "s1", "cwd": str(proj), "timestamp": "2026-02-01T10:00:00Z"}
    lines = [
        {**base, "type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "used:greet"}},
            {"type": "tool_use", "id": "t2", "name": "mcp__claude_ai_Slack__send", "input": {}},
            {"type": "tool_use", "id": "t3", "name": "mcp__mixy__peek", "input": {}},
        ]}},
        {**base, "type": "user", "uuid": "u1", "message": {"content":
            "<command-message>local</command-message>\n<command-name>/local</command-name>"}},
        {**base, "type": "user", "uuid": "u2", "message": {"content": "<command-name>/model</command-name>"}},
        {**base, "type": "attachment", "uuid": "h1", "attachment": {
            "type": "hook_success", "hookEvent": "Stop", "hookName": "Stop",
            "command": "${CLAUDE_PLUGIN_ROOT}/stop.sh"}},
        {**base, "type": "attachment", "uuid": "h2", "attachment": {
            "type": "hook_success", "hookEvent": "SessionStart", "hookName": "SessionStart:startup",
            "command": "Starting mixy..."}},
    ]
    text = "\n".join(json.dumps(l) for l in lines) + "\n"
    write(home / "projects/-repo/s1.jsonl", text + text)  # duplicated lines must not double count
    return home, proj


def test_inventory_and_usage(tmp_path):
    home, proj = make_home(tmp_path)
    sources = scan(home)
    by_id = {s.id: s for s in sources}

    assert by_id["used@m"].scope == "user" and by_id["used@m"].enabled is True
    idle = by_id[f"idle@m ({proj})"]
    assert idle.scope == "project" and idle.enabled is False
    assert idle.context_tokens > 50
    names = [c.name for s in sources for c in s.components]
    assert "old" not in names  # trash is ignored

    events, sessions = read_events(home)
    assert sessions.count == 1
    att = attribute(events, sources)
    usage = att.source
    unattributed = att.unattributed
    assert usage("used@m").count == 1
    assert usage(f"idle@m ({proj})").count == 0
    assert usage("hooky@m").by_kind == {"hook": 1}
    assert usage(f"loose:project:{proj}").count == 1
    assert "claude.ai connector: Slack" in unattributed
    assert not any("model" in k for k in unattributed)  # built-in command

    # Mixed plugin: hook found via plugin.json path and matched by statusMessage;
    # the mod's registered tool is credited to the mod; the skill stays idle.
    mixed = by_id["mixed@m"]
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    comps = []
    for i, c in enumerate(mixed.components):
        u = att.components[(mixed.id, i)]
        comps.append({"kind": c.kind, "uses": u.count, "verdict": component_verdict(c, u, now)})
    kinds = {c["kind"]: c for c in comps}
    assert kinds["hook"]["verdict"] == "active"
    assert kinds["mod"]["verdict"] == "invoked"
    assert kinds["skill"]["verdict"] == "UNUSED"
    assert source_verdict(comps) == "invoked (1/1 idle)"


def test_source_verdicts():
    hook_only = [{"kind": "hook", "uses": 5, "verdict": "active"},
                 {"kind": "skill", "uses": 0, "verdict": "UNUSED"}]
    assert source_verdict(hook_only) == "active (hooks only); 1/1 idle"
    mod_only = [{"kind": "mod", "uses": 0, "verdict": "unknown (UI mod)"},
                {"kind": "skill", "uses": 0, "verdict": "UNUSED"}]
    assert source_verdict(mod_only) == "unknown (UI mod); 1/1 idle"
    assert source_verdict([]) == "empty?"


def _session(path: Path, turns: list[tuple[str, str]], effort_cmd: str = "") -> None:
    """Write a transcript: each turn is (prompt, effort of the reply)."""
    lines = []
    for n, (prompt, effort) in enumerate(turns):
        ts = f"2026-02-01T10:{n:02d}:00Z"
        base = {"sessionId": path.stem, "cwd": "/repo", "timestamp": ts}
        if effort_cmd and n == 0:
            lines.append({**base, "type": "user", "uuid": f"c{n}",
                          "message": {"content": f"<command-name>/effort</command-name><command-args>{effort_cmd}</command-args>"}})
        lines.append({**base, "type": "user", "uuid": f"u{n}", "message": {"content": prompt}})
        lines.append({**base, "type": "assistant", "effort": effort, "message": {"content": [{"type": "text", "text": "ok"}]}})
    write(path, "\n".join(json.dumps(l) for l in lines) + "\n")


def test_max_effort_signature(tmp_path):
    from plugin_auditor.usage import parse_file

    boosted = tmp_path / "a.jsonl"
    _session(boosted, [("hi", "medium"), ("think", "max"), ("bye", "medium")])
    signals = [e for e in parse_file(boosted)[0] if e.kind == "signal"]
    assert [e.name for e in signals] == ["max-effort turn"]

    by_command = tmp_path / "b.jsonl"  # the user set /effort max: not a mod
    _session(by_command, [("hi", "max"), ("again", "max"), ("bye", "medium")], effort_cmd="max")
    assert not [e for e in parse_file(by_command)[0] if e.kind == "signal"]

    default_max = tmp_path / "c.jsonl"  # max all session long: a default, not a boost
    _session(default_max, [("hi", "max"), ("bye", "max")])
    assert not [e for e in parse_file(default_max)[0] if e.kind == "signal"]


def test_hook_event_parsing():
    from plugin_auditor.cpa_usage_hook import event_from

    assert event_from({"hook_event_name": "PreToolUse", "tool_name": "Skill",
                       "tool_input": {"skill": "brag:brag"}, "tool_use_id": "t1"}) == ("skill", "brag:brag", "t1")
    assert event_from({"hook_event_name": "PreToolUse", "tool_name": "mcp__x__y"})[:2] == ("mcp", "mcp__x__y")
    assert event_from({"hook_event_name": "PreToolUse", "tool_name": "Bash"}) is None
    assert event_from({"hook_event_name": "UserPromptSubmit", "prompt": "/ask what now"})[:2] == ("command", "ask")
    assert event_from({"hook_event_name": "UserPromptSubmit", "prompt": "a normal prompt"}) is None


def test_install_uninstall_roundtrip(tmp_path):
    from plugin_auditor import hookconfig

    settings = tmp_path / "settings.json"
    original = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}]}}
    settings.write_text(json.dumps(original))
    hookconfig.install(settings)
    hookconfig.install(settings)  # idempotent
    data = json.loads(settings.read_text())
    assert len(data["hooks"]["PreToolUse"]) == 1 and len(data["hooks"]["UserPromptSubmit"]) == 1
    assert data["hooks"]["Stop"] == original["hooks"]["Stop"]
    assert hookconfig.status(settings)
    hookconfig.uninstall(settings)
    assert json.loads(settings.read_text()) == original
    assert len(list(tmp_path.glob("settings.json.cpa-backup-*"))) == 3  # one per write, none overwritten


def test_hook_log_fills_sessions_without_transcripts(tmp_path):
    import sqlite3
    from plugin_auditor.cpa_usage_hook import SCHEMA

    home, proj = make_home(tmp_path)  # has a transcript for session s1
    db = home / "plugin-auditor" / "usage.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute(SCHEMA)
    con.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", [
        ("k1", "2026-01-15T10:00:00Z", "old", str(proj), "skill", "used:greet"),  # transcript deleted
        ("t1", "2026-02-01T10:00:00Z", "s1", str(proj), "skill", "used:greet"),  # transcript still exists
    ])
    con.commit()
    con.close()

    events, sessions = read_events(home)
    greets = [e for e in events if e.name == "used:greet"]
    assert len(greets) == 2  # one from the transcript, one from the log; the duplicate is skipped
    assert sessions.count == 2

    # The auditor's own hook runs never count as plugin usage.
    t = (home / "projects/-repo/s1.jsonl")
    t.write_text(t.read_text() + json.dumps({
        "sessionId": "s1", "cwd": str(proj), "timestamp": "2026-02-01T11:00:00Z", "type": "attachment",
        "uuid": "own", "attachment": {"type": "hook_success", "hookEvent": "PreToolUse",
                                      "command": "python3 -I -S /x/cpa_usage_hook.py"}}) + "\n")
    events, _ = read_events(home)
    assert not [e for e in events if "cpa_usage_hook" in e.name]


SAMPLE_REPORT = {  # shape of a real SkillSpector 2.12.0 JSON report, trimmed
    "skill": {"name": "demo"},
    "risk_assessment": {"score": 42, "severity": "MEDIUM", "recommendation": "CAUTION", "max_issue_severity": "HIGH"},
    "issues": [
        {"id": "AE1", "pattern": "Incomplete referenced artifact analysis", "severity": "HIGH", "confidence": 0.9,
         "location": {"file": "SKILL.md", "start_line": 3}},
        {"id": "E2", "pattern": "Env Variable Harvesting", "severity": "HIGH", "confidence": 0.6,
         "location": {"file": "scripts/run.py", "start_line": 28}},
        {"id": "AST4", "pattern": None, "severity": "MEDIUM", "confidence": 0.7,
         "location": {"file": "scripts/run.py", "start_line": 40}},
    ],
    "metadata": {"skillspector_version": "2.12.0"},
    "analysis_completeness": {"status": "partial"},
}


def test_parse_skillspector_report():
    from plugin_auditor.risk import parse_report

    rk = parse_report(SAMPLE_REPORT)
    assert (rk.score, rk.recommendation, rk.issues) == (42, "CAUTION", 3)
    assert rk.coverage_gaps == 1
    assert [f["id"] for f in rk.top()] == ["E2", "AST4"]  # coverage gaps are not "top findings"
    assert rk.top()[1]["pattern"] == ""  # a null pattern is tolerated


def test_scan_all_with_fake_skillspector(tmp_path, monkeypatch):
    import os
    import stat
    from plugin_auditor.risk import cached_risk, scan_all

    report = tmp_path / "report.json"
    report.write_text(json.dumps(SAMPLE_REPORT))
    calls = tmp_path / "calls.log"
    fake = tmp_path / "skillspector"
    # Fake CLI: records the target, refuses symlinks like the real one, copies the report to --output.
    fake.write_text(f"""#!/bin/sh
echo "$2" >> "{calls}"
if [ -L "$2" ]; then echo "Refusing to resolve a symlinked input" >&2; exit 2; fi
while [ $# -gt 0 ]; do [ "$1" = "--output" ] && cp "{report}" "$2"; shift; done
exit 0
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CPA_SKILLSPECTOR", str(fake))

    home = tmp_path / ".claude"
    real = tmp_path / "skills/real"
    write(real / "SKILL.md", skill("real", "x"))
    link = tmp_path / "skills/linked"
    os.symlink(real, link)

    paths = [str(real / "SKILL.md"), str(link / "SKILL.md")]
    results = scan_all(home, paths, progress=lambda *_: None)
    assert len(results) == 1  # the symlink resolves to the same folder: scanned once, not refused
    assert next(iter(results.values())).score == 42
    assert len(calls.read_text().split()) == 1

    scan_all(home, paths, progress=lambda *_: None)  # unchanged: served from cache
    assert len(calls.read_text().split()) == 1
    assert cached_risk(home, str(link / "SKILL.md")).score == 42

    (real / "SKILL.md").write_text(skill("real", "changed") + "more")
    assert cached_risk(home, str(real / "SKILL.md")) is None  # stale after a change
    scan_all(home, paths, progress=lambda *_: None)
    assert len(calls.read_text().split()) == 2


def test_risk_reasons():
    from plugin_auditor.risk import parse_report

    rk = parse_report(SAMPLE_REPORT)
    e = rk.explain()
    # HIGH E2: 25 x 0.6 = 15. AE1: 25 x 0.9 = 22.5. AST4 MEDIUM: 10 x 0.7 = 7. Total 44.5.
    assert e["computed"] == 44
    assert e["rules"][0]["id"] == "AE1" and e["rules"][0]["coverage_gap"]
    assert "21 to 50 band" in rk.reason() and "coverage gaps" in rk.reason()

    # Fail closed: a low score with an incomplete scan becomes CAUTION.
    low = parse_report({"risk_assessment": {"score": 7, "recommendation": "CAUTION"},
                        "issues": [{"id": "LP3", "severity": "MEDIUM", "confidence": 0.7}],
                        "analysis_completeness": {"status": "partial", "is_complete": False}})
    assert "fails closed" in low.reason()

    # Floor: findings add up to less than 51, but the score was raised to 51.
    floor = parse_report({"risk_assessment": {"score": 51, "recommendation": "DO_NOT_INSTALL"},
                          "issues": [{"id": "SC9", "severity": "MEDIUM", "confidence": 0.5}]})
    assert "minimum of 51" in floor.reason()


def test_disabled_items_are_not_unused(tmp_path):
    home, proj = make_home(tmp_path)
    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {str(proj): {
        "mcpServers": {"crawler": {"type": "http", "url": "https://example.invalid"}, "kept": {"type": "http"}},
        "disabledMcpServers": ["crawler"]}}}))
    sources = scan(home)
    events, _ = read_events(home)
    att = attribute(events, sources)
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    verdicts = {}
    for s in sources:
        for i, c in enumerate(s.components):
            verdicts[(s.id, c.name)] = component_verdict(c, att.components[(s.id, i)], now)
    assert verdicts[(f"loose:project:{proj}", "crawler")] == "disabled"
    assert verdicts[(f"loose:project:{proj}", "kept")] == "UNUSED"
    # idle@m is turned off in the project's enabledPlugins: its skill is disabled, not unused.
    assert verdicts[(f"idle@m ({proj})", "nap")] == "disabled"
    comps = [{"kind": "skill", "uses": 0, "verdict": "disabled"}]
    assert source_verdict(comps) == "disabled"


def test_llm_token_count():
    from plugin_auditor.risk import parse_report

    assert parse_report(SAMPLE_REPORT).llm_tokens == 0  # --no-llm: inference_usage is empty
    with_llm = {**SAMPLE_REPORT, "metadata": {"inference_usage": [
        {"input_tokens": 1200, "output_tokens": 300}, {"total_tokens": 500}]}}
    assert parse_report(with_llm).llm_tokens == 2000


def test_demo_world_tells_the_intended_story(tmp_path):
    from argparse import Namespace
    from plugin_auditor.cli import build
    from plugin_auditor.demo import DemoWorld

    world = DemoWorld(tmp_path).build()
    args = Namespace(home=str(world.home), since=None, no_risk=True, source=None)
    data = build(args)
    verdicts = {i["label"]: i["verdict"] for i in data["items"]}
    assert verdicts["lint-guard@devtools-market"] == "active (hooks only)"
    assert verdicts["focus-mode@ui-mods"] == "unknown (UI mod)"
    assert verdicts["prompt-library@community-hub"] == "UNUSED"
    assert verdicts["sql-helper@datatools"] == "disabled"
    assert verdicts["commit-coach@devtools-market"].startswith("invoked")
    risks = {c["invoked_as"]: c["risk"]["score"] for i in data["items"] for c in i["components"] if c.get("risk")}
    assert risks["prompt-library:brainstorm"] == 88 and risks["deploy-pilot:deploy"] == 63
    assert "command: old-formatter:format" in data["unattributed"]
    before = sum(i["invocations"] for i in data["items"])
    for _ in range(6):
        world.tick()
    assert sum(i["invocations"] for i in build(args)["items"]) > before
    assert str(tmp_path) not in world.rewrite(str(world.home))


def test_install_keeps_foreign_hooks_and_rejects_bad_json(tmp_path):
    import pytest
    from plugin_auditor import hookconfig

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(SystemExit):
        hookconfig.install(bad)
    assert bad.read_text() == "{not json"  # untouched

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "mine.sh"}]}]}}))
    hookconfig.install(settings)
    assert not hookconfig.is_stale(settings)
    assert len(json.loads(settings.read_text())["hooks"]["PreToolUse"]) == 2
    hookconfig.uninstall(settings)
    assert json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "mine.sh"
    assert not list(tmp_path.glob("*.cpa-tmp"))


def test_stale_hook_detected(tmp_path, monkeypatch):
    from plugin_auditor import hookconfig

    monkeypatch.setattr(hookconfig, "hook_command", lambda: '"/gone/python" -I -S "/gone/cpa_usage_hook.py"')
    settings = tmp_path / "settings.json"
    hookconfig.install(settings)
    assert hookconfig.is_stale(settings)


def test_fingerprint_ignores_node_modules_and_hidden(tmp_path):
    from plugin_auditor.risk import fingerprint

    write(tmp_path / "SKILL.md", "x")
    before = fingerprint(tmp_path)
    write(tmp_path / "node_modules/a/b.js", "x")
    write(tmp_path / ".git/c", "x")
    assert fingerprint(tmp_path) == before
    write(tmp_path / "run.sh", "x")
    assert fingerprint(tmp_path) != before


def test_dashboard_rejects_foreign_host(tmp_path):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from types import SimpleNamespace

    from plugin_auditor.serve import State, _handler

    state = State(SimpleNamespace(home=str(tmp_path), source=None))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/data"
    try:
        assert urllib.request.urlopen(url).status == 200
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(url, headers={"Host": "evil.example"}))
        assert e.value.code == 403
    finally:
        srv.shutdown()


@pytest.mark.parametrize("view", ["report", "skills", "mcp", "scan"])
def test_main_runs_every_view(tmp_path, capsys, view):
    from plugin_auditor.cli import main

    home, _ = make_home(tmp_path)
    assert main([view, "--home", str(home), "--no-risk"]) == 0
    assert capsys.readouterr().out


def test_main_json_is_valid_and_source_filter_trims_it(tmp_path, capsys):
    from plugin_auditor.cli import main

    home, _ = make_home(tmp_path)
    assert main(["--home", str(home), "--no-risk", "--json", "--source", "used"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["items"] and all("used" in i["label"] for i in data["items"])
    assert all("used" in e["source"].lower() for e in data["recent"])


def test_report_survives_broken_settings_json(tmp_path, capsys):
    from plugin_auditor.cli import main

    home, _ = make_home(tmp_path)
    (home / "settings.json").write_text("{not json")
    assert main(["--home", str(home), "--no-risk"]) == 0
    assert "not valid JSON" in capsys.readouterr().out


def test_bad_options_exit_with_a_message(tmp_path):
    from plugin_auditor.cli import main

    for argv in (["--since", "0"], ["--since", "99999999999"], ["serve", "--port", "99999"],
                 ["--home", str(tmp_path / "missing")]):
        with pytest.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == 2


def test_claude_config_dir_is_honored(tmp_path, monkeypatch):
    from plugin_auditor.inventory import default_home

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    assert default_home() == tmp_path / "custom"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert default_home() == Path.home() / ".claude"
