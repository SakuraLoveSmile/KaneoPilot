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
# The first mode rotates with the run attempt.
case "${GITHUB_RUN_ATTEMPT:-1}" in
 2) ORDER="attach full idle" ;;
 3) ORDER="idle full attach" ;;
 *) ORDER="full attach idle" ;;
esac
REPORTS="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/macos-reports"
mkdir -p "$REPORTS"
for mode in $ORDER; do
 for run in 1 2 3; do
  echo "PROBE_RUN mode=$mode run=$run attempt=${GITHUB_RUN_ATTEMPT:-1}"
  KANPILOT_PROBE_MODE="$mode" "$FIXTURE/fixture" | tee "$REPORTS/attempt-${GITHUB_RUN_ATTEMPT:-1}-$mode-$run.txt"
 done
done
