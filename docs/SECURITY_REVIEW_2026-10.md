# Security review, October 2026

Vulnerability assessment of GRC Flow (app.grc-flow.com) and the GRC-Flow website
(grc-flow.com), run on the code and on a local copy of the production stack (app,
PostgreSQL, website and Caddy with HTTPS), built from `main` of both repositories.

## What was tested

| Area | How |
|---|---|
| Known vulnerabilities in dependencies | `pip-audit` on a clean install of the app (PyPI advisory data, with CVE and GHSA ids), `npm audit` on the website |
| Container base images | `python:3.12-slim`, `node:22-alpine`: package versions; their OS update servers were blocked from the test environment, so pending OS patches couldn't be listed (see fix 7) |
| Static analysis | Bandit (Python), ESLint with `eslint-plugin-security` and `no-unsanitized` (TypeScript/React), manual review of every finding |
| Secrets | `detect-secrets` on both repositories, and a pattern search of their full git history (AWS, GitHub, Anthropic, OpenAI, Groq, Google, Slack, Resend keys, private keys, passwords) |
| Live testing | Scripted attacks against the running stack: TLS versions, security headers, cookies, login (enumeration, brute force, spraying), CSRF, session fixation and revocation, open redirect, path traversal, information leakage, HTTP methods, all 117 routes without login, every POST route as a viewer and as a member, stored XSS, AI chat Markdown injection, file uploads (type, content, size, file name), SSRF |

Not covered: the live server itself (Oracle Cloud security list, SSH, OS) because it
isn't reachable from the test environment, and Semgrep's rule sets (download blocked).

## Findings and fixes

| # | Severity (CVSS 3.1) | Finding | Fix |
|---|---|---|---|
| 1 | High (8.1) | **A removed user kept access for up to 8 hours.** Sessions are signed cookies and were never checked against the database, and a removed user's role (empty) counted as a member, so they could still change data. | Every request checks that the session's user still exists. |
| 2 | High (7.4) | **Changing or resetting a password didn't sign out other sessions** (another browser, a stolen cookie). | Sessions carry a fingerprint of the user's password hash; a new password ends every other session. The session that changed it stays signed in. |
| 3 | Medium (6.5) | **Password spraying.** The lockout counted per username only, so an attacker could try a few passwords against many accounts without ever being blocked. | Also limited per address: 20 failed sign-ins in 5 minutes. |
| 4 | Medium (6.8) | **SSRF through the AI provider address.** An admin could point the custom AI server at the cloud metadata service (169.254.169.254) or the server's internal network. On a hosted service the client's admin isn't the operator. | Metadata and link-local addresses are always refused, after resolving the host name. Once the app is served over HTTPS, private and loopback addresses are refused too, unless `GRC_ALLOW_PRIVATE_AI_URL=1`. |
| 5 | Medium (6.3) | **SSRF through connector redirects.** A self-hosted GitLab URL was checked to be public, but redirects were then followed without a check, so a hostile server could bounce the request into the private network. | Every redirect is checked again: it must be HTTPS to a public address. |
| 6 | Medium (4.6) | **Outdated pip and setuptools in the app image** (pip 25.0.1: CVE-2025-8869, CVE-2026-1703, CVE-2026-3219, CVE-2026-6357, CVE-2026-8643, CVE-2026-13346; setuptools 79: CVE-2026-59890). Build tools, not used by the running app, but present in the image. | The image upgrades to pip ≥ 26.2 and setuptools ≥ 83 (tested: pip 26.2.1, setuptools 84.0.0). |
| 7 | Low (3.7) | **OS packages in the base images aren't patched after the base image is published.** | The app image runs `apt-get upgrade` and the website image runs `apk upgrade` at every build. |
| 8 | Low (3.1) | **No Content-Security-Policy on the website** (the app had one). | Caddy sends a CSP for the website: only this site's scripts, styles, images and connections; no framing; forms only to this site or email. |
| 9 | Info (0.0) | **Server software disclosed** in `Via: 1.1 Caddy` and `x-nextjs-*` headers. | Removed. |
| 10 | Info (0.0) | Bandit flagged SHA1. Both uses are non-security (a version fingerprint and a schema name). | Marked `usedforsecurity=False`. |

## Checked and found secure

- **Dependencies:** 0 known vulnerabilities in the app's 76 runtime packages or the website's npm packages.
- **Secrets:** none in either repository or its history. Hits were placeholders, AWS's documented example key, test values and the public demo password.
- **SQL injection:** all 23 Bandit warnings are false positives. Only column/table names written in the code are put into SQL; every value is a bound parameter.
- **XSS:** user input is escaped everywhere (tested with `<script>` and `onerror` payloads). AI chat Markdown escapes raw HTML and drops `javascript:` and `data:` links.
- **TLS and headers:** TLS 1.0 and 1.1 refused; HSTS; app CSP with `frame-ancestors 'none'`; `nosniff`; `X-Frame-Options: DENY`.
- **Cookies:** the session cookie is `Secure`, `HttpOnly` and `SameSite=Strict`.
- **Login:**
  - the same message and timing for unknown users and wrong passwords (dummy hash)
  - lockout after 5 failures
  - a new session at sign-in (no session fixation)
- **CSRF:** a token is required on every form; the API (`/mcp`) uses a bearer key and an origin check.
- **Access control:**
  - no POST works without login
  - viewers can only change their own account, notifications and API keys
  - members are refused admin actions (AI provider, team, roles)
- **Other web checks:**
  - no open redirect after login
  - no path traversal on `/static`
  - `/docs`, `/openapi.json`, `.env` and `.git` are not served
  - `TRACE` is refused
- **Uploads:**
  - HTML and SVG are refused
  - content must match the extension
  - 10 MB limit
  - file names are cleaned and files are stored under random names
  - downloads come as attachments with `nosniff`

## Recommendations (not code changes)

1. Add the four `DEPLOY_*` secrets so fixes reach the server on merge, and redeploy now.
2. In Oracle Cloud, allow SSH (port 22) only from your own IP address, and keep Ubuntu
   patched (`sudo apt update && sudo apt upgrade`, or enable `unattended-upgrades`).
3. Turn on GitHub's Dependabot alerts and secret scanning for both repositories.
4. Back up the `grc-pg` and `grc-data` volumes off the server regularly.
5. Repeat this review after major changes, and once a year.
