# -*- coding: utf-8 -*-
"""Хранилище: папка данных, конфигурация (JSON), секреты (DPAPI), SQLite, журнал, аудит."""

import base64
import json
import os
import secrets as pysecrets
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from . import APP_ID

IS_WIN = sys.platform.startswith("win")
_DATA_OVERRIDE = None
_LOCK = threading.RLock()
_LOCAL = threading.local()


def set_data_dir(path):
    global _DATA_OVERRIDE
    _DATA_OVERRIDE = os.path.abspath(path) if path else None


def data_dir():
    d = _DATA_OVERRIDE or os.environ.get("HKCL_DATA")
    if not d:
        if IS_WIN:
            d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_ID)
        else:
            d = os.path.join(os.path.expanduser("~"), ".hkcl")
    os.makedirs(d, exist_ok=True)
    return d


def path(*parts):
    p = os.path.join(data_dir(), *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix=""):
    return prefix + uuid.uuid4().hex[:12]


# ------------------------------------------------------------------ log

def log(msg, level="INFO"):
    line = "[%s] %s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    try:
        lp = path("logs", "hkcl.log")
        if os.path.exists(lp) and os.path.getsize(lp) > 2_000_000:
            os.replace(lp, lp + ".1")
        with open(lp, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ json files

def _read_json(name, default):
    p = path(name)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log("Ошибка чтения %s: %s — файл сохранён как .broken" % (name, e), "ERROR")
        try:
            os.replace(p, p + ".broken")
        except Exception:
            pass
        return default


def _write_json(name, obj):
    p = path(name)
    tmp = p + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    for i in range(20):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:  # Windows: файл временно занят другим процессом
            time.sleep(0.1)
    os.replace(tmp, p)


DEFAULT_CONFIG = {
    "feeds": [
        {"id": "fa", "name": "Court of Final Appeal (CFA)",
         "url": "https://legalref.judiciary.hk/lrs/common/rss/newjudgments.fa.xml",
         "enabled": True, "robots_override": False},
    ],
    "profiles": [
        {"id": "p_sanctions", "name": "Санкции / экспортный контроль", "enabled": True,
         "feeds": [], "all_words": [],
         "any_words": ["sanction", "United Nations Sanctions", "Cap. 537", "OFAC",
                       "export control", "strategic commodities", "Cap. 60", "制裁", "戰略物品"],
         "exclude_words": [], "case_regex": "", "email": True},
    ],
    "smtp": {"host": "smtp.gmail.com", "port": 465, "security": "ssl", "user": "", "from": "", "to": []},
    "custom_sources": [],
    "extra_feed_hosts": [],
    "settings": {"retention_days": 730, "min_interval_min": 10, "proxy": "", "cafile": "",
                 "minimize_to_tray": True, "start_minimized": False, "check_updates": True},
    "automation": {"enabled": True, "paused": False, "mode": "daily", "interval_hours": 6,
                   "times": ["08:00"], "weekdays": [1, 2, 3, 4, 5, 6, 7],
                   "check_feeds": True, "send_digest": False, "case_alerts": True,
                   "reminders": True, "notify": True, "llm_summarize_new_docs": False,
                   "windows_task": False, "autostart": False},
    "agent": {"enabled": True, "permissions": "full", "allow_robots_override": True,
              "webhooks": []},
    "llm": {"enabled": False, "provider": "anthropic", "model": "claude-sonnet-5-5",
            "base_url": "", "max_tokens": 2000, "temperature": 0.2, "timeout": 120,
            "consent": False, "digest_summary": False},
}


def _merge_defaults(cfg, defaults):
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = json.loads(json.dumps(v))
        elif isinstance(v, dict) and isinstance(cfg[k], dict):
            _merge_defaults(cfg[k], v)
    return cfg


def get_config():
    with _LOCK:
        cfg = _read_json("config.json", None)
        if not cfg:
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            _write_json("config.json", cfg)
        return _merge_defaults(cfg, DEFAULT_CONFIG)


def save_config(cfg):
    with _LOCK:
        _write_json("config.json", cfg)


def update_config(fn):
    """Атомарное изменение: fn(cfg) меняет словарь на месте."""
    with _LOCK:
        cfg = get_config()
        fn(cfg)
        _write_json("config.json", cfg)
        return cfg


# ------------------------------------------------------------------ secrets (DPAPI)

def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI error %d" % kernel32.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _enc(value):
    raw = (value or "").encode("utf-8")
    if IS_WIN:
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode()
        except Exception as e:
            log("DPAPI недоступен: %s" % e, "WARN")
    return "b64:" + base64.b64encode(raw).decode()


def _dec(value):
    if not value:
        return ""
    try:
        if value.startswith("dpapi:"):
            return _dpapi(base64.b64decode(value[6:]), False).decode("utf-8")
        if value.startswith("b64:"):
            return base64.b64decode(value[4:]).decode("utf-8")
    except Exception as e:
        log("Не удалось расшифровать секрет: %s" % e, "ERROR")
    return ""


def get_secret(name):
    with _LOCK:
        return _dec(_read_json("secrets.json", {}).get(name, ""))


def set_secret(name, value):
    with _LOCK:
        s = _read_json("secrets.json", {})
        if value:
            s[name] = _enc(value)
        else:
            s.pop(name, None)
        _write_json("secrets.json", s)


def has_secret(name):
    return bool(_read_json("secrets.json", {}).get(name))


def api_token(rotate=False):
    tok = "" if rotate else get_secret("api_token")
    if not tok:
        tok = "hkcl_" + pysecrets.token_urlsafe(32)
        set_secret("api_token", tok)
    return tok


# ------------------------------------------------------------------ sqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, feed_id TEXT, feed_name TEXT, guid TEXT, title TEXT, link TEXT,
  description TEXT, pub TEXT, pub_raw TEXT, first_seen TEXT,
  ncn TEXT DEFAULT '[]', actions TEXT DEFAULT '[]', matches TEXT DEFAULT '{}', sent TEXT DEFAULT '[]');
CREATE INDEX IF NOT EXISTS ix_items_seen ON items(first_seen);
CREATE TABLE IF NOT EXISTS feed_state(feed_id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS cases(
  id TEXT PRIMARY KEY, title TEXT, numbers TEXT DEFAULT '[]', ncn TEXT DEFAULT '[]',
  parties TEXT DEFAULT '[]', keywords TEXT DEFAULT '[]', court TEXT, status TEXT DEFAULT 'active',
  priority TEXT DEFAULT 'normal', notes TEXT, remind_at TEXT, tags TEXT DEFAULT '[]',
  created_at TEXT, updated_at TEXT, last_event_at TEXT, created_by TEXT);
CREATE TABLE IF NOT EXISTS case_events(
  id TEXT PRIMARY KEY, case_id TEXT, type TEXT, date TEXT, title TEXT, details TEXT, link TEXT,
  item_id TEXT, doc_id TEXT, actor TEXT, created_at TEXT, seen INTEGER DEFAULT 0, notified INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_ev_case ON case_events(case_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ev_item ON case_events(case_id, item_id) WHERE item_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_ev_doc ON case_events(case_id, doc_id) WHERE doc_id IS NOT NULL AND type='document';
CREATE TABLE IF NOT EXISTS documents(
  id TEXT PRIMARY KEY, name TEXT, size INTEGER, path TEXT, text TEXT, meta TEXT DEFAULT '{}',
  added_at TEXT, actor TEXT, llm TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, details TEXT, ok INTEGER);
CREATE TABLE IF NOT EXISTS llm_runs(
  id TEXT PRIMARY KEY, ts TEXT, task TEXT, target TEXT, provider TEXT, model TEXT,
  input_chars INTEGER, output TEXT, ok INTEGER, error TEXT, actor TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def db():
    con = getattr(_LOCAL, "con", None)
    if con is None:
        con = sqlite3.connect(path("hkcl.sqlite3"), timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript(SCHEMA)
        _LOCAL.con = con
    return con


def q(sql, args=(), one=False):
    cur = db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    con = db()
    with con:
        return con.execute(sql, args)


def kv_get(k, default=None):
    r = q("SELECT v FROM kv WHERE k=?", (k,), one=True)
    return json.loads(r["v"]) if r else default


def kv_set(k, v):
    x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v, ensure_ascii=False)))


def row(r, json_fields=()):
    if r is None:
        return None
    d = dict(r)
    for f in json_fields:
        if f in d and isinstance(d[f], str):
            try:
                d[f] = json.loads(d[f])
            except Exception:
                pass
    return d


def audit(actor, action, details=None, ok=True):
    try:
        x("INSERT INTO audit(ts,actor,action,details,ok) VALUES(?,?,?,?,?)",
          (now_iso(), actor, action, json.dumps(details or {}, ensure_ascii=False)[:4000], 1 if ok else 0))
    except Exception as e:
        log("audit: %s" % e, "ERROR")


def runtime_write(info):
    _write_json("runtime.json", info)


def runtime_read():
    return _read_json("runtime.json", {})
