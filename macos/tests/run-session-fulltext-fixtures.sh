#!/bin/bash
# Real NSHostingView -> the production read-only NSTextView bridge. No DB/core calls.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-session-fulltext.XXXXXX")"
trap 'rm -rf "$FIXTURE"' EXIT
swiftc -O -whole-module-optimization -parse-as-library \
 "$ROOT/macos/KaneoPilot/Core/Models.swift" "$HERE/LabelsStub.swift" \
 "$ROOT/macos/KaneoPilot/Views/SessionMessagePresentation.swift" \
 "$ROOT/macos/KaneoPilot/Views/MarkdownReaderView.swift" \
 "$ROOT/macos/KaneoPilot/Views/SessionMessageDetailView.swift" \
 "$HERE/session_fulltext_main.swift" -o "$FIXTURE/fixture"
"$FIXTURE/fixture"
