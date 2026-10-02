# Deploy GRC Flow on Oracle Cloud (free, Mumbai)

This puts **both** on one Oracle Cloud Always Free server in the Mumbai region:

- **https://app.grc-flow.com**: the GRC Flow app (sign-in, clients, AI analyst);
- **https://grc-flow.com** (and www): your website. A starter page is included in
  `deploy/site/`; replace it with your own design whenever you like. Hosting the website
  here is optional: skip it if the website lives somewhere else.

Time needed: about 30 minutes, most of it waiting.

### How it fits together

```
            Internet
               │  ports 80 / 443 only
     ┌─────────▼──────────────────────────────────────────┐
     │ Oracle Cloud · Mumbai · free Arm server (Ubuntu)   │
     │                                                    │
     │  Caddy ── HTTPS certificates for every address     │
     │   ├─ app.grc-flow.com ──► GRC Flow app ──► PostgreSQL
     │   └─ grc-flow.com, www ──► website files (deploy/site)
     └────────────────────────────────────────────────────┘
```

Only Caddy is reachable from the internet. The app and the database have no public
ports, and everything runs in Docker so it can move to a bigger server later unchanged.

What you get for free: an Arm server with 2 CPUs and 12 GB of memory, enough for the app,
PostgreSQL and Caddy (HTTPS). Oracle's free tier has limits that change from time to time;
see "Keep it free and running" at the end.

---

## 1. Create the Oracle Cloud account

1. Go to **oracle.com/cloud/free** and sign up.
2. **Home region: choose _India West (Mumbai)_.** This can't be changed later, and free
   servers can only be created in your home region.
3. Oracle asks for a card to verify you. Free resources aren't charged.

## 2. Create the server

In the Oracle Cloud console: **Menu → Compute → Instances → Create instance**.

| Setting | Choose |
|---|---|
| Name | `grc-flow` |
| Image | **Canonical Ubuntu 24.04** (click *Change image*) |
| Shape | **Ampere → VM.Standard.A1.Flex**, **2 OCPUs**, **12 GB memory** (marked *Always Free-eligible*) |
| Networking | *Create new virtual cloud network* and *public subnet*, **Assign a public IPv4 address: Yes** |
| SSH keys | **Generate a key pair for me → Save private key** (keep this file safe, it's your login) |
| Boot volume | Default (about 50 GB) is enough |

Click **Create**. When it shows **Running**, copy the **Public IP address**.

> **"Out of capacity for shape VM.Standard.A1.Flex"?** Free Arm servers are popular. Try
> another *Availability domain* on the same page, or try again later (early morning IST
> often works).

## 3. Open ports 80 and 443 in Oracle's network

Oracle blocks web traffic by default. The setup script opens the server's own firewall,
but this network rule must be added in the console:

1. On the instance page, click the **Subnet** link → **Security Lists** →
   **Default Security List**.
2. **Add Ingress Rules** twice:
   - Source CIDR `0.0.0.0/0`, IP protocol **TCP**, destination port **80**
   - Source CIDR `0.0.0.0/0`, IP protocol **TCP**, destination port **443**

## 4. Point your domain at the server

At the company where you bought grc-flow.com (for example Namecheap: *Domain List →
Manage → Advanced DNS*), add these records. Remove any existing *parking page* or *URL
redirect* records for `@` and `www` first, if you're hosting the website here.

| Type | Host / Name | Value | TTL | For |
|---|---|---|---|---|
| A | `app` | the server's public IP | Automatic | the GRC Flow app |
| A | `@` | the server's public IP | Automatic | the website (skip if hosted elsewhere) |
| A | `www` | the server's public IP | Automatic | the website (skip if hosted elsewhere) |

Changes usually work within 5 to 30 minutes. Check from your PC with
`nslookup app.grc-flow.com`: it should show the server's IP.

## 5. Log in to the server

**Windows (PowerShell):**

```powershell
ssh -i C:\path\to\ssh-key.key ubuntu@YOUR_SERVER_IP
```

If it says the key's permissions are too open, run this once and try again:

```powershell
icacls C:\path\to\ssh-key.key /inheritance:r /grant:r "$($env:USERNAME):R"
```

**macOS / Linux:** `chmod 600 ssh-key.key && ssh -i ssh-key.key ubuntu@YOUR_SERVER_IP`

## 6. Run the setup script

On the server:

```bash
curl -fsSL https://raw.githubusercontent.com/Harshitahusts/GRC-Ai/main/deploy/setup-server.sh -o setup.sh
bash setup.sh
```

It asks for:

- **the app's subdomain**, for example `app.grc-flow.com`;
- **the website's domain**, `grc-flow.com` (or press Enter to skip hosting the website);
- **a Groq API key** (optional; you can add it later on the app's *AI provider* page).

Then it installs Docker, opens ports 80 and 443 on the server, downloads GRC Flow to
`~/grc-flow`, writes `.env` with a random database password, and starts the app,
PostgreSQL and Caddy. The first build takes about 5 minutes on the free Arm server.

## 7. Create your login and open the app

```bash
cd ~/grc-flow
sudo docker compose exec web grc-web adduser yourname
```

Open **https://app.grc-flow.com** and **https://grc-flow.com**. The first visit can take a
minute while Caddy gets the certificates. Then open **AI provider** in the app's sidebar to
add or test your Groq key.

## 8. Change the website

The website is the files in `~/grc-flow/deploy/site/` on the server (plain HTML; the
starter page is `index.html`). To use your own design, replace those files (for example
copy them up with `scp -r -i ssh-key.key mysite/* ubuntu@YOUR_SERVER_IP:~/grc-flow/deploy/site/`).
Changes show immediately; no restart needed.

For your own design, keep it in a separate folder rather than editing `deploy/site/`:
edited files there would stop the update script's `git pull`. Put the site in, say,
`/home/ubuntu/site`, add `GRC_SITE_DIR=/home/ubuntu/site` to `~/grc-flow/.env`, and run
`bash deploy/setup-server.sh` once to apply it.

---

## Everyday tasks

**Update to the latest version:**

```bash
cd ~/grc-flow && bash deploy/setup-server.sh
```

**See what's happening (logs):**

```bash
cd ~/grc-flow && sudo docker compose -f compose.yaml -f compose.postgres.yaml -f compose.caddy.yaml logs -f web caddy
```

**Back up** (database and evidence files), then copy the files off the server:

```bash
mkdir -p ~/backups && cd ~/grc-flow
sudo docker compose -f compose.yaml -f compose.postgres.yaml exec -T db pg_dump -U grc grc | gzip > ~/backups/db-$(date +%F).sql.gz
sudo docker run --rm -v grc-flow_grc-data:/data -v ~/backups:/b alpine tar czf /b/files-$(date +%F).tgz -C /data .
```

From your PC: `scp -i ssh-key.key "ubuntu@YOUR_SERVER_IP:~/backups/*" .`

## Troubleshooting

| Problem | Fix |
|---|---|
| Browser can't reach the site | Check step 3 (both ingress rules) and that `app` DNS points to the right IP: `nslookup app.grc-flow.com` |
| "Your connection is not private" | DNS changed recently: wait 5 to 10 minutes; Caddy retries. Check `logs -f caddy` |
| Build fails with "no space left" | `sudo docker system prune -af`, then run the script again |
| Forgot the login password | `sudo docker compose exec web grc-web passwd yourname` |

## Keep it free and running

- **Stay within the Always Free limits** (one A1 server with 2 OCPUs and 12 GB fits).
  Oracle halved these limits in 2026, so check their free-tier page now and then.
- **Idle servers can be reclaimed** on free accounts if they stay almost unused for about a
  week. Upgrading the account to *Pay As You Go* stops that; Always Free resources still
  cost nothing. Set a budget alert of ₹100 so you'd notice any charge.
- **Back up regularly** (see above). There are no automatic backups on the free tier.
- When real clients rely on it, consider a paid server with snapshots (for example
  DigitalOcean Bangalore). Moving is: back up, run the same script there, restore, and
  point the `app` DNS record at the new IP.
