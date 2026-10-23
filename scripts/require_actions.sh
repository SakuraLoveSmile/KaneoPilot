#!/bin/sh
# Application builds run only on GitHub-hosted Actions runners.
if [ "${GITHUB_ACTIONS:-}" != true ] || [ "${RUNNER_ENVIRONMENT:-}" != github-hosted ]; then
    printf '%s\n' 'KaneoPilot builds run only in GitHub-hosted Actions. Use Packages: https://github.com/SakuraLoveSmile/KaneoPilot/actions/workflows/packages.yml' >&2
    exit 1
fi
