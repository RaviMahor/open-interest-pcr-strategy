# pyright: reportGeneralTypeIssues=false
import math
import os
import json
import threading
import sys
import select
import time
import fcntl
import tty
import termios
import hmac
import hashlib
import requests
import signal
import atexit
import re
from datetime import datetime, timedelta
from collections import deque, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, TextIO
from requests.adapters import HTTPAdapter
# =========================================================
# 🔒 SINGLE INSTANCE — new start = old kill, new run
# =========================================================
def _acquire_single_instance_lock():
    base_name = os.path.splitext(os.path.basename(os.path.abspath(__file__)))[0]
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{base_name}.lock")
    for attempt in range(2):
        f = open(lock_path, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            f.close()
            # Old instance hai — PID read karke kill, phir retry
            try:
                with open(lock_path, "r") as rf:
                    old_pid = int(rf.read().strip())
                if old_pid != os.getpid():
                    os.kill(old_pid, signal.SIGTERM)
                    time.sleep(2)
            except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
                time.sleep(1)
            if attempt == 1:
                print("⚠️ Could not take over — old instance may still be exiting.", flush=True)
                sys.exit(1)
            continue
        f.write(str(os.getpid()))
        f.flush()
        return f

_SINGLE_INSTANCE_LOCK = _acquire_single_instance_lock()

# =========================================================
# 🌐 HTTP SESSION REUSE
# =========================================================
_HTTP_SESSION = requests.Session()
_HTTP_ADAPTER = HTTPAdapter(pool_connections=24, pool_maxsize=24)
_HTTP_SESSION.mount("https://", _HTTP_ADAPTER)
_HTTP_SESSION.mount("http://", _HTTP_ADAPTER)

def http_get(url, **kwargs):
    return _HTTP_SESSION.get(url, **kwargs)

def http_post(url, **kwargs):
    return _HTTP_SESSION.post(url, **kwargs)

# =========================================================
# 📁 PATHS / CONFIG
# =========================================================
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPT_BASE = os.path.splitext(os.path.basename(os.path.abspath(__file__)))[0]
_SETTINGS_PATH = os.path.join(_SCRIPT_DIR, _SCRIPT_BASE + ".json")
_SECRETS_PATH = os.path.join(_SCRIPT_DIR, _SCRIPT_BASE + ".secrets.json")

# =========================================================
# 📢 TELEGRAM ALERT CONFIG
# =========================================================
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN:
        return
    def _send():
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
            r = http_post(url, json=payload, timeout=5)
            if r.status_code != 200:
                print("❌ Telegram failed:", r.text, flush=True)
        except Exception as e:
            print("❌ Telegram failed:", e, flush=True)
    threading.Thread(target=_send, daemon=True).start()
SCRIPT_EXITED = False        
def script_exit_alert(reason="Unknown"):
    global SCRIPT_EXITED
    if SCRIPT_EXITED:
            return    
    SCRIPT_EXITED = True

    body = (
        f"⚠️ SCRIPT EXIT ALERT\n"
        f"Reason : {reason}\n"
        f"Time   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if reason == "Ctrl+C":
        body += "\n🛑 Positions unchanged (no auto square-off)."
    send_telegram(body)
atexit.register(lambda: script_exit_alert("Python process exited"))

LAST_401_TELEGRAM_AT = 0.0
_401_TELEGRAM_COOLDOWN_SEC = 120   # 2 min — 401 alert Telegram spam nahi
_401_ALERT_SENT = False   # jab 401 clear ho jaye (koi request 200 aaye) to Telegram pe "cleared" bhejne ke liye

def _send_401_telegram_once(status_code, response_text, path):
    """401 (e.g. ip_not_whitelisted) hone par Telegram pe ek alert bhejo; cooldown ke baad hi dubara."""
    global LAST_401_TELEGRAM_AT, _401_ALERT_SENT
    now = time.time()
    if now - LAST_401_TELEGRAM_AT < _401_TELEGRAM_COOLDOWN_SEC:
        return
    LAST_401_TELEGRAM_AT = now
    _401_ALERT_SENT = True   # ab jab bhi koi request 200 aayegi to "401 cleared" bhejenge
    err_short = (response_text[:250] if response_text else str(status_code))
    send_telegram(
        f"🔴 API 401 ERROR\n\n"
        f"Code   : {status_code}\n"
        f"Path   : {path}\n"
        f"Error  : {err_short}\n\n"
        f"⚠️ Agar 'ip_not_whitelisted' hai to Delta Exchange pe apna current IP whitelist karein.\n"
        f"Time   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

def _send_401_cleared_telegram_once():
    """Jab 401 error clear ho jaye (koi auth request 200 OK) to Telegram pe ek baar bhejo."""
    global _401_ALERT_SENT
    if not _401_ALERT_SENT:
        return
    _401_ALERT_SENT = False
    send_telegram(
        f"🟢 API 401 ERROR CLEARED\n\n"
        f"Auth / IP issue resolve ho gaya. API calls ab sahi chal rahi hain.\n"
        f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

def api_failure_alert(api_name, error):
    global API_DOWN
    if not HEALTH.get(api_name, True):
        return  # already marked DOWN, don't spam alerts
    now = time.time()
    last_time = LAST_API_ALERT_TIME.get(api_name)
    # 400 Bad Request = expired contract / invalid request — API OK, API_DOWN mat set (script band na ho)
    err_str = str(error).lower()
    is_400_bad_request = "400" in err_str and ("bad request" in err_str or "client error" in err_str)
    if api_name in ("ORDERS", "POSITIONS") and not is_400_bad_request:
        API_DOWN = True

    if last_time and (now - last_time < API_ALERT_COOLDOWN):
        return

    LAST_API_ALERT_TIME[api_name] = now

    if HEALTH.get(api_name, True):
        HEALTH[api_name] = False
        API_LATENCY[api_name] = -1  # -1 means DOWN

        send_telegram(
            f"🔴 API DOWN ALERT\n\n"
            f"API   : {api_name}\n"
            f"Error : {str(error)[:200]}\n"
            f"Time  : {datetime.now().strftime('%H:%M:%S')}"
        )
def api_recovered(api_name):
    if not HEALTH[api_name]:
        HEALTH[api_name] = True
        API_LATENCY[api_name] = 0   # reset to healthy latency
        now = time.time()
        last_rec = LAST_RECOVERED_ALERT_TIME.get(api_name, 0)
        if now - last_rec >= API_RECOVERED_COOLDOWN:
            LAST_RECOVERED_ALERT_TIME[api_name] = now
            send_telegram(
                f"🟢 API RECOVERED\n"
                f"API  : {api_name}\n"
                f"Time : {datetime.now().strftime('%H:%M:%S')}"
            )  

# =========================
# ⏱️ API LATENCY MONITOR
# =========================
API_LATENCY_LIMIT_MS = 3000   # thresholds: 1000, 2000, 3000 ms

def latency_alert(api_name, latency_ms):
    return  # latency alerts disabled
# =========================
# 🩺 API HEALTH TRACKING
# =========================
HEALTH = {
    "SPOT": True,
    "WALLET": True,
    "ORDERS": True,
    "POSITIONS": True,
}

LAST_API_ALERT_TIME = {
    "SPOT": 0.0,
    "WALLET": 0.0,
    "ORDERS": 0.0,
    "POSITIONS": 0.0,
}
LAST_RECOVERED_ALERT_TIME = {
    "SPOT": 0.0,
    "WALLET": 0.0,
    "ORDERS": 0.0,
    "POSITIONS": 0.0,
}
API_LATENCY = {
    "SPOT": 0,
    "WALLET": 0,
    "ORDERS": 0,
    "POSITIONS": 0,
}
def health_status_icon(name):
    return "🟢" if HEALTH.get(name) else "🔴"
def latency_status(latency_ms):
    if latency_ms < 0:
        return "🔴 DOWN"
    if latency_ms > 3000:
        return "🔴 CRITICAL"
    elif latency_ms > 2000:
        return "⚠️ WARNING"
    elif latency_ms >= 1000:
        return "🟡 SLOW"
    else:
        return "🟢 HEALTHY"

# =========================================================
# 👤 MAIN ACCOUNT DETAILS. [a] = account name (main/sub), [u] = edit User (name, email, phone).
# =========================================================
MAIN_ACCOUNT = {
    "account_name": "Main",   # main account / sub account label
    "username": "Quantitative Trader", # who uses this account
    "email": "trader@example.com",
    "phone": "+1 0000000000"
}
ACCOUNT_NAME_EDIT_PENDING = False  # [a] → prompt account name (main/sub)
USER_EDIT_PENDING = False  # [u] → prompt User name, email, phone
MARGIN_EDIT_PENDING = False  # [m] → prompt min margin % (1-100)
# =========================================================
# 🔧 USER SETTINGS
# =========================================================

SYMBOL = "BTC"   # "BTC" or "ETH" — switch with [i]; wait for bias change in either instrument, then square off + switch
INSTRUMENT_SWITCH_PENDING = False
INSTRUMENT_SWITCH_SQUARE_OFF_SENT = False
INSTRUMENT_SWITCH_BIAS_CHANGE_DETECTED = False  # Jab BTC ya ETH me bias change ho, tab square off + switch
INSTRUMENT_SWITCH_BIAS_CHANGE_MSG = ""  # e.g. "BTC SELL CALL→SELL PUT; ETH ..."
LAST_BIAS_BTC = None  # Track for "bias change" trigger; None = not yet set this pending session
LAST_BIAS_ETH = None
# FIRST_PCR_CHANGE strategy: jis instrument me pehle ALL_PCR change (SELL CALL/SELL PUT) usi me position
FIRST_PCR_LAST_BTC = None
FIRST_PCR_LAST_ETH = None
CMD_FEEDBACK = ""  # Persistent key-press confirmation (shown in UI until cleared)
CMD_FEEDBACK_CLEAR_AT = 0.0  # Timestamp to clear CMD_FEEDBACK
LAST_CMD_SHOWN = {"msg": "", "until": 0.0}  # Last command line, show for 30 sec
BASE_URL = "https://api.india.delta.exchange"

API_KEY = ""
API_SECRET = ""

REFRESH_SECONDS = 1
ATM_RANGE_PERCENT = 1.0

# ===== STRATEGY =====
# NEAR_PCR     : Near expiry PCR. PCR < 1 → SELL CALL, PCR ≥ 1 → SELL PUT (hamesha ek side).
# NEXT_PCR     : Next expiry PCR. Same: PCR < 1 → SELL CALL, PCR ≥ 1 → SELL PUT.
# ALL_PCR      : All expiry PCR. PCR < 1 → SELL CALL, PCR ≥ 1 → SELL PUT.
# MAX_OPEN_INTEREST: Jis expiry me sabse zyada OI ho us expiry ka PCR use.
# FIRST_PCR_CHANGE: Sab instruments (BTC/ETH) me ALL_PCR monitor; jis me pehle PCR change usi me position (square off other, build there).
# PRICE_CHANGE_DIST: Delta perpetuals universe breadth. Advancers > Decliners → SELL PUT, Decliners > Advancers → SELL CALL.
STRATEGY_MODE = "ALL_PCR"  # default; toggle with [s] (cycles through all strategy modes)

# ===== ORDER TYPE =====
# ORDER_TYPE: "Options_Delta_50/10/10" = 50/10/10 (short call/put, long calls, long puts)
#             "futures" = short perpetual futures only
ORDER_TYPE = "Options_Delta_50/10/10"  # default; toggle with [t] key
ORDER_TYPES_CYCLE = ["Options_Delta_50/10/10", "futures"]

execution_status = "IDLE"
# Margin mode for orders: "isolated" │ "cross" │ "portfolio" (Delta Exchange). Toggle with [p].
MARGIN_MODE = "cross"  # default cross; [p] cycles: isolated → cross → portfolio → isolated
MARGIN_MODES_CYCLE = ["isolated", "cross", "portfolio"]

# ===== RISK RULES =====
DIR_MARGIN_PCT = 0.10      # 10% directional
RANGE_MARGIN_PCT = 0.20 

TARGET_MARGIN_PCT = 0.15   # aim 15% so used lands 10–20%; kharab condition me 20% tak
MAX_MARGIN_PCT = 0.20      # cap 20% (user: 10% se jyada 20% se kam / kharab me 20% tak)
# Used margin cap = MIN_MARGIN_PCT ka hamesha double (e.g. min 10% → cap 20%)
MAX_USED_MARGIN_MULTIPLIER = 2.0
# Position sizing: "margin" = current behavior, "max_loss" = total capital ke 10% max loss ke hisaab se size
POSITION_SIZING_MODE = "max_loss"
MAX_LOSS_CAP_PCT = 0.10
# [m][a][u][p][k][1-6][x] se jo bhi edit — sab ek file me save, restart pe load (py jesa hi name dynamically set hoga)

def _position_sizing_mode_label():
    if POSITION_SIZING_MODE == "max_loss":
        return f"Max Loss {round(MAX_LOSS_CAP_PCT * 100)}%"
    return "Margin"

def _order_type_label():
    if ORDER_TYPE == "futures":
        return "Short Futures"
    return "Iron Fly (50/10/10)"

def _load_settings():
    """Saari settings ek hi JSON file se load karo; purani files se one-time migrate bhi karo."""
    defaults = {
        "min_margin_pct": 0.10,
        "account_name": "Main",
        "username": "Quantitative Trader",
        "email": "trader@example.com",
        "phone": "+1 0000000000",
        "margin_mode": "cross",
        "position_sizing_mode": "max_loss",
        "strategy_mode": "ALL_PCR",
        "order_type": "Options_Delta_50/10/10",
        "user_auto_execution": True,
        "log_enabled": True,
        "api_key": os.getenv("DELTA_API_KEY", ""),
        "api_secret": os.getenv("DELTA_API_SECRET", ""),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    }
    try:
        if os.path.isfile(_SECRETS_PATH):
            with open(_SECRETS_PATH) as f:
                secret_data = json.load(f)
            for k in ("api_key", "api_secret", "telegram_bot_token", "telegram_chat_id"):
                if k in secret_data and secret_data[k] is not None:
                    defaults[k] = str(secret_data[k]).strip()

        # Migrate: agar py-named json nahi hai lekin delta_settings.json hai to usse load karke nayi file me save
        if not os.path.isfile(_SETTINGS_PATH):
            _legacy = os.path.join(_SCRIPT_DIR, "delta_settings.json")
            if os.path.isfile(_legacy):
                with open(_legacy) as f:
                    d = json.load(f)
                    for k in defaults:
                        if k in d:
                            defaults[k] = d[k]
                    defaults["min_margin_pct"] = max(0.01, min(1.0, float(defaults.get("min_margin_pct", 0.10))))
                    if defaults["margin_mode"] not in ("isolated", "cross", "portfolio"):
                        defaults["margin_mode"] = "cross"
                    if defaults["position_sizing_mode"] not in ("margin", "max_loss"):
                        defaults["position_sizing_mode"] = "max_loss"
                    if defaults["strategy_mode"] not in ("NEAR_PCR", "ALL_PCR", "NEXT_PCR", "MAX_OPEN_INTEREST", "FIRST_PCR_CHANGE", "PRICE_CHANGE_DIST"):
                        defaults["strategy_mode"] = "ALL_PCR"
                    if defaults["order_type"] not in ("Options_Delta_50/10/10", "futures"):
                        defaults["order_type"] = "Options_Delta_50/10/10"
                    with open(_SETTINGS_PATH, "w") as out:
                        json.dump({k: defaults[k] for k in defaults}, out, indent=2)
                    return defaults
            with open(_SETTINGS_PATH, "w") as out:
                json.dump({k: defaults[k] for k in defaults}, out, indent=2)
            return defaults
        if os.path.isfile(_SETTINGS_PATH):
            with open(_SETTINGS_PATH) as f:
                d = json.load(f)
                for k in defaults:
                    if k in d:
                        defaults[k] = d[k]
                defaults["min_margin_pct"] = max(0.01, min(1.0, float(defaults.get("min_margin_pct", 0.10))))
                if defaults["margin_mode"] not in ("isolated", "cross", "portfolio"):
                    defaults["margin_mode"] = "cross"
                if defaults["position_sizing_mode"] not in ("margin", "max_loss"):
                    defaults["position_sizing_mode"] = "max_loss"
                if defaults["strategy_mode"] not in ("NEAR_PCR", "ALL_PCR", "NEXT_PCR", "MAX_OPEN_INTEREST", "FIRST_PCR_CHANGE", "PRICE_CHANGE_DIST"):
                    defaults["strategy_mode"] = "ALL_PCR"
                if defaults["order_type"] not in ("Options_Delta_50/10/10", "futures"):
                    defaults["order_type"] = "Options_Delta_50/10/10"
    except Exception:
        pass
    return defaults
def _save_settings():
    """Abhi jo values hain wo file me save — restart pe yahi load honge."""
    try:
        d = {
            "min_margin_pct": MIN_MARGIN_PCT,
            "account_name": MAIN_ACCOUNT.get("account_name", "Main"),
            "username": MAIN_ACCOUNT.get("username", ""),
            "email": MAIN_ACCOUNT.get("email", ""),
            "phone": MAIN_ACCOUNT.get("phone", ""),
            "margin_mode": MARGIN_MODE.lower(),
            "position_sizing_mode": POSITION_SIZING_MODE,
            "strategy_mode": STRATEGY_MODE,
            "order_type": ORDER_TYPE,
            "user_auto_execution": USER_AUTO_EXECUTION,
            "log_enabled": LOG_ENABLED,
            "api_key": API_KEY,
            "api_secret": API_SECRET,
            "telegram_bot_token": TELEGRAM_BOT_TOKEN,
            "telegram_chat_id": TELEGRAM_CHAT_ID,
        }
        with open(_SETTINGS_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        print("⚠️ Settings save fail:", e)

def _reset_settings_to_defaults():
    """[r] — saari edit-wali settings script ke original default par kar do + file me bhi save."""
    global MIN_MARGIN_PCT, MARGIN_MODE, POSITION_SIZING_MODE, STRATEGY_MODE, ORDER_TYPE, USER_AUTO_EXECUTION
    MIN_MARGIN_PCT = 0.10
    MAIN_ACCOUNT["account_name"] = "Main"
    MAIN_ACCOUNT["username"] = "Quantitative Trader"
    MAIN_ACCOUNT["email"] = "trader@example.com"
    MAIN_ACCOUNT["phone"] = "+1 0000000000"
    MARGIN_MODE = "cross"
    POSITION_SIZING_MODE = "max_loss"
    STRATEGY_MODE = "ALL_PCR"
    ORDER_TYPE = "Options_Delta_50/10/10"
    USER_AUTO_EXECUTION = True
    _save_settings()

# Startup: saved settings apply karo (restart pe [m][a][u][p][k][1-6][x] ki values na bhulein)
_s = _load_settings()
MIN_MARGIN_PCT = _s["min_margin_pct"]
MAIN_ACCOUNT["account_name"] = _s["account_name"]
MAIN_ACCOUNT["username"] = _s["username"]
MAIN_ACCOUNT["email"] = _s["email"]
MAIN_ACCOUNT["phone"] = _s["phone"]
MARGIN_MODE = _s["margin_mode"]
POSITION_SIZING_MODE = _s["position_sizing_mode"]
STRATEGY_MODE = _s["strategy_mode"]
ORDER_TYPE = _s["order_type"]
USER_AUTO_EXECUTION = _s["user_auto_execution"]
LOG_ENABLED = bool(_s.get("log_enabled", True))
API_KEY = _s["api_key"]
API_SECRET = _s["api_secret"]
TELEGRAM_BOT_TOKEN = _s["telegram_bot_token"]
TELEGRAM_CHAT_ID = _s["telegram_chat_id"]

HEDGE_DELTA_TARGET = 0.10
DELTA_THRESHOLD = 0.01

EQUALITY_ARMED = False         # Case A
EQUALITY_EVENT = False         # Case B

SYSTEM_EXECUTION_ALLOWED = True  # controlled by safety logic

EQUALITY_MANUAL_EXEC = False
EQUALITY_SUSPENDED = False
EQUALITY_SUSPENDED_UNTIL = 0   # time.time() after which auto-unsuspend (avoid 647x retry on 400)
EQUALITY_TRIGGER_REASON = "NONE"
EXECUTION_ALLOWED = True   # master execution permission
EQUALITY_WAIT_COUNT = 0
FORCE_UI_REFRESH = False
Q_EXIT_CONFIRM_UNTIL = 0.0
UI_PRINT_LOCK = threading.RLock()
LAST_VISIBLE_UI_AT = time.time()
LAST_LIVE_UI_RENDER_AT = 0.0
LAST_HEARTBEAT_PRINT_AT = 0.0
LIVE_RENDER_COUNT = 0
LAST_MAIN_LOOP_STAGE = "startup"
STALL_HEARTBEAT_AFTER_SEC = 4.0
STALL_HEARTBEAT_EVERY_SEC = 5.0
LAST_IGNORED_INPUT_LOG_AT = 0.0
LAST_FULL_SNAPSHOT_LOG_AT = 0.0
STALL_FULL_SNAPSHOT_EVERY_SEC = 10.0

# =========================================================
# 💸 LOW BALANCE MODE (avoid rejected orders spam)
# =========================================================
# When balance is too low (or free balance almost zero), placing new orders will often be rejected.
# In that case:
# - Only CLOSE wrong/extra legs (risk reduction)
# - Do NOT open new legs / roll 10d hedges
# - Keep existing legs managed until balance improves
LOW_BALANCE_USD = 5.0
LOW_FREE_USD = 0.25
PENDING_BIAS_REBALANCE = None  # e.g. "SELL PUT" / "SELL CALL" — pending full rebalance when balance recovers
PENDING_BIAS_SINCE = 0.0
LOW_BALANCE_ALIGN_COOLDOWN_SEC = 60.0
LOW_BALANCE_ALIGN_LAST_TRY = 0.0
# Order fail hone par naye orders na dale — LEG_MODE 1/2/3 rules use karo, leg mismatch chalega. Sirf margin > 10% ho to reduce.
ORDER_FAILURE_COOLDOWN_UNTIL = 0.0
ORDER_FAILURE_COOLDOWN_SEC = 300  # 5 min — order fail ke baad naye add/build orders nahi
# Leg mode (fixed): 1 = 3 legs (sell+long+long), 2 = 2 legs (sell+long: SELL CALL→long put only; SELL PUT→long call only), 3 = 1 leg (sell only)
LEG_MODE = 1
# Minimum position: kam se kam 1 contract hona chahiye; ya 10% margin — dono me se jo zyada ho (order size kabhi 0 nahi)
MIN_POSITION_CONTRACTS = 1
# Main/Secondary OI comparison: "usd" = oi_value_usd (default), "contracts" = contract count (Delta Analytics jaisa)
MAIN_SYMBOL_OI_BY = "usd"  # Main = highest total OI (USD); rank hamesa: highest OI = Main, lower = Secondary
# PCR rule (fixed): PCR > 1 → SELL PUT, PCR < 1 → SELL CALL, PCR = 1 → NO TRADE
# =========================================================
# 🎚️ MARGIN ENFORCEMENT MODE
# =========================================================
# Single mode: minimum 10% margin on bias change, then strategy loop allowed
MARGIN_ENFORCEMENT_MODE = "SIGNAL"
# USER_AUTO_EXECUTION set from _load_settings() above
ENFORCE_EQUALITY_JOB = True   # 🔴 ON / 🟢 OFF
# =========================================================
# 🛑 KILL SWITCH
# =========================================================
KILL_SWITCH = False
_SCRIPT_START_TIME = time.time()
_LAST_SIGINT_TIME = 0.0

def kill_switch(sig, frame):
    """Double Ctrl+C within 3s = exit; single press = warning only."""
    global _LAST_SIGINT_TIME
    now = time.time()
    if now - _LAST_SIGINT_TIME <= 3.0:
        print("\n🛑 SAFE EXIT (Ctrl+C x2)", flush=True)
        script_exit_alert("Ctrl+C")
        sys.exit(0)
    _LAST_SIGINT_TIME = now
    print("\n⚠️  Ctrl+C dabaya — dobara 3 sec mein dabao exit ke liye.", flush=True)
signal.signal(signal.SIGINT, kill_switch)

def _sigterm_handler(sig, frame):
    print("\n🔴 SCRIPT KILLED (SIGTERM — terminal close ya new instance started)", flush=True)
    script_exit_alert("SIGTERM (terminal closed or duplicate launch)")
    sys.exit(0)

def _sighup_handler(sig, frame):
    print("\n🔴 SCRIPT KILLED (SIGHUP — terminal disconnected)", flush=True)
    script_exit_alert("SIGHUP (terminal disconnected)")
    sys.exit(0)

signal.signal(signal.SIGTERM, _sigterm_handler)
signal.signal(signal.SIGHUP, _sighup_handler)

# =========================================================
# 🔐 AUTH
# =========================================================
def get_server_time():
    try:
        # Fetching server time to avoid 401 clock-drift errors
        r = http_get(f"{BASE_URL}/v2/indices/t", timeout=5)
        return str(r.json()["result"]["timestamp"])
    except:
        return str(int(time.time()))

def sign(method, path, payload, ts):
    # Delta India signature: METHOD + TIMESTAMP + PATH + PAYLOAD
    if not API_SECRET:
        raise RuntimeError(f"Missing api_secret in {_SETTINGS_PATH}")
    msg = method + ts + path + payload
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def private_request(method, path, body=None, params=None):
    import json
    if not API_KEY:
        raise RuntimeError(f"Missing api_key in {_SETTINGS_PATH}")
    json_body = json.dumps(body) if body else ""
    
    # Build full path with query parameters for GET requests
    full_path = path
    if method == "GET" and params:
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        full_path = f"{path}?{query_string}"
    
    # Sync with server time
    ts = get_server_time()
    sig = sign(method, full_path, json_body, ts)
    
    headers = {
        "api-key": API_KEY,
        "timestamp": ts,
        "signature": sig,
        "Content-Type": "application/json"
    }
    
    url = BASE_URL + full_path
    if method == "GET":
        start = time.time()
        r = http_get(url, headers=headers, timeout=10)
        latency = int((time.time() - start) * 1000)
        
    else:
        start = time.time()
        r = http_post(url, headers=headers, data=json_body, timeout=10)
        latency = int((time.time() - start) * 1000)
        
    
    # This will now print the exact error message from Delta if it fails
    if r.status_code != 200:
        print(f"❌ API Error {r.status_code}: {r.text}")
        # 401 (e.g. ip_not_whitelisted) — Telegram pe bhi bhejo taaki user ko pata chale
        if r.status_code == 401:
            _send_401_telegram_once(r.status_code, r.text, path)
    else:
        # 200 OK — agar pehle 401 alert bheja tha to ab "error cleared" Telegram bhejo
        _send_401_cleared_telegram_once()
    r.raise_for_status()
    return r.json()["result"]
# =========================================================
# 🧾 PLACE ORDER (LIVE)
# =========================================================

def place_order(symbol, side, size, margin_mode=None, reason=None):
    """Bina reason ke order nahi — rule #5: hamesha reason mandatory. UI load hone ke baad hi order place.
    Minimum 1 position: opening/add orders kabhi 0 size nahi — max(1, 10% size)."""
    global ORDER_FAILURE_COOLDOWN_UNTIL
    if KILL_SWITCH:
        return None
    if size <= 0:
        return None
    # Opening/add orders: kam se kam 1 contract (minimum 1 position ya 10% dono me se jo zyada)
    _open_reasons = ("NEW_POSITION", "ADD_MISSING_LEG", "LOW_BALANCE_BIAS_ALIGN", "BIAS_CHANGE_SHORT_SWAP", "BIAS_CHANGE_BUILD", "BIAS_CHANGE_LONG_ROLL", "NEW_POSITION_TOPUP")
    if reason and str(reason).strip() in _open_reasons and 0 < size < MIN_POSITION_CONTRACTS:
        size = MIN_POSITION_CONTRACTS
    if not FIRST_UI_RENDER_DONE:
        return None  # Pehle UI load ho, phir strategy ke hisaab se order
    # ORDER_FAILURE_COOLDOWN: opening orders mat try karo; close (UNKNOWN_POSITION etc) allow — bias align ke liye
    _open_reasons_block = ("NEW_POSITION", "ADD_MISSING_LEG", "LOW_BALANCE_BIAS_ALIGN", "BIAS_CHANGE_SHORT_SWAP", "BIAS_CHANGE_BUILD", "BIAS_CHANGE_LONG_ROLL", "NEW_POSITION_TOPUP", "EQUALITY", "MARGIN_ENFORCE", "MIN_1_POSITION_LOW_BALANCE")
    if reason and str(reason).strip() in _open_reasons_block and time.time() < ORDER_FAILURE_COOLDOWN_UNTIL:
        return None  # Cooldown me — new order try mat karo
    if not reason or not str(reason).strip():
        send_telegram(
            f"⚠️ ORDER BLOCKED — No reason provided\n"
            f"Symbol: {symbol} Side: {side} Size: {size}\n"
            f"Rule: Bina reason ke order nahi jayega.\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}"
        )
        return None
    if margin_mode is None:
        margin_mode = MARGIN_MODE
    margin_mode = margin_mode.lower()  # isolated │ cross │ portfolio
    body = {
        "product_symbol": symbol,
        "size": size,
        "side": side,
        "order_type": "market_order",
        "margin_mode": margin_mode
    }

    try:
        start = time.time()
        res = private_request("POST", "/v2/orders", body)
        
        latency = int((time.time() - start) * 1000)
        API_LATENCY["ORDERS"] = latency
        api_recovered("ORDERS")

        # ✅ ORDER SUCCESS ALERT — reason hamesha bhejo taaki pata ho kyu order gaya
        msg = (
            f"✅ ORDER PLACED\n"
            f"Symbol : {symbol}\n"
            f"Side   : {side.upper()}\n"
            f"Size   : {size}\n"
            f"Time   : {datetime.now().strftime('%H:%M:%S')}"
        )
        if reason:
            msg += f"\nReason : {reason}"
        send_telegram(msg)
        ORDER_FAILURE_COOLDOWN_UNTIL = 0.0  # success pe cooldown clear

        return res

    except Exception as e:
        api_failure_alert("ORDERS", e)
        # Sirf OPENING order fail pe cooldown — close/square-off (bias align) fail pe cooldown nahi, taaki position bias direction me kar sake
        _open_reasons_cooldown = ("NEW_POSITION", "ADD_MISSING_LEG", "LOW_BALANCE_BIAS_ALIGN", "BIAS_CHANGE_SHORT_SWAP", "BIAS_CHANGE_BUILD", "BIAS_CHANGE_LONG_ROLL", "NEW_POSITION_TOPUP", "EQUALITY", "MARGIN_ENFORCE", "MIN_1_POSITION_LOW_BALANCE")
        if reason and str(reason).strip() in _open_reasons_cooldown:
            ORDER_FAILURE_COOLDOWN_UNTIL = time.time() + ORDER_FAILURE_COOLDOWN_SEC

        # 🛑 EQUALITY SUSPEND: 400 (insufficient_margin / invalid_contract) pe equality job bar-bar na chale
        err_str = str(e).lower()
        if (reason == "EQUALITY" and "400" in str(e) and
            ("insufficient_margin" in err_str or "invalid_contract" in err_str)):
            global EQUALITY_SUSPENDED, EQUALITY_SUSPENDED_UNTIL
            EQUALITY_SUSPENDED = True
            EQUALITY_SUSPENDED_UNTIL = time.time() + 600  # 10 min cooldown
            send_telegram(
                f"🛑 EQUALITY SUSPENDED (10 min)\n"
                f"Reason: {str(e)[:150]}\n"
                f"Ab equality job 10 min tak nahi chalegi — funds bachane ke liye.\n"
                f"Time: {datetime.now().strftime('%H:%M:%S')}"
            )

        # ❌ ORDER REJECT ALERT
        send_telegram(
            f"❌ ORDER REJECTED\n"
            f"Symbol : {symbol}\n"
            f"Side   : {side.upper()}\n"
            f"Size   : {size}\n"
            f"Time   : {datetime.now().strftime('%H:%M:%S')}\n"
            f"Reason : {str(e)[:200]}"
        )

        return None


def orders_api_health_check():
    """
    GET /v2/orders se ORDERS API ki health check — bina order place kiye.
    Latency update → UI mein ORDERS health dikh jayegi.
    """
    last_err = None
    for attempt in range(API_RETRY_COUNT):
        try:
            start = time.time()
            private_request("GET", "/v2/orders", params={"page_size": 1})
            latency = int((time.time() - start) * 1000)
            API_LATENCY["ORDERS"] = latency
            api_recovered("ORDERS")
            return True
        except Exception as e:
            last_err = e
            if attempt < API_RETRY_COUNT - 1:
                time.sleep(API_RETRY_DELAY_SEC)
    api_failure_alert("ORDERS", last_err)
    return False


# =========================================================
# 📊 MARKET DATA
# =========================================================
def get_spot(symbol=None):
    """Retry on fail → reconnect koshish. symbol=None uses global SYMBOL (for parallel safety pass symbol)."""
    sym = symbol if symbol is not None else SYMBOL
    last_err = None
    for attempt in range(API_RETRY_COUNT):
        try:
            start = time.time()
            r = http_get(
                f"{BASE_URL}/v2/tickers/{sym}USD",
                timeout=3
            )
            latency = int((time.time() - start) * 1000)
            API_LATENCY["SPOT"] = latency
            price = float(r.json()["result"]["mark_price"])
            api_recovered("SPOT")
            return price
        except Exception as e:
            last_err = e
            if attempt < API_RETRY_COUNT - 1:
                time.sleep(API_RETRY_DELAY_SEC)
    print("❌ SPOT FETCH FAILED (after retries):", last_err)
    api_failure_alert("SPOT", last_err)
    return 0

ALL_TICKERS_CACHE_SEC = 10.0
ALL_TICKERS_TOTAL_TIMEOUT_SEC = 12.0
LAST_ALL_TICKERS = None
LAST_ALL_TICKERS_AT = 0.0
LAST_ALL_TICKERS_FAILURE_AT = 0.0
ALL_TICKERS_FAILURE_COOLDOWN_SEC = 3.0
ALL_TICKERS_LOCK = threading.Lock()

def _fetch_json_with_total_timeout(url, connect_timeout=4.0, read_timeout=4.0, total_timeout=ALL_TICKERS_TOTAL_TIMEOUT_SEC):
    """Large responses par hard total timeout enforce karo; sirf read-timeout enough nahi hota."""
    started = time.monotonic()
    with http_get(url, timeout=(connect_timeout, read_timeout), stream=True) as r:
        r.raise_for_status()
        chunks = []
        for chunk in r.iter_content(chunk_size=512 * 1024):
            if time.monotonic() - started > total_timeout:
                raise requests.Timeout(f"Total timeout {total_timeout:.0f}s exceeded for {url}")
            if chunk:
                chunks.append(chunk)
        if time.monotonic() - started > total_timeout:
            raise requests.Timeout(f"Total timeout {total_timeout:.0f}s exceeded for {url}")
    return json.loads(b"".join(chunks))

def fetch_all_tickers(refresh=False):
    """Heavy /v2/tickers ko ek hi jagah se fetch + cache karo taaki startup/UI hang na ho."""
    global LAST_ALL_TICKERS, LAST_ALL_TICKERS_AT, LAST_ALL_TICKERS_FAILURE_AT
    now_ts = time.time()
    if (
        not refresh
        and LAST_ALL_TICKERS is not None
        and (now_ts - LAST_ALL_TICKERS_AT) < ALL_TICKERS_CACHE_SEC
    ):
        return LAST_ALL_TICKERS
    if (
        LAST_ALL_TICKERS is None
        and (now_ts - LAST_ALL_TICKERS_FAILURE_AT) < ALL_TICKERS_FAILURE_COOLDOWN_SEC
    ):
        return []
    with ALL_TICKERS_LOCK:
        now_ts = time.time()
        if (
            not refresh
            and LAST_ALL_TICKERS is not None
            and (now_ts - LAST_ALL_TICKERS_AT) < ALL_TICKERS_CACHE_SEC
        ):
            return LAST_ALL_TICKERS
        if (
            LAST_ALL_TICKERS is None
            and (now_ts - LAST_ALL_TICKERS_FAILURE_AT) < ALL_TICKERS_FAILURE_COOLDOWN_SEC
        ):
            return []
        try:
            payload = _fetch_json_with_total_timeout(f"{BASE_URL}/v2/tickers")
            result = payload.get("result", [])
            LAST_ALL_TICKERS = result if isinstance(result, list) else []
            LAST_ALL_TICKERS_AT = time.time()
            LAST_ALL_TICKERS_FAILURE_AT = 0.0
            api_recovered("SPOT")
            return LAST_ALL_TICKERS
        except Exception as e:
            LAST_ALL_TICKERS_FAILURE_AT = time.time()
            api_failure_alert("SPOT", e)
            return LAST_ALL_TICKERS or []

def _extract_symbol_spot_from_tickers(tickers, sym):
    """Full tickers payload se BTCUSD/ETHUSD spot nikaalo taaki extra request na lage."""
    target_symbol = f"{sym}USD"
    for t in tickers:
        if str(t.get("symbol") or "") != target_symbol:
            continue
        for key in ("spot_price", "mark_price", "close"):
            try:
                px = float(t.get(key, 0) or 0)
            except (ValueError, TypeError):
                px = 0.0
            if px > 0:
                return px
    for t in tickers:
        if t.get("contract_type") != "perpetual_futures":
            continue
        if str(t.get("underlying_asset_symbol") or "").upper() != sym:
            continue
        for key in ("spot_price", "mark_price", "close"):
            try:
                px = float(t.get(key, 0) or 0)
            except (ValueError, TypeError):
                px = 0.0
            if px > 0:
                return px
    return 0.0

def _quick_option_oi_totals_by_symbol(timeout=8):
    """Startup/main-symbol selection ke liye fast OI compare; full per-symbol scan se bachata hai."""
    try:
        result = fetch_all_tickers(refresh=True)
        totals = {
            "BTC": {"usd": 0.0, "contracts": 0.0},
            "ETH": {"usd": 0.0, "contracts": 0.0},
        }
        for t in result:
            if t.get("contract_type") not in ("call_options", "put_options"):
                continue
            parts = str(t.get("symbol") or "").split("-")
            if len(parts) < 2:
                continue
            inst = parts[1].upper()
            if inst not in totals:
                continue
            try:
                totals[inst]["usd"] += float(t.get("oi_value_usd", 0) or 0)
            except (ValueError, TypeError):
                pass
            try:
                totals[inst]["contracts"] += float(t.get("oi", 0) or 0)
            except (ValueError, TypeError):
                pass
        return totals
    except Exception as e:
        api_failure_alert("SPOT", e)
        return None

def _pick_main_symbol_by_quick_oi(default_symbol=None, timeout=8):
    """BTC/ETH me higher OI wala symbol fast path se choose karo."""
    fallback = default_symbol if default_symbol in ("BTC", "ETH") else SYMBOL
    totals = _quick_option_oi_totals_by_symbol(timeout=timeout)
    if not totals:
        return fallback
    metric = "contracts" if MAIN_SYMBOL_OI_BY == "contracts" else "usd"
    total_oi_btc = float(totals.get("BTC", {}).get(metric, 0) or 0)
    total_oi_eth = float(totals.get("ETH", {}).get(metric, 0) or 0)
    return "BTC" if total_oi_btc >= total_oi_eth else "ETH"

def extract_expiry(symbol):
    try:
        return datetime.strptime(symbol.split("-")[-1], "%d%m%y")
    except:
        return None

def _format_expiry_for_display(exp):
    """Format expiry for UI: 25-Feb-2026. exp = string (e.g. 2026-02-25) or date/datetime."""
    if exp is None:
        return ""
    try:
        if hasattr(exp, "strftime"):
            return exp.strftime("%d-%b-%Y")
        s = str(exp).strip()
        if not s:
            return ""
        # Try YYYY-MM-DD
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            d = datetime.strptime(s[:10], "%Y-%m-%d")
            return d.strftime("%d-%b-%Y")
        return s[:10] if len(s) >= 10 else s
    except Exception:
        return str(exp)[:12]

def _norm_cdf(x):
    """Standard normal CDF using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def estimate_iv_bs(spot, strike, expiry, mark, is_call, r=0.0):
    """
    Estimate IV from mark price using Black-Scholes. Bisection for sigma.
    spot, strike, mark in same units (USD per BTC). expiry = datetime. Returns IV as decimal (0.46 = 46%).
    """
    if spot <= 0 or strike <= 0 or mark <= 0:
        return None
    try:
        if hasattr(expiry, "date"):
            t = (expiry - datetime.now()).total_seconds() / (365.25 * 24 * 3600)
        else:
            t = (expiry - datetime.now()).total_seconds() / (365.25 * 24 * 3600)
    except Exception:
        return None
    if t <= 0:
        t = 1.0 / 365.0  # 1 day min
    def bs_price(sig):
        if sig <= 0:
            return 0.0
        sqrt_t = math.sqrt(t)
        d1 = (math.log(spot / strike) + (r + 0.5 * sig * sig) * t) / (sig * sqrt_t)
        d2 = d1 - sig * sqrt_t
        if is_call:
            return spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
        else:
            return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    lo, hi = 0.01, 4.0
    for _ in range(50):
        mid = (lo + hi) * 0.5
        p = bs_price(mid)
        if abs(p - mark) < 0.01:
            return mid
        if p < mark:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) * 0.5, 4)

def fetch_options(symbol=None, include_spot=False):
    """symbol=None uses global SYMBOL. Pass symbol for parallel safety (e.g. get_spot_and_oi_for_symbol)."""
    sym = symbol if symbol is not None else SYMBOL
    try:
        result = fetch_all_tickers(refresh=False)
        spot = _extract_symbol_spot_from_tickers(result, sym)
        opts = []
        for t in result:
            if t.get("contract_type") not in ("call_options", "put_options"):
                continue
            if sym not in t.get("symbol", ""):
                continue
            expiry = extract_expiry(t["symbol"])
            if not expiry:
                continue

            greeks = t.get("greeks") or {}
            raw_delta = float(greeks.get("delta", 0) or 0)
            raw_theta = float(greeks.get("theta", 0) or 0)
            # Delta: store magnitude; sign applied in portfolio (call +ve, put -ve)
            # Theta: keep sign (negative = time decay for long); gamma/vega stored as magnitude
            iv_val = (
                t.get("iv") or t.get("implied_volatility") or t.get("volatility")
                or greeks.get("iv") or greeks.get("implied_volatility")
            )
            if iv_val is not None:
                v = float(iv_val)
                iv_val = v * 100 if v <= 1 else v  # store as % (0.46 -> 46, 46 -> 46)
            else:
                # Fallback: estimate IV from Black-Scholes (mark price se)
                strike = float(t.get("strike_price", 0))
                mark = float(t.get("mark_price", 0))
                is_call = t.get("contract_type") == "call_options"
                if spot <= 0:
                    spot = get_spot(sym)
                if spot > 0 and strike > 0 and mark > 0:
                    iv_dec = estimate_iv_bs(spot, strike, expiry, mark, is_call)
                    iv_val = round(iv_dec * 100, 2) if iv_dec is not None else None
                else:
                    iv_val = None
            vol = t.get("volume") or t.get("volume_24h") or t.get("turnover")
            vol = int(vol) if vol is not None else None
            opts.append({
                "symbol": t["symbol"],
                "type": "call" if t["contract_type"] == "call_options" else "put",
                "strike": float(t["strike_price"]),
                "expiry": expiry,
                "oi": float(t.get("oi_value_usd", 0)),
                "oi_contracts": float(t.get("oi", 0) or 0),
                "delta": abs(raw_delta),
                "gamma": abs(float(greeks.get("gamma", 0) or 0)),
                "theta": raw_theta,
                "vega": abs(float(greeks.get("vega", 0) or 0)),
                "rho": float(greeks.get("rho", 0) or 0),
                "mark_price": float(t.get("mark_price", 0)),
                "bid": float((t.get("quotes") or {}).get("best_bid") or t.get("best_bid") or t.get("bid") or 0),
                "ask": float((t.get("quotes") or {}).get("best_ask") or t.get("best_ask") or t.get("ask") or 0),
                "iv_pct": iv_val,
                "volume": vol,
            })

        return (opts, spot) if include_spot else opts

    except Exception as e:
        api_failure_alert("SPOT", e)
        return ([], 0.0) if include_spot else []

# =========================================================
# 📊 OPTION METRICS (Ask, Bid, Theor, Intrinsic, TV, IV, Vol, DTE, Greeks)
# =========================================================

def get_option_delta(symbol, opts_all):
    for opt in opts_all:
        if opt.get('symbol') == symbol:
            return opt.get('delta', 0)
    return 0

def get_option_metrics(opt, spot):
    """
    opt: single option dict from fetch_options (symbol, strike, type, mark_price, bid, ask, delta, gamma, theta, vega, rho, iv_pct, volume, expiry).
    spot: current underlying price.
    Returns dict for UI: ask, bid, theor, intrinsic, time_value, iv_pct, volume, dte, delta, gamma, vega, theta, rho.
    """
    if not opt or spot <= 0:
        return None
    strike = float(opt.get("strike", 0))
    is_call = (opt.get("type") or "").lower() == "call"
    mark = float(opt.get("mark_price", 0))
    if is_call:
        intrinsic = max(0.0, spot - strike)
    else:
        intrinsic = max(0.0, strike - spot)
    time_value = max(0.0, mark - intrinsic)
    expiry = opt.get("expiry")
    if hasattr(expiry, "date"):
        dte = (expiry.date() - datetime.now().date()).days
    else:
        try:
            dte = (expiry - datetime.now().date()).days
        except Exception:
            dte = None
    return {
        "ask": float(opt.get("ask", 0)),
        "bid": float(opt.get("bid", 0)),
        "theor": mark,
        "intrinsic": intrinsic,
        "time_value": time_value,
        "iv_pct": opt.get("iv_pct"),
        "volume": opt.get("volume"),
        "dte": dte,
        "delta": float(opt.get("delta", 0)),
        "gamma": float(opt.get("gamma", 0)),
        "vega": float(opt.get("vega", 0)),
        "theta": float(opt.get("theta", 0)),
        "rho": float(opt.get("rho", 0)),
        "symbol": opt.get("symbol", ""),
    }

def portfolio_theor_intrinsic_tv_iv(positions, opts_all, spot):
    """
    Combined Theor, Intrinsic, Time value (USD) for full position; IV% weighted avg if available.
    Size signed: long > 0, short < 0. Contract = 0.001 BTC.
    Returns: (theor_usd, intrinsic_usd, time_value_usd, iv_pct_or_none)
    """
    by_sym = {o["symbol"]: o for o in opts_all}
    theor_usd = 0.0
    intrinsic_usd = 0.0
    iv_sum = 0.0
    iv_weight = 0.0
    for pos in positions:
        sym = pos.get("product_symbol", "")
        if not (sym.startswith("C-") or sym.startswith("P-")):
            continue
        try:
            size = float(pos.get("size", 0))
            if size == 0:
                continue
        except (ValueError, TypeError):
            continue
        opt = by_sym.get(sym)
        if not opt:
            continue
        mark = float(opt.get("mark_price", 0))
        strike = float(opt.get("strike", 0))
        is_call = (opt.get("type") or "").lower() == "call"
        sb = abs(size) * 0.001  # size in BTC
        if is_call:
            intr = max(0.0, spot - strike)
        else:
            intr = max(0.0, strike - spot)
        # Value = price per BTC * size_btc; short = negative
        theor_usd += mark * size * 0.001
        intrinsic_usd += intr * size * 0.001
        if opt.get("iv_pct") is not None:
            iv_sum += opt["iv_pct"] * abs(size)
            iv_weight += abs(size)
    time_value_usd = theor_usd - intrinsic_usd
    iv_avg = (iv_sum / iv_weight) if iv_weight > 0 else None
    return round(theor_usd, 2), round(intrinsic_usd, 2), round(time_value_usd, 2), round(iv_avg, 2) if iv_avg is not None else None

# =========================================================
# 📦 EXPIRY SPLIT
# =========================================================
def split_expiries(opts):
    """near = first expiry with date > today (orderable). Same-day expiry exclude — settlement ke baad 400 invalid_contract avoid.
    Past expiry already excluded. opts_future = sabhi future (today + aage) display/roll logic ke liye."""
    today = datetime.now().date()
    opts_future = []
    opts_orderable = []  # sirf kal aur aage — same-day expiry exclude (settlement ke baad order nahi)
    for o in opts:
        exp = o.get("expiry")
        if not exp:
            continue
        exp_date = exp.date() if hasattr(exp, "date") else exp
        if exp_date >= today:
            opts_future.append(o)
        if exp_date > today:  # strictly future — kabhi bhi expire/settle ho chuke contract pe order mat do
            opts_orderable.append(o)
    if not opts_future:
        return [], [], []  # sab expire — koi order mat do (invalid contract avoid)
    expiries = sorted(set(o["expiry"] for o in opts_future))
    # near/nxt for ORDER PLACEMENT: use orderable only — 400 (expired contract) avoid
    orderable_expiries = sorted(set(o["expiry"] for o in opts_orderable)) if opts_orderable else []
    if orderable_expiries:
        near = [o for o in opts_orderable if o["expiry"] == orderable_expiries[0]]
        nxt  = [o for o in opts_orderable if len(orderable_expiries) > 1 and o["expiry"] == orderable_expiries[1]]
    else:
        # sab same-day ya past — koi orderable nahi; near/nxt empty, all_opts se display chalega
        near, nxt = [], []
    return near, nxt, opts_future

def get_spot_and_oi_for_symbol(sym):
    """Return (spot, expiry_oi_list, all_expiry_max_pain, total_contracts) for given symbol.
    total_contracts = sum of oi (contract count) across all options — for Main/Secondary comparison with Delta Analytics.
    Uses one full tickers fetch per symbol and extracts spot from same payload when possible."""
    opts, spot = fetch_options(symbol=sym, include_spot=True)
    if spot <= 0:
        spot = get_spot(sym)
    total_contracts = sum(o.get("oi_contracts", 0) for o in opts)
    expiry_oi_list = []
    for exp in sorted(set(o["expiry"] for o in opts)):
        opts_exp = [o for o in opts if o["expiry"] == exp]
        c, p, pr = oi_pcr(opts_exp)
        mp = max_pain(opts_exp)
        expiry_oi_list.append((exp, c, p, pr, mp))
    mp_all = max_pain(opts)
    return spot, expiry_oi_list, mp_all, total_contracts


def get_decision_for_symbol(sym):
    """Return (decision, source) for given symbol using its OI (for bias monitoring when instrument switch pending)."""
    if STRATEGY_MODE == "PRICE_CHANGE_DIST":
        return get_price_change_distribution_decision()
    try:
        _, expiry_oi_list, _, _ = get_spot_and_oi_for_symbol(sym)
    except Exception:
        return ("NO TRADE", "NO OI")
    if not expiry_oi_list:
        return ("NO TRADE", "NO OI")
    el = sorted(expiry_oi_list, key=lambda i: i[0])
    pcr_n = el[0][3] if len(el) >= 1 else 0
    pcr_x = el[1][3] if len(el) >= 2 else 0
    ca = sum(i[1] for i in el)
    pa = sum(i[2] for i in el)
    pcr_a = round(pa / ca, 2) if ca else 0
    max_oi_row = max(el, key=lambda i: (i[1] or 0) + (i[2] or 0)) if el else (None, 0, 0, 0, None)
    ca_max = max_oi_row[1] if len(max_oi_row) >= 2 else 0
    pa_max = max_oi_row[2] if len(max_oi_row) >= 3 else 0
    return decide(pcr_n, pcr_x, pcr_a, ca, pa, ca_max, pa_max)


def is_near_expiry_cleared(positions, near_expiry_date):
    """
    Check if all near-expiry option positions are closed
    """
    for pos in positions:
        symbol = pos.get("product_symbol", "")
        if symbol.startswith(("C-", "P-")):
            expiry = extract_expiry(symbol)
            if expiry and expiry == near_expiry_date:
                if float(pos.get("size", 0)) != 0:
                    return False
    return True
# =========================================================
# 🎯 ATM OPTION SELECTOR
# =========================================================

def select_atm_option(opts, spot, side):
    """
    side = "call" or "put"
    returns the ATM option dict
    """
    filtered = [o for o in opts if o["type"] == side]
    if not filtered:
        return None

    atm = min(filtered, key=lambda o: abs(o["strike"] - spot))
    return atm


def select_delta_option(opts, side, target_delta=0.10):
    """
    side = "call" or "put"
    returns the option with delta closest to target_delta (e.g. 0.25)
    """
    filtered = [o for o in opts if o["type"] == side]
    if not filtered:
        return None
    return min(filtered, key=lambda o: abs(o["delta"] - target_delta))
# =========================================================
# 📐 OI + PCR
# =========================================================
def oi_pcr(opts):
    call = sum(o["oi"] for o in opts if o["type"] == "call")
    put  = sum(o["oi"] for o in opts if o["type"] == "put")
    pcr = round(put / call, 2) if call else 0
    return call, put, pcr

# =========================================================
# 🧠 GREEKS
# =========================================================
def greek_avg(opts):
    if not opts:
        return 0,0,0,0
    avg = lambda k: sum(o[k] for o in opts) / len(opts)
    return avg("delta"), avg("gamma"), avg("theta"), avg("vega")

def fetch_options_for_symbol(sym):
    """Fetch options for given symbol (BTC/ETH) without changing global SYMBOL permanently."""
    global SYMBOL
    orig = SYMBOL
    try:
        SYMBOL = sym
        return fetch_options() or []
    finally:
        SYMBOL = orig

def portfolio_greeks_sum(positions, opts_all):
    """Sum of delta, gamma, theta, vega, rho across all position legs. Size is signed (short < 0).
    Delta: call +ve, put -ve. Theta: keep API sign. Gamma/Vega/Rho: magnitude * size."""
    by_sym = {o["symbol"]: o for o in opts_all}
    sum_delta = sum_gamma = sum_theta = sum_vega = sum_rho = 0.0
    for pos in positions:
        sym = pos.get("product_symbol", "")
        if not (sym.startswith("C-") or sym.startswith("P-")):
            continue
        try:
            size = float(pos.get("size", 0))
            if size == 0:
                continue
        except (ValueError, TypeError):
            continue
        opt = by_sym.get(sym)
        if not opt:
            continue
        d = float(opt.get("delta", 0) or 0)
        signed_delta = d if (opt.get("type") == "call") else -d
        sum_delta += signed_delta * size
        sum_gamma += float(opt.get("gamma", 0) or 0) * size
        sum_theta += float(opt.get("theta", 0) or 0) * size
        sum_vega += float(opt.get("vega", 0) or 0) * size
        sum_rho += float(opt.get("rho", 0) or 0) * size
    return round(sum_delta, 4), round(sum_gamma, 6), round(sum_theta, 4), round(sum_vega, 4), round(sum_rho, 4)

# =========================================================
# 🎯 MAX PAIN
# =========================================================
def max_pain(opts):
    strikes = sorted(set(o["strike"] for o in opts))
    pain = {}
    for s in strikes:
        loss = 0
        for o in opts:
            if o["type"] == "call":
                loss += max(0, s - o["strike"]) * o["oi"]
            else:
                loss += max(0, o["strike"] - s) * o["oi"]
        pain[s] = loss
    return min(pain, key=lambda s: pain[s]) if pain else None

# =========================================================
# 💰 WALLET
# =========================================================
def wallet_usd():
    last_err = None
    for attempt in range(API_RETRY_COUNT):
        try:
            start = time.time()
            data = private_request("GET", "/v2/wallet/balances")
            latency = int((time.time() - start) * 1000)
            API_LATENCY["WALLET"] = latency
            api_recovered("WALLET")
            for w in data:
                if w.get("asset_symbol") in ["USD", "USDT"]:
                    balance = float(w.get("balance", 0))
                    blocked = float(w.get("blocked_margin", 0))
                    upnl = float(w.get("unrealized_pnl", 0))
                    return balance, blocked, upnl
            return 0.0, 0.0, 0.0
        except Exception as e:
            last_err = e
            if attempt < API_RETRY_COUNT - 1:
                time.sleep(API_RETRY_DELAY_SEC)
    api_failure_alert("WALLET", last_err)
    return 0.0, 0.0, 0.0


# =========================================================
# 📊 POSITIONS & UPNL
# =========================================================
def get_positions():
    """Fetch positions for current SYMBOL only (for execution logic). Retry on fail → reconnect koshish."""
    last_err = None
    for attempt in range(API_RETRY_COUNT):
        try:
            params = {"underlying_asset_symbol": SYMBOL}
            start = time.time()
            data = private_request("GET", "/v2/positions", params=params)
            latency = int((time.time() - start) * 1000)
            API_LATENCY["POSITIONS"] = latency
            api_recovered("POSITIONS")
            return data if isinstance(data, list) else []
        except Exception as e:
            last_err = e
            if attempt < API_RETRY_COUNT - 1:
                time.sleep(API_RETRY_DELAY_SEC)
    print(f"❌ POSITIONS API ERROR (after {API_RETRY_COUNT} retries): {last_err}")
    api_failure_alert("POSITIONS", last_err)
    return []

def get_all_positions():
    """Fetch positions for both BTC and ETH in parallel (for UI display)"""
    def _fetch(sym):
        try:
            params = {"underlying_asset_symbol": sym}
            data = private_request("GET", "/v2/positions", params=params)
            return data if isinstance(data, list) else []
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=2) as ex:
        r_btc, r_eth = list(ex.map(_fetch, ["BTC", "ETH"]))
    return r_btc + r_eth

def calculate_upnl(positions, spot_btc=0.0, spot_eth=0.0):
    """Calculate total unrealized P&L and position count from positions"""
    total_upnl_usd = 0.0
    total_upnl_btc = 0.0
    position_count = 0
    position_details = []  # Store individual position UPNLs

    # Get current market prices for all options
    market_data = {}
    delta_map = {}  # product_symbol -> signed delta (Calls: +, Puts: -)
    try:
        for t in fetch_all_tickers(refresh=False):
            if t.get("contract_type") in ("call_options", "put_options"):
                symbol = t.get("symbol")
                if symbol:
                    market_data[symbol] = {
                        "mark_price": float(t.get("mark_price", 0)),
                        "bid": float((t.get("quotes") or {}).get("best_bid") or t.get("best_bid") or t.get("bid") or 0),
                        "ask": float((t.get("quotes") or {}).get("best_ask") or t.get("best_ask") or t.get("ask") or 0),
                    }
                    try:
                        greeks = t.get("greeks") or {}
                        raw_delta = greeks.get("delta", None)
                        if raw_delta is not None:
                            mag = abs(float(raw_delta))
                            ct = str(t.get("contract_type") or "").lower()
                            signed_delta = -mag if ct == "put_options" else mag
                            delta_map[symbol] = signed_delta
                    except (ValueError, TypeError):
                        pass
    except Exception as e:
        print(f"❌ Failed to fetch market data for UPNL calculation: {e}")
        return 0.0, 0.0, 0, []

    for pos in positions:
        # Only count options positions (calls and puts)
        symbol = pos.get("product_symbol", "")
        # Check if it's a call or put option (Delta format: C-BTC-... or P-BTC-...)
        is_option = symbol.startswith("C-") or symbol.startswith("P-")

        if is_option:

            # Try to use API UPNL first, then manual calculation
            found_upnl = False
            upnl_usd_value = 0.0

            # Check for API UPNL fields
            upnl_fields = ["unrealized_pnl", "upnl", "unrealized_profit_loss", "pnl", "profit_loss",
                          "realized_pnl", "total_pnl", "mark_to_market", "mtm_pnl"]
            for field_name in upnl_fields:
                if field_name in pos and pos[field_name] is not None:
                    try:
                        upnl_usd_value = float(pos[field_name])
                        found_upnl = True
                        break
                    except (ValueError, TypeError):
                        pass

            # If no API UPNL found, calculate manually
            if not found_upnl:
                entry_price = pos.get("entry_price") or pos.get("average_price")
                size = pos.get("size", 0)
                current_data = market_data.get(symbol)
                if current_data and entry_price and size:
                    try:
                        entry_price = float(entry_price)
                        size = float(size)
                        mark_price = current_data["mark_price"]
                        # Delta options: 1 contract = 0.001 BTC notional; price is per contract in USD.
                        # Exchange UPNL = (mark - entry) * size * 0.001 to match platform display (USD).
                        upnl_usd_value = (mark_price - entry_price) * size * 0.001
                        found_upnl = True
                    except (ValueError, TypeError):
                        pass

            if found_upnl:
                total_upnl_usd += upnl_usd_value
                total_upnl_btc += upnl_usd_value
                position_count += 1

                # Store individual position details; phone format: C-71000-100226 x0.006 BTC
                size = float(pos.get("size", 0))
                direction = "LONG" if size > 0 else "SHORT"
                dir_letter = "L" if size > 0 else "S"
                option_type = "CALL" if symbol.startswith("C-") else "PUT"
                parts = symbol.split("-")
                instrument = parts[1] if len(parts) >= 2 else ""
                expiry_code = parts[-1] if len(parts) >= 4 else ""
                # Contract id phone jaisa: C-71000-100226 (type-strike-expiry)
                contract_id = f"{parts[0]}-{parts[2]}-{parts[3]}" if len(parts) >= 4 else symbol
                size_btc = abs(size) * 0.001  # 1 contract = 0.001 BTC
                try:
                    strike = float(parts[2]) if len(parts) >= 3 else 0
                    entry = float(pos.get("entry_price") or pos.get("average_price") or 0)
                    if option_type == "CALL":
                        breakeven = strike + entry
                    else:
                        breakeven = strike - entry
                    breakeven = round(breakeven, 0) if strike else None
                except (ValueError, TypeError):
                    breakeven = None
                position_details.append({
                    "symbol": symbol,
                    "type": f"{direction} {option_type}",
                    "size": abs(size),
                    "size_btc": size_btc,
                    "contract_id": contract_id,
                    "dir_letter": dir_letter,
                    "upnl": upnl_usd_value,
                    "delta": delta_map.get(symbol),
                    "instrument": instrument,
                    "expiry": expiry_code,
                    "breakeven": breakeven,
                    "entry_price": pos.get("entry_price", 0),
                    "current_price": market_data.get(symbol, {}).get("mark_price", 0) if market_data.get(symbol) else 0
                })

        else:
            # Futures (perpetual) position — BTCUSD / ETHUSD
            try:
                size = float(pos.get("size", 0))
                if size == 0:
                    continue
                entry_price = float(pos.get("entry_price") or pos.get("average_price") or 0)
                # UPNL: API field pehle, warna manual
                upnl_fut = 0.0
                found_fut = False
                for field_name in ["unrealized_pnl", "upnl", "unrealized_profit_loss", "pnl", "profit_loss"]:
                    if field_name in pos and pos[field_name] is not None:
                        try:
                            upnl_fut = float(pos[field_name])
                            found_fut = True
                            break
                        except (ValueError, TypeError):
                            pass
                if not found_fut:
                    mark_px = float(pos.get("mark_price") or pos.get("last_price") or 0)
                    if mark_px == 0:
                        # API ne mark_price nahi diya — spot se calculate karo
                        _inst = symbol.replace("USD", "").upper()
                        mark_px = spot_btc if _inst == "BTC" else (spot_eth if _inst == "ETH" else 0.0)
                    if mark_px > 0 and entry_price > 0:
                        upnl_fut = (mark_px - entry_price) * size * 0.001
                        found_fut = True
                total_upnl_usd += upnl_fut
                total_upnl_btc += upnl_fut
                position_count += 1
                direction = "LONG" if size > 0 else "SHORT"
                dir_letter = "L" if size > 0 else "S"
                mark_px_disp = float(pos.get("mark_price") or pos.get("last_price") or 0)
                position_details.append({
                    "symbol": symbol,
                    "type": f"{direction} FUTURES",
                    "size": abs(size),
                    "size_btc": abs(size) * 0.001,
                    "contract_id": symbol,
                    "dir_letter": dir_letter,
                    "upnl": upnl_fut,
                    "delta": None,
                    "instrument": symbol.replace("USD", ""),
                    "expiry": "PERP",
                    "breakeven": None,
                    "entry_price": entry_price,
                    "current_price": mark_px_disp,
                    "is_futures": True,
                })
            except (ValueError, TypeError):
                pass

    return total_upnl_usd, total_upnl_btc, position_count, position_details

def calculate_projected_profit(positions, spot_btc=None, spot_eth=None):
    """Projected P&L at expiry at current spot. Entry = USD per BTC (same as strike); size_btc = size * 0.001."""
    if spot_btc is None:
        try:
            spot_btc = get_spot("BTC")
        except Exception:
            spot_btc = 0.0
    if spot_eth is None:
        try:
            spot_eth = get_spot("ETH")
        except Exception:
            spot_eth = 0.0
    mult = 0.001  # size_btc = size * 0.001
    total = 0.0
    for pos in positions:
        symbol = pos.get("product_symbol", "")
        if not (symbol.startswith("C-") or symbol.startswith("P-")):
            continue
        try:
            size = float(pos.get("size", 0))
            if size == 0:
                continue
            entry = float(pos.get("entry_price") or pos.get("average_price") or 0)
            parts = symbol.split("-")
            if len(parts) < 3:
                continue
            inst = parts[1] if len(parts) >= 2 else "BTC"
            strike = float(parts[2])
            spot = spot_btc if inst == "BTC" else spot_eth
        except (ValueError, TypeError):
            continue
        size_btc = abs(size) * mult
        if symbol.startswith("C-"):
            intrinsic = max(0.0, spot - strike)  # USD per BTC
            total += (intrinsic - entry) * size_btc * (1 if size > 0 else -1)
        else:
            intrinsic = max(0.0, strike - spot)
            total += (intrinsic - entry) * size_btc * (1 if size > 0 else -1)
    return round(total, 2)

def calculate_max_loss(positions, spot_btc=None, spot_eth=None):
    """
    Portfolio max loss at expiry to match Delta app.
    For 3-leg SELL PUT (short put + long put + long call): worst case at S <= K_long_put,
    loss = (K_short_put - K_long_put)*size_btc - net_premium.
    For 3-leg SELL CALL (short call + long call + long put): worst case at S >= K_short_call,
    loss = (K_short_call - K_long_call)*size_btc - net_premium (when long call strike <= short call).
    Else fallback: sum of long premiums only (short call unlimited, short put not added alone).
    """
    if spot_btc is None:
        try:
            spot_btc = get_spot("BTC")
        except Exception:
            spot_btc = 0.0
    if spot_eth is None:
        try:
            spot_eth = get_spot("ETH")
        except Exception:
            spot_eth = 0.0
    mult = 0.001
    # Build legs per instrument and compute portfolio max loss for 3-leg structure
    for inst in ("BTC", "ETH"):
        short_put = short_call = long_put = long_call = None
        for pos in positions:
            symbol = pos.get("product_symbol", "")
            if not (symbol.startswith("C-") or symbol.startswith("P-")):
                continue
            try:
                size = float(pos.get("size", 0))
                if size == 0:
                    continue
                parts = symbol.split("-")
                if len(parts) < 3 or (parts[1] if len(parts) >= 2 else "") != inst:
                    continue
                entry = float(pos.get("entry_price") or pos.get("average_price") or 0)
                strike = float(parts[2])
                sz_btc = abs(size) * mult
            except (ValueError, TypeError):
                continue
            if symbol.startswith("P-") and size < 0:
                short_put = {"strike": strike, "entry": entry, "size_btc": sz_btc}
            elif symbol.startswith("P-") and size > 0:
                long_put = {"strike": strike, "entry": entry, "size_btc": sz_btc}
            elif symbol.startswith("C-") and size < 0:
                short_call = {"strike": strike, "entry": entry, "size_btc": sz_btc}
            elif symbol.startswith("C-") and size > 0:
                long_call = {"strike": strike, "entry": entry, "size_btc": sz_btc}
        # 3-leg SELL PUT: put spread (sell put - long put) max loss + long call premium
        # Put spread worst case = (K_sp - K_lp)*sb - (entry_sp - entry_lp)*sb; then + long call premium paid
        if short_put and long_put and long_call:
            sb = short_put["size_btc"]
            if (
                math.isclose(sb, long_put["size_btc"], rel_tol=1e-9, abs_tol=1e-12)
                and math.isclose(sb, long_call["size_btc"], rel_tol=1e-9, abs_tol=1e-12)
            ):
                put_spread_intrinsic = (short_put["strike"] - long_put["strike"]) * sb
                put_spread_net = (short_put["entry"] - long_put["entry"]) * sb  # received - paid
                put_spread_loss = put_spread_intrinsic - put_spread_net
                long_call_premium = long_call["entry"] * sb
                loss = put_spread_loss + long_call_premium
                if loss > 0:
                    return round(loss, 2)
        # 3-leg SELL CALL: call spread (sell call - long call) max loss + long put premium
        # Worst case S high: we pay (K_long - K_short)*sb on spread
        if short_call and long_call and long_put:
            sb = short_call["size_btc"]
            if (
                math.isclose(sb, long_call["size_btc"], rel_tol=1e-9, abs_tol=1e-12)
                and math.isclose(sb, long_put["size_btc"], rel_tol=1e-9, abs_tol=1e-12)
            ):
                call_spread_intrinsic = abs(long_call["strike"] - short_call["strike"]) * sb
                call_spread_net = (short_call["entry"] - long_call["entry"]) * sb
                call_spread_loss = call_spread_intrinsic - call_spread_net
                long_put_premium = long_put["entry"] * sb
                loss = call_spread_loss + long_put_premium
                if loss > 0:
                    return round(loss, 2)

    # Fallback: only long premiums (no short put/call added)
    total_loss = 0.0
    for pos in positions:
        symbol = pos.get("product_symbol", "")
        if not (symbol.startswith("C-") or symbol.startswith("P-")):
            continue
        try:
            size = float(pos.get("size", 0))
            if size == 0:
                continue
            entry = float(pos.get("entry_price") or pos.get("average_price") or 0)
            parts = symbol.split("-")
            if len(parts) < 3:
                continue
            size_btc = abs(size) * mult
        except (ValueError, TypeError):
            continue
        if symbol.startswith("C-") and size > 0:
            total_loss += entry * size_btc
        elif symbol.startswith("P-") and size > 0:
            total_loss += entry * size_btc
    return round(total_loss, 2)

def get_current_positions_directions(positions):
    """Check current positions and return their directions"""
    current_directions = []
    for pos in positions:
        symbol = pos.get("product_symbol", "")
        size = float(pos.get("size", 0))

        if symbol.startswith("C-"):  # Call option
            if size > 0:  # Long call
                current_directions.append("LONG CALL")
            elif size < 0:  # Short call
                current_directions.append("SHORT CALL")
        elif symbol.startswith("P-"):  # Put option
            if size > 0:  # Long put
                current_directions.append("LONG PUT")
            elif size < 0:  # Short put
                current_directions.append("SHORT PUT")
    return current_directions

def square_off_position(symbol, size, reason="CLOSE_POSITION"):
    """Square off a position by placing opposite order"""
    if size > 0:  # Long position, sell to close
        return place_order(symbol, "sell", abs(size), MARGIN_MODE.lower(), reason=reason)
    elif size < 0:  # Short position, buy to close
        return place_order(symbol, "buy", abs(size), MARGIN_MODE.lower(), reason=reason)
    return None
# =========================================================
# 🧠 DECISION ENGINE
# =========================================================
# 🔒 FIXED RULE — KABHI BHI CHANGE NA KAREIN (PCR-based strategies par apply):
#   PCR > 1  → SELL PUT
#   PCR < 1  → SELL CALL
#   PCR = 1  → NO TRADE
#   Ye rule MAX_OPEN_INTEREST, NEAR_PCR, NEXT_PCR, ALL_PCR, FIRST_PCR_CHANGE sabme same hai.
#   Invalid OI (ca_max = 0): flip mat karo — LAST_EXECUTED_SIGNAL hi rahega (future me kabhi change na karein).
# =========================================================
def decide(pcr_n, pcr_x, pcr_a, ca=0, pa=0, ca_max=0, pa_max=0):
    """
    PCR strategies me same rule: PCR > 1 → SELL PUT, PCR < 1 → SELL CALL, PCR == 1 → NO TRADE.
    """
    if STRATEGY_MODE == "PRICE_CHANGE_DIST":
        return get_price_change_distribution_decision()
    if STRATEGY_MODE == "MAX_OPEN_INTEREST":
        pcr_max = round((pa_max / ca_max), 2) if ca_max else 0
        if pcr_max < 1:
            return ("SELL CALL", "PRIMARY: MAX_OPEN_INTEREST")
        if pcr_max > 1:
            return ("SELL PUT", "PRIMARY: MAX_OPEN_INTEREST")
        return ("NO TRADE", "PRIMARY: MAX_OPEN_INTEREST (PCR=1)")
    if STRATEGY_MODE == "NEAR_PCR":
        # PCR < 1 → SELL CALL, PCR > 1 → SELL PUT, PCR == 1 → NO TRADE
        if pcr_n < 1:
            return ("SELL CALL", "PRIMARY: NEAR_PCR")
        if pcr_n > 1:
            return ("SELL PUT", "PRIMARY: NEAR_PCR")
        return ("NO TRADE", "PRIMARY: NEAR_PCR (PCR=1)")

    if STRATEGY_MODE == "NEXT_PCR":
        # PCR < 1 → SELL CALL, PCR > 1 → SELL PUT, PCR == 1 → NO TRADE
        if pcr_x < 1:
            return ("SELL CALL", "PRIMARY: NEXT_PCR")
        if pcr_x > 1:
            return ("SELL PUT", "PRIMARY: NEXT_PCR")
        return ("NO TRADE", "PRIMARY: NEXT_PCR (PCR=1)")

    if STRATEGY_MODE == "ALL_PCR":
        # PCR < 1 → SELL CALL, PCR > 1 → SELL PUT, PCR == 1 → NO TRADE
        if pcr_a < 1:
            return ("SELL CALL", "PRIMARY: ALL_PCR")
        if pcr_a > 1:
            return ("SELL PUT", "PRIMARY: ALL_PCR")
        return ("NO TRADE", "PRIMARY: ALL_PCR (PCR=1)")

    if STRATEGY_MODE == "FIRST_PCR_CHANGE":
        # Per-instrument ALL_PCR; instrument choice = jis me pehle PCR change (main loop)
        if pcr_a < 1:
            return ("SELL CALL", "PRIMARY: FIRST_PCR_CHANGE")
        if pcr_a > 1:
            return ("SELL PUT", "PRIMARY: FIRST_PCR_CHANGE")
        return ("NO TRADE", "PRIMARY: FIRST_PCR_CHANGE (PCR=1)")

    return "NO TRADE", "NO SIGNAL"

# Global variables for position tracking
PREVIOUS_POSITIONS = []
LAST_POSITION_CHECK = None
POSITION_CHANGE_LOG = []
LAST_EXPIRY_ROLL_TELEGRAM_AT = 0.0
CONSECUTIVE_ZERO_POSITION_CYCLES = 0  # 2 cycle confirm ke baad hi "position 0" maan ke nayi entry
LAST_MAX_OI_EXPIRY_STR = None  # MAX_OPEN_INTEREST: last expiry used — change pe Telegram bhejenge
EXPIRY_ROLL_CONFIRMATION_PRINTED = False  # ek bar confirm print: new contract me position ban gai
MARGIN_ADJUSTED_THIS_SIGNAL = False
# =========================================================
# 🧾 TRADE JOURNAL
# =========================================================
last_orders = deque(maxlen=10)

def detect_position_changes(current_positions):
    """Detect changes in positions (squared off, new positions, etc.)"""
    global PREVIOUS_POSITIONS, POSITION_CHANGE_LOG

    changes = []
    current_symbols = {pos.get("product_symbol"): pos for pos in current_positions}
    previous_symbols = {pos.get("product_symbol"): pos for pos in PREVIOUS_POSITIONS}

    # Check for squared off positions
    for symbol, prev_pos in previous_symbols.items():
        if symbol not in current_symbols:
            # Position was squared off
            prev_size = float(prev_pos.get("size", 0))
            direction = "LONG" if prev_size > 0 else "SHORT"
            option_type = "CALL" if symbol.startswith("C-") else "PUT"
            changes.append(f"🔄 SQUARED OFF: {direction} {option_type} │ {symbol}")
            POSITION_CHANGE_LOG.append(f"[{datetime.now().strftime('%H:%M:%S')}] SQUARED OFF: {direction} {option_type} │ {symbol}")

    # Check for new positions
    for symbol, curr_pos in current_symbols.items():
        if symbol not in previous_symbols:
            # New position opened
            curr_size = float(curr_pos.get("size", 0))
            direction = "LONG" if curr_size > 0 else "SHORT"
            option_type = "CALL" if symbol.startswith("C-") else "PUT"
            changes.append(f"🆕 NEW POSITION: {direction} {option_type} │ {symbol}")
            POSITION_CHANGE_LOG.append(f"[{datetime.now().strftime('%H:%M:%S')}] NEW POSITION: {direction} {option_type} │ {symbol}")

    # Update previous positions for next check
    PREVIOUS_POSITIONS = current_positions.copy()

    return changes

def get_position_summary(positions):
    """Get a summary of current positions"""
    summary = []
    for pos in positions:
        symbol = pos.get("product_symbol", "")
        size = float(pos.get("size", 0))
        if size != 0:  # Only include active positions
            direction = "LONG" if size > 0 else "SHORT"
            option_type = "CALL" if symbol.startswith("C-") else "PUT"
            summary.append(f"{direction} {option_type} {abs(size)}x {symbol}")
    return summary

def format_position_line_for_message(active_positions):
    """Har message me position dikhane ke liye ek format: '4 legs, Total 50x, each leg 12x qty' ya '0 X'."""
    n_legs = len(active_positions)
    if not n_legs:
        return "0 X"
    total_x = sum(abs(int(float(p.get("size", 0)))) for p in active_positions)
    per_leg = total_x // n_legs
    return f"{n_legs} legs, Total {total_x}x, each leg {per_leg}x qty"

def _format_new_position_telegram_lines(active_positions, decision, symbol, expiry_display, bal, blocked, pos_line, strategy_mode="—"):
    """Options ya Futures dono ka formatted Telegram message."""
    # ── FUTURES MODE ──────────────────────────────────────────────────────────
    if ORDER_TYPE == "futures":
        used_pct = (blocked / bal * 100) if bal > 0 else 0
        ts = datetime.now().strftime("%H:%M:%S")
        perp_sym = f"{symbol}USD"
        fut_pos = next((p for p in active_positions if p.get("product_symbol", "") == perp_sym), None)
        if not fut_pos:
            # all_positions me se bhi dhundho (options nahi wala)
            fut_pos = next(
                (p for p in active_positions
                 if not str(p.get("product_symbol", "")).startswith(("C-", "P-"))),
                None,
            )
        size_val = float(fut_pos.get("size", 0)) if fut_pos else 0
        entry_px = float(fut_pos.get("entry_price") or fut_pos.get("average_price") or 0) if fut_pos else 0
        direction_label = "Long Futures" if size_val > 0 else "Short Futures"
        direction_icon  = "📈" if size_val > 0 else "📉"
        lines = [
            f"📋 Account  : {MAIN_ACCOUNT.get('account_name', 'Main')}",
            f"Reason   : NEW_POSITION ({decision})",
            f"Strategy : {strategy_mode}",
            f"Bias     : {decision}",
            f"Sizing   : {_position_sizing_mode_label()}",
            f"Instrument : {perp_sym} | Perpetual",
            f"{direction_icon} Direction : {direction_label}",
            f"Size     : {abs(int(size_val))}x contracts",
            f"Entry    : ${entry_px:.2f}" if entry_px > 0 else "Entry    : —",
            f"Balance : ${bal:.2f}",
            f"Used : ${blocked:.2f} ({used_pct:.1f}%)",
            f"Time : {ts}",
        ]
        return "\n".join(lines)
    # ── OPTIONS MODE ──────────────────────────────────────────────────────────
    short_put = short_call = long_put = long_call = None
    short_put_delta = short_call_delta = long_put_delta = long_call_delta = None

    # Position API kabhi-kabhi greeks/delta field miss karta hai.
    # Isliye: pehle position dict se delta try karo, aur agar missing ho to options chain se map karke delta add karo.
    delta_map = {}
    try:
        target_syms = {
            p.get("product_symbol")
            for p in active_positions
            if p and p.get("product_symbol")
        }
        if target_syms:
            opts = fetch_options(symbol=symbol) if symbol else fetch_options()
            for o in opts:
                s = o.get("symbol")
                if s in target_syms and o.get("delta") is not None:
                    delta_map[s] = abs(float(o.get("delta", 0) or 0))
    except Exception:
        delta_map = {}

    def _safe_signed_delta(p):
        """
        Delta Exchange me:
        - Calls: positive delta
        - Puts : negative delta
        Position/chain API kabhi-kabhi sign/missing delta de sakta hai,
        isliye product type (C-/P-) ke basis par sign enforce karte hain.
        """
        if not p:
            return None
        try:
            sym_p = p.get("product_symbol", "") or ""
            d = p.get("delta", None)
            if d is not None:
                mag = abs(float(d))
                if sym_p.startswith("P-"):
                    return -mag
                return mag
        except (ValueError, TypeError):
            return None

        # Fallback: delta from options chain map (magnitude), then enforce put/call sign
        try:
            sym_p = p.get("product_symbol", "") or ""
            if sym_p and sym_p in delta_map:
                mag = delta_map[sym_p]
                if sym_p.startswith("P-"):
                    return -mag
                return mag
        except Exception:
            pass
        return None

    for p in active_positions:
        sym = p.get("product_symbol", "")
        size = float(p.get("size", 0))
        if sym.startswith("P-") and size < 0:
            short_put = sym
            short_put_delta = _safe_signed_delta(p)
        elif sym.startswith("C-") and size < 0:
            short_call = sym
            short_call_delta = _safe_signed_delta(p)
        elif sym.startswith("P-") and size > 0:
            long_put = sym
            long_put_delta = _safe_signed_delta(p)
        elif sym.startswith("C-") and size > 0:
            long_call = sym
            long_call_delta = _safe_signed_delta(p)
    # Exp as "06-Mar" (dd-Mon)
    exp_short = ""
    if expiry_display is not None:
        try:
            if hasattr(expiry_display, "strftime"):
                exp_short = expiry_display.strftime("%d-%b")
            else:
                exp_short = str(expiry_display)[:6] if len(str(expiry_display)) >= 6 else str(expiry_display)
        except Exception:
            exp_short = str(expiry_display)[:7] if expiry_display else ""
    if not exp_short and active_positions:
        ex = extract_expiry(active_positions[0].get("product_symbol", ""))
        if ex and hasattr(ex, "strftime"):
            exp_short = ex.strftime("%d-%b")
    leg_size = 0
    if active_positions:
        total_x = sum(abs(int(float(p.get("size", 0)))) for p in active_positions)
        n_legs = len(active_positions)
        leg_size = total_x // n_legs if n_legs else 0
    used_pct = (blocked / bal * 100) if bal > 0 else 0
    ts = datetime.now().strftime("%H:%M:%S")
    lines = [
        f"📋 Account  : {MAIN_ACCOUNT.get('account_name', 'Main')}",
        f"Reason   : NEW_POSITION ({decision})",
        f"Strategy : {strategy_mode}",
        f"Bias     : {decision}",
        f"Sizing   : {_position_sizing_mode_label()}",
        f"Instrument : {symbol} | Exp {exp_short} | Leg size : {leg_size}x",
        # ASCII "delta" label (Telegram copy/render me Unicode symbols kabhi-kabhi miss ho jate hain)
        f"Short Put  : {short_put or '—'}{'' if short_put_delta is None else f' (Δ {short_put_delta:.2f})'}",
        f"Short Call : {short_call or '—'}{'' if short_call_delta is None else f' (Δ {short_call_delta:.2f})'}",
        f"Long Put   : {long_put or '—'}{'' if long_put_delta is None else f' (Δ {long_put_delta:.2f})'}",
        f"Long Call  : {long_call or '—'}{'' if long_call_delta is None else f' (Δ {long_call_delta:.2f})'}",
        f"Balance : ${bal:.2f}",
        f"Used : ${blocked:.2f} ({used_pct:.1f}%)",
        f"Position : {pos_line}",
        f"Time : {ts}",
    ]
    return "\n".join(lines)

def identify_unknown_positions(current_positions, decision):
    """Unknown leg = wrong direction for bias. SELL CALL → short put unknown. SELL PUT → short call unknown.
    Sirf wrong SHORT leg return hota hai; long legs kabhi unknown nahi — bias change pe sirf sell leg sahi karte hain (dono long 10d ho to bhi)."""
    unknown_positions = []

    for pos in current_positions:
        symbol = pos.get("product_symbol", "")
        size = float(pos.get("size", 0))
        if size == 0:
            continue

        is_call = symbol.startswith("C-")
        is_put  = symbol.startswith("P-")

        # SELL CALL bias → correct: short call + long call + long put (3-leg). Wrong: short put.
        if decision == "SELL CALL":
            if is_put and size < 0:
                unknown_positions.append(pos)

        # SELL PUT bias → correct: short put + long call + long put (3-leg). Wrong: short call.
        elif decision == "SELL PUT":
            if is_call and size < 0:
                unknown_positions.append(pos)

    return unknown_positions


def _strike_from_position(pos):
    """Extract strike from position product_symbol (e.g. C-BTC-71000-100226 -> 71000)."""
    if not pos:
        return None
    sym = pos.get("product_symbol", "")
    parts = sym.split("-")
    if len(parts) < 3:
        return None
    try:
        return float(parts[2])
    except (ValueError, TypeError):
        return None


def validate_three_leg_strike_order(decision, short_pos, long_leg_a, long_leg_b):
    """
    Validate strike order only (order placement nahi, sirf check).
    SELL CALL: short call centre → long put niche (below), long call upar (above).
               Example: P-64000 < C-67000 short < C-70000 → Long put < Short call < Long call.
    SELL PUT:  short put centre → long put niche (below), long call upar (above).
               Example: P-66000 < P-67000 short < C-68000 → Long put < Short put < Long call.
    Returns (ok: bool, msg: str). When ok, position sahi hai (delta/strike move ho sakti hai).
    """
    if not (short_pos and long_leg_a and long_leg_b):
        return None, ""
    s_short = _strike_from_position(short_pos)
    s_a = _strike_from_position(long_leg_a)
    s_b = _strike_from_position(long_leg_b)
    if s_short is None or s_a is None or s_b is None:
        return None, ""
    if decision == "SELL CALL":
        # Long put niche (below short), long call upar (above short)
        ok = s_a < s_short < s_b
        msg = "Long put < Short call < Long call" if ok else "Strike order mismatch (SELL CALL)"
        return ok, msg
    if decision == "SELL PUT":
        # Long put niche (below short), long call upar (above short)
        ok = s_a < s_short < s_b
        msg = "Long put < Short put < Long call" if ok else "Strike order mismatch (SELL PUT)"
        return ok, msg
    return None, ""


def identify_wrong_or_extra_legs_to_close(current_positions, decision, opts_all=None, bias_changed=False):
    """
    Short = Long = Long: sirf 3 legs, sab ki qty same. Koi unknown/extra leg → turant close.
    SELL CALL: Short call = Long call = Long put. SELL PUT: Short put = Long put = Long call.
    - Unknown/wrong: short put in SELL CALL, short call in SELL PUT → close
    - Extra: 2+ short/long same type → keep one at target, close rest
    - Sirf Short=Long=Long 3 ki qty match — baki sab hata do
    """
    if decision not in ("SELL CALL", "SELL PUT"):
        return []

    short_calls = []
    long_calls = []
    short_puts = []
    long_puts = []

    for pos in current_positions:
        sym = pos.get("product_symbol", "")
        size = float(pos.get("size", 0))
        if size == 0:
            continue
        if sym.startswith("C-") and size < 0:
            short_calls.append(pos)
        elif sym.startswith("C-") and size > 0:
            long_calls.append(pos)
        elif sym.startswith("P-") and size < 0:
            short_puts.append(pos)
        elif sym.startswith("P-") and size > 0:
            long_puts.append(pos)

    to_close = []

    def keep_one_at_target(positions, target_size):
        """Keep exactly one position with size == target_size; close all others (wrong size or extra)."""
        kept = False
        for p in sorted(positions, key=lambda x: abs(float(x.get("size", 0))), reverse=True):
            if abs(float(p.get("size", 0))) == target_size and not kept:
                kept = True
                continue
            to_close.append(p)

    if decision == "SELL PUT":
        # Valid: 1 short put, 1 long put, 1 long call — same quantity. Rest = wrong leg.
        if not short_puts:
            return to_close
        target_size = abs(float(short_puts[0].get("size", 0)))
        if target_size <= 0:
            return to_close
        # Wrong direction: any short call
        to_close.extend(short_calls)
        # Extra short puts: keep one at target, close rest
        keep_one_at_target(short_puts, target_size)
        keep_one_at_target(long_calls, target_size)
        keep_one_at_target(long_puts, target_size)

    elif decision == "SELL CALL":
        # Valid: 1 short call, 1 long call, 1 long put — same quantity. Rest = wrong leg.
        if not short_calls:
            return to_close
        target_size = abs(float(short_calls[0].get("size", 0)))
        if target_size <= 0:
            return to_close
        # Wrong direction: any short put
        to_close.extend(short_puts)
        # Extra short calls: keep one at target, close rest
        keep_one_at_target(short_calls, target_size)
        keep_one_at_target(long_calls, target_size)
        keep_one_at_target(long_puts, target_size)

    if bias_changed and opts_all:
        for pos in current_positions:
            if pos in to_close:
                continue
            size = float(pos.get("size", 0))
            if size > 0:  # long positions (hedges)
                delta = get_option_delta(pos.get("product_symbol", ""), opts_all)
                if abs(delta - HEDGE_DELTA_TARGET) > DELTA_THRESHOLD:
                    to_close.append(pos)

    return to_close


def _manage_instrument_pass(sym, expiry_oi_list, spot_val):
    """
    Run wrong_extra + add_missing for given symbol. Returns True if any order placed/closed.
    Call with SYMBOL temporarily set to sym.
    """
    global SYMBOL
    saved = SYMBOL
    SYMBOL = sym
    try:
        positions = get_positions()
        opts = fetch_options()
        near, nxt, all_opts = split_expiries(opts or [])
        if not all_opts and not near:
            return False
        # decision
        if expiry_oi_list and len(expiry_oi_list) > 0:
            el = expiry_oi_list
            pcr_n = el[0][3] if len(el) >= 1 else 0
            pcr_x = el[1][3] if len(el) >= 2 else 0
            ca = sum(i[1] for i in el)
            pa = sum(i[2] for i in el)
            pcr_a = round(pa / ca, 2) if ca else 0
            max_oi_row = max(el, key=lambda i: (i[1] or 0) + (i[2] or 0)) if el else (None, 0, 0, 0, None)
            ca_max, pa_max = (max_oi_row[1], max_oi_row[2]) if len(max_oi_row) >= 3 else (0, 0)
            decision, _ = decide(pcr_n, pcr_x, pcr_a, ca, pa, ca_max, pa_max)
        else:
            cn, pn, pcr_n = oi_pcr(near)
            cx, px, pcr_x = oi_pcr(nxt)
            ca, pa, pcr_a = oi_pcr(all_opts)
            ca_max, pa_max = (cn, pn) if (cn + pn) >= (cx + px) else (cx, px)
            decision, _ = decide(pcr_n, pcr_x, pcr_a, ca, pa, ca_max, pa_max)
        if decision not in ("SELL CALL", "SELL PUT"):
            return False
        # legs
        sc, sp, lc, lp = None, None, None, None
        for pos in positions:
            s, sz = pos.get("product_symbol", ""), float(pos.get("size", 0))
            if s.startswith("C-") and sz < 0:
                sc = pos
            elif s.startswith("C-") and sz > 0:
                lc = pos
            elif s.startswith("P-") and sz < 0:
                sp = pos
            elif s.startswith("P-") and sz > 0:
                lp = pos
        # wrong_extra
        we = identify_wrong_or_extra_legs_to_close(positions, decision)
        for pos in we:
            sym_p, size = pos.get("product_symbol", ""), float(pos.get("size", 0))
            if sym_p and size != 0:
                square_off_position(sym_p, size, reason="WRONG_EXTRA_LEG")
                send_telegram(f"🔄 WRONG LEG CLOSED ({sym}): {sym_p} size {size}\nReason: 3-leg only, equal qty\nTime: {datetime.now().strftime('%H:%M:%S')}")
                return True
        # add_missing
        cnt = (1 if sc else 0) + (1 if lc else 0) + (1 if sp else 0) + (1 if lp else 0)
        if cnt >= 3:
            return False
        if not USER_AUTO_EXECUTION or API_DOWN:
            return False
        if not spot_val or spot_val <= 0:
            spot_val = get_spot()
        _exp_leg = None
        for _p in [sc, sp, lc, lp]:
            if _p:
                _exp_leg = extract_expiry(_p.get("product_symbol", ""))
                break
        _opts_exp = [o for o in all_opts if o["expiry"] == _exp_leg] if _exp_leg else (near or all_opts)
        _target_size = 0
        for _p in [sc, sp, lc, lp]:
            if _p:
                _target_size = max(_target_size, abs(int(float(_p.get("size", 0)))))
        _target_size = max(_target_size, 1)
        acted = False
        if "SELL PUT" in decision and sp and lc and not lp:
            put_10d = select_delta_option(_opts_exp, "put", 0.10)
            if put_10d:
                acted = bool(place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG"))
        elif "SELL PUT" in decision and sp and not lc and not lp:
            put_10d = select_delta_option(_opts_exp, "put", 0.10)
            call_10d = select_delta_option(_opts_exp, "call", 0.10)
            if put_10d and call_10d:
                place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                time.sleep(0.5)
                acted = bool(place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG"))
        elif "SELL PUT" in decision and lp and not sp and not lc:
            atm_put = select_atm_option(_opts_exp, spot_val, "put")
            call_10d = select_delta_option(_opts_exp, "call", 0.10)
            if atm_put and call_10d:
                place_order(atm_put["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                time.sleep(0.5)
                acted = bool(place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG"))
        elif "SELL PUT" in decision and lc and not sp and not lp:
            atm_put = select_atm_option(_opts_exp, spot_val, "put")
            put_10d = select_delta_option(_opts_exp, "put", 0.10)
            if atm_put and put_10d:
                place_order(atm_put["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                time.sleep(0.5)
                acted = bool(place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG"))
        elif "SELL PUT" in decision and sp and lp and not lc:
            call_10d = select_delta_option(_opts_exp, "call", 0.10)
            if call_10d:
                acted = bool(place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG"))
        elif "SELL CALL" in decision and sc and not lc and not lp:
            call_10d = select_delta_option(_opts_exp, "call", 0.10)
            put_10d = select_delta_option(_opts_exp, "put", 0.10)
            if call_10d and put_10d:
                place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                time.sleep(0.5)
                acted = bool(place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG"))
        elif "SELL CALL" in decision and lc and not sc and not lp:
            atm_call = select_atm_option(_opts_exp, spot_val, "call")
            put_10d = select_delta_option(_opts_exp, "put", 0.10)
            if atm_call and put_10d:
                place_order(atm_call["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                time.sleep(0.5)
                acted = bool(place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG"))
        elif "SELL CALL" in decision and lp and not sc and not lc:
            atm_call = select_atm_option(_opts_exp, spot_val, "call")
            call_10d = select_delta_option(_opts_exp, "call", 0.10)
            if atm_call and call_10d:
                place_order(atm_call["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                time.sleep(0.5)
                acted = bool(place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG"))
        elif "SELL CALL" in decision and sc and lp and not lc:
            call_10d = select_delta_option(_opts_exp, "call", 0.10)
            if call_10d:
                acted = bool(place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG"))
        elif "SELL CALL" in decision and sc and lc and not lp:
            put_10d = select_delta_option(_opts_exp, "put", 0.10)
            if put_10d:
                acted = bool(place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG"))
        if acted:
            send_telegram(f"🔧 ADD_MISSING ({sym}): Added missing leg(s)\nTime: {datetime.now().strftime('%H:%M:%S')}")
        return acted
    finally:
        SYMBOL = saved


def square_off_unknown_positions(unknown_positions):
    """Square off all unknown positions. Ctrl+C pe koi order nahi (KILL_SWITCH check)."""
    squared_off_count = 0

    for pos in unknown_positions:
        if KILL_SWITCH:
            return squared_off_count
        symbol = pos.get("product_symbol", "")
        size = float(pos.get("size", 0))

        if size != 0:
            print(f"🔄 Squaring off unknown position: {symbol} (size: {size})")
            res = square_off_position(symbol, size, reason="UNKNOWN_POSITION")
            if res:
                last_orders.append(
                    f"[{datetime.now().strftime('%H:%M:%S')}] SQUARE OFF UNKNOWN │ {symbol}"
                )
                # Remove from executed positions tracking
                
                squared_off_count += 1
            else:
                print(f"❌ Failed to square off: {symbol}")

    return squared_off_count


def square_off_current_instrument_for_switch(reason="INSTRUMENT_SWITCH"):
    """Current instrument ki saari option positions square off. reason = INSTRUMENT_SWITCH or FIRST_PCR_CHANGE. Returns count."""
    if KILL_SWITCH:
        return 0
    try:
        positions = get_positions()
    except Exception:
        return 0
    count = 0
    for pos in positions:
        symbol = pos.get("product_symbol", "")
        size = float(pos.get("size", 0))
        if not symbol.startswith(("C-", "P-")):
            continue
        if size == 0:
            continue
        print(f"🔄 Squaring off {symbol} (size: {size}) — reason: {reason}")
        res = square_off_position(symbol, size, reason=reason)
        if res:
            last_orders.append(
                f"[{datetime.now().strftime('%H:%M:%S')}] SQUARE OFF (switch) │ {symbol}"
            )
            count += 1
        else:
            print(f"❌ Failed to square off for switch: {symbol}")
    return count


# =========================================================
# 🔒 EXECUTION LOCK (IMPORTANT)
# =========================================================
API_ALERT_COOLDOWN = 300     # 5 min — DOWN alert spam nahi
API_RECOVERED_COOLDOWN = 300 # 5 min — RECOVERED alert spam nahi
API_DOWN = False
# API recover ke baad 10s tak koi order nahi — positions stable ho jayein, phir hi execute
API_RECOVERY_COOLDOWN_SEC = 10
API_RECOVERY_COOLDOWN_UNTIL = 0.0
API_RETRY_COUNT = 3       # API fail pe reconnect ki koshish — 3 baar retry
API_RETRY_DELAY_SEC = 1.5 # Retry ke beech delay
API_MIN_HEALTH_PCT = 25   # 25%+ health = position manage allow (kam health me bhi try); <25% = block
LAST_EXECUTED_SIGNAL = None
LAST_EXECUTION_TIME = None
LAST_EQUALITY_TIME = None
# NO TRADE alert: sirf ek hi baar per cycle (SELL CALL→NO TRADE→SELL PUT→NO TRADE→SELL CALL: har transition pe 1 alert)
NO_TRADE_ALERT_SENT_FOR_OLD_BIAS = None  # e.g. "SELL CALL" — is bias se NO TRADE pe alert bhej chuke
# =========================================================
# 📐 POSITION SIZE (MARGIN / MAX LOSS)
# =========================================================

def calc_position_size(balance, margin_pct, option_price):
    """
    balance     : total wallet balance (USD)
    margin_pct  : 0.10 = 10%, 0.20 = 20%
    option_price: estimated premium per contract (USD) — use option mark_price for 10% sizing

    returns number of contracts so that *total* 3-leg position uses ~margin_pct of balance (blocked).
    Minimum: 1 contract (MIN_POSITION_CONTRACTS) ya 10% ke liye jo size aaye dono me se jo zyada — order kabhi 0 nahi.
    """
    if balance <= 0 or option_price <= 0:
        return max(MIN_POSITION_CONTRACTS, 0)  # 0 mat do; caller ko at least MIN_POSITION_CONTRACTS use karna chahiye

    effective_pct = max(margin_pct, MIN_MARGIN_PCT)
    block_rate = 0.035  # portfolio margin: ~3.5% of premium blocked per contract

    if effective_pct >= 0.09 and effective_pct <= 0.11:
        # 10%: 3 legs — total blocked = 3 * size * option_price * block_rate = balance * 10%
        # size = (balance * 0.10) / (3 * option_price * block_rate); thoda oversize 1.15 so used ≥ 10%
        block_per_leg = option_price * block_rate
        if block_per_leg <= 0:
            return 1
        size = (balance * 0.10 * 1.15) / (3 * block_per_leg)
        size = int(max(1, size))
        # Cap: zyada se zyada ~11% target (overshoot avoid)
        cap_size = (balance * 0.11) / (3 * block_per_leg)
        size = min(size, max(1, int(cap_size)))
        return size

    # [m] 1% / 100% / other — existing logic
    margin_scale = 1.0
    target_used_pct = effective_pct
    if effective_pct <= 0.02:
        margin_scale = 3.0
        target_used_pct = 0.015
    elif effective_pct >= 0.99:
        margin_scale = 2.0
        target_used_pct = 0.55
    elif effective_pct >= 0.005 and effective_pct < 0.09:
        margin_scale = 1.5
    capital_to_use = balance * max(effective_pct, target_used_pct) * margin_scale
    size = int(capital_to_use / option_price)
    if option_price > 0 and effective_pct <= 0.02:
        min_for_1pct = int((balance * 0.01) / (option_price * 0.06)) if (option_price * 0.06) > 0 else 0
        size = max(size, min_for_1pct)
    return max(size, 1)

def estimate_three_leg_max_loss_per_unit(decision, short_opt, long_call_opt, long_put_opt):
    """
    Estimate max loss for 1 contract-per-leg 3-leg structure using option mark prices.
    Returns USD loss for 1x size set. Safer side: negative/invalid cases -> 0.
    """
    if decision not in ("SELL CALL", "SELL PUT"):
        return 0.0
    if not (short_opt and long_call_opt and long_put_opt):
        return 0.0
    try:
        mult = 0.001  # 1 contract = 0.001 BTC notional
        short_strike = float(short_opt.get("strike", 0) or 0)
        short_entry = float(short_opt.get("mark_price", 0) or 0)
        call_strike = float(long_call_opt.get("strike", 0) or 0)
        call_entry = float(long_call_opt.get("mark_price", 0) or 0)
        put_strike = float(long_put_opt.get("strike", 0) or 0)
        put_entry = float(long_put_opt.get("mark_price", 0) or 0)
    except (ValueError, TypeError):
        return 0.0
    if min(short_entry, call_entry, put_entry) <= 0:
        return 0.0
    if decision == "SELL PUT":
        put_spread_intrinsic = abs(short_strike - put_strike) * mult
        put_spread_net = (short_entry - put_entry) * mult
        loss = put_spread_intrinsic - put_spread_net + (call_entry * mult)
    else:
        call_spread_intrinsic = abs(call_strike - short_strike) * mult
        call_spread_net = (short_entry - call_entry) * mult
        loss = call_spread_intrinsic - call_spread_net + (put_entry * mult)
    return round(max(loss, 0.0), 6)

def calc_position_size_by_max_loss(balance, decision, short_opt, long_call_opt, long_put_opt, loss_pct=MAX_LOSS_CAP_PCT):
    """
    Size by max-loss cap: total strategy max loss <= balance * loss_pct.
    Returns 0 when even 1 set exceeds cap or estimate unavailable.
    """
    if balance <= 0 or loss_pct <= 0:
        return 0
    per_set_loss = estimate_three_leg_max_loss_per_unit(decision, short_opt, long_call_opt, long_put_opt)
    if per_set_loss <= 0:
        return 0
    loss_budget = balance * loss_pct
    return max(int(loss_budget / per_set_loss), 0)

def calc_futures_position_size(balance, margin_pct, spot_price):
    """
    FUTURES position sizing — margin mode.
    Contracts = (balance * margin_pct) / (spot_price * contract_size * initial_margin)
    For 0.001 BTC contracts with leverage: effective_margin = margin_pct (e.g., 10%)
    Returns number of contracts minimum 1.
    """
    if balance <= 0 or spot_price <= 0 or margin_pct <= 0:
        return 1
    
    effective_margin = max(margin_pct, 0.05)  # min 5% for safety
    multiplier = 0.001  # 1 contract = 0.001 BTC notional
    
    # Available margin to use
    margin_available = balance * effective_margin
    
    # Size = margin_available / (spot_price * multiplier * leverage) — but for simplicity:
    # If using 10% margin, can control ~size contracts at 10x leverage
    size = int(margin_available / (spot_price * multiplier))
    
    return max(size, 1)

def calc_futures_position_size_by_max_loss(balance, decision, spot_price, loss_pct=MAX_LOSS_CAP_PCT):
    """
    FUTURES position sizing — max loss mode.
    If spot moves by loss_pct (e.g., 10%), total loss should be balance * loss_pct.
    loss = (spot_price_change / spot_price) * contracts * spot_price * multiplier
    loss = spot_change * contracts * multiplier
    
    For 10% spot move resulting in 10% loss:
    0.10 * balance = (0.10 * spot) * contracts * 0.001
    contracts = balance / (spot * 0.001) = balance / spot * 1000
    
    But capped by MAX_USED_MARGIN_MULTIPLIER for safety.
    """
    if balance <= 0 or spot_price <= 0 or loss_pct <= 0:
        return 1
    
    multiplier = 0.001  # 1 contract = 0.001 BTC
    
    # Desired loss on 10% spot move (or loss_pct move)
    loss_budget = balance * loss_pct
    
    # If spot moves by loss_pct:
    # Loss = spot_move * contracts * multiplier
    # spot_move = spot_price * loss_pct
    # loss = (spot_price * loss_pct) * contracts * multiplier
    # contracts = loss_budget / (spot_price * loss_pct * multiplier)
    
    spot_move = spot_price * loss_pct
    size_raw = loss_budget / (spot_move * multiplier) if spot_move > 0 else 0
    
    # Safety cap: max margin used should respect MAX_USED_MARGIN_MULTIPLIER
    margin_safety_pct = min(0.99, max(0.01, 0.10 * MAX_USED_MARGIN_MULTIPLIER))  # 10% * 2 = 20% max
    size_safety = calc_futures_position_size(balance, margin_safety_pct, spot_price)
    
    size = min(int(size_raw), size_safety) if size_raw > 0 else 0
    
    return max(size, 1)

def create_or_manage_futures_position(symbol, decision, balance, spot_price):
    """
    Bias follow karo: SELL CALL → Short perpetual, SELL PUT → Long perpetual.
    Bias change pe opposite position close karke naya open karo.
    Returns: (position_created, reason_msg)
    """
    if not (decision and balance > 0 and spot_price > 0):
        return False, "Invalid parameters"
    
    try:
        positions = get_positions()
    except Exception as e:
        return False, f"Failed to fetch positions: {e}"
    
    # Find existing perpetual position
    perp_symbol = f"{symbol}USD"  # e.g., "BTCUSD" for perpetual
    existing_futures_pos = None
    for pos in positions:
        if pos.get("product_symbol", "") == perp_symbol:
            existing_futures_pos = pos
            break
    
    # Determine desired futures direction from the strategy decision
    if decision == "SELL CALL":
        desired_side = "sell"
        opposite_desc = "long"
        same_desc = "short"
        close_reason = "CLOSE_LONG_FOR_SHORT"
        success_msg = "Short futures"
    elif decision == "SELL PUT":
        desired_side = "buy"
        opposite_desc = "short"
        same_desc = "long"
        close_reason = "CLOSE_SHORT_FOR_PUT"
        success_msg = "Long futures"
    else:
        return False, f"Unknown decision: {decision}"

    if existing_futures_pos:
        current_size = float(existing_futures_pos.get("size", 0))
        if desired_side == "sell" and current_size < 0:
            return False, f"Futures already short {abs(current_size)}"
        if desired_side == "buy" and current_size > 0:
            return False, f"Futures already long {abs(current_size)}"

        if (desired_side == "sell" and current_size > 0) or (desired_side == "buy" and current_size < 0):
            # Close opposite direction first — pass signed size so square_off sends correct side
            res = square_off_position(perp_symbol, current_size, reason=close_reason)
            if not res:
                return False, f"Failed to close existing {opposite_desc} position"
            time.sleep(1)

    # Calculate size
    if POSITION_SIZING_MODE == "margin":
        size_dir = calc_futures_position_size(balance, MIN_MARGIN_PCT, spot_price)
    else:
        size_dir = calc_futures_position_size_by_max_loss(balance, decision, spot_price, MAX_LOSS_CAP_PCT)

    if size_dir <= 0:
        return False, "Position size too small (margin cap exceeded)"

    # Place desired futures order
    res = place_order(perp_symbol, desired_side, size_dir, MARGIN_MODE.lower(), reason="NEW_POSITION")
    if res:
        return True, f"{success_msg} {perp_symbol} x{size_dir} created"
    else:
        return False, f"Failed to place {same_desc} futures order"
    
    return False, f"Unknown decision: {decision}"

def enforce_strict_equal_legs(short_call, long_call, long_put):
    """
    SHORT CALL = LONG CALL = LONG PUT
    ❌ Square off / reduce / close kabhi nahi. ✅ Sirf ADD (jitna chahiye utna order).
    """

    if not (short_call and long_call and long_put):
        return []

    sc = abs(int(short_call["size"]))
    lc = abs(int(long_call["size"]))
    lp = abs(int(long_put["size"]))

    target = max(sc, lc, lp)
    adjustments = []

    if sc < target:
        adjustments.append({
            "symbol": short_call["product_symbol"],
            "side": "sell",
            "size": target - sc
        })

    if lc < target:
        adjustments.append({
            "symbol": long_call["product_symbol"],
            "side": "buy",
            "size": target - lc
        })

    if lp < target:
        adjustments.append({
            "symbol": long_put["product_symbol"],
            "side": "buy",
            "size": target - lp
        })

    return adjustments


def enforce_strict_equal_legs_put(short_put, long_call, long_put):
    """
    SELL PUT: put sell = call buy = put buy. SHORT PUT = LONG CALL = LONG PUT
    ❌ Square off kabhi na use. ✅ Sirf quantity equal karne ke liye jitna order chahiye utna ADD.
    """
    if not (short_put and long_call and long_put):
        return []

    sp = abs(int(short_put["size"]))
    lc = abs(int(long_call["size"]))
    lp = abs(int(long_put["size"]))

    target = max(sp, lc, lp)
    adjustments = []

    if sp < target:
        adjustments.append({
            "symbol": short_put["product_symbol"],
            "side": "sell",
            "size": target - sp
        })
    if lc < target:
        adjustments.append({
            "symbol": long_call["product_symbol"],
            "side": "buy",
            "size": target - lc
        })
    if lp < target:
        adjustments.append({
            "symbol": long_put["product_symbol"],
            "side": "buy",
            "size": target - lp
        })
    return adjustments


def enforce_strict_equal_2leg(short_pos, long_pos):
    """
    Tier 2: short + one 10d long — dono size equal. Sirf ADD (kabhi reduce nahi).
    short_pos, long_pos = position dicts with product_symbol, size.
    """
    if not (short_pos and long_pos):
        return []
    s_sz = abs(int(float(short_pos.get("size", 0))))
    l_sz = abs(int(float(long_pos.get("size", 0))))
    target = max(s_sz, l_sz, 1)
    adjustments = []
    if s_sz < target:
        adjustments.append({
            "symbol": short_pos["product_symbol"],
            "side": "sell",
            "size": target - s_sz
        })
    if l_sz < target:
        adjustments.append({
            "symbol": long_pos["product_symbol"],
            "side": "buy",
            "size": target - l_sz
        })
    return adjustments


def enforce_equal_legs_and_margin(
    balance,
    blocked,
    short_call,
    long_call,
    long_put,
    option_price_estimate
):
    """
    Ensures:
    - SHORT CALL = LONG CALL = LONG PUT
    - Minimum 10% margin usage
    """

    used_pct = blocked / balance if balance > 0 else 0

    # 🎯 Step 1: 1% / 10% / 100% — same scale as calc_position_size
    if MIN_MARGIN_PCT <= 0.02:
        target_pct = max(used_pct, MIN_MARGIN_PCT, 0.015)
        margin_scale = 3.0
    elif 0.09 <= MIN_MARGIN_PCT <= 0.11:
        target_pct = max(used_pct, MIN_MARGIN_PCT, 0.18)
        margin_scale = 9.0
    elif MIN_MARGIN_PCT >= 0.99:
        target_pct = max(used_pct, MIN_MARGIN_PCT, 0.55)
        margin_scale = 2.0
    else:
        target_pct = max(used_pct, MIN_MARGIN_PCT)
        margin_scale = 1.5 if MIN_MARGIN_PCT < 0.09 else 1.0
    target_capital = balance * target_pct * margin_scale

    # 🎯 Step 2: determine target contract size
    target_size = int(target_capital / option_price_estimate)
    target_size = max(target_size, 1)

    adjustments = []

    for leg in [short_call, long_call, long_put]:
        if not leg:
            continue

        current_size = abs(float(leg["size"]))
        diff = target_size - current_size

        if diff > 0:
            adjustments.append({
                "symbol": leg["product_symbol"],
                "side": "buy" if leg["size"] > 0 else "sell",
                "size": diff
            })

    return adjustments
def ensure_minimum_margin_usage(
    current_balance,
    current_blocked,
    symbol,
    side,
    base_size,
    margin_mode
):
    # Target = Min (10%); Ceiling = Min + 1% of Min (10.1%) — isse zyada mat add
    target_pct = MIN_MARGIN_PCT
    target_blocked = current_balance * target_pct
    ceiling_pct = min(MIN_MARGIN_PCT * 1.01, 0.99)
    max_blocked = current_balance * ceiling_pct  # Ceiling = max

    max_loops = 15
    loops = 0
    added_size = 0
    # used < Min pe zyada aggressive add taaki jaldi Min cross ho
    min_threshold = current_balance * MIN_MARGIN_PCT
    if target_pct >= 0.09 and current_blocked < min_threshold:
        add_mult = 4
    elif target_pct >= 0.09 and current_blocked < target_blocked * 0.6:
        add_mult = 3
    elif target_pct >= 0.5:
        add_mult = 2
    else:
        add_mult = 1

    while current_blocked < target_blocked:
        loops += 1
        if loops > max_loops:
            print("⚠️ Margin enforcement stopped — max attempts reached")
            break

        if current_blocked >= max_blocked:
            send_telegram(
                f"⚠️ MARGIN LIMIT REACHED\n"
                f"Blocked: ${current_blocked:.2f}\n"
                f"Max Allowed: ${max_blocked:.2f}"
            )
            break

        chunk = max(1, int(base_size * add_mult))
        if current_blocked >= target_blocked * 0.85 and add_mult > 1:
            add_mult = 1
            chunk = base_size
        res = place_order(symbol, side, chunk, margin_mode, reason="MARGIN_ENFORCE")
        if not res:
            break

        time.sleep(1)

        bal, blocked, _ = wallet_usd()
        current_blocked = blocked
        added_size += chunk

    return added_size


def ensure_10pct_after_entry_measure(leg1_symbol, leg1_side, leg2_symbol, leg2_side, leg3_symbol, leg3_side, margin_mode, current_size=None, reason="MARGIN_TOP_UP"):
    """
    Sirf jab 3 legs balanced hon. Margin balance: teeno pe add.
    current_size diya ho to ek shot me required qty add (time bachao). Nahi to +1 per round.
    reason: ORDER PLACED alert me dikhega — MARGIN_TOP_UP / NEW_POSITION_TOPUP.
    """
    if not (0.01 <= MIN_MARGIN_PCT <= 0.99):
        return
    target_pct = MIN_MARGIN_PCT
    cap_pct = min(MIN_MARGIN_PCT + 0.01, 0.99)
    legs = [(leg1_symbol, leg1_side), (leg2_symbol, leg2_side), (leg3_symbol, leg3_side)]

    bal, blocked, _ = wallet_usd()
    if bal <= 0 or blocked <= 0:
        return
    used_pct = blocked / bal
    if used_pct >= target_pct or used_pct >= cap_pct:
        return

    # Order ke baad exchange blocked update karta hai — thoda wait karo taaki pehla hisaab sahi ho
    if current_size and current_size > 0:
        time.sleep(1.0)

    bal, blocked, _ = wallet_usd()
    if bal <= 0 or blocked <= 0:
        return
    used_pct = blocked / bal
    if used_pct >= target_pct or used_pct >= cap_pct:
        return

    target_blocked = bal * target_pct
    deficit = max(0, target_blocked - blocked)
    if deficit <= 0:
        return

    if current_size and current_size > 0 and blocked > 0:
        # Jab tak minimum tak nahi pahunchta tab tak add — margin 10% tak
        max_rounds = 15
        for _ in range(max_rounds):
            if (blocked / bal) >= target_pct:
                break
            target_blocked = bal * target_pct
            cap_blocked = bal * cap_pct
            deficit = max(0, target_blocked - blocked)
            deficit_to_cap = max(0, cap_blocked - blocked)
            if deficit <= 0 or (blocked / bal) >= cap_pct or deficit_to_cap <= 0:
                break
            # Kitni sell=long=long aur chahiye 10% ke liye — ek saath itna add (cap se zyada mat)
            chunk_to_target = int(deficit * current_size / blocked)
            chunk_to_cap = int(deficit_to_cap * current_size / blocked) if blocked > 0 else 0
            if chunk_to_cap <= 0:
                break
            chunk = max(1, min(chunk_to_target, chunk_to_cap))  # ek saath saari position, sirf cap limit
            for sym, side in legs:
                place_order(sym, side, chunk, margin_mode, reason=reason)
                time.sleep(0.2)
            current_size += chunk
            time.sleep(0.3)
            bal, blocked, _ = wallet_usd()
            if bal <= 0 or blocked <= 0:
                break
        # Agar max_rounds ke baad bhi 10% se niche — ek aur round +1 per leg (rounding/API delay compensate)
        bal, blocked, _ = wallet_usd()
        if bal > 0 and blocked > 0 and (blocked / bal) < target_pct:
            for sym, side in legs:
                place_order(sym, side, 1, margin_mode, reason=reason)
                time.sleep(0.2)
        return

    # Fallback: +1 per round (jab current_size nahi pata) — jab tak min tak nahi pahunchta
    for _ in range(20):
        bal, blocked, _ = wallet_usd()
        if bal <= 0 or (blocked / bal) >= target_pct:
            break
        for sym, side in legs:
            place_order(sym, side, 1, margin_mode, reason=reason)
            time.sleep(0.2)
        time.sleep(0.3)
    return


# 🔥 Margin heatmap
def render_margin_heatmap(balance, blocked):
    if balance <= 0:
        return "N/A", "⚪ UNKNOWN", ""

    used_pct = (blocked / balance) * 100

    # Heat logic
    if used_pct < 10:
        level = "🟢 SAFE"
        color_code = "\033[92m"   # green
    elif used_pct < 15:
        level = "🟡 WARNING"
        color_code = "\033[93m"   # yellow
    else:
        level = "🔴 DANGER"
        color_code = "\033[91m"   # red

    # Visual bar (20 blocks = 100%) — color according to level
    filled = int(min(used_pct / 5, 20))  # 20 blocks = 100%
    reset = "\033[0m"
    bar = color_code + ("█" * filled) + reset + ("░" * (20 - filled))

    return round(used_pct, 2), level, bar
# =========================================================
# 📐 MARGIN DRIFT CALCULATOR
# =========================================================
def calculate_margin_drift(balance, blocked, target_pct):
    """
    Min = target (e.g. 10%), Ceiling = Min + 1% of Min (e.g. 10.1%).
    Drift: used >= Min → aligned; used < Min → under; used > Ceiling → over (pause).
    Priority: hamesha Min ke above margin; Ceiling se zyada mat jao.
    """
    if balance <= 0:
        return 0, 0, "UNKNOWN", "⚪", 0  # added ceiling as 5th return

    used_pct = (blocked / balance) * 100
    min_pct = target_pct * 100
    ceiling_pct = min_pct * 1.01  # Ceiling = Min + 1% of Min
    drift_pct = round(used_pct - min_pct, 2)

    if used_pct < min_pct:
        status = "UNDER-EXPOSED"
        icon = "🟡"
    elif used_pct <= ceiling_pct:
        status = "ALIGNED"
        icon = "🟢"
    else:
        status = "OVER-EXPOSED"
        icon = "🔴"

    return round(used_pct, 2), drift_pct, status, icon, ceiling_pct
# =========================================================
# 🌐 API HEALTH SCORE (COMBINED)
# =========================================================
def render_api_health_bar():
    """API health score — thresholds aligned with latency_status (<1s HEALTHY, 1–2s SLOW, 2–3s WARNING, >3s CRITICAL)."""
    score = 0
    count = 0

    for api in ["SPOT", "WALLET", "ORDERS", "POSITIONS"]:
        latency = API_LATENCY.get(api, -1)
        healthy = HEALTH.get(api, False)

        if not healthy or latency < 0:
            api_score = 0
        elif latency < 1000:
            api_score = 100   # HEALTHY
        elif latency < 2000:
            api_score = 75    # SLOW
        elif latency < 3000:
            api_score = 50    # WARNING
        else:
            api_score = 15    # CRITICAL

        score += api_score
        count += 1

    avg = int(score / count) if count else 0

    if avg >= 80:
        status = "🟢 HEALTHY"
        color_code = "\033[92m"   # green
    elif avg >= 50:
        status = "🟡 DEGRADED"
        color_code = "\033[93m"   # yellow
    else:
        status = "🔴 UNSTABLE"
        color_code = "\033[91m"   # red

    filled = int(avg / 5)
    filled = min(filled, 20)
    reset = "\033[0m"
    bar = color_code + ("█" * filled) + reset + ("░" * (20 - filled))

    return avg, status, bar
STRATEGIES_LIST = ["NEAR_PCR", "ALL_PCR", "NEXT_PCR", "MAX_OPEN_INTEREST", "FIRST_PCR_CHANGE", "PRICE_CHANGE_DIST"]

def keyboard_listener():
    global EQUALITY_MANUAL_EXEC, EQUALITY_SUSPENDED, EQUALITY_SUSPENDED_UNTIL
    global ENFORCE_EQUALITY_JOB, EQUALITY_ARMED
    global EQUALITY_EVENT, EQUALITY_TRIGGER_REASON
    global STRATEGY_MODE, MIN_MARGIN_PCT, MARGIN_MODE, POSITION_SIZING_MODE, ORDER_TYPE, USER_AUTO_EXECUTION
    global INSTRUMENT_SWITCH_PENDING, INSTRUMENT_SWITCH_SQUARE_OFF_SENT, INSTRUMENT_SWITCH_BIAS_CHANGE_DETECTED
    global INSTRUMENT_SWITCH_BIAS_CHANGE_MSG, LAST_BIAS_BTC, LAST_BIAS_ETH, CMD_FEEDBACK, CMD_FEEDBACK_CLEAR_AT, LAST_CMD_SHOWN
    global Q_EXIT_CONFIRM_UNTIL, LAST_IGNORED_INPUT_LOG_AT, LOG_ENABLED
    allowed_keys = set("mixaupk123456rqtls")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
    except Exception:
        old_settings = None
    def _restore_tty():
        if old_settings is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass
    atexit.register(_restore_tty)

    while True:
        if select.select([sys.stdin], [], [], 0.1)[0]:
            try:
                raw = os.read(fd, 4096)
            except OSError:
                continue
            if not raw:
                continue
            # Ctrl+C (\x03) — raw mode me SIGINT nahi aata, handle karo directly
            if b"\x03" in raw:
                _restore_tty()
                print("\n🛑 SAFE EXIT (Ctrl+C)", flush=True)
                script_exit_alert("Ctrl+C")
                sys.exit(0)
            typed = raw.decode("utf-8", errors="ignore").lower()
            typed = "".join(ch for ch in typed if ch not in "\r\n")
            if len(typed) != 1 or typed not in allowed_keys:
                if typed and (time.time() - LAST_IGNORED_INPUT_LOG_AT >= 2.0):
                    LAST_IGNORED_INPUT_LOG_AT = time.time()
                continue
            key = typed

            if key == "m":
                global MARGIN_EDIT_PENDING
                MARGIN_EDIT_PENDING = True
                CMD_FEEDBACK = "⌨️ [m] Enter min margin % (1-100) in terminal"
                CMD_FEEDBACK_CLEAR_AT = time.time() + 8
                LAST_CMD_SHOWN = {"msg": "[m] Min margin % — type 1 to 100 & Enter", "until": time.time() + 15}

            if key == "i":
                if INSTRUMENT_SWITCH_PENDING:
                    # Second [i] press = cancel switch, stay on current instrument
                    INSTRUMENT_SWITCH_PENDING = False
                    INSTRUMENT_SWITCH_SQUARE_OFF_SENT = False
                    INSTRUMENT_SWITCH_BIAS_CHANGE_DETECTED = False
                    INSTRUMENT_SWITCH_BIAS_CHANGE_MSG = ""
                    LAST_BIAS_BTC = LAST_BIAS_ETH = None
                    CMD_FEEDBACK = f"⌨️ [i] Switch cancelled — staying on {SYMBOL}"
                    CMD_FEEDBACK_CLEAR_AT = time.time() + 5
                    LAST_CMD_SHOWN = {"msg": f"[i] Switch cancelled — staying on {SYMBOL}", "until": time.time() + 10}
                    send_telegram(
                        f"⌨️ SHORTCUT [i] (cancel)\n"
                        f"Switch cancelled — staying on {SYMBOL}\n"
                        f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                else:
                    INSTRUMENT_SWITCH_PENDING = True
                    INSTRUMENT_SWITCH_BIAS_CHANGE_DETECTED = False
                    INSTRUMENT_SWITCH_SQUARE_OFF_SENT = False
                    LAST_BIAS_BTC = None
                    LAST_BIAS_ETH = None
                    next_sym = "ETH" if SYMBOL == "BTC" else "BTC"
                    CMD_FEEDBACK = f"⌨️ [i] Switch → {next_sym} (wait for bias change in BTC/ETH)"
                    CMD_FEEDBACK_CLEAR_AT = 0.0
                    if next_sym == "ETH":
                        LAST_CMD_SHOWN = {"msg": f"[i] Switch → ETH when bias changes in BTC or ETH", "until": time.time() + 86400}
                    else:
                        LAST_CMD_SHOWN = {"msg": f"[i] Switch → BTC when bias changes in BTC or ETH", "until": time.time() + 86400}
                    send_telegram(
                        f"⌨️ SHORTCUT [i]\n"
                        f"Instrument switch pending → {next_sym}\n"
                        f"Waiting for bias change in either BTC or ETH; then close {SYMBOL} positions and switch.\n"
                        f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )

            elif key == "x":
                CMD_FEEDBACK = f"⌨️ [x] Auto Execution {'ON' if not USER_AUTO_EXECUTION else 'OFF'}"
                CMD_FEEDBACK_CLEAR_AT = time.time() + 5
                LAST_CMD_SHOWN = {"msg": f"[x] Auto Exec → {'ON' if not USER_AUTO_EXECUTION else 'OFF'}", "until": time.time() + 30}
                USER_AUTO_EXECUTION = not USER_AUTO_EXECUTION
                _save_settings()
                send_telegram(
                    f"⌨️ SHORTCUT [x]\n"
                    f"Auto Execution : {'ON' if USER_AUTO_EXECUTION else 'OFF'}\n"
                    f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

            elif key == "a":
                global ACCOUNT_NAME_EDIT_PENDING
                ACCOUNT_NAME_EDIT_PENDING = True
                CMD_FEEDBACK = "⌨️ [a] Enter account name (main/sub) in terminal"
                CMD_FEEDBACK_CLEAR_AT = time.time() + 8
                LAST_CMD_SHOWN = {"msg": "[a] Edit account name (main/sub) — type in terminal & Enter", "until": time.time() + 15}

            elif key == "u":
                global USER_EDIT_PENDING
                USER_EDIT_PENDING = True
                CMD_FEEDBACK = "⌨️ [u] Enter User (name, email, phone) in terminal"
                CMD_FEEDBACK_CLEAR_AT = time.time() + 8
                LAST_CMD_SHOWN = {"msg": "[u] Edit User — name, email, phone (3 prompts)", "until": time.time() + 15}

            elif key == "p":
                current = MARGIN_MODE.lower()
                try:
                    idx = (MARGIN_MODES_CYCLE.index(current) + 1) % len(MARGIN_MODES_CYCLE)
                except ValueError:
                    idx = 0
                MARGIN_MODE = MARGIN_MODES_CYCLE[idx]
                _save_settings()
                mode_label = MARGIN_MODE.capitalize()
                CMD_FEEDBACK = f"⌨️ [p] Margin mode → {mode_label}"
                CMD_FEEDBACK_CLEAR_AT = time.time() + 5
                LAST_CMD_SHOWN = {"msg": f"[p] Margin mode → {mode_label}", "until": time.time() + 30}
                send_telegram(
                    f"⌨️ SHORTCUT [p]\n"
                    f"Margin mode : {mode_label}\n"
                    f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

            elif key == "k":
                POSITION_SIZING_MODE = "max_loss" if POSITION_SIZING_MODE == "margin" else "margin"
                _save_settings()
                mode_label = _position_sizing_mode_label()
                CMD_FEEDBACK = f"⌨️ [k] Position sizing → {mode_label}"
                CMD_FEEDBACK_CLEAR_AT = time.time() + 5
                LAST_CMD_SHOWN = {"msg": f"[k] Position sizing → {mode_label}", "until": time.time() + 30}
                send_telegram(
                    f"⌨️ SHORTCUT [k]\n"
                    f"Position sizing : {mode_label}\n"
                    f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

            elif key == "t":
                current = ORDER_TYPE
                try:
                    idx = (ORDER_TYPES_CYCLE.index(current) + 1) % len(ORDER_TYPES_CYCLE)
                except ValueError:
                    idx = 0
                ORDER_TYPE = ORDER_TYPES_CYCLE[idx]
                _save_settings()
                order_type_label = "Options Delta 50/10/10" if ORDER_TYPE == "Options_Delta_50/10/10" else "Short Futures"
                CMD_FEEDBACK = f"⌨️ [t] Order type → {order_type_label}"
                CMD_FEEDBACK_CLEAR_AT = time.time() + 5
                LAST_CMD_SHOWN = {"msg": f"[t] Order type → {order_type_label}", "until": time.time() + 30}
                send_telegram(
                    f"⌨️ SHORTCUT [t]\n"
                    f"Order type : {order_type_label}\n"
                    f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

            elif key in ("1", "2", "3", "4", "5", "6"):
                CMD_FEEDBACK = f"⌨️ [{key}] Strategy → {STRATEGIES_LIST[int(key)-1]}"
                CMD_FEEDBACK_CLEAR_AT = time.time() + 5
                LAST_CMD_SHOWN = {"msg": f"[{key}] Strategy → {STRATEGIES_LIST[int(key)-1]}", "until": time.time() + 30}
                idx = int(key) - 1
                if idx < len(STRATEGIES_LIST):
                    STRATEGY_MODE = STRATEGIES_LIST[idx]
                    _save_settings()
                    print(f"\n📊 STRATEGY CHANGED → {STRATEGY_MODE}")
                    send_telegram(
                        f"⌨️ SHORTCUT [{key}]\n"
                        f"Strategy : {STRATEGY_MODE}\n"
                        f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )

            elif key == "r":
                _reset_settings_to_defaults()
                CMD_FEEDBACK = "⌨️ [r] All settings reset to script defaults"
                CMD_FEEDBACK_CLEAR_AT = time.time() + 6
                LAST_CMD_SHOWN = {"msg": "[r] Reset — margin 10%, sizing Max Loss 10%, account Main, strategy ALL_PCR, order type Iron Fly, auto ON", "until": time.time() + 25}
                print("\n🔄 All settings reset to script defaults (margin 10%, sizing Max Loss 10%, Main, ALL_PCR, Iron Fly, auto ON).")
                send_telegram(
                    f"⌨️ SHORTCUT [r] — RESET\n"
                    f"All settings restored to script defaults.\n"
                    f"Margin: 10% | Sizing: Max Loss 10% | Account: Main | Strategy: ALL_PCR | Order Type: Iron Fly | Auto: ON\n"
                    f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

            elif key == "l":
                LOG_ENABLED = not LOG_ENABLED
                _save_settings()
                status = "ON" if LOG_ENABLED else "OFF"
                CMD_FEEDBACK = f"⌨️ [l] Log file → {status}"
                CMD_FEEDBACK_CLEAR_AT = time.time() + 5
                LAST_CMD_SHOWN = {"msg": f"[l] Log file → {status}", "until": time.time() + 30}
                print(f"\n📋 Log file: {status}", flush=True)
                send_telegram(
                    f"⌨️ SHORTCUT [l]\n"
                    f"Log file : {status}\n"
                    f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

            elif key == "s":
                _restore_tty()
                print("\n🔄 RESTARTING SCRIPT ([s])...", flush=True)
                send_telegram(
                    f"🔄 SCRIPT RESTARTING (manual [s])\n"
                    f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                time.sleep(0.5)
                os.execv(sys.executable, [sys.executable] + sys.argv)

            elif key == "q":
                _restore_tty()
                print("\n🛑 SAFE EXIT ([q])", flush=True)
                script_exit_alert("Manual quit (Q)")
                sys.exit(0)
if sys.stdin.isatty():
    threading.Thread(target=keyboard_listener, daemon=True).start()
else:
    print("⚠️ Keyboard shortcuts disabled (not running in real terminal)") 
# =========================================================
# 🖥️ TERMINAL UI RENDERER
# =========================================================
if FORCE_UI_REFRESH:
    FORCE_UI_REFRESH = False

def _fmt_oi(v):
    if v >= 1e6:
        return f"{v/1e6:.2f}M"
    if v >= 1e3:
        return f"{v/1e3:.2f}K"
    return f"{v:.0f}"

PRICE_CHANGE_ADV_THRESHOLD_PCT = 0.25
PRICE_CHANGE_BIAS_MIN_PCT = 0.60   # 60% advancers → SELL PUT, 60% decliners → SELL CALL; baaki NO TRADE
PRICE_CHANGE_BAR_WIDTH = 24
PRICE_CHANGE_CACHE_SEC = 2.0
LAST_PRICE_CHANGE_DISTRIBUTION = None
LAST_PRICE_CHANGE_DISTRIBUTION_AT = 0.0

def fetch_price_change_distribution(refresh=False):
    """Delta app style distribution: perpetuals universe, 24h mark change, advancers if > threshold."""
    global LAST_PRICE_CHANGE_DISTRIBUTION, LAST_PRICE_CHANGE_DISTRIBUTION_AT
    now_ts = time.time()
    if (
        not refresh
        and LAST_PRICE_CHANGE_DISTRIBUTION is not None
        and (now_ts - LAST_PRICE_CHANGE_DISTRIBUTION_AT) < PRICE_CHANGE_CACHE_SEC
    ):
        return LAST_PRICE_CHANGE_DISTRIBUTION
    try:
        result = fetch_all_tickers(refresh=refresh)
        universe = 0
        advancers = 0
        threshold = PRICE_CHANGE_ADV_THRESHOLD_PCT
        for t in result:
            if t.get("contract_type") != "perpetual_futures":
                continue
            trading_status = str(t.get("product_trading_status") or "").lower()
            if trading_status and trading_status != "operational":
                continue
            raw_change = t.get("mark_change_24h")
            try:
                change_pct = float(raw_change) if raw_change is not None else None
            except (ValueError, TypeError):
                change_pct = None
            if change_pct is None:
                try:
                    open_px = float(t.get("open", 0) or 0)
                    mark_px = float(t.get("mark_price", 0) or 0)
                    if open_px <= 0 or mark_px <= 0:
                        continue
                    change_pct = ((mark_px - open_px) / open_px) * 100
                except (ValueError, TypeError, ZeroDivisionError):
                    continue
            universe += 1
            if change_pct > threshold:
                advancers += 1
        if universe > 0:
            LAST_PRICE_CHANGE_DISTRIBUTION = {
                "advancers": advancers,
                "decliners": max(universe - advancers, 0),
                "universe": universe,
                "threshold_pct": threshold,
                "basis": "mark_change_24h",
            }
            LAST_PRICE_CHANGE_DISTRIBUTION_AT = now_ts
    except Exception as e:
        api_failure_alert("SPOT", e)
    return LAST_PRICE_CHANGE_DISTRIBUTION

def get_price_change_distribution_decision(refresh=False):
    """Market breadth strategy: bias change sirf tab jab advancers ya decliners >= 60% universe."""
    dist = fetch_price_change_distribution(refresh=refresh)
    if not dist:
        return ("NO TRADE", "PRIMARY: PRICE_CHANGE_DIST (NO DATA)")
    advancers = int(dist.get("advancers", 0) or 0)
    decliners = int(dist.get("decliners", 0) or 0)
    universe = int(dist.get("universe", 0) or 0)
    threshold = float(dist.get("threshold_pct", PRICE_CHANGE_ADV_THRESHOLD_PCT) or PRICE_CHANGE_ADV_THRESHOLD_PCT)
    bias_min = PRICE_CHANGE_BIAS_MIN_PCT   # 0.60 = 60%
    if universe <= 0:
        return ("NO TRADE", f"PRIMARY: PRICE_CHANGE_DIST (NO DATA | U 0)")
    adv_pct = advancers / universe
    dec_pct = decliners / universe
    if adv_pct >= bias_min:
        return ("SELL PUT", f"PRIMARY: PRICE_CHANGE_DIST (A {advancers} = {adv_pct*100:.1f}% >= {bias_min*100:.0f}% | U {universe} | >{threshold:.2f}%)")
    if dec_pct >= bias_min:
        return ("SELL CALL", f"PRIMARY: PRICE_CHANGE_DIST (D {decliners} = {dec_pct*100:.1f}% >= {bias_min*100:.0f}% | U {universe} | >{threshold:.2f}%)")
    return ("NO TRADE", f"PRIMARY: PRICE_CHANGE_DIST (A {adv_pct*100:.1f}% D {dec_pct*100:.1f}% — 60% nahi hua | U {universe})")

def _render_price_change_distribution_bar(dist):
    if not dist:
        return ""
    decliners = int(dist.get("decliners", 0) or 0)
    advancers = int(dist.get("advancers", 0) or 0)
    total = decliners + advancers
    if total <= 0:
        return ""
    red_blocks = int(round((decliners / total) * PRICE_CHANGE_BAR_WIDTH))
    red_blocks = max(0, min(PRICE_CHANGE_BAR_WIDTH, red_blocks))
    if decliners > 0 and red_blocks == 0:
        red_blocks = 1
    if advancers > 0 and red_blocks == PRICE_CHANGE_BAR_WIDTH:
        red_blocks -= 1
    green_blocks = PRICE_CHANGE_BAR_WIDTH - red_blocks
    red = "\033[91m"
    green = "\033[92m"
    reset = "\033[0m"
    return f"{red}{'█' * red_blocks}{reset}{green}{'█' * green_blocks}{reset}"

def build_price_change_ui_kwargs(
    symbol_primary=None,
    spot_primary=None,
    symbol_secondary=None,
    spot_secondary=None,
    update_state=False,
):
    dist = fetch_price_change_distribution(refresh=update_state)
    if not dist:
        return {
            "market_price_change_header": None,
            "market_price_change_bar": None,
            "market_price_change_counts": None,
        }
    universe = int(dist.get("universe", 0) or 0)
    advancers = int(dist.get("advancers", 0) or 0)
    decliners = int(dist.get("decliners", 0) or 0)
    threshold = float(dist.get("threshold_pct", PRICE_CHANGE_ADV_THRESHOLD_PCT) or PRICE_CHANGE_ADV_THRESHOLD_PCT)
    adv_pct = (advancers / universe * 100) if universe > 0 else 0
    dec_pct = (decliners / universe * 100) if universe > 0 else 0
    return {
        "market_price_change_header": f"📉 Price Change Dist: Perpetuals 24h mark (>{threshold:.2f}% = Advancers)",
        "market_price_change_bar": f"{_render_price_change_distribution_bar(dist)}  D {dec_pct:.1f}%  │  A {adv_pct:.1f}%",
        "market_price_change_counts": f"Decliners: {decliners}  │  Advancers: {advancers}  │  Universe: {universe}",
    }

def render_terminal_ui(
    now, spot, decision, source,
    bal, blocked, upnl,
    wallet_upnl_pct, blocked_upnl_pct,
    used_pct_ui, heat_level, heat_bar,
    delta, gamma,
    position_details, position_count,
    execution_status,
    used_pct_calc, margin_drift, drift_status, drift_icon,
    api_score, api_status, api_bar,
    expiry_oi_list=None,
    expiry_oi_list_secondary=None,
    spot_secondary=None,
    symbol_secondary=None,
    symbol_primary=None,
    all_expiry_max_pain_primary=None,
    all_expiry_max_pain_secondary=None,
    projected_profit=None,
    max_loss=None,
    position_delta_sum=None,
    position_gamma_sum=None,
    position_theta_sum=None,
    position_vega_sum=None,
    position_rho_sum=None,
    strike_order_ok=None,
    strike_order_msg=None,
    option_metrics=None,
    last_executed_signal=None,
    market_price_change_header=None,
    market_price_change_bar=None,
    market_price_change_counts=None,
):
    import os
    global CMD_FEEDBACK, CMD_FEEDBACK_CLEAR_AT, LAST_CMD_SHOWN
    global LAST_VISIBLE_UI_AT, LAST_LIVE_UI_RENDER_AT, LAST_HEARTBEAT_PRINT_AT, LIVE_RENDER_COUNT
    if CMD_FEEDBACK and CMD_FEEDBACK_CLEAR_AT > 0 and time.time() > CMD_FEEDBACK_CLEAR_AT:
        CMD_FEEDBACK = ""
        CMD_FEEDBACK_CLEAR_AT = 0.0
    with UI_PRINT_LOCK:
        now_ts = time.time()
        LAST_LIVE_UI_RENDER_AT = now_ts
        LAST_VISIBLE_UI_AT = now_ts
        LAST_HEARTBEAT_PRINT_AT = now_ts
        LIVE_RENDER_COUNT += 1
        live_clock = now.strftime("%H:%M:%S") if hasattr(now, "strftime") else str(now)
        # ANSI clear (VS Code terminal me os.system('clear') se display nahi aata)
        print("\033[2J\033[H", end="", flush=True)
        print("\n" * 4)  # upar space — UI top cut na ho
        print("┌──────────────────────────────────────────────────────────────────────────────")
        print(f"│ 👤 User: {MAIN_ACCOUNT['username']}")
        print(f"│ 📧 Email: {MAIN_ACCOUNT['email']}")
        print(f"│ 📞 Phone: {MAIN_ACCOUNT['phone']}")
        print("│──────────────────────────────────────────────────────────────────────────────")
        print(f"│ ⏰ Time: {now}")
        print(f"│ 🤖 Strat: {STRATEGY_MODE}")
        print(f"│ 📡 Source: {source}")
        exec_on = "ON" if USER_AUTO_EXECUTION else "OFF"
        system_ok = "OK" if CAN_EXECUTE else "BLOCKED"
        log_on = "ON" if LOG_ENABLED else "OFF"
        print(f"│ ⚙️ Exec Status: {exec_on} │ {system_ok} │ Log: {log_on} │ {execution_status}")
        # Bias = aligned signal (LAST_EXECUTED_SIGNAL) when set, else strategy decision — oscillating PCR se confusion avoid
        _bias_display = last_executed_signal if last_executed_signal is not None else decision
        print(f"│ 🎯 Bias: {_bias_display}")
        print("│")
        switch_next = "ETH" if SYMBOL == "BTC" else "BTC"
        _main_label = (symbol_primary or SYMBOL)
        print(f"│ 📌 Instrument: {_main_label} (Main)  │  💹 Spot {spot:.2f}")
        if INSTRUMENT_SWITCH_PENDING:
            print(f"│   ⏳ SWITCH PENDING → {switch_next} (on next signal)")
        if CMD_FEEDBACK:
            print(f"│   {CMD_FEEDBACK}")
        OI_CW = 11
        OI_PCR_W = 6
        el = expiry_oi_list or []
        for item in el:
            exp, call_oi, put_oi, pcr = item[0], item[1], item[2], item[3]
            mp = item[4] if len(item) > 4 else None
            exp_str = exp.strftime("%d-%b-%Y") if hasattr(exp, "strftime") else str(exp)
            mp_str = f"  │  MaxPain: {int(mp)}" if mp is not None else ""
            print(f"│   {exp_str:<12} : {_fmt_oi(call_oi):>{OI_CW}}  │  {_fmt_oi(put_oi):>{OI_CW}}  │  PCR {pcr:>{OI_PCR_W}.2f}{mp_str}")
        if el:
            total_call = sum(i[1] for i in el)
            total_put = sum(i[2] for i in el)
            pcr_all = round(total_put / total_call, 2) if total_call else 0
            mp_primary_str = f"  │  MaxPain: {int(all_expiry_max_pain_primary)}" if all_expiry_max_pain_primary is not None else ""
            print(f"│   {'All Expiry':<12} : {_fmt_oi(total_call):>{OI_CW}}  │  {_fmt_oi(total_put):>{OI_CW}}  │  PCR {pcr_all:>{OI_PCR_W}.2f}{mp_primary_str}")
        # Second instrument OI block (Secondary = less total OI)
        sec_oi_list = expiry_oi_list_secondary or []
        if symbol_secondary and spot_secondary is not None and sec_oi_list:
            print("│")
            print(f"│ 📌 Instrument: {symbol_secondary} (Secondary)  │  💹 Spot {spot_secondary:.2f}")
            for item in sec_oi_list:
                exp, call_oi, put_oi, pcr = item[0], item[1], item[2], item[3]
                mp = item[4] if len(item) > 4 else None
                exp_str = exp.strftime("%d-%b-%Y") if hasattr(exp, "strftime") else str(exp)
                mp_str = f"  │  MaxPain: {int(mp)}" if mp is not None else ""
                print(f"│   {exp_str:<12} : {_fmt_oi(call_oi):>{OI_CW}}  │  {_fmt_oi(put_oi):>{OI_CW}}  │  PCR {pcr:>{OI_PCR_W}.2f}{mp_str}")
            if sec_oi_list:
                total_call = sum(i[1] for i in sec_oi_list)
                total_put = sum(i[2] for i in sec_oi_list)
                pcr_all = round(total_put / total_call, 2) if total_call else 0
                mp_sec_str = f"  │  MaxPain: {int(all_expiry_max_pain_secondary)}" if all_expiry_max_pain_secondary is not None else ""
                print(f"│   {'All Expiry':<12} : {_fmt_oi(total_call):>{OI_CW}}  │  {_fmt_oi(total_put):>{OI_CW}}  │  PCR {pcr_all:>{OI_PCR_W}.2f}{mp_sec_str}")
        if market_price_change_header or market_price_change_bar or market_price_change_counts:
            print("│")
            if market_price_change_header:
                print(f"│ {market_price_change_header}")
            if market_price_change_bar:
                print(f"│   {market_price_change_bar}")
            if market_price_change_counts:
                print(f"│   {market_price_change_counts}")
        print("│")

        API_W = 16  # align : across API section (same indent + width)
        print(f"│   {'🩺 API STATUS':<{API_W}}: Health + Latency")
        for api in ["SPOT", "WALLET", "ORDERS", "POSITIONS"]:
            latency = API_LATENCY.get(api, -1)
            if not HEALTH.get(api):
                print(f"│   {api:<{API_W}}: 🔴 DOWN ❌")
            else:
                print(f"│   {api:<{API_W}}: {latency_status(latency):<12} {latency} ms")
        print(f"│   🌐 API HEALTH: {api_bar} {api_score}%")

        print("│")
        print(f"│ 📐 Risk: Δ = {delta:>6.2f}  │  Γ = {gamma:>8.6f}")
        if MIN_MARGIN_PCT >= 0.99:
            min_pct_label = "100%"
        elif 0.09 <= MIN_MARGIN_PCT <= 0.11:
            min_pct_label = "10%"
        elif MIN_MARGIN_PCT <= 0.02:
            min_pct_label = "1%"
        else:
            min_pct_label = f"{round(MIN_MARGIN_PCT * 100)}%"
        print(f"│ 🎚️ Min margin: {min_pct_label} [m]: set % (1-100)")
        print(f"│ 📏 Position sizing: {_position_sizing_mode_label()} [k]")
        print(f"│ 📊 Used Bar: {heat_bar} {used_pct_ui}%")
        print(f"│ 📐 MARGIN DRIFT: {margin_drift:+.1f}%")
        print("│")
        print(f"│ 💰 Wallet Balance: ${bal:.2f}")
        print(f"│ UPNL %: {wallet_upnl_pct:+.2f}% {'🟢' if wallet_upnl_pct >= 0 else '🔴'}")
        print(f"│ 🔒 Blocked Amount: ${blocked:.2f}")
        print(f"│ UPNL %: {blocked_upnl_pct:+.2f}% {'🟢' if blocked_upnl_pct >= 0 else '🔴'}")
        print("│")
        pos_msg = f"{position_count}" if position_count > 0 else "0 (monitoring in background)"
        print(f"│ 📋 Account: {MAIN_ACCOUNT['account_name']} │ 🔄 Mode: {MARGIN_MODE.capitalize()} │ 📦 Positions: {pos_msg}")
        upnl_line = f"│ 💵 UPNL: ${upnl:+.2f}"
        if projected_profit is not None:
            upnl_line += f"  │  Projected: ${projected_profit:.2f}"
        if max_loss is not None:
            upnl_line += f"  │  Max Loss: ${max_loss:.2f}"
        print(upnl_line)
        show_greeks_here = position_delta_sum is not None and position_gamma_sum is not None and position_theta_sum is not None and position_vega_sum is not None
        if show_greeks_here and not (option_metrics and option_metrics.get("is_combined")):
            rho_str = f"  │  ρ: {position_rho_sum:+.4f}" if position_rho_sum is not None else ""
            print(f"│   Δ: {position_delta_sum:+.4f}  │  Γ: {position_gamma_sum:+.6f}  │  Θ: {position_theta_sum:+.4f}  │  ν: {position_vega_sum:+.4f}{rho_str}")
        if option_metrics:
            om = option_metrics
            if om.get("is_combined") and position_details:
                theor = om.get("combined_theor") or 0.0
                intr = om.get("combined_intrinsic") or 0.0
                tv = om.get("combined_time_value") or 0.0
                iv = om.get("combined_iv_pct")
                print(f"│ 📈 Option Greeks : Theor {theor:.2f}  │  Intrinsic {intr:.2f}  │  Time value {tv:.2f}" + (f"  │  IV {iv:.2f}%" if iv is not None else "  │  IV —"))
                if position_delta_sum is not None and position_gamma_sum is not None and position_theta_sum is not None and position_vega_sum is not None:
                    rho_str = f"  │  ρ: {position_rho_sum:+.4f}" if position_rho_sum is not None else ""
                    print(f"│  Δ {position_delta_sum:+.4f}  │  Γ {position_gamma_sum:+.6f}  │  ν {position_vega_sum:+.4f}  │  Θ {position_theta_sum:+.4f}{rho_str}")
            else:
                ref_src = om.get("ref_label", "ref")
                ref_cid = om.get("ref_contract", "")
                ref_line = f"│ 📈 Option (ref): {ref_src}"
                if ref_cid:
                    ref_line += f"  →  {ref_cid}"
                print(ref_line)
                print(f"│   Ask {om['ask']:.2f}  │  Bid {om['bid']:.2f}  │  Theor {om['theor']:.2f}  │  Intrinsic {om['intrinsic']:.2f}  │  Time value {om['time_value']:.2f}")
                iv_s = f"{om['iv_pct']:.2f}%" if om.get('iv_pct') is not None else "-"
                vol_s = str(om['volume']) if om.get('volume') is not None else "-"
                dte_s = str(om['dte']) if om.get('dte') is not None else "-"
                print(f"│   IV% {iv_s}  │  Vol {vol_s}  │  DTE {dte_s}  │  Δ {om['delta']:.2f}  Γ {om['gamma']:.4f}  ν {om['vega']:.2f}  Θ {om['theta']:.2f}  ρ {om['rho']:.3f}")
        print("│")

        if position_details:
            icon = "✓" if strike_order_ok else "⚠"
            header = f"│ Position Details: {strike_order_msg} {icon}" if strike_order_msg else "│ Position Details:"
            print(header)
            for p in position_details:
                color = "🟢" if p["upnl"] >= 0 else "🔴"
                dl = p.get("dir_letter", "L" if "LONG" in p["type"] else "S")
                inst = p.get("instrument", "BTC")
                if p.get("is_futures"):
                    entry_p = float(p.get("entry_price") or 0)
                    mark_p  = float(p.get("current_price") or 0)
                    entry_s = f" Entry${entry_p:.0f}" if entry_p > 0 else ""
                    mark_s  = f" Mark${mark_p:.0f}" if mark_p > 0 else ""
                    left = f"{color} {dl} {p['contract_id']} x{int(p['size'])}{entry_s}{mark_s} PERP"
                else:
                    cid = p.get("contract_id", "")
                    sz_btc = p.get("size_btc", p["size"] * 0.001)
                    d = p.get("delta", None)
                    delta_s = f" Δ {d:.2f}" if d is not None else " Δ —"
                    left = f"{color} {dl} {cid} x{sz_btc:.3f} {inst}{delta_s}"
                print(f"│   {left}  UPNL: ${p['upnl']:+.2f}")

        print("│──────────────────────────────────────────────────────────────────────────────")

        if LAST_CMD_SHOWN.get("until", 0) > time.time() and LAST_CMD_SHOWN.get("msg"):
            print(f"│ 📌 Last command: {LAST_CMD_SHOWN['msg']}")
        _w = 74
        _gap = " " * 61  # align Strategy header over the right column starting at [1]
        print("│ ⌨️ SHORTCUTS:" + _gap + "📊 STRATEGY:")
        _left = [
            "│   [i]  → Instrument (e.g. BTC / ETH)",
            "│   [m]  → Min margin % (e.g. 10 = 10%)",
            "│   [a]  → Account (e.g. Main / Sub)",
            "│   [u]  → User (name, email, phone)",
            "│   [p]  → Margin (e.g. Iso / Cross / Port)",
            "│   [k]  → Position sizing (Margin / Max Loss)",
            "│   [t]  → Order type (Options / Futures)",
            "│   [x]  → Auto exec (e.g. ON / OFF)",
            "│   [l]  → Log file (ON / OFF)",
            "│   [r]  → Reset (e.g. margin 10%, sizing Max Loss 10%, ALL_PCR, auto ON)",
            "│   [q]  → Exit  │  [s]  → Restart  │  Ctrl+C → Exit",
        ]
        _right = [
            " [1]  → NEAR_PCR (<1 CALL, >1 PUT, =1 NO TRADE)",
            " [2]  → ALL_PCR (<1 CALL, >1 PUT, =1 NO TRADE)",
            " [3]  → NEXT_PCR (<1 CALL, >1 PUT, =1 NO TRADE)",
            " [4]  → MAX_OPEN_INTEREST (PCR of max-OI expiry: <1 CALL, >1 PUT, =1 NO TRADE)",
            " [5]  → FIRST_PCR_CHANGE (jis instrument me pehle ALL_PCR change usi me position)",
            " [6]  → PRICE_CHANGE_DIST (Decliners>Advancers CALL, Advancers>Decliners PUT)",
            "",
        ]
        for i in range(max(len(_left), len(_right))):
            _l = _left[i] if i < len(_left) else "│"
            _r = _right[i] if i < len(_right) else ""
            _l_pad = (_l[: _w] if len(_l) > _w else _l.ljust(_w))
            print(_l_pad + "│ " + _r.rstrip())
        print("│")
        print(f"│ 📋 Last 10 Orders:")
        for o in list(last_orders):
            print(f"│   {o}")
        print("└──────────────────────────────────────────────────────────────────────────────")
        sys.stdout.flush()
# =========================================================
# 🔁 MAIN LOOP
# =========================================================
CAN_EXECUTE = True
CAN_EXECUTE = (
    USER_AUTO_EXECUTION
    and not API_DOWN
    and not KILL_SWITCH
)
FIRST_UI_RENDER_DONE = False  # UI load hone ke baad hi order place (strategy ke hisaab se)

# Log file: sirf last 24h ka data rakho — startup + live har 1h trim. Tee = stdout bhi log me apne aap likhe.
OPEN_INTEREST_LOG_FILE = f"{_SCRIPT_BASE}.log"
LOG_RETENTION_HOURS = 24

def _stderr_write(message: str) -> None:
    err = sys.__stderr__ or sys.stderr
    if err is None:
        return
    try:
        err.write(message)
    except Exception:
        pass

class _TeeLog:
    """Har print stdout + open_interest.log dono me jaye — log apne aap update."""
    def __init__(self, log_path: str):
        self._stdout: TextIO = sys.stdout
        self._file: Optional[TextIO] = None
        self._log_path = log_path
        try:
            self._file = open(log_path, "a", encoding="utf-8")
        except Exception as e:
            _stderr_write(f"⚠️ Log file open fail: {log_path} — {e}\n")

    def reopen(self) -> None:
        """Trim ke baad file descriptor sync — truncate hone par stale fd avoid."""
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
        try:
            self._file = open(self._log_path, "a", encoding="utf-8")
        except Exception as e:
            _stderr_write(f"⚠️ Log reopen fail: {self._log_path} — {e}\n")

    def write(self, data: str) -> None:
        self._stdout.write(data)
        if self._file and LOG_ENABLED:
            try:
                self._file.write(data)
                self._file.flush()
            except Exception as e:
                _stderr_write(f"⚠️ Log write fail: {e}\n")

    def flush(self) -> None:
        self._stdout.flush()
        if self._file:
            try:
                self._file.flush()
            except Exception:
                pass

def _reopen_stdout_log() -> None:
    if isinstance(sys.stdout, _TeeLog):
        sys.stdout.reopen()

def _write_log_only(text: str) -> None:
    if not text:
        return
    if not LOG_ENABLED:
        return
    if not isinstance(sys.stdout, _TeeLog):
        return
    if not getattr(sys.stdout, "_file", None):
        return
    try:
        sys.stdout._file.write(text) # pyright: ignore[reportOptionalMemberAccess]
        if not text.endswith("\n"):
            sys.stdout._file.write("\n") # type: ignore
        sys.stdout._file.flush() # pyright: ignore[reportOptionalMemberAccess]
    except Exception as e:
        _stderr_write(f"⚠️ Log-only write fail: {e}\n")

def _load_last_ui_frame_from_log(log_path=None, max_bytes=768 * 1024):
    """Startup par last valid full UI frame dikhao taaki terminal blank na lage."""
    if log_path is None:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OPEN_INTEREST_LOG_FILE)
    if not os.path.isfile(log_path):
        return ""
    try:
        size = os.path.getsize(log_path)
        if size <= 0:
            return ""
        with open(log_path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read().decode("utf-8", errors="replace")
        fallback = ""
        search_end = len(data)
        while True:
            end = data.rfind("└", 0, search_end)
            if end == -1:
                break
            start = data.rfind("┌", 0, end)
            if start == -1:
                break
            end_line = data.find("\n", end)
            if end_line == -1:
                end_line = len(data)
            frame = data[start:end_line].rstrip() + "\n"
            if "│ 👤 User:" in frame:
                frame = frame.replace(
                    "│   [q]  → Exit (Ctrl+C to kill)",
                    "│   [q]  → Exit  │  [s]  → Restart  │  Ctrl+C → Exit",
                )
                if not fallback:
                    fallback = frame
                if "BOOTING..." not in frame:
                    return frame
            search_end = start
        return fallback
    except Exception:
        return ""

def _ui_stall_watchdog():
    """Agar live UI refresh slow ho jaye to visible heartbeat print karo."""
    global LAST_VISIBLE_UI_AT, LAST_HEARTBEAT_PRINT_AT, LAST_FULL_SNAPSHOT_LOG_AT
    while not KILL_SWITCH:
        try:
            now_ts = time.time()
            if (
                now_ts - LAST_VISIBLE_UI_AT >= STALL_HEARTBEAT_AFTER_SEC
                and now_ts - LAST_HEARTBEAT_PRINT_AT >= STALL_HEARTBEAT_EVERY_SEC
            ):
                live_age = (now_ts - LAST_LIVE_UI_RENDER_AT) if LAST_LIVE_UI_RENDER_AT > 0 else None
                if live_age is None or LIVE_RENDER_COUNT == 0:
                    live_msg = "first live UI pending"
                else:
                    live_msg = f"last live UI {live_age:.0f}s ago"
                status_msg = execution_status if execution_status else "working"
                if now_ts - LAST_FULL_SNAPSHOT_LOG_AT >= STALL_FULL_SNAPSHOT_EVERY_SEC:
                    snapshot = _load_last_ui_frame_from_log()
                    if snapshot:
                        _write_log_only(snapshot)
                        _write_log_only(
                            f"⏳ UI snapshot replay | {live_msg} | stage: {LAST_MAIN_LOOP_STAGE} | status: {status_msg} | {datetime.now().strftime('%H:%M:%S')}"
                        )
                        with UI_PRINT_LOCK:
                            print("\033[2J\033[H", end="", flush=True)
                            print(snapshot, end="" if snapshot.endswith("\n") else "\n", flush=True)
                            print(
                                f"⏳ UI snapshot replay | {live_msg} | stage: {LAST_MAIN_LOOP_STAGE} | status: {status_msg} | {datetime.now().strftime('%H:%M:%S')}",
                                flush=True,
                            )
                            LAST_VISIBLE_UI_AT = time.time()
                            LAST_HEARTBEAT_PRINT_AT = LAST_VISIBLE_UI_AT
                        LAST_FULL_SNAPSHOT_LOG_AT = now_ts
                        continue
                with UI_PRINT_LOCK:
                    print(
                        f"\n⏳ Bot running... {live_msg} | stage: {LAST_MAIN_LOOP_STAGE} | status: {status_msg} | {datetime.now().strftime('%H:%M:%S')}",
                        flush=True,
                    )
                    LAST_VISIBLE_UI_AT = time.time()
                    LAST_HEARTBEAT_PRINT_AT = LAST_VISIBLE_UI_AT
        except Exception:
            pass
        time.sleep(1.0)

_tmp_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OPEN_INTEREST_LOG_FILE)
_tl = _TeeLog(_tmp_log_path)
sys.stdout = _tl
if _tl._file:
    _stderr_write(f"📋 Log file: {_tmp_log_path}\n")
LOG_TRIM_INTERVAL_SEC = 1 * 3600  # live: har 1 ghante trim (apne aap 24h manage)
LAST_LOG_TRIM_AT = 0.0

def trim_log_to_last_24h(log_path=None, hours=24, fast_if_large=True):
    """Trim log file to keep only frames (│ ⏰ Time: ...) from the last `hours` hours. Hamesha 24h time-based; badi file par bhi."""
    if log_path is None:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OPEN_INTEREST_LOG_FILE)
    if not os.path.isfile(log_path):
        return
    frame_start_re = re.compile(r"^│ ⏰ Time: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)")
    cutoff = datetime.now() - timedelta(hours=hours)

    def _parse_ts(m):
        try:
            ts_str = m.group(1)
            if "." in ts_str:
                return datetime.strptime(ts_str[:26], "%Y-%m-%d %H:%M:%S.%f")
            return datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    try:
        _size_mb = os.path.getsize(log_path) / (1024 * 1024)
        if _size_mb > 15:
            # Badi file: stream (binary) se pehla frame offset jahan ts >= cutoff; phir us offset se copy. 24h hi rehta hai.
            first_kept_offset = None
            last_frame_offset = 0
            with open(log_path, "rb") as f:
                offset = 0
                for line in f:
                    start = offset
                    offset += len(line)
                    try:
                        s = line.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    m = frame_start_re.match(s.strip()) or frame_start_re.match(s)
                    if m:
                        ts = _parse_ts(m)
                        last_frame_offset = start
                        if ts is not None and ts >= cutoff and first_kept_offset is None:
                            first_kept_offset = start
            if first_kept_offset is None:
                first_kept_offset = last_frame_offset
            if first_kept_offset is None or first_kept_offset <= 0:
                return
            tmp_path = log_path + ".trim.tmp"
            with open(log_path, "rb") as fin:
                fin.seek(first_kept_offset)
                with open(tmp_path, "wb") as fout:
                    while True:
                        chunk = fin.read(2 * 1024 * 1024)
                        if not chunk:
                            break
                        fout.write(chunk)
            os.replace(tmp_path, log_path)
            _reopen_stdout_log()
            print(f"📋 Log trimmed to last {hours}h (stream): {log_path}", flush=True)
            return
        # Chhoti file: memory me read karke trim
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) < 10:
            return
        kept = []
        current = []
        prev_ts = None
        for line in lines:
            m = frame_start_re.match(line.strip()) or frame_start_re.match(line)
            if m:
                ts = _parse_ts(m)
                if current and prev_ts is not None and prev_ts >= cutoff:
                    kept.extend(current)
                current = [line]
                prev_ts = ts
            else:
                current.append(line)
        if current and prev_ts is not None and prev_ts >= cutoff:
            kept.extend(current)
        if len(kept) < len(lines):
            with open(log_path, "w", encoding="utf-8", newline="") as f:
                f.writelines(kept)
            _reopen_stdout_log()
            print(f"📋 Log trimmed to last {hours}h: {log_path}", flush=True)
    except Exception as e:
        print(f"⚠️ Log trim skipped: {e}", flush=True)

print("🚀 DELTA INDIA OPTIONS MASTER ENGINE STARTED", flush=True)
trim_log_to_last_24h(hours=LOG_RETENTION_HOURS)
LAST_LOG_TRIM_AT = time.time()  # next trim after LOG_TRIM_INTERVAL_SEC
LAST_MAIN_LOOP_STAGE = "startup-restore"
# 🔄 On restart: position wale instrument pe SYMBOL; position 0 ho to jis instrument ka OI zyada usko main banao
try:
    all_pos = get_all_positions()
    btc_active = eth_active = 0
    for p in all_pos:
        sym = p.get("product_symbol", "")
        if not (sym.startswith("C-") or sym.startswith("P-")):
            continue
        if float(p.get("size", 0)) == 0:
            continue
        parts = sym.split("-")
        if len(parts) >= 2:
            inst = parts[1]
            if inst == "BTC":
                btc_active += 1
            elif inst == "ETH":
                eth_active += 1
    if eth_active > 0 and btc_active == 0:
        SYMBOL = "ETH"
        print(f"🔄 Restart: ETH positions detected → Instrument set to ETH (OI/Spot will show ETH)", flush=True)
    elif btc_active > 0 and eth_active == 0:
        SYMBOL = "BTC"
        print(f"🔄 Restart: BTC positions detected → Instrument set to BTC (OI/Spot will show BTC)", flush=True)
    elif btc_active > 0 and eth_active > 0:
        SYMBOL = "BTC" if btc_active >= eth_active else "ETH"
        print(f"🔄 Restart: Both have positions → Instrument set to {SYMBOL}", flush=True)
    else:
        # Position 0 dono me — background thread me OI check karo taaki startup block na ho
        def _bg_oi_pick():
            global SYMBOL, MAIN_SYMBOL_OI_BY
            try:
                picked = _pick_main_symbol_by_quick_oi(default_symbol=SYMBOL, timeout=8)
                SYMBOL = picked
                print(f"🔄 Restart: Position 0 → Instrument set to {SYMBOL} (higher OI, by {MAIN_SYMBOL_OI_BY})", flush=True)
            except Exception as oi_err:
                print(f"⚠️ Restart OI comparison skipped: {oi_err}", flush=True)
        threading.Thread(target=_bg_oi_pick, daemon=True).start()
except Exception as e:
    print(f"⚠️ Restart instrument detection skipped: {e}")
send_telegram(
    f"🚀 SCRIPT STARTED\n"
    f"Engine : Delta Options Bot\n"
    f"Time   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
)
_cached_ui = _load_last_ui_frame_from_log()
if _cached_ui:
    with UI_PRINT_LOCK:
        print("\033[2J\033[H", end="", flush=True)
        print(_cached_ui, end="" if _cached_ui.endswith("\n") else "\n", flush=True)
        LAST_VISIBLE_UI_AT = time.time()
threading.Thread(target=_ui_stall_watchdog, daemon=True).start()
# 🔄 Auto-restart: crash/error ke baad 10s wait karke phir se start (KILL_SWITCH tak)
while not KILL_SWITCH:
    try:
        while not KILL_SWITCH:
            LAST_MAIN_LOOP_STAGE = "cycle-start"
            SKIP_REST = False
            if KILL_SWITCH:
                break
            # 📋 Live 24h log manage — har 1 ghante trim (purana data hatao)
            if time.time() - LAST_LOG_TRIM_AT >= LOG_TRIM_INTERVAL_SEC:
                trim_log_to_last_24h(hours=LOG_RETENTION_HOURS)
                LAST_LOG_TRIM_AT = time.time()
            # 🔄 [a] Edit account name (main/sub) — prompt in main thread
            if ACCOUNT_NAME_EDIT_PENDING:
                try:
                    new_val = input("\n📋 Account name (main/sub): ").strip()
                    if new_val:
                        MAIN_ACCOUNT["account_name"] = new_val
                        _save_settings()
                        send_telegram(
                            f"⌨️ ACCOUNT NAME UPDATED\n"
                            f"Account : {new_val}\n"
                            f"Time    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                except (EOFError, KeyboardInterrupt):
                    pass
                ACCOUNT_NAME_EDIT_PENDING = False
            # 🔄 [u] Edit User (name, email, phone) — 3 prompts in main thread
            if USER_EDIT_PENDING:
                try:
                    new_name = input("\n👤 User name: ").strip()
                    if new_name:
                        MAIN_ACCOUNT["username"] = new_name
                    new_email = input("📧 Email: ").strip()
                    if new_email:
                        MAIN_ACCOUNT["email"] = new_email
                    new_phone = input("📞 Phone: ").strip()
                    if new_phone:
                        MAIN_ACCOUNT["phone"] = new_phone
                    if new_name or new_email or new_phone:
                        _save_settings()
                        send_telegram(
                            f"⌨️ USER UPDATED\n"
                            f"Name  : {MAIN_ACCOUNT['username']}\n"
                            f"Email : {MAIN_ACCOUNT['email']}\n"
                            f"Phone : {MAIN_ACCOUNT['phone']}\n"
                            f"Time  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                except (EOFError, KeyboardInterrupt):
                    pass
                USER_EDIT_PENDING = False
            # 🔄 [m] Min margin % (1-100) — ek hi key se koi bhi value
            if MARGIN_EDIT_PENDING:
                try:
                    raw = input("\n🎚️ Min margin % (1-100): ").strip()
                    if raw:
                        pct = float(raw)
                        if 0 < pct <= 100:
                            MIN_MARGIN_PCT = max(0.01, min(1.0, pct / 100.0))
                            _save_settings()
                            label = "100%" if MIN_MARGIN_PCT >= 0.99 else f"{round(MIN_MARGIN_PCT * 100)}%"
                            send_telegram(
                                f"⌨️ MIN MARGIN UPDATED [m]\n"
                                f"Min margin : {label}\n"
                                f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                            )
                            print(f"🎚️ Min margin → {label}")
                        else:
                            print("❌ Enter 1 to 100 only")
                except (EOFError, KeyboardInterrupt, ValueError):
                    pass
                MARGIN_EDIT_PENDING = False
            # 🔄 INSTRUMENT SWITCH — [i] request = switch to other instrument. Position hai to dono me se pehle bias change aate hi square off + switch; position 0 to turant switch
            if INSTRUMENT_SWITCH_PENDING:
                new_sym = "ETH" if SYMBOL == "BTC" else "BTC"  # requested target (jo [i] pe choose kiya)
                try:
                    current_positions = get_positions()
                except Exception:
                    current_positions = []
                has_active_positions = any(
                    p.get("product_symbol", "").startswith(("C-", "P-")) and float(p.get("size", 0)) != 0
                    for p in current_positions
                )
                # Monitor both instruments for bias change
                try:
                    decision_btc, _ = get_decision_for_symbol("BTC")
                    decision_eth, _ = get_decision_for_symbol("ETH")
                except Exception:
                    decision_btc = decision_eth = "NO TRADE"
                if LAST_BIAS_BTC is None:
                    LAST_BIAS_BTC = decision_btc
                    LAST_BIAS_ETH = decision_eth
                else:
                    if decision_btc != LAST_BIAS_BTC or decision_eth != LAST_BIAS_ETH:
                        INSTRUMENT_SWITCH_BIAS_CHANGE_DETECTED = True
                        which = []
                        if decision_btc != LAST_BIAS_BTC:
                            which.append(f"BTC {LAST_BIAS_BTC}→{decision_btc}")
                        if decision_eth != LAST_BIAS_ETH:
                            which.append(f"ETH {LAST_BIAS_ETH}→{decision_eth}")
                        INSTRUMENT_SWITCH_BIAS_CHANGE_MSG = "; ".join(which)
                    LAST_BIAS_BTC = decision_btc
                    LAST_BIAS_ETH = decision_eth
                if INSTRUMENT_SWITCH_BIAS_CHANGE_DETECTED and has_active_positions:
                    if not INSTRUMENT_SWITCH_SQUARE_OFF_SENT:
                        n = square_off_current_instrument_for_switch()
                        INSTRUMENT_SWITCH_SQUARE_OFF_SENT = True
                        send_telegram(
                            f"🔄 [i] Bias change → closing {SYMBOL} positions for switch\n"
                            f"Change: {INSTRUMENT_SWITCH_BIAS_CHANGE_MSG or 'bias change'}\n"
                            f"Square-off sent: {n} leg(s). Switch to {new_sym} when positions = 0.\n"
                            f"Time : {datetime.now().strftime('%H:%M:%S')}"
                        )
                elif not has_active_positions:
                    # Position 0 → turant switch (bias change wait mat karo); nayi instrument me strategy position bana degi
                    INSTRUMENT_SWITCH_PENDING = False
                    INSTRUMENT_SWITCH_SQUARE_OFF_SENT = False
                    INSTRUMENT_SWITCH_BIAS_CHANGE_DETECTED = False
                    INSTRUMENT_SWITCH_BIAS_CHANGE_MSG = ""
                    LAST_BIAS_BTC = LAST_BIAS_ETH = None
                    SYMBOL = new_sym
                    CMD_FEEDBACK = f"✅ [i] Switch instrument → {SYMBOL} (done)"
                    CMD_FEEDBACK_CLEAR_AT = time.time() + 5
                    LAST_CMD_SHOWN = {"msg": "", "until": 0.0}
                    LAST_EXECUTED_SIGNAL = None
                    send_telegram(
                        f"🔄 INSTRUMENT SWITCHED → {SYMBOL}\n"
                        f"Position 0 tha — ab {SYMBOL} pe strategy ke hisaab se position banegi.\n"
                        f"Time : {datetime.now().strftime('%H:%M:%S')}"
                    )
            # 🎯 FIRST_PCR_CHANGE: no position → jyada OI wala instrument; uske baad jis me pehle PCR change usi me position
            if STRATEGY_MODE == "FIRST_PCR_CHANGE":
                try:
                    decision_btc, _ = get_decision_for_symbol("BTC")
                    decision_eth, _ = get_decision_for_symbol("ETH")
                except Exception:
                    decision_btc = decision_eth = "NO TRADE"
                if FIRST_PCR_LAST_BTC is None:
                    # Pehli run / script start: agar koi position nahi to jis me jyada OI use me position banega
                    try:
                        all_pos = get_all_positions()
                        has_any_pos = any(
                            (p.get("product_symbol") or "").startswith(("C-", "P-")) and float(p.get("size", 0)) != 0
                            for p in all_pos
                        )
                        if not has_any_pos:
                            SYMBOL = _pick_main_symbol_by_quick_oi(default_symbol=SYMBOL, timeout=8)
                            LAST_EXECUTED_SIGNAL = None
                    except Exception:
                        pass
                    FIRST_PCR_LAST_BTC = decision_btc
                    FIRST_PCR_LAST_ETH = decision_eth
                else:
                    # Jis instrument ka PCR pehle change (request) use consider karo — current SYMBOL pehle check; usi me change to usi me position, nahi to dusre me
                    btc_changed = decision_btc != FIRST_PCR_LAST_BTC and decision_btc in ("SELL CALL", "SELL PUT")
                    eth_changed = decision_eth != FIRST_PCR_LAST_ETH and decision_eth in ("SELL CALL", "SELL PUT")
                    trigger = None
                    # Current SYMBOL pehle check: agar usi ka change hua to usi me position (ETH me ho + ETH change → ETH; BTC me ho + BTC change → BTC)
                    if SYMBOL == "BTC":
                        if btc_changed:
                            trigger = "BTC"
                        elif eth_changed:
                            trigger = "ETH"
                    else:
                        if eth_changed:
                            trigger = "ETH"
                        elif btc_changed:
                            trigger = "BTC"
                    # Dono change hue to bhi above se: current pehle check kiye, so current ka change pehle consider = trigger = current (stay)
                    FIRST_PCR_LAST_BTC = decision_btc
                    FIRST_PCR_LAST_ETH = decision_eth
                    if trigger is not None and trigger != SYMBOL:
                        try:
                            cur_pos = get_positions()
                        except Exception:
                            cur_pos = []
                        has_pos = any(
                            p.get("product_symbol", "").startswith(("C-", "P-")) and float(p.get("size", 0)) != 0
                            for p in cur_pos
                        )
                        if has_pos:
                            n = square_off_current_instrument_for_switch(reason="FIRST_PCR_CHANGE")
                            send_telegram(
                                f"🔄 FIRST_PCR_CHANGE: {SYMBOL} → {trigger}\n"
                                f"Bias change in {trigger} (ALL_PCR). Closed {SYMBOL} positions ({n} leg(s)). Building in {trigger}.\n"
                                f"Time : {datetime.now().strftime('%H:%M:%S')}"
                            )
                        else:
                            send_telegram(
                                f"🔄 FIRST_PCR_CHANGE: switching to {trigger} (bias change, no position to close).\n"
                                f"Time : {datetime.now().strftime('%H:%M:%S')}"
                            )
                        SYMBOL = trigger
                        LAST_EXECUTED_SIGNAL = None
            # 1️⃣ Fresh data fetch — sabhi 5 APIs ek saath parallel (8s timeout — hang hone par skip & retry)
            LAST_MAIN_LOOP_STAGE = "api-fetch"
            _api_timeout = 8
            _api_ex = None
            try:
                _api_ex = ThreadPoolExecutor(max_workers=5)
                f_spot = _api_ex.submit(get_spot)
                f_wallet = _api_ex.submit(wallet_usd)
                f_pos = _api_ex.submit(get_positions)
                f_all = _api_ex.submit(get_all_positions)
                f_orders = _api_ex.submit(orders_api_health_check)
                spot = f_spot.result(timeout=_api_timeout)
                bal, blocked, wallet_upnl = f_wallet.result(timeout=_api_timeout)
                positions = f_pos.result(timeout=_api_timeout)
                positions_all = f_all.result(timeout=_api_timeout)
                f_orders.result(timeout=_api_timeout)
            except Exception as _api_err:
                if _api_ex is not None:
                    _api_ex.shutdown(wait=False, cancel_futures=True)
                print(f"⚠️ API fetch timeout/error (retry next cycle): {repr(_api_err) or type(_api_err).__name__}", flush=True)
                _bal, _blocked, _upnl = 0, 0, 0
                _pos_all, _pos_details, _pos_count = [], [], 0
                _spot_retry = 0.0
                try:
                    _spot_retry = get_spot()
                    _bal, _blocked, _wallet_upnl = wallet_usd()
                    _pos_all = get_all_positions() or []
                    _s_btc = _spot_retry if SYMBOL == "BTC" else 0.0
                    _s_eth = _spot_retry if SYMBOL == "ETH" else 0.0
                    _calc_upnl, _, _pos_count, _pos_details = calculate_upnl(_pos_all, spot_btc=_s_btc, spot_eth=_s_eth)
                    _upnl = _wallet_upnl if abs(_wallet_upnl) > 0 else _calc_upnl
                except Exception:
                    pass
                _u, _hl, _hb = render_margin_heatmap(_bal, _blocked)
                _uc, _dr, _ds, _di, _ = calculate_margin_drift(_bal, _blocked, MIN_MARGIN_PCT)
                _as, _ast, _ab = render_api_health_bar()
                _err_short = (repr(_api_err) or type(_api_err).__name__)[:60] if _api_err else "Unknown"
                _price_change_ui_kwargs = build_price_change_ui_kwargs(
                    symbol_primary=SYMBOL,
                    spot_primary=_spot_retry,
                    update_state=True,
                )
                render_terminal_ui(
                    now=datetime.now(), spot=_spot_retry, decision="NO TRADE", source=f"API Error ({_err_short})",
                    bal=_bal, blocked=_blocked, upnl=_upnl,
                    wallet_upnl_pct=(_upnl / _bal * 100) if _bal > 0 else 0,
                    blocked_upnl_pct=(_upnl / _blocked * 100) if _blocked > 0 else 0,
                    used_pct_ui=_u, heat_level=_hl, heat_bar=_hb,
                    delta=0, gamma=0,
                    position_details=_pos_details, position_count=_pos_count,
                    execution_status=f"API Error — retry next cycle ({_err_short})",
                    used_pct_calc=_uc, margin_drift=_dr, drift_status=_ds, drift_icon=_di,
                    api_score=_as, api_status=_ast, api_bar=_ab,
                    expiry_oi_list=[], expiry_oi_list_secondary=[],
                    spot_secondary=None, symbol_secondary=None, symbol_primary=SYMBOL,
                    all_expiry_max_pain_primary=None, all_expiry_max_pain_secondary=None,
                    projected_profit=None, max_loss=None,
                    position_delta_sum=None, position_gamma_sum=None, position_theta_sum=None,
                    position_vega_sum=None, position_rho_sum=None,
                    strike_order_ok=None, strike_order_msg=None, option_metrics=None,
                    last_executed_signal=LAST_EXECUTED_SIGNAL,
                    **_price_change_ui_kwargs
                )
                time.sleep(REFRESH_SECONDS)
                continue
            finally:
                if _api_ex is not None:
                    _api_ex.shutdown(wait=False, cancel_futures=True)
            _s_btc = spot if SYMBOL == "BTC" else 0.0
            _s_eth = spot if SYMBOL == "ETH" else 0.0
            _calc_upnl, _, position_count, position_details = calculate_upnl(positions_all, spot_btc=_s_btc, spot_eth=_s_eth)
            upnl = wallet_upnl if abs(wallet_upnl) > 0 else _calc_upnl

            # 🟢 API RECOVERY CHECK — pehle check karo taaki recover hote hi cooldown set ho
            if API_DOWN and all(HEALTH.values()):
                API_RECOVERY_COOLDOWN_UNTIL = time.time() + API_RECOVERY_COOLDOWN_SEC
                no_pos = not any(
                    (p.get("product_symbol") or "").startswith(("C-", "P-")) and float(p.get("size", 0)) != 0
                    for p in positions_all
                )
                msg = (
                    "🟢 APIs RECOVERED — AUTO EXECUTION RESUMED\n"
                    f"Stabilizing {API_RECOVERY_COOLDOWN_SEC}s, phir resume.\n"
                )
                if no_pos:
                    msg += "Position nahi → strategy decision ke hisaab se naya position banega.\n"
                msg += f"Time: {datetime.now().strftime('%H:%M:%S')}"
                send_telegram(msg)
                API_DOWN = False
            # Step 1: HEALTH — API DOWN / cooldown / api health < 25% = block. 25%+ = position manage allow (slow API bhi try)
            api_score_early, _ast_early, _ab_early = render_api_health_bar()
            # Agar API health already 50%+ hai to cooldown clear — "API RECOVERING" mat dikhao, position manage resume
            if api_score_early >= 50:
                API_RECOVERY_COOLDOWN_UNTIL = 0.0
            if API_DOWN or time.time() < API_RECOVERY_COOLDOWN_UNTIL or api_score_early < API_MIN_HEALTH_PCT:
                has_any_option_pos = any(
                    (p.get("product_symbol") or "").startswith(("C-", "P-")) and float(p.get("size", 0)) != 0
                    for p in positions
                )
                if not has_any_option_pos:
                    LAST_EXECUTED_SIGNAL = None
                _u, _hl, _hb = render_margin_heatmap(bal, blocked)
                _uc, _dr, _ds, _di, _ceiling = calculate_margin_drift(bal, blocked, MIN_MARGIN_PCT)
                _as, _ast, _ab = render_api_health_bar()
                # OI data bhi dikhao recovery me (20s timeout — hang hone par bhi UI render)
                _rec_oi_primary, _rec_oi_secondary = [], []
                _rec_spot_sec, _rec_sym_sec = None, None
                _rec_mp_primary, _rec_mp_secondary = None, None
                _rec_main = SYMBOL
                _rec_spot_primary = None
                _rec_ex = None
                try:
                    LAST_MAIN_LOOP_STAGE = "recovery-oi"
                    _rec_ex = ThreadPoolExecutor(max_workers=2)
                    _f_btc = _rec_ex.submit(get_spot_and_oi_for_symbol, "BTC")
                    _f_eth = _rec_ex.submit(get_spot_and_oi_for_symbol, "ETH")
                    _spot_btc, _oi_btc, _mp_btc, _tc_btc = _f_btc.result(timeout=12)
                    _spot_eth, _oi_eth, _mp_eth, _tc_eth = _f_eth.result(timeout=12)
                    if MAIN_SYMBOL_OI_BY == "contracts":
                        _tot_btc, _tot_eth = _tc_btc, _tc_eth
                    else:
                        _tot_btc = sum(i[1] + i[2] for i in (_oi_btc or []))
                        _tot_eth = sum(i[1] + i[2] for i in (_oi_eth or []))
                    _rec_main = "BTC" if _tot_btc >= _tot_eth else "ETH"
                    _rec_oi_primary = _oi_btc if _rec_main == "BTC" else _oi_eth
                    _rec_oi_secondary = _oi_eth if _rec_main == "BTC" else _oi_btc
                    _rec_spot_primary = _spot_btc if _rec_main == "BTC" else _spot_eth
                    _rec_spot_sec = _spot_eth if _rec_main == "BTC" else _spot_btc
                    _rec_sym_sec = "ETH" if _rec_main == "BTC" else "BTC"
                    _rec_mp_primary = _mp_btc if _rec_main == "BTC" else _mp_eth
                    _rec_mp_secondary = _mp_eth if _rec_main == "BTC" else _mp_btc
                except Exception:
                    pass
                finally:
                    if _rec_ex is not None:
                        _rec_ex.shutdown(wait=False, cancel_futures=True)
                _price_change_ui_kwargs = build_price_change_ui_kwargs(
                    symbol_primary=_rec_main,
                    spot_primary=_rec_spot_primary,
                    symbol_secondary=_rec_sym_sec,
                    spot_secondary=_rec_spot_sec,
                    update_state=True,
                )
                block_reason = "API RECOVERING" if (API_DOWN or time.time() < API_RECOVERY_COOLDOWN_UNTIL) else f"API health {api_score_early}% < {API_MIN_HEALTH_PCT}%"
                wallet_upnl_pct = (upnl / bal * 100) if bal > 0 else 0
                blocked_upnl_pct = (upnl / blocked * 100) if blocked > 0 else 0
                render_terminal_ui(
                    now=datetime.now(), spot=spot, decision="NO TRADE", source=block_reason + "...",
                    bal=bal, blocked=blocked, upnl=upnl,
                    wallet_upnl_pct=wallet_upnl_pct, blocked_upnl_pct=blocked_upnl_pct,
                    used_pct_ui=_u, heat_level=_hl, heat_bar=_hb,
                    delta=0, gamma=0,
                    position_details=position_details, position_count=position_count,
                    execution_status=f"{block_reason} (no new orders)",
                    used_pct_calc=_uc, margin_drift=_dr, drift_status=_ds, drift_icon=_di,
                    api_score=_as, api_status=_ast, api_bar=_ab,
                    expiry_oi_list=_rec_oi_primary or [], expiry_oi_list_secondary=_rec_oi_secondary or [],
                    spot_secondary=_rec_spot_sec, symbol_secondary=_rec_sym_sec, symbol_primary=_rec_main,
                    all_expiry_max_pain_primary=_rec_mp_primary, all_expiry_max_pain_secondary=_rec_mp_secondary,
                    projected_profit=None, max_loss=None,
                    position_delta_sum=None, position_gamma_sum=None, position_theta_sum=None,
                    position_vega_sum=None, position_rho_sum=None,
                    strike_order_ok=None, strike_order_msg=None, option_metrics=None,
                    last_executed_signal=LAST_EXECUTED_SIGNAL,
                    **_price_change_ui_kwargs
                )
                time.sleep(REFRESH_SECONDS)
                continue

            def _inst(pos):
                s = (pos.get("product_symbol") or "")
                return s.split("-")[1] if len(s.split("-")) >= 2 else ""
            btc_active = sum(1 for p in positions_all if (p.get("product_symbol") or "").startswith(("C-", "P-")) and float(p.get("size", 0)) != 0 and _inst(p) == "BTC")
            eth_active = sum(1 for p in positions_all if (p.get("product_symbol") or "").startswith(("C-", "P-")) and float(p.get("size", 0)) != 0 and _inst(p) == "ETH")
            manage_both = btc_active > 0 and eth_active > 0

            # 2️⃣ Calculations
            now = datetime.now()
            wallet_upnl_pct = (upnl / bal * 100) if bal > 0 else 0
            blocked_upnl_pct = (upnl / blocked * 100) if blocked > 0 else 0

            used_pct_ui, heat_level, heat_bar = render_margin_heatmap(bal, blocked)
            api_score, api_status, api_bar = render_api_health_bar()

            # 📊 Main = jis me jyada OI — Delta Analytics ke mutabiq: contracts ya usd (MAIN_SYMBOL_OI_BY)
            _oi_ex = None
            try:
                LAST_MAIN_LOOP_STAGE = "oi-scan"
                _oi_ex = ThreadPoolExecutor(max_workers=2)
                _fb = _oi_ex.submit(get_spot_and_oi_for_symbol, "BTC")
                _fe = _oi_ex.submit(get_spot_and_oi_for_symbol, "ETH")
                spot_btc, oi_btc, mp_all_btc, tc_btc = _fb.result(timeout=12)
                spot_eth, oi_eth, mp_all_eth, tc_eth = _fe.result(timeout=12)
                if MAIN_SYMBOL_OI_BY == "contracts":
                    total_oi_btc, total_oi_eth = tc_btc, tc_eth
                else:
                    total_oi_btc = sum(i[1] + i[2] for i in (oi_btc or []))
                    total_oi_eth = sum(i[1] + i[2] for i in (oi_eth or []))
                main_sym = "BTC" if total_oi_btc >= total_oi_eth else "ETH"
                # Dono me position → main_sym pehle (dono manage honge — second pass me other)
                # Sirf ek me position → wahi manage. Koi nahi → main_sym (jyada OI)
                if btc_active > 0 and eth_active == 0:
                    SYMBOL = "BTC"
                elif eth_active > 0 and btc_active == 0:
                    SYMBOL = "ETH"
                else:
                    SYMBOL = main_sym
                spot = spot_btc if SYMBOL == "BTC" else spot_eth
                expiry_oi_list_primary = oi_btc if main_sym == "BTC" else oi_eth
                spot_primary_display = spot_btc if main_sym == "BTC" else spot_eth
                spot_secondary_oi = spot_eth if main_sym == "BTC" else spot_btc
                symbol_secondary_oi = "ETH" if main_sym == "BTC" else "BTC"
                expiry_oi_list_secondary_oi = oi_eth if main_sym == "BTC" else oi_btc
                all_expiry_mp_primary = mp_all_btc if main_sym == "BTC" else mp_all_eth
                all_expiry_mp_secondary = mp_all_eth if main_sym == "BTC" else mp_all_btc
            except Exception:
                main_sym = SYMBOL
                expiry_oi_list_primary = None
                spot = spot_primary_display = 0.0
                spot_secondary_oi = symbol_secondary_oi = expiry_oi_list_secondary_oi = None
                all_expiry_mp_primary = all_expiry_mp_secondary = None
                try:
                    spot_btc = get_spot("BTC")
                    spot_eth = get_spot("ETH")
                except Exception:
                    spot_btc = spot_eth = get_spot()
            finally:
                if _oi_ex is not None:
                    _oi_ex.shutdown(wait=False, cancel_futures=True)
            price_change_ui_kwargs = build_price_change_ui_kwargs(
                symbol_primary=main_sym,
                spot_primary=spot_primary_display,
                symbol_secondary=symbol_secondary_oi,
                spot_secondary=spot_secondary_oi,
                update_state=True,
            )
            _opt_ex = None
            try:
                LAST_MAIN_LOOP_STAGE = "chain-fetch"
                _opt_ex = ThreadPoolExecutor(max_workers=1)
                opts = _opt_ex.submit(fetch_options).result(timeout=12)
            except Exception:
                opts = []
            finally:
                if _opt_ex is not None:
                    _opt_ex.shutdown(wait=False, cancel_futures=True)
            LAST_MAIN_LOOP_STAGE = "decision"
            near, nxt, all_opts = split_expiries(opts)

            cn, pn, pcr_n = oi_pcr(near)
            cx, px, pcr_x = oi_pcr(nxt)
            ca, pa, pcr_a = oi_pcr(all_opts)

            delta, gamma, theta, vega = greek_avg(near)
            mp = max_pain(near)

            # Fresh wallet/positions snapshot upar api-fetch me aa chuka hai; yahan dobara network call mat karo.
            # ===== STEP 1: POSITION DETECTION (SCRIPT START ENTRY GATE) =====
            # POSITIONS fail ho to isi iteration me order mat dalo — par UI dikhao
            if API_DOWN:
                # Ensure these are always defined in this branch (avoid NameError during auto-restart)
                try:
                    _upnl_now = upnl
                except NameError:
                    _upnl_now = 0
                wallet_upnl_pct = (_upnl_now / bal * 100) if bal > 0 else 0
                blocked_upnl_pct = (_upnl_now / blocked * 100) if blocked > 0 else 0
                _u, _hl, _hb = render_margin_heatmap(bal, blocked)
                _uc, _dr, _ds, _di, _ceiling = calculate_margin_drift(bal, blocked, MIN_MARGIN_PCT)
                _as, _ast, _ab = render_api_health_bar()
                render_terminal_ui(
                    now=datetime.now(), spot=spot, decision="NO TRADE", source="API DOWN",
                    bal=bal, blocked=blocked, upnl=_upnl_now,
                    wallet_upnl_pct=wallet_upnl_pct, blocked_upnl_pct=blocked_upnl_pct,
                    used_pct_ui=_u, heat_level=_hl, heat_bar=_hb,
                    delta=delta, gamma=gamma,
                    position_details=position_details, position_count=position_count,
                    execution_status="API DOWN (no orders)",
                    used_pct_calc=_uc, margin_drift=_dr, drift_status=_ds, drift_icon=_di,
                    api_score=_as, api_status=_ast, api_bar=_ab,
                    expiry_oi_list=expiry_oi_list_primary or [], expiry_oi_list_secondary=expiry_oi_list_secondary_oi or [],
                    spot_secondary=spot_secondary_oi, symbol_secondary=symbol_secondary_oi, symbol_primary=main_sym,
                    all_expiry_max_pain_primary=all_expiry_mp_primary, all_expiry_max_pain_secondary=all_expiry_mp_secondary,
                    projected_profit=None, max_loss=None,
                    position_delta_sum=None, position_gamma_sum=None, position_theta_sum=None,
                    position_vega_sum=None, position_rho_sum=None,
                    strike_order_ok=None, strike_order_msg=None, option_metrics=None,
                    last_executed_signal=LAST_EXECUTED_SIGNAL,
                    **price_change_ui_kwargs
                )
                time.sleep(REFRESH_SECONDS)
                continue

            has_active_position = any(
                p.get("product_symbol", "").startswith(("C-", "P-"))
                and float(p.get("size", 0)) != 0
                for p in positions
            )

            if not has_active_position:
                execution_status = "NO POSITION → STRATEGY ENTRY ALLOWED"
            else:
                execution_status = "POSITION EXISTS → MANAGE ONLY"        
            # ================= LEG IDENTIFICATION =================
            short_call_pos = short_put_pos = None
            long_call_pos = None
            long_put_pos  = None

            for pos in positions:
                sym = pos.get("product_symbol", "")
                size = float(pos.get("size", 0))

                if sym.startswith("C-") and size < 0:
                    short_call_pos = pos
                elif sym.startswith("C-") and size > 0:
                    long_call_pos = pos
                elif sym.startswith("P-") and size < 0:
                    short_put_pos = pos
                elif sym.startswith("P-") and size > 0:
                    long_put_pos = pos

            # ✅ EVERYTHING BELOW MUST BE OUTSIDE THE LOOP
            pos_upnl_usd, pos_upnl_btc, position_count, position_details = calculate_upnl(positions_all, spot_btc=_s_btc, spot_eth=_s_eth)
            active_positions = [
                p for p in positions
                if p.get("product_symbol", "").startswith(("C-", "P-")) and float(p.get("size", 0)) != 0
            ]

            # ================= SAME EXPIRY ENFORCE =================
            # Jis expiry me zyada legs (dominance) wahi main; galat expiry wale square off; naye orders bhi usi expiry me
            dominant_expiry_this_cycle = None
            if len(active_positions) >= 1:
                exp_counts = Counter()
                for p in active_positions:
                    exp = extract_expiry(p.get("product_symbol", ""))
                    if exp is not None:
                        exp_counts[exp] += 1
                if len(exp_counts) > 1:
                    dominant_expiry = exp_counts.most_common(1)[0][0]
                    wrong_expiry_positions = [p for p in active_positions if extract_expiry(p.get("product_symbol", "")) != dominant_expiry]
                    for p in wrong_expiry_positions:
                        sym = p.get("product_symbol", "")
                        size = float(p.get("size", 0))
                        if sym and size != 0:
                            square_off_position(sym, size, reason="WRONG_EXPIRY")
                            send_telegram(
                                f"🔄 EXPIRY FIX: Squared off wrong-expiry leg\n"
                                f"Symbol: {sym}\n"
                                f"Size: {size}\n"
                                f"Dominant expiry kept: {dominant_expiry.strftime('%d-%b-%Y')}\n"
                                f"Time: {datetime.now().strftime('%H:%M:%S')}"
                            )
                    SKIP_REST = True
                    _uc, _dr, _ds, _di, _ = calculate_margin_drift(bal, blocked, MIN_MARGIN_PCT)
                    render_terminal_ui(
                        now=datetime.now(), spot=spot_primary_display, decision="NO TRADE", source="Expiry fix (squaring wrong-expiry)",
                        bal=bal, blocked=blocked, upnl=upnl,
                        wallet_upnl_pct=wallet_upnl_pct, blocked_upnl_pct=blocked_upnl_pct,
                        used_pct_ui=used_pct_ui, heat_level=heat_level, heat_bar=heat_bar,
                        delta=delta, gamma=gamma,
                        position_details=position_details, position_count=position_count,
                        execution_status="Expiry fix (squaring wrong-expiry legs)",
                        used_pct_calc=_uc, margin_drift=_dr, drift_status=_ds, drift_icon=_di,
                        api_score=api_score, api_status=api_status, api_bar=api_bar,
                        expiry_oi_list=expiry_oi_list_primary or [], expiry_oi_list_secondary=expiry_oi_list_secondary_oi or [],
                        spot_secondary=spot_secondary_oi, symbol_secondary=symbol_secondary_oi, symbol_primary=main_sym,
                        all_expiry_max_pain_primary=all_expiry_mp_primary, all_expiry_max_pain_secondary=all_expiry_mp_secondary,
                        projected_profit=None, max_loss=None,
                        position_delta_sum=None, position_gamma_sum=None, position_theta_sum=None,
                        position_vega_sum=None, position_rho_sum=None,
                        strike_order_ok=None, strike_order_msg=None, option_metrics=None,
                        last_executed_signal=LAST_EXECUTED_SIGNAL,
                        **price_change_ui_kwargs
                    )
                    time.sleep(REFRESH_SECONDS)
                    continue
                if exp_counts:
                    dominant_expiry_this_cycle = exp_counts.most_common(1)[0][0]
                    # Option chain me ye expiry ab future list me nahi (expiry day pe filter ho jata hai) — roll_opts next use karega
                    exp_still_valid = any(o["expiry"] == dominant_expiry_this_cycle for o in all_opts)
                    if not exp_still_valid:
                        dominant_expiry_this_cycle = None
                        # Message mat bhejo — actual expiry tab hai jab positions 0 ho jayein (neeche len(active_positions)==0 pe bhejenge)

            execution_blocked = False
            # Per instrument max 3 legs (BTC me 3, ETH me 3 allow)
            if len(active_positions) > 3 and not (EQUALITY_EVENT or EQUALITY_MANUAL_EXEC):
                execution_blocked = True
                execution_status = "⚠️ Execution blocked — max 3 legs per instrument"

            # ================= POSITION STATE CHECK =================
            has_short_call = has_long_put = has_short_put = has_long_call = False
            has_long_10d_call = has_long_10d_put = False
            has_any_long_call = False

            for pos in positions:
                symbol = pos.get("product_symbol", "")
                size = float(pos.get("size", 0))
                delta_val = abs(float(pos.get("delta", 0))) if pos.get("delta") is not None else 0

                if symbol.startswith("C-"):
                    if size < 0:
                        has_short_call = True
                    else:
                        has_long_call = True
                        has_any_long_call = True
                        if abs(delta_val - HEDGE_DELTA_TARGET) <= DELTA_THRESHOLD:
                            has_long_10d_call = True

                elif symbol.startswith("P-"):
                    if size < 0:
                        has_short_put = True
                    else:
                        has_long_put = True
                        if abs(delta_val - HEDGE_DELTA_TARGET) <= DELTA_THRESHOLD:
                            has_long_10d_put = True
            if API_DOWN:
                execution_status = "PAUSED — API DOWN"
                time.sleep(REFRESH_SECONDS)
                SKIP_REST = True
            elif time.time() < API_RECOVERY_COOLDOWN_UNTIL:
                execution_status = f"STABILIZING — API just recovered (no orders for {API_RECOVERY_COOLDOWN_SEC}s)"
                time.sleep(REFRESH_SECONDS)
                SKIP_REST = True
            # ================= MARGIN 10% =================
            # Single mode: 10% sirf bias change pe (neeche bias_changed block mein); LIVE hata diya
            # ===== END AUTO MARGIN 10% =====                  
            # ========================================================
            # Detect position changes (NEW POSITION ALERT bhejenge decision/expiry ke baad)
            position_changes = detect_position_changes(positions)
            has_new_position = any("NEW POSITION" in c for c in position_changes)
            pending_new_position_changes_txt = "\n".join(position_changes) if has_new_position else None

            # 🔁 EXPIRY / POSITION RESET LOGIC — positions 0 = actual expiry (exchange settle ho chuka)
            # Confirm: POSITIONS API healthy ho + 2 cycle lagatar 0 dikhne par hi "position 0" maan ke nayi entry allow
            # API fail pe [] aata hai — usko real 0 mat mano, isliye sirf HEALTH["POSITIONS"] True ho to count karo
            positions_api_healthy = HEALTH.get("POSITIONS", True)
            _has_futures_pos = ORDER_TYPE == "futures" and any(
                p.get("product_symbol") == f"{SYMBOL}USD" and float(p.get("size", 0)) != 0
                for p in positions
            )
            if len(active_positions) == 0 and not _has_futures_pos and positions_api_healthy:
                CONSECUTIVE_ZERO_POSITION_CYCLES = CONSECUTIVE_ZERO_POSITION_CYCLES + 1
            else:
                CONSECUTIVE_ZERO_POSITION_CYCLES = 0
            if CONSECUTIVE_ZERO_POSITION_CYCLES >= 2:
                LAST_EXECUTED_SIGNAL = None
                MARGIN_ADJUSTED_THIS_SIGNAL = False
                # Sirf confirm ke baad: "position 0" — nayi position naye near expiry me banegi
                if time.time() - LAST_EXPIRY_ROLL_TELEGRAM_AT > 60:
                    if USER_AUTO_EXECUTION and ORDER_TYPE != "futures":
                        send_telegram(
                            f"📅 EXPIRY ROLL: Positions 0 confirmed (2 cycles). Contract settled — new payoff in new near expiry.\n"
                            f"Time: {datetime.now().strftime('%H:%M:%S')}"
                        )
                    LAST_EXPIRY_ROLL_TELEGRAM_AT = time.time()

            # UPNL: wallet API sabse accurate (futures+options dono cover); fallback = position-level sum
            upnl = wallet_upnl if abs(wallet_upnl) > 0 else pos_upnl_usd
            wallet_upnl_pct = round((upnl / bal) * 100, 2) if bal > 0 else 0
            blocked_upnl_pct = round((upnl / blocked) * 100, 2) if blocked > 0 else 0
            # Strategy: jo OI screen pe dikhe wahi use karo — confusion avoid
            max_oi_expiry_display = None
            if expiry_oi_list_primary and len(expiry_oi_list_primary) > 0:
                el = expiry_oi_list_primary
                pcr_n = el[0][3] if len(el) >= 1 else 0
                pcr_x = el[1][3] if len(el) >= 2 else 0
                ca = sum(i[1] for i in el)
                pa = sum(i[2] for i in el)
                pcr_a = round(pa / ca, 2) if ca else 0
                max_oi_row = max(el, key=lambda i: (i[1] or 0) + (i[2] or 0)) if el else (None, 0, 0, 0, None)
                ca_max, pa_max = (max_oi_row[1], max_oi_row[2]) if len(max_oi_row) >= 3 else (0, 0)
                if max_oi_row and len(max_oi_row) > 0:
                    max_oi_expiry_display = max_oi_row[0]
                decision, source = decide(pcr_n, pcr_x, pcr_a, ca, pa, ca_max, pa_max)
            else:

                # expiry_oi_list_primary failed (API timeout) — build from all_opts (includes same-day, matches Analytics)
                el = []
                for exp in sorted(set(o["expiry"] for o in all_opts)):
                    opts_exp = [o for o in all_opts if o["expiry"] == exp]
                    c, p, pr = oi_pcr(opts_exp)
                    mp = max_pain(opts_exp) if opts_exp else None
                    el.append((exp, c, p, pr, mp))
                pcr_n = el[0][3] if len(el) >= 1 else 0
                pcr_x = el[1][3] if len(el) >= 2 else 0
                ca = sum(i[1] for i in el)
                pa = sum(i[2] for i in el)
                pcr_a = round(pa / ca, 2) if ca else 0
                max_oi_row = max(el, key=lambda i: (i[1] or 0) + (i[2] or 0)) if el else (None, 0, 0, 0, None)
                ca_max, pa_max = (max_oi_row[1], max_oi_row[2]) if len(max_oi_row) >= 3 else (0, 0)
                if max_oi_row and len(max_oi_row) > 0:
                    max_oi_expiry_display = max_oi_row[0]
                decision, source = decide(pcr_n, pcr_x, pcr_a, ca, pa, ca_max, pa_max)
            # 🔒 FIXED — KABHI BHI CHANGE NA KAREIN: ca_max = 0 (invalid OI) → flip nahi, LAST_EXECUTED_SIGNAL hi rahega
            if STRATEGY_MODE == "MAX_OPEN_INTEREST" and LAST_EXECUTED_SIGNAL in ("SELL CALL", "SELL PUT"):
                if not ca_max or ca_max <= 0:
                    decision = LAST_EXECUTED_SIGNAL  # Invalid OI — flip mat karo (fixed rule)
            if STRATEGY_MODE == "MAX_OPEN_INTEREST" and max_oi_expiry_display:
                _exp_str = _format_expiry_for_display(max_oi_expiry_display)
                if _exp_str:
                    source = f"{source} ({_exp_str})"
                    # MAX_OPEN_INTEREST expiry change alert — ab script kis expiry ka PCR use kar raha hai
                    if LAST_MAX_OI_EXPIRY_STR is not None and _exp_str != LAST_MAX_OI_EXPIRY_STR:
                        pcr_now = round(pa_max / ca_max, 2) if ca_max else 0
                        prev_pcr = None
                        if expiry_oi_list_primary:
                            for row in expiry_oi_list_primary:
                                if len(row) >= 4 and _format_expiry_for_display(row[0]) == LAST_MAX_OI_EXPIRY_STR:
                                    prev_pcr = row[3]
                                    break
                        send_telegram(
                            f"📊 MAX_OPEN_INTEREST expiry change\n"
                            f"Previous: {LAST_MAX_OI_EXPIRY_STR} → Now: {_exp_str}\n"
                            f"PCR ({LAST_MAX_OI_EXPIRY_STR}): {prev_pcr if prev_pcr is not None else '—'}\n"
                            f"PCR ({_exp_str}): {pcr_now}\n"
                            f"Script ab {_exp_str} ka PCR use kar raha hai.\n"
                            f"Instrument: {SYMBOL}\n"
                            f"Time: {datetime.now().strftime('%H:%M:%S')}"
                        )
                    LAST_MAX_OI_EXPIRY_STR = _exp_str
                else:
                    LAST_MAX_OI_EXPIRY_STR = None
            else:
                LAST_MAX_OI_EXPIRY_STR = None  # strategy MAX_OPEN_INTEREST nahi to reset (next time pe "first" na dikhe)
            # NEW POSITION ALERT — IRON FLY OPENED jaisa format (Reason, Instrument, Exp, legs, Balance, Used, Position, Time)
            if pending_new_position_changes_txt is not None:
                pos_line = format_position_line_for_message(active_positions)
                expiry_for_msg = max_oi_expiry_display
                if not expiry_for_msg and active_positions:
                    expiry_for_msg = extract_expiry(active_positions[0].get("product_symbol", ""))
                strategy_for_msg = STRATEGY_MODE
                if STRATEGY_MODE == "MAX_OPEN_INTEREST" and max_oi_expiry_display:
                    _ed = _format_expiry_for_display(max_oi_expiry_display)
                    if _ed:
                        strategy_for_msg = f"MAX_OPEN_INTEREST ({_ed})"
                _positions_for_alert = positions_all if ORDER_TYPE == "futures" else active_positions
                msg_body = _format_new_position_telegram_lines(
                    _positions_for_alert, decision, SYMBOL, expiry_for_msg, bal, blocked, pos_line, strategy_mode=strategy_for_msg
                )
                send_telegram(msg_body)
                pending_new_position_changes_txt = None
            # 🛡️ FIRST RUN SAFETY — detect from actual position
            if LAST_EXECUTED_SIGNAL is None and has_active_position:
                if has_short_call:
                    LAST_EXECUTED_SIGNAL = "SELL CALL"
                elif has_short_put:
                    LAST_EXECUTED_SIGNAL = "SELL PUT"
            # Futures mode: infer from existing perpetual position direction
            if LAST_EXECUTED_SIGNAL is None and ORDER_TYPE == "futures":
                _perp_sym_fr = f"{SYMBOL}USD"
                for _p_fr in positions:
                    if _p_fr.get("product_symbol") == _perp_sym_fr:
                        _fsize_fr = float(_p_fr.get("size", 0))
                        if _fsize_fr < 0:
                            LAST_EXECUTED_SIGNAL = "SELL CALL"  # SHORT futures = bearish
                        elif _fsize_fr > 0:
                            LAST_EXECUTED_SIGNAL = "SELL PUT"   # LONG futures = bullish
                        break

            # If current strategy says NO TRADE but we already have an active position,
            # keep using the last executed bias for position maintenance and equality.
            execution_decision = decision
            if (
                decision == "NO TRADE"
                and has_active_position
                and LAST_EXECUTED_SIGNAL in ("SELL CALL", "SELL PUT")
            ):
                execution_decision = LAST_EXECUTED_SIGNAL

            # 🛑 SAFETY: Per instrument max 3 legs (BTC 3, ETH 3)
            if len(active_positions) >= 3:
                execution_status = "EQUALITY SKIPPED — MAX LEGS"
                adjustments = [] 
            # ================= EXECUTION STATUS DEFAULT =================
            if execution_blocked:
                execution_status = "BLOCKED: MAX 3 LEGS (per instrument)"

            elif execution_status.startswith("WAITING"):
                # ⏳ Equality is waiting for legs, do NOT overwrite status
                pass

            else:
                execution_status = "AUTO EXEC READY"

            # LEG_MODE 1/2/3 = build logic. Same leg rules: 1=3 legs, 2=2 legs, 3=1 leg (sell only). Galat samajh ke legs mat becho.
            # Legs bechna sirf: position 0 ho (naya build), leg/order fail ho rahe hon, ya explicit user action. Margin high hone par proactively reduce NAHI.

            # 🔒 GLOBAL SKIP GATE
            if SKIP_REST:
                execution_status = execution_status or "SKIPPED (SOFT)"
                SKIP_REST = False   # 🔥 IMPORTANT  

            # ================= BIAS CHANGE DETECTION =================
            bias_changed = (
                LAST_EXECUTED_SIGNAL is not None
                and execution_decision != LAST_EXECUTED_SIGNAL
            )

            # Pending rebalance: if previously skipped due to low balance, and now funds are available, allow it to run.
            if PENDING_BIAS_REBALANCE in ("SELL CALL", "SELL PUT"):
                _bal_p, _blocked_p, _ = wallet_usd()
                _free_p = (_bal_p - _blocked_p) if _bal_p is not None else 0
                _low_balance_mode_p = (
                    (_bal_p is not None and _bal_p <= LOW_BALANCE_USD)
                    or (_free_p <= LOW_FREE_USD)
                )
                if not _low_balance_mode_p and decision == PENDING_BIAS_REBALANCE:
                    # force treat as bias change so rebalance block executes
                    bias_changed = True

            if bias_changed and MARGIN_ENFORCEMENT_MODE == "SIGNAL":
                EQUALITY_EVENT = True
                EQUALITY_ARMED = True
                EQUALITY_TRIGGER_REASON = "BIAS_CHANGE_MARGIN"
                used_pct = (blocked / bal * 100) if bal > 0 else 0
                pos_line = format_position_line_for_message(active_positions)
                # NO TRADE: sirf ek hi alert per cycle (SELL CALL→NO TRADE spam avoid; SELL PUT→NO TRADE spam avoid)
                should_send = True
                if decision == "NO TRADE":
                    if NO_TRADE_ALERT_SENT_FOR_OLD_BIAS == LAST_EXECUTED_SIGNAL:
                        should_send = False  # pehle se bhej chuke
                    else:
                        NO_TRADE_ALERT_SENT_FOR_OLD_BIAS = LAST_EXECUTED_SIGNAL
                else:
                    NO_TRADE_ALERT_SENT_FOR_OLD_BIAS = None  # naya bias (CALL/PUT) — reset, next NO TRADE pe 1 alert
                if STRATEGY_MODE == "MAX_OPEN_INTEREST" and max_oi_expiry_display:
                    _ed = _format_expiry_for_display(max_oi_expiry_display)
                    strategy_line = f"Strategy: MAX_OPEN_INTEREST({_ed})\n" if _ed else f"Strategy: {STRATEGY_MODE}\n"
                    data_expiry_line = ""
                elif STRATEGY_MODE == "NEAR_PCR":
                    strategy_line = f"Strategy: {STRATEGY_MODE}\n"
                    data_expiry_line = "Data: Near expiry\n"
                elif STRATEGY_MODE == "NEXT_PCR":
                    strategy_line = f"Strategy: {STRATEGY_MODE}\n"
                    data_expiry_line = "Data: Next expiry\n"
                elif STRATEGY_MODE == "ALL_PCR":
                    strategy_line = f"Strategy: {STRATEGY_MODE}\n"
                    data_expiry_line = ""
                elif STRATEGY_MODE == "FIRST_PCR_CHANGE":
                    strategy_line = f"Strategy: {STRATEGY_MODE} (ALL_PCR, first change)\n"
                    data_expiry_line = f"Active instrument: {SYMBOL}\n"
                elif STRATEGY_MODE == "PRICE_CHANGE_DIST":
                    strategy_line = f"Strategy: {STRATEGY_MODE}\n"
                    _pcd = fetch_price_change_distribution(refresh=False)
                    if _pcd:
                        _thr = float(_pcd.get("threshold_pct", PRICE_CHANGE_ADV_THRESHOLD_PCT) or PRICE_CHANGE_ADV_THRESHOLD_PCT)
                        _d = int(_pcd.get("decliners", 0) or 0)
                        _a = int(_pcd.get("advancers", 0) or 0)
                        _u = int(_pcd.get("universe", 0) or 0)
                        data_expiry_line = f"Data: Perpetuals 24h mark >{_thr:.2f}% | D {_d} | A {_a} | U {_u}\n"
                    else:
                        data_expiry_line = "Data: Perpetuals 24h mark breadth\n"
                else:
                    strategy_line = f"Strategy: {STRATEGY_MODE}\n"
                    data_expiry_line = ""
                if should_send:
                    send_telegram(
                        f"🔄 BIAS CHANGE ALERT\n"
                        f"Time: {datetime.now().strftime('%H:%M:%S')}\n"
                        f"Old Bias: {LAST_EXECUTED_SIGNAL}\n"
                        f"New Bias: {decision}\n"
                        f"{strategy_line}"
                        f"{data_expiry_line}"
                        f"Spot: {spot:.2f}\n"
                        f"Balance: ${bal:.2f}\n"
                        f"Used: ${blocked:.2f} ({used_pct:.1f}%)\n"
                        f"Position: {pos_line}"
                    )

                # force fresh hedges on new bias
                has_any_long_call = False
                has_long_10d_call = False
                has_long_10d_put = False

                # allow rebalance AFTER new orders
                MARGIN_ADJUSTED_THIS_SIGNAL = False                

                # ================= UNKNOWN POSITION HANDLING =================
                # Ctrl+C = sirf script stop, position square-off mat karo
                if KILL_SWITCH:
                    break
                if bias_changed and decision != "NO TRADE":

                    unknown_positions = identify_unknown_positions(
                        positions,
                        decision
                    )

                    if unknown_positions and not KILL_SWITCH:
                        squared = square_off_unknown_positions(unknown_positions)
                        if squared > 0:
                            print("🛑 Square-off done — skipping execution this cycle")
                            SKIP_REST = True
                            _uc, _dr, _ds, _di, _ = calculate_margin_drift(bal, blocked, MIN_MARGIN_PCT)
                            render_terminal_ui(
                                now=datetime.now(), spot=spot, decision=decision, source=source,
                                bal=bal, blocked=blocked, upnl=upnl,
                                wallet_upnl_pct=wallet_upnl_pct, blocked_upnl_pct=blocked_upnl_pct,
                                used_pct_ui=used_pct_ui, heat_level=heat_level, heat_bar=heat_bar,
                                delta=delta, gamma=gamma,
                                position_details=position_details, position_count=position_count,
                                execution_status="Square-off done (unknown legs) — skipping execution this cycle",
                                used_pct_calc=_uc, margin_drift=_dr, drift_status=_ds, drift_icon=_di,
                                api_score=api_score, api_status=api_status, api_bar=api_bar,
                                expiry_oi_list=expiry_oi_list_primary or [], expiry_oi_list_secondary=expiry_oi_list_secondary_oi or [],
                                spot_secondary=spot_secondary_oi, symbol_secondary=symbol_secondary_oi, symbol_primary=main_sym,
                                all_expiry_max_pain_primary=all_expiry_mp_primary, all_expiry_max_pain_secondary=all_expiry_mp_secondary,
                                projected_profit=None, max_loss=None,
                                position_delta_sum=None, position_gamma_sum=None, position_theta_sum=None,
                                position_vega_sum=None, position_rho_sum=None,
                                strike_order_ok=None, strike_order_msg=None, option_metrics=None,
                                last_executed_signal=LAST_EXECUTED_SIGNAL,
                                **price_change_ui_kwargs
                            )
                            time.sleep(REFRESH_SECONDS)
                            continue

                # ================= OPTIONS MODE: futures position open hai to close karo =================
                if (
                    ORDER_TYPE != "futures"
                    and USER_AUTO_EXECUTION
                    and not API_DOWN
                    and not KILL_SWITCH
                ):
                    _perp_sym = f"{SYMBOL}USD"
                    _fut_pos = next(
                        (p for p in positions
                         if p.get("product_symbol", "") == _perp_sym
                         and float(p.get("size", 0)) != 0),
                        None
                    )
                    if _fut_pos:
                        square_off_position(_perp_sym, float(_fut_pos.get("size", 0)), reason="OPTIONS_MODE_SWITCH")
                        send_telegram(
                            f"🔄 OPTIONS MODE: Futures position closed\n"
                            f"Symbol : {_perp_sym}\n"
                            f"Size   : {abs(float(_fut_pos.get('size', 0))):.0f}x\n"
                            f"Next   : Options position will be built\n"
                            f"Time   : {datetime.now().strftime('%H:%M:%S')}"
                        )
                        LAST_EXECUTED_SIGNAL = None
                        SKIP_REST = True
                        continue

                # ================= FUTURES MODE: bias change pe options close → futures open =================
                if (
                    ORDER_TYPE == "futures"
                    and bias_changed
                    and decision in ("SELL CALL", "SELL PUT")
                    and USER_AUTO_EXECUTION
                    and not API_DOWN
                    and not KILL_SWITCH
                ):
                    # Saari options positions close karo pehle
                    _opts_to_close = [
                        p for p in positions
                        if p.get("product_symbol", "").startswith(("C-", "P-"))
                        and float(p.get("size", 0)) != 0
                    ]
                    if _opts_to_close:
                        for _p in _opts_to_close:
                            square_off_position(_p["product_symbol"], float(_p.get("size", 0)), reason="FUTURES_MODE_SWITCH")
                            time.sleep(0.3)
                        send_telegram(
                            f"🔄 FUTURES MODE: Options positions closed on bias change\n"
                            f"Bias : {LAST_EXECUTED_SIGNAL} → {decision}\n"
                            f"Closed : {len(_opts_to_close)} leg(s)\n"
                            f"Next : Futures position will open\n"
                            f"Time : {datetime.now().strftime('%H:%M:%S')}"
                        )
                        LAST_EXECUTED_SIGNAL = None
                        SKIP_REST = True
                        time.sleep(0.5)
                        continue
                    # Options nahi hain — seedha futures banao (neeche execution block chalega)
                    LAST_EXECUTED_SIGNAL = None

                # ================= BIAS CHANGE REBALANCE (short + 10d longs) =================
                # Goal:
                # - Sell leg hamesha bias direction me rahe (SELL CALL => short call, SELL PUT => short put)
                # - Long legs me jo 10-delta se drift ho gaya ho, sirf usi ko roll/replace karo
                # - Margin agar ceiling se upar ho to pehle size reduce karke ceiling ke andar lao (fund safety)
                if (
                    bias_changed
                    and decision in ("SELL CALL", "SELL PUT")
                    and USER_AUTO_EXECUTION
                    and not API_DOWN
                    and not KILL_SWITCH
                    and ORDER_TYPE != "futures"
                ):
                    # Bias change: sell=long=long try karo — cooldown block nahi (sirf bias change pe naye legs add try)
                    # Low balance mode: avoid opening/rolling legs (orders will likely reject). Only cleanup/close wrong legs.
                    _bal_lb, _blocked_lb, _ = wallet_usd()
                    _free_lb = (_bal_lb - _blocked_lb) if _bal_lb is not None else 0
                    _low_balance_mode = (
                        (_bal_lb is not None and _bal_lb <= LOW_BALANCE_USD)
                        or (_free_lb <= LOW_FREE_USD)
                    )
                    if _low_balance_mode:
                        # Mark pending so when balance improves we can fully rebalance
                        PENDING_BIAS_REBALANCE = decision
                        if not PENDING_BIAS_SINCE:
                            PENDING_BIAS_SINCE = time.time()

                    # Pick expiry context from any existing leg (same-expiry is enforced elsewhere)
                    _exp_leg = None
                    for _p in [short_call_pos, short_put_pos, long_call_pos, long_put_pos]:
                        if _p:
                            _exp_leg = extract_expiry(_p.get("product_symbol", ""))
                            break
                    _opts_exp = [o for o in all_opts if o["expiry"] == _exp_leg] if _exp_leg else (near or all_opts)

                    # Refresh legs after any square-off above
                    positions = get_positions()
                    if API_DOWN:
                        SKIP_REST = True
                        _uc, _dr, _ds, _di, _ = calculate_margin_drift(bal, blocked, MIN_MARGIN_PCT)
                        render_terminal_ui(
                            now=datetime.now(), spot=spot, decision=decision, source=source,
                            bal=bal, blocked=blocked, upnl=upnl,
                            wallet_upnl_pct=wallet_upnl_pct, blocked_upnl_pct=blocked_upnl_pct,
                            used_pct_ui=used_pct_ui, heat_level=heat_level, heat_bar=heat_bar,
                            delta=delta, gamma=gamma,
                            position_details=position_details, position_count=position_count,
                            execution_status="API DOWN (no orders)",
                            used_pct_calc=_uc, margin_drift=_dr, drift_status=_ds, drift_icon=_di,
                            api_score=api_score, api_status=api_status, api_bar=api_bar,
                            expiry_oi_list=expiry_oi_list_primary or [], expiry_oi_list_secondary=expiry_oi_list_secondary_oi or [],
                            spot_secondary=spot_secondary_oi, symbol_secondary=symbol_secondary_oi, symbol_primary=main_sym,
                            all_expiry_max_pain_primary=all_expiry_mp_primary, all_expiry_max_pain_secondary=all_expiry_mp_secondary,
                            projected_profit=None, max_loss=None,
                            position_delta_sum=None, position_gamma_sum=None, position_theta_sum=None,
                            position_vega_sum=None, position_rho_sum=None,
                            strike_order_ok=None, strike_order_msg=None, option_metrics=None,
                            last_executed_signal=LAST_EXECUTED_SIGNAL,
                            **price_change_ui_kwargs
                        )
                        time.sleep(REFRESH_SECONDS)
                        continue
                    short_call_pos = short_put_pos = None
                    long_call_pos = long_put_pos = None
                    for _pos in positions:
                        _sym = _pos.get("product_symbol", "")
                        try:
                            _sz = float(_pos.get("size", 0))
                        except (ValueError, TypeError):
                            _sz = 0
                        if _sz == 0:
                            continue
                        if _sym.startswith("C-") and _sz < 0:
                            short_call_pos = _pos
                        elif _sym.startswith("C-") and _sz > 0:
                            long_call_pos = _pos
                        elif _sym.startswith("P-") and _sz < 0:
                            short_put_pos = _pos
                        elif _sym.startswith("P-") and _sz > 0:
                            long_put_pos = _pos

                    def _abs_int_size(p):
                        try:
                            return abs(int(float(p.get("size", 0)))) if p else 0
                        except (ValueError, TypeError):
                            return 0

                    def _is_10d(p):
                        try:
                            d = abs(float(p.get("delta", 0) or 0))
                        except (ValueError, TypeError):
                            d = 0
                        return abs(d - HEDGE_DELTA_TARGET) <= DELTA_THRESHOLD

                    def _delta_abs(p):
                        try:
                            return abs(float(p.get("delta", 0) or 0))
                        except (ValueError, TypeError):
                            return 0.0

                    # 0) Extra-legs cleanup (log me 4–5 legs cases aaye the). Keep: 1 long call, 1 long put, 1 short (as per bias)
                    short_calls = []
                    short_puts = []
                    long_calls = []
                    long_puts = []
                    for _pos in positions:
                        _sym = _pos.get("product_symbol", "")
                        try:
                            _sz = float(_pos.get("size", 0))
                        except (ValueError, TypeError):
                            _sz = 0
                        if _sz == 0:
                            continue
                        if _sym.startswith("C-") and _sz < 0:
                            short_calls.append(_pos)
                        elif _sym.startswith("C-") and _sz > 0:
                            long_calls.append(_pos)
                        elif _sym.startswith("P-") and _sz < 0:
                            short_puts.append(_pos)
                        elif _sym.startswith("P-") and _sz > 0:
                            long_puts.append(_pos)

                    def _pick_best_10d(cands):
                        if not cands:
                            return None
                        # Prefer 10d (<=0.15), else closest to 0.10
                        def key(p):
                            d = _delta_abs(p)
                            return (0 if d <= 0.15 else 1, abs(d - 0.10), -abs(float(p.get("size", 0) or 0)))
                        return min(cands, key=key)

                    keep_lc = _pick_best_10d(long_calls)
                    keep_lp = _pick_best_10d(long_puts)
                    # Keep one correct short if already present, else keep none (we will add)
                    keep_sc = max(short_calls, key=lambda p: abs(float(p.get("size", 0) or 0))) if short_calls else None
                    keep_sp = max(short_puts, key=lambda p: abs(float(p.get("size", 0) or 0))) if short_puts else None

                    to_close = []
                    # Close wrong-direction shorts immediately (bias safety)
                    if decision == "SELL CALL":
                        to_close.extend(short_puts)
                        # keep only one short call if multiple
                        to_close.extend([p for p in short_calls if keep_sc and p is not keep_sc])
                    elif decision == "SELL PUT":
                        to_close.extend(short_calls)
                        # keep only one short put if multiple
                        to_close.extend([p for p in short_puts if keep_sp and p is not keep_sp])
                    # Close extra longs; keep only best 10d-ish per side
                    to_close.extend([p for p in long_calls if keep_lc and p is not keep_lc])
                    to_close.extend([p for p in long_puts if keep_lp and p is not keep_lp])

                    if to_close:
                        for p in to_close:
                            sym_p = p.get("product_symbol", "")
                            try:
                                sz_p = float(p.get("size", 0))
                            except (ValueError, TypeError):
                                sz_p = 0
                            if sym_p and sz_p != 0:
                                square_off_position(sym_p, sz_p, reason="BIAS_CHANGE_CLEAN_EXTRA_LEGS")
                                time.sleep(0.2)
                        time.sleep(0.8)

                        # Refresh after cleanup
                        positions = get_positions()
                        if API_DOWN:
                            SKIP_REST = True
                            continue
                        # Rebuild single-leg pointers from cleaned positions
                        short_call_pos = short_put_pos = None
                        long_call_pos = long_put_pos = None
                        for _pos in positions:
                            _sym = _pos.get("product_symbol", "")
                            try:
                                _sz = float(_pos.get("size", 0))
                            except (ValueError, TypeError):
                                _sz = 0
                            if _sz == 0:
                                continue
                            if _sym.startswith("C-") and _sz < 0:
                                short_call_pos = _pos
                            elif _sym.startswith("C-") and _sz > 0:
                                long_call_pos = _pos
                            elif _sym.startswith("P-") and _sz < 0:
                                short_put_pos = _pos
                            elif _sym.startswith("P-") and _sz > 0:
                                long_put_pos = _pos

                    # If low-balance, stop here (only cleanup). Don't try to open/roll anything.
                    if _low_balance_mode:
                        # But bias direction guarantee: never stay in opposite short.
                        # Try (cooldown) to align short in bias direction; if rejected, at least wrong short is removed.
                        _now = time.time()
                        _can_try = (_now - LOW_BALANCE_ALIGN_LAST_TRY) >= LOW_BALANCE_ALIGN_COOLDOWN_SEC

                        # Refresh current pointers one more time (after cleanup)
                        positions = get_positions()
                        if API_DOWN:
                            SKIP_REST = True
                            continue
                        short_call_pos = short_put_pos = None
                        long_call_pos = long_put_pos = None
                        for _pos in positions:
                            _sym = _pos.get("product_symbol", "")
                            try:
                                _sz = float(_pos.get("size", 0))
                            except (ValueError, TypeError):
                                _sz = 0
                            if _sz == 0:
                                continue
                            if _sym.startswith("C-") and _sz < 0:
                                short_call_pos = _pos
                            elif _sym.startswith("C-") and _sz > 0:
                                long_call_pos = _pos
                            elif _sym.startswith("P-") and _sz < 0:
                                short_put_pos = _pos
                            elif _sym.startswith("P-") and _sz > 0:
                                long_put_pos = _pos

                        def _abs_int_size(p):
                            try:
                                return abs(int(float(p.get("size", 0)))) if p else 0
                            except (ValueError, TypeError):
                                return 0

                        _target_size = max(_abs_int_size(short_call_pos), _abs_int_size(short_put_pos), _abs_int_size(long_call_pos), _abs_int_size(long_put_pos), 1)

                        if _can_try:
                            LOW_BALANCE_ALIGN_LAST_TRY = _now
                            if decision == "SELL CALL":
                                # Close opposite short if any (should already be closed by cleanup)
                                if short_put_pos:
                                    square_off_position(short_put_pos["product_symbol"], float(short_put_pos.get("size", 0)), reason="LOW_BALANCE_BIAS_ALIGN")
                                    time.sleep(0.4)
                                # Try to create correct short call if missing
                                if not short_call_pos:
                                    atm_call = select_atm_option(_opts_exp, spot, "call")
                                    if atm_call:
                                        place_order(atm_call["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="LOW_BALANCE_BIAS_ALIGN")
                                        time.sleep(0.4)
                            elif decision == "SELL PUT":
                                if short_call_pos:
                                    square_off_position(short_call_pos["product_symbol"], float(short_call_pos.get("size", 0)), reason="LOW_BALANCE_BIAS_ALIGN")
                                    time.sleep(0.4)
                                if not short_put_pos:
                                    atm_put = select_atm_option(_opts_exp, spot, "put")
                                    if atm_put:
                                        place_order(atm_put["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="LOW_BALANCE_BIAS_ALIGN")
                                        time.sleep(0.4)

                        # Leg mode: 3 = only short; 2 = short + one long. Never close correct short.
                        _tier_lb = LEG_MODE
                        if _tier_lb == 3:
                            if long_call_pos:
                                square_off_position(long_call_pos["product_symbol"], float(long_call_pos.get("size", 0)), reason="LOW_BAL_TIER3_SELL_ONLY")
                                time.sleep(0.4)
                            if long_put_pos:
                                square_off_position(long_put_pos["product_symbol"], float(long_put_pos.get("size", 0)), reason="LOW_BAL_TIER3_SELL_ONLY")
                                time.sleep(0.4)
                        elif _tier_lb == 2 and long_call_pos and long_put_pos:
                            if decision == "SELL CALL":
                                square_off_position(long_call_pos["product_symbol"], float(long_call_pos.get("size", 0)), reason="LOW_BAL_TIER2_SELL_LONG")
                                time.sleep(0.4)
                            else:
                                square_off_position(long_put_pos["product_symbol"], float(long_put_pos.get("size", 0)), reason="LOW_BAL_TIER2_SELL_LONG")
                                time.sleep(0.4)

                        execution_status = "LOW BALANCE — bias aligned best-effort (no hedge rolls/adds)"
                        SKIP_REST = True
                        continue

                    _sz_sc = _abs_int_size(short_call_pos)
                    _sz_sp = _abs_int_size(short_put_pos)
                    _sz_lc = _abs_int_size(long_call_pos)
                    _sz_lp = _abs_int_size(long_put_pos)
                    _target_size = max(_sz_sc, _sz_sp, _sz_lc, _sz_lp, 1)
                    _bal2, _blocked2, _ = wallet_usd()
                    _tier_rebal = LEG_MODE

                    # 1) Margin too high => reduce size proportionally (close all three legs equally)
                    _used_pct_now = (_blocked2 / _bal2 * 100) if _bal2 > 0 else 0
                    _margin_ceiling = (MIN_MARGIN_PCT * 100) * 1.01
                    _ceiling_hard = _margin_ceiling + 1
                    # Only reduce when short is in bias direction; reduce only that short + both longs (never touch wrong short)
                    _short_pos = short_call_pos if decision == "SELL CALL" else short_put_pos
                    if (
                        _used_pct_now >= _ceiling_hard
                        and long_call_pos
                        and long_put_pos
                        and _short_pos
                    ):
                        _base = min(_abs_int_size(long_call_pos), _abs_int_size(long_put_pos), _abs_int_size(_short_pos))
                        if _base > 1:
                            _new = max(1, int(_base * (_ceiling_hard / max(_used_pct_now, 0.0001))))
                            _close_each = max(0, _base - _new)
                            if _close_each > 0:
                                # close same qty: bias-direction short only + both longs (position hamesha bias direction me)
                                square_off_position(_short_pos["product_symbol"], -_close_each, reason="MARGIN_REDUCE_ON_BIAS_CHANGE")
                                square_off_position(long_call_pos["product_symbol"], +_close_each, reason="MARGIN_REDUCE_ON_BIAS_CHANGE")
                                square_off_position(long_put_pos["product_symbol"], +_close_each, reason="MARGIN_REDUCE_ON_BIAS_CHANGE")
                                time.sleep(0.8)

                                # Refresh after reduce
                                positions = get_positions()
                                if API_DOWN:
                                    SKIP_REST = True
                                    continue
                                short_call_pos = short_put_pos = None
                                long_call_pos = long_put_pos = None
                                for _pos in positions:
                                    _sym = _pos.get("product_symbol", "")
                                    try:
                                        _sz = float(_pos.get("size", 0))
                                    except (ValueError, TypeError):
                                        _sz = 0
                                    if _sz == 0:
                                        continue
                                    if _sym.startswith("C-") and _sz < 0:
                                        short_call_pos = _pos
                                    elif _sym.startswith("C-") and _sz > 0:
                                        long_call_pos = _pos
                                    elif _sym.startswith("P-") and _sz < 0:
                                        short_put_pos = _pos
                                    elif _sym.startswith("P-") and _sz > 0:
                                        long_put_pos = _pos
                                _target_size = max(_abs_int_size(short_call_pos), _abs_int_size(short_put_pos), _abs_int_size(long_call_pos), _abs_int_size(long_put_pos), 1)

                    # 2) Long legs: tier 3 = no longs; tier 2 = one long (SELL CALL → long put only; SELL PUT → long call only); tier 1 = both longs
                    if _tier_rebal == 3:
                        # Tier 3: sell only. Close any longs (tier enforcement at top should have done it; repeat for safety).
                        if long_call_pos:
                            square_off_position(long_call_pos["product_symbol"], float(long_call_pos.get("size", 0)), reason="TIER3_BIAS_REBAL")
                            time.sleep(0.4)
                        if long_put_pos:
                            square_off_position(long_put_pos["product_symbol"], float(long_put_pos.get("size", 0)), reason="TIER3_BIAS_REBAL")
                            time.sleep(0.4)
                    elif _tier_rebal == 2:
                        # Tier 2: short + one long. SELL CALL → keep long put only (close long call); SELL PUT → keep long call only (close long put).
                        if decision == "SELL CALL":
                            if long_call_pos:
                                square_off_position(long_call_pos["product_symbol"], float(long_call_pos.get("size", 0)), reason="TIER2_ONE_LONG")
                                time.sleep(0.4)
                            long_call_pos = None
                            if long_put_pos and not _is_10d(long_put_pos):
                                _sz = float(long_put_pos.get("size", 0))
                                if _sz != 0:
                                    square_off_position(long_put_pos["product_symbol"], _sz, reason="BIAS_CHANGE_LONG_ROLL")
                                    time.sleep(0.5)
                                put_10d = select_delta_option(_opts_exp, "put", 0.10)
                                if put_10d:
                                    place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="BIAS_CHANGE_LONG_ROLL")
                                    time.sleep(0.5)
                            if not long_put_pos:
                                put_10d = select_delta_option(_opts_exp, "put", 0.10)
                                if put_10d:
                                    place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="BIAS_CHANGE_BUILD")
                                    time.sleep(0.5)
                            elif _abs_int_size(long_put_pos) < _target_size:
                                diff = _target_size - _abs_int_size(long_put_pos)
                                if diff > 0:
                                    place_order(long_put_pos["product_symbol"], "buy", diff, MARGIN_MODE.lower(), reason="BIAS_CHANGE_BUILD")
                                    time.sleep(0.5)
                        else:
                            if long_put_pos:
                                square_off_position(long_put_pos["product_symbol"], float(long_put_pos.get("size", 0)), reason="TIER2_ONE_LONG")
                                time.sleep(0.4)
                            long_put_pos = None
                            if long_call_pos and not _is_10d(long_call_pos):
                                _sz = float(long_call_pos.get("size", 0))
                                if _sz != 0:
                                    square_off_position(long_call_pos["product_symbol"], _sz, reason="BIAS_CHANGE_LONG_ROLL")
                                    time.sleep(0.5)
                                call_10d = select_delta_option(_opts_exp, "call", 0.10)
                                if call_10d:
                                    place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="BIAS_CHANGE_LONG_ROLL")
                                    time.sleep(0.5)
                            if not long_call_pos:
                                call_10d = select_delta_option(_opts_exp, "call", 0.10)
                                if call_10d:
                                    place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="BIAS_CHANGE_BUILD")
                                    time.sleep(0.5)
                            elif _abs_int_size(long_call_pos) < _target_size:
                                diff = _target_size - _abs_int_size(long_call_pos)
                                if diff > 0:
                                    place_order(long_call_pos["product_symbol"], "buy", diff, MARGIN_MODE.lower(), reason="BIAS_CHANGE_BUILD")
                                    time.sleep(0.5)
                    else:
                        # Tier 1: both longs (3 legs)
                        if long_call_pos and not _is_10d(long_call_pos):
                            _sz = float(long_call_pos.get("size", 0))
                            if _sz != 0:
                                square_off_position(long_call_pos["product_symbol"], _sz, reason="BIAS_CHANGE_LONG_ROLL")
                                time.sleep(0.5)
                            call_10d = select_delta_option(_opts_exp, "call", 0.10)
                            if call_10d:
                                place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="BIAS_CHANGE_LONG_ROLL")
                                time.sleep(0.5)
                        if long_put_pos and not _is_10d(long_put_pos):
                            _sz = float(long_put_pos.get("size", 0))
                            if _sz != 0:
                                square_off_position(long_put_pos["product_symbol"], _sz, reason="BIAS_CHANGE_LONG_ROLL")
                                time.sleep(0.5)
                            put_10d = select_delta_option(_opts_exp, "put", 0.10)
                            if put_10d:
                                place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="BIAS_CHANGE_LONG_ROLL")
                                time.sleep(0.5)

                    # 3) Ensure longs exist per tier (tier 3 = none, tier 2 = one, tier 1 = both)
                    if _tier_rebal == 1:
                        if not long_call_pos:
                            call_10d = select_delta_option(_opts_exp, "call", 0.10)
                            if call_10d:
                                place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="BIAS_CHANGE_BUILD")
                                time.sleep(0.5)
                        elif _abs_int_size(long_call_pos) < _target_size:
                            diff = _target_size - _abs_int_size(long_call_pos)
                            if diff > 0:
                                place_order(long_call_pos["product_symbol"], "buy", diff, MARGIN_MODE.lower(), reason="BIAS_CHANGE_BUILD")
                                time.sleep(0.5)

                        if not long_put_pos:
                            put_10d = select_delta_option(_opts_exp, "put", 0.10)
                            if put_10d:
                                place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="BIAS_CHANGE_BUILD")
                                time.sleep(0.5)
                        elif _abs_int_size(long_put_pos) < _target_size:
                            diff = _target_size - _abs_int_size(long_put_pos)
                            if diff > 0:
                                place_order(long_put_pos["product_symbol"], "buy", diff, MARGIN_MODE.lower(), reason="BIAS_CHANGE_BUILD")
                                time.sleep(0.5)

                    # 4) Short leg: keep it aligned with bias
                    if decision == "SELL CALL":
                        if short_put_pos:
                            square_off_position(short_put_pos["product_symbol"], float(short_put_pos.get("size", 0)), reason="BIAS_CHANGE_SHORT_SWAP")
                            time.sleep(0.5)
                        if not short_call_pos:
                            atm_call = select_atm_option(_opts_exp, spot, "call")
                            if atm_call:
                                place_order(atm_call["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="BIAS_CHANGE_SHORT_SWAP")
                                time.sleep(0.5)
                        elif _abs_int_size(short_call_pos) < _target_size:
                            diff = _target_size - _abs_int_size(short_call_pos)
                            if diff > 0:
                                place_order(short_call_pos["product_symbol"], "sell", diff, MARGIN_MODE.lower(), reason="BIAS_CHANGE_SHORT_SWAP")
                                time.sleep(0.5)
                    elif decision == "SELL PUT":
                        if short_call_pos:
                            square_off_position(short_call_pos["product_symbol"], float(short_call_pos.get("size", 0)), reason="BIAS_CHANGE_SHORT_SWAP")
                            time.sleep(0.5)
                        if not short_put_pos:
                            atm_put = select_atm_option(_opts_exp, spot, "put")
                            if atm_put:
                                place_order(atm_put["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="BIAS_CHANGE_SHORT_SWAP")
                                time.sleep(0.5)
                        elif _abs_int_size(short_put_pos) < _target_size:
                            diff = _target_size - _abs_int_size(short_put_pos)
                            if diff > 0:
                                place_order(short_put_pos["product_symbol"], "sell", diff, MARGIN_MODE.lower(), reason="BIAS_CHANGE_SHORT_SWAP")
                                time.sleep(0.5)

                    # Mark handled so we don't keep re-triggering bias_changed loop
                    LAST_EXECUTED_SIGNAL = decision
                    LAST_EXECUTION_TIME = time.time()
                    EQUALITY_EVENT = True
                    EQUALITY_ARMED = True
                    EQUALITY_TRIGGER_REASON = "BIAS_CHANGE_REBALANCE"
                    PENDING_BIAS_REBALANCE = None
                    PENDING_BIAS_SINCE = 0.0
                    SKIP_REST = True
                    continue

            # ================= ADD MISSING LEG (Step 4) — Short=Long=Long: koi bhi leg missing = INSTANT add =================
            # Gate: bias change pe allow (sell=long=long changes). Non-bias me cooldown block. Margin 10% ke andar hona chahiye.
            _used_pct_add = (blocked / bal * 100) if bal and bal > 0 else 0
            _ceiling_soft_add = (MIN_MARGIN_PCT * 100) * 1.01 + 1
            _order_fail_cooldown = time.time() < ORDER_FAILURE_COOLDOWN_UNTIL
            _add_leg_ok = (bias_changed or not _order_fail_cooldown) and _used_pct_add <= _ceiling_soft_add
            if execution_decision in ("SELL CALL", "SELL PUT") and not SKIP_REST and USER_AUTO_EXECUTION and not API_DOWN and _add_leg_ok:
                _legs_count = (1 if short_call_pos else 0) + (1 if long_call_pos else 0) + (1 if short_put_pos else 0) + (1 if long_put_pos else 0)
                if _legs_count < 3:
                    _exp_leg = None
                    for _p in [short_call_pos, short_put_pos, long_call_pos, long_put_pos]:
                        if _p:
                            _exp_leg = extract_expiry(_p.get("product_symbol", ""))
                            break
                    _opts_exp = [o for o in all_opts if o["expiry"] == _exp_leg] if _exp_leg else (near or all_opts)
                    _target_size = 0
                    for _p in [short_call_pos, short_put_pos, long_call_pos, long_put_pos]:
                        if _p:
                            _target_size = max(_target_size, abs(int(float(_p.get("size", 0)))))
                    _target_size = max(_target_size, 1)
                    if "SELL PUT" in execution_decision and short_put_pos and long_call_pos and not long_put_pos:
                        put_10d = select_delta_option(_opts_exp, "put", 0.10)
                        if put_10d:
                            res = place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Long Put {put_10d['symbol']} size {_target_size}\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue
                    elif "SELL PUT" in execution_decision and short_put_pos and not long_call_pos and not long_put_pos:
                        put_10d = select_delta_option(_opts_exp, "put", 0.10)
                        call_10d = select_delta_option(_opts_exp, "call", 0.10)
                        if put_10d and call_10d:
                            place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            time.sleep(0.5)
                            res = place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Long Put + Long Call\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue
                    elif "SELL PUT" in execution_decision and long_put_pos and not short_put_pos and not long_call_pos:
                        atm_put = select_atm_option(_opts_exp, spot, "put")
                        call_10d = select_delta_option(_opts_exp, "call", 0.10)
                        if atm_put and call_10d:
                            place_order(atm_put["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            time.sleep(0.5)
                            res = place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Short Put + Long Call\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue
                    elif "SELL PUT" in execution_decision and long_call_pos and not short_put_pos and not long_put_pos:
                        atm_put = select_atm_option(_opts_exp, spot, "put")
                        put_10d = select_delta_option(_opts_exp, "put", 0.10)
                        if atm_put and put_10d:
                            place_order(atm_put["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            time.sleep(0.5)
                            res = place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Short Put + Long Put\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue
                    elif "SELL PUT" in execution_decision and short_put_pos and long_put_pos and not long_call_pos:
                        call_10d = select_delta_option(_opts_exp, "call", 0.10)
                        if call_10d:
                            res = place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Long Call {call_10d['symbol']} size {_target_size}\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue
                    elif "SELL CALL" in execution_decision and short_call_pos and not long_call_pos and not long_put_pos:
                        call_10d = select_delta_option(_opts_exp, "call", 0.10)
                        put_10d = select_delta_option(_opts_exp, "put", 0.10)
                        if call_10d and put_10d:
                            place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            time.sleep(0.5)
                            res = place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Long Call + Long Put\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue
                    elif "SELL CALL" in execution_decision and long_call_pos and not short_call_pos and not long_put_pos:
                        atm_call = select_atm_option(_opts_exp, spot, "call")
                        put_10d = select_delta_option(_opts_exp, "put", 0.10)
                        if atm_call and put_10d:
                            place_order(atm_call["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            time.sleep(0.5)
                            res = place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Short Call + Long Put\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue
                    elif "SELL CALL" in execution_decision and long_put_pos and not short_call_pos and not long_call_pos:
                        atm_call = select_atm_option(_opts_exp, spot, "call")
                        call_10d = select_delta_option(_opts_exp, "call", 0.10)
                        if atm_call and call_10d:
                            place_order(atm_call["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            time.sleep(0.5)
                            res = place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Short Call + Long Call\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue
                    elif "SELL CALL" in execution_decision and long_call_pos and long_put_pos and not short_call_pos:
                        atm_call = select_atm_option(_opts_exp, spot, "call")
                        if atm_call:
                            res = place_order(atm_call["symbol"], "sell", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Short Call {atm_call['symbol']} size {_target_size}\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue
                    elif "SELL CALL" in execution_decision and short_call_pos and long_put_pos and not long_call_pos:
                        call_10d = select_delta_option(_opts_exp, "call", 0.10)
                        if call_10d:
                            res = place_order(call_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Long Call {call_10d['symbol']} size {_target_size}\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue
                    elif "SELL CALL" in execution_decision and short_call_pos and long_call_pos and not long_put_pos:
                        put_10d = select_delta_option(_opts_exp, "put", 0.10)
                        if put_10d:
                            res = place_order(put_10d["symbol"], "buy", _target_size, MARGIN_MODE.lower(), reason="ADD_MISSING_LEG")
                            if res:
                                send_telegram(f"🔧 INSTANT: Added missing Long Put {put_10d['symbol']} size {_target_size}\nReason: Short=Long=Long — koi bhi leg missing add\nTime: {datetime.now().strftime('%H:%M:%S')}")
                                SKIP_REST = True
                                continue

            # ================= WRONG/EXTRA LEGS — pehle clean (3 leg only), phir ADD/NEW. Galat leg turant close =================
            if execution_decision in ("SELL CALL", "SELL PUT"):
                wrong_extra = identify_wrong_or_extra_legs_to_close(positions, execution_decision, all_opts, bias_changed)
                if wrong_extra:
                    for pos in wrong_extra:
                        sym = pos.get("product_symbol", "")
                        size = float(pos.get("size", 0))
                        if sym and size != 0:
                            print(f"🛑 Closing wrong/extra leg: {sym} (size: {size}) — 3 leg equal quantity only")
                            res = square_off_position(sym, size, reason="WRONG_EXTRA_LEG")
                            if res:
                                send_telegram(
                                    f"🔄 WRONG LEG CLOSED: {sym} size {size}\n"
                                    f"Reason: put sell = put buy = call buy (equal qty only)\n"
                                    f"Time: {datetime.now().strftime('%H:%M:%S')}"
                                )
                    SKIP_REST = True
                    continue

            # ================= DONO MANAGE: second pass — other instrument (BTC+ETH dono position → dono manage) =================
            if manage_both and not SKIP_REST:
                other_sym = "ETH" if SYMBOL == "BTC" else "BTC"
                expiry_oi_other = expiry_oi_list_secondary_oi  # secondary = other symbol's OI
                spot_other = spot_secondary_oi or 0  # 0 → _manage_instrument_pass uses get_spot()
                if _manage_instrument_pass(other_sym, expiry_oi_other, spot_other):
                    SKIP_REST = True
                    continue

            # ================= EXPIRY AUTO ROLL =================
            auto_roll = (
                LAST_EXECUTED_SIGNAL is not None
                and decision == LAST_EXECUTED_SIGNAL
                and near
                and nxt
            )
            if not auto_roll:
                EXPIRY_ROLL_CONFIRMATION_PRINTED = False  # next roll pe phir ek bar confirm print hoga

            if (
                LAST_EXECUTED_SIGNAL is not None
                and decision == LAST_EXECUTED_SIGNAL        # bias same
                and decision != "NO TRADE"
                and near                                    # near expiry exists
            ):
                near_expiry_date = near[0]["expiry"]

                if auto_roll:
                    LAST_EXECUTED_SIGNAL = decision
                    LAST_EXECUTION_TIME = time.time()
                    expiry_reset = True
                    EQUALITY_EVENT = True
                    EQUALITY_ARMED = True
                    EQUALITY_TRIGGER_REASON = "EXPIRY_ROLL"
                    auto_roll = True
                    expiry_reset = True   
                
            # Decide which expiry to trade: on bias change or no active position, always use the earliest available orderable expiry.
            if bias_changed or not has_active_position:
                roll_opts = near if near else (nxt if nxt else [])
            elif dominant_expiry_this_cycle is not None:
                roll_opts = [o for o in all_opts if o["expiry"] == dominant_expiry_this_cycle]
                if not roll_opts:
                    # Expired ya chain me nahi — naya near use karo (orderable only; all_opts mat — same-day 400 avoid)
                    roll_opts = nxt if auto_roll and nxt else (near if near else (nxt if nxt else []))
            else:
                # No positions — sirf orderable expiry me position banao (same-day expiry pe order mat do)
                roll_opts = near if near else (nxt if nxt else [])
            # ============================================================
            # EXECUTION FLOW (5 STEPS — strict order)
            # 1. HEALTH: API + cooldown — fail = NO ORDERS (already done above)
            # 2. BIAS/STRATEGY: decision (SELL CALL / SELL PUT / NO TRADE) — NO TRADE = no entry
            # 3. BALANCE: bal > 0, margin headroom — fail = no new position
            # 4. POSITION: new entry / add missing / equality — only if 1–3 OK
            # 5. MARGIN TOP-UP: used < min → assume + balance — only after position exists
            # RULE: Bina reason ke koi order nahi (place_order blocks if reason empty)
            # ============================================================
            # 🔒 GLOBAL EXECUTION GATE
            if not (USER_AUTO_EXECUTION and SYSTEM_EXECUTION_ALLOWED):
                execution_status = "PAUSED (AUTO EXEC OFF)"
                SKIP_REST = True
            # 📐 MARGIN: Min = first priority. Ceiling thoda exceed ok. Block sirf jab used > Ceiling+1%
            # Exception 1: 2-leg incomplete position pe margin block mat lagao — missing leg add karna critical
            # Exception 2: Bias change — usi bias ki side position banana critical, nahi to loss; ceiling block mat lagao
            _incomplete_2leg = (decision in ("SELL CALL", "SELL PUT") and
                ((1 if short_call_pos else 0) + (1 if long_call_pos else 0) + (1 if short_put_pos else 0) + (1 if long_put_pos else 0)) == 2)
            used_pct_now = (blocked / bal * 100) if bal > 0 else 0
            # 🛑 HARD CAP: used >= (MIN * 2) → koi order mat do (min double safety)
            max_used_margin_pct = min(0.99, max(0.01, MIN_MARGIN_PCT * MAX_USED_MARGIN_MULTIPLIER))
            # Futures bias change: close wrong direction first (margin free hoga), hard cap bypass
            _fut_hard_cap_bypass = ORDER_TYPE == "futures" and bias_changed and decision in ("SELL CALL", "SELL PUT")
            if used_pct_now >= (max_used_margin_pct * 100) and not _fut_hard_cap_bypass:
                execution_status = f"PAUSED: Used margin {used_pct_now:.1f}% >= {max_used_margin_pct*100:.0f}% (safety cap = min×{MAX_USED_MARGIN_MULTIPLIER:.1f})"
                SKIP_REST = True
            margin_ceiling = (MIN_MARGIN_PCT * 100) * 1.01
            ceiling_hard = margin_ceiling + 1  # allow thoda overshoot (Ceiling+1%)
            # Bias change: allow if (1) margin balanced, ya (2) dono long legs 10-delta → sirf short leg swap, ya (3) futures mode → bias follow karna critical, ceiling rok na de
            _both_longs_10d = bool(long_call_pos and long_put_pos and has_long_10d_call and has_long_10d_put)
            _futures_bias_change = (ORDER_TYPE == "futures" and bias_changed)
            _allow_for_bias_change = bias_changed and (used_pct_now < ceiling_hard or _both_longs_10d or _futures_bias_change)
            if not _incomplete_2leg and not _allow_for_bias_change and used_pct_now >= ceiling_hard and (decision != LAST_EXECUTED_SIGNAL or auto_roll or not has_active_position):
                execution_status = f"PAUSED: Margin {used_pct_now:.1f}% >= {ceiling_hard:.1f}%"
                SKIP_REST = True
            # Step 2: BIAS/STRATEGY — same bias = no new entry (equality/margin top-up chale)
            if execution_decision == "NO TRADE":
                execution_status = "NO TRADE (Strategy)"
                SKIP_REST = True
            # 🚫 HARD BLOCK: same bias = no new orders (but run equality if drift)
            if execution_decision == LAST_EXECUTED_SIGNAL and not auto_roll:
                # ⚖️ INSTANT EQUALITY CHECK — Tier 1: short = long call = long put; Tier 2: short = one long (size-equal sabhi me)
                if ENFORCE_EQUALITY_JOB and not EQUALITY_EVENT:
                    if "SELL CALL" in execution_decision and short_call_pos and long_call_pos and long_put_pos:
                        sc, lc, lp = abs(short_call_pos["size"]), abs(long_call_pos["size"]), abs(long_put_pos["size"])
                        if not (sc == lc == lp):
                            LAST_EQUALITY_TIME = time.time()
                            EQUALITY_EVENT = True
                            EQUALITY_ARMED = True
                            EQUALITY_TRIGGER_REASON = "IMMEDIATE_DRIFT"
                    elif "SELL PUT" in execution_decision and short_put_pos and long_call_pos and long_put_pos:
                        sp, lc, lp = abs(short_put_pos["size"]), abs(long_call_pos["size"]), abs(long_put_pos["size"])
                        if not (sp == lc == lp):
                            LAST_EQUALITY_TIME = time.time()
                            EQUALITY_EVENT = True
                            EQUALITY_ARMED = True
                            EQUALITY_TRIGGER_REASON = "IMMEDIATE_DRIFT"
                    # Tier 2: short + one 10d long — size equal
                    elif "SELL CALL" in execution_decision and short_call_pos and long_put_pos and not long_call_pos:
                        sc, lp = abs(float(short_call_pos["size"])), abs(float(long_put_pos["size"]))
                        if sc != lp:
                            LAST_EQUALITY_TIME = time.time()
                            EQUALITY_EVENT = True
                            EQUALITY_ARMED = True
                            EQUALITY_TRIGGER_REASON = "IMMEDIATE_DRIFT_2LEG"
                    elif "SELL PUT" in execution_decision and short_put_pos and long_call_pos and not long_put_pos:
                        sp, lc = abs(float(short_put_pos["size"])), abs(float(long_call_pos["size"]))
                        if sp != lc:
                            LAST_EQUALITY_TIME = time.time()
                            EQUALITY_EVENT = True
                            EQUALITY_ARMED = True
                            EQUALITY_TRIGGER_REASON = "IMMEDIATE_DRIFT_2LEG"
                execution_status = "BIAS SAME (MONITORING)"
                SKIP_REST = True
                # Don't continue — fall through so equality execution can run (instant balance)
            # Step 3: BALANCE CHECK — position banane se pehle balance confirm
            if (execution_decision != LAST_EXECUTED_SIGNAL or auto_roll or not has_active_position) and execution_decision != "NO TRADE" and not SKIP_REST:
                bal, blocked, _ = wallet_usd()
                if bal <= 0:
                    execution_status = "PAUSED: Balance ≤ 0"
                    SKIP_REST = True
                elif blocked < 0:
                    execution_status = "PAUSED: Invalid margin state"
                    SKIP_REST = True
            # Step 4: POSITION — new entry / add missing / equality (only if 1–3 OK)
            # Bias change pe allow (sell=long=long). Non-bias me cooldown block
            _would_run_s4 = (execution_decision != LAST_EXECUTED_SIGNAL or auto_roll or not has_active_position) and execution_decision != "NO TRADE" and not SKIP_REST
            if _would_run_s4 and not bias_changed and time.time() < ORDER_FAILURE_COOLDOWN_UNTIL:
                execution_status = "Order fail cooldown — new order try nahi (bias change pe try karega)"
                SKIP_REST = True
            if _would_run_s4 and (bias_changed or time.time() >= ORDER_FAILURE_COOLDOWN_UNTIL):
                execution_status = "AUTO EXECUTING"
                any_order_done = False
                _is_margin_sizing = POSITION_SIZING_MODE == "margin"

                # Balance kam + 0 position → low balance par partial entry mat karo.
                _bal_s4, _blocked_s4, _ = wallet_usd()
                _free_s4 = (_bal_s4 - _blocked_s4) if _bal_s4 is not None else 0
                _low_bal_no_pos = (
                    not has_active_position
                    and execution_decision in ("SELL CALL", "SELL PUT")
                    and (
                        (_bal_s4 is not None and _bal_s4 <= LOW_BALANCE_USD)
                        or (_free_s4 <= LOW_FREE_USD)
                    )
                )

                # Sab legs SAME expiry mein: agar pehle se short leg hai to hedge usi expiry se
                opts_hedge_call = roll_opts
                if short_call_pos:
                    exp_leg = extract_expiry(short_call_pos.get("product_symbol", ""))
                    if exp_leg:
                        opts_hedge_call = [o for o in all_opts if o["expiry"] == exp_leg]
                        if not opts_hedge_call:
                            opts_hedge_call = roll_opts
                opts_hedge_put = roll_opts
                if short_put_pos:
                    exp_leg = extract_expiry(short_put_pos.get("product_symbol", ""))
                    if exp_leg:
                        opts_hedge_put = [o for o in all_opts if o["expiry"] == exp_leg]
                        if not opts_hedge_put:
                            opts_hedge_put = roll_opts

                hedge_delta = 0.10

                if "SELL CALL" in execution_decision:
                    if ORDER_TYPE == "futures":
                        # FUTURES: SHORT perpetual contract only
                        futures_created, futures_msg = create_or_manage_futures_position(SYMBOL, execution_decision, bal, spot)
                        if futures_created:
                            any_order_done = True
                            LAST_EXECUTED_SIGNAL = decision
                            LAST_EXECUTION_TIME = time.time()
                            execution_status = futures_msg
                            send_telegram(
                                f"📈 SHORT FUTURES OPENED (SELL CALL equiv.)\n"
                                f"Symbol   : {SYMBOL}\n"
                                f"Status   : {futures_msg}\n"
                                f"Balance  : ${bal:.2f}\n"
                                f"Time     : {datetime.now().strftime('%H:%M:%S')}"
                            )
                        else:
                            execution_status = f"Futures creation failed: {futures_msg}"
                    else:
                        # OPTIONS: 3-leg iron fly (short call, long call, long put)
                        # Margin mode = min margin based size. Max-loss mode = 3-leg estimated max loss <= capital ke 10%.
                        atm_call = select_atm_option(roll_opts, spot, "call")
                        put_10d  = select_delta_option(opts_hedge_call, "put", hedge_delta)
                        call_10d = select_delta_option(opts_hedge_call, "call", hedge_delta)
                        premium_call = (atm_call.get("mark_price") or 0) or ((atm_call["strike"]) * 0.005) if atm_call else 0.01
                        premium_call = max(0.01, premium_call)
                        if _is_margin_sizing:
                            size_dir = max(MIN_POSITION_CONTRACTS, calc_position_size(bal, MIN_MARGIN_PCT, premium_call))
                        else:
                            size_dir = calc_position_size_by_max_loss(bal, execution_decision, atm_call, call_10d, put_10d, MAX_LOSS_CAP_PCT)
                            _margin_safety_pct = min(0.99, max(0.01, MIN_MARGIN_PCT * MAX_USED_MARGIN_MULTIPLIER))
                            _margin_safety_size = calc_position_size(bal, _margin_safety_pct, premium_call)
                            size_dir = min(size_dir, _margin_safety_size) if size_dir > 0 else 0
                            if size_dir <= 0:
                                LAST_MAIN_LOOP_STAGE = "risk-cap"
                                execution_status = f"PAUSED: Max loss {round(MAX_LOSS_CAP_PCT * 100)}% cap me 1 set bhi fit nahi"
                                SKIP_REST = True
                                continue

                        if _low_bal_no_pos and (not atm_call or not call_10d or not put_10d):
                            execution_status = "LOW BALANCE — full 3-leg not possible, skip new position"
                            SKIP_REST = True
                            continue

                        # 1. Sell call
                        if not has_short_call and atm_call:
                            res = place_order(atm_call["symbol"], "sell", size_dir, MARGIN_MODE.lower(), reason="NEW_POSITION")
                            if res:
                                any_order_done = True
                                LAST_EXECUTED_SIGNAL = decision
                                LAST_EXECUTION_TIME = time.time()
                                time.sleep(0.5)

                        # 2. Buy call
                        if not has_any_long_call and call_10d:
                            res = place_order(call_10d["symbol"], "buy", size_dir, MARGIN_MODE.lower(), reason="NEW_POSITION")
                            if res:
                                any_order_done = True
                                LAST_EXECUTION_TIME = time.time()
                                time.sleep(0.5)

                        # 3. Buy put
                        if not has_long_10d_put and put_10d:
                            res = place_order(put_10d["symbol"], "buy", size_dir, MARGIN_MODE.lower(), reason="NEW_POSITION")
                            if res:
                                any_order_done = True
                                LAST_EXECUTION_TIME = time.time()

                        # Margin mode me hi 10% minimum top-up karo; max-loss mode me extra add mat karo.
                        if _is_margin_sizing and any_order_done and atm_call and call_10d and put_10d:
                            ensure_10pct_after_entry_measure(
                                atm_call["symbol"], "sell",
                                call_10d["symbol"], "buy",
                                put_10d["symbol"], "buy",
                                MARGIN_MODE.lower(),
                                current_size=size_dir,
                                reason="NEW_POSITION_TOPUP"
                            )

                        # 🔔 POST ENTRY — FORCE EQUALITY CHECK (ONLY if orders happened)
                        if any_order_done:
                            if EQUALITY_TRIGGER_REASON == "EXPIRY_ROLL" and not EXPIRY_ROLL_CONFIRMATION_PRINTED:
                                print("✅ Expiry roll complete — new contract me position ban gai.")
                                EXPIRY_ROLL_CONFIRMATION_PRINTED = True
                            EQUALITY_EVENT = True
                            EQUALITY_ARMED = True
                            EQUALITY_TRIGGER_REASON = "POST_ENTRY_SYNC"
                elif "SELL PUT" in execution_decision:
                    if ORDER_TYPE == "futures":
                        # FUTURES: LONG perpetual (SELL PUT = bullish bias). Bias change pe short close → long open.
                        futures_created, futures_msg = create_or_manage_futures_position(SYMBOL, execution_decision, bal, spot)
                        if futures_created:
                            any_order_done = True
                            LAST_EXECUTED_SIGNAL = decision
                            LAST_EXECUTION_TIME = time.time()
                            execution_status = futures_msg
                            send_telegram(
                                f"📉 LONG FUTURES OPENED (SELL PUT bias)\n"
                                f"Symbol   : {SYMBOL}\n"
                                f"Status   : {futures_msg}\n"
                                f"Balance  : ${bal:.2f}\n"
                                f"Time     : {datetime.now().strftime('%H:%M:%S')}"
                            )
                        else:
                            execution_status = f"Futures creation failed: {futures_msg}"
                    else:
                        # OPTIONS: 3-leg iron fly (short put, long put, long call)
                        atm_put = select_atm_option(roll_opts, spot, "put")
                        call_10d = select_delta_option(opts_hedge_put, "call", hedge_delta)
                        put_10d  = select_delta_option(opts_hedge_put, "put", hedge_delta)
                        premium_put = (atm_put.get("mark_price") or 0) or ((atm_put["strike"]) * 0.005) if atm_put else 0.01
                        premium_put = max(0.01, premium_put)
                        if _is_margin_sizing:
                            size_dir = max(MIN_POSITION_CONTRACTS, calc_position_size(bal, MIN_MARGIN_PCT, premium_put))
                        else:
                            size_dir = calc_position_size_by_max_loss(bal, execution_decision, atm_put, call_10d, put_10d, MAX_LOSS_CAP_PCT)
                            _margin_safety_pct = min(0.99, max(0.01, MIN_MARGIN_PCT * MAX_USED_MARGIN_MULTIPLIER))
                            _margin_safety_size = calc_position_size(bal, _margin_safety_pct, premium_put)
                            size_dir = min(size_dir, _margin_safety_size) if size_dir > 0 else 0
                            if size_dir <= 0:
                                LAST_MAIN_LOOP_STAGE = "risk-cap"
                                execution_status = f"PAUSED: Max loss {round(MAX_LOSS_CAP_PCT * 100)}% cap me 1 set bhi fit nahi"
                                SKIP_REST = True
                                continue

                        if _low_bal_no_pos and (not atm_put or not call_10d or not put_10d):
                            execution_status = "LOW BALANCE — full 3-leg not possible, skip new position"
                            SKIP_REST = True
                            continue

                        # 1. Sell put
                        if not has_short_put and atm_put:
                            res = place_order(atm_put["symbol"], "sell", size_dir, MARGIN_MODE.lower(), reason="NEW_POSITION")
                            if res:
                                any_order_done = True
                                LAST_EXECUTED_SIGNAL = decision
                                LAST_EXECUTION_TIME = time.time()
                                time.sleep(0.5)

                        # 2. Buy put
                        if not has_long_10d_put and put_10d:
                            res = place_order(put_10d["symbol"], "buy", size_dir, MARGIN_MODE.lower(), reason="NEW_POSITION")
                            if res:
                                any_order_done = True
                                LAST_EXECUTION_TIME = time.time()
                                time.sleep(0.5)

                        # 3. Buy call
                        if not has_any_long_call and call_10d:
                            res = place_order(call_10d["symbol"], "buy", size_dir, MARGIN_MODE.lower(), reason="NEW_POSITION")
                            if res:
                                any_order_done = True
                                LAST_EXECUTION_TIME = time.time()

                        # Margin mode me hi 10% minimum top-up karo; max-loss mode me extra add mat karo.
                        if _is_margin_sizing and any_order_done and atm_put and put_10d and call_10d:
                            ensure_10pct_after_entry_measure(
                                atm_put["symbol"], "sell",
                                put_10d["symbol"], "buy",
                                call_10d["symbol"], "buy",
                                MARGIN_MODE.lower(),
                                current_size=size_dir,
                                reason="NEW_POSITION_TOPUP"
                            )

                        # 🔔 POST ENTRY — FORCE EQUALITY CHECK
                        if any_order_done:
                            if EQUALITY_TRIGGER_REASON == "EXPIRY_ROLL" and not EXPIRY_ROLL_CONFIRMATION_PRINTED:
                                print("✅ Expiry roll complete — new contract me position ban gai.")
                                EXPIRY_ROLL_CONFIRMATION_PRINTED = True
                            EQUALITY_EVENT = True
                            EQUALITY_ARMED = True
                            EQUALITY_TRIGGER_REASON = "POST_ENTRY_SYNC" 

            # =========================================================
            # ⚖️ EQUALITY — Short = Long = Long: teeno legs ki qty hamesha same
            # Unknown leg → pehle close (wrong_or_extra). Sirf 3 valid legs, qty match (sirf ADD, kabhi square off nahi)
            # =========================================================
            positions = get_positions()
            if API_DOWN:
                _u, _hl, _hb = render_margin_heatmap(bal, blocked)
                _uc, _dr, _ds, _di, _ = calculate_margin_drift(bal, blocked, MIN_MARGIN_PCT)
                _as, _ast, _ab = render_api_health_bar()
                expiry_list = expiry_oi_list_primary if (expiry_oi_list_primary and len(expiry_oi_list_primary) > 0) else []
                expiry_list_sec = expiry_oi_list_secondary_oi or []
                render_terminal_ui(
                    now=datetime.now(), spot=spot, decision=decision, source=source,
                    bal=bal, blocked=blocked, upnl=upnl,
                    wallet_upnl_pct=wallet_upnl_pct, blocked_upnl_pct=blocked_upnl_pct,
                    used_pct_ui=_u, heat_level=_hl, heat_bar=_hb,
                    delta=delta, gamma=gamma,
                    position_details=position_details, position_count=position_count,
                    execution_status="API DOWN (no orders)",
                    used_pct_calc=_uc, margin_drift=_dr, drift_status=_ds, drift_icon=_di,
                    api_score=_as, api_status=_ast, api_bar=_ab,
                    expiry_oi_list=expiry_list, expiry_oi_list_secondary=expiry_list_sec,
                    spot_secondary=spot_secondary_oi, symbol_secondary=symbol_secondary_oi, symbol_primary=main_sym,
                    all_expiry_max_pain_primary=all_expiry_mp_primary, all_expiry_max_pain_secondary=all_expiry_mp_secondary,
                    projected_profit=None, max_loss=None,
                    position_delta_sum=None, position_gamma_sum=None, position_theta_sum=None,
                    position_vega_sum=None, position_rho_sum=None,
                    strike_order_ok=None, strike_order_msg=None, option_metrics=None,
                    last_executed_signal=LAST_EXECUTED_SIGNAL,
                    **price_change_ui_kwargs
                )
                time.sleep(REFRESH_SECONDS)
                continue
            short_call_pos = short_put_pos = None
            long_call_pos = long_put_pos = None
            for pos in positions:
                sym = pos.get("product_symbol", "")
                size = float(pos.get("size", 0))
                if sym.startswith("C-") and size < 0:
                    short_call_pos = pos
                elif sym.startswith("C-") and size > 0:
                    long_call_pos = pos
                elif sym.startswith("P-") and size < 0:
                    short_put_pos = pos
                elif sym.startswith("P-") and size > 0:
                    long_put_pos = pos
            adjustments = []
            # Auto-unsuspend equality after cooldown (10 min) — 400 pe 647x retry se bachne ke liye
            if EQUALITY_SUSPENDED and EQUALITY_SUSPENDED_UNTIL > 0 and time.time() > EQUALITY_SUSPENDED_UNTIL:
                EQUALITY_SUSPENDED = False
                EQUALITY_SUSPENDED_UNTIL = 0
            # Disbalance detect karte hi equality chalao (EQUALITY_EVENT wait mat karo)
            run_equality = (
                ENFORCE_EQUALITY_JOB
                and not EQUALITY_SUSPENDED
                and (EQUALITY_EVENT or EQUALITY_MANUAL_EXEC)
            )
            if not run_equality and ENFORCE_EQUALITY_JOB and not EQUALITY_SUSPENDED:
                # Same iteration mein disbalance check — LEG_MODE 1 (3-leg) ya 2 (2-leg) — short + 10d longs size-equal sabhi me
                if "SELL CALL" in execution_decision and short_call_pos and long_call_pos and long_put_pos:
                    sc, lc, lp = abs(float(short_call_pos["size"])), abs(float(long_call_pos["size"])), abs(float(long_put_pos["size"]))
                    if not (sc == lc == lp):
                        run_equality = True
                        EQUALITY_EVENT = True
                        EQUALITY_ARMED = True
                        EQUALITY_TRIGGER_REASON = "INSTANT_BALANCE"
                elif "SELL PUT" in execution_decision and short_put_pos and long_call_pos and long_put_pos:
                    sp, lc, lp = abs(float(short_put_pos["size"])), abs(float(long_call_pos["size"])), abs(float(long_put_pos["size"]))
                    if not (sp == lc == lp):
                        run_equality = True
                        EQUALITY_EVENT = True
                        EQUALITY_ARMED = True
                        EQUALITY_TRIGGER_REASON = "INSTANT_BALANCE"
                elif "SELL CALL" in execution_decision and short_call_pos and long_put_pos and not long_call_pos:
                    if abs(float(short_call_pos["size"])) != abs(float(long_put_pos["size"])):
                        run_equality = True
                        EQUALITY_EVENT = True
                        EQUALITY_ARMED = True
                        EQUALITY_TRIGGER_REASON = "INSTANT_BALANCE_2LEG"
                elif "SELL PUT" in execution_decision and short_put_pos and long_call_pos and not long_put_pos:
                    if abs(float(short_put_pos["size"])) != abs(float(long_call_pos["size"])):
                        run_equality = True
                        EQUALITY_EVENT = True
                        EQUALITY_ARMED = True
                        EQUALITY_TRIGGER_REASON = "INSTANT_BALANCE_2LEG"
            if run_equality and not SKIP_REST:
                if "SELL CALL" in execution_decision and short_call_pos and long_call_pos and long_put_pos:
                    adjustments = enforce_strict_equal_legs(
                        short_call_pos, long_call_pos, long_put_pos
                    )
                elif "SELL PUT" in execution_decision and short_put_pos and long_call_pos and long_put_pos:
                    adjustments = enforce_strict_equal_legs_put(
                        short_put_pos, long_call_pos, long_put_pos
                    )
                elif "SELL CALL" in execution_decision and short_call_pos and long_put_pos and not long_call_pos:
                    adjustments = enforce_strict_equal_2leg(short_call_pos, long_put_pos)
                elif "SELL PUT" in execution_decision and short_put_pos and long_call_pos and not long_put_pos:
                    adjustments = enforce_strict_equal_2leg(short_put_pos, long_call_pos)
                else:
                    execution_status = "EQUALITY PENDING (WAITING LEGS)"
                if adjustments:
                    FORCE_UI_REFRESH = True
                    EQUALITY_WAIT_COUNT += 1
                if EQUALITY_WAIT_COUNT > 5 and not adjustments:
                    EQUALITY_EVENT = False
                    EQUALITY_ARMED = False
                    EQUALITY_MANUAL_EXEC = False
                    EQUALITY_TRIGGER_REASON = "ABORTED"
                    EQUALITY_WAIT_COUNT = 0
                if EQUALITY_WAIT_COUNT > 10:
                    EQUALITY_EVENT = False
                    EQUALITY_ARMED = False
                    EQUALITY_WAIT_COUNT = 0
                if adjustments:
                    time.sleep(0.5)
                    SKIP_REST = True

            if adjustments:
                print("\n⚖️ EQUALITY JOB RUNNING")
                print(f"Trigger Reason : {EQUALITY_TRIGGER_REASON}")
                # Low balance: don't spam rejected ADD orders; wait for funds to return
                _bal_eq, _blocked_eq, _ = wallet_usd()
                _free_eq = (_bal_eq - _blocked_eq) if _bal_eq is not None else 0
                if (_bal_eq is not None and _bal_eq <= LOW_BALANCE_USD) or (_free_eq <= LOW_FREE_USD):
                    execution_status = "EQUALITY SKIPPED — LOW BALANCE"
                    EQUALITY_EVENT = False
                    EQUALITY_ARMED = False
                    EQUALITY_MANUAL_EXEC = False
                    EQUALITY_TRIGGER_REASON = "NONE"
                    adjustments = []
                    FORCE_UI_REFRESH = True
                    SKIP_REST = True
                    continue
                for adj in adjustments:
                    place_order(
                        adj["symbol"],
                        adj["side"],
                        adj["size"],
                        MARGIN_MODE.lower(),
                        reason="EQUALITY"
                    )
                send_telegram(
                    "⚖️ EQUALITY ENFORCED\n"
                    f"Reason : {EQUALITY_TRIGGER_REASON}\n"
                    f"Time   : {datetime.now().strftime('%H:%M:%S')}"
                )
                # Margin sizing mode me hi leg balance ke turant baad top-up karo; max-loss mode me extra add mat karo.
                _bal, _blocked, _ = wallet_usd()
                _used = (_blocked / _bal) if _bal > 0 else 0
                if POSITION_SIZING_MODE == "margin" and _used < MIN_MARGIN_PCT and 0.01 <= MIN_MARGIN_PCT <= 0.99:
                    if "SELL PUT" in execution_decision and short_put_pos and long_put_pos and long_call_pos:
                        _curr = max(abs(int(float(short_put_pos.get("size", 0)))), abs(int(float(long_put_pos.get("size", 0)))), abs(int(float(long_call_pos.get("size", 0)))))
                        ensure_10pct_after_entry_measure(
                            short_put_pos["product_symbol"], "sell",
                            long_put_pos["product_symbol"], "buy",
                            long_call_pos["product_symbol"], "buy",
                            MARGIN_MODE.lower(),
                            current_size=_curr,
                            reason="MARGIN_AFTER_EQUALITY"
                        )
                    elif "SELL CALL" in execution_decision and short_call_pos and long_call_pos and long_put_pos:
                        _curr = max(abs(int(float(short_call_pos.get("size", 0)))), abs(int(float(long_call_pos.get("size", 0)))), abs(int(float(long_put_pos.get("size", 0)))))
                        ensure_10pct_after_entry_measure(
                            short_call_pos["product_symbol"], "sell",
                            long_call_pos["product_symbol"], "buy",
                            long_put_pos["product_symbol"], "buy",
                            MARGIN_MODE.lower(),
                            current_size=_curr,
                            reason="MARGIN_AFTER_EQUALITY"
                        )
            if adjustments:
                EQUALITY_EVENT = False
                EQUALITY_ARMED = False
                EQUALITY_MANUAL_EXEC = False
                EQUALITY_TRIGGER_REASON = "NONE"
                EQUALITY_WAIT_COUNT = 0
                FORCE_UI_REFRESH = True
            if EQUALITY_EVENT and not adjustments:
                EQUALITY_EVENT = False
                EQUALITY_ARMED = False
                EQUALITY_MANUAL_EXEC = False
                EQUALITY_TRIGGER_REASON = "NONE"
                EQUALITY_WAIT_COUNT = 0
                FORCE_UI_REFRESH = True

            # Step 5: MARGIN — sirf position bane ya leg balance ho tab. Bina new position/equality ke kabhi margin balance NAHI.

            # ===== UI (har iteration pe dikhao — yahi pe run karo) =====
            used_pct_calc, margin_drift, drift_status, drift_icon, margin_ceiling = calculate_margin_drift(
                bal, blocked, MIN_MARGIN_PCT
            )
            used_pct_ui, heat_level, heat_bar = render_margin_heatmap(bal, blocked)
            api_score, api_status, api_bar = render_api_health_bar()
            # 10% Min = first priority. Ceiling thoda exceed → no issue. Block sirf jab bahut zyada (Ceiling+2%)
            min_pct = MIN_MARGIN_PCT * 100
            ceiling_soft = margin_ceiling + 1  # allow thoda overshoot (Ceiling+1%)
            EXECUTION_ALLOWED = (used_pct_calc < min_pct) or (used_pct_calc <= ceiling_soft)
            if not EXECUTION_ALLOWED:
                execution_status = f"PAUSED: Margin {used_pct_calc:.1f}% > {ceiling_soft:.1f}% (Ceiling+1%)"
            CAN_EXECUTE = (
                USER_AUTO_EXECUTION
                and not API_DOWN
                and not KILL_SWITCH
            )
            expiry_oi_list = []
            for exp in sorted(set(o["expiry"] for o in opts)):
                opts_exp = [o for o in opts if o["expiry"] == exp]
                c, p, pr = oi_pcr(opts_exp)
                mp = max_pain(opts_exp)
                expiry_oi_list.append((exp, c, p, pr, mp))
            all_mp_from_opts = max_pain(opts)
            # Main/Secondary OI (jis ka total OI zyada = Main, set at start of loop)
            spot_secondary = spot_secondary_oi
            oi_secondary = expiry_oi_list_secondary_oi
            symbol_secondary = symbol_secondary_oi
            _oi_primary = expiry_oi_list_primary if (expiry_oi_list_primary is not None and len(expiry_oi_list_primary or []) > 0) else expiry_oi_list
            _all_mp_primary = all_expiry_mp_primary if (expiry_oi_list_primary is not None and len(expiry_oi_list_primary or []) > 0) else all_mp_from_opts
            _spot_primary = spot_primary_display if spot_primary_display is not None else spot
            projected_profit = calculate_projected_profit(positions_all, spot_btc, spot_eth)
            max_loss = calculate_max_loss(positions_all, spot_btc, spot_eth)
            # Futures mode: 10% up/down projection (options functions skip futures positions)
            if ORDER_TYPE == "futures":
                _fp = next((p for p in positions_all
                            if not str(p.get("product_symbol","")).startswith(("C-","P-"))
                            and float(p.get("size", 0)) != 0), None)
                if _fp:
                    _fsize = float(_fp.get("size", 0))
                    _fentry = float(_fp.get("entry_price") or _fp.get("average_price") or 0)
                    _fspot = spot  # current mark proxy
                    if _fentry > 0 and _fspot > 0:
                        _abs = abs(_fsize)
                        if _fsize < 0:  # SHORT: profit = price down, loss = price up
                            projected_profit = round((_fentry - _fspot * 0.9) * _abs * 0.001, 2)
                            max_loss        = round((_fspot * 1.1 - _fentry) * _abs * 0.001, 2)
                        else:           # LONG: profit = price up, loss = price down
                            projected_profit = round((_fspot * 1.1 - _fentry) * _abs * 0.001, 2)
                            max_loss        = round((_fentry - _fspot * 0.9) * _abs * 0.001, 2)
            opts_all = list(opts) if opts else []
            for sym in ["BTC", "ETH"]:
                if sym != SYMBOL and any(
                    (p.get("product_symbol") or "").startswith(("C-" + sym + "-", "P-" + sym + "-"))
                    and float(p.get("size", 0)) != 0
                    for p in positions_all
                ):
                    opts_all.extend(fetch_options_for_symbol(sym) or [])
            pos_delta, pos_gamma, pos_theta, pos_vega, pos_rho = portfolio_greeks_sum(positions_all, opts_all)
            strike_order_ok = None
            strike_order_msg = ""
            if execution_decision == "SELL CALL" and short_call_pos and long_call_pos and long_put_pos:
                strike_order_ok, strike_order_msg = validate_three_leg_strike_order(execution_decision, short_call_pos, long_put_pos, long_call_pos)
            elif execution_decision == "SELL PUT" and short_put_pos and long_call_pos and long_put_pos:
                strike_order_ok, strike_order_msg = validate_three_leg_strike_order(execution_decision, short_put_pos, long_put_pos, long_call_pos)
            # Strike order sirf verify ke liye — match na ho to sabhi legs close karo, next cycle naya entry as per bias
            if strike_order_ok is False:
                legs_to_close = []
                if decision == "SELL CALL" and short_call_pos and long_call_pos and long_put_pos:
                    legs_to_close = [short_call_pos, long_call_pos, long_put_pos]
                elif decision == "SELL PUT" and short_put_pos and long_call_pos and long_put_pos:
                    legs_to_close = [short_put_pos, long_put_pos, long_call_pos]
                if legs_to_close:
                    for pos in legs_to_close:
                        sym = pos.get("product_symbol", "")
                        size = float(pos.get("size", 0))
                        if sym and size != 0:
                            square_off_position(sym, size, reason="STRIKE_ORDER_MISMATCH")
                            time.sleep(0.5)
                    send_telegram(
                        f"⚠️ STRIKE ORDER MISMATCH — ALL LEGS CLOSED\n"
                        f"Expected: {strike_order_msg}\n"
                        f"Position will be re-created as per current bias on next cycle.\n"
                        f"Time: {datetime.now().strftime('%H:%M:%S')}"
                    )
                    LAST_EXECUTED_SIGNAL = None
                    SKIP_REST = True
                    render_terminal_ui(
                        now=datetime.now(), spot=_spot_primary, decision=decision, source=source,
                        bal=bal, blocked=blocked, upnl=upnl,
                        projected_profit=projected_profit, max_loss=max_loss,
                        wallet_upnl_pct=wallet_upnl_pct, blocked_upnl_pct=blocked_upnl_pct,
                        used_pct_ui=used_pct_ui, heat_level=heat_level, heat_bar=heat_bar,
                        delta=delta, gamma=gamma,
                        position_details=position_details, position_count=position_count,
                        execution_status="Strike order mismatch — legs closed, will re-create next cycle",
                        used_pct_calc=used_pct_calc, margin_drift=margin_drift, drift_status=drift_status, drift_icon=drift_icon,
                        api_score=api_score, api_status=api_status, api_bar=api_bar,
                        expiry_oi_list=_oi_primary, expiry_oi_list_secondary=oi_secondary,
                        spot_secondary=spot_secondary, symbol_secondary=symbol_secondary, symbol_primary=main_sym,
                        all_expiry_max_pain_primary=_all_mp_primary, all_expiry_max_pain_secondary=all_expiry_mp_secondary,
                        position_delta_sum=pos_delta, position_gamma_sum=pos_gamma,
                        position_theta_sum=pos_theta, position_vega_sum=pos_vega, position_rho_sum=pos_rho,
                        strike_order_ok=strike_order_ok, strike_order_msg=strike_order_msg, option_metrics=None,
                        last_executed_signal=LAST_EXECUTED_SIGNAL,
                        **price_change_ui_kwargs
                    )
                    time.sleep(REFRESH_SECONDS)
                    continue
            # Ref option: futures mode → ATM option by direction; options → combined; no position → ATM call
            option_metrics = None
            _fut_det = next((p for p in position_details if p.get("is_futures")), None) if position_details else None
            if ORDER_TYPE == "futures" and _fut_det:
                # SHORT futures → ATM CALL reference; LONG futures → ATM PUT reference
                # dir_letter use karo — size always abs() hota hai position_details mein
                _is_short = _fut_det.get("dir_letter", "S") == "S"
                _atm_side = "call" if _is_short else "put"
                _atm_opt = None
                if opts:
                    _today = datetime.now().date()
                    # 0 DTE (aaj expiry) pehle; nahi mila to nearest expiry fallback
                    _zero_dte = [o for o in opts if (o["expiry"].date() if hasattr(o["expiry"], "date") else o["expiry"]) == _today]
                    if _zero_dte:
                        _pool = _zero_dte
                        _dte_label = "0 DTE"
                    else:
                        _expiries = sorted(set(o["expiry"] for o in opts))
                        _pool = [o for o in opts if o["expiry"] == _expiries[0]] if _expiries else []
                        _dte_label = "near"
                    _atm_opt = select_atm_option(_pool, _spot_primary, _atm_side) if _pool else None
                if _atm_opt:
                    option_metrics = get_option_metrics(_atm_opt, _spot_primary)
                    if option_metrics:
                        _dir_label = "Short" if _is_short else "Long"
                        option_metrics["ref_label"] = f"ATM {_atm_side.upper()} [{_dte_label}] ({_dir_label} Futures)"
                        option_metrics["ref_contract"] = _atm_opt.get("symbol", "")
                    # Greeks from ATM option as reference (unsigned — just market context)
                    pos_delta = float(_atm_opt.get("delta", 0) or 0)
                    pos_gamma = float(_atm_opt.get("gamma", 0) or 0)
                    pos_theta = float(_atm_opt.get("theta", 0) or 0)
                    pos_vega  = float(_atm_opt.get("vega", 0) or 0)
                    pos_rho   = float(_atm_opt.get("rho", 0) or 0)
            elif position_details and len(position_details) >= 1:
                option_metrics = {"is_combined": True}
                pt, pi, ptv, piv = portfolio_theor_intrinsic_tv_iv(positions_all, opts_all, _spot_primary)
                option_metrics["combined_theor"] = pt
                option_metrics["combined_intrinsic"] = pi
                option_metrics["combined_time_value"] = ptv
                option_metrics["combined_iv_pct"] = piv
            else:
                ref_opt = None
                if opts:
                    expiries = sorted(set(o["expiry"] for o in opts))
                    near = [o for o in opts if o["expiry"] == expiries[0]] if expiries else []
                    ref_opt = select_atm_option(near, _spot_primary, "call") if near else None
                if ref_opt:
                    option_metrics = get_option_metrics(ref_opt, _spot_primary)
                    if option_metrics:
                        option_metrics["ref_label"] = "ATM near call (koi position nahi)"
                        option_metrics["ref_contract"] = ref_opt.get("symbol", "")
            render_terminal_ui(
                # Live full render complete
                now=datetime.now(),
                spot=_spot_primary,
                decision=decision,
                source=source,
                bal=bal,
                blocked=blocked,
                upnl=upnl,
                projected_profit=projected_profit,
                max_loss=max_loss,
                wallet_upnl_pct=wallet_upnl_pct,
                blocked_upnl_pct=blocked_upnl_pct,
                used_pct_ui=used_pct_ui,
                heat_level=heat_level,
                heat_bar=heat_bar,
                delta=delta,
                gamma=gamma,
                position_details=position_details,
                position_count=position_count,
                execution_status=execution_status,
                used_pct_calc=used_pct_calc,
                margin_drift=margin_drift,
                drift_status=drift_status,
                drift_icon=drift_icon,
                api_score=api_score,
                api_status=api_status,
                api_bar=api_bar,
                expiry_oi_list=_oi_primary,
                expiry_oi_list_secondary=oi_secondary,
                spot_secondary=spot_secondary,
                symbol_secondary=symbol_secondary,
                symbol_primary=main_sym,
                all_expiry_max_pain_primary=_all_mp_primary,
                all_expiry_max_pain_secondary=all_expiry_mp_secondary,
                position_delta_sum=pos_delta,
                position_gamma_sum=pos_gamma,
                position_theta_sum=pos_theta,
                position_vega_sum=pos_vega,
                position_rho_sum=pos_rho,
                strike_order_ok=strike_order_ok,
                strike_order_msg=strike_order_msg,
                option_metrics=option_metrics,
                last_executed_signal=LAST_EXECUTED_SIGNAL,
                **price_change_ui_kwargs
            )
            FIRST_UI_RENDER_DONE = True  # Ab UI load ho chuka — agle cycle se order place
            LAST_MAIN_LOOP_STAGE = "sleep"
            time.sleep(REFRESH_SECONDS)

    except Exception as e:
        print(f"\n❌ ERROR: {e}", flush=True)
        send_telegram(
            f"💥 RUNTIME ERROR — AUTO-RESTART IN 10s\n"
            f"Error : {str(e)[:300]}\n"
            f"Time  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        time.sleep(10)
        if KILL_SWITCH:
            break
        print("🔄 Restarting main loop...", flush=True)













