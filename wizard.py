"""
Startup wizard — collects phone, auth, destination, API keys.
Shows summary, saves config, confirms launch.
Handles first-time Telegram auth (code + 2FA).
"""
import asyncio
import getpass
import sys
from pathlib import Path
from typing import Optional

import yaml

SECRETS_FILE = ".secrets.yaml"
SESSIONS_DIR = "sessions"


def load_secrets() -> dict:
    path = Path(SECRETS_FILE)
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def save_secrets(data: dict):
    existing = load_secrets()
    existing.update(data)
    with open(SECRETS_FILE, "w") as f:
        yaml.dump(existing, f, default_flow_style=False)
    Path(SECRETS_FILE).chmod(0o600)


def load_targets_file(path: str = "targets.txt") -> list:
    p = Path(path)
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")]


def input_text(label: str, default: str = "") -> str:
    value = input(f"  {label}: ").strip()
    return value if value else default


def input_secret(label: str) -> str:
    return getpass.getpass(f"  {label}: ").strip()


async def auth_telegram(api_id: int, api_hash: str, phone: str) -> Optional[str]:
    """Authenticate a phone number with Telegram. Returns session name or None."""
    from telethon import TelegramClient
    from telethon.errors import (
        SessionPasswordNeededError,
        FloodWaitError,
        PhoneNumberInvalidError,
        PhoneNumberBannedError,
    )

    session_name = phone.replace("+", "_").replace(" ", "")
    session_path = str(Path(SESSIONS_DIR) / session_name)
    Path(SESSIONS_DIR).mkdir(parents=True, exist_ok=True)

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"\n  ✓ Already authorized as {me.first_name} (@{me.username})")
            await client.disconnect()
            return session_name

        result = await client.send_code_request(phone)
        print(f"\n  Code sent via {result.type} to {phone}")
        code = input("  Enter verification code: ").strip()

        try:
            await client.sign_in(phone, code, phone_code_hash=result.phone_code_hash)
        except SessionPasswordNeededError:
            password = getpass.getpass("  2FA password required: ").strip()
            await client.sign_in(password=password)

        me = await client.get_me()
        print(f"  ✓ Authorized as {me.first_name} (@{me.username})")
        await client.disconnect()
        return session_name

    except FloodWaitError as e:
        print(f"\n  ✗ Rate limited — wait {e.seconds}s and try again")
        await client.disconnect()
        return None
    except (PhoneNumberInvalidError, PhoneNumberBannedError) as e:
        print(f"\n  ✗ Phone number rejected: {e}")
        await client.disconnect()
        return None
    except Exception as e:
        print(f"\n  ✗ Auth failed: {type(e).__name__}: {e}")
        await client.disconnect()
        return None


async def wizard(config: dict) -> bool:
    """Run startup wizard. Returns True if user confirms launch."""
    print()
    print("╔══════════════════════════════════════════╗")
    print("║        ⚡ TG Tool — Setup Wizard         ║")
    print("╚══════════════════════════════════════════╝")
    print()

    # ── Step 1: API Credentials ──
    api_id = config.get("api_id", 0)
    api_hash = config.get("api_hash", "")

    if api_id == 0 or api_hash in ("YOUR_API_HASH", ""):
        print("┌─ Telegram API Credentials ───────────────┐")
        print("  Get yours at: https://my.telegram.org/apps")
        api_id = input_text("API ID").strip()
        api_hash = input_text("API Hash").strip()
        if not api_id or not api_hash:
            print("  ✗ API credentials required\n")
            return False
        config["api_id"] = int(api_id)
        config["api_hash"] = api_hash
        print("  ✓ Saved to config.yaml")
        print("└──────────────────────────────────────────┘")
        print()

    # ── Step 2: Phone(s) + Auth ──
    secrets = load_secrets()
    saved_phones = secrets.get("phones", [])
    session_names = []

    max_sessions = config.get("max_sessions", 5)

    while True:
        remaining = max_sessions - len(session_names)
        if remaining <= 0:
            print(f"  Session pool full ({max_sessions} max)")
            break

        print(f"┌─ Telegram Account ({len(session_names)}/{max_sessions} used) {'─' * 24}┐")

        if saved_phones and len(session_names) < len(saved_phones):
            phone = saved_phones[len(session_names)]
            masked = phone[:4] + "****" + phone[-4:] if len(phone) > 8 else phone
            print(f"  Stored: {masked}")
            use = input_text("Use this number? (Y/n)", "y").lower()
            if use == "n":
                phone = input_text("Phone (+XXXXXXXXXXX)")
        else:
            phone = input_text("Phone (+XXXXXXXXXXX)")

        if not phone:
            print("  Skipped")
            print("└──────────────────────────────────────────┘")
            break

        print("\n  Authenticating...")
        name = await auth_telegram(int(config["api_id"]), config["api_hash"], phone)

        if name is None:
            print("  ✗ Auth failed — skipping this number")
            print("└──────────────────────────────────────────┘")
            if not session_names:
                print("\n  ✗ No accounts authorized. Cannot continue.\n")
                return False
        else:
            session_names.append(name)
            if phone not in saved_phones:
                saved_phones.append(phone)
            print("└──────────────────────────────────────────┘")

        if len(session_names) >= max_sessions:
            print(f"\n  Session pool full ({max_sessions}/{max_sessions})\n")
            break

        if len(session_names) >= 1:
            more = input_text("\nAdd another account? (y/N)", "n").lower()
            if more not in ("y", "yes"):
                break

    if not session_names:
        print("  ✗ Phone number required\n")
        return False

    save_secrets({"phones": saved_phones})
    print()

    # ── Step 3: Destination ──
    print("┌─ Destination Channel ────────────────────┐")
    dest_current = config.get("dest_channel", "me")
    if dest_current and dest_current != "me":
        print(f"  Current: {dest_current}")
    dest = input_text(
        "Destination (@handle, invite link, or 'me')",
        default=dest_current,
    )
    print("└──────────────────────────────────────────┘")
    print()

    # ── Step 4: 2Captcha ──
    print("┌─ 2Captcha API Key (optional) ────────────┐")
    captcha_current = secrets.get("2captcha_key", "")
    captcha_key = ""
    if captcha_current:
        masked = captcha_current[:4] + "****" + captcha_current[-4:] if len(captcha_current) > 8 else "****"
        print(f"  Stored: {masked}")
        use = input_text("Use stored key? (Y/n)", "y").lower()
        captcha_key = captcha_current if use != "n" else input_secret("2Captcha API key")
    else:
        captcha_key = input_secret("2Captcha API key (Enter to skip)")
    if captcha_key:
        save_secrets({"2captcha_key": captcha_key})
    print("└──────────────────────────────────────────┘")
    print()

    # ── Step 5: Webshare ──
    print("┌─ Webshare.io Proxy Key (optional) ───────┐")
    webshare_current = secrets.get("webshare_key", "")
    webshare_key = ""
    if webshare_current:
        masked = webshare_current[:4] + "****" + webshare_current[-4:] if len(webshare_current) > 8 else "****"
        print(f"  Stored: {masked}")
        use = input_text("Use stored key? (Y/n)", "y").lower()
        webshare_key = webshare_current if use != "n" else input_secret("Webshare.io API key")
    else:
        webshare_key = input_secret("Webshare.io API key (Enter to skip)")
    if webshare_key:
        save_secrets({"webshare_key": webshare_key})
    print("└──────────────────────────────────────────┘")
    print()

    # ── Summary ──
    targets = load_targets_file(config.get("targets", "targets.txt"))
    targets_display = ", ".join(targets[:3])
    if len(targets) > 3:
        targets_display += f" +{len(targets) - 3} more"

    print("╔══════════════════════════════════════════╗")
    print("║             Launch Summary               ║")
    print("╠══════════════════════════════════════════╣")
    print(f"  Accounts    : {len(session_names)}/{config.get('max_sessions', 5)} ({', '.join(session_names)})")
    print(f"  Sessions    : {config.get('max_sessions', 5)} max")
    print(f"  Targets     : {targets_display or 'none'}")
    print(f"  Destination : {dest}")
    print(f"  2Captcha    : {'✓' if captcha_key else '✗'}")
    print(f"  Webshare    : {'✓' if webshare_key else '✗'}")
    print(f"  Scrape delay: {config.get('scrape_delay_ms', 2000)}ms")
    print(f"  Add delay   : {config.get('add_delay_ms', 5000)}ms")
    print("╚══════════════════════════════════════════╝")
    print()

    answer = input_text("Start now? (Y/n)", "y").lower()
    if answer in ("", "y", "yes"):
        config["dest_channel"] = dest
        with open("config.yaml", "w") as f:
            yaml.dump(config, f, default_flow_style=False)
        return True
    else:
        print("\n  Aborted. Edit targets.txt and run again.\n")
        return False


if __name__ == "__main__":
    config = yaml.safe_load(Path("config.yaml").read_text()) if Path("config.yaml").exists() else {}
    # Handle placeholder
    if not str(config.get("api_id", "")).isdigit():
        config["api_id"] = 0
    asyncio.run(wizard(config))
