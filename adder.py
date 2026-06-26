"""
Adder — pulls from deduped DB, adds to destination channel.
Anti-limiting: progressive backoff, long base delay, retry on PEER_FLOOD.
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

# Anti-limiting defaults
DEFAULT_ADD_DELAY_MS = 60000      # 60 seconds base (one per minute)
DEFAULT_JITTER_MS = 15000          # 15 seconds jitter
PEER_FLOOD_BACKOFF_MS = 120000    # 2 minutes backoff on PEER_FLOOD
PEER_FLOOD_RETRIES = 5             # Retry same user up to 5 times
CONSECUTIVE_FLOOD_MULTIPLIER = 2.0 # Double delay on consecutive floods
MAX_CONSECUTIVE_FLOODS = 5         # After 5 consecutive floods, long cooldown
LONG_COOLDOWN_MS = 300000          # 5 minute cooldown
ROTATE_EVERY_N_ADDS = 5            # Rotate session after every N successful adds
ROTATE_ON_PEER_FLOOD = True        # Also rotate immediately on PEER_FLOOD


class Adder:
    def __init__(self, pool: SessionPool, db: Database, config: dict):
        self.pool = pool
        self.db = db
        self.dest_channel = config.get("dest_channel", "me")
        self.delay_ms = config.get("add_delay_ms", DEFAULT_ADD_DELAY_MS)
        self.batch_size = config.get("add_batch_size", 50)
        self.jitter_ms = config.get("add_jitter_ms", DEFAULT_JITTER_MS)
        self.progress: dict = {
            "total": 0, "added": 0, "failed": 0, "skipped": 0, "status": "idle"
        }
        self._stop = False
        self._rate_limit_events: List[Dict] = []
        self._current_delay = self.delay_ms
        self._consecutive_floods = 0
        # Cached DB stats for TUI
        self.db_counts = {"total": 0, "added": 0}

    def stop(self):
        self._stop = True

    async def resolve_destination(self, client) -> object:
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
        """Add one user. Retries on PEER_FLOOD with progressive backoff.
        Verifies the user actually appears in the channel after adding."""
        for attempt in range(1, PEER_FLOOD_RETRIES + 1):
            try:
                user = await client.get_entity(user_id)
                await client(InviteToChannelRequest(dest_entity, [user]))

                # Verify they actually landed
                try:
                    await asyncio.sleep(2)  # Brief wait for server
                    participants = await client.get_participants(dest_entity, limit=200)
                    participant_ids = {p.id for p in participants if hasattr(p, 'id')}
                    if user_id not in participant_ids:
                        logger.warning(f"User {user_id} not found in channel after add — ghost add")
                        return "GHOST_ADD"
                except Exception:
                    pass  # Verification failed, but add might have worked

                self._consecutive_floods = 0
                self._current_delay = max(self.delay_ms, self._current_delay * 0.95)
                return ""

            except FloodWaitError as e:
                wait = e.seconds
                logger.warning(f"FLOOD_WAIT user {user_id}: {wait}s")
                self._rate_limit_events.append({
                    "type": "FLOOD_WAIT", "user_id": user_id,
                    "seconds": wait, "session": session.session_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                await self.pool.report_flood_wait(session, wait)
                await asyncio.sleep(wait)
                # Retry after flood wait
                try:
                    user = await client.get_entity(user_id)
                    await client(InviteToChannelRequest(dest_entity, [user]))
                    return ""
                except Exception as e:
                    return f"FLOOD_RETRY_FAILED: {e}"

            except PeerFloodError:
                backoff = int(PEER_FLOOD_BACKOFF_MS * (1.5 ** (attempt - 1)))
                self._consecutive_floods += 1
                self._current_delay = int(self._current_delay * CONSECUTIVE_FLOOD_MULTIPLIER)

                logger.warning(
                    f"PEER_FLOOD user {user_id} — backing off {backoff/1000:.0f}s "
                    f"(attempt {attempt}/{PEER_FLOOD_RETRIES}, delay now {self._current_delay/1000:.0f}s)"
                )
                self._rate_limit_events.append({
                    "type": "PEER_FLOOD", "user_id": user_id,
                    "session": session.session_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "seconds": backoff // 1000,
                    "attempt": attempt,
                })
                await self.pool.report_failure(session)
                await asyncio.sleep(backoff / 1000.0)
                # Continue loop to retry

            except UserBannedInChannelError:
                logger.error(f"Session {session.session_id} BANNED from channel")
                await self.pool.quarantine(session, "banned_in_channel")
                return "BANNED_IN_CHANNEL"

            except UserPrivacyRestrictedError:
                return "PRIVACY_RESTRICTED"

            except UserNotMutualContactError:
                return "NOT_MUTUAL_CONTACT"

            except UserAlreadyParticipantError:
                return ""  # Already there

            except ChatWriteForbiddenError:
                logger.error(f"Session {session.session_id} can't write to dest")
                return "CHAT_WRITE_FORBIDDEN"

            except Exception as e:
                return str(e)

        return f"PEER_FLOOD_MAX_RETRIES({PEER_FLOOD_RETRIES})"

    async def _get_delay(self) -> float:
        """Calculate jittered delay based on current anti-limiting state."""
        jitter = random.randint(0, self.jitter_ms)
        return (self._current_delay + jitter) / 1000.0

    async def run(self):
        self.progress["status"] = "running"
        self._current_delay = self.delay_ms
        self._consecutive_floods = 0
        self._adds_since_rotate = 0

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
            users = await self.db.get_unadded_users(self.batch_size)
            if not users:
                logger.info("No more users to add — queue empty")
                break

            self.db_counts["total"] = await self.db.count_users()
            self.db_counts["added"] = await self.db.count_added()
            self.progress["total"] = self.db_counts["total"]
            self.progress["status"] = "adding"

            for user in users:
                if self._stop:
                    break

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
                        session = await self.pool.get_session()
                        if session is None:
                            self.progress["status"] = "error: all sessions quarantined"
                            return
                        client = session.client
                        dest = await self.resolve_destination(client)
                        await self.db.mark_add_error(uid, error)
                        self.progress["failed"] += 1

                    elif "PEER_FLOOD_MAX_RETRIES" in error.upper():
                        await self.db.mark_add_error(uid, error)
                        self.progress["skipped"] += 1

                    elif "GHOST_ADD" in error.upper():
                        await self.db.mark_add_error(uid, error)
                        self.progress["failed"] += 1

                    elif "PRIVACY" in error.upper() or "NOT_MUTUAL" in error.upper():
                        await self.db.mark_add_error(uid, error)
                        self.progress["skipped"] += 1

                    else:
                        await self.db.mark_add_error(uid, error)
                        self.progress["failed"] += 1
                else:
                    await self.db.mark_added(uid)
                    await self.pool.report_success(session)
                    self.progress["added"] += 1
                    self._adds_since_rotate += 1

                # Rotate session proactively
                should_rotate = False
                if error and "PEER_FLOOD" in error.upper() and ROTATE_ON_PEER_FLOOD:
                    should_rotate = True
                elif self._adds_since_rotate >= ROTATE_EVERY_N_ADDS:
                    should_rotate = True

                if should_rotate:
                    new_session = await self.pool.get_session()
                    if new_session and new_session.session_id != session.session_id:
                        logger.info(
                            f"Rotating session: {session.session_id} → {new_session.session_id}"
                        )
                        session = new_session
                        client = session.client
                        dest = await self.resolve_destination(client)
                        self._adds_since_rotate = 0
                        self._consecutive_floods = 0
                        self._current_delay = self.delay_ms

                # Update cached counts
                self.db_counts["added"] = self.progress["added"]
                self.db_counts["total"] = self.progress["total"]

                # Long cooldown if hitting too many consecutive floods
                if self._consecutive_floods >= MAX_CONSECUTIVE_FLOODS:
                    cooldown_min = LONG_COOLDOWN_MS // 60000
                    logger.warning(
                        f"Hit {self._consecutive_floods} consecutive floods — "
                        f"cooling down {cooldown_min}min"
                    )
                    self.progress["status"] = f"cooldown {cooldown_min}min"
                    self._rate_limit_events.append({
                        "type": "LONG_COOLDOWN",
                        "session": session.session_id,
                        "seconds": LONG_COOLDOWN_MS // 1000,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    await asyncio.sleep(LONG_COOLDOWN_MS / 1000.0)
                    self._consecutive_floods = 0
                    self._current_delay = self.delay_ms
                    self.progress["status"] = "adding"

                delay = await self._get_delay()
                logger.debug(
                    f"Delay: {delay:.1f}s (base={self._current_delay}ms "
                    f"floods={self._consecutive_floods})"
                )
                await asyncio.sleep(delay)

        self.db_counts["total"] = await self.db.count_users()
        self.db_counts["added"] = await self.db.count_added()
        self.progress["status"] = "done" if not self._stop else "stopped"

    def get_progress(self) -> dict:
        return dict(self.progress)

    def get_rate_limit_events(self) -> List[Dict]:
        return list(self._rate_limit_events)
