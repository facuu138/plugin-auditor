"""Optional security risk per skill, from NVIDIA SkillSpector.

SkillSpector is not a dependency. If its CLI is on PATH (or CPA_SKILLSPECTOR
points to it), `cpa risk` scans each installed skill folder in static mode
(`--no-llm`) and caches the result. Other views only read the cache.

Note: even in static mode, SkillSpector looks up package names and versions
on OSV.dev to find known vulnerabilities.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Bump when the cached fields change: every skill is then rescanned once.
CACHE_VERSION = "2"

# SkillSpector's scoring, read from its source (skillspector/nodes/report.py, v2.12.0).
POINTS = {"CRITICAL": 50, "HIGH": 25, "MEDIUM": 10, "LOW": 5}
REPEAT_WEIGHTS = (1.0, 0.5, 0.25)  # 1st, 2nd, 3rd finding of the same rule; later ones add nothing
EXECUTABLE_MULTIPLIER = 1.3

SCHEMA = """
CREATE TABLE IF NOT EXISTS risk (
    path TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    result TEXT NOT NULL
)
"""


@dataclass
class Risk:
    score: int | None = None
    severity: str = ""
    recommendation: str = ""
    max_issue_severity: str = ""
    issues: int = 0
    findings: list[dict] = field(default_factory=list)  # every issue, compact
    status: str = ""  # SkillSpector's analysis status, e.g. complete, complete_with_caveats, partial
    error: str = ""
    scanned_at: str = ""
    version: str = ""
    is_complete: bool = True
    has_executable_scripts: bool = False
    llm_tokens: int = 0  # tokens SkillSpector spent on LLM analysis (0 with --no-llm)
    folder: str = ""

    def short(self) -> str:
        if self.error:
            return "error"
        return f"{self.score} {self.recommendation}".strip()

    def explain(self) -> dict:
        """Approximate breakdown of the score by rule, using SkillSpector's own formula."""
        by_rule: dict[str, list[dict]] = {}
        for f in self.findings:
            by_rule.setdefault(f.get("id") or "UNKNOWN", []).append(f)
        rows, total = [], 0.0
        for rule, fs in by_rule.items():
            fs = sorted(fs, key=lambda f: -_SEV.get(str(f.get("severity", "")).upper(), 0))
            points = 0.0
            for n, f in enumerate(fs[:len(REPEAT_WEIGHTS)]):
                conf = max(0.0, min(1.0, f.get("confidence") or 0.0))
                p = POINTS.get(str(f.get("severity", "")).upper(), 5) * REPEAT_WEIGHTS[n] * conf
                if self.has_executable_scripts and f.get("executable"):
                    p *= EXECUTABLE_MULTIPLIER
                points += p
            total += points
            rows.append({"id": rule, "pattern": fs[0].get("pattern", ""), "category": fs[0].get("category", ""),
                         "severity": fs[0].get("severity", ""), "count": len(fs), "points": round(points, 1),
                         "coverage_gap": rule.startswith("AE")})
        rows.sort(key=lambda r: -r["points"])
        return {"rules": rows, "computed": min(100, int(total))}

    def reason(self) -> str:
        """Why SkillSpector gave this recommendation, in one or two sentences."""
        if self.error:
            return f"The scan failed: {self.error}"
        score = self.score or 0
        computed = self.explain()["computed"]
        parts = []
        if score >= 51 and computed < 51:
            parts.append(f"The findings add up to about {computed}, but SkillSpector raised the score to its minimum "
                         f"of 51 because part of an executable file could not be analyzed.")
        elif score >= 51:
            parts.append(f"A score of {score} is in the 51 to 100 band, which SkillSpector marks DO NOT INSTALL.")
        elif score >= 21:
            parts.append(f"A score of {score} is in the 21 to 50 band, which SkillSpector marks CAUTION.")
        else:
            parts.append(f"A score of {score} is in the 0 to 20 band, which SkillSpector marks SAFE.")
        if self.recommendation == "CAUTION" and score <= 20:
            parts.append("The scan was incomplete, so SkillSpector fails closed and raises SAFE to CAUTION.")
        gaps = self.coverage_gaps
        if gaps:
            share = sum(r["points"] for r in self.explain()["rules"] if r["coverage_gap"])
            parts.append(f"{gaps} findings are coverage gaps (files the scanner could not fully inspect), "
                         f"worth about {round(share)} points. They are not threats in themselves.")
        return " ".join(parts)

    def payload(self) -> dict:
        """What the dashboard and JSON output get."""
        return {**self.__dict__, "explain": self.explain(), "reason": self.reason(),
                "coverage_gaps": self.coverage_gaps}

    @property
    def coverage_gaps(self) -> int:
        """AE* issues: parts the scanner could not fully inspect. Not threats in themselves."""
        return sum(1 for f in self.findings if str(f.get("id", "")).startswith("AE"))

    def top(self, n: int = 3) -> list[dict]:
        """Most severe real findings, skipping coverage gaps."""
        real = [f for f in self.findings if not str(f.get("id", "")).startswith("AE")]
        return sorted(real, key=lambda f: (-_SEV.get(str(f.get("severity", "")).upper(), 0),
                                           -(f.get("confidence") or 0)))[:n]


def find_skillspector() -> str | None:
    return os.environ.get("CPA_SKILLSPECTOR") or shutil.which("skillspector")


def db_file(claude_home: Path) -> Path:
    return claude_home / "plugin-auditor" / "risk.db"


def skill_folder(skill_md: str) -> Path:
    """The folder to scan. SkillSpector refuses symlinked input, so resolve it."""
    return Path(skill_md).parent.resolve()


def fingerprint(folder: Path) -> str:
    """Changes when any file in the folder is added, removed, or modified."""
    h = hashlib.sha256(f"cache-v{CACHE_VERSION}\n".encode())
    for dirpath, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "node_modules")  # never descend into them
        for name in sorted(files):
            if name.startswith("."):
                continue
            f = Path(dirpath) / name
            try:
                st = f.stat()
            except OSError:
                continue
            h.update(f"{f.relative_to(folder)}|{st.st_size}|{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


_SEV = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _llm_tokens(report: dict) -> int:
    """Sum the token counts in metadata.inference_usage. Empty when no LLM was used."""
    total = 0
    for entry in (report.get("metadata") or {}).get("inference_usage") or []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("total_tokens"), int):
            total += entry["total_tokens"]
        else:
            total += sum(v for k, v in entry.items() if k.endswith("_tokens") and isinstance(v, int))
    return total


def parse_report(report: dict) -> Risk:
    ra = report.get("risk_assessment") or {}
    issues = report.get("issues") or []
    executable = {c.get("path") for c in report.get("components") or [] if c.get("executable")}
    completeness = report.get("analysis_completeness") or {}
    findings = []
    for i in issues:
        loc = i.get("location") or {}
        findings.append({
            "id": i.get("id", ""),
            "pattern": i.get("pattern") or "",
            "category": i.get("category", ""),
            "severity": i.get("severity", ""),
            "confidence": i.get("confidence"),
            "file": loc.get("file", "") if isinstance(loc, dict) else str(loc),
            "line": loc.get("start_line") if isinstance(loc, dict) else None,
            "executable": (loc.get("file") if isinstance(loc, dict) else None) in executable,
            "finding": str(i.get("finding") or "")[:200],
            "explanation": str(i.get("explanation") or "")[:500],
            "remediation": str(i.get("remediation") or "")[:400],
        })
    return Risk(
        score=ra.get("score"),
        severity=ra.get("severity", ""),
        recommendation=ra.get("recommendation", ""),
        max_issue_severity=ra.get("max_issue_severity", ""),
        issues=len(issues),
        findings=findings,
        status=completeness.get("status", ""),
        version=(report.get("metadata") or {}).get("skillspector_version", ""),
        is_complete=bool(completeness.get("is_complete", True)),
        has_executable_scripts=bool((report.get("metadata") or {}).get("has_executable_scripts", bool(executable))),
        llm_tokens=_llm_tokens(report),
    )


def scan_folder(exe: str, folder: Path, timeout: int = 300) -> Risk:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        try:
            proc = subprocess.run(
                [exe, "scan", str(folder), "--no-llm", "--format", "json", "--output", str(out)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return Risk(error=f"timed out after {timeout}s")
        # Exit 0: scanned. Exit 1: scanned, risk above 50 or a strict gate. Exit 2: error.
        if proc.returncode not in (0, 1) or not out.exists():
            msg = " ".join((proc.stderr or proc.stdout or "").split())[:200]
            return Risk(error=msg or f"exit code {proc.returncode}")
        try:
            return parse_report(json.loads(out.read_text()))
        except ValueError as exc:
            return Risk(error=f"unreadable report: {exc}")


class RiskCache:
    def __init__(self, claude_home: Path) -> None:
        self.path = db_file(claude_home)

    def _connect(self, write: bool = False) -> sqlite3.Connection | None:
        if not write and not self.path.exists():
            return None
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=5)
        con.execute(SCHEMA)
        return con

    def load(self) -> dict[str, tuple[str, Risk]]:
        """{folder: (fingerprint, risk)} for everything scanned before."""
        con = self._connect()
        if con is None:
            return {}
        try:
            rows = con.execute("SELECT path, fingerprint, result FROM risk").fetchall()
        finally:
            con.close()
        out = {}
        for path, fp, result in rows:
            try:
                out[path] = (fp, Risk(**json.loads(result)))
            except (TypeError, ValueError):
                continue  # written by an older version: treated as not scanned
        return out

    def save(self, folder: Path, fp: str, risk: Risk) -> None:
        con = self._connect(write=True)
        try:
            con.execute("INSERT OR REPLACE INTO risk VALUES (?, ?, ?, ?)",
                        (str(folder), fp, risk.scanned_at, json.dumps(risk.__dict__)))
            con.commit()
        finally:
            con.close()


def cached_risk(claude_home: Path, skill_md: str, cache: dict | None = None) -> Risk | None:
    """The cached result for a skill, if it is still current. Never scans."""
    cache = cache if cache is not None else RiskCache(claude_home).load()
    folder = skill_folder(skill_md)
    hit = cache.get(str(folder))
    if not hit:
        return None
    fp, risk = hit
    return risk if fp == fingerprint(folder) else None


def stale_folders(claude_home: Path, skill_paths: list[str]) -> list[Path]:
    """Skill folders never scanned, or changed since their last scan."""
    cache = RiskCache(claude_home).load()
    folders = dict.fromkeys(skill_folder(p) for p in skill_paths if Path(p).exists())
    return [f for f in folders if (cache.get(str(f)) or ("",))[0] != fingerprint(f)]


def scan_all(claude_home: Path, skill_paths: list[str], refresh: bool = False, workers: int = 4,
             progress=print, on_result=None) -> dict[str, Risk]:
    """Scan every skill folder that changed since its last scan. Returns {folder: risk}."""
    exe = find_skillspector()
    if not exe:
        raise FileNotFoundError(
            "SkillSpector is not installed. Install it with:\n"
            "  uv tool install git+https://github.com/NVIDIA/skillspector.git")
    store = RiskCache(claude_home)
    cache = store.load()
    folders = list(dict.fromkeys(skill_folder(p) for p in skill_paths if Path(p).exists()))
    results: dict[str, Risk] = {}
    todo = []
    for folder in folders:
        fp = fingerprint(folder)
        hit = cache.get(str(folder))
        if hit and hit[0] == fp and not refresh:
            results[str(folder)] = hit[1]
        else:
            todo.append((folder, fp))

    if todo:
        progress(f"Scanning {len(todo)} skill folders with SkillSpector (static, --no-llm). "
                 f"{len(folders) - len(todo)} are cached.")

    def work(item):
        folder, fp = item
        risk = scan_folder(exe, folder)
        risk.scanned_at = datetime.now(timezone.utc).isoformat()
        risk.folder = str(folder)
        return folder, fp, risk

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (folder, fp, risk) in enumerate(pool.map(work, todo), 1):
            store.save(folder, fp, risk)
            results[str(folder)] = risk
            if on_result:
                on_result(n, len(todo))
            progress(f"  [{n}/{len(todo)}] {folder.name}: {risk.short() if not risk.error else 'error: ' + risk.error}")
    return results
