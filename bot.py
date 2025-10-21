#!/usr/bin/env python3
"""
Discord Giftcode Relay Bot
--------------------------
Dieser Bot:

- beobachtet einen oder mehrere **versteckte Kanäle** (SOURCE_CHANNEL_IDS)
- sucht nach Nachrichten mit Keywords wie "gift code", "redeem", "Chief Concierge", etc.
- erkennt Codes + Ablaufdatum + VIP-Hinweis
- postet dann automatisch eine saubere Nachricht in einen **öffentlichen Kanal**
- merkt sich Codes, damit wiederkehrende Codes als „Recurring gift code!“ gepostet werden
"""

import asyncio
import os
import re
import sqlite3
from datetime import datetime, date
from typing import List, Optional, Tuple

import discord
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web

# ---------- Konfiguration ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Die Kanal-IDs und Keywords werden ausschließlich über Environment Variables gesteuert.
# Beispiel:
#   SOURCE_CHANNEL_IDS = "1430128900758831158,143012890012345678"
#   TARGET_CHANNEL_ID = "1429833113315446807"

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

CODE_REGEX = re.compile(r"\b([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+|[A-Z0-9]{6,25})\b")
EXPIRY_LEADS = re.compile(
    r"(?:expires|expiry|expire|valid\s+until|redeem\s+until|until|valid\s+by|valid\s+thru|through)\s*[:\-]?\s*",
    re.IGNORECASE,
)
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

# ---------- Datenbank ----------
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
        cur.execute(
            "SELECT code, expiry, is_vip FROM giftcodes WHERE code = ?", (code,)
        )
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

# ---------- Hilfsfunktionen ----------
def normalize_code(token: str) -> str:
    return token.upper()

def looks_like_gift_announcement(raw: str) -> bool:
    lower = raw.lower()
    return any(k in lower for k in KEYWORDS)

def extract_expiry(raw: str) -> Optional[date]:
    match = re.search(r"\b(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})\b", raw)
    if match:
        try:
            y, m, d = map(int, match.groups())
            return date(y, m, d)
        except:
            pass
    match2 = re.search(r"\b(\d{1,2})[\/\.](\d{1,2})[\/\.](\d{4})\b", raw)
    if match2:
        try:
            d, m, y = map(int, match2.groups())
            return date(y, m, d)
        except:
            pass
    return None

def find_codes(raw: str) -> List[str]:
    cleaned = re.sub(r"https?://\S+", " ", raw)
    return [normalize_code(m.group(1)) for m in CODE_REGEX.finditer(cleaned)]

def format_date_iso(d: Optional[date]) -> str:
    return d.strftime("%Y/%m/%d") if d else "unbekannt"

# ---------- Discord Bot ----------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_messages = True
bot = commands.Bot(command_prefix="/", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Eingeloggt als {bot.user} (id={bot.user.id})")

async def announce_code(target_channel: discord.TextChannel, code: str, expiry: Optional[date], is_vip: bool, recurring: bool):
    header = "Recurring gift code!" if recurring else "New gift code!"
    lines = [f"{header} `{code}` — redeem until {format_date_iso(expiry)}"]
    if is_vip:
        lines.append(f"VIP12 gift code: `{code}`")
    await target_channel.send("\n".join(lines))

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot:
        return
    if message.channel.id not in SOURCE_CHANNEL_IDS:
        return

    raw = message.content
    if not looks_like_gift_announcement(raw):
        return

    codes = find_codes(raw)
    if not codes:
        return

    expiry = extract_expiry(raw)
    is_vip = bool(VIP_REGEX.search(raw))
    target = bot.get_channel(TARGET_CHANNEL_ID)
    if not isinstance(target, discord.TextChannel):
        print("⚠️ Zielkanal nicht gefunden.")
        return

    now = datetime.utcnow()
    for code in codes:
        is_new, prev_expiry, prev_vip = store.upsert_code(code, now, expiry, is_vip)
        recurring = not is_new
        best_expiry = expiry or prev_expiry
        await announce_code(target, code, best_expiry, is_vip or prev_vip, recurring)

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.reply("pong")

# ---------- Keep-Alive Server (Railway/Replit) ----------
async def _alive_handler(request):
    return web.Response(text="I'm alive!")

async def start_keepalive_server():
    app = web.Application()
    app.router.add_get('/', _alive_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN fehlt!")
    loop = asyncio.get_event_loop()
    loop.create_task(start_keepalive_server())
    bot.run(TOKEN)
