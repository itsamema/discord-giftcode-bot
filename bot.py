#!/usr/bin/env python3
"""
Discord Giftcode Relay Bot
- Watches hidden SOURCE channels
- Detects gift codes via keywords
- Extracts expiry dates (YYYY/MM/DD)
- Detects Chief Concierge / VIP12
- Reposts a clean message to TARGET channel
- Tracks codes in SQLite to mark recurring ones
"""

import os
import re
import sqlite3
import asyncio
from datetime import datetime, date
from typing import List, Optional, Tuple

import discord
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web

# ---------- Config ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Channel IDs ONLY via env vars (comma-separated for multiple sources)
# SOURCE_CHANNEL_IDS="111,222"   TARGET_CHANNEL_ID="333"
SOURCE_CHANNEL_IDS = [
    int(x.strip())
    for x in os.getenv("SOURCE_CHANNEL_IDS", "").split(",")
    if x.strip()
]
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0") or 0)

DEFAULT_KEYWORDS = [
    "gift code",
    "giftcode",
    "voucher",
    "redeem",
    "chief concierge",
    "concierge",
    "vip",
]
KEYWORDS = [
    k.strip().lower()
    for k in os.getenv("KEYWORDS", ",".join(DEFAULT_KEYWORDS)).split(",")
    if k.strip()
]

# Code pattern: 6–25 alnum or blocks like ABCD-1234-XYZ
CODE_REGEX = re.compile(r"\b([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+|[A-Z0-9]{6,25})\b")
VIP_REGEX = re.compile(r"\b(chief\s*concierge|vip\s*1?2?)\b", re.IGNORECASE)

DB_PATH = os.getenv("DB_PATH", "giftcodes.sqlite3")
SCHEMA = """
CREATE TABLE IF NOT EXISTS giftcodes (
    code TEXT PRIMARY KEY,
    first_seen TIMESTAMP NOT NULL,
    last_seen TIMESTAMP NOT NULL,
    expiry DATE,
    is_vip INTEGER DEFAULT 0
);
"""

# ---------- Persistence ----------
class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def upsert_code(
        self, code: str, seen_at: datetime, expiry: Optional[date], is_vip: bool
    ) -> Tuple[bool, Optional[date], bool]:
        cur = self.conn.cursor()
        cur.execute("SELECT code, expiry, is_vip FROM giftcodes WHERE code = ?", (code,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO giftcodes (code, first_seen, last_seen, expiry, is_vip) VALUES (?, ?, ?, ?, ?)",
                (code, seen_at.isoformat(), seen_at.isoformat(), expiry.isoformat() if expiry else None, int(is_vip)),
            )
            self.conn.commit()
            return True, None, False
        else:
            _, existing_expiry_str, prev_vip_int = row
            existing_expiry = date.fromisoformat(existing_expiry_str) if existing_expiry_str else None
            best_expiry = expiry or existing_expiry
            cur.execute(
                "UPDATE giftcodes SET last_seen=?, expiry=?, is_vip=? WHERE code=?",
                (seen_at.isoformat(), best_expiry.isoformat() if best_expiry else None, int(is_vip or prev_vip_int), code),
            )
            self.conn.commit()
            return False, existing_expiry, bool(prev_vip_int)

store = Store(DB_PATH)

# ---------- Helpers ----------
def normalize_code(token: str) -> str:
    return token.upper()

def looks_like_gift_announcement(raw: str) -> bool:
    lower = raw.lower()
    return any(k in lower for k in KEYWORDS)

def extract_expiry(raw: str) -> Optional[date]:
    # YYYY/MM/DD or YYYY-MM-DD
    m = re.search(r"\b(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})\b", raw)
    if m:
        try:
            y, mm, dd = map(int, m.groups()); return date(y, mm, dd)
        except: pass
    # DD.MM.YYYY or DD/MM/YYYY
    m2 = re.search(r"\b(\d{1,2})[\/\.](\d{1,2})[\/\.](\d{4})\b", raw)
    if m2:
        try:
            dd, mm, y = map(int, m2.groups()); return date(y, mm, dd)
        except: pass
    return None

def find_codes(raw: str) -> List[str]:
    cleaned = re.sub(r"https?://\S+", " ", raw)
    return [normalize_code(m.group(1)) for m in CODE_REGEX.finditer(cleaned)]

def format_date_iso(d: Optional[date]) -> str:
    return d.strftime("%Y/%m/%d") if d else "unbekannt"

def collect_text_from_message(message: discord.Message) -> str:
    """Gather plaintext + embed/attachment text (forwarders often use embeds)."""
    parts = []
    if message.content:
        parts.append(message.content)

    for e in message.embeds:
        if e.title: parts.append(e.title)
        if e.description: parts.append(e.description)
        for f in getattr(e, "fields", []):
            if f.name: parts.append(f.name)
            if f.value: parts.append(f.value)
        if e.footer and getattr(e.footer, "text", None):
            parts.append(e.footer.text)
        if e.author and getattr(e.author, "name", None):
            parts.append(e.author.name)

    for a in message.attachments:
        if getattr(a, "description", None):
            parts.append(a.description)
        if getattr(a, "filename", None):
            parts.append(a.filename)

    return "\n".join(parts)

# ---------- Discord Bot ----------
intents = discord.Intents.default()
intents.message_content = True  # enable in Developer Portal too
intents.guilds = True
intents.guild_messages = True
bot = commands.Bot(command_prefix="/", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")
    print(f"SOURCE_CHANNEL_IDS: {SOURCE_CHANNEL_IDS}")
    print(f"TARGET_CHANNEL_ID:  {TARGET_CHANNEL_ID}")
    # Presence/status
    await bot.change_presence(activity=discord.Game(name="scanning for giftcodes 🎁"))
    # Start the tiny HTTP server AFTER loop is running so Render sees an open port
    try:
        await start_keepalive_server()
        print("🌐 Keep-alive server started.")
    except Exception as e:
        print(f"⚠️ Keep-alive server failed to start: {e}")

async def announce_code(target_channel: discord.TextChannel, code: str, expiry: Optional[date], is_vip: bool, recurring: bool):
    header = "Recurring gift code!" if recurring else "New gift code!"
    lines = [f"{header} `{code}` — redeem until {format_date_iso(expiry)}"]
    if is_vip:
        lines.append(f"VIP12 gift code: `{code}`")
    await target_channel.send("\n".join(lines))

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    # Ignore only our own messages; allow forwarded messages from other bots/webhooks
    if message.author.id == bot.user.id:
        return

    # Only watch configured source channels
    if message.channel.id not in SOURCE_CHANNEL_IDS:
        return

    # ✅ Debug: confirm we see messages in the source channel
    print(f"🔎 Seen message in source {message.channel.id} from {message.author} (bot={message.author.bot})")

    raw = collect_text_from_message(message)
    if not raw or not looks_like_gift_announcement(raw):
        return

    # Optional extra debug:
    print(f"🧩 Matched keywords in message: {raw[:120]}{'…' if len(raw) > 120 else ''}")

    codes = find_codes(raw)
    if not codes:
        return

    expiry = extract_expiry(raw)
    is_vip = bool(VIP_REGEX.search(raw))
    target = bot.get_channel(TARGET_CHANNEL_ID)
    if not isinstance(target, discord.TextChannel):
        print("⚠️ Target channel not found or not a text channel.")
        return

    now = datetime.utcnow()
    for code in codes:
        is_new, prev_expiry, prev_vip = store.upsert_code(code, now, expiry, is_vip)
        recurring = not is_new
        best_expiry = expiry or prev_expiry
        print(f"📤 Reposting code {code} (recurring={recurring}, vip={is_vip or prev_vip})")
        await announce_code(target, code, best_expiry, is_vip or prev_vip, recurring)


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.reply("pong")

# ---------- Keep-alive for Render Web Service (bind to $PORT) ----------
async def _alive_handler(request):
    return web.Response(text="I'm alive!")

async def start_keepalive_server():
    app = web.Application()
    app.router.add_get('/', _alive_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))  # Render provides PORT
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN missing!")
    bot.run(TOKEN)
