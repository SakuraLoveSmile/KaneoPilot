# KaneoPilot core (`kanpilot`)

Python 3.9+ scheduling core for the KaneoPilot workbench. SQLite is the only
state write source; the desktop app and executors interact exclusively through
the fixed JSON command interface implemented here. The frozen contracts live in
`../docs/protocol.md` and `../docs/state-model.md` — this directory contains
the implementation, not the spec.

## Layout

```
core/
  kanpilot.py            CLI entry point (thin shim -> kanpilot.cli:main)
  kanpilot/
    cli.py               command registry, payload schema validation, main()
    constants.py         frozen constants (projects, slugs, defaults, paths)
    util.py              ISO-8601 UTC time, hashing, JSON, path/frontmatter helpers
    db.py                WAL SQLite schema (state-model.md §1) + row helpers
    remote.py            injectable Kaneo client factory + bridge call wrappers
    bridge.py            adapted kaneo_bridge.py (hash algorithms unchanged)
    derive.py            read-side views: display_status, lost, allowed_actions
    core.py              shared transitions: claim/report/block/finish helpers
    planfile.py          plan frontmatter + source snapshot validation
    dispatch_validate.py §4.5 dispatch claim guard chain
    cmd_read.py          doctor/tick/overview/list-*/get-task
    cmd_queue.py         enqueue/set-priority/set-paused/resolve/sync-tasks
    cmd_exec.py          claim/heartbeat/report/publish-plan/register-review
    cmd_remote.py        advance-task/create-review-fix/recheck
    cmd_dispatch.py      dispatch-prepare/begin-send/confirm/fail
    cmd_migrate.py       legacy manifest backup + idempotent import
  _vendor/               vendored PyYAML (only third-party dependency)
  tests/                 isolated unittest suite (no real network)
```

## CLI

```
python3 core/kanpilot.py <command> [--json '<payload>'] [--db <path>]
```

- Payload sources, in order: `--json` argument, then JSON on stdin (when stdin
  is not a TTY), then `--<flag> <value>` extras (dashes become underscores).
- Database path precedence: `--db` → `KANPILOT_DB` →
  `~/Library/Application Support/KaneoPilot/kanpilot.db`.
- Success: exactly one JSON object on stdout, exit code 0.
- Failure: `{"error": <message>, "code": <stable code>}` on stdout, exit 1.
  Codes are the frozen set in protocol.md §4.11 (`bad_request`, `not_found`,
  `conflict`, `stale_run`, `busy`, `paused`, `guard_failed`,
  `validation_failed`, `remote_unavailable`, `remote_conflict`,
  `evidence_invalid`, `send_conflict`, `already_done`, `migration_needed`,
  `internal`).
- Unknown commands, missing required fields, and unknown payload fields all
  fail with `bad_request`.

### Commands (25)

| Group | Commands |
|---|---|
| diagnostics | `doctor`, `tick`, `overview` |
| reads | `list-tasks`, `get-task`, `list-requests`, `list-events` |
| queue | `enqueue`, `set-priority`, `set-paused`, `resolve`, `sync-tasks` |
| executor | `claim`, `heartbeat`, `report`, `publish-plan`, `register-review` |
| remote writes | `advance-task`, `create-review-fix`, `recheck` |
| dispatch | `dispatch-prepare`, `dispatch-begin-send`, `dispatch-confirm`, `dispatch-fail` |
| ops | `migrate` |

## Semantics summary

- **Idempotency**: one active (`queued|claimed`) request per
  `idempotency_key` (partial unique index). `enqueue` returns the existing
  request with `deduplicated: true`.
- **Claim**: single `BEGIN IMMEDIATE` transaction — inline `tick`, pause
  check, dispatch stage-busy check, priority ordering, guard, atomic claim
  (run_id + lease + attempt). For `dispatch` requests the §4.5 validation
  chain (plan hash, snapshot, requirements, remote state, review markers,
  send-evidence conflicts) runs **outside** the transaction; the request is
  re-checked `queued` inside a second `BEGIN IMMEDIATE` before claiming.
- **Leases**: `lease_expires_at = now + lease_seconds` (default 1200).
  `heartbeat` renews. A `claimed` request past its lease is *derived* as
  `lost` at read time — never rewritten or requeued automatically; only
  `resolve release_lost` releases it (restoring the pre-claim task status
  stored in the request payload).
- **Serial bits**: at most one `claimed` dispatch request at a time
  (state-model §5). Remote writes (`dispatch-begin-send`, `advance-task`,
  `create-review-fix`) hold `settings.lifecycle_owner` for the duration.
  These are durable local operation leases; Kaneo has no CAS, so they do not
  prevent manual/remote races.
- **Pause**: `set-paused` persists global/per-stage pause; it blocks only new
  claims, never running work.
- **Display**: `list-tasks`/`get-task` derive `display_status`
  (`待规划|规划中|就绪|已排队|派发中|执行中|评审中|需人工|执行端失联|已归档`)
  and `allowed_actions` from live rows.
- **Migration**: `migrate` copies `KaneoPlanner|KaneoExecutor|KaneoReviewer`
  state and the Plan directory to `backup_root/migration-<ts>/` (originals
  untouched), imports manifests by hash set recorded in `settings.migration`
  (idempotent), converts mid-dispatch leftovers to
  `needs_user/interrupted_dispatch`, and never resurrects file locks.

## Environment overrides (tests / sandboxing)

| Variable | Default |
|---|---|
| `KANPILOT_DB` | `$KANPILOT_APP_SUPPORT/kanpilot.db` |
| `KANPILOT_APP_SUPPORT` | `~/Library/Application Support/KaneoPilot` |
| `KANPILOT_PLAN_ROOT` | `~/Documents/Code/Plan` |
| `KANPILOT_REPOS_ROOT` | `~/Documents/Code/Project/Personal Project` |
| `KANPILOT_LEGACY_ROOT` | `~/Library/Application Support/Codex` |
| `KANPILOT_REPORTS_ROOT` | `$KANPILOT_LEGACY_ROOT/KaneoReviewer/reports` |
| `KANEO_CREDENTIALS_PATH` | `~/.config/kaneo-mcp/credentials.json` |
| `KANEO_TIMEOUT` | `10` (seconds) |

Credentials are read only by `bridge.load_credentials()`; tokens are never
printed, logged, or persisted. The bridge refuses non-HTTPS base URLs except
localhost.

## Tests

```
cd core && python3 -m unittest discover -s tests -v
# or: python3 -m pytest core/tests -q   (from repo root, if pytest present)
```

The suite runs fully offline: `tests/helpers.py` injects `FakeKaneoClient`
via `remote.set_client_factory()` and redirects every path constant to a
tempdir. Coverage: schema init, enqueue dedupe, competing claims, claim
guards, the §4.5 validation chain, prepare→begin-send→confirm→fail,
not_sent/unknown semantics, lost derivation + resolve, tick cycles,
publish-plan, register-review, advance-task, create-review-fix idempotency,
pause, and idempotent migration.
