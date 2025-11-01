#!/usr/bin/env python3
# main.py
# Integrated bot: create session (interactive) + OTP watcher (runs on same VPS)
# Requirements: pyrogram, telethon, python-dotenv, cryptography(optional), aiofiles

import os
import asyncio
import tempfile
import zipfile
import shutil
import re
from pathlib import Path
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError

load_dotenv()

# ---------------- CONFIG (from env) ----------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # your telegram user id (owner)
SESSION_FOLDER = Path(os.getenv("SESSION_FOLDER", "sessions"))
SESSION_FOLDER.mkdir(parents=True, exist_ok=True)
OTP_LOG_FOLDER = Path(os.getenv("OTP_LOG_FOLDER", "otp_logs"))
OTP_LOG_FOLDER.mkdir(parents=True, exist_ok=True)
WATCHER_CONCURRENCY = int(os.getenv("WATCHER_CONCURRENCY", "6"))  # max concurrent telethon clients at a time

if not (API_ID and API_HASH and BOT_TOKEN and OWNER_ID):
    raise SystemExit("Please set API_ID, API_HASH, BOT_TOKEN and OWNER_ID in environment variables (.env)")

# OTP detection regex and hints
OTP_RE = re.compile(r"\b(\d{4,8})\b")
OTP_HINTS = ["code", "otp", "verification", "pin", "one-time", "one time", "passcode"]

# Pyrogram bot app
app = Client("sessmgr_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# in-memory state per owner flow
STATE = {}

# watcher global tasks and clients
WATCHER_TASKS = []
WATCHER_RUNNING = False
WATCHER_CLIENTS = []

# keyboard
HOME_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔐 Create Session", callback_data="create_session")],
    [InlineKeyboardButton("📁 Upload session.zip (list)", callback_data="upload_list")],
    [InlineKeyboardButton("🕵️ Start Watcher (read OTPs)", callback_data="start_watcher")],
    [InlineKeyboardButton("⛔ Stop Watcher", callback_data="stop_watcher")],
    [InlineKeyboardButton("ℹ️ Status", callback_data="status")],
])

# ---------------- helper functions ----------------
def owner_only(func):
    async def wrapper(c, m):
        user_id = (m.from_user.id if hasattr(m, "from_user") else (m.from_user.id if hasattr(m, "from_user") else None))
        # callback_query has .from_user, messages have .from_user
        if user_id != OWNER_ID:
            # reply politely and ignore
            try:
                await m.reply_text("Access denied — only owner can use this bot.")
            except:
                pass
            return
        return await func(c, m)
    return wrapper

async def notify_owner_text(text):
    """Send text message to owner (async)."""
    try:
        await app.send_message(OWNER_ID, text)
    except Exception as e:
        print("Failed to notify owner:", e)

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

# ---------------- Bot commands / callbacks ----------------
@app.on_message(filters.private & filters.command("start"))
@filters.me  # not necessary but keep; we'll check owner below
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
        await cb.message.reply_text("Please upload a ZIP file containing .session/.sqlite/.db files. I will list them (I will NOT auto-login).")
        await cb.answer()
        return
    if data == "start_watcher":
        if WATCHER_RUNNING:
            await cb.answer("Watcher already running", show_alert=True)
            return
        STATE[OWNER_ID] = {"flow": "await_zip_watcher"}
        await cb.message.reply_text("Upload your `session.zip` (or send a ZIP of session files). I will extract and start watcher on the VPS (owner-only).")
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

# ---------------- message handlers for flows ----------------
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
        # basic validation
        if not phone.startswith("+") or len(phone) < 6:
            await m.reply_text("Send phone in international format, e.g. +9198xxxxxxx")
            return
        # store and start send_code
        st.update({"phone": phone, "subflow": "sent_code"})
        await m.reply_text("Requesting code... (may take a few seconds)")
        asyncio.create_task(initiate_send_code(phone, OWNER_ID))
        return

    if flow == "await_otp_for_signin":
        # used when waiting for OTP during create session signin
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

    await m.reply_text("Unhandled flow state. Use /start to restart.")

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
        # just list sessions
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
        if not found:
            await m.reply_text("No session files found inside the ZIP.")
        else:
            msg = "Found session files:\n" + "\n".join(found)
            await m.reply_text(msg)
        shutil.rmtree(tmpdir, ignore_errors=True)
        STATE.pop(OWNER_ID, None)
        return

    if st.get("flow") == "await_zip_watcher":
        # save uploaded zip into sessions folder and start watcher
        try:
            extract_to = tmpdir / "extracted"
            extract_to.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(str(zip_path), "r") as z:
                z.extractall(extract_to)
            # move session files to SESSION_FOLDER
            moved = []
            for p in extract_to.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".session", ".sqlite", ".db") or ".session" in p.name.lower():
                    dest = SESSION_FOLDER / p.name
                    shutil.move(str(p), str(dest))
                    moved.append(dest.name)
            if not moved:
                # fallback: move all files
                for p in extract_to.rglob("*"):
                    if p.is_file():
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

# ---------------- Create Session (Telethon interactive flow) ----------------
async def initiate_send_code(phone: str, user_id: int):
    """Sends code request using a temporary Telethon client."""
    st = STATE.get(user_id)
    tmp_name = f"temp_{user_id}_{phone.replace('+','')}"
    tmp_session_path = SESSION_FOLDER / f"{tmp_name}.tmp"
    client = TelegramClient(str(tmp_session_path), API_ID, API_HASH)
    try:
        await client.connect()
        res = await client.send_code_request(phone)
        # store phone_code_hash and tmp session path
        st.update({"phone_code_hash": getattr(res, "phone_code_hash", None), "tmp_session_path": str(tmp_session_path), "flow": "await_otp_for_signin"})
        await notify_owner_text("Code request sent. Please paste the OTP here in chat.")
    except Exception as e:
        await notify_owner_text(f"Failed to send code request: {e}")
        STATE.pop(user_id, None)
    finally:
        await client.disconnect()

async def complete_signin_flow(user_id: int):
    st = STATE.get(user_id)
    if not st:
        await notify_owner_text("No active signin flow.")
        return
    phone = st.get("phone")
    code = st.get("otp")
    tmp_session_path = st.get("tmp_session_path")
    phone_code_hash = st.get("phone_code_hash")
    if not (phone and code and tmp_session_path):
        await notify_owner_text("Missing data for signin. Start over.")
        STATE.pop(user_id, None)
        return

    client = TelegramClient(tmp_session_path, API_ID, API_HASH)
    try:
        await client.connect()
        try:
            # Telethon sign_in; prefer using phone and code
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            # require 2FA password
            STATE[user_id]["flow"] = "await_2fa"
            await notify_owner_text("This account has 2FA enabled. Reply with the 2FA password here.")
            await client.disconnect()
            return
        except PhoneCodeInvalidError:
            await notify_owner_text("The code you provided was invalid. Start over.")
            await client.disconnect()
            STATE.pop(user_id, None)
            return
        except PhoneCodeExpiredError:
            await notify_owner_text("The code expired. Start over.")
            await client.disconnect()
            STATE.pop(user_id, None)
            return

        # success: Telethon has created a session file at tmp_session_path(.session)
        # find actual file and move to sessions folder
        candidates = []
        base = Path(tmp_session_path)
        for ext in (".session", ".sqlite", ".db", ""):
            p = Path(str(base) + ext)
            if p.exists():
                candidates.append(p)
        if not candidates and base.exists():
            candidates.append(base)

        if not candidates:
            await notify_owner_text("Sign-in succeeded but session file not found. Client may hold auth in-memory.")
            await client.disconnect()
            STATE.pop(user_id, None)
            return

        final = candidates[0]
        dest = SESSION_FOLDER / final.name
        # ensure unique name to avoid overwrite
        if dest.exists():
            # append suffix
            i = 1
            while True:
                d2 = SESSION_FOLDER / f"{final.stem}_{i}{final.suffix}"
                if not d2.exists():
                    dest = d2
                    break
                i += 1
        # move file
        shutil.move(str(final), str(dest))
        await notify_owner_text(f"Session created and saved as `{dest.name}` in sessions folder.")
    except Exception as e:
        await notify_owner_text(f"Sign-in failed: {e}")
    finally:
        try:
            await client.disconnect()
        except:
            pass
        STATE.pop(user_id, None)

# ---------------- Watcher logic ----------------
async def watcher_client_task(session_path: str):
    """Starts a Telethon client for given session file and listens for incoming messages and OTPs."""
    p = Path(session_path)
    session_name = p.stem or p.name
    client = TelegramClient(str(p), API_ID, API_HASH)
    try:
        await client.connect()
    except Exception as e:
        print(f"[{session_name}] connect error: {e}")
        return None
    try:
        if not await client.is_user_authorized():
            print(f"[{session_name}] not authorized/expired — skipping.")
            await client.disconnect()
            return None
    except Exception as e:
        print(f"[{session_name}] auth-check error: {e}")
        await client.disconnect()
        return None

    # map identity
    me = None
    try:
        me = await client.get_me()
    except Exception:
        pass
    display = session_name
    if me:
        display = f"{getattr(me,'username',None) or getattr(me,'id',session_name)}"

    print(f"[{display}] watcher started.")

    @client.on(events.NewMessage(incoming=True))
    async def incoming_handler(event):
        try:
            text = event.raw_text or ""
            if not is_otp_like(text):
                return
            codes = OTP_RE.findall(text)
            codes = list(dict.fromkeys(codes))
            if codes:
                for code in codes:
                    line = f"[{display}] OTP: {code} | from: {event.sender_id} | msg: {text}"
                    print(line)
                    log_otp(session_name, line)
                    # notify owner via bot
                    try:
                        await app.send_message(OWNER_ID, f"OTP for {display}: `{code}`\nFull msg: {text}")
                    except Exception as e:
                        print("Failed to send owner notify via bot:", e)
            else:
                line = f"[{display}] Message with OTP-hint | from: {event.sender_id} | msg: {text}"
                print(line)
                log_otp(session_name, line)
        except Exception as ex:
            print(f"[{display}] handler exception: {ex}")

    WATCHER_CLIENTS.append(client)
    # keep running until disconnected
    try:
        await client.run_until_disconnected()
    except Exception as e:
        print(f"[{display}] run_until_disconnected ended: {e}")
    finally:
        try:
            await client.disconnect()
        except:
            pass
    return

async def start_watcher():
    global WATCHER_RUNNING, WATCHER_TASKS, WATCHER_CLIENTS
    if WATCHER_RUNNING:
        await notify_owner_text("Watcher already running.")
        return
    # gather session files
    session_files = [str(p) for p in SESSION_FOLDER.iterdir() if p.is_file()]
    if not session_files:
        await notify_owner_text("No session files found in sessions folder.")
        return
    WATCHER_RUNNING = True
    WATCHER_CLIENTS = []
    WATCHER_TASKS = []
    # create tasks with limited concurrency
    sem = asyncio.Semaphore(WATCHER_CONCURRENCY)
    async def start_with_sem(path):
        async with sem:
            return await watcher_client_task(path)
    for p in session_files:
        t = asyncio.create_task(start_with_sem(p))
        WATCHER_TASKS.append(t)
    await notify_owner_text(f"Watcher started on {len(session_files)} sessions. You will receive OTPs here.")
    # don't await tasks here — they run in background
    return

async def stop_watcher():
    global WATCHER_RUNNING, WATCHER_TASKS, WATCHER_CLIENTS
    if not WATCHER_RUNNING:
        return
    # disconnect all clients
    for c in WATCHER_CLIENTS:
        try:
            await c.disconnect()
        except:
            pass
    # cancel tasks
    for t in WATCHER_TASKS:
        try:
            t.cancel()
        except:
            pass
    WATCHER_CLIENTS = []
    WATCHER_TASKS = []
    WATCHER_RUNNING = False
    await notify_owner_text("Watcher stopped.")

# ---------------- Startup ----------------
if __name__ == "__main__":
    print("Starting Session Manager Bot...")
    # run pyrogram app — it will keep event loop running and Telethon tasks will operate in same loop
    app.run()
