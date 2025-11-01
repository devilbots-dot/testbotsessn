#!/usr/bin/env python3
# main.py — Minimal Secure Session Creator + OTP-reader (no persistent sessions)

import os
import time
import asyncio
import tempfile
import zipfile
import shutil
import re
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ---- Time sync ----
try:
    os.environ["TZ"] = "UTC"
    time.tzset()
except:
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

# ---- Globals / App ----
app = Client("otpwatch_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)
STATE = {}  # simple per-owner flow state

# OTP detection
OTP_REGEX = re.compile(r"\b(\d{4,8})\b")
OTP_HINTS = ["otp", "code", "pin", "verify", "verification", "passcode"]

def is_owner(uid): return uid == OWNER_ID

async def notify(text):
    try:
        await app.send_message(OWNER_ID, text)
    except Exception:
        pass

def otp_found(text):
    if not text: return False
    if OTP_REGEX.search(text): return True
    low = text.lower()
    return any(h in low for h in OTP_HINTS)

def extract_otps(text):
    return OTP_REGEX.findall(text) if text else []

# ---- Keyboards (simplified) ----
HOME_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("📞 Create Session", callback_data="create_session"),
     InlineKeyboardButton("🔎 Read OTP from session", callback_data="read_otp")],
    [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
])

# ---- owner-only decorator ----
def owner_only(func):
    async def wrap(c, m):
        # m may be Message or CallbackQuery — unify check
        uid = None
        if hasattr(m, "from_user") and m.from_user:
            uid = m.from_user.id
        elif hasattr(m, "from_user") and m.from_user:
            uid = m.from_user.id
        elif hasattr(m, "from_user") and m.from_user:
            uid = m.from_user.id
        # fallback (pyrogram callback query has .from_user)
        if uid is None or not is_owner(uid):
            try:
                return await m.reply_text("Access denied.")
            except:
                return
        return await func(c, m)
    return wrap

# ---- Sign-in flow: send code request ----
async def start_signin(phone, oid):
    # Use a temporary session path (not kept)
    safe_name = re.sub(r'[^0-9]', '_', phone)
    tmp_dir = tempfile.mkdtemp()
    tmp_path = Path(tmp_dir) / f"tmp_{safe_name}"
    cli = TelegramClient(str(tmp_path), API_ID, API_HASH)
    try:
        await cli.connect()
        sent = await cli.send_code_request(phone)
        STATE[oid] = {"phone": phone, "tmp_path": str(tmp_path), "phone_code_hash": sent.phone_code_hash, "flow": "await_code"}
        await app.send_message(oid, "📩 Code request sent. Ab bot ko wahi code bhejo (private chat).")
    except Exception as e:
        await app.send_message(oid, f"❌ Error sending code: {e}")
        try:
            await cli.disconnect()
        except: pass

# ---- Complete sign-in with code and send created .session to owner, then delete ----
async def complete_signin(oid, code):
    st = STATE.get(oid, {})
    phone = st.get("phone")
    tmp_path = st.get("tmp_path")
    pch = st.get("phone_code_hash")
    if not all([phone, tmp_path, code]):
        await app.send_message(oid, "Missing signin state. Start again.")
        STATE.pop(oid, None)
        return

    cli = TelegramClient(tmp_path, API_ID, API_HASH)
    try:
        await cli.connect()
        try:
            await cli.sign_in(phone=phone, code=code, phone_code_hash=pch)
        except SessionPasswordNeededError:
            # 2FA required
            STATE[oid]["flow"] = "await_2fa"
            await app.send_message(oid, "🔐 2FA is enabled on this account. Send the 2FA password now.")
            await cli.disconnect()
            return
        # sign-in succeeded. telethon stores session as tmp_path + ".session"
        session_file = Path(str(tmp_path) + ".session")
        if not session_file.exists():
            # fallback: telethon may use different extension; try listing tmp dir
            # but in most cases .session exists
            files = list(Path(tmp_path).parent.glob("*.session"))
            session_file = files[0] if files else None

        if session_file and session_file.exists():
            # send to owner
            await app.send_document(oid, str(session_file), caption=f"✅ Session for {phone} (temporary). Keep it safe!")
            # delete immediately (do not store)
            try:
                session_file.unlink()
            except: pass
        else:
            await app.send_message(oid, "⚠️ Session file not found, but login succeeded.")

        await notify(f"✅ Sign-in completed for {phone}. Session file sent to owner.")
    except Exception as e:
        await app.send_message(oid, f"Error completing sign-in: {e}")
    finally:
        try:
            await cli.disconnect()
        except:
            pass
        # cleanup tmp dir if exists
        try:
            td = Path(tmp_path).parent
            if td.exists():
                shutil.rmtree(td, ignore_errors=True)
        except:
            pass
        STATE.pop(oid, None)

# ---- Handler: Read OTP from an uploaded .session file (temporary use) ----
# Flow: user presses "Read OTP from session" -> bot asks to upload .session file -> user sends document -> bot downloads, uses it to connect and scans recent messages for OTPs
async def process_session_for_otps(owner_uid, session_file_path):
    # session_file_path is a local filesystem path to e.g. "something.session"
    tmp_session = Path(session_file_path)
    name = tmp_session.stem
    await app.send_message(owner_uid, f"🔎 Connecting with provided session `{tmp_session.name}` and scanning recent messages...")
    cli = TelegramClient(str(tmp_session.with_suffix('')), API_ID, API_HASH)  # pass path without .session suffix is ok
    try:
        await cli.connect()
        if not await cli.is_user_authorized():
            await app.send_message(owner_uid, "⚠️ Provided session is not authorized / invalid.")
            await cli.disconnect()
            return
    except Exception as e:
        await app.send_message(owner_uid, f"Connection error: {e}")
        try: await cli.disconnect()
        except: pass
        return

    found = []
    try:
        # iterate recent incoming messages globally (limit adjustable)
        # telethon supports client.iter_messages(None, limit=N) to iterate across dialogs
        async for msg in cli.iter_messages(None, limit=350):  # scan last 350 messages across chats
            if not msg.message:
                continue
            text = msg.message
            if otp_found(text):
                otps = extract_otps(text)
                chats = getattr(msg, "chat_id", None) or getattr(msg, "peer_id", None)
                preview = text if len(text) < 200 else text[:197] + "..."
                found.append((otps, preview, getattr(msg, "date", None)))
        if not found:
            await app.send_message(owner_uid, "🔕 No OTP-like messages found in the scanned recent messages.")
        else:
            # send results (limit size)
            total = 0
            for otps, preview, date in found:
                total += 1
                await app.send_message(owner_uid, f"🔔 Found OTP(s): {', '.join(otps)}\nMsg preview: `{preview}`")
                if total >= 25:  # safety cap
                    await app.send_message(owner_uid, "ℹ️ Reached reporting cap (25). Stop.")
                    break
    except Exception as e:
        await app.send_message(owner_uid, f"Error scanning messages: {e}")
    finally:
        try:
            await cli.disconnect()
        except:
            pass
        # remove local session file
        try:
            tmp_session.unlink()
        except:
            pass

# ---- Bot Command / Callback Handlers ----
@app.on_message(filters.private & filters.command("start"))
@owner_only
async def start_cmd(c, m):
    await m.reply_text("Welcome Owner! Use the buttons below.", reply_markup=HOME_KB)

@app.on_callback_query()
@owner_only
async def cb(c, cbq):
    data = cbq.data
    if data == "create_session":
        STATE[OWNER_ID] = {"flow": "phone"}
        await cbq.message.reply_text("Send phone in international format, e.g. +91xxxxxxxxxx")
        await cbq.answer()
    elif data == "read_otp":
        STATE[OWNER_ID] = {"flow": "await_session_file"}
        await cbq.message.reply_text("Please upload the `.session` file as a document (don't zip). Bot will temporarily use it to read recent messages for OTPs and then delete it.")
        await cbq.answer()
    elif data == "help":
        await cbq.message.reply_text("Flow:\n• Create Session → send phone → receive code → send code → you'll get .session file in chat (bot won't store it)\n• Read OTP from session → upload your .session file → bot scans recent messages and returns OTP-like messages.")
        await cbq.answer()

@app.on_message(filters.private & filters.text)
@owner_only
async def txt(c, m):
    st = STATE.get(OWNER_ID, {})
    flow = st.get("flow")
    if flow == "phone":
        phone = m.text.strip()
        STATE[OWNER_ID] = {"flow": "await_code", "phone": phone}
        await m.reply_text("Sending OTP request to Telegram...")
        asyncio.create_task(start_signin(phone, OWNER_ID))
    elif flow == "await_code":
        code = m.text.strip()
        asyncio.create_task(complete_signin(OWNER_ID, code))
        await m.reply_text("Trying to complete sign-in with provided code...")
    elif flow == "await_2fa":
        # user provided 2FA password; try to sign in using it
        pwd = m.text.strip()
        st2 = STATE.get(OWNER_ID, {})
        tmp_path = st2.get("tmp_path")
        if not tmp_path:
            await m.reply_text("Session temp path missing. Start over.")
            STATE.pop(OWNER_ID, None)
            return
        cli = TelegramClient(tmp_path, API_ID, API_HASH)
        try:
            await cli.connect()
            await cli.sign_in(password=pwd)
            session_file = Path(str(tmp_path) + ".session")
            await app.send_document(OWNER_ID, str(session_file), caption="✅ Session created (2FA). Keep it safe!")
            try:
                session_file.unlink()
            except: pass
            await m.reply_text("2FA sign-in complete. Session sent.")
        except Exception as e:
            await m.reply_text(f"2FA sign-in error: {e}")
        finally:
            try:
                await cli.disconnect()
            except: pass
            try:
                shutil.rmtree(Path(tmp_path).parent, ignore_errors=True)
            except: pass
            STATE.pop(OWNER_ID, None)
    else:
        await m.reply_text("Use /start and buttons to begin.")

@app.on_message(filters.private & filters.document)
@owner_only
async def on_document(c, m):
    st = STATE.get(OWNER_ID, {})
    flow = st.get("flow")
    # If the owner uploaded a .session file in read_otp flow
    if flow == "await_session_file":
        # Download the document to temp
        tmpd = Path(tempfile.mkdtemp())
        local = tmpd / m.document.file_name
        await m.download(file_name=str(local))
        # Basic validation: must end with .session (or just accept)
        if not local.suffix == ".session":
            # try to accept zip containing .session
            if zipfile.is_zipfile(str(local)):
                try:
                    with zipfile.ZipFile(str(local), "r") as z:
                        z.extractall(tmpd)
                    # find first .session inside
                    found = list(tmpd.glob("*.session"))
                    if found:
                        local = found[0]
                    else:
                        await m.reply_text("No .session file inside the zip. Send a raw .session file.")
                        shutil.rmtree(tmpd, ignore_errors=True)
                        STATE.pop(OWNER_ID, None)
                        return
                except Exception as e:
                    await m.reply_text(f"Error extracting zip: {e}")
                    shutil.rmtree(tmpd, ignore_errors=True)
                    STATE.pop(OWNER_ID, None)
                    return
            else:
                await m.reply_text("Please upload a `.session` file (not other file types).")
                shutil.rmtree(tmpd, ignore_errors=True)
                STATE.pop(OWNER_ID, None)
                return

        # now process session file for OTPs (async)
        asyncio.create_task(process_session_for_otps(OWNER_ID, str(local)))
        await m.reply_text("Session received. Scanning for OTPs — results will be posted here.")
        # cleanup state (we won't keep file)
        STATE.pop(OWNER_ID, None)
    else:
        await m.reply_text("If you want bot to read OTP from a session, first press 'Read OTP from session' button in /start.")

# ---- Run ----
if __name__ == "__main__":
    print("🚀 Running minimal devil session bot (no persistent sessions)...")
    while True:
        try:
            app.run()
        except Exception as e:
            print("[BOT CRASH]", e)
            time.sleep(5)
