#!/usr/bin/env bash
# One-time setup of GRC Flow on a fresh Ubuntu server (Oracle Cloud, DigitalOcean, ...).
#
#   curl -fsSL https://raw.githubusercontent.com/Harshitahusts/GRC-Ai/main/deploy/setup-server.sh -o setup.sh
#   bash setup.sh
#
# It installs Docker, opens ports 80 and 443 in the server's own firewall (Oracle's Ubuntu
# images block them by default), downloads GRC Flow, writes .env with a random database
# password, and starts the app, PostgreSQL and Caddy (HTTPS). Safe to run again: it keeps an
# existing .env and only updates and restarts the app.
set -euo pipefail

REPO="https://github.com/Harshitahusts/GRC-Ai.git"
DIR="$HOME/grc-flow"
COMPOSE=(docker compose -f compose.yaml -f compose.postgres.yaml -f compose.caddy.yaml)

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

say "1/6 Checking the server"
# shellcheck source=/dev/null
. /etc/os-release
echo "System: $PRETTY_NAME on $(uname -m)"
if [ "${ID:-}" != "ubuntu" ]; then
  warn "This script is tested on Ubuntu. On $PRETTY_NAME some steps may need changes."
fi
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
echo "Memory: ${mem_mb} MB"
if [ "$mem_mb" -lt 1800 ]; then
  warn "Less than 2 GB of memory. GRC Flow needs about 2 GB (4 GB with name detection)."
fi

say "2/6 Installing Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Docker is already installed: $(docker --version)"
else
  $SUDO apt-get update -y
  $SUDO apt-get install -y ca-certificates curl git
  curl -fsSL https://get.docker.com | $SUDO sh
fi
$SUDO systemctl enable --now docker
if ! groups "$USER" | grep -qw docker; then
  $SUDO usermod -aG docker "$USER"
fi
# Use sudo for docker in this run; after logging in again it works without.
if docker info >/dev/null 2>&1; then DOCKER=""; else DOCKER="$SUDO"; fi

say "3/6 Opening ports 80 and 443 in the server's firewall"
# Oracle's Ubuntu images ship iptables rules that reject everything except SSH.
# This adds ACCEPT rules for web traffic just before that reject rule, and saves them.
if command -v iptables >/dev/null 2>&1 && $SUDO iptables -S INPUT 2>/dev/null | grep -q REJECT; then
  for port in 80 443; do
    if ! $SUDO iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null; then
      $SUDO iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
    fi
  done
  if ! $SUDO iptables -C INPUT -p udp --dport 443 -j ACCEPT 2>/dev/null; then
    $SUDO iptables -I INPUT 5 -p udp --dport 443 -j ACCEPT   # HTTP/3
  fi
  if command -v netfilter-persistent >/dev/null 2>&1; then
    $SUDO netfilter-persistent save
  else
    $SUDO apt-get install -y iptables-persistent
    $SUDO netfilter-persistent save
  fi
  echo "Ports 80 and 443 are open on the server."
elif command -v ufw >/dev/null 2>&1 && $SUDO ufw status | grep -q "Status: active"; then
  $SUDO ufw allow 80/tcp && $SUDO ufw allow 443/tcp && $SUDO ufw allow 443/udp
else
  echo "No blocking firewall found on the server."
fi
echo "Also open ports 80 and 443 in your cloud provider's network settings"
echo "(Oracle: Virtual Cloud Network > Security List > Ingress Rules). See docs/DEPLOY_ORACLE.md."

say "4/6 Downloading GRC Flow"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

say "5/6 Settings (.env)"
if [ -f .env ] && grep -q '^GRC_DOMAIN=' .env; then
  echo "Keeping the existing .env."
else
  read -rp "Subdomain for the app, e.g. app.grc-flow.com: " domain
  read -rp "Also host the website here? Enter its domain (e.g. grc-flow.com) or press Enter to skip: " site
  read -rsp "Groq API key (optional, press Enter to add it later in the app): " groq
  echo
  cp .env.example .env
  {
    echo ""
    echo "# Written by deploy/setup-server.sh on $(date -u +%Y-%m-%d)"
    echo "GRC_DOMAIN=$domain"
    if [ -n "$site" ]; then echo "GRC_SITE_ADDRESS=$site, www.$site"; fi
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
    if [ -n "$groq" ]; then echo "GROQ_API_KEY=$groq"; fi
  } >> .env
  chmod 600 .env
  echo "Saved .env (readable only by you). The database password was generated for you."
fi

domain=$(grep '^GRC_DOMAIN=' .env | tail -1 | cut -d= -f2)
site=$(grep '^GRC_SITE_ADDRESS=' .env | tail -1 | cut -d= -f2 | cut -d, -f1 || true)
public_ip=$(curl -fsS --max-time 5 https://api.ipify.org || true)
echo "This server's public IP: ${public_ip:-unknown}"
for name in "$domain" ${site:+"$site" "www.$site"}; do
  resolved=$(getent ahostsv4 "$name" 2>/dev/null | awk 'NR==1 {print $1}' || true)
  echo "$name points to: ${resolved:-nothing yet}"
  if [ -z "$resolved" ] || [ "$resolved" != "$public_ip" ]; then
    warn "DNS for $name doesn't point here yet. Add an A record for it with ${public_ip:-the server IP}."
  fi
done
echo "Caddy keeps retrying certificates, so HTTPS starts a few minutes after DNS is right."

say "6/6 Starting GRC Flow (the first build takes a few minutes)"
$DOCKER "${COMPOSE[@]}" up -d --build
$DOCKER "${COMPOSE[@]}" ps

run="${DOCKER:+$DOCKER }docker compose"
cat <<EOF

Done. Next:
  1. Create your login (asks for a password):
       cd $DIR && $run exec web grc-web adduser yourname
  2. Open https://$domain${site:+   (website: https://$site)}
     (the first visit can take a minute while the certificate is issued)

Update later:   cd $DIR && bash deploy/setup-server.sh
Logs:           cd $DIR && ${DOCKER:+$DOCKER }${COMPOSE[*]} logs -f web caddy
EOF
