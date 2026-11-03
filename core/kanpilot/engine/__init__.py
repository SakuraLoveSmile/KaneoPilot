"""New scheduling engine (protocol §5.27 / state-model §7–§10).

All ordinary projects use live scheduling. Explicit process-wide shadow mode
is diagnostic only. Existing per-stage runners remain shared, and all state
writes still go through core commands (SQLite is the single source of truth).

Layout:
- ``ladder``    — four failure ladders + exponential backoff (state-model §8)
- ``decide``    — pure, table-driven reconciler decision (phase×condition ×
                  failure class); no DB, no I/O
- ``attention`` — single-source attention inbox derivation (§5.27.3)
- ``loop``      — long-lived reconciler (read snapshot → decide → leased
                  request → worker; crash recovery by lease + world probe)
- ``steps``     — deterministic step handlers (intake/plan/confirm/dispatch/
                  observe/verdict/commit/review/fix/pr/sync)
- ``health``    — engine status / lease health (engine status)
- ``runners``   — Plus runner carrying planning / execution / review
"""
from __future__ import annotations

# Live scheduling plus the explicit process-wide diagnostic mode.
ENGINE_MODE_SHADOW = "shadow"
ENGINE_MODE_LIVE = "live"
ENGINE_MODES = (ENGINE_MODE_SHADOW, ENGINE_MODE_LIVE)

__all__ = [
    "ENGINE_MODE_SHADOW", "ENGINE_MODE_LIVE",
    "ENGINE_MODES",
]
