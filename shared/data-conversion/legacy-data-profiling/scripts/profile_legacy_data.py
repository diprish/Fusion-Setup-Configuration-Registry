"""Profile legacy-system CSV/TSV extracts ahead of Oracle Fusion data conversion.

Source-agnostic and offline: reads local delimited files only (no network, no
Fusion access), computes per-file and per-column statistics, evaluates a fixed
rule set, and writes a machine-readable result plus a Markdown report.

Values in columns whose names look sensitive are never printed or persisted;
only counts are reported for them (see references/profiling-rules.md).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_ID = "shared.data-conversion.legacy-data-profiling"
DEFAULT_RELATIVE_DIR = Path("shared") / "data-conversion" / "legacy-data-profiling" / "current"
RESULT_NAME = "legacy-data-profile.json"
REPORT_NAME = "legacy-data-profile-report.md"

NULL_TOKENS = {"", "null", "(null)", "n/a", "na", "none", "nil", "\\n"}
SENSITIVE_HINTS = (
    "ssn", "sin", "tax_id", "taxid", "tin", "national_id", "nino", "passport", "email",
    "phone", "mobile", "birth", "dob", "iban", "account_num", "acct_num", "bank",
    "salary", "address", "driver", "license",
)
SENTINEL_DATE_RE = re.compile(r"(4712[-/]12[-/]31|31[-/ ]?dec[-/ ]?4712|12/31/4712|31/12/4712|9999[-/]12[-/]31|31[-/ ]?dec[-/ ]?9999)", re.I)
INT_RE = re.compile(r"^[+-]?\d+$")
DEC_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)$")
GROUPED_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
BOOL_VALUES = {"y", "n", "yes", "no", "true", "false", "t", "f", "0", "1"}
DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d-%b-%Y", "%d-%b-%y", "%m/%d/%Y", "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
)
DATEISH_RE = re.compile(r"^\d{1,4}[-/]\w{1,3}[-/]\d{1,4}([ T]\d{1,2}:\d{2}(:\d{2})?)?$|^\d{8}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DISTINCT_CAP = 200_000
CASE_VARIANT_MAX_DISTINCT = 50_000
SEVERITY_ORDER = {"blocker": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "core" / "auth" / "fusion-oauth-authentication").exists():
            return candidate
    raise RuntimeError("Could not locate repository root with the required layout")


def is_sensitive(column: str) -> bool:
    name = re.sub(r"[^a-z0-9]+", "_", column.lower())
    return any(hint in name for hint in SENSITIVE_HINTS)


def parse_date(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


class ColumnStats:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sensitive = is_sensitive(name)
        self.rows = 0
        self.nulls = 0
        self.values: Counter[str] = Counter()
        self.distinct_capped = False
        self.min_len: int | None = None
        self.max_len = 0
        self.types: Counter[str] = Counter()
        self.date_formats: Counter[str] = Counter()
        self.slash_day_first_only = 0
        self.slash_month_first_only = 0
        self.slash_ambiguous = 0
        self.sentinel_dates = 0
        self.edge_whitespace = 0
        self.non_ascii = 0
        self.control_chars = 0
        self.leading_zero_numbers = 0
        self.grouped_numbers = 0

    def add(self, raw: str) -> None:
        self.rows += 1
        if raw.strip().lower() in NULL_TOKENS:
            self.nulls += 1
            return
        if raw != raw.strip():
            self.edge_whitespace += 1
        value = raw.strip()
        length = len(value)
        self.min_len = length if self.min_len is None else min(self.min_len, length)
        self.max_len = max(self.max_len, length)
        if not value.isascii():
            self.non_ascii += 1
        if CONTROL_RE.search(raw):
            self.control_chars += 1
        if value in self.values or len(self.values) < DISTINCT_CAP:
            self.values[value] += 1
        else:
            self.distinct_capped = True
        self.types[self.classify(value)] += 1

    def classify(self, value: str) -> str:
        if SENTINEL_DATE_RE.fullmatch(value):
            self.sentinel_dates += 1
            return "date"
        if INT_RE.match(value):
            digits = value.lstrip("+-")
            if len(digits) > 1 and digits.startswith("0"):
                self.leading_zero_numbers += 1
                return "text"
            return "integer"
        if GROUPED_RE.match(value):
            self.grouped_numbers += 1
            return "decimal"
        if DEC_RE.match(value):
            return "decimal"
        if DATEISH_RE.match(value):
            matched = [fmt for fmt in DATE_FORMATS if parse_date(value, fmt)]
            if matched:
                self.date_formats[matched[0]] += 1
                if "/" in value and ("%m/%d/%Y" in matched or "%d/%m/%Y" in matched):
                    month_first = parse_date(value, "%m/%d/%Y")
                    day_first = parse_date(value, "%d/%m/%Y")
                    if month_first and day_first:
                        self.slash_ambiguous += 1
                    elif month_first:
                        self.slash_month_first_only += 1
                    elif day_first:
                        self.slash_day_first_only += 1
                return "date"
        if value.lower() in BOOL_VALUES and len(value) <= 5:
            return "boolean"
        return "text"

    def summary(self) -> dict[str, Any]:
        non_null = self.rows - self.nulls
        dominant = self.types.most_common(1)[0][0] if self.types else "empty"
        data: dict[str, Any] = {
            "column": self.name,
            "sensitive_redacted": self.sensitive,
            "rows": self.rows,
            "non_null": non_null,
            "null_rate": round(self.nulls / self.rows, 4) if self.rows else 0.0,
            "distinct": len(self.values),
            "distinct_capped": self.distinct_capped,
            "inferred_type": dominant,
            "type_mix": dict(self.types),
            "min_length": self.min_len,
            "max_length": self.max_len,
            "edge_whitespace": self.edge_whitespace,
            "non_ascii": self.non_ascii,
            "control_chars": self.control_chars,
            "leading_zero_numbers": self.leading_zero_numbers,
            "sentinel_dates": self.sentinel_dates,
            "date_formats": dict(self.date_formats),
            "slash_dates": {
                "month_first_only": self.slash_month_first_only,
                "day_first_only": self.slash_day_first_only,
                "ambiguous": self.slash_ambiguous,
            },
            "case_variant_groups": self.case_variants(),
        }
        if not self.sensitive:
            data["top_values"] = [[v[:60], c] for v, c in self.values.most_common(5)]
        return data

    def case_variants(self) -> int:
        if len(self.values) > CASE_VARIANT_MAX_DISTINCT or self.distinct_capped:
            return 0
        groups: dict[str, set[str]] = {}
        for value in self.values:
            groups.setdefault(re.sub(r"\s+", " ", value).casefold(), set()).add(value)
        return sum(1 for variants in groups.values() if len(variants) > 1)


def read_file(path: Path, delimiter: str | None, encoding: str | None) -> tuple[list[str], list[list[str]], str, str]:
    raw = path.read_bytes()
    used = encoding
    if used is None:
        try:
            text = raw.decode("utf-8-sig")
            used = "utf-8"
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
            used = "cp1252 (fallback)"
    else:
        text = raw.decode(used, errors="replace")
    sample = text[:20000]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return [], [], used, delimiter
    return rows[0], rows[1:], used, delimiter


def profile_file(path: Path, args: argparse.Namespace, keys: dict[str, list[str]]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[str]]]:
    header, rows, encoding, delimiter = read_file(path, args.delimiter, args.encoding)
    findings: list[dict[str, Any]] = []
    fname = path.name

    def add(severity: str, area: str, finding: str, evidence: str, recommendation: str, rule: str) -> None:
        findings.append({"severity": severity, "area": f"{fname} / {area}", "finding": finding,
                         "evidence": evidence, "recommendation": recommendation, "source": rule})

    if not header:
        add("blocker", "structure", "File is empty or has no header row.", "0 rows read.",
            "Re-extract the file with a header row.", "STRUCT-001")
        return {"file": fname, "rows": 0, "columns": []}, findings, {}

    clean_header = [h.strip() for h in header]
    dup_headers = [h for h, c in Counter(clean_header).items() if c > 1]
    blank_headers = sum(1 for h in clean_header if not h)
    if dup_headers or blank_headers:
        add("high", "structure", "Header row has duplicate or blank column names.",
            f"duplicates={len(dup_headers)}, blank={blank_headers}",
            "Fix header names in the extract; downstream mapping needs unique column names.", "STRUCT-002")
    if encoding.startswith("cp1252"):
        add("medium", "structure", "File is not valid UTF-8; decoded as cp1252.", f"encoding={encoding}",
            "Re-extract as UTF-8 so accented characters survive load.", "STRUCT-003")

    width = len(header)
    ragged = 0
    full_rows: Counter[tuple[str, ...]] = Counter()
    cols = [ColumnStats(h or f"(blank_{i})") for i, h in enumerate(clean_header)]
    for row in rows:
        if len(row) != width:
            ragged += 1
            row = (row + [""] * width)[:width]
        full_rows[tuple(row)] += 1
        for stats, value in zip(cols, row):
            stats.add(value)
    duplicate_rows = sum(c - 1 for c in full_rows.values() if c > 1)

    if ragged:
        add("high", "structure", "Rows with a different column count than the header.",
            f"{ragged} of {len(rows)} rows", "Check embedded delimiters/newlines and quoting in the extract.", "STRUCT-004")
    if duplicate_rows:
        add("medium", "duplicates", "Fully duplicated rows.", f"{duplicate_rows} extra copies",
            "Confirm whether duplicates are legitimate before conversion; dedupe in cleansing rules.", "DUP-001")
    if not rows:
        add("high", "structure", "File has a header but no data rows.", "0 data rows",
            "Confirm the extract filter and date range.", "STRUCT-005")

    column_summaries = []
    for stats in cols:
        s = stats.summary()
        column_summaries.append(s)
        label = f"column {stats.name}"
        if stats.rows and s["null_rate"] == 1.0:
            add("low", label, "Column is entirely empty.", "null_rate=1.0",
                "Likely unused in the legacy system; exclude from mapping unless a default is required.", "COL-001")
        elif s["null_rate"] >= args.null_threshold:
            add("low", label, "High null rate.", f"null_rate={s['null_rate']}",
                "Confirm whether the column is conditionally populated or a candidate to drop.", "COL-002")
        non_null = s["non_null"]
        mix = {t: c for t, c in s["type_mix"].items() if c / max(non_null, 1) >= 0.01}
        if len(mix) > 1 and not (set(mix) <= {"integer", "decimal"}):
            add("medium", label, "Mixed data types in one column.", json.dumps(mix, sort_keys=True),
                "Define a conversion rule or quarantine the minority type before load.", "TYPE-001")
        if s["leading_zero_numbers"]:
            add("medium", label, "Numeric-looking values with leading zeros.", f"{s['leading_zero_numbers']} values",
                "Treat as text; loading as numbers (or opening in a spreadsheet) will strip the zeros.", "TYPE-002")
        if s["edge_whitespace"]:
            add("low", label, "Leading or trailing whitespace.", f"{s['edge_whitespace']} values",
                "Trim during cleansing; whitespace breaks key matching and lookups.", "TEXT-001")
        if s["control_chars"]:
            add("high", label, "Control characters in values.", f"{s['control_chars']} values",
                "Strip before load; they commonly cause loader rejections.", "TEXT-002")
        if s["non_ascii"] and encoding == "utf-8":
            add("info", label, "Non-ASCII characters present.", f"{s['non_ascii']} values",
                "Confirm the target loader and any downstream files preserve UTF-8.", "TEXT-003")
        if s["case_variant_groups"]:
            add("medium", label, "Values differing only by case or spacing.", f"{s['case_variant_groups']} groups",
                "Standardise before conversion or they will load as separate values.", "TEXT-004")
        if s["sentinel_dates"]:
            add("medium", label, "End-of-time sentinel dates (e.g. 4712-12-31).", f"{s['sentinel_dates']} values",
                "Decide per field whether to load as-is, null, or the Fusion end date convention.", "DATE-001")
        sd = s["slash_dates"]
        if sd["ambiguous"] and not sd["month_first_only"] and not sd["day_first_only"]:
            add("medium", label, "Slash dates cannot be classified as day-first or month-first.",
                f"{sd['ambiguous']} ambiguous, none disambiguating", "Confirm the legacy date format with the source owner.", "DATE-002")
        if sd["month_first_only"] and sd["day_first_only"]:
            add("high", label, "Slash dates mix day-first and month-first formats.",
                f"month-first={sd['month_first_only']}, day-first={sd['day_first_only']}",
                "Resolve the format per source/region before conversion; silent swaps corrupt dates.", "DATE-003")
        if len(s["date_formats"]) > 1:
            add("medium", label, "Multiple date formats in one column.", json.dumps(s["date_formats"], sort_keys=True),
                "Normalise to a single ISO format during cleansing.", "DATE-004")
        if stats.sensitive and non_null:
            add("info", label, "Column name suggests sensitive data; values are redacted in outputs.",
                f"{non_null} populated values", "Apply data-protection controls and masking before sharing extracts.", "PII-001")

    key_results = []
    key_values: dict[str, list[str]] = {}
    for key_cols in keys.get(fname, []):
        names = key_cols
        idx = []
        for n in names:
            if n in clean_header:
                idx.append(clean_header.index(n))
        if len(idx) != len(names):
            add("high", "key", "Declared key column not found in header.", "+".join(names),
                "Correct the --key argument.", "KEY-000")
            continue
        seen: Counter[tuple[str, ...]] = Counter()
        null_keys = 0
        for row in rows:
            row = (row + [""] * width)[:width]
            parts = tuple(row[i].strip() for i in idx)
            if any(p.lower() in NULL_TOKENS for p in parts):
                null_keys += 1
                continue
            seen[parts] += 1
        dup_keys = sum(1 for c in seen.values() if c > 1)
        dup_rows = sum(c for c in seen.values() if c > 1)
        key_results.append({"key": "+".join(names), "distinct": len(seen), "null_keys": null_keys,
                            "duplicate_key_values": dup_keys, "rows_in_duplicate_keys": dup_rows})
        if null_keys:
            add("blocker", "key", "Null values in declared key.", f"{null_keys} rows, key={'+'.join(names)}",
                "Resolve before conversion; a key must be populated.", "KEY-001")
        if dup_keys:
            add("blocker", "key", "Duplicate values in declared key.",
                f"{dup_keys} key values across {dup_rows} rows, key={'+'.join(names)}",
                "Decide survivorship or dedupe rules; the key must be unique.", "KEY-002")
        if len(names) == 1:
            key_values["+".join(names)] = list(seen.keys())  # tuples flattened below
    flat_keys = {k: [t[0] for t in v] for k, v in key_values.items()}

    summary = {"file": fname, "path": str(path), "rows": len(rows), "column_count": width,
               "delimiter": delimiter, "encoding": encoding, "ragged_rows": ragged,
               "duplicate_rows": duplicate_rows, "keys": key_results, "columns": column_summaries}
    column_values = {c.name: list(c.values.keys()) for c in cols if not c.distinct_capped}
    return summary, findings, {**flat_keys, **{f"{fname}:{n}": v for n, v in column_values.items()}}


def check_references(refs: list[str], value_index: dict[str, dict[str, list[str]]]) -> list[dict[str, Any]]:
    findings = []
    for ref in refs:
        match = re.fullmatch(r"(.+)\.([^.]+)->(.+)\.([^.]+)", ref)
        if not match:
            findings.append({"severity": "high", "area": "references", "finding": "Unparseable --ref.", "evidence": ref,
                             "recommendation": "Use child.csv.COLUMN->parent.csv.COLUMN", "source": "REF-000"})
            continue
        cf, cc, pf, pc = match.groups()
        child = value_index.get(cf, {}).get(f"{cf}:{cc}")
        parent = value_index.get(pf, {}).get(f"{pf}:{pc}")
        if child is None or parent is None:
            findings.append({"severity": "high", "area": "references", "finding": "Reference column not available (missing or too many distinct values).",
                             "evidence": ref, "recommendation": "Check file and column names.", "source": "REF-000"})
            continue
        parent_set = set(parent)
        orphans = [v for v in child if v not in parent_set]
        if orphans:
            findings.append({"severity": "high", "area": f"references / {ref}", "finding": "Child values with no matching parent.",
                             "evidence": f"{len(orphans)} distinct orphan values of {len(child)}",
                             "recommendation": "Load parents first, add the missing parents, or exclude/repair orphan rows.", "source": "REF-001"})
    return findings


def decision(findings: list[dict[str, Any]]) -> str:
    severities = {f["severity"] for f in findings}
    if "blocker" in severities:
        return "not ready"
    if "high" in severities or "medium" in severities:
        return "conditionally ready"
    return "ready"


def render_report(result: dict[str, Any]) -> str:
    lines = [f"# Legacy Data Profile", "",
             f"- Generated: {result['generated_at']}",
             f"- Skill: `{result['skill_id']}`",
             f"- Decision: **{result['decision']}** (shared profiling scope only)", "",
             "## Files", "", "| File | Rows | Columns | Delimiter | Encoding | Ragged | Dup rows |", "|---|---|---|---|---|---|---|"]
    for f in result["files"]:
        lines.append(f"| {f['file']} | {f['rows']} | {f.get('column_count', 0)} | `{f.get('delimiter', '')}` | {f.get('encoding', '')} | {f.get('ragged_rows', 0)} | {f.get('duplicate_rows', 0)} |")
    counts = Counter(f["severity"] for f in result["findings"])
    lines += ["", "## Findings summary", "", ", ".join(f"{s}: {counts[s]}" for s in SEVERITY_ORDER if counts[s]) or "No findings.", "",
              "## Findings", "", "| ID | Severity | Area | Finding | Evidence | Recommendation | Source |", "|---|---|---|---|---|---|---|"]
    for f in result["findings"]:
        cells = [f["id"], f["severity"], f["area"], f["finding"], f["evidence"], f["recommendation"], f["source"]]
        lines.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    lines += ["", "## Column profiles", ""]
    for f in result["files"]:
        lines += [f"### {f['file']}", "", "| Column | Type | Non-null | Null rate | Distinct | Len (min-max) |", "|---|---|---|---|---|---|"]
        for c in f.get("columns", []):
            tag = " (redacted)" if c["sensitive_redacted"] else ""
            lines.append(f"| {c['column']}{tag} | {c['inferred_type']} | {c['non_null']} | {c['null_rate']} | {c['distinct']}{'+' if c['distinct_capped'] else ''} | {c['min_length']}-{c['max_length']} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def parse_keys(values: list[str]) -> dict[str, list[list[str]]]:
    keys: dict[str, list[list[str]]] = {}
    for item in values:
        fname, _, cols = item.partition(":")
        if not cols:
            raise SystemExit(f"--key must look like file.csv:COL or file.csv:COL1+COL2 (got {item!r})")
        keys.setdefault(fname, []).append([c.strip() for c in cols.split("+")])
    return keys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", nargs="+", required=True, help="CSV/TSV extract files to profile")
    p.add_argument("--key", action="append", default=[], help="Declared unique key, e.g. suppliers.csv:VENDOR_ID or lines.csv:PO+LINE (repeatable)")
    p.add_argument("--ref", action="append", default=[], help="Referential check child.csv.COL->parent.csv.COL (repeatable)")
    p.add_argument("--delimiter", help="Force delimiter (default: auto-detect)")
    p.add_argument("--encoding", help="Force encoding (default: utf-8, falling back to cp1252)")
    p.add_argument("--null-threshold", type=float, default=0.9, help="Null rate that triggers COL-002 (default 0.9)")
    p.add_argument("--project-id", help="Project workspace id (default output location)")
    p.add_argument("--output-dir", help="Write results here instead of the project workspace")
    p.add_argument("--replace", action="store_true", help="Replace existing outputs (otherwise refuse)")
    p.add_argument("--json", action="store_true", help="Print the result JSON to stdout")
    args = p.parse_args(argv)

    keys = parse_keys(args.key)
    files, findings, value_index = [], [], {}
    for name in args.input:
        path = Path(name)
        if not path.is_file():
            print(f"error: input not found: {name}", file=sys.stderr)
            return 2
        summary, file_findings, values = profile_file(path, args, keys)
        files.append(summary)
        findings.extend(file_findings)
        value_index[path.name] = values
    findings.extend(check_references(args.ref, value_index))

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["area"], f["source"]))
    for i, f in enumerate(findings, 1):
        f["id"] = f"LDP-{i:03d}"
    result = {"skill_id": SKILL_ID, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "decision": decision(findings), "files": files, "findings": findings,
              "redaction_note": "Values in sensitive-looking columns are never persisted; counts only."}

    if args.output_dir:
        out = Path(args.output_dir)
    else:
        repo_root = find_repo_root(Path(__file__).resolve())
        sys.path.insert(0, str(repo_root / "shared" / "setup-export" / "fsm-csv-export" / "scripts"))
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
