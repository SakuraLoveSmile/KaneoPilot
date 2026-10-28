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
# Bare-AppKit control: attach mode only, 65 537 bytes, product background path and plain AppKit control,
# 3 runs each per attempt. Only the first process with text on a runner is cold, so the first variant rotates.
case "${GITHUB_RUN_ATTEMPT:-1}" in
 2|4|6) ORDER="product bare" ;;
 *) ORDER="bare product" ;;
esac
REPORTS="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/macos-reports"
mkdir -p "$REPORTS"
for variant in $ORDER; do
 for run in 1 2 3; do
  echo "PROBE_RUN mode=attach variant=$variant run=$run attempt=${GITHUB_RUN_ATTEMPT:-1} bytes=65537"
  KANPILOT_PROBE_MODE=attach KANPILOT_PROBE_VARIANT="$variant" KANPILOT_PROBE_BYTES=65537 "$FIXTURE/fixture" | tee "$REPORTS/attempt-${GITHUB_RUN_ATTEMPT:-1}-$variant-$run.txt"
 done
done
