#!/usr/bin/env bash
# Run once on the server to let GitHub Actions deploy GRC Flow (see docs/DEPLOY_ORACLE.md,
# "Automatic deploys"):
#
#   cd ~/grc-flow && bash deploy/enable-ci.sh
#
# It makes a new SSH key that can only run deploy/ci-deploy.sh (no shell, no tunnels),
# prints what to paste into GitHub, then deletes the private key from this server.
# Running it again replaces the key, and the old one stops working.
set -euo pipefail

mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
ssh-keygen -q -t ed25519 -N "" -C "grc-flow-deploy" -f "$tmp/key"

# Replace any earlier deploy key, keep every other key.
grep -v 'grc-flow-deploy$' ~/.ssh/authorized_keys > "$tmp/keys" || true
echo "command=\"bash $HOME/grc-flow/deploy/ci-deploy.sh\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty $(cat "$tmp/key.pub")" >> "$tmp/keys"
cat "$tmp/keys" > ~/.ssh/authorized_keys

ip=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
hostkey=$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)

cat <<MSG

GitHub Actions can now deploy to this server. In GitHub, open each repository
(Harshitahusts/GRC-Ai and Harshitahusts/GRC-WEBSITE) > Settings > Secrets and variables >
Actions > New repository secret, and add these four:

DEPLOY_HOST
$ip

DEPLOY_USER
$USER

DEPLOY_KNOWN_HOSTS
$ip $hostkey

DEPLOY_SSH_KEY  (all lines, including BEGIN and END)
$(cat "$tmp/key")

The private key above is not kept on this server. If you lose it, run this script again.
MSG
