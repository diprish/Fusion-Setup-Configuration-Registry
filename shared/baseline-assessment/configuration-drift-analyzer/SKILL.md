---
skill_id: "shared.baseline-assessment.configuration-drift-analyzer"
name: "Configuration Drift Analyzer"
description: "Compares two Oracle Fusion setup CSV packages offline (baseline vs target), classifies differences as drift or expected, and produces a findings table and decision summary without a Fusion login."
layer: "shared"
pillar: "Common"
type: "assessment"
execution_mode: "assisted"
owner_team: "Shared enablement team with pillar review"
version: "0.2.0"
inputs: 
  - "implementation_objective"
  - "project_context"
  - "pillar_scope"
  - "offering_scope"
  - "functional_area_scope"
  - "baseline_setup_package"
  - "target_setup_package"
  - "declared_keys"
  - "expected_difference_patterns"
outputs: 
  - "recommendations"
  - "work_products"
  - "review_points"
  - "configuration_drift_result"
  - "configuration_drift_report"
  - "token_usage_record"
uses_core: []
persists_project_state: true
state_updates:
  - "reports"
  - "validation_results"
workspace_outputs:
  - artifact_type: "validation_result"
    default_location: "project/{project_id}/artifacts/shared/baseline-assessment/configuration-drift-analyzer/current/configuration-drift.json"
    versioning_policy: "major-new-version-minor-confirm-update"
  - artifact_type: "report"
    default_location: "project/{project_id}/artifacts/shared/baseline-assessment/configuration-drift-analyzer/current/configuration-drift-report.md"
    versioning_policy: "major-new-version-minor-confirm-update"
token_usage_tracking:
  enabled: true
  granularity: "per_skill_run"
  artifact_type: "token_usage_record"
  default_location: "project/{project_id}/artifacts/shared/token-usage/current/token-usage-log.md"
  estimation_method: "chars/4 estimate unless exact tokenizer or API accounting is available"
  required_fields:
    - "activity_start_time"
    - "skill_id"
    - "task"
    - "trigger"
    - "artifacts_written"
    - "files_read"
    - "estimated_input_tokens"
    - "estimated_output_tokens"
    - "estimated_total_tokens"
    - "estimation_method"
    - "notes"
applicability:
  industries: 
    - "All"
  countries: 
    - "All"
  fusion_releases: 
    - "Current Oracle Fusion Cloud release"
side_effects:
  - "Writes a drift result JSON and Markdown report under the project workspace. Reads local setup packages only; makes no network or Fusion calls and extracts nothing to disk."
required_privileges: []
---

# Configuration Drift Analyzer

## Purpose

Finds and classifies differences between two exported Oracle Fusion setup
packages (a baseline and a target) entirely offline. It answers: what
configuration is missing, extra, or different in the target, and which of those
differences are expected for that environment versus drift to resolve.

Package A is the baseline (the intended state); package B is the target being
checked. Direction matters: rows missing from B are reported as higher severity
than rows extra in B.

## When to Use

Use it after setup exports exist and you need a reviewable, classified difference
list without logging in to Fusion, for example to check a test pod against a
golden baseline, review two exports before import, or produce pipeline evidence.

When you want Oracle's own compare process run inside Fusion, use
`shared.baseline-assessment.setup-compare.*` instead; those upload both packages
to Fusion and need a live environment and login. This skill complements them with
offline classification of expected versus drift.

## Inputs

- `baseline_setup_package` and `target_setup_package`: a ZIP or a directory of setup CSV files (one CSV per business object, as produced by `shared.setup-export.*`). Use approved or masked data; never place real customer data in the repository.
- `declared_keys`: optional row key per CSV, for example `EGO_CHANGE_STATUS.csv:INTERNAL_NAME`. Without one the script tries to detect a unique key and says so.
- `expected_difference_patterns`: optional regexes for columns (`--expected-col`) or whole files (`--expected-file`) that are expected to differ between environments, such as environment names, URLs, or contact fields.

## Outputs

- `configuration_drift_result`: JSON with per-file statistics, row-level details (capped, counts exact), and all findings.
- `configuration_drift_report`: Markdown report with the decision, per-file table, findings table, and row-level detail.
- `recommendations`, `work_products`, `review_points`: the agent's reviewed interpretation (workflow step 5).

## Workflow

1. Confirm with the user which package is the baseline and which is the target, the offering or functional area covered, and that both exports come from comparable releases.
2. Ask which columns or files are environment-specific so they can be passed as expected patterns, and whether row keys are known.
3. Run the analyzer:

```bash
python3 shared/baseline-assessment/configuration-drift-analyzer/scripts/analyze_config_drift.py \
  --baseline baseline.zip --target test-pod.zip \
  --baseline-label golden --target-label test-pod \
  --key EGO_CHANGE_STATUS.csv:INTERNAL_NAME \
  --expected-col "URL|ENVIRONMENT" \
  --project-id my-project
```

The script refuses to overwrite existing outputs; rerun with `--replace` only after the user confirms. Use `--output-dir` for a location outside the workspace and `--fail-on-drift` to return exit code 1 when drift is found (for pipelines).

4. Read the JSON and report. Auto-detected keys (`KEYS-001`) are a heuristic: verify them with the user before trusting cell-level conclusions.
5. Interpret: separate drift needing action from expected differences, group by business object and impact, flag any schema or release mismatch first (it can invalidate row comparisons), and list questions for the environment owner as `review_points`.
6. Persist outputs and update project state for `reports` and `validation_results`.
7. Record the token usage record as required.

## Decision Summary

- `drift detected`: any high, medium, or low finding.
- `expected differences only`: only differences matched by the expected patterns.
- `no differences`: nothing to report.

The decision covers the supplied packages only. It does not say a package is importable or correct; use the setup validation and import skills for that. See `references/drift-rules.md` for the rules and defaults.

## Required Finding Table Columns

`ID`, `Severity`, `Area`, `Finding`, `Evidence`, `Recommendation`, `Source`.

## Failure Modes and Recovery

| Failure | Recoverable | Behaviour |
| --- | --- | --- |
| Package missing or not a ZIP/directory | Yes | Exit code 2; fix the path |
| Output already exists | Yes | Exit code 3; rerun with `--replace` after user confirmation |
| Archive too large or too many members | Yes | Exit code 2 or member skipped with a PKG-004 finding; raise limits only for trusted packages |
| Unsafe ZIP member names (absolute, `..`, macOS junk) | Yes | Skipped with a PKG-004 finding; nothing is extracted to disk |
| No unique key detected | Yes | Falls back to whole-row comparison (KEYS-002); pass `--key` |

## Error Redaction

Values in columns whose names suggest sensitive data are never printed or
persisted. The hint list is a name heuristic only. Never include real customer
data, credentials, or secrets in examples, diagnostics, or project artifacts.

## Implementation Notes

Keep this skill pillar-agnostic: it compares CSV packages generically and holds no
object-specific rules. Object-specific meaning of a difference belongs in the
owning pillar skill. Add rules to `references/drift-rules.md` and the script
together with a stable rule ID. Requires Python 3.10+ and the standard library only.
Validated against one real FSM task export (Manage Change Statuses, release 11.13.26.07.0, 19 CSV files): one header row per file, UTF-8, parent-key columns named `PARENT.Column`, several files header-only. Other business objects are not yet verified; confirm before relying on it for them.
