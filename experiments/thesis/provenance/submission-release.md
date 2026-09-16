# Local submission release

The recorded release preserves the bounded study results, source snapshot, full
external evidence and a verified control-database backup.
The release directory is `C:\CHARMDB-RELEASES\d073`, outside the OneDrive source
workspace. The standalone chapter is [results and discussion](../../../results/thesis-final/thesis-results-discussion.md).

## Deliverables

| Component | Contents | Verification |
|---|---|---|
| `submission.zip` | Standalone results/discussion chapter, five scientific SVG figures, nine CSV tables, safety-accounting erratum, guide and manifest | Every member read back by SHA-256; five SVGs parse; retained scientific table values match frozen sources |
| `evidence.zip` | All 5,457 frozen evidence files plus their original tree manifest | Source files matched post-activation evidence trust anchor; all 5,458 archive members read back by size and SHA-256 |
| `control-backup/control.dump` | PostgreSQL custom-format logical backup of the control database | Restored into a unique temporary database; every row fingerprint matched across 79 tables/48,013 rows; temporary database removed |
| `source.zip` | Current tracked and nonignored untracked source, migrations, configuration templates, tests, documentation, and `uv.lock` | Per-member SHA-256 manifest and complete archive read-back; captures pending implementation beyond Git HEAD |
| `release-manifest.json` and receipts | Component hashes, sizes, verification results and scope | Preserve alongside all four components |

Source packaging excludes `.git`, local credentials, hidden tool state, dependency
caches and external/generated artifacts. The external evidence and submission
artifacts are included in their own archives. Environment credentials and PostgreSQL
global roles/ownership/ACLs are not a shareable reproduction dependency and are not
bundled. The control dump is local research state. These receipts establish local archive
verification, not publication or off-host storage.

This is a verified local archive on the same physical disk. It is not an off-host
backup or an independent clean-machine experiment reproduction. A distinct copy
and independent environment remain separate completion criteria.

## Results packet

The packet deliberately replaces the historical report's erroneous safety table
and caption with the safety-accounting correction. The ambiguous infrastructure-attempt column
is omitted from the method-outcomes presentation; all retained scientific cells
are unchanged. Five original performance/drift figures are included. The original
sixth safety figure and frozen source report remain preserved in `evidence.zip`.
Interpret the original safety display together with the documented correction.

## Control backup evidence

The backup is 18,231,225 bytes, SHA-256
`df395af00c2162ec656f0c4cfcee8ce7b533111a6d9e9db58b8a8b1ae34f54c3`.
The source was stable before and after the backup; sorted, length-delimited
row-JSON SHA-256 fingerprints matched the isolated restoration for all 79 tables.
The backup includes application data and migrations but excludes ownership and
ACL commands. The rehearsal used the local control PostgreSQL server and a new
`charmdb_d073_verify_*` database, which was removed afterwards. It neither restored
the active target nor changed champion E.

The [backup verification record](d073-control-backup-verification.json) retains
table names, counts and hashes, not table contents or credentials.

## Reproduction and verification

Each ZIP contains `RELEASE-MANIFEST.json`. Verify an archive with the packaged
script before extracting it into a new directory:

```powershell
.venv\Scripts\python.exe scripts/package_thesis_release.py --verify C:\CHARMDB-RELEASES\d073\source.zip
.venv\Scripts\python.exe scripts/package_thesis_release.py --verify C:\CHARMDB-RELEASES\d073\submission.zip
.venv\Scripts\python.exe scripts/package_thesis_release.py --verify C:\CHARMDB-RELEASES\d073\evidence.zip
```

The evidence verification reads about 60 GB. It does not contact PostgreSQL. A
fresh environment can use `uv sync --extra dev --frozen`; that clean-machine step
was not executed here. After extraction, the source's
`scripts/audit_thesis_closeout.py --root <extracted-evidence> --output <new-output>`
authenticates and regenerates the final historical report and safety-accounting companion.
The standalone chapter and figures can be read directly from the extracted
submission packet without the control database.

Archive packaging and backup verification tools are
`scripts/package_thesis_release.py` and `scripts/backup_thesis_control.py`.
Builders require fresh output paths and refuse to write into their input trees.
The database script connects only to the configured local control server, creates
a unique verification database, compares data, and removes only that database.
It must not be repointed to an unrelated database.
