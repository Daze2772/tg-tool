## TG Tool — Full Handoff

You're picking up a Telegram member scraper + channel adder. Python, Telethon, rich TUI. Fully working scraper, problematic adder.

**Repo:** https://github.com/Daze2772/tg-tool
**Branch:** main
**Python:** 3.7.5 (project uses `typing.List` not `list[]`, watch for this)
**Env:** `source venv/bin/activate`

---

### Architecture (what each file does)

```
tg-tool/
├── main.py              # Entry point → wizard → connects sessions → scraper → adder → TUI
├── wizard.py            # Startup wizard: API keys, phone auth, destination, speed mode, manage accounts
├── tui.py               # Rich terminal UI (live panels: scraper, adder, sessions, rate limits, DB)
├── session_manager.py   # Multi-session pool: load .session files, health scoring, quarantine, proxy binding
├── scraper.py           # GetParticipantsRequest pagination, user extraction, dedup passthrough
├── adder.py             # InviteToChannelRequest with retry, PEER_FLOOD backoff, session rotation, verify
├── database.py          # SQLite: user_id PK, source tracking, add status, CSV export
├── tests.py             # Unit tests (DB, sessions, dashboard) + live Telegram tests (needs auth)
├── config.yaml          # api_id, api_hash, delays, targets path, dest_channel
├── targets.txt          # One Telegram group/channel per line (@handle or invite link)
├── .secrets.yaml        # phone numbers, 2captcha key, webshare key (gitignored)
└── sessions/            # Telethon .session files (auto-discovered, authorized ones auto-connect)
```

---

### How it works (happy path)

1. `python main.py` → wizard asks for API ID/Hash (first time) → phone number → sends Telegram code → you enter it → 2FA if needed → session saved
2. Asks destination channel, speed mode (Lightning/Normal/Safe/Custom), optional 2captcha/webshare keys
3. Shows summary → "Start now?" → scraper fires → adder fires → TUI shows live progress
4. Ctrl+C anytime, CSV auto-exports

---

### Current state (June 2026)

**Scraper: ✅ Fully working**
- Paginates via GetParticipantsRequest (offset/limit, 200 per chunk)
- Handles private groups (needs membership), FLOOD_WAIT, resume/stop
- Successfully scraped 4,600+ users from two target groups

**Adder: ⚠️ Blocked by Telegram limits**
- Code is correct: auto-joins destination, self-tests permissions, retries PEER_FLOOD with backoff, rotates sessions, verifies adds
- BUT Telegram caps adds at ~30-50 per account per day. Fresh accounts hit PEER_FLOOD on the first add
- Two USA accounts connected and cycling, but neither can push more than a handful per day
- `add_user()` has a verify step after InviteToChannelRequest — checks if the user actually appeared in the channel. Ghost adds are caught and marked "GHOST_ADD"

**Known cause of zero adds:**
The destination channel `https://t.me/BinSockCC2` likely has "Who can add members" set to "Only admins." The bot accounts aren't admins, so Telegram silently rejects every invite (or returns PEER_FLOOD as anti-spam). Solution: use a channel where the bot IS admin, or make the bot admin in the destination.

**Session rotation:** Switches every 5 successful adds or immediately on PEER_FLOOD.

**2Captcha / Webshare:** Keys are collected and stored but NOT wired into any code. No captcha solving or proxy rotation exists yet.

---

### Key config values

```yaml
scrape_delay_ms: 2000        # 2s between scrape batches (200 users each)
scrape_batch_size: 200       # Users per GetParticipantsRequest call
add_delay_ms: 30000          # Base delay between adds (Normal mode)
add_jitter_ms: 10000         # Random jitter on add delay
max_sessions: 5              # Max accounts in pool
```

Speed modes set these delays:
- Lightning: 500ms scrape / 5s add / 3s jitter
- Normal: 2s scrape / 30s add / 10s jitter  
- Safe: 3s scrape / 60s add / 15s jitter
- Custom: user types their own

---

### What needs work

1. **Adder actually landing users** — make bot admin in destination, or test with a channel the bot owns. The self-test in `adder.py run()` will tell you immediately if permissions are broken.

2. **Multiple account warmup** — new accounts need to "age" before Telegram trusts them to add members. You can't spin up a fresh number and immediately add 100 people.

3. **Wire up webshare.io** — `session_manager.py` already has proxy support (`_parse_proxy`, proxy binding). Webshare gives you rotating residential proxies. Add a `webshare.py` module that fetches the proxy list from their API, writes to `proxies.txt`, then each session connects through a different IP. This is the biggest impact change — different IPs per account = Telegram treats them as different people.

4. **Wire up 2captcha** — Telegram sometimes throws a captcha during auth or when adding. Solve it programmatically instead of failing. `wizard.py` already stores the key.

5. **Resumable adder state** — if the tool crashes, it should resume from where it left off. Currently `add_error` column tracks failures, but successful-but-unverified adds aren't distinguished from verified ones. Add a "verified" flag.

6. **Actually verify adds at scale** — the current verify step calls `get_participants` after every add, which itself counts toward rate limits. Find a lighter check or batch-verify.

7. **Python 3.7 compat** — no `list[dict]`, no `str | None`, no walrus operator. Use `typing.List`, `typing.Dict`, `typing.Optional`.

---

### Running locally

```bash
cd /Users/ryan/tg-tool
source venv/bin/activate
python main.py
```

Logs: `logs/tool.log`
DB: `data/scraper.db`
CSV exports: `data/export.csv`
Sessions: `sessions/*.session`

### Testing

```bash
python tests.py    # Unit tests (DB, sessions, TUI) + live Telegram if authorized
```

---

### Telethon version caveat

We're on Telethon 1.29.3 (last version supporting Python 3.7). Key API differences from modern Telethon:
- `get_participants()` has no `offset` param → use `GetParticipantsRequest` from `telethon.tl.functions.channels`
- `iter_participants()` exists but `limit` is total cap, not per-page
- `flood_sleep_threshold` doesn't exist → handle FLOOD_WAIT manually in except blocks
