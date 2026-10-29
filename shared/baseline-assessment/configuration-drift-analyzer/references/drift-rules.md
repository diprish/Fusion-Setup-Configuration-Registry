# Configuration Drift Rules

Rule IDs are stable and appear in the `Source` column of findings. A is the
baseline; B is the target.

| Rule | Severity | Check |
| --- | --- | --- |
| PKG-001 | high (info if expected file) | CSV exists only in baseline |
| PKG-002 | medium (info if expected file) | CSV exists only in target |
| PKG-003 | medium | `ASM_SETUP_CSV_METADATA.xml` differs or is missing in one package |
| PKG-004 | low | Member skipped (oversize, unsafe name, macOS junk) |
| COLS-001 | high | Baseline columns missing in target |
| COLS-002 | high | Target columns missing in baseline |
| COLS-003 | low | Common columns in a different order |
| KEYS-001 | info | Row key auto-detected; verify it |
| KEYS-002 | medium | No unique key; whole-row comparison used |
| KEYS-003 | medium | Duplicate or empty key values (first occurrence compared) |
| KEYS-004 | high | Declared key column not in both files |
| ROW-001 | high (info if expected file) | Rows in baseline missing in target |
| ROW-002 | medium (info if expected file) | Rows in target not in baseline |
| VAL-001 | medium | Cell values differ for a column |
| VAL-002 | info | Cell values differ but the column or file is expected to differ |

## Defaults and behaviour

- Ignored columns by default: `CREATED_BY`, `CREATION_DATE`, `LAST_UPDATED_BY`, `LAST_UPDATE_DATE`, `LAST_UPDATE_LOGIN`, `OBJECT_VERSION_NUMBER`. Add more with `--ignore-col`.
- Files are matched by name, case-insensitive, including any folder path inside the package.
- Values are compared as trimmed strings. `1.0` and `1` are different; normalise in the export if that matters.
- Key auto-detection tries single columns, then pairs of the top eight candidates, preferring names containing code, name, number, key, type, then id. Surrogate `*_ID` columns often differ between environments, so a code or name key is usually safer; pass `--key` when known.
- Without a key, a changed row appears as one removed plus one added row.
- Row-level detail is capped per file (`--max-detail`, default 200); counts are exact.

## Decision

- `drift detected`: any high, medium, or low finding.
- `expected differences only`: no such findings, but expected differences exist.
- `no differences`: nothing beyond informational notes.

## Safety

- Package contents are untrusted input. Nothing is extracted to disk; ZIP members are read into memory with a size cap, and a member-count cap protects against archive bombs.
- Absolute paths, `..` segments, `__MACOSX` entries, and `._` files are skipped.
- The metadata XML is hashed after whitespace normalisation, not parsed.
- Values in columns with sensitive-looking names are replaced by `<redacted>`. This is a heuristic, so treat packages and outputs as sensitive.
