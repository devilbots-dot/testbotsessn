#!/usr/bin/env python3
# main.py — Secure Session Manager Bot + OTP Watcher (upload-based)
# Works with session.zip upload and monitors OTPs safely

import os, asyncio, tempfile, zipfile, shutil, re, time
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
except: pass
print("[UTC TIME]", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))

# ---- Load ENV ----
load_dotenv()
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SESSION_FOLDER = Path("sessions"); SESSION_FOLDER.mkdir(exist_ok=True)
OTP_LOG_FOLDER = Path("otp_logs"); OTP_LOG_FOLDER.mkdir(exist_ok=True)
if not (API_ID and API_HASH and BOT_TOKEN and OWNER_ID):
    raise SystemExit("Please set API_ID, API_HASH, BOT_TOKEN, OWNER_ID!")

# ---- Globals ----
app = Client("otpwatch_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)
STATE, WATCHER_TASKS, WATCHER_CLIENTS = {}, [], []
WATCHER_RUNNING = False
OTP_REGEX = re.compile(r"\b(\d{4,8})\b")
OTP_HINTS = ["otp", "code", "pin", "verify", "verification", "passcode"]

# ---- Utils ----
def is_owner(uid): return uid == OWNER_ID
async def notify(text): 
    try: await app.send_message(OWNER_ID, text)
    except: pass

def otp_found(text):
    if OTP_REGEX.search(text): return True
    low = text.lower()
    return any(h in low for h in OTP_HINTS)

def log_otp(name, msg):
    with open(OTP_LOG_FOLDER / f"{name}.log", "a", encoding="utf8") as f:
        f.write(msg + "\n")

# ---- Keyboards ----
HOME_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📞 Create Session", callback_data="create_session"),
        InlineKeyboardButton("📤 Upload session.zip", callback_data="upload_zip")
    ],
    [
        InlineKeyboardButton("🕵️ Start Watcher", callback_data="start_watcher"),
        InlineKeyboardButton("🛑 Stop Watcher", callback_data="stop_watcher")
    ],
])

# ---- Owner check ----
def owner_only(func):
    async def wrap(c, m):
        if not is_owner(m.from_user.id):
            return await m.reply_text("Access denied.")
        return await func(c, m)
    return wrap

# ---- Create session flow ----
async def start_signin(phone, oid):
    try:
        tmp = SESSION_FOLDER / f"tmp_{re.sub(r'[^0-9]', '_', phone)}"
        cli = TelegramClient(str(tmp), API_ID, API_HASH)
        await cli.connect()
        sent = await cli.send_code_request(phone)
        STATE[oid] = {"phone": phone, "tmp": str(tmp), "hash": sent.phone_code_hash, "flow": "otp"}
        await notify("📩 OTP sent! Please reply with the code.")
        await cli.disconnect()
    except Exception as e:
        await notify(f"❌ Error sending code: {e}")

async def complete_signin(oid, code):
    st = STATE.get(oid, {})
    phone, tmp, pch = st.get("phone"), st.get("tmp"), st.get("hash")
    if not all([phone, tmp, code]): return await notify("Missing details.")
    cli = TelegramClient(tmp, API_ID, API_HASH)
    try:
        await cli.connect()
        try:
            await cli.sign_in(phone=phone, code=code, phone_code_hash=pch)
        except SessionPasswordNeededError:
            STATE[oid]["flow"] = "2fa"
            await notify("🔐 2FA required — send password.")
            await cli.disconnect(); return
        f = Path(tmp + ".session")
        dest = SESSION_FOLDER / f"{phone.replace('+','')}.session"
        shutil.move(str(f), str(dest))
        await notify(f"✅ Session created: `{dest.name}`")
        await app.send_document(oid, str(dest), caption="Your session file is ready.")
    except Exception as e:
        await notify(f"Error signing in: {e}")
    finally:
        try: await cli.disconnect()
        except: pass
        STATE.pop(oid, None)

# ---- Watcher ----
async def watcher_runner(sf: Path):
    name = sf.stem
    cli = TelegramClient(str(sf), API_ID, API_HASH)
    try:
        await cli.connect()
        if not await cli.is_user_authorized():
            await notify(f"⚠️ {name} not authorized.")
            await cli.disconnect(); return
    except Exception as e:
        print(f"[{name}] connect error:", e); return
    WATCHER_CLIENTS.append(cli)

    @cli.on(events.NewMessage(incoming=True))
    async def msg(evt):
        txt = evt.raw_text or ""
        if otp_found(txt):
            codes = OTP_REGEX.findall(txt)
            msg_ = f"[{name}] OTPs: {', '.join(codes)} | msg: {txt}"
            log_otp(name, msg_)
            await notify(f"🔔 {msg_}")

    try:
        await cli.run_until_disconnected()
    except Exception as e:
        print(f"[{name}] stopped: {e}")
    finally:
        await cli.disconnect()

async def start_watcher():
    global WATCHER_RUNNING, WATCHER_TASKS
    if WATCHER_RUNNING: return await notify("Watcher already running.")
    files = [p for p in SESSION_FOLDER.iterdir() if ".session" in p.name]
    if not files: return await notify("No sessions found.")
    WATCHER_RUNNING = True
    for f in files:
        WATCHER_TASKS.append(asyncio.create_task(watcher_runner(f)))
    await notify(f"Watcher started on {len(files)} sessions.")

async def stop_watcher():
    global WATCHER_RUNNING
    for c in WATCHER_CLIENTS:
        try: await c.disconnect()
        except: pass
    WATCHER_RUNNING = False
    await notify("Watcher stopped.")

# ---- Bot Handlers ----
@app.on_message(filters.private & filters.command("start"))
@owner_only
async def start_cmd(c, m):
    await m.reply_text("Welcome Owner!", reply_markup=HOME_KB)

@app.on_callback_query()
async def cb(c, cb):
    if not is_owner(cb.from_user.id): return await cb.answer("Denied", show_alert=True)
    data = cb.data
    if data == "create_session":
        STATE[OWNER_ID] = {"flow": "phone"}
        await cb.message.reply_text("Send phone in +91xxxx format")
    elif data == "upload_zip":
        STATE[OWNER_ID] = {"flow": "zip"}
        await cb.message.reply_text("Send your session.zip file now.")
    elif data == "start_watcher":
        asyncio.create_task(start_watcher())
        await cb.answer("Starting watcher...")
    elif data == "stop_watcher":
        asyncio.create_task(stop_watcher())
        await cb.answer("Stopping watcher...")

@app.on_message(filters.private & filters.text)
@owner_only
async def txt(c, m):
    st = STATE.get(OWNER_ID, {})
    flow = st.get("flow")
    if flow == "phone":
        STATE[OWNER_ID] = {"phone": m.text.strip(), "flow": "otp_init"}
        await m.reply_text("Sending OTP...")
        asyncio.create_task(start_signin(m.text.strip(), OWNER_ID))
    elif flow == "otp":
        asyncio.create_task(complete_signin(OWNER_ID, m.text.strip()))
    elif flow == "2fa":
        await notify("2FA step can be added here manually later.")
        STATE.pop(OWNER_ID, None)
    else:
        await m.reply_text("Use /start menu again.")

@app.on_message(filters.private & filters.document)
@owner_only
async def upload_zip(c, m):
    st = STATE.get(OWNER_ID, {})
    if st.get("flow") != "zip":
        return await m.reply_text("Please choose upload option first.")
    tmp = Path(tempfile.mkdtemp())
    zp = tmp / m.document.file_name
    await m.download(file_id=m.document.file_id, file_name=str(zp))
    try:
        with zipfile.ZipFile(zp, "r") as z:
            z.extractall(SESSION_FOLDER)
        await m.reply_text("✅ Uploaded and extracted. Watcher can now be started.")
    except Exception as e:
        await m.reply_text(f"Error: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    STATE.pop(OWNER_ID, None)

# ---- Main ----
if __name__ == "__main__":
    print("🚀 Running bot...")
    while True:
        try:
            app.run()
        except Exception as e:
            print("[BOT CRASH]", e)
            time.sleep(5)
