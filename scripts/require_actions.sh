#!/bin/sh
# Release/Android entries stay Actions-only. Selected macOS development entries
# explicitly opt in to a local build; never pretend to be an Actions runner.
if [ "${1:-}" = --allow-local-macos ] && [ "${KANPILOT_LOCAL_BUILD:-}" = 1 ] && [ "${GITHUB_ACTIONS:-}" != true ]; then
    if [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ]; then
        exit 0
    fi
    printf '%s\n' 'Local macOS builds require an Apple Silicon Mac.' >&2
    exit 1
fi
if [ "${GITHUB_ACTIONS:-}" != true ] || [ "${RUNNER_ENVIRONMENT:-}" != github-hosted ]; then
    printf '%s\n' 'KaneoPilot builds run only in GitHub-hosted Actions. Use Packages: https://github.com/SakuraLoveSmile/KaneoPilot/actions/workflows/packages.yml' >&2
    exit 1
fi
