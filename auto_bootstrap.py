#!/usr/bin/env python3
"""
Auto-bootstrap: create Telegram sessions using free SMS receivers.
Usage: python auto_bootstrap.py
"""
import asyncio
import logging
import re
import sys
import time
from pathlib import Path

import urllib.request
import yaml

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    PhoneNumberInvalidError,
    PhoneNumberBannedError,
    PhoneNumberUnoccupiedError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("auto_bootstrap")

# List of free SMS receiver services to try
SMS_SERVICES = [
    {
        "name": "receive-sms-online.info",
        "url": "https://receive-sms-online.info",
        # Extract numbers from main page
        "extract_numbers": lambda html: re.findall(r'\+4[67]\d{8,10}', html),
        # Build the URL for a specific number's messages
        "message_url": lambda num: f"https://receive-sms-online.info/{num.replace('+', '')}",
        # Extract SMS messages containing codes
        "extract_code": lambda html, sender="Telegram": _extract_code_from_html(html, sender),
    },
]


def _extract_code_from_html(html, sender_hint="Telegram"):
    """Extract verification code from SMS page HTML."""
    # Look for common code patterns
    patterns = [
        r'(?:code|Code|CODE)[:\s]*(\d{4,8})',
        r'(?:code is|Code is)[:\s]*(\d{4,8})',
        r'(\d{4,8})\s*(?:is your|is the)',
        r'(\d{5,6})',  # Telegram codes are usually 5 digits
        r'>(\d{5})<',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, html, re.IGNORECASE)
        for match in matches:
            code = match if isinstance(match, str) else match[0]
            if code.isdigit() and 4 <= len(code) <= 8:
                # Filter out non-code numbers (phone numbers, timestamps, etc.)
                # Heuristic: code should be surrounded by code-like context
                context_pattern = re.compile(
                    r'(?:code|Code|CODE|login|Login|verif|Verif|otp|OTP|'
                    r'Telegram|telegram|confirm).{0,30}' + code,
                    re.IGNORECASE
                )
                if context_pattern.search(html):
                    return code
    return None


def fetch_page(url, timeout=15):
    """Fetch a web page."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.read().decode("utf-8", errors="replace")


def get_free_number():
    """Get a free phone number from SMS receiver services."""
    for service in SMS_SERVICES:
        try:
            logger.info(f"Trying {service['name']}...")
            html = fetch_page(service["url"])
            numbers = service["extract_numbers"](html)
            if numbers:
                # Try to find a number that works for Telegram
                for num in numbers[:5]:
                    logger.info(f"Found number: {num}")
                    return service, num
        except Exception as e:
            logger.warning(f"{service['name']} failed: {e}")
    return None, None


def poll_for_code(service, number, timeout_sec=120, interval=5):
    """Poll the SMS receiver page for a verification code."""
    url = service["message_url"](number)
    deadline = time.time() + timeout_sec
    seen_codes = set()

    while time.time() < deadline:
        try:
            html = fetch_page(url)
            code = service["extract_code"](html)
            if code and code not in seen_codes:
                seen_codes.add(code)
                logger.info(f"Found code: {code}")
                return code
        except Exception as e:
            logger.debug(f"Poll error: {e}")

        time.sleep(interval)

    logger.warning(f"No code received for {number} after {timeout_sec}s")
    return None


async def create_session(api_id, api_hash, phone, service, number_info):
    """Create a Telegram session using the free number."""
    session = StringSession()
    client = TelegramClient(session, api_id, api_hash)
    await client.connect()

    try:
        logger.info(f"Sending code to {phone}...")
        result = await client.send_code_request(phone)
        logger.info(f"Code sent via {result.type}")

        # Poll for the code
        code = poll_for_code(service, number_info)
        if not code:
            logger.error(f"No code received for {phone}")
            await client.disconnect()
            return None

        logger.info(f"Signing in with code: {code}")
        try:
            user = await client.sign_in(phone, code, phone_code_hash=result.phone_code_hash)
        except SessionPasswordNeededError:
            logger.warning("2FA required — cannot auto-bootstrap this number")
            await client.disconnect()
            return None
        except PhoneNumberUnoccupiedError:
            # Sign up new account
            logger.info("New account — signing up")
            user = await client.sign_up(code, first_name="Test", last_name="User")

        logger.info(f"Authorized! user_id={user.id}")
        session_str = session.save()
        await client.disconnect()
        return session_str

    except FloodWaitError as e:
        logger.error(f"FLOOD_WAIT {e.seconds}s — cannot auto-bootstrap right now")
        await client.disconnect()
        return None
    except (PhoneNumberInvalidError, PhoneNumberBannedError) as e:
        logger.error(f"Number invalid/banned: {e}")
        await client.disconnect()
        return None
    except Exception as e:
        logger.error(f"Unexpected error: {type(e).__name__}: {e}")
        await client.disconnect()
        return None


async def main():
    config_path = Path("config.yaml")
    if not config_path.exists():
        logger.error("config.yaml not found")
        return 1

    config = yaml.safe_load(config_path.read_text()) or {}
    api_id = config.get("api_id", 0)
    api_hash = config.get("api_hash", "")

    if not api_id or not api_hash:
        logger.error("api_id and api_hash required in config.yaml")
        return 1

    logger.info("=== Auto-bootstrap: Creating Telegram sessions ===")
    logger.info(f"API ID: {api_id}")

    # Get a free number
    service, number = get_free_number()
    if not service:
        logger.error("Could not get a free phone number")
        logger.error("Manual fallback: run 'python bootstrap.py' and enter codes manually")
        return 1

    # Create session
    session_str = await create_session(api_id, api_hash, number, service, number)
    if not session_str:
        logger.error("Failed to create session")
        return 1

    # Save session
    sessions_dir = Path("sessions")
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions_dir / f"{number.replace('+', '_')}.session"

    # Write as Telethon session
    client = TelegramClient(str(session_file), api_id, api_hash)
    from telethon.sessions import StringSession
    client.session = StringSession(session_str)
    await client.connect()
    if await client.is_user_authorized():
        logger.info(f"Session saved: {session_file}")
    await client.disconnect()

    logger.info("=== Success! Session created ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
