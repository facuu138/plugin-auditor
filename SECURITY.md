# Security

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private reporting:
https://github.com/facuu138/plugin-auditor/security/advisories/new

You can expect a first reply within a week.

## What this tool touches

- It reads files under `~/.claude` (transcripts, plugin folders, settings). It makes no network calls itself.
- `cpa serve` listens on `127.0.0.1` only and rejects requests with any other `Host` header.
- `cpa install-hook` edits `~/.claude/settings.json`. It writes a timestamped backup first.
- The optional usage hook stores only the name of a skill, agent, MCP tool or slash command. It never stores prompt text.
- Optional risk scans run NVIDIA SkillSpector in static mode. SkillSpector itself looks up package names on OSV.dev.
