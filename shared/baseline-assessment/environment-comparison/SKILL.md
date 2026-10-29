---
skill_id: "shared.baseline-assessment.environment-comparison"
name: "Environment Comparison"
description: "Compares Oracle Fusion setup between two environments across many setup tasks: exports each side via FSM CSV export, runs the offline drift analyzer per task, and produces one roll-up of differences."
layer: "shared"
pillar: "Common"
type: "assessment"
execution_mode: "hybrid"
owner_team: "Shared enablement team with pillar review"
version: "0.2.0"
inputs: 
  - "implementation_objective"
  - "project_context"
  - "pillar_scope"
  - "offering_scope"
  - "functional_area_scope"
  - "baseline_environment"
  - "target_environment"
  - "setup_task_selection"
  - "expected_difference_patterns"
outputs: 
  - "recommendations"
  - "work_products"
  - "review_points"
  - "environment_comparison_result"
  - "environment_comparison_report"
  - "token_usage_record"
depends_on:
  - "shared.setup-export.fsm-csv-export"
  - "shared.baseline-assessment.configuration-drift-analyzer"
uses_core:
  - "core.auth.fusion-oauth-authentication"
persists_project_state: true
state_updates:
  - "reports"
  - "validation_results"
  - "exports"
workspace_outputs:
  - artifact_type: "validation_result"
    default_location: "project/{project_id}/artifacts/shared/baseline-assessment/environment-comparison/current/environment-comparison.json"
    versioning_policy: "major-new-version-minor-confirm-update"
  - artifact_type: "report"
    default_location: "project/{project_id}/artifacts/shared/baseline-assessment/environment-comparison/current/environment-comparison-report.md"
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
  - "With --execute, creates one FSM CSV export process per missing task in each environment (setup data is read, not changed) and may open a browser sign-in per environment. Writes exports, per-task drift reports, and a roll-up under the project workspace."
required_privileges:
  - "Privileges to create and retrieve FSM CSV export packages for the selected setup tasks, in both environments"
---

# Environment Comparison

## Purpose

Shows teams what differs in setup between two Oracle Fusion environments. Give it
a baseline and a target environment and a set of setup tasks (or a functional
area). It obtains one FSM CSV export per task per environment, compares each pair
offline with the configuration drift analyzer, and writes one roll-up.

The baseline is the intended state; the target is the environment being checked.
Direction matters: rows missing from the target rank higher than extra rows.

## When to Use

- Checking a test or production-like pod against a golden or development pod.
- Confirming a migration or import landed the same setup in the target.
- Producing evidence of environment differences for a go-live review.

For a single pair of packages you already hold, use
`shared.baseline-assessment.configuration-drift-analyzer` directly. For Oracle's
in-Fusion compare, use `shared.baseline-assessment.setup-compare.*`.

## Inputs

- `baseline_environment`, `target_environment`: a short label and a `fusionEnv`-style properties file for each (`environment`, `restBaseUrl`, `clientId`, `scope`). Each environment needs its own OAuth client and enrollment; see `core.auth.fusion-oauth-authentication`.
- `setup_task_selection`: `--task CODE` (repeatable), `--tasks-file`, or `--offering` with optional `--functional-area` resolved from the task catalog. Area selections skip tasks the catalog does not flag as CSV-exportable; an explicit task flagged unsupported is refused.
- `expected_difference_patterns`: columns or files expected to differ between environments (`--expected-col`, `--expected-file`), plus `--key` and `--ignore-col`, passed to the analyzer.

## Outputs

- `environment_comparison_result`: roll-up JSON with decision, completeness, per-task status, severity counts, differing files with capped row detail, observed releases, and failures with a class.
- `environment_comparison_report`: Markdown with a per-task table, what differs, tasks not compared, and reused exports. Per-task drift reports sit under `tasks/<TASK>/`.
- `recommendations`, `work_products`, `review_points`: the agent's reviewed interpretation (workflow step 7).

## Workflow

1. Confirm baseline and target, that both are non-production unless the user says otherwise, the task or functional-area scope, and which differences are expected (URLs, endpoints, contacts). Check that the releases are comparable; the report shows the release each side reported.
2. Confirm each environment has a working properties file and OAuth enrollment. Never ask for or store passwords or tokens.
3. Run a plan (no `--execute`). It lists which exports exist and which would be created, and writes nothing:

```bash
python3 shared/baseline-assessment/environment-comparison/scripts/compare_environments.py \
  --baseline-label dev --baseline-properties path/to/dev.properties \
  --target-label test --target-properties path/to/test.properties \
  --offering PIM_PROD_MNG --functional-area EGO_CHANGE_ORDERS \
  --project-id my-project
```

4. Show the plan and get the user's approval, then add `--execute`. This creates exports in both environments; sign-in happens in the user's browser, once per environment. Stream the output; do not hide the sign-in URL.
5. If the run is incomplete (exit 4), report which tasks failed and why. Rerun with `--replace --execute`: completed exports are reused, so only the missing ones are retried. Use `--refresh` to force new exports.
6. Read the JSON and the per-task reports. Auto-detected keys (`KEYS-001`) are heuristics; verify them before trusting cell-level conclusions.
7. Interpret: separate drift needing action from expected differences, group by business object, lead with any release or schema mismatch, and list questions for the environment owners as `review_points`.
8. Persist outputs, update project state, and record the token usage record.

## Safety and Approval Gates

- Without `--execute` the runner never calls Fusion.
- Defaults: at most 25 tasks (`--allow-many` to raise), one task export at a time, `--task-timeout` 900 seconds per export, `--wait-seconds` 300 for the Fusion process.
- Refuses when both properties files name the same environment or host (one shared token cache would make the comparison meaningless).
- Refuses to replace an existing roll-up without `--replace`, before any export is spent.
- Implements no authentication or REST calls. It runs the repository exporter, which uses core OAuth with a per-environment token cache. The runner never reads, prints, or stores tokens.
- Persisted logs are redacted by pattern (tokens, URL query strings, tenant hosts). Terminal output is not, so the sign-in URL stays usable. Treat all outputs and exports as sensitive; keep `project/` exports out of git.

## Failure Modes and Recovery

| Class | Meaning | Behaviour |
| --- | --- | --- |
| input_validation | Bad labels, properties, task codes, cap exceeded, unsupported task | Exit 2; nothing exported |
| catalog_or_metadata_mismatch | Exporter rejected the task | That task is not compared; others continue |
| auth_expired_or_invalid | HTTP 401 or rejected token | Remaining exports for that environment are skipped; sign in again and rerun |
| authorization_or_privilege | HTTP 403 | That task is not compared; check privileges |
| transport_or_environment | Network or TLS failure | Remaining exports for that environment are skipped; rerun when reachable |
| fusion_api_error | Other HTTP 4xx or 5xx | That task is not compared |
| process_timeout | Export exceeded `--task-timeout` | Exporter is stopped; the Fusion process may still run; rerun |
| process_failed | Exporter failed or produced no ZIP | That task is not compared; see the redacted log |
| artifact_persistence | Output could not be written, or the analyzer failed | That task is not compared |
| approval_blocker | Missing exports and no `--execute` | Plan printed; exit 0 |

Exit codes: 0 done; 1 drift with `--fail-on-drift` (only when complete); 2 input or preflight error; 3 roll-up exists without `--replace`; 4 incomplete, some tasks not compared (takes precedence over 1).

## Decision Summary

`drift detected` > `expected differences only` > `no differences` across compared tasks, or `not compared` if none. The roll-up carries a `complete` flag; an incomplete run is not a clean bill of health. The decision covers the selected tasks only. See `references/comparison-runner.md`.

## Implementation Notes

Keep this skill pillar-agnostic and free of Fusion transport. Object-specific meaning of a difference belongs in the owning pillar skill. Requires Python 3.10+ and the standard library only. The runner was tested offline with a stub exporter and a real before/after pair of one task export; it has not been run against two live pods, and only one business object has been verified.
