# TG Tool — Telegram Member Scraper + Channel Adder

Scrape members from Telegram groups/channels → deduplicate into SQLite → add to your destination channel.
Session rotation, rate-limit resilience, live terminal UI.

## Quick Start

```bash
# 1. Install
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Get API credentials (one-time)
# Go to https://my.telegram.org/apps → create an app → copy api_id and api_hash

# 3. Add targets
echo "@target_group" >> targets.txt

# 4. Launch
python main.py
```

The wizard will walk you through everything: API keys, phone auth (Telegram code + 2FA), destination channel, optional 2Captcha/Webshare keys.

## What the wizard asks

| Step | Required | Stored in |
|------|----------|-----------|
| API ID + Hash | Yes (first time) | `config.yaml` |
| Phone number + auth code | Yes (first time) | `.secrets.yaml` |
| Destination channel | Yes | `config.yaml` |
| 2Captcha key | Optional | `.secrets.yaml` |
| Webshare.io key | Optional | `.secrets.yaml` |

Then a summary appears — confirm and the pipeline starts with a live terminal UI.

## Terminal UI

```
╔══════════════════════════════════════════════════════════╗
║ ⚡ TG Tool  │  Dashboard  │  14:32:05                    ║
╠══════════════════════════╦═══════════════════════════════╣
║ 👥 Database              ║ 📤 Adder                      ║
║ Total: 156  Added: 42    ║ ✅ 42  ❌ 3  ⏭️ 12           ║
╠══════════════════════════╣                               ║
║ 📡 Scraper               ╠═══════════════════════════════╣
║ @crypto_signals          ║ ⚠️ Rate Limit Events          ║
║ ████████░░ 82%  200/243  ║ 14:31 FLOOD_WAIT 8s user=... ║
║ @nft_alpha               ║                               ║
║ ██████████ 100%  done    ║                               ║
╠══════════════════════════╩═══════════════════════════════╣
║ 🔌 Sessions: aviciio [0.95] active  │  uses: 23  │ ...  ║
╚══════════════════════════════════════════════════════════╝
```

- **Scraper** — per-target progress bars with participant counts
- **Adder** — added / failed / skipped counts
- **Rate Limit Events** — FLOOD_WAIT, PEER_FLOOD with timestamps
- **Sessions** — health scores, quarantine status, usage stats
- **Database** — total scraped, added, remaining

Ctrl+C stops. CSV auto-exports to `data/export.csv` on completion.

## Configuration

All in `config.yaml` (the wizard fills most of it):

```yaml
api_id: YOUR_API_ID
api_hash: "YOUR_API_HASH"
session_dir: "sessions"
max_sessions: 5
proxy_list: "proxies.txt"
targets: "targets.txt"
dest_channel: "me"
scrape_delay_ms: 2000
add_delay_ms: 5000
add_jitter_ms: 3000
```

### Targets (`targets.txt`)

```
@crypto_signals
https://t.me/+abc123def456
nft_alpha
```

### Proxies (`proxies.txt` — optional)

```
socks5://user:pass@host:port
http://user:pass@host:port
```

## Project Layout

```
tg-tool/
├── main.py              # Orchestrator — wizard → scraper → adder → TUI
├── wizard.py            # Startup wizard (auth, destination, keys)
├── tui.py               # Rich terminal UI (live progress)
├── session_manager.py   # Multi-session pool, proxy binding, health scoring
├── scraper.py           # Member extraction (username, invite link, group ID)
├── database.py          # SQLite dedup (user_id PK, source tracking, CSV export)
├── adder.py             # Add users to destination with jittered rate limiting
├── tests.py             # Test suite (unit + live)
├── config.yaml          # All settings
├── targets.txt          # Groups/channels to scrape
├── proxies.txt          # Proxy list
├── requirements.txt     # Python deps
├── .secrets.yaml        # Phone, 2Captcha, Webshare keys (gitignored)
├── .gitignore
├── sessions/            # Persisted .session files
├── data/                # SQLite DB + CSV exports
└── logs/                # Structured logs
```

## Rate Limit Resilience

- **FLOOD_WAIT** — caught, exact duration slept, retried. Session health penalized.
- **PEER_FLOOD** — user skipped + logged, pipeline continues.
- **Session ban** — quarantined, remaining users reassigned.
- **Health scoring** — success up, failure/flood down. Quarantined at 0.0.

## Database Schema

```sql
users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    last_name     TEXT,
    phone         TEXT,
    source        TEXT,       -- source group
    scraped_at    TEXT,       -- ISO timestamp
    added_to_dest INTEGER,    -- 1 = added
    added_at      TEXT,
    add_error     TEXT
)
```

## Troubleshooting

**"API credentials required"** — go to https://my.telegram.org/apps, log in, create an app, copy the ID and hash.

**"Phone number rejected"** — make sure it's in international format: `+12345678901`.

**FLOOD_WAIT during auth** — wait the listed duration, try again.

**Private groups** — you must be a member. Join first, then scrape.

**All sessions quarantined** — check `logs/tool.log` for the ban reason. Add more sessions via the wizard or `bootstrap.py`.
