"""
Adder — pulls from deduped DB, adds to destination channel.
Per-session rate limiting with jitter. Flood/ban resilience.
"""
import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Dict, List, Optional

from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserBannedInChannelError,
    UserPrivacyRestrictedError,
    UserNotMutualContactError,
    UserAlreadyParticipantError,
    ChatWriteForbiddenError,
)
from telethon.tl.functions.channels import InviteToChannelRequest

from session_manager import SessionPool, SessionInfo
from database import Database

logger = logging.getLogger("adder")


class Adder:
    def __init__(self, pool: SessionPool, db: Database, config: dict):
        self.pool = pool
        self.db = db
        self.dest_channel = config.get("dest_channel", "me")
        self.delay_ms = config.get("add_delay_ms", 5000)
        self.batch_size = config.get("add_batch_size", 50)
        self.jitter_ms = config.get("add_jitter_ms", 3000)
        self.progress: dict = {
            "total": 0, "added": 0, "failed": 0, "skipped": 0, "status": "idle"
        }
        self._stop = False
        self._rate_limit_events: List[Dict] = []

    def stop(self):
        self._stop = True

    async def resolve_destination(self, client) -> object:
        """Resolve the destination channel."""
        dest = self.dest_channel.strip()
        if dest.lower() == "me":
            return await client.get_me()
        try:
            return await client.get_entity(dest)
        except ValueError:
            if dest.startswith("http"):
                return await client.get_entity(dest)
            raise

    async def add_user(
        self, client, dest_entity, user_id: int, session: SessionInfo
    ) -> str:
        """Add one user to destination. Returns '' on success, error string on failure."""
        try:
            user = await client.get_entity(user_id)
            await client(InviteToChannelRequest(dest_entity, [user]))
            return ""
        except FloodWaitError as e:
            wait = e.seconds
            logger.warning(f"FLOOD_WAIT adding user {user_id}: {wait}s")
            self._rate_limit_events.append({
                "type": "FLOOD_WAIT",
                "user_id": user_id,
                "seconds": wait,
                "session": session.session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            await self.pool.report_flood_wait(session, wait)
            await asyncio.sleep(wait)
            # Retry after sleep
            try:
                user = await client.get_entity(user_id)
                await client(InviteToChannelRequest(dest_entity, [user]))
                return ""
            except Exception as e:
                return str(e)
        except PeerFloodError:
            logger.warning(f"PEER_FLOOD for user {user_id} — skipping")
            self._rate_limit_events.append({
                "type": "PEER_FLOOD",
                "user_id": user_id,
                "session": session.session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return "PEER_FLOOD"
        except UserBannedInChannelError:
            logger.error(f"Session {session.session_id} BANNED from channel")
            await self.pool.quarantine(session, "banned_in_channel")
            return "BANNED_IN_CHANNEL"
        except UserPrivacyRestrictedError:
            return "PRIVACY_RESTRICTED"
        except UserNotMutualContactError:
            return "NOT_MUTUAL_CONTACT"
        except UserAlreadyParticipantError:
            return ""  # Already there, treat as success
        except ChatWriteForbiddenError:
            logger.error(f"Session {session.session_id} can't write to dest")
            return "CHAT_WRITE_FORBIDDEN"
        except Exception as e:
            return str(e)

    async def run(self):
        """Main add loop. Pulls unadded users and adds to destination."""
        self.progress["status"] = "running"

        session = await self.pool.get_session()
        if session is None:
            logger.error("No available sessions for adding")
            self.progress["status"] = "error: no sessions"
            return

        client = session.client
        try:
            dest = await self.resolve_destination(client)
        except Exception as e:
            logger.error(f"Cannot resolve destination: {e}")
            self.progress["status"] = f"error: {e}"
            return

        while not self._stop:
            # Get new batch
            users = await self.db.get_unadded_users(self.batch_size)
            if not users:
                logger.info("No more users to add — queue empty")
                break

            self.progress["total"] = await self.db.count_users()
            self.progress["status"] = "adding"

            for user in users:
                if self._stop:
                    break

                # Check if current session is healthy
                if session.quarantined:
                    session = await self.pool.get_session()
                    if session is None:
                        self.progress["status"] = "error: all sessions quarantined"
                        return
                    client = session.client
                    dest = await self.resolve_destination(client)

                uid = user["user_id"]
                error = await self.add_user(client, dest, uid, session)

                if error:
                    if "BANNED" in error.upper():
                        # Session was quarantined, get new one
                        session = await self.pool.get_session()
                        if session is None:
                            self.progress["status"] = "error: all sessions quarantined"
                            return
                        client = session.client
                        dest = await self.resolve_destination(client)
                        # Re-mark this user as not added for retry
                        await self.db.mark_add_error(uid, error)
                        self.progress["failed"] += 1
                    elif "PEER_FLOOD" in error.upper():
                        await self.db.mark_add_error(uid, error)
                        self.progress["skipped"] += 1
                    else:
                        await self.db.mark_add_error(uid, error)
                        self.progress["failed"] += 1
                else:
                    await self.db.mark_added(uid)
                    await self.pool.report_success(session)
                    self.progress["added"] += 1

                # Jittered delay
                jitter = random.randint(0, self.jitter_ms)
                await asyncio.sleep((self.delay_ms + jitter) / 1000.0)

        self.progress["status"] = "done" if not self._stop else "stopped"

    def get_progress(self) -> dict:
        return dict(self.progress)

    def get_rate_limit_events(self) -> List[Dict]:
        return list(self._rate_limit_events)
