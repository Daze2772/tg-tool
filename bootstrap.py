"""
Session bootstrapper — handles one-time auth for new sessions.
Prompts for phone number + code, persists authorized session.
Run once per session before using the main tool.
"""
import asyncio
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


async def bootstrap_session(
    api_id: int,
    api_hash: str,
    phone: str,
    session_name: str = None,
    session_dir: str = "sessions",
):
    """Create or re-auth a session file. Interactive code prompt."""
    if session_name is None:
        session_name = phone.replace("+", "_").replace(" ", "")

    session_path = str(Path(session_dir) / session_name)
    Path(session_dir).mkdir(parents=True, exist_ok=True)

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Session already authorized: {me.first_name} (@{me.username})")
        await client.disconnect()
        return session_name

    print(f"Sending code to {phone}...")
    result = await client.send_code_request(phone)
    print(f"Code sent via {result.type}")

    code = input("Enter the verification code: ").strip()

    try:
        await client.sign_in(phone, code, phone_code_hash=result.phone_code_hash)
    except SessionPasswordNeededError:
        password = input("2FA password required: ")
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"Authorized! {me.first_name} (@{me.username}) id={me.id}")
    await client.disconnect()
    return session_name


async def main():
    config_path = Path("config.yaml")
    if config_path.exists():
        import yaml
        config = yaml.safe_load(config_path.read_text()) or {}
        api_id = config.get("api_id", 0)
        api_hash = config.get("api_hash", "")
    else:
        api_id = int(input("API ID: "))
        api_hash = input("API Hash: ")

    phones = []
    targets_path = Path("phones.txt")
    if targets_path.exists():
        phones = [l.strip() for l in targets_path.read_text().splitlines()
                  if l.strip() and not l.startswith("#")]

    if not phones:
        phone = input("Phone number (+XXXXXXXXXXX): ").strip()
        phones = [phone]

    for phone in phones:
        print(f"\n--- Bootstrapping {phone} ---")
        await bootstrap_session(api_id, api_hash, phone)

    print("\nAll sessions bootstrapped. Run main.py to start the tool.")


if __name__ == "__main__":
    asyncio.run(main())
