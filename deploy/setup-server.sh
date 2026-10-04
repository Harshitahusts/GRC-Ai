#!/usr/bin/env bash
# One-time setup of GRC Flow on a fresh Ubuntu server (Oracle Cloud, DigitalOcean, ...).
#
#   curl -fsSL https://raw.githubusercontent.com/Harshitahusts/GRC-Ai/main/deploy/setup-server.sh -o setup.sh
#   bash setup.sh
#
# It installs Docker, opens ports 80 and 443 in the server's own firewall (Oracle's Ubuntu
# images block them by default), downloads GRC Flow, writes .env with a random database
# password, and starts the app, PostgreSQL and Caddy (HTTPS). With a website domain it also
# runs the website (grc-flow.com, www.), linked to and from the app.
# Safe to run again: it keeps an existing .env and only updates and restarts everything.
#
# Deploy work that isn't merged yet: add branches on top of main for this server only.
# Nothing is pushed or merged on GitHub; a branch that conflicts with main is skipped.
#   bash deploy/setup-server.sh --branches          every open branch of both repositories
#   bash deploy/setup-server.sh --branches a,b      only these branch names (in either repo)
# To do it on every run, set GRC_DEPLOY_BRANCHES=all (or a list) in .env; --main-only overrides.
set -euo pipefail

branches_arg=""
while [ $# -gt 0 ]; do
  case "$1" in
    --branches)
      if [ -n "${2:-}" ] && [ "${2#-}" = "$2" ]; then branches_arg="$2"; shift; else branches_arg="all"; fi ;;
    --branches=*) branches_arg="${1#*=}" ;;
    --main-only) branches_arg="none" ;;
    *) echo "Unknown option: $1 (use --branches [list] or --main-only)"; exit 2 ;;
  esac
  shift
done

REPO="https://github.com/Harshitahusts/GRC-Ai.git"
WEBSITE_REPO="https://github.com/Harshitahusts/GRC-WEBSITE.git"
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
    if [ -n "$site" ]; then echo "GRC_SITE_DOMAIN=$site"; fi
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
    if [ -n "$groq" ]; then echo "GROQ_API_KEY=$groq"; fi
  } >> .env
  chmod 600 .env
  echo "Saved .env (readable only by you). The database password was generated for you."
fi

# .env files from before the website moved here name the website as
# GRC_SITE_ADDRESS="grc-flow.com, www.grc-flow.com"; carry that over.
if ! grep -q '^GRC_SITE_DOMAIN=' .env && grep -q '^GRC_SITE_ADDRESS=' .env; then
  old=$(grep '^GRC_SITE_ADDRESS=' .env | tail -1 | cut -d= -f2 | cut -d, -f1 | tr -d ' "')
  if [ -n "$old" ]; then echo "GRC_SITE_DOMAIN=$old" >> .env; fi
fi

domain=$(grep '^GRC_DOMAIN=' .env | tail -1 | cut -d= -f2)
site=$(grep '^GRC_SITE_DOMAIN=' .env | tail -1 | cut -d= -f2 || true)
if [ -n "$site" ]; then
  COMPOSE+=(-f compose.site.yaml)
fi
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

# Which code to build: main, or main with branches merged on top (see the top of this file).
branches="${branches_arg:-$(grep '^GRC_DEPLOY_BRANCHES=' .env | tail -1 | cut -d= -f2- | tr -d '"' || true)}"
if [ -n "$branches" ] && [ "$branches" != "none" ] && [ "$branches" != "main" ]; then
  say "Merging branches on top of main (this server only)"
  # build_copy NAME URL FOLDER: a copy of origin/main with the branches merged into it.
  build_copy() {
    local name=$1 url=$2 dir=$3 b
    local -a list=()
    if [ -d "$dir/.git" ]; then git -C "$dir" fetch -q --prune origin; else git clone -q "$url" "$dir"; fi
    git -C "$dir" checkout -q --detach origin/main
    git -C "$dir" reset -q --hard origin/main
    git -C "$dir" clean -qfdx
    if [ "$branches" = "all" ]; then
      mapfile -t list < <(git -C "$dir" for-each-ref --format='%(refname:lstrip=3)' refs/remotes/origin | grep -vxE 'HEAD|main' || true)
    else
      read -ra list <<< "${branches//,/ }"
    fi
    for b in "${list[@]}"; do
      git -C "$dir" rev-parse -q --verify "origin/$b" >/dev/null || continue   # not in this repo
      if git -C "$dir" merge-base --is-ancestor "origin/$b" HEAD; then continue; fi   # already in main
      if git -C "$dir" -c user.name="GRC Flow deploy" -c user.email=deploy@localhost \
          merge -q --no-edit "origin/$b" >/dev/null 2>&1; then
        echo "$name: merged $b"
      else
        git -C "$dir" merge --abort 2>/dev/null || git -C "$dir" reset -q --hard HEAD
        warn "$name: skipped $b, it conflicts with main. Resolve it in a pull request."
      fi
    done
    echo "$name: building $(git -C "$dir" log -1 --format='%h %s')"
  }
  mkdir -p "$DIR/.deploy"
  build_copy "App" "$REPO" "$DIR/.deploy/app"
  export GRC_APP_CONTEXT="$DIR/.deploy/app"
  if [ -n "$site" ]; then
    build_copy "Website" "$WEBSITE_REPO" "$DIR/.deploy/website"
    export GRC_WEBSITE_CONTEXT="$DIR/.deploy/website"
  fi
else
  echo "Building main. To include branches that aren't merged yet: bash deploy/setup-server.sh --branches"
fi

say "6/6 Starting GRC Flow (the first build takes 5 to 10 minutes)"
$DOCKER "${COMPOSE[@]}" up -d --build --remove-orphans   # also stops services no longer used (the old demo)
$DOCKER "${COMPOSE[@]}" ps

# The server no longer runs a public demo: remove its nightly reset if an earlier
# version of this script added one.
cron=$( (command -v crontab >/dev/null 2>&1 && $SUDO crontab -l 2>/dev/null) || true)
if printf '%s\n' "$cron" | grep -q 'grc-flow-demo-reset'; then
  printf '%s\n' "$cron" | { grep -v 'grc-flow-demo-reset' || true; } | $SUDO crontab -
  echo "Removed the old nightly demo reset."
fi

run="${DOCKER:+$DOCKER }docker compose"
cat <<EOF

Done. Next:
  1. Create your login (asks for a password):
       cd $DIR && $run exec web grc-web adduser yourname
  2. Open https://$domain${site:+   (website: https://$site)}
     (the first visit can take a minute while the certificates are issued)

Update later:   cd $DIR && bash deploy/setup-server.sh
Logs:           cd $DIR && ${DOCKER:+$DOCKER }${COMPOSE[*]} logs -f web caddy${site:+ website}
EOF
