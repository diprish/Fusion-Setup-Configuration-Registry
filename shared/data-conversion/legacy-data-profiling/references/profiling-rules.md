# Legacy Data Profiling Rules

Rule IDs are stable and appear in the `Source` column of findings. All rules are
source-agnostic: they describe the extract file, not the legacy application.
Pillar- and object-specific checks (for example supplier site uniqueness or
worker assignment rules) belong in pillar skills, not here.

| Rule | Severity | Check | Typical action |
| --- | --- | --- | --- |
| STRUCT-001 | blocker | File empty or no header | Re-extract |
| STRUCT-002 | high | Duplicate or blank header names | Fix extract headers |
| STRUCT-003 | medium | Not valid UTF-8 (decoded as cp1252) | Re-extract as UTF-8 |
| STRUCT-004 | high | Rows whose column count differs from header | Check quoting and embedded delimiters |
| STRUCT-005 | high | Header only, no data rows | Check extract filter |
| DUP-001 | medium | Fully duplicated rows | Confirm, then dedupe |
| COL-001 | low | Column entirely empty | Exclude from mapping |
| COL-002 | low | Null rate at or above threshold (default 0.9) | Confirm conditional use |
| TYPE-001 | medium | Mixed data types (types under 1% ignored; integer/decimal mix allowed) | Convert or quarantine |
| TYPE-002 | medium | Numeric-looking values with leading zeros | Treat as text |
| TEXT-001 | low | Leading or trailing whitespace | Trim |
| TEXT-002 | high | Control characters | Strip |
| TEXT-003 | info | Non-ASCII characters (UTF-8 files) | Verify UTF-8 end to end |
| TEXT-004 | medium | Values differing only by case or spacing | Standardise |
| DATE-001 | medium | End-of-time sentinel dates (4712-12-31, 9999-12-31) | Decide per field |
| DATE-002 | medium | Slash dates all ambiguous (day/month order unknowable) | Confirm format with source owner |
| DATE-003 | high | Slash dates mix day-first and month-first | Resolve per source or region |
| DATE-004 | medium | Multiple date formats in one column | Normalise to ISO |
| PII-001 | info | Column name suggests sensitive data; values redacted | Apply masking and handling controls |
| KEY-000 | high | Declared key column not found | Fix `--key` |
| KEY-001 | blocker | Null values in declared key | Resolve before conversion |
| KEY-002 | blocker | Duplicate values in declared key | Define survivorship or dedupe |
| REF-000 | high | `--ref` unparseable or column unavailable | Fix arguments |
| REF-001 | high | Child values with no parent | Load parents first, repair, or exclude |

## Decision summary

- `not ready`: at least one blocker.
- `conditionally ready`: no blockers, at least one high or medium finding.
- `ready`: only low or info findings.

This is a statement about the profiled extracts only. It does not mean the data
will load into Fusion; target-side validation belongs to
`core.data-loader.task-hook` and the pillar skills.

## Redaction

Columns whose names contain hints such as ssn, sin, tax id, passport, email,
phone, birth, iban, bank, salary, address, or license have their values
withheld: no top values, no samples, only counts. The hint list is a name
heuristic, not a guarantee. Reviewers must still treat every extract as
sensitive and never commit real customer data to this repository.

## Limits

- Distinct values are tracked up to 200,000 per column; beyond that the column
  is flagged as capped and case-variant and reference checks skip it.
- Whole files are read into memory. For very large extracts, profile a
  representative sample and state the sampling basis in the review notes.
- Type inference is statistical, not a schema. Date handling covers the formats
  in the script's `DATE_FORMATS`; add formats there when a source needs them.
