#!/usr/bin/env python3
# main.py — Updated: Robust Session Creator + OTP-reader (Telethon + Pyrogram compatible)
# Behavior:
# - Create session (temporary dir) -> send code -> accept code -> accept 2FA password if required -> send .session to owner -> delete temp
# - Read OTP from uploaded .session or .zip: try Telethon first, then Pyrogram (supports sessions created by either). Scans recent messages and reports OTP-like matches.

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
from pyrogram import Client, errors as py_errors
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram import filters

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

# ---- App ----
app = Client("otpwatch_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)
STATE = {}  # per-owner flow state; keeps tmp_dir until flow completes

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

# ---- Keyboards ----
HOME_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("📞 Create Session", callback_data="create_session"),
     InlineKeyboardButton("🔎 Read OTP from session", callback_data="read_otp")],
    [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
])

# ---- owner-only decorator (works for Message and CallbackQuery) ----
def owner_only(func):
    async def wrap(c, m):
        uid = None
        # Message vs CallbackQuery handling
        if hasattr(m, "from_user") and m.from_user:
            uid = m.from_user.id
        elif hasattr(m, "from_user") and getattr(m, "from_user", None):
            uid = m.from_user.id
        elif hasattr(m, "message") and getattr(m.message, "from_user", None):
            uid = m.message.from_user.id
        # Fallback: some pyrogram callback queries expose .from_user
        if uid is None or not is_owner(uid):
            try:
                # try to reply (for callback query reply_text not available; use answer)
                if hasattr(m, "answer"):
                    return await m.answer("Access denied.", show_alert=True)
                else:
                    return await m.reply_text("Access denied.")
            except:
                return
        return await func(c, m)
    return wrap

# ---------------- Sign-in flow ----------------
async def start_signin(phone, oid):
    """
    Create temporary directory and Telethon client to send code request.
    tmp_dir stays until flow completes (2FA handled).
    """
    safe_name = re.sub(r'[^0-9]', '_', phone)
    tmp_dir = tempfile.mkdtemp(prefix=f"tmpsess_{safe_name}_")
    tmp_path = Path(tmp_dir) / f"tmp_{safe_name}"
    cli = TelegramClient(str(tmp_path), API_ID, API_HASH)
    try:
        await cli.connect()
        sent = await cli.send_code_request(phone)
        # Save state: tmp_dir path so it won't be deleted prematurely
        STATE[oid] = {
            "phone": phone,
            "tmp_path": str(tmp_path),
            "tmp_dir": tmp_dir,
            "phone_code_hash": sent.phone_code_hash,
            "flow": "await_code"
        }
        await app.send_message(oid, "📩 Code request sent. Send the code here (private chat).")
    except Exception as e:
        await app.send_message(oid, f"❌ Error sending code: {e}")
        try:
            await cli.disconnect()
        except: pass
        # cleanup
        try: shutil.rmtree(tmp_dir, ignore_errors=True)
        except: pass
        STATE.pop(oid, None)

async def complete_signin(oid, code):
    """
    Complete sign-in with code. Handles SessionPasswordNeededError by moving to await_2fa state.
    Will NOT remove tmp_dir until final completion (so 2FA can use same tmp session).
    """
    st = STATE.get(oid, {})
    phone = st.get("phone")
    tmp_path = st.get("tmp_path")
    tmp_dir = st.get("tmp_dir")
    pch = st.get("phone_code_hash")
    if not all([phone, tmp_path, code]):
        await app.send_message(oid, "Missing signin state. Start again.")
        if tmp_dir:
            try: shutil.rmtree(tmp_dir, ignore_errors=True)
            except: pass
        STATE.pop(oid, None)
        return

    cli = TelegramClient(tmp_path, API_ID, API_HASH)
    try:
        await cli.connect()
        try:
            await cli.sign_in(phone=phone, code=code, phone_code_hash=pch)
        except SessionPasswordNeededError:
            # 2FA required — keep tmp_dir, set flow to await_2fa
            STATE[oid]["flow"] = "await_2fa"
            await app.send_message(oid, "🔐 2FA required. Send the 2FA password now.")
            # DO NOT disconnect here — keep session files intact. But safe to disconnect and reconnect later.
            try: await cli.disconnect()
            except: pass
            return
        except PhoneCodeInvalidError:
            await app.send_message(oid, "❌ The provided code is invalid. Start again.")
            await cli.disconnect()
            try: shutil.rmtree(tmp_dir, ignore_errors=True)
            except: pass
            STATE.pop(oid, None)
            return
        except PhoneCodeExpiredError:
            await app.send_message(oid, "❌ The provided code has expired. Start over.")
            await cli.disconnect()
            try: shutil.rmtree(tmp_dir, ignore_errors=True)
            except: pass
            STATE.pop(oid, None)
            return

        # sign-in succeeded
        session_file = Path(str(tmp_path) + ".session")
        # sometimes telethon stores in parent dir; check
        if not session_file.exists():
            candidates = list(Path(tmp_path).parent.glob("*.session"))
            session_file = candidates[0] if candidates else None

        if session_file and session_file.exists():
            await app.send_document(oid, str(session_file), caption=f"✅ Session for {phone} (temporary). Keep it safe!")
            # delete session file
            try: session_file.unlink()
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
        # cleanup tmp_dir
        try:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except:
            pass
        STATE.pop(oid, None)

async def complete_signin_2fa(oid, password):
    """
    When in await_2fa state: use the same tmp_path to sign in with password.
    """
    st = STATE.get(oid, {})
    tmp_path = st.get("tmp_path")
    tmp_dir = st.get("tmp_dir")
    phone = st.get("phone")
    if not tmp_path:
        await app.send_message(oid, "Session temp path missing. Start over.")
        STATE.pop(oid, None)
        if tmp_dir:
            try: shutil.rmtree(tmp_dir, ignore_errors=True)
            except: pass
        return

    cli = TelegramClient(tmp_path, API_ID, API_HASH)
    try:
        await cli.connect()
        await cli.sign_in(password=password)  # completes 2FA
        # session file should exist now
        session_file = Path(str(tmp_path) + ".session")
        if not session_file.exists():
            candidates = list(Path(tmp_path).parent.glob("*.session"))
            session_file = candidates[0] if candidates else None

        if session_file and session_file.exists():
            await app.send_document(oid, str(session_file), caption=f"✅ Session for {phone} (2FA). Keep it safe!")
            try: session_file.unlink()
            except: pass
            await app.send_message(oid, "2FA success. Session sent.")
        else:
            await app.send_message(oid, "2FA succeeded but session file not found.")
    except Exception as e:
        await app.send_message(oid, f"2FA sign-in error: {e}")
    finally:
        try:
            await cli.disconnect()
        except: pass
        try:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except: pass
        STATE.pop(oid, None)

# ---------------- OTP Read: try Telethon then Pyrogram ----------------
async def process_session_for_otps(owner_uid, session_file_path, scan_dialog_limit=60, per_dialog_limit=10, global_message_limit=200):
    """
    Try Telethon first. If not authorized/invalid, attempt Pyrogram.
    - session_file_path: local path to .session file
    - scan_dialog_limit: how many dialogs to check in Pyrogram
    - per_dialog_limit: messages per dialog to fetch
    - global_message_limit: total messages to scan for Telethon fallback
    """
    tmp_session = Path(session_file_path)
    await app.send_message(owner_uid, f"🔎 Connecting with provided session `{tmp_session.name}` and scanning recent messages...")

    # --- Try Telethon ---
    tele_ok = False
    try:
        tclient = TelegramClient(str(tmp_session.with_suffix('')), API_ID, API_HASH)
        await tclient.connect()
        if await tclient.is_user_authorized():
            tele_ok = True
            # scan messages globally (iter_messages across all dialogs)
            found = []
            scanned = 0
            async for msg in tclient.iter_messages(None, limit=global_message_limit):
                if not msg or not getattr(msg, "message", None):
                    continue
                scanned += 1
                text = msg.message
                if otp_found(text):
                    otps = extract_otps(text)
                    preview = text if len(text) < 200 else text[:197] + "..."
                    found.append((otps, preview, getattr(msg, "date", None)))
                if scanned >= global_message_limit:
                    break
            if not found:
                await app.send_message(owner_uid, "🔕 No OTP-like messages found (Telethon scan).")
            else:
                total = 0
                for otps, preview, date in found:
                    total += 1
                    await app.send_message(owner_uid, f"🔔 Found OTP(s): {', '.join(otps)}\nMsg preview: `{preview}`")
                    if total >= 25:
                        await app.send_message(owner_uid, "ℹ️ Reached reporting cap (25). Stop.")
                        break
        else:
            await app.send_message(owner_uid, "⚠️ Provided session is not authorized (Telethon). Will try Pyrogram next.")
        try:
            await tclient.disconnect()
        except: pass
    except Exception as e:
        # Telethon failed — try Pyrogram
        try:
            await notify(f"Telethon attempt raised: {e}")
        except: pass
        try:
            await tclient.disconnect()
        except: pass

    if tele_ok:
        # remove file and return
        try:
            tmp_session.unlink()
        except:
            pass
        return

    # --- Try Pyrogram ---
    # Pyrogram client accepts a session_name (path without extension can work). We'll attempt multiple forms.
    py_success = False
    # create a temporary copy under tmpdir to avoid messing original naming spaces
    tmpd = tempfile.mkdtemp(prefix="py_sess_")
    try:
        # copy session file into tmpdir with same basename
        copy_path = Path(tmpd) / tmp_session.name
        shutil.copyfile(tmp_session, copy_path)
        # Pyrogram session name: path without suffix
        session_name_candidate = str(copy_path.with_suffix(''))  # path minus .session
        pycl = Client(session_name_candidate, api_id=API_ID, api_hash=API_HASH)
        try:
            await pycl.start()
            # check authorized
            try:
                me = await pycl.get_me()
                if me is None:
                    await app.send_message(owner_uid, "⚠️ Pyrogram session not authorized.")
                else:
                    py_success = True
                    # iterate dialogs (limited)
                    total_found = 0
                    async for dialog in pycl.get_dialogs(limit=scan_dialog_limit):
                        # for each dialog, get recent messages
                        try:
                            async for msg in pycl.get_history(dialog.chat.id, limit=per_dialog_limit):
                                text = getattr(msg, "text", None) or getattr(msg, "message", None)
                                if not text:
                                    continue
                                if otp_found(text):
                                    otps = extract_otps(text)
                                    preview = text if len(text) < 200 else text[:197] + "..."
                                    await app.send_message(owner_uid, f"🔔 Found OTP(s): {', '.join(otps)}\nChat: {getattr(dialog.chat, 'title', getattr(dialog.chat, 'first_name', dialog.chat.id))}\nMsg preview: `{preview}`")
                                    total_found += 1
                                    if total_found >= 25:
                                        await app.send_message(owner_uid, "ℹ️ Reached reporting cap (25).")
                                        raise StopAsyncIteration
                        except StopAsyncIteration:
                            break
                        except Exception:
                            continue
                    if total_found == 0:
                        await app.send_message(owner_uid, "🔕 No OTP-like messages found (Pyrogram scan).")
            except Exception as e:
                await app.send_message(owner_uid, f"Pyrogram: could not read account info: {e}")
            await pycl.stop()
        except Exception as e:
            try:
                await pycl.stop()
            except: pass
            raise e
    except Exception as e:
        await app.send_message(owner_uid, f"❌ Could not use provided session with Pyrogram either: {e}")
    finally:
        # cleanup copied files and supplied file
        try:
            shutil.rmtree(tmpd, ignore_errors=True)
        except:
            pass
        try:
            tmp_session.unlink()
        except:
            pass

# ---------------- Bot Handlers ----------------
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
        await cbq.message.reply_text("Please upload the `.session` file (or a zip containing it) as a document. Bot will temporarily use it to read recent messages for OTPs and then delete it.")
        await cbq.answer()
    elif data == "help":
        await cbq.message.reply_text(
            "Flow:\n• Create Session → send phone → receive code → send code → you'll get .session file in chat (bot won't store it)\n• If 2FA enabled, send password when asked\n• Read OTP from session → upload your .session file (or .zip) → bot scans recent messages and returns OTP-like messages.")
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
        pwd = m.text.strip()
        asyncio.create_task(complete_signin_2fa(OWNER_ID, pwd))
        await m.reply_text("Trying 2FA password now...")
    else:
        await m.reply_text("Use /start and buttons to begin.")

@app.on_message(filters.private & filters.document)
@owner_only
async def on_document(c, m):
    st = STATE.get(OWNER_ID, {})
    flow = st.get("flow")
    if flow == "await_session_file":
        tmpd = Path(tempfile.mkdtemp(prefix="uploaded_sess_"))
        local = tmpd / m.document.file_name
        await m.download(file_name=str(local))
        # If zip, extract
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
            # if not .session, try common pyrogram sqlite names too
            if local.suffix != ".session":
                # attempt to accept anyway if it looks like a sqlite session (e.g., contains 'sqlite' or is DB)
                # but prefer explicit .session
                # We'll still try to process it — process_session_for_otps will attempt both libs.
                pass

        # now process session file for OTPs (async)
        asyncio.create_task(process_session_for_otps(OWNER_ID, str(local)))
        await m.reply_text("Session received. Scanning for OTPs — results will be posted here.")
        STATE.pop(OWNER_ID, None)
    else:
        await m.reply_text("If you want bot to read OTP from a session, first press 'Read OTP from session' button in /start.")

# ---- Run ----
if __name__ == "__main__":
    print("🚀 Running improved session bot (Telethon + Pyrogram compatible)...")
    while True:
        try:
            app.run()
        except Exception as e:
            print("[BOT CRASH]", e)
            time.sleep(5)
