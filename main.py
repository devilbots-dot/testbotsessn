#!/usr/bin/env python3
# main.py — Fixed for Heroku BadMsgNotification + Auto Restart + Time Sync
# No logic changed, only stable enhancements added.

import os
import asyncio
import tempfile
import zipfile
import shutil
import re
import time
import datetime
import subprocess
from pathlib import Path
from dotenv import load_dotenv

# ----- FIX HEROKU TIME SYNC -----
print("[⏱] Fixing Heroku clock sync...")

try:
    os.environ["TZ"] = "UTC"
    time.tzset()
    print("[TIME SYNC OK] Current UTC:", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
except Exception as e:
    print("[⚠️] Time sync fallback:", e)
# ---------------------------------

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError

load_dotenv()

# ---------------- CONFIG ----------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

SESSION_FOLDER = Path(os.getenv("SESSION_FOLDER", "sessions"))
SESSION_FOLDER.mkdir(parents=True, exist_ok=True)
OTP_LOG_FOLDER = Path(os.getenv("OTP_LOG_FOLDER", "otp_logs"))
OTP_LOG_FOLDER.mkdir(parents=True, exist_ok=True)

WATCHER_CONCURRENCY = int(os.getenv("WATCHER_CONCURRENCY", "6"))

if not (API_ID and API_HASH and BOT_TOKEN and OWNER_ID):
    raise SystemExit("Please set API_ID, API_HASH, BOT_TOKEN and OWNER_ID in environment variables (.env)")

OTP_RE = re.compile(r"\b(\d{4,8})\b")
OTP_HINTS = ["code", "otp", "verification", "pin", "one-time", "passcode"]

# Pyrogram Bot App
app = Client("sessmgr_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

STATE = {}
WATCHER_TASKS = []
WATCHER_RUNNING = False
WATCHER_CLIENTS = []

HOME_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔐 Create Session", callback_data="create_session")],
    [InlineKeyboardButton("📁 Upload session.zip (list)", callback_data="upload_list")],
    [InlineKeyboardButton("🕵️ Start Watcher (read OTPs)", callback_data="start_watcher")],
    [InlineKeyboardButton("⛔ Stop Watcher", callback_data="stop_watcher")],
    [InlineKeyboardButton("ℹ️ Status", callback_data="status")],
])

# ---------------- Helper ----------------
def owner_only(func):
    async def wrapper(c, m):
        user_id = getattr(m.from_user, "id", None)
        if user_id != OWNER_ID:
            try:
                await m.reply_text("Access denied — only owner can use this bot.")
            except:
                pass
            return
        return await func(c, m)
    return wrapper

async def notify_owner_text(text):
    try:
        await app.send_message(OWNER_ID, text)
    except Exception as e:
        print("Notify owner failed:", e)

def is_otp_like(text: str) -> bool:
    if not text:
        return False
    tl = text.lower()
    if OTP_RE.search(text):
        return True
    for h in OTP_HINTS:
        if h in tl:
            return True
    return False

def log_otp(session_name: str, line: str):
    path = OTP_LOG_FOLDER / f"{session_name}.log"
    with open(path, "a", encoding="utf8") as fh:
        fh.write(line + "\n")

# ---------------- Bot Commands ----------------
@app.on_message(filters.private & filters.command("start"))
async def start_cmd(c, m):
    if m.from_user.id != OWNER_ID:
        await m.reply_text("Access denied.")
        return
    await m.reply_text("Welcome — choose an action:", reply_markup=HOME_KB)

@app.on_callback_query()
async def cb_handler(c, cb):
    user_id = cb.from_user.id
    if user_id != OWNER_ID:
        await cb.answer("Access denied", show_alert=True)
        return

    data = cb.data
    if data == "create_session":
        STATE[OWNER_ID] = {"flow": "await_phone"}
        await cb.message.reply_text("Send the phone number (international format), e.g. `+9198xxxxxxx`")
        await cb.answer()
        return

    if data == "upload_list":
        STATE[OWNER_ID] = {"flow": "await_zip_list"}
        await cb.message.reply_text("Please upload a ZIP file containing .session/.sqlite/.db files.")
        await cb.answer()
        return

    if data == "start_watcher":
        if WATCHER_RUNNING:
            await cb.answer("Watcher already running", show_alert=True)
            return
        STATE[OWNER_ID] = {"flow": "await_zip_watcher"}
        await cb.message.reply_text("Upload your `session.zip` for OTP reading on VPS.")
        await cb.answer()
        return

    if data == "stop_watcher":
        if not WATCHER_RUNNING:
            await cb.answer("Watcher not running", show_alert=True)
            return
        await cb.answer("Stopping watcher...")
        await stop_watcher()
        await cb.message.reply_text("Watcher stopped.")
        return

    if data == "status":
        txt = f"Watcher running: {WATCHER_RUNNING}\nStored sessions: {len(list(SESSION_FOLDER.glob('*')))}\nOTP logs dir: {OTP_LOG_FOLDER.resolve()}"
        await cb.answer()
        await cb.message.reply_text(txt)
        return

    await cb.answer()

# ---------------- Flow Handlers ----------------
@app.on_message(filters.private & filters.text)
async def text_flow(c, m):
    if m.from_user.id != OWNER_ID:
        await m.reply_text("Access denied.")
        return

    st = STATE.get(OWNER_ID)
    if not st:
        await m.reply_text("Use /start to begin.")
        return

    flow = st.get("flow")
    if flow == "await_phone":
        phone = m.text.strip()
        if not phone.startswith("+") or len(phone) < 6:
            await m.reply_text("Send phone in international format, e.g. +9198xxxxxxx")
            return
        st.update({"phone": phone, "subflow": "sent_code"})
        await m.reply_text("Requesting code... (may take a few seconds)")
        asyncio.create_task(initiate_send_code(phone, OWNER_ID))
        return

    if flow == "await_otp_for_signin":
        otp = m.text.strip()
        st.update({"otp": otp})
        await m.reply_text("Received OTP — attempting to complete sign-in...")
        await complete_signin_flow(OWNER_ID)
        return

    if flow == "await_2fa":
        pwd = m.text.strip()
        st.update({"2fa": pwd})
        await m.reply_text("Received 2FA password — attempting to complete sign-in...")
        await complete_signin_flow(OWNER_ID)
        return

    await m.reply_text("Unhandled flow state. Use /start again.")

@app.on_message(filters.private & filters.document)
async def doc_flow(c, m):
    if m.from_user.id != OWNER_ID:
        await m.reply_text("Access denied.")
        return

    st = STATE.get(OWNER_ID)
    if not st:
        await m.reply_text("Use /start to begin.")
        return

    file_name = m.document.file_name.lower()
    tmpdir = Path(tempfile.mkdtemp())
    zip_path = tmpdir / (m.document.file_name or "upload.zip")
    await m.download(file_id=m.document.file_id, file_name=str(zip_path))

    if st.get("flow") == "await_zip_list":
        found = []
        try:
            with zipfile.ZipFile(str(zip_path), "r") as z:
                for info in z.infolist():
                    if info.filename.lower().endswith((".session", ".sqlite", ".db")):
                        found.append(info.filename)
        except Exception as e:
            await m.reply_text(f"Error reading ZIP: {e}")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return

        msg = "Found session files:\n" + ("\n".join(found) if found else "None found.")
        await m.reply_text(msg)
        shutil.rmtree(tmpdir, ignore_errors=True)
        STATE.pop(OWNER_ID, None)
        return

    if st.get("flow") == "await_zip_watcher":
        try:
            extract_to = tmpdir / "extracted"
            extract_to.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(str(zip_path), "r") as z:
                z.extractall(extract_to)

            moved = []
            for p in extract_to.rglob("*"):
                if p.is_file() and (p.suffix.lower() in (".session", ".sqlite", ".db") or ".session" in p.name.lower()):
                    dest = SESSION_FOLDER / p.name
                    shutil.move(str(p), str(dest))
                    moved.append(dest.name)

            await m.reply_text(f"Saved {len(moved)} session files to `{SESSION_FOLDER}`. Starting watcher now...")
            STATE.pop(OWNER_ID, None)
            await start_watcher()
        except Exception as e:
            await m.reply_text(f"Error preparing watcher: {e}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return

    await m.reply_text("No action for this document. Use /start.")

# ---------------- SAFE MAIN LOOP ----------------
if __name__ == "__main__":
    print("Starting Session Manager Bot...")

    while True:
        try:
            app.run()
        except Exception as e:
            print(f"[❌] Bot crashed: {e}")
            if "msg_id is too low" in str(e).lower():
                print("[🩹] Time desync fix triggered — retrying...")
                time.sleep(3)
                continue
            print("[♻️] Restarting in 5 seconds...")
            time.sleep(5)
