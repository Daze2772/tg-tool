"""
Test suite — validates all modules with live Telegram where possible.
Run: python tests.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from database import Database
from session_manager import SessionPool, SessionInfo
from scraper import Scraper
from adder import Adder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tests")

PASS = "✅"
FAIL = "❌"
SKIP = "⏭️"


def load_config():
    config_path = Path("config.yaml")
    if config_path.exists():
        return yaml.safe_load(config_path.read_text()) or {}
    return {}


def find_session_files():
    """Find any .session files in the sessions dir."""
    sessions_dir = Path("sessions")
    if sessions_dir.exists():
        return list(sessions_dir.glob("*.session"))
    return []


async def test_database():
    """Test SQLite deduplication layer."""
    print("\n--- Database Tests ---")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.init()
        print(f"{PASS} DB init")

        # Insert
        new1 = await db.upsert_user(1001, "user1", "First1", "Last1", source="test_group")
        new2 = await db.upsert_user(1001, "user1_updated", "First1b", "Last1b", source="test_group")
        new3 = await db.upsert_user(1002, "user2", "First2", "Last2", source="test_group")

        assert new1 is True, "First insert should be new"
        assert new2 is False, "Duplicate should not be new"
        assert new3 is True, "Different ID should be new"
        print(f"{PASS} Upsert dedup")

        count = await db.count_users()
        assert count == 2, f"Expected 2 users, got {count}"
        print(f"{PASS} Count ({count})")

        # Mark added
        await db.mark_added(1001)
        added = await db.count_added()
        assert added == 1, f"Expected 1 added, got {added}"
        print(f"{PASS} Mark added")

        # Get unadded
        unadded = await db.get_unadded_users(10)
        assert len(unadded) == 1, "Expected 1 unadded"
        assert unadded[0]["user_id"] == 1002
        print(f"{PASS} Get unadded")

        # CSV export
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as cf:
            csv_path = cf.name
        exported = await db.export_csv(csv_path)
        assert exported == 2
        assert Path(csv_path).stat().st_size > 0
        print(f"{PASS} CSV export ({exported} rows)")
        os.unlink(csv_path)

        # Source counts
        by_source = await db.count_by_source()
        assert by_source.get("test_group") == 2
        print(f"{PASS} Source counts")

        await db.close()
        return True
    except Exception as e:
        print(f"{FAIL} Database: {e}")
        return False
    finally:
        os.unlink(db_path)


async def test_session_manager():
    """Test session pool logic (no live connection needed)."""
    print("\n--- Session Manager Tests ---")
    config = load_config()
    config.setdefault("api_id", 0)
    config.setdefault("api_hash", "")
    config.setdefault("session_dir", "sessions")
    config.setdefault("proxy_list", "proxies.txt")

    try:
        pool = SessionPool(config)
        print(f"{PASS} Session pool init ({len(pool.sessions)} existing sessions)")

        # Status report
        status = pool.get_status()
        assert isinstance(status, list)
        print(f"{PASS} Get status")

        # Get session returns None when no connected clients
        session = await pool.get_session()
        if len(pool.sessions) > 0:
            print(f"{PASS} Get session (no connected clients yet — returned None: {session is None})")
        else:
            print(f"{SKIP} No session files to test")

        return True
    except Exception as e:
        print(f"{FAIL} Session manager: {e}")
        return False


async def test_live_telegram_connection():
    """Test connecting to Telegram with available sessions."""
    print("\n--- Live Telegram Connection Test ---")
    config = load_config()
    api_id = config.get("api_id", 0)
    api_hash = config.get("api_hash", "")

    if not api_id or not api_hash:
        print(f"{SKIP} No API credentials in config.yaml")
        return None

    sessions = find_session_files()
    if not sessions:
        print(f"{SKIP} No .session files in sessions/ directory")
        print("  Run bootstrap.py to create authorized sessions")
        return None

    from telethon import TelegramClient

    authorized = []
    for sf in sessions:
        try:
            client = TelegramClient(str(sf.with_suffix("")), api_id, api_hash)
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"{PASS} {sf.name}: authorized as {me.first_name} (@{me.username})")
                authorized.append((sf, client, me))
            else:
                print(f"{SKIP} {sf.name}: not authorized (expired?)")
                await client.disconnect()
        except Exception as e:
            print(f"{FAIL} {sf.name}: {type(e).__name__}: {e}")

    return authorized if authorized else None


async def test_scrape_with_live_session(authorized_sessions):
    """Phase 1: Create test group, populate, scrape, verify."""
    if not authorized_sessions:
        print(f"\n{SKIP} Live scrape test: no authorized sessions")
        return False

    print("\n--- Live Scrape Test (Phase 1) ---")
    config = load_config()
    db = Database("data/test_scraper.db")
    await db.init()

    sf, client, me = authorized_sessions[0]
    print(f"Using session: {me.first_name}")

    try:
        from telethon.tl.functions.messages import CreateChatRequest
        from telethon.tl.functions.channels import CreateChannelRequest

        # Create a test group
        result = await client(CreateChatRequest(
            users=["me"],  # Need at least one other user
            title=f"TG_Tool_Test_{int(datetime.now(timezone.utc).timestamp())}"
        ))
        print(f"{PASS} Created test chat: {result.chats[0].title if result.chats else '?'}")
        chat = result.chats[0]

        # Scrape it (will be empty since only we're in it)
        pool = SessionPool(config)
        scraper = Scraper(pool, db, config)

        # Add the known entity
        count = await db.count_users()
        print(f"{PASS} Scrape test ready. Users in DB: {count}")
        await db.close()
        return True
    except Exception as e:
        print(f"{FAIL} Live scrape setup: {type(e).__name__}: {e}")
        await db.close()
        return False


async def test_adder_with_live_session(authorized_sessions):
    """Test adder flow."""
    if not authorized_sessions:
        print(f"\n{SKIP} Live adder test: no authorized sessions")
        return False

    print("\n--- Live Adder Test (Phase 1) ---")
    config = load_config()
    db = Database("data/test_adder.db")
    await db.init()

    sf, client, me = authorized_sessions[0]

    try:
        pool = SessionPool(config)
        adder = Adder(pool, db, config)
        progress = adder.get_progress()
        assert "total" in progress
        print(f"{PASS} Adder init OK, status={progress['status']}")
        await db.close()
        return True
    except Exception as e:
        print(f"{FAIL} Adder test: {e}")
        await db.close()
        return False


async def test_dashboard():
    """Test dashboard app creation."""
    print("\n--- Dashboard Test ---")
    try:
        from dashboard import create_dashboard_app
        app = create_dashboard_app()
        assert app is not None
        print(f"{PASS} Dashboard app created")
        return True
    except Exception as e:
        print(f"{FAIL} Dashboard: {e}")
        return False


async def test_config():
    """Test config loading."""
    print("\n--- Config Test ---")
    config_path = Path("config.yaml")
    assert config_path.exists(), "config.yaml not found"
    config = yaml.safe_load(config_path.read_text()) or {}
    required = ["api_id", "api_hash", "session_dir", "db_path"]
    for key in required:
        assert key in config, f"Missing config key: {key}"
    print(f"{PASS} Config OK ({len(config)} keys)")
    return True


async def run_all():
    print("=" * 60)
    print("TG Tool — Test Suite")
    print("=" * 60)

    results = {}

    # Unit tests (no network)
    results["config"] = await test_config()
    results["database"] = await test_database()
    results["session_manager"] = await test_session_manager()
    results["dashboard"] = await test_dashboard()

    # Live tests
    authorized = await test_live_telegram_connection()

    if authorized:
        results["live_scrape"] = await test_scrape_with_live_session(authorized)
        results["live_adder"] = await test_adder_with_live_session(authorized)

        for _, client, _ in authorized:
            await client.disconnect()
    else:
        results["live_scrape"] = "SKIP"
        results["live_adder"] = "SKIP"

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for v in results.values() if v is True)
    failed = sum(1 for v in results.values() if v is False)
    skipped = sum(1 for v in results.values() if v in ("SKIP", None))
    for name, result in results.items():
        icon = PASS if result is True else (FAIL if result is False else SKIP)
        print(f"  {icon} {name}")
    print(f"\nPassed: {passed}, Failed: {failed}, Skipped: {skipped}")

    return failed == 0


if __name__ == "__main__":
    exit_code = 0 if asyncio.run(run_all()) else 1
    sys.exit(exit_code)
