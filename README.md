# Garmin Running Sync

A Windows-native Python tool for reviewing Garmin running activities and
synchronizing them with a local JSON history while preserving existing notes.

The project separates account access, FIT decoding, review, and file updates.
Network retrieval produces a proposal; a separate offline command applies
reviewed additions to a **private test copy only**. Original-history updates and
a dedicated notes-entry command are not enabled.

## What it demonstrates

- Paginated API retrieval that finds missed activities, not just the latest run.
- Garmin activity ID normalization and duplicate prevention.
- Strict FIT decoding, CRC verification, unit checks, and explicit missing values.
- Conservative activity classification using Garmin types and FIT sport evidence.
- Versioned 15-second processing with separate legacy and precision fields.
- Append-only updates with verified backups, stale-plan checks, and atomic replacement.
- Windows DPAPI credential storage and explicit NTFS permission verification.
- Synthetic tests for parsing, privacy boundaries, preservation, and failure recovery.

This is a personal integration, not a Garmin product or a general-purpose Garmin
SDK. It requires existing access to the Garmin Connect Personal API. It does not
obtain API keys or establish whether another account is eligible for that API.

## Architecture

```text
Windows DPAPI credential
        |
        v
Garmin HTTPS client -> activity pages -> ZIP/FIT bytes in memory
                                            |
                                            v
                              strict decoder + running classification
                                            |
                                            v
                              processed proposal + conversion audit
                                            |
                                       human review
                                            |
                                            v
                              offline private-copy updater
                              validate -> backup -> atomic replace
```

| File | Responsibility |
| --- | --- |
| `garmin_sync.py` | Credential access, API requests, classification, decoding, and dry-run reports |
| `processing_audit.py` | Compare aggregation hypotheses and report cadence outliers |
| `history_update.py` | Validate and append reviewed records to the fixed private test copy |
| `scripts/` | Windows setup, credential import, and test-gated dry-run helpers |
| `tests/` | Synthetic binary FIT, transport, processing, and update tests |

## Requirements and installation

- Windows with PowerShell and NTFS permissions support.
- Python 3.13, the tested runtime.
- An existing Garmin Personal API key for live requests.
- A compatible schema-version-3.0 history for synchronization.
- WSL is needed only if importing a key from an existing WSL configuration;
  synchronization itself runs directly on Windows.

From your project checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

The only package dependency is pinned in `requirements.txt`. Most tests use
synthetic data and need no account, network, or private history. Two optional
checks inspect a locally supplied history and are skipped when it is absent.
The binary-decoder tests require `fitdecode`; skipping those is not a complete
parser verification.

## Private history and credentials

The fixed history target is `private/running_history_master.test.json`. Supply a
test copy of your compatible history there. The repository distributes no
personal history, activity downloads, or real credentials. This is not an
arbitrary-JSON importer: schema version 3.0, `activity_count`, and
`running_activities` with unique `garmin_activity_id` values are required.

For an existing key at `~/.config/garmin-sync/api_key` in WSL, first run the
synthetic credential tests from your own Windows PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\Test-CredentialImport.ps1
```

After reviewing a successful test run, import the existing key:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Import-GarminKey.ps1
```

The importer captures the key in memory and saves only DPAPI-encrypted bytes at
`%LOCALAPPDATA%\GarminRunningSync\api_key.dpapi`. The file and directory ACLs are
verified to allow only the current Windows account, with inheritance disabled.
Existing credentials are never overwritten. An existing directory with different
permissions is rejected without changing those permissions.

`-ExecutionPolicy Bypass` affects that PowerShell process only. The import script
supports `-Distribution` when the key is not in the default WSL distribution.
Never paste a real key into chat, source code, or terminal command arguments.

## Review a synchronization

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Invoke-ReadOnlyDryRun.ps1
```

The helper checks the decoder, runs Python tests, and contacts Garmin only if
those checks pass. The equivalent retrieval command is:

```powershell
.\.venv\Scripts\python.exe .\garmin_sync.py --dry-run --save-plan
```

The report shows proposed additions, duplicate counts, grouped activity types,
and historical conversion checks. Unknown types are examined for explicit FIT
sport evidence; ambiguous or multisession activities remain out of automatic
imports. Names or keywords alone never establish that an activity is running.
This includes ambiguous `indoor_cardio` activities.

`--save-plan` writes a processed proposal under `private/`. It does not update
history. Review the proposed additions and conversion discrepancies before using
that exact proposal for an update. Console summaries are private training data,
even though they contain no credentials or GPS coordinates.

## Apply a reviewed proposal to the test copy

Replace `dry_run_TIMESTAMP.json` with the exact reviewed proposal filename:

```powershell
.\.venv\Scripts\python.exe .\history_update.py --plan .\private\dry_run_TIMESTAMP.json --dry-run
.\.venv\Scripts\python.exe .\history_update.py --plan .\private\dry_run_TIMESTAMP.json --apply-test-copy
```

The updater has no network or credential operations and accepts no alternate
history path. It checks the processing version and SHA-256 fingerprint of the
history used to generate the proposal. Stale and unsupported plans are rejected.

Before replacement, the updater acquires an exclusive lock, creates a uniquely
named backup, flushes and verifies the backup, then writes and verifies a
same-directory temporary JSON file. It checks for intervening edits before
`os.replace`. Existing IDs are skipped; conflicting new duplicates are rejected.
Existing records, notes, and unknown manual fields retain their JSON values.
Formatting may change; the backup preserves the original bytes. A no-op update
leaves history bytes unchanged and needs no backup.

## Processing and historical comparability

New records retain the existing summary and series fields. The method is recorded
as `history-compatible-v1` in `fit_details.processing`:

- Half-open 15-second buckets align to FIT session start; records are sorted by time.
- Distance uses the last valid cumulative distance in each bucket.
- Legacy cadence uses integer native cadence multiplied by two, then a positive-only mean.
- Speed uses a positive-only mean; heart rate and temperature average available values.
- Legacy series values round to one decimal. Pace uses unrounded mean speed before rounding.
- Missing measurements remain null. There is no interpolation or invented split data.
- Duration uses FIT timer time, not an implicit substitution of elapsed time.

`fit_details.precision` separately retains fractional cadence and unrounded bucket
values. It also preserves cadence and speed means including measured zeros.
Stationary readings and useful precision are therefore not silently lost through
the legacy aggregation rules. Summary distance and duration retain FIT precision.

The audit compares candidate aggregations and identifies cadence outliers.
Historical processing may not be exactly reproducible; no threshold is invented
to force outliers to match. Consumers with strict schemas must allow the optional
`processing` and `precision` extensions. Old records are never rewritten to add them.

## Security and repository boundaries

- Keys and tokens are not printed or logged. Upstream error bodies are suppressed.
- HTTPS redirects are disabled to avoid forwarding authorization elsewhere.
- Tokens remain in memory. Raw FIT and ZIP downloads are not written to disk.
- Processed fields use an allowlist excluding GPS coordinates.
- `.gitignore` allows only explicitly reviewed source, tests, and documentation.
  Private history, proposals, backups, credentials, downloads, environments, and
  temporary files are excluded. Review and extend the allowlist for new source files.
- Test credential values and binary fixtures are synthetic. Do not replace them
  with real account data or force-add ignored files.

DPAPI protects the key at rest for the current Windows account. Programs running
as that account can decrypt it, and an administrator controlling the machine may
gain access. Managed memory copies cannot all be reliably wiped. Private history
and proposals are not encrypted and inherit local project permissions.

## Limitations and next steps

- Original-history updates, background automation, and dedicated notes entry are not enabled.
- Kilometer split estimation is not implemented; its output list is empty.
- FIT files with conflicting sports, multiple sessions, unexpected units, or failed integrity checks require review.
- Duplicate detection does not reconcile later edits to existing Garmin activities.
- Concurrent uploads can shift offset-based pages; repeat a dry run after uploading finishes.
- Update locks coordinate this tool, not arbitrary editors. Avoid editing history during updates.
- Crashes can leave a lock or temporary file. Confirm no updater is active before cleanup.
- Flushed files and atomic replacement do not guarantee recovery from every power or storage failure.

Live account verification is separate from the synthetic test suite. No public
repository content should include a user's private dry-run output.

Decoder references: [fitdecode](https://github.com/polyvertex/fitdecode),
[CRC checking](https://fitdecode.readthedocs.io/en/latest/reference/reader.html),
and [FIT field definitions](https://github.com/polyvertex/fitdecode/blob/master/fitdecode/profile.py).
