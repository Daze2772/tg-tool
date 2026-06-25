"""
Session pool manager — multi-session, proxy binding, health scoring,
auto-quarantine on flood/ban. Persisted to disk, auto-reconnect.
"""
import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UserBannedInChannelError,
    PeerFloodError,
)
from telethon.sessions import StringSession

logger = logging.getLogger("session_manager")

PROXY_TYPES = {
    "socks5": "socks5",
    "http": "http",
    "socks4": "socks4",
}


@dataclass
class SessionInfo:
    session_id: str
    phone: str
    proxy: Optional[str] = None
    quarantined: bool = False
    quarantine_reason: str = ""
    health_score: float = 1.0
    uses: int = 0
    successes: int = 0
    flood_waits: int = 0
    last_used: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    client: Optional[TelegramClient] = field(default=None, repr=False)

    @property
    def success_rate(self) -> float:
        if self.uses == 0:
            return 1.0
        return self.successes / self.uses


class SessionPool:
    def __init__(self, config: dict):
        self.config = config
        self.session_dir = Path(config.get("session_dir", "sessions"))
        self.max_sessions = config.get("max_sessions", 5)
        self.proxy_list = self._load_proxies(config.get("proxy_list", "proxies.txt"))
        self.api_id = config["api_id"]
        self.api_hash = config["api_hash"]
        self.sessions: List[SessionInfo] = []
        self._lock = asyncio.Lock()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._load_existing_sessions()

    def _load_proxies(self, path: str) -> List[Optional[str]]:
        """Load proxy list from file. Returns list with None entries for direct connections."""
        proxy_path = Path(path)
        if not proxy_path.exists():
            logger.warning(f"Proxy file not found: {path}, using direct connection")
            return [None]
        lines = [l.strip() for l in proxy_path.read_text().splitlines() if l.strip() and not l.startswith("#")]
        if not lines:
            return [None]
        return lines

    def _parse_proxy(self, proxy_str: str) -> Tuple:
        """Parse proxy string into (type, host, port, user, password)."""
        if "://" not in proxy_str:
            return ("socks5", proxy_str, None, None, None)
        proto, rest = proxy_str.split("://", 1)
        proto = proto.lower()
        auth_host = rest
        user, password = None, None
        if "@" in auth_host:
            auth, hostpart = auth_host.rsplit("@", 1)
            host_port = hostpart
            if ":" in auth:
                user, password = auth.split(":", 1)
            else:
                user = auth
        else:
            host_port = auth_host
        host, _, port = host_port.partition(":")
        return (proto, host, int(port) if port else None, user, password)

    def _load_existing_sessions(self):
        """Discover existing .session files."""
        for f in self.session_dir.glob("*.session"):
            sid = f.stem
            if not any(s.session_id == sid for s in self.sessions):
                info = SessionInfo(
                    session_id=sid,
                    phone=sid.replace("_", "+"),
                )
                proxy_idx = hash(sid) % len(self.proxy_list)
                info.proxy = self.proxy_list[proxy_idx]
                self.sessions.append(info)
                logger.info(f"Loaded existing session: {sid}")

    async def connect_all_sessions(self):
        """Connect all loaded sessions and keep authorized ones."""
        connected = 0
        for info in list(self.sessions):
            try:
                client = await self._init_client(info)
                if await client.is_user_authorized():
                    connected += 1
                    logger.info(f"Session {info.session_id} authorized and connected")
                else:
                    logger.warning(f"Session {info.session_id} not authorized — removing")
                    await client.disconnect()
                    self.sessions.remove(info)
            except Exception as e:
                logger.warning(f"Session {info.session_id} connection failed: {e}")
                if info in self.sessions:
                    self.sessions.remove(info)
        logger.info(f"Connected {connected}/{len(self.sessions) + connected} sessions")
        return connected

    async def _init_client(self, info: SessionInfo) -> TelegramClient:
        """Create and connect a Telethon client for a session."""
        session_path = str(self.session_dir / info.session_id)
        client_kwargs = {
            "session": session_path,
            "api_id": self.api_id,
            "api_hash": self.api_hash,
        }
        if info.proxy:
            proto, host, port, user, pwd = self._parse_proxy(info.proxy)
            client_kwargs["proxy"] = (proto, host, port, True, user, pwd)
        client = TelegramClient(**client_kwargs)
        await client.connect()
        info.client = client
        return client

    async def register_session(self, phone: str, proxy: Optional[str] = None) -> SessionInfo:
        """Register a new session. Signs in via code if needed."""
        async with self._lock:
            if len(self.sessions) >= self.max_sessions:
                # Evict lowest health score
                self.sessions.sort(key=lambda s: s.health_score)
                evicted = self.sessions.pop(0)
                logger.warning(f"Evicting session {evicted.session_id} (health={evicted.health_score:.2f})")

            sid = phone.replace("+", "_").replace(" ", "")
            if proxy is None and self.proxy_list:
                proxy = random.choice(self.proxy_list)

            info = SessionInfo(session_id=sid, phone=phone, proxy=proxy)
            client = await self._init_client(info)

            if not await client.is_user_authorized():
                logger.info(f"Session {sid} needs auth — sending code to {phone}")
                await client.send_code_request(phone)
                # In autonomous mode we can't receive codes
                # Session creation will need pre-authorized sessions
                raise RuntimeError(
                    f"Session {sid} requires phone code verification. "
                    f"Pre-authorize sessions or use existing .session files."
                )

            self.sessions.append(info)
            logger.info(f"Session {sid} registered (proxy: {proxy})")
            return info

    async def get_session(self) -> Optional[SessionInfo]:
        """Get the best available non-quarantined session."""
        async with self._lock:
            available = [s for s in self.sessions if not s.quarantined and s.client is not None]
            if not available:
                return None
            # Pick best health score, weighted with jitter
            available.sort(key=lambda s: s.health_score, reverse=True)
            top_score = available[0].health_score
            candidates = [s for s in available if s.health_score >= top_score * 0.9]
            chosen = random.choice(candidates)
            chosen.last_used = datetime.now(timezone.utc)
            return chosen

    async def report_success(self, info: SessionInfo):
        """Report a successful operation on a session."""
        async with self._lock:
            info.uses += 1
            info.successes += 1
            info.health_score = min(1.0, info.health_score + 0.02)

    async def report_failure(self, info: SessionInfo):
        """Report a failed operation on a session."""
        async with self._lock:
            info.uses += 1
            info.health_score = max(0.0, info.health_score - 0.05)

    async def report_flood_wait(self, info: SessionInfo, seconds: int):
        """Report a FLOOD_WAIT hit — penalize health significantly."""
        async with self._lock:
            info.uses += 1
            info.flood_waits += 1
            info.health_score = max(0.0, info.health_score - 0.15)
            logger.warning(
                f"Session {info.session_id} hit FLOOD_WAIT {seconds}s "
                f"(health now {info.health_score:.2f})"
            )

    async def quarantine(self, info: SessionInfo, reason: str):
        """Quarantine a session — it won't be used again."""
        async with self._lock:
            info.quarantined = True
            info.quarantine_reason = reason
            info.health_score = 0.0
            logger.error(f"Session {info.session_id} QUARANTINED: {reason}")

    async def close_all(self):
        """Disconnect all clients."""
        for s in self.sessions:
            if s.client:
                try:
                    await s.client.disconnect()
                except Exception:
                    pass

    def get_status(self) -> List[Dict]:
        """Return session status for dashboard."""
        return [
            {
                "id": s.session_id,
                "health": round(s.health_score, 2),
                "quarantined": s.quarantined,
                "quarantine_reason": s.quarantine_reason,
                "uses": s.uses,
                "flood_waits": s.flood_waits,
                "success_rate": round(s.success_rate, 2),
                "proxy": s.proxy[:40] if s.proxy else "direct",
                "last_used": s.last_used.isoformat() if s.last_used else None,
            }
            for s in self.sessions
        ]
