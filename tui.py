"""
Terminal UI dashboard — live progress using rich.
Replaces the FastAPI web dashboard with a beautiful TUI.
"""
import csv
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text


class TuiDashboard:
    def __init__(
        self, database=None, scraper=None, adder=None, session_pool=None,
        refresh_per_second: float = 2.0,
    ):
        self.database = database
        self.scraper = scraper
        self.adder = adder
        self.session_pool = session_pool
        self.refresh_rate = refresh_per_second
        self.console = Console()
        self._running = False

    def _build_session_table(self) -> Table:
        table = Table(title="🔌 Sessions", expand=True, padding=(0, 1))
        table.add_column("Session", style="cyan", no_wrap=True)
        table.add_column("Health", justify="right")
        table.add_column("Status")
        table.add_column("Uses", justify="right")
        table.add_column("Floods", justify="right")
        table.add_column("Rate", justify="right")
        table.add_column("Proxy")

        if self.session_pool:
            for s in self.session_pool.get_status():
                health_color = "green" if s["health"] > 0.7 else ("yellow" if s["health"] > 0.3 else "red")
                status = "🔒 QUARANTINED" if s["quarantined"] else "🟢 active"
                status_style = "red" if s["quarantined"] else "green"
                table.add_row(
                    s["id"],
                    f"[{health_color}]{s['health']:.2f}[/{health_color}]",
                    f"[{status_style}]{status}[/{status_style}]",
                    str(s["uses"]),
                    str(s["flood_waits"]),
                    f"{s['success_rate']:.2f}",
                    s["proxy"],
                )
        else:
            table.add_row("—", "—", "—", "—", "—", "—", "—")
        return table

    def _build_scraper_panel(self) -> Panel:
        if not self.scraper:
            return Panel("Scraper not running", title="📡 Scraper")

        progress = self.scraper.get_progress()
        if not progress:
            return Panel("Waiting for targets...", title="📡 Scraper")

        lines = []
        for target, info in progress.items():
            scraped = info.get("scraped", 0) or 0
            total = info.get("total", 0)
            status = info.get("status", "?")
            status_color = "green" if status == "done" else ("yellow" if "flood" in str(status) or "error" in str(status) else "cyan")

            if total and total > 0:
                pct = min(100, int(scraped / max(total, 1) * 100))
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                count_str = f"{scraped}/{total}"
            else:
                pct = 0
                bar = "▓" * (min(scraped % 10, 10)) + "░" * (10 - min(scraped % 10, 10))
                count_str = f"{scraped} scraped"

            lines.append(
                f"[bold]{target}[/bold]\n"
                f"  {bar} {pct}%  {count_str}  "
                f"[{status_color}]{status}[/{status_color}]"
            )

        return Panel("\n".join(lines), title="📡 Scraper")

    def _build_adder_panel(self) -> Panel:
        if not self.adder:
            return Panel("Adder not running", title="📤 Adder")

        progress = self.adder.get_progress()
        status = progress.get("status", "idle")
        added = progress.get("added", 0)
        failed = progress.get("failed", 0)
        skipped = progress.get("skipped", 0)
        total = added + failed + skipped

        # Get anti-limiting state
        delay = getattr(self.adder, '_current_delay', 0)
        floods = getattr(self.adder, '_consecutive_floods', 0)

        status_color = "green" if status == "done" else ("yellow" if status == "running" else "dim")
        lines = [
            f"Status: [{status_color}]{status}[/{status_color}]",
            f"✅ Added:  [green]{added}[/green]",
            f"❌ Failed: [red]{failed}[/red]",
            f"⏭️ Skipped: [yellow]{skipped}[/yellow]",
        ]
        if delay > 1000:
            lines.append(f"⏱️ Delay: [dim]{delay/1000:.0f}s[/dim]" + (f"  ⚡ floods:{floods}" if floods else ""))
        if total:
            pct = int(added / total * 100) if total else 0
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(f"\n{bar} {pct}%")

        return Panel("\n".join(lines), title="📤 Adder")

    def _build_events_panel(self) -> Panel:
        if not self.adder:
            return Panel("No events", title="⚠️ Rate Limits")

        events = self.adder.get_rate_limit_events()[-10:]
        if not events:
            return Panel("No rate-limit events yet", title="⚠️ Rate Limits")

        lines = []
        for e in reversed(events):
            ts = e.get("timestamp", "")[11:19] if e.get("timestamp") else ""
            etype = e.get("type", "?")
            uid = e.get("user_id", "?")
            session = e.get("session", "?")
            seconds = e.get("seconds", "")
            wait_str = f" {seconds}s" if seconds else ""
            color = "red" if etype == "FLOOD_WAIT" else "yellow"
            lines.append(f"[{color}]{ts} {etype}[/{color}] user={uid} sess={session}{wait_str}")

        return Panel("\n".join(lines), title="⚠️ Rate Limit Events")

    def _build_counts_panel(self) -> Panel:
        """Read cached counts from adder (updated in real-time by pipeline)."""
        lines = []

        # Try adder cached counts first
        if self.adder and hasattr(self.adder, 'db_counts'):
            c = self.adder.db_counts
            total = c.get("total", 0)
            added = c.get("added", 0)
            remaining = total - added
            lines = [
                f"👥 Total:    [bold cyan]{total}[/bold cyan]",
                f"✅ Added:    [bold green]{added}[/bold green]",
                f"⏳ Remaining: [bold yellow]{remaining}[/bold yellow]",
            ]
        elif self.scraper:
            # Fallback: show scraper totals
            progress = self.scraper.get_progress()
            total_scraped = sum(
                v.get("scraped", 0) or 0 for v in progress.values()
            )
            lines = [
                f"👥 Scraped:  [bold cyan]{total_scraped}[/bold cyan]",
                f"📡 Targets: [dim]{len(progress)}[/dim]",
            ]
        else:
            lines = ["Waiting for pipeline..."]

        return Panel("\n".join(lines), title="👥 Database")

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=7),
        )
        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right"),
        )
        layout["left"].split(
            Layout(self._build_counts_panel(), name="counts", ratio=2),
            Layout(self._build_scraper_panel(), name="scraper", ratio=3),
        )
        layout["right"].split(
            Layout(self._build_adder_panel(), name="adder", ratio=2),
            Layout(self._build_events_panel(), name="events", ratio=3),
        )

        # Header
        now = datetime.now().strftime("%H:%M:%S")
        header_text = Text()
        header_text.append("⚡ TG Tool", style="bold cyan")
        header_text.append("  │  ", style="dim")
        # Show active session count
        if self.session_pool:
            active = sum(1 for s in self.session_pool.get_status() if not s.get("quarantined"))
            total = len(self.session_pool.get_status())
            header_text.append(f"[green]{active} accounts[/green]  ", style="white")
        header_text.append(f"Dashboard  ", style="white")
        header_text.append(f"│  {now}", style="dim")
        header_text.append("\n", style="")
        header_text.append("[bold yellow]Ctrl+C[/bold yellow] to stop  │  CSV auto-exports on completion", style="dim")

        layout["header"].update(Panel(header_text))
        layout["footer"].update(Panel(self._build_session_table()))

        return layout

    def run(self):
        """Blocking — render the TUI live until interrupted."""
        self._running = True
        with Live(
            self._build_layout(),
            console=self.console,
            refresh_per_second=self.refresh_rate,
            screen=True,
        ) as live:
            try:
                while self._running:
                    live.update(self._build_layout())
                    import time
                    time.sleep(1.0 / self.refresh_rate)
            except KeyboardInterrupt:
                pass

    def stop(self):
        self._running = False

    def export_csv(self, path: str = "data/export.csv") -> int:
        """Export users to CSV. Returns count."""
        import asyncio

        async def do_export():
            if self.database is None:
                return 0
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            return await self.database.export_csv(path)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return 0
            return loop.run_until_complete(do_export())
        except Exception:
            return 0
