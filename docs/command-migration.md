# CLI command migration

Canonical thesis commands drop the leading `v2-` prefix while keeping their arguments and persisted identities. Examples:

| Historical transcript | Canonical command |
|---|---|
| `v2-primary-run` | `primary-run` |
| `v2-primary-final-report` | `primary-final-report` |
| `v2-candidate-restore-ensure` | `candidate-restore-ensure` |
| `v2-primary-wave-b-analyze` | `primary-wave-b-analyze` |
| `v2-apply-best-status` | `apply-best-status` |

Historical optimizer, index/arm orchestration, API/report/soak, direct non-restoring benchmark, and retired temporal-stage command surfaces have been removed. Historic command strings embedded in frozen manifests or research transcripts remain unchanged as provenance, not executable aliases. Use `charmdb --help` and [operator-guide.md](operator-guide.md) for the complete current command set.
