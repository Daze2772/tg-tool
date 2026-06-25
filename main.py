"""
Main orchestrator — startup wizard → scraper → adder → TUI dashboard.
"""
import asyncio
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import List

import yaml

from session_manager import SessionPool
from database import Database
from scraper import Scraper
from adder import Adder
from tui import TuiDashboard
from wizard import wizard

logger = logging.getLogger("main")


def load_config(path: str = "config.yaml") -> dict:
    """Load YAML config with env var overrides."""
    import os

    config = {}
    config_path = Path(path)
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

    for key in [
        "api_id", "api_hash", "session_dir", "max_sessions",
        "proxy_list", "dest_channel", "db_path",
        "scrape_delay_ms", "scrape_batch_size",
        "add_delay_ms", "add_batch_size", "add_jitter_ms",
        "log_level", "log_file",
    ]:
        env_key = f"TG_{key.upper()}"
        if env_key in os.environ:
            val = os.environ[env_key]
            if key.endswith("_ms") or key.endswith("_port") or key == "max_sessions":
                val = int(val)
            elif key == "api_id":
                val = int(val)
            config[key] = val

    config["api_id"] = int(config.get("api_id", 0)) if str(config.get("api_id", "")).isdigit() else 0
    config["max_sessions"] = int(config.get("max_sessions", 5))
    config["scrape_delay_ms"] = int(config.get("scrape_delay_ms", 2000))
    config["scrape_batch_size"] = int(config.get("scrape_batch_size", 100))
    config["add_delay_ms"] = int(config.get("add_delay_ms", 5000))
    config["add_batch_size"] = int(config.get("add_batch_size", 50))
    config["add_jitter_ms"] = int(config.get("add_jitter_ms", 3000))
    return config


def load_targets(path: str) -> List[str]:
    """Load target usernames/links from file."""
    target_path = Path(path)
    if not target_path.exists():
        logger.warning(f"Targets file not found: {path}")
        return []
    return [l.strip() for l in target_path.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def setup_logging(config: dict):
    """File-only logging when TUI is active (no stdout clutter)."""
    log_level = getattr(logging, config.get("log_level", "INFO").upper(), logging.INFO)
    log_file = config.get("log_file", "logs/tool.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(log_level)

    fh = logging.FileHandler(log_file)
    fh.setLevel(log_level)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    root.handlers.clear()
    root.addHandler(fh)


async def run_pipeline(tui: TuiDashboard, config: dict, pool, db, targets):
    """Run scraper then adder in background while TUI shows progress."""
    scraper = Scraper(pool, db, config)
    adder = Adder(pool, db, config)
    tui.scraper = scraper
    tui.adder = adder

    try:
        if targets:
            await scraper.run(targets)
            tui.export_csv()

        total_in_db = await db.count_users()
        if total_in_db > 0:
            await adder.run()
            tui.export_csv()
    except Exception:
        logger.error("Pipeline error", exc_info=True)
    finally:
        tui.export_csv()


async def main():
    config = load_config()

    # ── Startup wizard ──
    if not sys.stdin.isatty():
        print("No interactive terminal — skipping wizard, running from config")
    elif not await wizard(config):
        return

    setup_logging(config)

    # Init database
    db = Database(config.get("db_path", "data/scraper.db"))
    await db.init()

    # Init session pool
    pool = SessionPool(config)
    await pool.connect_all_sessions()

    # Placeholder files
    for fname, content in [
        ("targets.txt", "# One target per line: username or invite link\n"),
        ("proxies.txt", "# One proxy per line: socks5://user:pass@host:port\n"),
    ]:
        p = Path(fname)
        if not p.exists():
            p.write_text(content)

    targets = load_targets(config.get("targets", "targets.txt"))

    # Build TUI
    tui = TuiDashboard(
        database=db,
        session_pool=pool,
        refresh_per_second=4.0,
    )

    # Run pipeline as background task
    pipeline_task = asyncio.create_task(
        run_pipeline(tui, config, pool, db, targets)
    )

    # Run TUI in a thread (rich.Live is sync)
    loop = asyncio.get_event_loop()
    stop_event = threading.Event()

    def shutdown(*args):
        stop_event.set()
        if not pipeline_task.done():
            pipeline_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass

    # Run TUI in thread
    def tui_thread():
        tui.console.print("[bold cyan]⚡ TG Tool — Terminal Dashboard[/bold cyan]")
        tui.console.print(f"[dim]Targets: {targets or 'none'}[/dim]")
        tui.console.print("[dim]Ctrl+C to stop[/dim]\n")
        tui.run()

    thread = threading.Thread(target=tui_thread, daemon=True)
    thread.start()

    # Wait for pipeline, then keep TUI alive until Ctrl+C
    try:
        # First, wait for pipeline to finish
        while not stop_event.is_set() and not pipeline_task.done():
            await asyncio.sleep(0.5)
        # Pipeline done — keep TUI alive for review
        if not stop_event.is_set():
            # Give TUI a moment to render final state
            await asyncio.sleep(1)
        # Stay alive until Ctrl+C
        while not stop_event.is_set():
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        tui.stop()
        stop_event.set()
        if not pipeline_task.done():
            pipeline_task.cancel()
            try:
                await pipeline_task
            except asyncio.CancelledError:
                pass

    thread.join(timeout=2)
    await pool.close_all()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
