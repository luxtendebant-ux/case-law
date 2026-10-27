#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HK Case Law Monitor v.1.0.2 — локальный сервер.

Назначение: частный некоммерческий мониторинг новых решений судов Гонконга
по официальным RSS-лентам Legal Reference System (Судебная власть САР Гонконг),
фильтрация по профилям поиска и e-mail-рассылка.

Принципы:
  * только стандартная библиотека Python 3.8+;
  * читаются только RSS-ленты с разрешённых хостов (legalref.judiciary.hk, hkcfa.hk,
    judiciary.hk); тексты решений не скачиваются, сайты не обходятся;
  * HKLII не опрашивается (политика HKLII запрещает автоматических агентов);
  * перед опросом ленты проверяется robots.txt; если он запрещает путь, лента
    опрашивается только после явного решения оператора (флажок в интерфейсе);
  * не чаще одного запроса к ленте в N минут (по умолчанию 10), с ETag/If-Modified-Since.

Запуск:  python3 hk_caselaw_server.py            (http://127.0.0.1:8767)
Флаги:   --port 8767 --host 127.0.0.1 --auto-check 6 --proxy http://host:port
         --allow-origin https://example.github.io --data ./hk_caselaw_data
"""

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import threading
import time
import uuid
import html as htmlmod
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime, formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlreq, error as urlerr
from urllib.parse import urlparse, parse_qs
from urllib.robotparser import RobotFileParser

VERSION = "1.0.2"
USER_AGENT = "HKCaseLawMonitor/1.0 (private non-commercial RSS reader)"
ALLOWED_FEED_HOSTS = {"legalref.judiciary.hk", "www.hkcfa.hk", "www.judiciary.hk"}
DEFAULT_ORIGINS = {"https://luxtendebant-ux.github.io"}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOCK = threading.RLock()
STATE = {"last_check": None, "last_check_summary": None, "auto_check_hours": 0,
         "next_check": None, "started": None}
ARGS = None

# ----------------------------------------------------------------- storage

def data_dir():
    d = os.path.abspath(ARGS.data) if ARGS and ARGS.data else os.path.join(BASE_DIR, "hk_caselaw_data")
    os.makedirs(d, exist_ok=True)
    return d


def _path(name):
    return os.path.join(data_dir(), name)


def load_json(name, default):
    try:
        with open(_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:  # повреждённый файл — сохраняем копию и начинаем заново
        log("Ошибка чтения %s: %s" % (name, e))
        try:
            os.replace(_path(name), _path(name + ".broken"))
        except Exception:
            pass
        return default


def save_json(name, obj, private=False):
    tmp = _path(name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    if private:
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
    os.replace(tmp, _path(name))


DEFAULT_CONFIG = {
    "feeds": [
        {"id": "fa", "name": "Court of Final Appeal (CFA)",
         "url": "https://legalref.judiciary.hk/lrs/common/rss/newjudgments.fa.xml",
         "enabled": True, "robots_override": False},
    ],
    "profiles": [
        {"id": "p_sanctions", "name": "Санкции / экспортный контроль", "enabled": True,
         "feeds": [], "all_words": [],
         "any_words": ["sanction", "United Nations Sanctions", "Cap. 537", "Cap 537",
                       "OFAC", "export control", "strategic commodities", "Cap. 60",
                       "制裁", "戰略物品"],
         "exclude_words": [], "case_regex": "", "email": True},
    ],
    "smtp": {"host": "smtp.gmail.com", "port": 465, "security": "ssl", "user": "",
             "from": "", "to": []},
    "custom_sources": [],
    "extra_feed_hosts": [],
    "settings": {"auto_check_hours": 0, "auto_email": False, "retention_days": 365,
                 "min_interval_min": 10},
}


def get_config():
    cfg = load_json("config.json", None)
    if not cfg:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        save_json("config.json", cfg, private=True)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, json.loads(json.dumps(v)))
    for k, v in DEFAULT_CONFIG["settings"].items():
        cfg["settings"].setdefault(k, v)
    return cfg


def get_secret():
    return load_json("secret.json", {})


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(_path("server.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ----------------------------------------------------------------- network

def opener():
    handlers = []
    if ARGS and ARGS.proxy:
        handlers.append(urlreq.ProxyHandler({"http": ARGS.proxy, "https": ARGS.proxy}))
    ctx = ssl.create_default_context(cafile=ARGS.cafile if ARGS and ARGS.cafile else None)
    handlers.append(urlreq.HTTPSHandler(context=ctx))
    return urlreq.build_opener(*handlers)


def http_get(url, headers=None, timeout=30):
    h = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
    if headers:
        h.update(headers)
    req = urlreq.Request(url, headers=h)
    try:
        with opener().open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urlerr.HTTPError as e:
        return e.code, dict(e.headers or {}), b""


def clean_hosts(v):
    out = []
    for h in (v or []):
        h = str(h).strip().lower()
        h = re.sub(r"^https?://", "", h).split("/")[0].split(":")[0]
        if h and "hklii" not in h and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", h) and h not in out:
            out.append(h)
    return out


def feed_url_ok(url, extra=None):
    hosts = set(ALLOWED_FEED_HOSTS) | set(clean_hosts(extra if extra is not None else get_config().get("extra_feed_hosts")))
    try:
        u = urlparse(url)
    except Exception:
        return False, "некорректный URL"
    if u.scheme != "https" and not (u.hostname in hosts and u.hostname in ("127.0.0.1", "localhost")):
        return False, "допускается только https"
    if "hklii" in (u.hostname or ""):
        return False, "HKLII запрещает автоматический доступ"
    if u.hostname not in hosts:
        return False, "хост не в списке разрешённых: " + ", ".join(sorted(hosts))
    if "hklii" in (u.hostname or ""):
        return False, "HKLII запрещает автоматический доступ"
    return True, ""


ROBOTS_CACHE = {}


def robots_status(url):
    """allowed | disallowed | unknown — по правилам RFC 9309 для нашего User-Agent."""
    u = urlparse(url)
    base = "%s://%s" % (u.scheme, u.netloc)
    now = time.time()
    c = ROBOTS_CACHE.get(base)
    if not c or now - c["t"] > 86400:
        rp = RobotFileParser()
        note = ""
        try:
            code, _, body = http_get(base + "/robots.txt", {"Accept": "text/plain"}, timeout=20)
            if code in (401, 403):
                rp.disallow_all = True
                note = "robots.txt: HTTP %d (трактуется как запрет)" % code
            elif 400 <= code < 500:
                rp.allow_all = True
                note = "robots.txt отсутствует (HTTP %d)" % code
            elif code >= 500:
                rp.disallow_all = True
                note = "robots.txt: HTTP %d (трактуется как запрет)" % code
            else:
                rp.parse(body.decode("utf-8", "replace").splitlines())
                note = "robots.txt получен"
            c = {"t": now, "rp": rp, "note": note, "ok": True}
        except Exception as e:
            c = {"t": now, "rp": None, "note": "robots.txt недоступен: %s" % e, "ok": False}
        ROBOTS_CACHE[base] = c
    if not c["ok"]:
        return "unknown", c["note"]
    allowed = c["rp"].can_fetch(USER_AGENT, url)
    return ("allowed" if allowed else "disallowed"), c["note"]

# ----------------------------------------------------------------- parsing

NCN_RE = re.compile(r"\[(19|20)\d{2}\]\s*HK(?:CFAR|CFA|CA|CFI|DC|FC|LdT|CT|CrC|OAT|LaT|MagC|LRD|C)\s*\d{1,5}", re.I)
ACTION_RE = re.compile(r"\b(?:FA|CA|HC|DC|FC|LD|KC|ES|KT|WK|FL|TM|ST|CT|LB|SC|OAT|MC)[A-Z]{1,4}\s?\d{1,6}[A-Z]?\s?/\s?(?:19|20)\d{2}\b")
ACTION_NO_RE = re.compile(r"\b((?:FA|CA|HC|DC|FC|LD|KC|ES|KT|WK|FL|TM|ST|CT|LB|SC|OAT|MC)[A-Z]{1,4})\s+No\.?\s*(\d{1,6}[A-Z]?)\s+of\s+((?:19|20)\d{2})\b", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def find_actions(text):
    a = set(re.sub(r"\s+", "", m.group(0)) for m in ACTION_RE.finditer(text))
    a |= set("%s%s/%s" % (m.group(1).upper(), m.group(2).upper(), m.group(3)) for m in ACTION_NO_RE.finditer(text))
    return sorted(a)


def strip_html(s):
    s = TAG_RE.sub(" ", s or "")
    s = htmlmod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child_text(el, names):
    for ch in el:
        if _local(ch.tag) in names:
            if _local(ch.tag) == "link" and ch.get("href"):
                return ch.get("href")
            return "".join(ch.itertext()).strip()
    return ""


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone(timedelta(hours=8)))
        return d.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y"):
        try:
            d = datetime.strptime(s.strip(), fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone(timedelta(hours=8)))
            return d.astimezone(timezone.utc).isoformat()
        except Exception:
            continue
    return None


def parse_feed(body):
    root = ET.fromstring(body)
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = strip_html(_child_text(el, {"title"}))
        link = _child_text(el, {"link"}).strip()
        desc = strip_html(_child_text(el, {"description", "summary", "content"}))
        pub = _child_text(el, {"pubdate", "published", "updated", "date"})
        guid = _child_text(el, {"guid", "id"}) or link or (title + "|" + pub)
        blob = " ".join([title, desc])
        items.append({
            "guid": guid, "title": title, "link": link, "description": desc[:2000],
            "pub_raw": pub, "pub": parse_date(pub),
            "ncn": sorted(set(m.group(0) for m in NCN_RE.finditer(blob))),
            "actions": find_actions(blob),
        })
    return items

# ----------------------------------------------------------------- matching

def _norm_list(v):
    if isinstance(v, str):
        v = [x for x in re.split(r"[\n;,]", v)]
    return [x.strip() for x in (v or []) if x and x.strip()]


def match_profile(p, item):
    if not p.get("enabled", True):
        return None
    feeds = p.get("feeds") or []
    if feeds and item.get("feed_id") not in feeds:
        return None
    text = " ".join([item.get("title", ""), item.get("description", ""),
                     " ".join(item.get("ncn", [])), " ".join(item.get("actions", []))]).lower()
    hits = []
    for w in _norm_list(p.get("exclude_words")):
        if w.lower() in text:
            return None
    all_w = _norm_list(p.get("all_words"))
    for w in all_w:
        if w.lower() not in text:
            return None
        hits.append(w)
    any_w = _norm_list(p.get("any_words"))
    if any_w:
        found = [w for w in any_w if w.lower() in text]
        if not found:
            return None
        hits.extend(found)
    rx = (p.get("case_regex") or "").strip()
    if rx:
        try:
            m = re.search(rx, text, re.I)
        except re.error:
            return None
        if not m:
            return None
        hits.append(m.group(0))
    if not all_w and not any_w and not rx:
        hits.append("(все решения ленты)")
    return hits

# ----------------------------------------------------------------- core

def check_feeds(force=False):
    cfg = get_config()
    st = cfg["settings"]
    min_iv = max(5, int(st.get("min_interval_min", 10))) * 60
    db = load_json("items.json", {"items": {}, "feeds": {}})
    summary = {"time": datetime.now(timezone.utc).isoformat(), "feeds": [], "new": 0, "new_matches": 0}
    for f in cfg["feeds"]:
        fid = f.get("id")
        fs = db["feeds"].setdefault(fid, {})
        res = {"id": fid, "name": f.get("name"), "status": "", "new": 0}
        ok, why = feed_url_ok(f.get("url", ""), cfg.get("extra_feed_hosts"))
        if not f.get("enabled", True):
            res["status"] = "отключена"
        elif not ok:
            res["status"] = "отклонено: " + why
        else:
            rs, note = robots_status(f["url"])
            fs["robots"], fs["robots_note"] = rs, note
            if rs != "allowed" and not f.get("robots_override"):
                res["status"] = "не опрошена: robots.txt = %s; требуется решение оператора" % rs
            elif fs.get("last_fetch_ts") and time.time() - fs["last_fetch_ts"] < min_iv and not force:
                res["status"] = "пропуск: интервал %d мин ещё не истёк" % (min_iv // 60)
            else:
                hdr = {}
                if fs.get("etag"):
                    hdr["If-None-Match"] = fs["etag"]
                if fs.get("last_modified"):
                    hdr["If-Modified-Since"] = fs["last_modified"]
                try:
                    code, headers, body = http_get(f["url"], hdr)
                    fs["last_fetch_ts"] = time.time()
                    fs["last_fetch"] = datetime.now(timezone.utc).isoformat()
                    fs["http"] = code
                    if code == 304:
                        res["status"] = "без изменений (304)"
                    elif code != 200:
                        res["status"] = "HTTP %d" % code
                    else:
                        hl = {k.lower(): v for k, v in headers.items()}
                        fs["etag"] = hl.get("etag", "")
                        fs["last_modified"] = hl.get("last-modified", "")
                        parsed = parse_feed(body)
                        fs["count_last"] = len(parsed)
                        for it in parsed:
                            key = fid + "::" + it["guid"]
                            if key in db["items"]:
                                continue
                            it.update({"id": key, "feed_id": fid, "feed_name": f.get("name"),
                                       "first_seen": datetime.now(timezone.utc).isoformat(),
                                       "sent": []})
                            db["items"][key] = it
                            res["new"] += 1
                        res["status"] = "OK: элементов в ленте %d, новых %d" % (len(parsed), res["new"])
                except ET.ParseError as e:
                    res["status"] = "ошибка разбора XML: %s" % e
                except Exception as e:
                    res["status"] = "ошибка: %s" % e
        fs["last_status"] = res["status"]
        summary["feeds"].append(res)
        summary["new"] += res["new"]
    # срок хранения
    keep = int(st.get("retention_days", 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep)).isoformat()
    for k in [k for k, v in db["items"].items() if v.get("first_seen", "") < cutoff]:
        del db["items"][k]
    # пересчёт совпадений
    new_matches = 0
    for it in db["items"].values():
        m = {}
        for p in cfg["profiles"]:
            h = match_profile(p, it)
            if h is not None:
                m[p["id"]] = h
        if m and not it.get("matches") and it.get("first_seen", "") >= summary["time"][:19]:
            new_matches += 1
        it["matches"] = m
    summary["new_matches"] = new_matches
    with LOCK:
        save_json("items.json", db)
        STATE["last_check"] = summary["time"]
        STATE["last_check_summary"] = summary
    log("Проверка лент: новых %d, совпадений среди новых %d" % (summary["new"], new_matches))
    return summary


def build_digest(cfg, db, profile_ids=None, only_unsent=True):
    out = {}
    for p in cfg["profiles"]:
        if profile_ids is not None and p["id"] not in profile_ids:
            continue
        if profile_ids is None and not p.get("email"):
            continue
        rows = [it for it in db["items"].values()
                if p["id"] in (it.get("matches") or {})
                and (not only_unsent or p["id"] not in (it.get("sent") or []))]
        rows.sort(key=lambda x: x.get("pub") or x.get("first_seen") or "", reverse=True)
        if rows:
            out[p["id"]] = (p, rows)
    return out


def plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def ru_date(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(timezone(timedelta(hours=8))).strftime("%d.%m.%Y")
    except Exception:
        return (iso or "")[:10]


def esc(s):
    return htmlmod.escape(s or "")


def send_mail(subject, text, html_body):
    cfg = get_config()
    sm = cfg["smtp"]
    pw = get_secret().get("smtp_password", "")
    to = _norm_list(sm.get("to"))
    if not sm.get("host") or not to:
        raise RuntimeError("SMTP не настроен: укажите сервер и получателей")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sm.get("from") or sm.get("user")
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    port = int(sm.get("port") or 465)
    sec = sm.get("security", "ssl")
    ctx = ssl.create_default_context()
    if sec == "ssl":
        s = smtplib.SMTP_SSL(sm["host"], port, context=ctx, timeout=30)
    else:
        s = smtplib.SMTP(sm["host"], port, timeout=30)
        if sec == "starttls":
            s.starttls(context=ctx)
    try:
        if sm.get("user"):
            s.login(sm["user"], pw)
        s.sendmail(msg["From"], to, msg.as_string())
    finally:
        try:
            s.quit()
        except Exception:
            pass


def send_digest(profile_ids=None, only_unsent=True, dry_run=False):
    cfg = get_config()
    db = load_json("items.json", {"items": {}, "feeds": {}})
    dig = build_digest(cfg, db, profile_ids, only_unsent)
    total = sum(len(r) for _, r in dig.values())
    if not total:
        return {"sent": 0, "message": "Новых совпадений для рассылки нет"}
    today = datetime.now().strftime("%d.%m.%Y")
    parts_t, parts_h = [], []
    for pid, (p, rows) in dig.items():
        parts_t.append("\n== %s (%d) ==" % (p["name"], len(rows)))
        parts_h.append("<h3 style='font:600 15px Arial;margin:18px 0 6px'>%s — %d</h3><table cellpadding='6' style='border-collapse:collapse;font:13px Arial'>"
                       "<tr style='border-bottom:2px solid #0f2a44;text-align:left'><th>Дата</th><th>Лента</th><th>Решение / реквизиты</th><th>Совпадения</th></tr>" % (esc(p["name"]), len(rows)))
        for it in rows:
            d = ru_date(it.get("pub") or it.get("first_seen") or "")
            parts_t.append("- %s | %s | %s\n  %s" % (d, it.get("feed_name", ""), it.get("title", ""), it.get("link", "")))
            parts_h.append("<tr style='border-bottom:1px solid #ddd;vertical-align:top'><td style='white-space:nowrap'>%s</td><td>%s</td><td><a href='%s'>%s</a><br><span style='color:#666'>%s</span></td><td>%s</td></tr>" % (
                esc(d), esc(it.get("feed_name")), esc(it.get("link")), esc(it.get("title")),
                esc(" · ".join(it.get("ncn", []) + it.get("actions", []))),
                esc(", ".join((it.get("matches") or {}).get(pid, [])))))
        parts_h.append("</table>")
    note = ("Источник: официальные RSS-ленты Legal Reference System, Судебная власть САР Гонконг. "
            "Только метаданные и ссылки; тексты решений — на сайте источника. "
            "Частное некоммерческое использование.")
    subject = "HK Case Law: %d %s по профилям — %s" % (total, plural(total, "новое решение", "новых решения", "новых решений"), today)
    text = "Новые решения судов Гонконга по профилям поиска (%s)\n%s\n\n%s" % (today, "\n".join(parts_t), note)
    html_body = "<div style='font:13px Arial'><h2 style='font:600 17px Arial'>Новые решения судов Гонконга — %s</h2>%s<p style='color:#666;font-size:12px;margin-top:18px'>%s</p></div>" % (today, "".join(parts_h), esc(note))
    if dry_run:
        return {"sent": 0, "preview_subject": subject, "preview_html": html_body, "count": total}
    send_mail(subject, text, html_body)
    with LOCK:
        db = load_json("items.json", {"items": {}, "feeds": {}})
        for pid, (p, rows) in dig.items():
            for it in rows:
                cur = db["items"].get(it["id"])
                if cur is not None:
                    cur.setdefault("sent", [])
                    if pid not in cur["sent"]:
                        cur["sent"].append(pid)
        save_json("items.json", db)
    log("Дайджест отправлен: %d позиций" % total)
    return {"sent": total, "message": "Отправлено позиций: %d" % total}


def send_report(body, dry_run=False):
    """Отчёт по анализу загруженных решений (вкладка 5). Данные приходят со страницы."""
    rows = body.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return {"sent": 0, "message": "Нет результатов для отправки"}
    rows = rows[:300]
    only = bool(body.get("only_matching"))
    if only:
        rows = [r for r in rows if r.get("ok")]
        if not rows:
            return {"sent": 0, "message": "Нет соответствующих документов"}
    q = str(body.get("query") or "")[:500]
    today = datetime.now().strftime("%d.%m.%Y")
    n_ok = sum(1 for r in rows if r.get("ok"))
    th = "".join("<th align='left' style='border-bottom:2px solid #0f2a44;padding:6px'>%s</th>" % h for h in
                 ("Документ", "Суд / реквизиты", "Дата / состав", "Совпадения и фрагменты"))
    trs, txt = [], []
    for r in rows:
        snips = [str(x)[:700] for x in (r.get("snippets") or [])[:6]]
        req = " · ".join([str(x) for x in (r.get("ncn") or []) + (r.get("acts") or [])][:12])
        trs.append("<tr style='border-bottom:1px solid #ddd;vertical-align:top'><td style='padding:6px'><b>%s</b><br><span style='color:%s'>%s</span></td><td style='padding:6px'>%s<br><span style='color:#666'>%s</span></td><td style='padding:6px'>%s<br><span style='color:#666'>%s</span></td><td style='padding:6px'>%s%s</td></tr>" % (
            esc(str(r.get("file", ""))[:200]), "#1d7a3e" if r.get("ok") else "#888",
            "соответствует" if r.get("ok") else "не соответствует",
            esc(str(r.get("court") or "—")), esc(req), esc(str(r.get("date") or "—")), esc(str(r.get("before") or "")[:300]),
            esc(str(r.get("counts") or "")),
            "".join("<div style='border-left:2px solid #ccc;padding-left:8px;margin:4px 0;color:#333'>…%s…</div>" % esc(x) for x in snips)))
        txt.append("- %s [%s] %s | %s | %s" % (r.get("file"), "да" if r.get("ok") else "нет", r.get("court") or "", req, r.get("counts") or ""))
    note = "Отчёт сформирован HK Case Law Monitor по документам, загруженным оператором. Частное некоммерческое использование."
    subject = "HK Case Law: анализ решений — %d из %d %s — %s" % (n_ok, len(rows), "соответствует" if n_ok % 10 == 1 and n_ok % 100 != 11 else "соответствуют", today)
    html_body = ("<div style='font:13px Arial'><h2 style='font:600 17px Arial'>Анализ решений — %s</h2><p>Запрос: <b>%s</b><br>Документов: %d, соответствуют: %d</p>"
                 "<table cellpadding='0' style='border-collapse:collapse;font:13px Arial;width:100%%'><tr>%s</tr>%s</table><p style='color:#666;font-size:12px;margin-top:18px'>%s</p></div>") % (
        today, esc(q or "—"), len(rows), n_ok, th, "".join(trs), esc(note))
    text = "Анализ решений — %s\nЗапрос: %s\n\n%s\n\n%s" % (today, q, "\n".join(txt), note)
    if dry_run:
        return {"sent": 0, "preview_subject": subject, "preview_html": html_body, "count": len(rows)}
    send_mail(subject, text, html_body)
    log("Отчёт анализа отправлен: %d документов" % len(rows))
    return {"sent": len(rows), "message": "Отчёт отправлен: документов %d" % len(rows)}


def scheduler():
    while True:
        hours = float(STATE.get("auto_check_hours") or 0)
        if hours <= 0:
            STATE["next_check"] = None
            time.sleep(30)
            continue
        nxt = time.time() + hours * 3600
        STATE["next_check"] = datetime.fromtimestamp(nxt, timezone.utc).isoformat()
        while time.time() < nxt:
            time.sleep(20)
            if float(STATE.get("auto_check_hours") or 0) != hours:
                break
        else:
            try:
                check_feeds()
                if get_config()["settings"].get("auto_email"):
                    send_digest()
            except Exception as e:
                log("Ошибка автопроверки: %s" % e)

# ----------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "HKCaseLawMonitor/" + VERSION

    def log_message(self, fmt, *a):
        pass

    def _origin_ok(self):
        o = self.headers.get("Origin")
        if not o:
            return True, None
        allowed = set(DEFAULT_ORIGINS) | set(ARGS.allow_origin or [])
        port = self.server.server_address[1]
        allowed |= {"http://127.0.0.1:%d" % port, "http://localhost:%d" % port, "null"}
        return (o in allowed), o

    def _cors(self, origin):
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-HKCL")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, obj, code=200):
        ok, origin = self._origin_ok()
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._cors(origin if ok else None)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        ok, origin = self._origin_ok()
        self.send_response(204 if ok else 403)
        self._cors(origin if ok else None)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("слишком большой запрос")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            p = os.path.join(BASE_DIR, "index.html")
            if not os.path.exists(p):
                b = ("<meta charset='utf-8'><p>Сервер HK Case Law Monitor работает. Положите index.html в папку "
                     "%s или откройте страницу инструмента на GitHub Pages.</p>" % esc(BASE_DIR)).encode("utf-8")
            else:
                with open(p, "rb") as f:
                    b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        ok, _ = self._origin_ok()
        if not ok:
            return self._json({"error": "origin не разрешён"}, 403)
        try:
            if u.path == "/api/status":
                cfg = get_config()
                db = load_json("items.json", {"items": {}, "feeds": {}})
                return self._json({
                    "ok": True, "version": VERSION, "started": STATE["started"],
                    "last_check": STATE["last_check"], "next_check": STATE["next_check"],
                    "auto_check_hours": STATE["auto_check_hours"],
                    "items": len(db["items"]),
                    "matches": sum(1 for v in db["items"].values() if v.get("matches")),
                    "smtp_configured": bool(cfg["smtp"].get("host") and _norm_list(cfg["smtp"].get("to"))),
                    "password_set": bool(get_secret().get("smtp_password")),
                    "data_dir": data_dir(), "allowed_hosts": sorted(set(ALLOWED_FEED_HOSTS) | set(clean_hosts(cfg.get("extra_feed_hosts")))),
                    "user_agent": USER_AGENT, "summary": STATE["last_check_summary"]})
            if u.path == "/api/config":
                cfg = get_config()
                cfg["password_set"] = bool(get_secret().get("smtp_password"))
                return self._json(cfg)
            if u.path == "/api/items":
                q = parse_qs(u.query)
                db = load_json("items.json", {"items": {}, "feeds": {}})
                items = list(db["items"].values())
                items.sort(key=lambda x: x.get("pub") or x.get("first_seen") or "", reverse=True)
                if q.get("matched", ["0"])[0] == "1":
                    items = [i for i in items if i.get("matches")]
                lim = int(q.get("limit", ["2000"])[0])
                return self._json({"items": items[:lim], "feeds": db["feeds"], "total": len(items)})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        ok, _ = self._origin_ok()
        if not ok:
            return self._json({"error": "origin не разрешён"}, 403)
        if self.headers.get("X-HKCL") != "1":
            return self._json({"error": "нет заголовка X-HKCL"}, 403)
        try:
            body = self._body()
            if u.path == "/api/config":
                cfg = get_config()
                for k in ("feeds", "profiles", "smtp", "settings", "custom_sources", "extra_feed_hosts"):
                    if k in body:
                        cfg[k] = body[k]
                cfg["extra_feed_hosts"] = clean_hosts(cfg.get("extra_feed_hosts"))
                for cs in cfg.get("custom_sources") or []:
                    for key in ("search", "home", "rss"):
                        v = str(cs.get(key) or "").strip()
                        if v and not re.match(r"^https?://", v, re.I):
                            return self._json({"error": "Источник «%s»: адрес должен начинаться с http(s)://" % cs.get("name")}, 400)
                for f in cfg["feeds"]:
                    f.setdefault("id", "f_" + uuid.uuid4().hex[:8])
                    good, why = feed_url_ok(f.get("url", ""), cfg.get("extra_feed_hosts"))
                    if not good:
                        return self._json({"error": "Лента «%s»: %s" % (f.get("name"), why)}, 400)
                for p in cfg["profiles"]:
                    p.setdefault("id", "p_" + uuid.uuid4().hex[:8])
                    if p.get("case_regex"):
                        try:
                            re.compile(p["case_regex"])
                        except re.error as e:
                            return self._json({"error": "Профиль «%s»: ошибка regex: %s" % (p.get("name"), e)}, 400)
                if "smtp_password" in body and body["smtp_password"]:
                    sec = get_secret()
                    sec["smtp_password"] = body["smtp_password"]
                    save_json("secret.json", sec, private=True)
                if body.get("clear_password"):
                    save_json("secret.json", {}, private=True)
                save_json("config.json", cfg, private=True)
                STATE["auto_check_hours"] = float(cfg["settings"].get("auto_check_hours") or 0)
                # пересчитать совпадения по новым профилям
                with LOCK:
                    db = load_json("items.json", {"items": {}, "feeds": {}})
                    for it in db["items"].values():
                        it["matches"] = {p["id"]: h for p in cfg["profiles"] for h in [match_profile(p, it)] if h is not None}
                    save_json("items.json", db)
                return self._json({"ok": True})
            if u.path == "/api/check":
                return self._json(check_feeds(force=bool(body.get("force"))))
            if u.path == "/api/robots":
                good, why = feed_url_ok(body.get("url", ""))
                if not good:
                    return self._json({"status": "rejected", "note": why})
                rs, note = robots_status(body["url"])
                return self._json({"status": rs, "note": note})
            if u.path == "/api/digest":
                return self._json(send_digest(body.get("profiles"), not body.get("all"), bool(body.get("dry_run"))))
            if u.path == "/api/report":
                return self._json(send_report(body, bool(body.get("dry_run"))))
            if u.path == "/api/test-email":
                send_mail("HK Case Law Monitor: тестовое письмо",
                          "Тест SMTP-настроек прошёл успешно.",
                          "<p>Тест SMTP-настроек прошёл успешно.</p>")
                return self._json({"ok": True, "message": "Тестовое письмо отправлено"})
            if u.path == "/api/reset-sent":
                with LOCK:
                    db = load_json("items.json", {"items": {}, "feeds": {}})
                    for it in db["items"].values():
                        it["sent"] = []
                    save_json("items.json", db)
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except smtplib.SMTPException as e:
            return self._json({"error": "SMTP: %s" % e}, 502)
        except Exception as e:
            return self._json({"error": str(e)}, 500)


def main():
    global ARGS
    ap = argparse.ArgumentParser(description="HK Case Law Monitor — локальный сервер")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--auto-check", type=float, default=None, help="автопроверка каждые N часов (0 — выкл.)")
    ap.add_argument("--proxy", default=None, help="HTTP(S)-прокси, напр. http://proxy:3128")
    ap.add_argument("--cafile", default=None, help="CA-сертификат корпоративного прокси")
    ap.add_argument("--allow-origin", action="append", help="доп. разрешённый Origin страницы")
    ap.add_argument("--data", default=None, help="папка данных")
    ap.add_argument("--allow-feed-host", action="append", help="доп. разрешённый хост RSS (только для официальных лент)")
    ap.add_argument("--check-once", action="store_true", help="однократная проверка и рассылка (для планировщика)")
    ARGS = ap.parse_args()
    for h in (ARGS.allow_feed_host or []):
        if "hklii" not in h.lower():
            ALLOWED_FEED_HOSTS.add(h.lower())
    if ARGS.host not in ("127.0.0.1", "localhost"):
        print("ВНИМАНИЕ: сервер без аутентификации; используйте только во внутренней сети.")
    cfg = get_config()
    STATE["auto_check_hours"] = float(ARGS.auto_check if ARGS.auto_check is not None else cfg["settings"].get("auto_check_hours") or 0)
    if ARGS.auto_check is not None:
        cfg["settings"]["auto_check_hours"] = ARGS.auto_check
        save_json("config.json", cfg, private=True)
    if ARGS.check_once:
        s = check_feeds()
        print(json.dumps(s, ensure_ascii=False, indent=1))
        if cfg["settings"].get("auto_email"):
            print(send_digest())
        return
    STATE["started"] = datetime.now(timezone.utc).isoformat()
    threading.Thread(target=scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    log("HK Case Law Monitor %s: http://%s:%d  (данные: %s)" % (VERSION, ARGS.host, ARGS.port, data_dir()))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
