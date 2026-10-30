"""Compare Oracle Fusion setup between two environments across many setup tasks.

For each selected setup task the runner obtains one FSM CSV export per
environment (reusing an existing export, or running the repository's
fsm_csv_export.py when --execute is given), compares the pair with the offline
configuration drift analyzer, and writes one roll-up (JSON and Markdown) plus a
per-task drift report.

Safety model
- Without --execute the runner never calls Fusion. If every export already
  exists it compares them offline; otherwise it prints a plan and writes nothing.
- --execute is the approval gate: it may create one export process per task per
  environment in Fusion (setup data is only read).
- The runner implements no Fusion authentication or REST calls of its own; it
  delegates to fsm_csv_export.py (core OAuth handles login and token caching).
- Tokens, authorize-URL query strings, and tenant host names are redacted from
  everything the runner persists. Terminal output is not redacted so that a
  sign-in URL stays usable if the browser does not open.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SKILL_ID = "shared.baseline-assessment.environment-comparison"
RELATIVE_DIR = Path("shared") / "baseline-assessment" / "environment-comparison" / "current"
RESULT_NAME = "environment-comparison.json"
REPORT_NAME = "environment-comparison-report.md"
CATALOG_RELATIVE = Path("knowledge") / "setup-task-catalog" / "effective" / "offering-functional-area-task-catalog.json"
EXPORTER_RELATIVE = Path("shared") / "setup-export" / "fsm-csv-export" / "scripts" / "fsm_csv_export.py"
ANALYZER_RELATIVE = Path("shared") / "baseline-assessment" / "configuration-drift-analyzer" / "scripts" / "analyze_config_drift.py"
PATHS_HELPER_DIR = Path("shared") / "setup-export" / "fsm-csv-export" / "scripts"

TASK_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.:-]*$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,29}$")
REQUIRED_PROPS = ("environment", "restBaseUrl", "clientId", "scope")
PLACEHOLDER_RE = re.compile(r"^<.*>$|YOUR_|example\.invalid", re.I)
DEFAULT_MAX_TASKS = 25
EXIT_OK, EXIT_DRIFT, EXIT_INPUT, EXIT_EXISTS, EXIT_INCOMPLETE = 0, 1, 2, 3, 4
STOP_SIDE_CLASSES = {"auth_expired_or_invalid", "transport_or_environment"}


class RunnerError(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "core" / "auth" / "fusion-oauth-authentication").exists():
            return candidate
    raise RuntimeError("Could not locate repository root with the required layout")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(text: str) -> str:
    """Pattern-based redaction for anything persisted. Not a guarantee; treat outputs as sensitive."""
    def url(match: re.Match[str]) -> str:
        parts = urlsplit(match.group(0))
        return f"https://<host>{parts.path}" if parts.scheme else "<url>"

    text = re.sub(r"https?://[^\s\"'<>]+", url, text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)\b(access_token|refresh_token|id_token|client_secret|code_verifier|code_challenge|code|state)=([^&\s]+)",
                  r"\1=<redacted>", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<redacted-jwt>", text)
    return text


def read_properties(path: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key.strip()] = value.strip()
    return props


def check_properties(side: str, path: Path) -> tuple[dict[str, str], list[str]]:
    if not path.is_file():
        return {}, [f"{side}: properties file not found: {path}"]
    props = read_properties(path)
    problems = []
    for key in REQUIRED_PROPS:
        value = props.get(key, "")
        if not value or PLACEHOLDER_RE.search(value):
            problems.append(f"{side}: property '{key}' is missing, empty, or still a placeholder in {path.name}")
    url = props.get("restBaseUrl", "")
    if url and not re.match(r"^https://[^/\s]+/?$", url):
        problems.append(f"{side}: restBaseUrl must be an HTTPS base URL with no path ({path.name})")
    return props, problems


def norm_flag(value: Any) -> str:
    if value is True or str(value).strip().upper() in {"Y", "YES", "TRUE"}:
        return "yes"
    if value is False or str(value).strip().upper() in {"N", "NO", "FALSE"}:
        return "no"
    return "unknown"


# --------------------------------------------------------------------------- catalog and task selection

def load_catalog(root: Path) -> dict[str, Any] | None:
    path = root / CATALOG_RELATIVE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def catalog_task_rows(catalog: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for off_code, off in catalog.get("offerings", {}).items():
        for fa_code, fa in off.get("functionalAreas", {}).items():
            for task in fa.get("tasks", []) or []:
                rows.append({"code": str(task.get("taskShortName", "")).upper(), "name": task.get("taskName", ""),
                             "offering": off_code, "offering_name": off.get("offeringName", ""),
                             "functional_area": fa_code, "functional_area_name": fa.get("functionalAreaName", ""),
                             "csv": norm_flag(task.get("csvExportImportSupportedFlag"))})
    return rows


def pick(rows: list[dict[str, str]], value: str, short_key: str, name_key: str, what: str) -> str:
    wanted = value.strip().lower()
    hits = sorted({r[short_key] for r in rows if r[short_key].lower() == wanted})
    if not hits:
        hits = sorted({r[short_key] for r in rows if r[name_key].strip().lower() == wanted})
    if len(hits) == 1:
        return hits[0]
    if not hits:
        close = sorted({r[short_key] for r in rows if wanted in r[short_key].lower() or wanted in r[name_key].lower()})[:8]
        raise RunnerError(f"{what} '{value}' not found in the catalog." + (f" Close matches: {', '.join(close)}" if close else ""))
    raise RunnerError(f"{what} '{value}' is ambiguous: {', '.join(hits[:8])}")


def select_tasks(args: argparse.Namespace, root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return (selected, skipped_unsupported, warnings)."""
    catalog = load_catalog(root)
    rows = catalog_task_rows(catalog) if catalog else []
    warnings: list[str] = []
    if not rows:
        warnings.append("Task catalog not found or unreadable; task names and CSV support flags are not checked.")
    explicit = [t.strip().upper() for t in args.task] if args.task else []
    if args.tasks_file:
        for line in Path(args.tasks_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                explicit.append(line.upper())
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()

    if args.offering or args.functional_area:
        if not rows:
            raise RunnerError("--offering/--functional-area need the task catalog, which was not found.")
        pool = rows
        if args.offering:
            off = pick(pool, args.offering, "offering", "offering_name", "Offering")
            pool = [r for r in pool if r["offering"] == off]
        if args.functional_area:
            fa = pick(pool, args.functional_area, "functional_area", "functional_area_name", "Functional area")
            pool = [r for r in pool if r["functional_area"] == fa]
        for row in pool:
            if row["code"] in seen:
                continue
            seen.add(row["code"])
            entry = {"code": row["code"], "name": row["name"], "offering": row["offering"],
                     "functional_area": row["functional_area"], "catalog": row["csv"]}
            (selected if row["csv"] == "yes" else skipped).append(entry)
        if skipped:
            warnings.append(f"{len(skipped)} task(s) in the selection are not flagged CSV-exportable and were skipped.")

    by_code: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_code.setdefault(row["code"], []).append(row)
    for code in explicit:
        if code in seen:
            continue
        if not TASK_RE.match(code):
            raise RunnerError(f"Invalid task code: {code!r}")
        seen.add(code)
        hits = by_code.get(code)
        if not hits:
            warnings.append(f"{code}: not in the task catalog (catalog may predate your release); export will be attempted.")
            selected.append({"code": code, "name": "", "offering": "", "functional_area": "", "catalog": "not in catalog"})
            continue
        flags = {h["csv"] for h in hits}
        status = "yes" if "yes" in flags else ("unknown" if "unknown" in flags else "no")
        if status == "no":
            raise RunnerError(f"{code}: the catalog flags this task as not CSV-exportable. Remove it or use a different task.")
        if status == "unknown":
            warnings.append(f"{code}: catalog CSV support flag is UNKNOWN; export will be attempted.")
        first = hits[0]
        selected.append({"code": code, "name": first["name"], "offering": first["offering"],
                         "functional_area": first["functional_area"], "catalog": status})
    return selected, skipped, warnings


# --------------------------------------------------------------------------- exports

def find_zip(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    import zipfile
    zips = sorted((p for p in directory.glob("*.zip") if p.is_file() and zipfile.is_zipfile(p)), key=lambda p: p.stat().st_mtime)
    return zips[-1] if zips else None


def classify(lines: list[str], timed_out: bool) -> str:
    if timed_out:
        return "process_timeout"
    errors = [l for l in lines if l.strip().lower().startswith("error")] or lines[-5:]
    text = "\n".join(errors).lower()
    if "http 403" in text or "forbidden" in text:
        return "authorization_or_privilege"
    if "http 401" in text or "invalid_token" in text or "rejected by fusion" in text:
        return "auth_expired_or_invalid"
    if re.search(r"urlerror|timed out|name or service|connection (refused|reset|closed)|temporary failure|ssl", text):
        return "transport_or_environment"
    if re.search(r"http [45]\d\d", text):
        return "fusion_api_error"
    if re.search(r"not supported|unknown task|no match", text):
        return "catalog_or_metadata_mismatch"
    return "process_failed"


def terminate(proc: subprocess.Popen[str]) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def run_exporter(cmd: list[str], label: str, timeout: int) -> tuple[int | None, list[str], bool]:
    lines: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            start_new_session=hasattr(os, "setsid"))

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            print(f"    [{label}] {line}", flush=True)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        rc: int | None = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate(proc)
        rc, timed_out = None, True
    reader.join(timeout=5)
    return rc, lines, timed_out


def age_text(path: Path) -> str:
    seconds = max(0, time.time() - path.stat().st_mtime)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} old"
    return "just now"


# --------------------------------------------------------------------------- comparison and reporting

def compare_pair(analyzer: Path, zip_a: Path, zip_b: Path, la: str, lb: str, out_dir: Path, passthrough: list[str]) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(analyzer), "--baseline", str(zip_a), "--target", str(zip_b), "--baseline-label", la,
           "--target-label", lb, "--output-dir", str(out_dir), "--replace", *passthrough]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    result_file = out_dir / "configuration-drift.json"
    if done.returncode not in (0, 1) or not result_file.is_file():
        tail = redact((done.stderr or done.stdout or "").strip().splitlines()[-1] if (done.stderr or done.stdout) else "no output")
        return {"ok": False, "failure_class": "input_validation" if done.returncode == 2 else "artifact_persistence",
                "reason": f"analyzer exit {done.returncode}: {tail[:200]}"}
    data = json.loads(result_file.read_text(encoding="utf-8"))
    sev = Counter(f["severity"] for f in data["findings"])
    differing = []
    for f in data["files"]:
        if "only_in" in f:
            differing.append({"file": f["file"], "note": f"only in {f['only_in']}", "added": 0, "removed": 0, "changed": 0, "details": []})
        elif f["added"] or f["removed"] or f["changed"] or f["changed_expected"] or f["columns_added"] or f["columns_removed"]:
            differing.append({"file": f["file"], "key": f.get("key"), "added": f["added"], "removed": f["removed"],
                              "changed": f["changed"], "changed_expected": f["changed_expected"],
                              "columns_added": f["columns_added"], "columns_removed": f["columns_removed"],
                              "details": f["details"][:5]})
    return {"ok": True, "decision": data["decision"], "severity": dict(sev), "differing_files": differing,
            "baseline_metadata": data["baseline"].get("metadata", {}), "target_metadata": data["target"].get("metadata", {})}


def overall_decision(tasks: list[dict[str, Any]]) -> str:
    decisions = {t["decision"] for t in tasks if t["status"] == "compared"}
    if not decisions:
        return "not compared"
    for name in ("drift detected", "expected differences only"):
        if name in decisions:
            return name
    return "no differences"


def render_report(roll: dict[str, Any]) -> str:
    a, b = roll["baseline"]["label"], roll["target"]["label"]
    L = ["# Environment Comparison", "",
         f"- Generated: {roll['generated_at']}",
         f"- Skill: `{roll['skill_id']}`",
         f"- Baseline (A): **{a}**; Target (B): **{b}**",
         f"- Decision: **{roll['decision']}**" + ("" if roll["complete"] else " (INCOMPLETE: some tasks were not compared)"),
         f"- Tasks: {roll['counts']['total']} selected, {roll['counts']['compared']} compared, {roll['counts']['not_compared']} not compared", ""]
    ra, rb = roll["releases"]["baseline"], roll["releases"]["target"]
    if ra or rb:
        L += [f"- Release observed: {a} = {', '.join(ra) or 'unknown'}; {b} = {', '.join(rb) or 'unknown'}"
              + ("  **(releases differ)**" if ra and rb and set(ra) != set(rb) else ""), ""]
    L += ["## Tasks", "", f"| Task | Name | {a} export | {b} export | Decision | Findings | Report |", "|---|---|---|---|---|---|---|"]
    for t in roll["tasks"]:
        sev = t.get("severity", {})
        finding = ", ".join(f"{k}: {sev[k]}" for k in ("high", "medium", "low") if sev.get(k)) or ("-" if t["status"] == "compared" else "")
        decision = t["decision"] if t["status"] == "compared" else f"not compared ({t['reason']})"
        report = f"[report]({t['report']})" if t.get("report") else ""
        L.append(f"| `{t['code']}` | {t['name']} | {t['exports']['baseline']['summary']} | {t['exports']['target']['summary']} | {decision} | {finding} | {report} |")
    drift_tasks = [t for t in roll["tasks"] if t["status"] == "compared" and t.get("differing_files")]
    if drift_tasks:
        L += ["", "## What differs", ""]
        for t in drift_tasks:
            L += [f"### `{t['code']}`" + (f" - {t['name']}" if t["name"] else ""), ""]
            for f in t["differing_files"][:10]:
                if f.get("note"):
                    L.append(f"- `{f['file']}`: {f['note']}")
                    continue
                bits = [x for x in (f"+{f['added']} added" if f["added"] else "", f"-{f['removed']} removed" if f["removed"] else "",
                                    f"{f['changed']} changed" if f["changed"] else "",
                                    f"{f['changed_expected']} expected-only" if f["changed_expected"] else "",
                                    f"columns differ" if f["columns_added"] or f["columns_removed"] else "") if x]
                L.append(f"- `{f['file']}`: {', '.join(bits)}")
                for d in f["details"]:
                    if d["type"] == "changed":
                        c = d["changes"][0]
                        L.append(f"    - changed `{d['key']}`: {c['column']}: {c['baseline']} -> {c['target']}")
                    else:
                        L.append(f"    - {d['type']} `{d['key']}`")
            if len(t["differing_files"]) > 10:
                L.append(f"- ... {len(t['differing_files']) - 10} more files in the task report")
            L.append("")
    bad = [t for t in roll["tasks"] if t["status"] != "compared"]
    if bad:
        L += ["## Not compared", "", "| Task | Side | Class | Detail |", "|---|---|---|---|"]
        for t in bad:
            for side in ("baseline", "target"):
                ex = t["exports"][side]
                if ex["status"] in ("failed", "timeout", "missing", "skipped"):
                    L.append(f"| `{t['code']}` | {roll[side]['label']} | {ex.get('failure_class', ex['status'])} | {ex.get('reason', '')} |")
            if t.get("failure_class") and t["exports"]["baseline"]["status"] not in ("failed", "timeout", "missing", "skipped") \
                    and t["exports"]["target"]["status"] not in ("failed", "timeout", "missing", "skipped"):
                L.append(f"| `{t['code']}` | comparison | {t['failure_class']} | {t['reason']} |")
        L += ["", "Rerun with `--replace --execute` to retry only the missing exports; completed exports are reused."]
    reused = [(t["code"], s) for t in roll["tasks"] for s in ("baseline", "target") if t["exports"][s]["status"] == "reused"]
    if reused:
        L += ["", "## Reused exports", "",
              "These exports already existed and were not refreshed. Use `--refresh` for current data.", ""]
        L += [f"- `{c}` ({roll[s]['label']}): {t}" for c, s in reused for t in [next(x for x in roll['tasks'] if x['code'] == c)['exports'][s]['summary']]]
    L += ["", "Redaction note: tokens, URL query strings, and tenant host names are removed from persisted logs by pattern; treat all outputs as sensitive.", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- main

def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunnerError(f"Cannot read --config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RunnerError("--config must contain a JSON object")
    return data


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="JSON file supplying any of these options (CLI values win)")
    p.add_argument("--baseline-label"); p.add_argument("--baseline-properties", help="fusionEnv-style properties file for the baseline environment")
    p.add_argument("--target-label"); p.add_argument("--target-properties", help="fusionEnv-style properties file for the target environment")
    p.add_argument("--task", action="append", default=[], help="Setup task code (repeatable)")
    p.add_argument("--tasks-file", help="Text file with one task code per line")
    p.add_argument("--offering", help="Offering short name or exact name (selects tasks from the catalog)")
    p.add_argument("--functional-area", help="Functional area short name or exact name")
    p.add_argument("--max-tasks", type=int, default=None, help=f"Refuse larger selections (default {DEFAULT_MAX_TASKS})")
    p.add_argument("--allow-many", action="store_true", help="Allow more than --max-tasks tasks")
    p.add_argument("--plan-json", action="store_true", help="Print the plan as one 'PLAN_JSON {...}' line and stop; never exports or writes (for tools)")
    p.add_argument("--execute", action="store_true", help="Approval gate: allow creating Fusion export processes for missing exports")
    p.add_argument("--refresh", action="store_true", help="With --execute, export again even if an export exists")
    p.add_argument("--wait-seconds", type=int, default=300, help="Passed to the exporter (polling window per export)")
    p.add_argument("--task-timeout", type=int, default=900, help="Hard limit per export including any browser sign-in (seconds)")
    p.add_argument("--exporter", help="Alternative exporter CLI (testing); default is the repository's fsm_csv_export.py")
    p.add_argument("--allow-same-environment", action="store_true", help="Do not refuse when both properties files name the same environment and host")
    p.add_argument("--key", action="append", default=[], help="Passed to the analyzer: FILE.csv:COL[+COL]")
    p.add_argument("--ignore-col", action="append", default=[]); p.add_argument("--expected-col", action="append", default=[])
    p.add_argument("--expected-file", action="append", default=[])
    p.add_argument("--project-id"); p.add_argument("--output-dir", help="Write results here instead of the project workspace")
    p.add_argument("--exports-dir", help="Where per-environment exports live (default: workspace exports, or <output-dir>/exports)")
    p.add_argument("--replace", action="store_true", help="Replace an existing roll-up")
    p.add_argument("--fail-on-drift", action="store_true", help="Exit 1 when drift is detected and the run is complete")
    p.add_argument("--json", action="store_true", help="Print the roll-up JSON to stdout")
    return p


CONFIG_MAP = {"baseline_label": "baseline_label", "baseline_properties": "baseline_properties", "target_label": "target_label",
              "target_properties": "target_properties", "tasks_file": "tasks_file", "offering": "offering",
              "functional_area": "functional_area", "max_tasks": "max_tasks", "project_id": "project_id"}


def apply_config(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    for key, attr in CONFIG_MAP.items():
        if getattr(args, attr, None) in (None, "") and cfg.get(key) not in (None, ""):
            setattr(args, attr, cfg[key])
    for key, attr in (("tasks", "task"), ("key", "key"), ("ignore_col", "ignore_col"), ("expected_col", "expected_col"), ("expected_file", "expected_file")):
        if not getattr(args, attr) and cfg.get(key):
            value = cfg[key]
            setattr(args, attr, [value] if isinstance(value, str) else list(value))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except RunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT


def run(args: argparse.Namespace) -> int:
    apply_config(args, load_config(args.config))
    root = find_repo_root(Path(__file__).resolve())

    # ---- preflight: inputs, labels, environments, tools (no side effects)
    problems: list[str] = []
    if sys.version_info < (3, 10):
        problems.append("Python 3.10 or newer is required")
    for side in ("baseline", "target"):
        if not getattr(args, f"{side}_label"):
            problems.append(f"--{side}-label is required")
        elif not LABEL_RE.match(getattr(args, f"{side}_label")):
            problems.append(f"--{side}-label must be 1-30 letters, digits, '.', '_' or '-' (got {getattr(args, f'{side}_label')!r})")
        if not getattr(args, f"{side}_properties"):
            problems.append(f"--{side}-properties is required")
    if problems:
        raise RunnerError("; ".join(problems))
    la, lb = args.baseline_label, args.target_label
    if la.lower() == lb.lower():
        raise RunnerError("Baseline and target labels must differ")
    props_a, errs_a = check_properties("baseline", Path(args.baseline_properties))
    props_b, errs_b = check_properties("target", Path(args.target_properties))
    problems += errs_a + errs_b
    if not problems and not args.allow_same_environment:
        same_env = props_a["environment"].lower() == props_b["environment"].lower()
        same_host = props_a["restBaseUrl"].rstrip("/").lower() == props_b["restBaseUrl"].rstrip("/").lower()
        if same_env or same_host:
            problems.append("Both properties files point at the same environment name or host. Comparing a pod with itself, "
                            "and sharing one token cache, is almost certainly a mistake (override with --allow-same-environment).")
    exporter = Path(args.exporter) if args.exporter else root / EXPORTER_RELATIVE
    analyzer = root / ANALYZER_RELATIVE
    if not exporter.is_file():
        problems.append(f"exporter not found: {exporter}")
    if not analyzer.is_file():
        problems.append("configuration drift analyzer not found; this skill depends on shared.baseline-assessment.configuration-drift-analyzer")
    if problems:
        raise RunnerError("; ".join(problems))

    selected, skipped, warnings = select_tasks(args, root)
    if not selected:
        raise RunnerError("No tasks selected. Use --task, --tasks-file, or --offering/--functional-area.")
    cap = args.max_tasks or DEFAULT_MAX_TASKS
    if len(selected) > cap and not args.allow_many:
        raise RunnerError(f"{len(selected)} tasks selected, above the limit of {cap}. Narrow the selection, or pass --allow-many "
                          f"(each task costs one export per environment).")

    # ---- locations
    project_id = args.project_id
    if args.output_dir:
        results_dir = Path(args.output_dir)
        exports_root = Path(args.exports_dir) if args.exports_dir else results_dir / "exports"
    else:
        sys.path.insert(0, str(root / PATHS_HELPER_DIR))
        from fusion_skill_paths import project_artifacts_root, project_exports_root, resolve_project_id  # noqa: E402
        try:
            project_id = resolve_project_id(args.project_id)
        except RuntimeError as exc:
            raise RunnerError(str(exc)) from exc
        results_dir = project_artifacts_root(project_id) / RELATIVE_DIR
        exports_root = Path(args.exports_dir) if args.exports_dir else project_exports_root(project_id) / "environment-comparison"
    sides = {"baseline": (la, props_a, Path(args.baseline_properties)), "target": (lb, props_b, Path(args.target_properties))}

    def export_dir(side: str, code: str) -> Path:
        return exports_root / sides[side][0] / code

    existing = {(t["code"], s): (None if args.refresh and (args.execute or args.plan_json) else find_zip(export_dir(s, t["code"])))
                for t in selected for s in sides}
    missing = [(t["code"], s) for t in selected for s in sides if existing[(t["code"], s)] is None]

    # ---- plan
    print(f"Environment comparison: {la} (baseline) vs {lb} (target)")
    print(f"Environments from properties: {props_a['environment']} vs {props_b['environment']}")
    print(f"{len(selected)} task(s); {len(selected) * 2 - len(missing)} export(s) available, {len(missing)} to create.")
    for t in selected:
        flags = "".join("R" if existing[(t["code"], s)] else "-" for s in sides)
        print(f"  [{flags}] {t['code']}" + (f"  {t['name']}" if t["name"] else "") + (f"  ({t['catalog']})" if t["catalog"] not in ("yes",) else ""))
    print("  legend: R = existing export will be reused, - = no export yet (order: baseline, target)")
    for w in warnings:
        print(f"  note: {w}")
    if args.plan_json:
        plan = {"baseline": {"label": la, "environment": props_a["environment"]}, "target": {"label": lb, "environment": props_b["environment"]},
                "tasks": [{"code": t["code"], "name": t["name"], "catalog": t["catalog"],
                           "baseline": "reuse" if existing[(t["code"], "baseline")] else "create",
                           "target": "reuse" if existing[(t["code"], "target")] else "create"} for t in selected],
                "counts": {"tasks": len(selected), "toCreate": len(missing), "available": len(selected) * 2 - len(missing)},
                "warnings": warnings, "skippedUnsupported": len(skipped), "maxTasks": cap}
        print("PLAN_JSON " + json.dumps(plan))
        return EXIT_OK
    if missing and not args.execute:
        print("\nPlan only: nothing was exported and nothing was written.")
        print("Re-run with --execute to create the missing exports. That creates one export process per missing task per environment "
              "in Fusion (setup data is only read) and may open a browser sign-in for each environment.")
        return EXIT_OK
    if args.refresh and not args.execute:
        print("note: --refresh has no effect without --execute")

    # ---- guard against overwriting before any export is spent
    result_path, report_path = results_dir / RESULT_NAME, results_dir / REPORT_NAME
    if (result_path.exists() or report_path.exists()) and not args.replace:
        print(f"error: a roll-up already exists in {results_dir}; rerun with --replace after confirming. "
              "Existing exports are reused, so a rerun only retries what is missing.", file=sys.stderr)
        return EXIT_EXISTS
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "logs").mkdir(exist_ok=True)

    # ---- exports (sequential, one environment at a time so each signs in once)
    exports: dict[tuple[str, str], dict[str, Any]] = {}
    for t in selected:
        for s in sides:
            zp = existing[(t["code"], s)]
            if zp:
                exports[(t["code"], s)] = {"status": "reused", "file": zp.name, "summary": f"reused ({age_text(zp)})"}
    total_jobs = len(missing)
    done_jobs = 0
    for side, (label, props, props_path) in sides.items():
        stopped: str | None = None
        for t in selected:
            code = t["code"]
            if (code, side) not in missing:
                continue
            done_jobs += 1
            if stopped:
                exports[(code, side)] = {"status": "skipped", "failure_class": stopped, "summary": "skipped",
                                         "reason": f"skipped after an earlier {stopped} on {label}"}
                continue
            target_dir = export_dir(side, code)
            target_dir.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, str(exporter), "--scope", "task", "--task-code", code, "--properties", str(props_path),
                   "--token-cache", "auto", "--wait-seconds", str(args.wait_seconds), "--output-dir", str(target_dir)]
            if project_id:
                cmd += ["--project-id", project_id]
            print(f"[{done_jobs}/{total_jobs}] exporting {code} from {label} ...", flush=True)
            rc, lines, timed_out = run_exporter(cmd, label, args.task_timeout)
            log_rel = f"logs/{label}-{code}.log"
            (results_dir / log_rel).write_text(redact("\n".join(lines)) + "\n", encoding="utf-8")
            zp = find_zip(target_dir) if rc == 0 else None
            if zp:
                exports[(code, side)] = {"status": "exported", "file": zp.name, "summary": "exported now"}
                continue
            klass = classify(lines, timed_out) if (rc != 0 or timed_out) else "process_failed"
            tail = [redact(l)[:300] for l in lines if l.strip().lower().startswith("error")][-2:] or [redact(l)[:300] for l in lines[-2:]]
            reason = ("export timed out; the Fusion process may still be running and is not retried automatically. "
                      if timed_out else ("exporter reported success but produced no ZIP. " if rc == 0 else "")) + " | ".join(tail)
            exports[(code, side)] = {"status": "timeout" if timed_out else "failed", "failure_class": klass, "summary": f"failed ({klass})",
                                     "reason": reason.strip(" |"), "log": log_rel, "return_code": rc}
            print(f"    -> {klass}", flush=True)
            if klass in STOP_SIDE_CLASSES:
                stopped = klass
                print(f"    -> skipping the remaining {label} exports ({klass} will repeat)", flush=True)

    # ---- compare
    passthrough: list[str] = []
    for flag, values in (("--key", args.key), ("--ignore-col", args.ignore_col), ("--expected-col", args.expected_col), ("--expected-file", args.expected_file)):
        for v in values:
            passthrough += [flag, v]
    task_rows: list[dict[str, Any]] = []
    releases = {"baseline": set(), "target": set()}
    for t in selected:
        code = t["code"]
        ea, eb = exports.get((code, "baseline")), exports.get((code, "target"))
        row: dict[str, Any] = {"code": code, "name": t["name"], "offering": t["offering"], "functional_area": t["functional_area"],
                               "exports": {"baseline": ea or {"status": "missing", "summary": "missing"},
                                           "target": eb or {"status": "missing", "summary": "missing"}}}
        usable = all(e and e["status"] in ("reused", "exported") for e in (ea, eb))
        if not usable:
            row.update(status="not_compared", decision="", reason="export missing or failed")
        else:
            za, zb = find_zip(export_dir("baseline", code)), find_zip(export_dir("target", code))
            outcome = compare_pair(analyzer, za, zb, la, lb, results_dir / "tasks" / code, passthrough) if za and zb else {"ok": False, "failure_class": "artifact_persistence", "reason": "export file vanished"}
            if outcome["ok"]:
                row.update(status="compared", decision=outcome["decision"], severity=outcome["severity"],
                           differing_files=outcome["differing_files"], report=f"tasks/{code}/configuration-drift-report.md")
                for side, key in (("baseline", "baseline_metadata"), ("target", "target_metadata")):
                    if outcome[key].get("release"):
                        releases[side].add(outcome[key]["release"])
            else:
                row.update(status="not_compared", decision="", reason=outcome["reason"], failure_class=outcome["failure_class"])
        task_rows.append(row)

    counts = {"total": len(task_rows), "compared": sum(r["status"] == "compared" for r in task_rows)}
    counts["not_compared"] = counts["total"] - counts["compared"]
    roll = {"skill_id": SKILL_ID, "generated_at": now_iso(), "baseline": {"label": la, "environment": props_a["environment"]},
            "target": {"label": lb, "environment": props_b["environment"]}, "decision": overall_decision(task_rows),
            "complete": counts["not_compared"] == 0, "counts": counts,
            "releases": {k: sorted(v) for k, v in releases.items()}, "tasks": task_rows,
            "skipped_unsupported": [{"code": s["code"], "name": s["name"]} for s in skipped], "warnings": warnings,
            "options": {"execute": args.execute, "refresh": args.refresh, "expected_col": args.expected_col,
                        "expected_file": args.expected_file, "ignore_col": args.ignore_col, "key": args.key}}
    result_path.write_text(json.dumps(roll, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report(roll), encoding="utf-8")
    print(f"\ndecision: {roll['decision']}" + ("" if roll["complete"] else "  (INCOMPLETE)"))
    print(f"compared {counts['compared']}/{counts['total']} task(s)")
    print(f"wrote {result_path}\nwrote {report_path}")
    if args.json:
        print(json.dumps(roll, indent=2))
    if not roll["complete"]:
        return EXIT_INCOMPLETE
    return EXIT_DRIFT if args.fail_on_drift and roll["decision"] == "drift detected" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
