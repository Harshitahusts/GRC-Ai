#!/usr/bin/env bash
# Runs when GitHub Actions connects with the deploy key (set up by deploy/enable-ci.sh).
# authorized_keys forces this script for that key, so the key can't open a shell or run
# anything else. What GitHub asks for arrives in SSH_ORIGINAL_COMMAND:
#
#   deploy                   update and restart, with GRC_DEPLOY_BRANCHES from .env
#   deploy main              only main
#   deploy branches all      main plus every open branch
#   deploy branches a,b      main plus these branches
#   status                   what's running now
set -euo pipefail

DIR="$HOME/grc-flow"
read -r verb mode list rest <<< "${SSH_ORIGINAL_COMMAND:-status}" || true

if [ -n "${rest:-}" ] || ! [[ "${list:-}" =~ ^[A-Za-z0-9._/,-]*$ ]]; then
  echo "Refused: unexpected request '${SSH_ORIGINAL_COMMAND:-}'." >&2
  exit 2
fi

case "${verb:-}:${mode:-}" in
  status:)
    cd "$DIR"
    git log -1 --format='Server code: %h %s (%cr)'
    fmt='table {{.Label "com.docker.compose.service"}}\t{{.Status}}'
    docker ps --filter label=com.docker.compose.project=grc-flow --format "$fmt" 2>/dev/null \
      || sudo -n docker ps --filter label=com.docker.compose.project=grc-flow --format "$fmt"
    exit 0 ;;
  deploy:) flags=() ;;
  deploy:main) flags=(--main-only) ;;
  deploy:branches) [ -n "${list:-}" ] || list=all; flags=(--branches "$list") ;;
  *) echo "Refused: unknown request '${SSH_ORIGINAL_COMMAND:-}'." >&2; exit 2 ;;
esac

# One deploy at a time: the app and the website repositories can both start one.
exec 9>"$HOME/.grc-flow-deploy.lock"
if ! flock -n 9; then
  echo "Another deploy is running; waiting for it to finish..."
  flock -w 1800 9
fi

# Update first, so the setup script that runs is the new one.
git -C "$DIR" pull --ff-only
exec bash "$DIR/deploy/setup-server.sh" "${flags[@]}"
