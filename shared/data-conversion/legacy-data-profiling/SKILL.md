---
skill_id: "shared.data-conversion.legacy-data-profiling"
name: "Legacy Data Profiling"
description: "Profiles legacy-system CSV/TSV extracts (structure, nulls, types, dates, keys, references, text quality) and produces a findings table and decision summary ahead of Oracle Fusion data conversion."
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
  - "legacy_extract_files"
  - "declared_keys"
  - "reference_checks"
outputs: 
  - "recommendations"
  - "work_products"
  - "review_points"
  - "legacy_data_profile"
  - "legacy_data_profile_report"
  - "token_usage_record"
uses_core: []
persists_project_state: true
state_updates:
  - "reports"
  - "validation_results"
workspace_outputs:
  - artifact_type: "validation_result"
    default_location: "project/{project_id}/artifacts/shared/data-conversion/legacy-data-profiling/current/legacy-data-profile.json"
    versioning_policy: "major-new-version-minor-confirm-update"
  - artifact_type: "report"
    default_location: "project/{project_id}/artifacts/shared/data-conversion/legacy-data-profiling/current/legacy-data-profile-report.md"
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
  - "Writes profile JSON and Markdown report under the project workspace. Reads local extract files only; makes no network or Fusion calls."
required_privileges: []
---

# Legacy Data Profiling

## Purpose

Profiles legacy-system extracts before conversion so cleansing, mapping, and load
effort can be sized from evidence instead of assumption. The method is
source-agnostic: it works on delimited files from any legacy application. It
reports what the data looks like; it does not decide target values, and it does
not validate against Fusion load rules.

## When to Use

Use this shared skill after a conversion scope is known and legacy extracts exist,
and before `shared.data-conversion.cleansing-rule-advisor` and
`shared.data-conversion.mapping-template-generator`. Findings feed those skills
and the conversion strategy.

Do not use it for Fusion setup data (use `shared.validation.setup-data-validator`)
or for object-specific rules (use the owning pillar skill).

## Inputs

- `implementation_objective`, `project_context`, `pillar_scope`, `offering_scope`, `functional_area_scope`
- `legacy_extract_files`: one or more CSV/TSV files, one per legacy entity. Use synthetic or approved, masked data; do not place real customer data in the repository.
- `declared_keys`: optional unique business key per file, for example `suppliers.csv:VENDOR_ID` or `po_lines.csv:PO_NUMBER+LINE_NUM`.
- `reference_checks`: optional child-to-parent checks, for example `po_lines.csv.VENDOR_ID->suppliers.csv.VENDOR_ID`.

## Outputs

- `legacy_data_profile`: machine-readable JSON with per-file and per-column statistics and all findings.
- `legacy_data_profile_report`: Markdown report with the decision summary, findings table, and column profiles.
- `recommendations`, `work_products`, `review_points`: the agent's reviewed interpretation of the findings (see Workflow step 5).

## Workflow

1. Confirm scope and inputs with the user: which entities, which files, which columns are business keys, and which relationships matter. Ask for the legacy date format and encoding if not obvious.
2. Confirm the extracts are approved for use (masked or synthetic where required). Do not copy extracts into repository folders other than the project workspace.
3. Run the profiler:

```bash
python3 shared/data-conversion/legacy-data-profiling/scripts/profile_legacy_data.py \
  --input suppliers.csv po_lines.csv \
  --key suppliers.csv:VENDOR_ID \
  --key po_lines.csv:PO_NUMBER+LINE_NUM \
  --ref "po_lines.csv.VENDOR_ID->suppliers.csv.VENDOR_ID" \
  --project-id my-project
```

Use `--output-dir` for a location outside the workspace. The script refuses to overwrite existing outputs; rerun with `--replace` only after the user confirms. A new major pass should be saved as a new version per the workspace versioning policy.

4. Read the JSON and the report. Treat the script's findings as evidence, not conclusions.
5. Interpret: group findings by conversion impact (blockers first), separate shared data-quality issues from object-specific ones (hand those to the pillar skill), and state which findings need a business decision versus a cleansing rule. Add `review_points` for anything that needs a source owner.
6. Persist outputs under the project workspace and update project state for `reports` and `validation_results`.
7. Record the token usage record as required by the token usage standard.

## Decision Summary

Report one of `ready`, `conditionally ready`, or `not ready` for the profiled
scope, with blockers (key problems) listed separately from advisory findings.
See `references/profiling-rules.md` for the rules and thresholds. The decision
covers the extracts only, not Fusion loadability.

## Required Finding Table Columns

`ID`, `Severity`, `Area`, `Finding`, `Evidence`, `Recommendation`, `Source`.

## Failure Modes and Recovery

| Failure | Recoverable | Behaviour |
| --- | --- | --- |
| Input file missing | Yes | Exit code 2; fix the path |
| Output already exists | Yes | Exit code 3; rerun with `--replace` after user confirmation |
| Wrong delimiter or encoding detected | Yes | Force with `--delimiter` or `--encoding` |
| Extract too large for memory | Yes | Profile a representative sample and record the sampling basis |

## Error Redaction

Values in columns whose names suggest sensitive data are never printed or
persisted; only counts are. This is a name heuristic. Never include real customer
data, credentials, or secrets in examples, diagnostics, or project artifacts.

## Implementation Notes

Keep this skill source- and pillar-agnostic. Add new generic rules to
`references/profiling-rules.md` and the script together, with a stable rule ID.
Capture country-, offering-, or release-specific conditions in the work product
rather than hardcoding them here. Requires Python 3.10+ and the standard library only.
