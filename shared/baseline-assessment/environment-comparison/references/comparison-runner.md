# Environment Comparison Runner Reference

## Task selection

- `--task CODE` (repeatable) and `--tasks-file` (one code per line, `#` comments) select tasks directly. Codes are upper-cased.
- `--offering` and `--functional-area` take a short name or an exact name, case-insensitive, and resolve against `knowledge/setup-task-catalog/effective/offering-functional-area-task-catalog.json`. An ambiguous or unknown value stops with candidates.
- Catalog flags: supported (`True`/`Y`) runs; unsupported (`False`/`N`) is skipped in area mode and refused for an explicit task; `UNKNOWN` or not in the catalog warns and is attempted.
- `--config file.json` may supply the same options (`baseline_label`, `tasks`, `offering`, ...); command-line values win.

## Layout

```
project/{id}/exports/environment-comparison/{label}/{TASK}/*.zip     exports (newest valid ZIP is used)
project/{id}/artifacts/shared/baseline-assessment/environment-comparison/current/
  environment-comparison.json
  environment-comparison-report.md
  tasks/{TASK}/configuration-drift.json, configuration-drift-report.md
  logs/{label}-{TASK}.log                                              redacted exporter output
```

With `--output-dir`, results go there and exports to `<output-dir>/exports` unless `--exports-dir` is given.

## Resume behaviour

Exports are reused when a valid ZIP exists for that task and environment. A rerun with `--replace --execute` therefore retries only failed or missing exports. `--refresh --execute` forces new exports. Reused exports are listed with their age in the report because they may be stale.

## Redaction

Persisted logs and failure reasons replace URL hosts and query strings, bearer tokens, `access_token`/`refresh_token`/`code`/`state`/`code_challenge` values, and JWT-shaped strings. This is pattern based and not a guarantee.
