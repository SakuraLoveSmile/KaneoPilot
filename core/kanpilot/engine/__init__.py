"""New scheduling engine (protocol §5.27 / state-model §7–§10).

Strangler phase (AGENTS.md): ``engine_mode`` is switched per project through
``legacy | shadow | live``. This package is **additive** — no existing module
is removed and all state writes still go through core commands (SQLite stays
the single source of truth). Nothing here imports a submodule at package load
time, so a partially-landed engine never breaks ``import kanpilot.engine`` or
the CLI command registry (the rehearsal "engine gate" relies on this).

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

# engine_mode vocabulary (strangler three-state switch, per project).
ENGINE_MODE_LEGACY = "legacy"
ENGINE_MODE_SHADOW = "shadow"
ENGINE_MODE_LIVE = "live"
ENGINE_MODES = (ENGINE_MODE_LEGACY, ENGINE_MODE_SHADOW, ENGINE_MODE_LIVE)

__all__ = [
    "ENGINE_MODE_LEGACY", "ENGINE_MODE_SHADOW", "ENGINE_MODE_LIVE",
    "ENGINE_MODES",
]
