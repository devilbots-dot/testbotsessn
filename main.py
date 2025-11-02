#!/usr/bin/env python3
# main.py — Fixed: Telethon-format Session Creator + OTP Reader (Stable)
# Author: Ankit Edition

import os
import time
import asyncio
import tempfile
import zipfile
import shutil
import re
from pathlib import Path
from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
)
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ---- Time sync ----
try:
    os.environ["TZ"] = "UTC"
    time.tzset()
except Exception:
    pass

print("[UTC TIME]", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))

# ---- Load ENV ----
load_dotenv()
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
if not (API_ID and API_HASH and BOT_TOKEN and OWNER_ID):
    raise SystemExit("Please set API_ID, API_HASH, BOT_TOKEN, OWNER_ID!")

app = Client("otpwatch_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)
STATE = {}

OTP_REGEX = re.compile(r"\b(\d{4,8})\b")
OTP_HINTS = ["otp", "code", "pin", "verify", "verification", "passcode"]


def otp_found(text):
    if not text:
        return False
    if OTP_REGEX.search(text):
        return True
    low = text.lower()
    return any(h in low for h in OTP_HINTS)


def extract_otps(text):
    return OTP_REGEX.findall(text) if text else []


HOME_KB = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("📞 Create Session", callback_data="create_session"),
            InlineKeyboardButton("🔎 Read OTP", callback_data="read_otp"),
        ],
    ]
)


def is_owner(uid):
    return uid == OWNER_ID


def owner_only(func):
    async def wrapper(c, m):
        uid = (
            getattr(m.from_user, "id", None)
            if hasattr(m, "from_user")
            else getattr(getattr(m, "message", None), "from_user", None).id
        )
        if uid != OWNER_ID:
            try:
                if hasattr(m, "answer"):
                    return await m.answer("Access denied.", show_alert=True)
                else:
                    return await m.reply_text("Access denied.")
            except Exception:
                return
        return await func(c, m)

    return wrapper


# ---------------- Telethon Session Create ----------------
async def start_signin(phone, oid):
    safe = re.sub(r"[^0-9]", "_", phone)
    tmp_dir = tempfile.mkdtemp(prefix=f"sess_{safe}_")
    sess_path = Path(tmp_dir) / f"{safe}"

    cli = TelegramClient(str(sess_path), API_ID, API_HASH)
    await cli.connect()
    await cli.get_me()  # sync fix
    try:
        sent = await cli.send_code_request(phone)
        STATE[oid] = {
            "phone": phone,
            "tmp_dir": tmp_dir,
            "sess_path": str(sess_path),
            "phone_code_hash": sent.phone_code_hash,
            "flow": "await_code",
        }
        await app.send_message(oid, "📩 Code sent! Now send the OTP code here.")
    except Exception as e:
        await app.send_message(oid, f"❌ Error: {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        STATE.pop(oid, None)
        await cli.disconnect()


async def complete_signin(oid, code):
    st = STATE.get(oid, {})
    phone = st.get("phone")
    tmp_dir = st.get("tmp_dir")
    sess_path = st.get("sess_path")
    pch = st.get("phone_code_hash")

    cli = TelegramClient(sess_path, API_ID, API_HASH)
    await cli.connect()
    await cli.get_me()
    try:
        await cli.sign_in(phone=phone, code=code, phone_code_hash=pch)
    except SessionPasswordNeededError:
        STATE[oid]["flow"] = "await_2fa"
        await app.send_message(oid, "🔐 2FA required. Please send your password.")
        await cli.disconnect()
        return
    except PhoneCodeInvalidError:
        await app.send_message(oid, "❌ Invalid code. Start again.")
        await cli.disconnect()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        STATE.pop(oid, None)
        return
    except PhoneCodeExpiredError:
        await app.send_message(oid, "❌ Code expired. Start again.")
        await cli.disconnect()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        STATE.pop(oid, None)
        return

    session_file = Path(str(sess_path) + ".session")
    if session_file.exists():
        await app.send_document(oid, str(session_file), caption="✅ Session created!")
        await app.send_message(oid, "Session file sent. Delete it only after saving.")
    else:
        await app.send_message(oid, "⚠️ Login success but file not found!")

    await cli.disconnect()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    STATE.pop(oid, None)


async def complete_signin_2fa(oid, pwd):
    st = STATE.get(oid, {})
    sess_path = st.get("sess_path")
    tmp_dir = st.get("tmp_dir")

    cli = TelegramClient(sess_path, API_ID, API_HASH)
    await cli.connect()
    await cli.get_me()
    try:
        await cli.sign_in(password=pwd)
        session_file = Path(str(sess_path) + ".session")
        if session_file.exists():
            await app.send_document(oid, str(session_file), caption="✅ 2FA Success! Session saved.")
        else:
            await app.send_message(oid, "2FA success but session file not found.")
    except Exception as e:
        await app.send_message(oid, f"2FA error: {e}")
    await cli.disconnect()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    STATE.pop(oid, None)


# ---------------- OTP Reader ----------------
async def scan_otps(oid, file_path):
    f = Path(file_path)
    cli = TelegramClient(str(f.with_suffix("")), API_ID, API_HASH)
    try:
        await cli.connect()
        await cli.get_me()
        if not await cli.is_user_authorized():
            await app.send_message(oid, "⚠️ Session unauthorized or expired.")
            return
        found = []
        async for msg in cli.iter_messages(None, limit=200):
            if msg and getattr(msg, "message", None):
                txt = msg.message
                if otp_found(txt):
                    otps = extract_otps(txt)
                    preview = txt[:180]
                    found.append(f"🔔 {', '.join(otps)} — `{preview}`")
                    if len(found) >= 25:
                        break
        if not found:
            await app.send_message(oid, "🔕 No OTP messages found.")
        else:
            await app.send_message(oid, "\n\n".join(found))
    except Exception as e:
        await app.send_message(oid, f"❌ Error reading session: {e}")
    finally:
        await cli.disconnect()
        try:
            f.unlink()
        except:
            pass


# ---------------- Handlers ----------------
@app.on_message(filters.command("start") & filters.private)
@owner_only
async def start_cmd(c, m):
    await m.reply_text("👋 Welcome! Use options below.", reply_markup=HOME_KB)


@app.on_callback_query()
@owner_only
async def cb(c, q):
    data = q.data
    if data == "create_session":
        STATE[OWNER_ID] = {"flow": "phone"}
        await q.message.reply_text("📱 Send phone number (e.g. +91xxxxxxxxxx)")
    elif data == "read_otp":
        STATE[OWNER_ID] = {"flow": "await_session"}
        await q.message.reply_text("📂 Upload your .session file (Telethon format).")
    await q.answer()


@app.on_message(filters.private & filters.text)
@owner_only
async def on_text(c, m):
    st = STATE.get(OWNER_ID, {})
    flow = st.get("flow")
    if flow == "phone":
        phone = m.text.strip()
        await start_signin(phone, OWNER_ID)
    elif flow == "await_code":
        code = m.text.strip()
        await complete_signin(OWNER_ID, code)
    elif flow == "await_2fa":
        pwd = m.text.strip()
        await complete_signin_2fa(OWNER_ID, pwd)
    else:
        await m.reply_text("Use /start and choose an action.")


@app.on_message(filters.private & filters.document)
@owner_only
async def on_doc(c, m):
    st = STATE.get(OWNER_ID, {})
    if st.get("flow") == "await_session":
        tmp = tempfile.mkdtemp(prefix="sess_in_")
        file_path = Path(tmp) / m.document.file_name
        await m.download(file_name=str(file_path))
        if zipfile.is_zipfile(str(file_path)):
            with zipfile.ZipFile(file_path, "r") as z:
                z.extractall(tmp)
            found = list(Path(tmp).glob("*.session"))
            if not found:
                await m.reply_text("No .session file found in ZIP.")
                shutil.rmtree(tmp, ignore_errors=True)
                return
            file_path = found[0]
        await m.reply_text("🔍 Reading OTPs... please wait.")
        await scan_otps(OWNER_ID, str(file_path))
        shutil.rmtree(tmp, ignore_errors=True)
        STATE.pop(OWNER_ID, None)
    else:
        await m.reply_text("Press 'Read OTP' first in /start menu.")


if __name__ == "__main__":
    print("🚀 Running stable OTP bot...")
    app.run()
