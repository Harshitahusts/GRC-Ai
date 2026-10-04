# Deploy GRC Flow on Oracle Cloud (free, Mumbai)

This puts everything on one Oracle Cloud Always Free server in the Mumbai region:

- **https://app.grc-flow.com**: the GRC Flow app (sign-in, clients, AI analyst), with its
  data in PostgreSQL;
- **https://grc-flow.com** (www redirects to it): the website, built from
  [GRC-WEBSITE](https://github.com/Harshitahusts/GRC-WEBSITE).

The two are linked: the website's **Sign in** button opens app.grc-flow.com, and the app's
sign-in page links back to grc-flow.com. The website is optional: skip it if it lives
somewhere else.

Time needed: about 45 minutes, most of it waiting.

### How it fits together

```
            Internet
               │  ports 80 / 443 only
     ┌─────────▼──────────────────────────────────────────┐
     │ Oracle Cloud · Mumbai · free Arm server (Ubuntu)   │
     │                                                    │
     │  Caddy ── HTTPS certificates for every address     │
     │   ├─ app.grc-flow.com ──► GRC Flow app ──► PostgreSQL
     │   └─ grc-flow.com, www ──► website (Next.js)       │
     └────────────────────────────────────────────────────┘
```

Only Caddy is reachable from the internet. The app, website and database have no
public ports, and everything runs in Docker so it can move to a bigger server later unchanged.

What you get for free: an Arm server with 2 CPUs and 12 GB of memory, enough for all of
it. Oracle's free tier has limits that change from time to time;
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

## 4. Point your domain at the server (Namecheap)

In Namecheap: **Domain List → grc-flow.com → Manage → Advanced DNS → Host Records**.

1. **Delete** Namecheap's parking records: usually a `CNAME Record` for `www` pointing to
   `parkingpage.namecheap.com` and a `URL Redirect Record` for `@`.
2. **Add three A records** (*Add New Record → A Record*), each with the server's public IP:

| Type | Host | Value | TTL | For |
|---|---|---|---|---|
| A Record | `app` | the server's public IP | Automatic | the GRC Flow app |
| A Record | `@` | the server's public IP | Automatic | the website |
| A Record | `www` | the server's public IP | Automatic | the website |

Skip `@` and `www` if the website is hosted somewhere else. If you added a `demo` record
earlier, you can delete it: it's no longer used.

> **Don't touch the email records.** If you use Google Workspace or Gmail for
> talk@grc-flow.com, leave its `MX` and `TXT` records (SPF, DKIM, verification) exactly as
> they are, or email stops arriving. Also check *Mail Settings* still says *Custom MX* (or
> *Gmail*), not *No email service*.

Changes usually work within 5 to 30 minutes. Check from your PC:

```
nslookup app.grc-flow.com
nslookup grc-flow.com
```

Each should show the server's IP. Wait for this before step 6, or the HTTPS
certificates can't be issued yet (Caddy keeps retrying, so it fixes itself later).

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
PostgreSQL, the website and Caddy. The first build takes 5 to 10 minutes on the
free Arm server.

## 7. Create your login and open the app

```bash
cd ~/grc-flow
sudo docker compose exec web grc-web adduser yourname
```

Open **https://grc-flow.com** and click **Sign in**: it takes you to
**https://app.grc-flow.com**. The first visit can take a minute while Caddy gets the certificates. Then open **AI provider** in the
app's sidebar to add or test your Groq key.

## 8. Change the website

The website is built from the `main` branch of
[GRC-WEBSITE](https://github.com/Harshitahusts/GRC-WEBSITE). Merge your changes there, then
run the update command below: it rebuilds the website from the latest `main`.

---

## Everyday tasks

**Update to the latest version** (app and website):

```bash
cd ~/grc-flow && bash deploy/setup-server.sh
```

**Also deploy branches that aren't merged yet** (for example to try a change on the
real server before merging its pull request):

```bash
cd ~/grc-flow && bash deploy/setup-server.sh --branches            # every open branch
cd ~/grc-flow && bash deploy/setup-server.sh --branches my-branch  # only these (comma-separated)
```

The server builds a copy of `main` with those branches merged on top, for both the app and
the website. Nothing is merged or pushed on GitHub. A branch that conflicts with `main` is
skipped with a warning. To do this on every update, add `GRC_DEPLOY_BRANCHES=all` to
`~/grc-flow/.env`; `--main-only` goes back to `main` alone. Unreviewed branches can break
the live site, so prefer merging pull requests once they're ready.

**After an update** a normal reload shows the new version; Ctrl+F5 isn't needed. Pages are
always checked for a newer version, and the app's styles and scripts change their address
whenever they change.

**See what's happening (logs):** the script prints the exact command at the end. With the
website it's:

```bash
cd ~/grc-flow && sudo docker compose -f compose.yaml -f compose.postgres.yaml -f compose.caddy.yaml -f compose.site.yaml logs -f web website caddy
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
| Browser can't reach the site | Check step 3 (both ingress rules) and that DNS points to the right IP: `nslookup app.grc-flow.com` |
| Build stops with "killed" | Out of memory: use at least 6 GB, or add swap: `sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` |
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
  point the DNS records at the new IP.
