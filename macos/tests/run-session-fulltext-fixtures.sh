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
# Diagnostic branch only (stage 1b). Stage 1 showed the stall only on the first launch of a freshly
# compiled binary, so that cold launch is sampled with `sample -wait`. The first mode rotates with the
# run attempt, which separates the first launch from the mode. All other launches are warm and
# unsampled. Latency is reported, not gated.
case "${GITHUB_RUN_ATTEMPT:-1}" in
 2) ORDER="attach full idle" ;;
 3) ORDER="idle full attach" ;;
 *) ORDER="full attach idle" ;;
esac
REPORTS="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/macos-reports"
mkdir -p "$REPORTS"
cold=1
for mode in $ORDER; do
 for run in 1 2 3; do
  echo "PROBE_RUN mode=$mode run=$run attempt=${GITHUB_RUN_ATTEMPT:-1} cold=$cold"
  if [ "$cold" = 1 ]; then
   cold=0
   out="$REPORTS/cold-sample-$mode-attempt-${GITHUB_RUN_ATTEMPT:-1}.txt"
   sample fixture 15 5 -wait -file "$out" > "$out.stdout" 2>&1 &
   sampler=$!
   # Give `sample -wait` time to start watching before the first launch.
   sleep 1
   echo "SAMPLE_START mode=$mode out=$out"
   KANPILOT_PROBE_MODE="$mode" "$FIXTURE/fixture"
   for _ in 1 2 3 4 5 6; do kill -0 "$sampler" 2>/dev/null || break; sleep 5; done
   kill "$sampler" 2>/dev/null || true
   wait "$sampler" 2>/dev/null || true
   cat "$out.stdout"
   cat "$out" 2>/dev/null || echo "SAMPLE_MISSING out=$out"
  else
   KANPILOT_PROBE_MODE="$mode" "$FIXTURE/fixture"
  fi
 done
done
