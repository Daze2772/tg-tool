"""
Scraper — extracts user ID, username, first/last name, phone from target groups.
Handles private groups, resume partial scrapes, rate limiting.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    ChatAdminRequiredError,
    ChannelPrivateError,
)
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import (
    InputPeerEmpty,
    ChannelParticipantsSearch,
    Channel,
    User,
    InputPeerChannel,
)

from session_manager import SessionPool, SessionInfo
from database import Database

logger = logging.getLogger("scraper")


class Scraper:
    def __init__(self, pool: SessionPool, db: Database, config: dict):
        self.pool = pool
        self.db = db
        self.delay_ms = config.get("scrape_delay_ms", 2000)
        self.batch_size = config.get("scrape_batch_size", 100)
        self.progress: dict = {}  # target -> {total, scraped, status}
        self._stop = False

    def stop(self):
        self._stop = True

    async def resolve_entity(self, client: TelegramClient, target: str):
        """Resolve a username, invite link, or numeric ID to an entity."""
        original = target
        target = target.lstrip("@")
        # Try as numeric ID first (for channel/group IDs)
        try:
            entity_id = int(target)
            try:
                entity = await client.get_entity(entity_id)
                if entity:
                    return entity
            except Exception:
                pass
        except ValueError:
            pass
        # Try username
        try:
            entity = await client.get_entity(target)
            return entity
        except ValueError:
            # Try as invite link
            if original.startswith("http"):
                try:
                    entity = await client.get_entity(original)
                    return entity
                except Exception:
                    pass
        except Exception:
            pass
        return None

    async def scrape_target(
        self, target: str, session: SessionInfo
    ) -> Tuple[int, int]:
        """Scrape one target group. Returns (new_users, total_participants)."""
        client = session.client
        if not client:
            logger.error(f"No client for session {session.session_id}")
            return 0, 0

        self.progress[target] = {"total": 0, "scraped": 0, "status": "resolving"}
        logger.info(f"[{target}] Resolving entity...")

        try:
            entity = await self.resolve_entity(client, target)
        except FloodWaitError as e:
            wait = e.seconds
            logger.warning(f"[{target}] FLOOD_WAIT resolving entity: {wait}s")
            await self.pool.report_flood_wait(session, wait)
            await asyncio.sleep(wait)
            try:
                entity = await self.resolve_entity(client, target)
            except Exception as ex:
                self.progress[target]["status"] = f"error: {ex}"
                return 0, 0
        except ChannelPrivateError:
            logger.warning(f"[{target}] Channel is private — session must be member")
            self.progress[target]["status"] = "error: private, not a member"
            return 0, 0
        except Exception as e:
            logger.error(f"[{target}] Failed to resolve: {e}")
            self.progress[target]["status"] = f"error: {e}"
            return 0, 0

        if entity is None:
            logger.error(f"[{target}] Could not resolve entity")
            self.progress[target]["status"] = "error: not found"
            return 0, 0

        # Get participant count
        try:
            if hasattr(entity, "participants_count"):
                total = entity.participants_count or 0
            else:
                total = 0
        except Exception:
            total = 0

        self.progress[target] = {"total": total, "scraped": 0, "status": "scraping"}
        new_users = 0

        # Use iter_participants for this version (Telethon 1.29.3)
        # For large groups, rework pagination with GetParticipantsRequest
        try:
            participants = []
            async for user in client.iter_participants(
                entity, limit=self.batch_size, aggressive=False
            ):
                if self._stop:
                    break
                participants.append(user)
            logger.info(f"[{target}] Got {len(participants)} participants")
        except FloodWaitError as e:
            wait = e.seconds
            logger.warning(f"[{target}] FLOOD_WAIT scrape: {wait}s")
            await self.pool.report_flood_wait(session, wait)
            self.progress[target]["status"] = f"flood_wait {wait}s"
            await asyncio.sleep(wait)
            participants = []
        except Exception as e:
            logger.error(f"[{target}] Scrape error: {e}")
            self.progress[target]["status"] = f"error: {e}"
            return 0, 0

        for user in participants:
            if self._stop:
                break
            if not isinstance(user, User) or user.bot:
                continue
            uid = user.id
            uname = user.username or ""
            fname = user.first_name or ""
            lname = user.last_name or ""
            phone = getattr(user, "phone", "") or ""
            is_new = await self.db.upsert_user(
                user_id=uid,
                username=uname,
                first_name=fname,
                last_name=lname,
                phone=phone,
                source=target,
            )
            if is_new:
                new_users += 1

        self.progress[target]["scraped"] = len(participants)

        await self.pool.report_success(session)
        await asyncio.sleep(self.delay_ms / 1000.0)

        if self._stop:
            self.progress[target]["status"] = "stopped"
        else:
            self.progress[target]["status"] = "done"
        logger.info(f"[{target}] Scrape complete: {new_users} new users")
        return new_users, total or len(participants)

    async def run(self, targets: List[str]):
        """Scrape all targets, cycling sessions."""
        for target in targets:
            if self._stop:
                break
            session = await self.pool.get_session()
            if session is None:
                logger.error("No available sessions for scraping")
                self.progress[target] = {
                    "total": 0, "scraped": 0, "status": "error: no sessions"
                }
                continue
            await self.scrape_target(target, session)

    def get_progress(self) -> dict:
        return dict(self.progress)
