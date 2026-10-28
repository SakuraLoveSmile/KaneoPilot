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
# Diagnostic branch only. No sampler: `sample` suspends the target and distorts the timing it measures.
# The fixture reads the main thread's state from its own watchdog instead. Latency is reported, not gated.
# Stage 2 C size comparison: attach mode only, 65 537 and 262 144 bytes, 3 runs each per attempt.
# Only the first text install on a runner is cold, so the first size rotates with the run attempt.
case "${GITHUB_RUN_ATTEMPT:-1}" in
 2|4|6) ORDER="262144 65537" ;;
 *) ORDER="65537 262144" ;;
esac
REPORTS="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/macos-reports"
mkdir -p "$REPORTS"
for bytes in $ORDER; do
 for run in 1 2 3; do
  echo "PROBE_RUN mode=attach run=$run attempt=${GITHUB_RUN_ATTEMPT:-1} bytes=$bytes"
  KANPILOT_PROBE_MODE=attach KANPILOT_PROBE_BYTES="$bytes" "$FIXTURE/fixture" | tee "$REPORTS/attempt-${GITHUB_RUN_ATTEMPT:-1}-$bytes-$run.txt"
 done
done
