# CD3 Leaderboard Hub

Local-first multi-leaderboard manager by AWS Student Builder Group.
Flask + SQLite. No cloud, no build step.

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

- Viewer: http://127.0.0.1:5000/
- Admin: http://127.0.0.1:5000/admin — default password `admin123` (change in Settings)

## Run with Docker

```bash
docker compose up -d --build
# Viewer at http://localhost/  (host port 80 -> container 5000)
```

Data persists in the `leaderboard-data` volume across rebuilds.
`SECRET_KEY` / `ADMIN_PASSWORD` can be set via environment (see `docker-compose.yml`).
`ADMIN_PASSWORD` only applies on very first boot; afterwards change it in Settings.

## Deploy on EC2 free tier (t3.micro, Amazon Linux 2023)

1. **Launch instance**: t3.micro, Amazon Linux 2023, 8 GB gp3. In the security
   group open inbound **port 80** (HTTP) and **port 22** (SSH, your IP only).
2. **SSH in and install Docker**:
   ```bash
   sudo dnf update -y
   sudo dnf install -y docker git
   sudo systemctl enable --now docker
   sudo usermod -aG docker ec2-user
   # log out and back in so the docker group applies
   ```
3. **Start the app**:
   ```bash
   git clone <your-repo-url> && cd cd3-leaderboard
   cp deploy/.env.example .env   # then fill in SECRET_KEY + ADMIN_PASSWORD
   docker compose up -d --build
   ```
4. Open `http://<ec2-public-ip-or-dns>/`. Log in at `/admin` and change the
   password in Settings if you kept the default.

## Custom domain + HTTPS (nginx + Let's Encrypt)

`deploy/` holds the production front-end kit (mirrors the live EC2 setup):

- `deploy/cd3.conf` — nginx reverse proxy (`cd3.awsatria.tech` → app on loopback)
- `deploy/.env.example` — copy to `.env`; `APP_BIND=127.0.0.1 APP_PORT=5000`
  keeps the app reachable only through nginx

Steps on the instance:

1. Point DNS at the server: `A cd3.awsatria.tech → <elastic-ip>`, open **443** in the security group.
2. Install and wire nginx:
   ```bash
   sudo dnf install -y nginx
   sudo cp deploy/cd3.conf /etc/nginx/conf.d/cd3.conf
   sudo nginx -t && sudo systemctl enable --now nginx
   ```
3. Issue the certificate (needs DNS live first):
   ```bash
   sudo dnf install -y certbot python3-certbot-nginx
   sudo certbot --nginx -d cd3.awsatria.tech
   ```
   Renewals are automatic via the certbot systemd timer.

**Update later**: `git pull && docker compose up -d --build` (volume keeps the DB).
**Back up the DB**: `docker run --rm -v cd3-leaderboard_leaderboard-data:/d -v "$PWD":/b alpine cp /d/leaderboard.db /b/backup.db`

## Features

**Viewer (`/`)** — display only
- Unified view: every event as a card, Top 10 each, auto-refresh 15s.
  Multi-metric boards show the primary score with the rest underneath.
- Dropdown filter → single board with player search + pagination (10/page, numbered).
  Single boards open with a visual top-3 podium, then ranks 4+ as a table.
- Refresh button. No admin links, no exports, no registration here.
- QR share button: opens a scannable code for the board URL, plus copy-link,
  so the crowd can follow along on their phones.
- Light/dark theme switcher in the header (sun/moon icons). Choice persists
  in the browser; first visit follows the OS setting, defaulting to dark.

**Design** — minimal instrument-panel language: Space Grotesk display,
JetBrains Mono for all data/labels/scores, Inter body, single amber accent,
Lucide SVG icons only (no emojis anywhere, including PDF exports).

**Admin (`/admin`, password-protected)**
- Events support **1–3 metrics each** (e.g. Score DESC plus Time ASC as tiebreak),
  every metric with its own name, unit and sort direction; list order sets rank priority
- **Standings**: per-event ranked board with Name, USN, tappable phone numbers
  and scores, plus a top-3 winners strip, for calling winners on the spot
- **New registration**: Name + USN + Phone Number + dynamic score metric per event
  (e.g. *Time (sec), lower wins* vs *Score (pts), higher wins*), instant rank feedback
- Create/edit/delete events: name + **Lucide SVG icon picker** (no emojis) + editable
  score metric name + unit + ranking direction (highest/lowest wins) + description
- Entries browser with delete, per-event filter
- **Reset** per board → snapshots current scores to History, then clears.
  Reset dialog offers one-click PDF/JSON export first.
- **History**: every reset (manual or auto) archived with timestamp + reason;
  view, export snapshot to PDF/JSON, delete
- **Hourly auto-reset**: toggle + interval in hours (e.g. 1h). Stale boards are
  auto-archived + cleared on next request
- Seed demo data (4 events × 12 scores) for testing

## Data

`leaderboard.db` (SQLite, created on first run):

- `events(id, name, icon, score_label, unit, sort_dir, description, created_at, last_reset_at)`
  (`score_*` mirrors the primary metric)
- `metrics(id, event_id, label, unit, sort_dir, position)` — 1–3 per event
- `entries(id, event_id, player_name, usn, phone, score, values_json, created_at)`
- `snapshots(id, event_id, event_name, score_label, unit, taken_at, reason, entry_count, data_json)`
- `settings(key, value)` — password hash, auto-reset flags

## PDF exports

Generated server-side with ReportLab: title block, per-board tables (rank,
player, score+unit, date), gold/silver/bronze podium rows, page footers.
