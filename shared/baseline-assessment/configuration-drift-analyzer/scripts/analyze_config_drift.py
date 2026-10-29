"""Offline configuration drift analysis for Oracle Fusion setup CSV packages.

Compares a baseline package (A) with a target package (B). A package is a ZIP
or a directory holding one CSV per setup business object, as produced by FSM
CSV export. Everything is local: no Fusion login, no network, no extraction to
disk (ZIP members are read into memory with size limits).

Differences are classified as drift or expected (environment-specific) using
caller-supplied patterns. Values in sensitive-looking columns are never
written to the outputs.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Any

SKILL_ID = "shared.baseline-assessment.configuration-drift-analyzer"
DEFAULT_RELATIVE_DIR = Path("shared") / "baseline-assessment" / "configuration-drift-analyzer" / "current"
RESULT_NAME = "configuration-drift.json"
REPORT_NAME = "configuration-drift-report.md"
METADATA_MEMBER = "ASM_SETUP_CSV_METADATA.XML"
DEFAULT_IGNORE = (r"^(created_?by|creation_?date|last_?updated?_?by|last_?update_?date|last_?update_?login|object_?version_?number)$",)
SENSITIVE_HINTS = ("ssn", "tax_id", "taxid", "passport", "password", "secret", "token", "email", "phone",
                   "birth", "iban", "account_num", "bank", "salary", "address")
KEY_HINTS = ("code", "lang", "seq", "number", "key", "id", "name", "type")  # columns containing these rank ahead of others
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
MAX_KEY_CANDIDATES = 8
MAX_KEY_EXTRA_COLUMNS = 3
MAX_META_CHUNKS = 5000


class PackageError(Exception):
    pass


def find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "core" / "auth" / "fusion-oauth-authentication").exists():
            return candidate
    raise RuntimeError("Could not locate repository root with the required layout")


def is_sensitive(column: str) -> bool:
    name = re.sub(r"[^a-z0-9]+", "_", column.lower())
    return any(hint in name for hint in SENSITIVE_HINTS)


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        return [], []
    return [h.strip() for h in rows[0]], rows[1:]


class Package:
    def __init__(self, label: str, source: Path) -> None:
        self.label = label
        self.source = source
        self.csvs: dict[str, tuple[str, list[str], list[list[str]]]] = {}  # UPPER name -> (name, header, rows)
        self.meta: dict[str, str] | None = None
        self.warnings: list[str] = []
        self.ignored_members = 0


def safe_member_name(name: str) -> str | None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.name:
        return None
    if any(part.upper() == "__MACOSX" for part in path.parts) or path.name.startswith("._") or path.name == ".DS_Store":
        return None
    return str(path)


META_FIELDS = {"release": "ProcessRelease", "object": "ObjectShortName", "object_type": "ObjectType", "env": "ProcessEnv"}


def extract_metadata(text: str) -> dict[str, str]:
    """Pull only stable fields from the export metadata with a regex (no XML parser on untrusted input).

    Process id/name/dates, ESS request id and the submitter are volatile or personal and are never read.
    """
    meta: dict[str, str] = {}
    for key, tag in META_FIELDS.items():
        match = re.search(rf"<{tag}>([^<]{{0,200}})</{tag}>", text)
        if match:
            meta[key] = match.group(1).strip()
    return meta


def absorb(pkg: Package, name: str, raw: bytes) -> None:
    base = PurePosixPath(name).name
    if base.upper() == METADATA_MEMBER:
        pkg.meta = extract_metadata(decode(raw))
    elif name.lower().endswith(".csv"):
        header, rows = parse_csv(decode(raw))
        pkg.csvs[name.upper()] = (name, header, rows)
    else:
        pkg.ignored_members += 1


def load_package(label: str, source: Path, max_member_bytes: int, max_members: int) -> Package:
    pkg = Package(label, source)
    if source.is_dir():
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            name = safe_member_name(path.relative_to(source).as_posix())
            if name is None:
                continue
            if path.stat().st_size > max_member_bytes:
                pkg.warnings.append(f"skipped oversize file {name}")
                continue
            absorb(pkg, name, path.read_bytes())
    elif source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > max_members:
                raise PackageError(f"{source.name}: {len(infos)} members exceeds limit {max_members}")
            for info in infos:
                name = safe_member_name(info.filename)
                if name is None:
                    pkg.warnings.append(f"skipped unsafe or junk member {info.filename!r}")
                    continue
                if info.file_size > max_member_bytes:
                    pkg.warnings.append(f"skipped oversize member {name}")
                    continue
                with zf.open(info) as handle:
                    raw = handle.read(max_member_bytes + 1)
                if len(raw) > max_member_bytes:
                    pkg.warnings.append(f"skipped oversize member {name}")
                    continue
                absorb(pkg, name, raw)
    else:
        raise PackageError(f"{source}: not a directory or readable ZIP file")
    return pkg


def parse_keys(items: list[str]) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    for item in items:
        fname, _, cols = item.partition(":")
        if not cols:
            raise SystemExit(f"--key must look like FILE.csv:COL or FILE.csv:COL1+COL2 (got {item!r})")
        keys[PurePosixPath(fname).name.upper()] = [c.strip().upper() for c in cols.split("+")]
    return keys


def has_key_hint(column: str) -> bool:
    lowered = column.lower()
    return any(hint in lowered for hint in KEY_HINTS)


def column_values(rows: list[list[str]], idx: int) -> list[str]:
    return [(r[idx] if idx < len(r) else "").strip() for r in rows]


def auto_key(cols: list[str], ia: dict[str, int], ib: dict[str, int], ra: list[list[str]], rb: list[list[str]]) -> list[str] | None:
    """Guess a row key. FSM child files carry parent-key columns named PARENT.Column; start from those.

    Among the remaining columns, key-like names rank first, then file order (Oracle lists key columns first).
    """
    sides = [(idx, rows) for idx, rows in ((ia, ra), (ib, rb)) if rows]  # a side with no rows cannot disprove a key
    if not sides:
        return None
    usable = [c for c in cols if all(all(column_values(rows, idx[c])) for idx, rows in sides)]
    parents = [c for c in usable if "." in c]
    others = sorted((c for c in usable if "." not in c), key=lambda c: (not has_key_hint(c), cols.index(c)))[:MAX_KEY_CANDIDATES]

    def unique(combo: tuple[str, ...]) -> bool:
        for idx, rows in sides:
            seen = {tuple((r[idx[c]] if idx[c] < len(r) else "").strip() for c in combo) for r in rows}
            if len(seen) != len(rows):
                return False
        return True

    if parents and unique(tuple(parents)):
        return parents
    for extra in range(1, MAX_KEY_EXTRA_COLUMNS + 1):
        for combo in combinations(others, extra):
            if unique(tuple(parents) + combo):
                return parents + list(combo)
    return None


def build_matchers(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I) for p in patterns]


def matches(matchers: list[re.Pattern[str]], *targets: str) -> bool:
    return any(m.search(t) for m in matchers for t in targets)


def short(value: str, redact: bool) -> str:
    if redact:
        return "<redacted>"
    value = value.replace("\n", " ")
    return value if len(value) <= 40 else value[:37] + "..."


def diff_snippet(a: str, b: str, redact: bool, width: int = 40) -> tuple[str, str]:
    """Shorten two values for display, centred on where they first differ."""
    if redact:
        return "<redacted>", "<redacted>"
    a, b = a.replace("\n", " "), b.replace("\n", " ")
    if len(a) <= width and len(b) <= width:
        return a, b
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    start = max(0, i - 12)

    def cut(v: str) -> str:
        return ("..." if start > 0 else "") + v[start:start + width] + ("..." if start + width < len(v) else "")

    return cut(a), cut(b)


def diff_file(name: str, a: tuple[str, list[str], list[list[str]]], b: tuple[str, list[str], list[list[str]]],
              args: argparse.Namespace, keys: dict[str, list[str]], ignore: list[re.Pattern[str]],
              exp_cols: list[re.Pattern[str]], exp_files: list[re.Pattern[str]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    disp, ha, ra = a
    _, hb, rb = b
    findings: list[dict[str, Any]] = []

    def add(severity: str, finding: str, evidence: str, rec: str, rule: str, area: str = "") -> None:
        findings.append({"severity": severity, "area": f"{disp}{(' / ' + area) if area else ''}", "finding": finding,
                         "evidence": evidence, "recommendation": rec, "source": rule})

    file_expected = matches(exp_files, disp)
    up_a, up_b = [h.upper() for h in ha], [h.upper() for h in hb]
    removed_cols = [h for h in ha if h.upper() not in set(up_b)]
    added_cols = [h for h in hb if h.upper() not in set(up_a)]
    if removed_cols:
        add("high", "Columns present in baseline are missing in target.", ", ".join(removed_cols),
            "Check the Fusion release/patch level of both exports; schemas differ.", "COLS-001")
    if added_cols:
        add("high", "Columns present in target are missing in baseline.", ", ".join(added_cols),
            "Check the Fusion release/patch level of both exports; schemas differ.", "COLS-002")
    common = [h for h in up_a if h in set(up_b)]
    if common != [h for h in up_b if h in set(up_a)]:
        add("low", "Common columns appear in a different order.", "header order differs",
            "Harmless for comparison; note it if the packages will be imported.", "COLS-003")
    ia = {h.upper(): i for i, h in enumerate(ha)}
    original = {h.upper(): h for h in ha}

    def key_label(cols: list[str]) -> str:
        return "+".join(original.get(c, c) for c in cols)

    ib = {h.upper(): i for i, h in enumerate(hb)}
    compare_cols = [c for c in common if not matches(ignore, c)]
    ignored_cols = [c for c in common if matches(ignore, c)]

    key_cols = None
    key_mode = "none"
    keyless = False
    wanted = keys.get(PurePosixPath(disp).name.upper())
    if wanted:
        if all(k in ia and k in ib for k in wanted):
            key_cols, key_mode = wanted, "explicit"
        else:
            add("high", "Declared key column not found in both files.", "+".join(wanted), "Correct the --key argument.", "KEYS-004")
    if key_cols is None:
        key_cols = auto_key(compare_cols, ia, ib, ra, rb)
        if key_cols:
            key_mode = "auto"
            add("info", "Row key was auto-detected.", key_label(key_cols),
                "Verify it is the business key; pass --key to pin it.", "KEYS-001")
        else:
            keyless = True

    details: list[dict[str, Any]] = []
    stats = {"removed": 0, "added": 0, "changed": 0, "changed_expected": 0, "cells_changed": 0, "cells_expected": 0}
    details_expected: list[dict[str, Any]] = []
    sens = {c: is_sensitive(c) for c in compare_cols}

    def cell(rows: list[list[str]], idx: dict[str, int], row: list[str], col: str) -> str:
        i = idx[col]
        return (row[i] if i < len(row) else "").strip()

    def record(entry: dict[str, Any]) -> None:
        if len(details) < args.max_detail:
            details.append(entry)

    if key_cols:
        def index_rows(rows: list[list[str]], idx: dict[str, int], side: str) -> dict[tuple[str, ...], list[str]]:
            out: dict[tuple[str, ...], list[str]] = {}
            dups = nulls = 0
            for r in rows:
                k = tuple(cell(rows, idx, r, c) for c in key_cols)
                if not all(k):
                    nulls += 1
                elif k in out:
                    dups += 1
                else:
                    out[k] = r
            if dups or nulls:
                add("medium", f"{side} has duplicate or empty key values.", f"duplicates={dups}, empty={nulls}, key={key_label(key_cols)}",
                    "First occurrence is compared; resolve duplicates in the export.", "KEYS-003")
            return out

        ma, mb = index_rows(ra, ia, "Baseline"), index_rows(rb, ib, "Target")
        key_redact = any(is_sensitive(c) for c in key_cols)
        disp_key = lambda k: "<redacted>" if key_redact else "+".join(k)  # noqa: E731
        for k in sorted(set(ma) - set(mb)):
            stats["removed"] += 1
            record({"type": "removed", "key": disp_key(k)})
        for k in sorted(set(mb) - set(ma)):
            stats["added"] += 1
            record({"type": "added", "key": disp_key(k)})
        col_changes: dict[str, dict[str, Any]] = {}
        for k in sorted(set(ma) & set(mb)):
            changes = []
            for c in compare_cols:
                if c in key_cols:
                    continue
                va, vb = cell(ra, ia, ma[k], c), cell(rb, ib, mb[k], c)
                if va != vb:
                    expected = file_expected or matches(exp_cols, c, f"{disp}:{c}")
                    sa, sb = diff_snippet(va, vb, sens[c])
                    changes.append({"column": c, "baseline": sa, "target": sb, "expected": expected})
                    info = col_changes.setdefault(c, {"count": 0, "expected": expected, "example": None})
                    info["count"] += 1
                    info["example"] = info["example"] or f"{disp_key(k)}: {sa} -> {sb}"
                    stats["cells_expected" if expected else "cells_changed"] += 1
            if any(not c["expected"] for c in changes):
                stats["changed"] += 1
                record({"type": "changed", "key": disp_key(k), "changes": changes})
            elif changes:
                stats["changed_expected"] += 1
                if len(details_expected) < 10:
                    details_expected.append({"type": "changed", "key": disp_key(k), "changes": changes})
        for c, info in sorted(col_changes.items()):
            sev, rule = ("info", "VAL-002") if info["expected"] else ("medium", "VAL-001")
            add(sev, "Values differ between baseline and target." + (" (expected)" if info["expected"] else ""),
                f"{info['count']} rows; e.g. {info['example']}",
                "Treat as environment-specific." if info["expected"] else "Confirm whether the target should be aligned to the baseline.",
                rule, f"column {c}")
    else:
        ca = Counter(tuple(cell(ra, ia, r, c) for c in compare_cols) for r in ra)
        cb = Counter(tuple(cell(rb, ib, r, c) for c in compare_cols) for r in rb)
        for row, n in sorted((ca - cb).items()):
            stats["removed"] += n
            record({"type": "removed", "key": short("|".join(row), False)})
        for row, n in sorted((cb - ca).items()):
            stats["added"] += n
            record({"type": "added", "key": short("|".join(row), False)})

    if keyless and (ra or rb):
        add("medium" if (stats["removed"] or stats["added"]) and not file_expected else "info",
            "No unique key found; comparing whole rows.", f"{len(ra)} baseline rows, {len(rb)} target rows",
            "Changed rows show as one removed plus one added. Pass --key to enable cell-level diffs.", "KEYS-002")
    sev_removed = "info" if file_expected else "high"
    sev_added = "info" if file_expected else "medium"
    suffix = " (expected)" if file_expected else ""
    if stats["removed"]:
        add(sev_removed, "Rows in baseline are missing in target." + suffix, f"{stats['removed']} rows",
            "Configuration present in the baseline was not found in the target.", "ROW-001")
    if stats["added"]:
        add(sev_added, "Rows in target are not in baseline." + suffix, f"{stats['added']} rows",
            "Extra configuration in the target; confirm it is intended.", "ROW-002")

    summary = {"file": disp, "baseline_rows": len(ra), "target_rows": len(rb), "key": key_label(key_cols) if key_cols else None,
               "key_mode": key_mode, "ignored_columns": ignored_cols, "columns_removed": removed_cols,
               "columns_added": added_cols, **stats, "details": details, "details_expected_only": details_expected,
               "details_truncated": sum(stats[k] for k in ("removed", "added", "changed")) > len(details)}
    return summary, findings


def analyze(pa: Package, pb: Package, args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keys = parse_keys(args.key)
    ignore = build_matchers(list(DEFAULT_IGNORE) + args.ignore_col)
    exp_cols = build_matchers(args.expected_col)
    exp_files = build_matchers(args.expected_file)
    files, findings = [], []

    def pkg_finding(sev: str, area: str, finding: str, evidence: str, rec: str, rule: str) -> None:
        findings.append({"severity": sev, "area": area, "finding": finding, "evidence": evidence, "recommendation": rec, "source": rule})

    for pkg in (pa, pb):
        for w in pkg.warnings:
            pkg_finding("low", f"{pkg.label} package", "Member skipped while reading.", w, "Check the package contents.", "PKG-004")
    if (pa.meta is None) != (pb.meta is None):
        missing = pb.label if pb.meta is None else pa.label
        pkg_finding("medium", "package metadata", "ASM_SETUP_CSV_METADATA.xml is missing in one package.", f"missing in {missing}",
                    "Confirm both packages came from the same kind of export before trusting the comparison.", "PKG-003")
    elif pa.meta is not None and pb.meta is not None:
        for key, label in (("release", "Export release"), ("object", "Exported setup object"), ("object_type", "Export scope type")):
            va, vb = pa.meta.get(key), pb.meta.get(key)
            if va is not None and vb is not None and va != vb:
                pkg_finding("medium", "package metadata", f"{label} differs.", f"{pa.label}={va}, {pb.label}={vb}",
                            "Packages from different releases or scopes may not be comparable; schema differences can follow.", "PKG-003")
        if pa.meta.get("env") and pb.meta.get("env") and pa.meta["env"] != pb.meta["env"]:
            pkg_finding("info", "package metadata", "Source environments differ.", f"{pa.label}={pa.meta['env']}, {pb.label}={pb.meta['env']}",
                        "Expected when comparing two environments; use expected-column patterns for environment-specific values.", "PKG-006")
    only_a = sorted(set(pa.csvs) - set(pb.csvs))
    only_b = sorted(set(pb.csvs) - set(pa.csvs))
    for n in only_a:
        exp = matches(exp_files, pa.csvs[n][0])
        pkg_finding("info" if exp else "high", pa.csvs[n][0], "File exists only in baseline." + (" (expected)" if exp else ""),
                    f"{len(pa.csvs[n][2])} rows", "The business object is not configured or not exported in the target.", "PKG-001")
    for n in only_b:
        exp = matches(exp_files, pb.csvs[n][0])
        pkg_finding("info" if exp else "medium", pb.csvs[n][0], "File exists only in target." + (" (expected)" if exp else ""),
                    f"{len(pb.csvs[n][2])} rows", "Extra business object in the target; confirm it is intended.", "PKG-002")
    for n in sorted(set(pa.csvs) & set(pb.csvs)):
        summary, ff = diff_file(n, pa.csvs[n], pb.csvs[n], args, keys, ignore, exp_cols, exp_files)
        files.append(summary)
        findings.extend(ff)
    files.extend({"file": pa.csvs[n][0], "only_in": pa.label} for n in only_a)
    files.extend({"file": pb.csvs[n][0], "only_in": pb.label} for n in only_b)
    return files, findings


def decision(findings: list[dict[str, Any]]) -> str:
    material = [f for f in findings if f["severity"] in ("high", "medium", "low")]
    if material:
        return "drift detected"
    if any(f["source"] in ("VAL-002", "ROW-001", "ROW-002", "PKG-001", "PKG-002") for f in findings):
        return "expected differences only"
    return "no differences"


def render_report(result: dict[str, Any]) -> str:
    L = ["# Configuration Drift Report", "",
         f"- Generated: {result['generated_at']}",
         f"- Skill: `{result['skill_id']}`",
         f"- Baseline (A): {result['baseline']['label']} (`{result['baseline']['source']}`)",
         f"- Target (B): {result['target']['label']} (`{result['target']['source']}`)",
         f"- Decision: **{result['decision']}**", "",
         "## Files", "", "| File | Baseline rows | Target rows | Key | Removed | Added | Changed rows | Expected-only rows | Cells (drift/expected) |", "|---|---|---|---|---|---|---|---|---|"]
    for f in result["files"]:
        if "only_in" in f:
            L.append(f"| {f['file']} | only in {f['only_in']} | | | | | | | |")
        else:
            key = f"{f['key']} ({f['key_mode']})" if f["key"] else "none"
            L.append(f"| {f['file']} | {f['baseline_rows']} | {f['target_rows']} | {key} | {f['removed']} | {f['added']} | {f['changed']} | {f['changed_expected']} | {f['cells_changed']}/{f['cells_expected']} |")
    counts = Counter(f["severity"] for f in result["findings"])
    L += ["", "## Findings summary", "", ", ".join(f"{s}: {counts[s]}" for s in SEVERITY_ORDER if counts[s]) or "No findings.", "",
          "## Findings", "", "| ID | Severity | Area | Finding | Evidence | Recommendation | Source |", "|---|---|---|---|---|---|---|"]
    for f in result["findings"]:
        cells = [f["id"], f["severity"], f["area"], f["finding"], f["evidence"], f["recommendation"], f["source"]]
        L.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    L += ["", "## Row-level detail (first entries per file)", ""]
    for f in result["files"]:
        if "only_in" in f or not (f["details"] or f["changed_expected"]):
            continue
        L += [f"### {f['file']}", ""]
        for d in f["details"][:25]:
            if d["type"] == "changed":
                parts = "; ".join(f"{c['column']}: {c['baseline']} -> {c['target']}{' (expected)' if c['expected'] else ''}" for c in d["changes"][:4])
                L.append(f"- changed `{d['key']}`: {parts}")
            else:
                L.append(f"- {d['type']} `{d['key']}`")
        if f["details_truncated"] or len(f["details"]) > 25:
            L.append("- ... more in the JSON output")
        if f["changed_expected"]:
            L.append(f"- {f['changed_expected']} rows differ only in expected columns (first few in the JSON output)")
        L.append("")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--baseline", required=True, help="Baseline package: ZIP or directory of setup CSVs (A)")
    p.add_argument("--target", required=True, help="Target package: ZIP or directory of setup CSVs (B)")
    p.add_argument("--baseline-label", default="baseline")
    p.add_argument("--target-label", default="target")
    p.add_argument("--key", action="append", default=[], help="Row key, e.g. EGO_CHANGE_STATUS.csv:INTERNAL_NAME or FILE.csv:COL1+COL2 (repeatable)")
    p.add_argument("--ignore-col", action="append", default=[], help="Regex of columns to ignore (adds to defaults: who columns, OBJECT_VERSION_NUMBER)")
    p.add_argument("--expected-col", action="append", default=[], help="Regex of columns whose differences are expected (matches COL or FILE:COL)")
    p.add_argument("--expected-file", action="append", default=[], help="Regex of files whose differences are all expected")
    p.add_argument("--max-detail", type=int, default=200, help="Max row-level details stored per file (counts stay exact)")
    p.add_argument("--max-member-mb", type=int, default=200, help="Skip files larger than this")
    p.add_argument("--max-members", type=int, default=5000, help="Refuse archives with more members than this")
    p.add_argument("--project-id", help="Project workspace id (default output location)")
    p.add_argument("--output-dir", help="Write results here instead of the project workspace")
    p.add_argument("--replace", action="store_true", help="Replace existing outputs (otherwise refuse)")
    p.add_argument("--fail-on-drift", action="store_true", help="Exit 1 when drift is detected (for pipelines)")
    p.add_argument("--json", action="store_true", help="Print the result JSON to stdout")
    args = p.parse_args(argv)

    try:
        limit = args.max_member_mb * 1024 * 1024
        pa = load_package(args.baseline_label, Path(args.baseline), limit, args.max_members)
        pb = load_package(args.target_label, Path(args.target), limit, args.max_members)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not pa.csvs and not pb.csvs:
        print("error: neither package contains any CSV files", file=sys.stderr)
        return 2

    files, findings = analyze(pa, pb, args)
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["area"], f["source"]))
    for i, f in enumerate(findings, 1):
        f["id"] = f"CFG-{i:03d}"
    result = {"skill_id": SKILL_ID, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "baseline": {"label": pa.label, "source": Path(args.baseline).name},
              "target": {"label": pb.label, "source": Path(args.target).name},
              "decision": decision(findings), "files": files, "findings": findings,
              "redaction_note": "Values in sensitive-looking columns are never persisted."}

    if args.output_dir:
        out = Path(args.output_dir)
    else:
        sys.path.insert(0, str(find_repo_root(Path(__file__).resolve()) / "shared" / "setup-export" / "fsm-csv-export" / "scripts"))
        from fusion_skill_paths import project_artifacts_root  # noqa: E402
        out = project_artifacts_root(args.project_id) / DEFAULT_RELATIVE_DIR
    out.mkdir(parents=True, exist_ok=True)
    targets = [out / RESULT_NAME, out / REPORT_NAME]
    if any(t.exists() for t in targets) and not args.replace:
        print(f"error: outputs already exist in {out}; rerun with --replace after confirming", file=sys.stderr)
        return 3
    targets[0].write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    targets[1].write_text(render_report(result), encoding="utf-8")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        counts = Counter(f["severity"] for f in findings)
        print(f"decision: {result['decision']}; findings: " + (", ".join(f"{s}={counts[s]}" for s in SEVERITY_ORDER if counts[s]) or "none"))
        print(f"wrote {targets[0]}\nwrote {targets[1]}")
    return 1 if args.fail_on_drift and result["decision"] == "drift detected" else 0


if __name__ == "__main__":
    sys.exit(main())
