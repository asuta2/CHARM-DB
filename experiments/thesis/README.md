# Thesis protocol records

[`manifests`](manifests) and [`preregistration`](preregistration) preserve the frozen scientific inputs byte-for-byte. [`frozen-sha256.json`](frozen-sha256.json) records the original logical path, length, and SHA-256 for all 16 inputs. Run `make frozen-input-check` after any move. Application path resolution maps these logical names to this directory without editing the files.

[`provenance`](provenance) holds terminal closeout, release, audit, and DOCX archive receipts. [`archive/docx`](archive/docx) retains successive research exports; these are historical snapshots; use the current documentation and closeout record for the final conclusions. Raw evidence and database state remain in the configured artifact root and control database.
