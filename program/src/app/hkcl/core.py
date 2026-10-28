# -*- coding: utf-8 -*-
"""Ядро: ленты LRS, robots.txt, профили, дела на контроле, документы, поиск, рассылка, вебхуки."""

import base64
import hashlib
import hmac
import html as htmlmod
import io
import json
import re
import smtplib
import ssl
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime, formatdate
from urllib import request as urlreq, error as urlerr
from urllib.parse import urlparse, urlencode, quote
from urllib.robotparser import RobotFileParser

from . import USER_AGENT
from . import store as S

ALLOWED_FEED_HOSTS = {"legalref.judiciary.hk", "www.hkcfa.hk", "www.judiciary.hk"}
TEST_FEED_HOSTS = set()          # только для тестов (--allow-feed-host)
HKT = timezone(timedelta(hours=8))
NOTIFIERS = []                   # callables(title, message) — трей/уведомления
_CHECK_LOCK = threading.Lock()


class UserError(Exception):
    """Ошибка ввода — показывается пользователю/агенту как есть."""


# ================================================================ helpers

def norm_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        v = re.split(r"[\n;]", v)
    return [str(x).strip() for x in v if x is not None and str(x).strip()]


def esc(s):
    return htmlmod.escape("" if s is None else str(s))


def plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def ru_date(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(HKT).strftime("%d.%m.%Y")
    except Exception:
        try:
            return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
        except Exception:
            return iso[:10]


def term_re(w):
    """Поиск термина: без учёта регистра; для терминов от 6 символов допускаются
    одиночные пробелы внутри (артефакты извлечения текста из PDF); граница слова в начале."""
    w = str(w).strip()
    fuzzy = len(re.sub(r"\s+", "", w)) >= 6
    parts = [r"\s*" if c.isspace() else re.escape(c) for c in w]
    body = (r"\s?" if fuzzy else "").join(parts)
    pre = r"(?<![A-Za-z0-9])" if re.match(r"[A-Za-z0-9]", w) else ""
    return re.compile(pre + body, re.I)


def has_term(text, w):
    return bool(term_re(w).search(text or ""))


# ================================================================ network

def _opener():
    cfg = S.get_config()["settings"]
    handlers = []
    if cfg.get("proxy"):
        handlers.append(urlreq.ProxyHandler({"http": cfg["proxy"], "https": cfg["proxy"]}))
    ctx = ssl.create_default_context(cafile=cfg.get("cafile") or None)
    handlers.append(urlreq.HTTPSHandler(context=ctx))
    return urlreq.build_opener(*handlers)


def http_request(url, data=None, headers=None, timeout=30, method=None):
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urlreq.Request(url, data=data, headers=h, method=method)
    try:
        with _opener().open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urlerr.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, dict(e.headers or {}), body


def clean_hosts(v):
    out = []
    for h in (v or []):
        h = str(h).strip().lower()
        h = re.sub(r"^https?://", "", h).split("/")[0].split(":")[0]
        if h and "hklii" not in h and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", h) and h not in out:
            out.append(h)
    return out


def allowed_hosts(cfg=None):
    cfg = cfg or S.get_config()
    return set(ALLOWED_FEED_HOSTS) | set(clean_hosts(cfg.get("extra_feed_hosts"))) | TEST_FEED_HOSTS


def feed_url_ok(url, cfg=None):
    hosts = allowed_hosts(cfg)
    try:
        u = urlparse(url)
    except Exception:
        return False, "некорректный URL"
    host = (u.hostname or "").lower()
    if "hklii" in host:
        return False, "HKLII запрещает автоматический доступ"
    if u.scheme != "https" and host not in TEST_FEED_HOSTS:
        return False, "допускается только https"
    if host not in hosts:
        return False, "хост не в списке разрешённых: " + ", ".join(sorted(hosts))
    return True, ""


_ROBOTS = {}


def robots_status(url):
    u = urlparse(url)
    base = "%s://%s" % (u.scheme, u.netloc)
    c = _ROBOTS.get(base)
    if not c or time.time() - c["t"] > 86400:
        rp = RobotFileParser()
        try:
            code, _, body = http_request(base + "/robots.txt", headers={"Accept": "text/plain"}, timeout=20)
            if code in (401, 403) or code >= 500:
                rp.disallow_all = True
                note = "robots.txt: HTTP %d (трактуется как запрет)" % code
            elif 400 <= code < 500:
                rp.allow_all = True
                note = "robots.txt отсутствует (HTTP %d)" % code
            else:
                rp.parse(body.decode("utf-8", "replace").splitlines())
                note = "robots.txt получен"
            c = {"t": time.time(), "rp": rp, "note": note, "ok": True}
        except Exception as e:
            c = {"t": time.time(), "rp": None, "note": "robots.txt недоступен: %s" % e, "ok": False}
        _ROBOTS[base] = c
    if not c["ok"]:
        return "unknown", c["note"]
    return ("allowed" if c["rp"].can_fetch(USER_AGENT, url) else "disallowed"), c["note"]


# ================================================================ parsing

NCN_RE = re.compile(r"\[(?:19|20)\d{2}\]\s*HK(?:CFAR|CFA|CA|CFI|DC|FC|LdT|CT|CrC|OAT|LaT|MagC|LRD|C)\s*\d{1,5}", re.I)
ACT_PFX = r"(?:FA|CA|HC|DC|FC|LD|KC|ES|KT|WK|FL|TM|ST|CT|LB|SC|OAT|MC)[A-Z]{1,4}"
ACTION_RE = re.compile(r"\b" + ACT_PFX + r"\s?\d{1,6}[A-Z]?\s?/\s?(?:19|20)\d{2}\b")
ACTION_NO_RE = re.compile(r"\b(" + ACT_PFX + r")\s+No\.?\s*(\d{1,6}[A-Z]?)\s+of\s+((?:19|20)\d{2})\b", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def norm_action(s):
    s = re.sub(r"\s+", "", str(s or "").upper())
    m = re.match(r"^([A-Z]{3,8})NO\.?(\d+[A-Z]?)OF(\d{4})$", s)
    if m:
        s = "%s%s/%s" % m.groups()
    return s


def norm_ncn(s):
    s = re.sub(r"\s+", " ", str(s or "").strip().upper())
    s = re.sub(r"\]\s*", "] ", s)
    return re.sub(r"(HK[A-Z]+)\s*(\d+)$", r"\1 \2", s)


def find_ncn(text):
    return sorted(set(norm_ncn(m.group(0)) for m in NCN_RE.finditer(text or "")))


def find_actions(text):
    a = set(norm_action(m.group(0)) for m in ACTION_RE.finditer(text or ""))
    a |= set("%s%s/%s" % (m.group(1).upper(), m.group(2).upper(), m.group(3)) for m in ACTION_NO_RE.finditer(text or ""))
    return sorted(a)


def strip_html(s):
    return re.sub(r"\s+", " ", htmlmod.unescape(TAG_RE.sub(" ", s or ""))).strip()


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
            d = d.replace(tzinfo=HKT)
        return d.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            d = datetime.strptime(s.strip(), fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=HKT)
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
        blob = title + " " + desc
        items.append({"guid": guid, "title": title, "link": link, "description": desc[:3000],
                      "pub_raw": pub, "pub": parse_date(pub), "ncn": find_ncn(blob), "actions": find_actions(blob)})
    return items


# ================================================================ profiles

def item_text(it):
    return " ".join([it.get("title") or "", it.get("description") or "",
                     " ".join(it.get("ncn") or []), " ".join(it.get("actions") or [])])


def match_profile(p, it, text=None):
    if not p.get("enabled", True):
        return None
    feeds = p.get("feeds") or []
    if feeds and it.get("feed_id") and it.get("feed_id") not in feeds:
        return None
    text = text if text is not None else item_text(it)
    hits = []
    if any(has_term(text, w) for w in norm_list(p.get("exclude_words"))):
        return None
    all_w = norm_list(p.get("all_words"))
    for w in all_w:
        if not has_term(text, w):
            return None
        hits.append(w)
    any_w = norm_list(p.get("any_words"))
    if any_w:
        found = [w for w in any_w if has_term(text, w)]
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


def validate_profiles(profiles):
    for p in profiles:
        p.setdefault("id", S.new_id("p_"))
        p["name"] = str(p.get("name") or "Профиль").strip()[:200]
        for k in ("any_words", "all_words", "exclude_words", "feeds"):
            p[k] = norm_list(p.get(k))
        if p.get("case_regex"):
            try:
                re.compile(p["case_regex"])
            except re.error as e:
                raise UserError("Профиль «%s»: ошибка regex: %s" % (p["name"], e))
    return profiles


def validate_feeds(feeds, cfg):
    for f in feeds:
        f.setdefault("id", S.new_id("f_"))
        f.setdefault("enabled", True)
        f.setdefault("robots_override", False)
        ok, why = feed_url_ok(f.get("url", ""), cfg)
        if not ok:
            raise UserError("Лента «%s»: %s" % (f.get("name"), why))
    return feeds


def validate_sources(sources):
    for c in sources:
        c.setdefault("id", S.new_id())
        if not c.get("name"):
            raise UserError("У источника нет названия")
        for k in ("search", "home", "rss"):
            v = str(c.get(k) or "").strip()
            if v and not re.match(r"^https?://", v, re.I):
                raise UserError("Источник «%s»: адрес должен начинаться с http(s)://" % c.get("name"))
            c[k] = v
        if not (c.get("search") or c.get("home")):
            raise UserError("Источник «%s»: нужен адрес поиска или главной страницы" % c["name"])
    return sources


def item_from_row(r):
    return S.row(r, ("ncn", "actions", "matches", "sent"))


def recompute_matches(cfg=None):
    cfg = cfg or S.get_config()
    rows = S.q("SELECT * FROM items")
    con = S.db()
    with con:
        for r in rows:
            it = item_from_row(r)
            m = {p["id"]: h for p in cfg["profiles"] for h in [match_profile(p, it)] if h is not None}
            con.execute("UPDATE items SET matches=? WHERE id=?", (json.dumps(m, ensure_ascii=False), it["id"]))


# ================================================================ feeds

def feed_states():
    return {r["feed_id"]: json.loads(r["data"]) for r in S.q("SELECT * FROM feed_state")}


def _save_state(fid, st):
    S.x("INSERT INTO feed_state(feed_id,data) VALUES(?,?) ON CONFLICT(feed_id) DO UPDATE SET data=excluded.data",
        (fid, json.dumps(st, ensure_ascii=False)))


def check_feeds(force=False, actor="system"):
    with _CHECK_LOCK:
        return _check_feeds(force, actor)


def _check_feeds(force, actor):
    cfg = S.get_config()
    min_iv = max(5, int(cfg["settings"].get("min_interval_min", 10))) * 60
    states = feed_states()
    started = S.now_iso()
    summary = {"time": started, "feeds": [], "new": 0, "new_matches": 0, "new_items": []}
    for f in cfg["feeds"]:
        fid = f.get("id")
        st = states.get(fid, {})
        res = {"id": fid, "name": f.get("name"), "status": "", "new": 0}
        ok, why = feed_url_ok(f.get("url", ""), cfg)
        if not f.get("enabled", True):
            res["status"] = "отключена"
        elif not ok:
            res["status"] = "отклонено: " + why
        else:
            rs, note = robots_status(f["url"])
            st["robots"], st["robots_note"] = rs, note
            if rs != "allowed" and not f.get("robots_override"):
                res["status"] = "не опрошена: robots.txt = %s; требуется решение оператора" % rs
            elif st.get("last_fetch_ts") and time.time() - st["last_fetch_ts"] < min_iv and not force:
                res["status"] = "пропуск: интервал %d мин ещё не истёк" % (min_iv // 60)
            else:
                hdr = {"Accept": "application/rss+xml, application/xml, text/xml, */*"}
                if st.get("etag"):
                    hdr["If-None-Match"] = st["etag"]
                if st.get("last_modified"):
                    hdr["If-Modified-Since"] = st["last_modified"]
                try:
                    code, headers, body = http_request(f["url"], headers=hdr)
                    st["last_fetch_ts"] = time.time()
                    st["last_fetch"] = S.now_iso()
                    st["http"] = code
                    if code == 304:
                        res["status"] = "без изменений (304)"
                    elif code != 200:
                        res["status"] = "HTTP %d" % code
                    else:
                        hl = {k.lower(): v for k, v in headers.items()}
                        st["etag"] = hl.get("etag", "")
                        st["last_modified"] = hl.get("last-modified", "")
                        parsed = parse_feed(body)
                        st["count_last"] = len(parsed)
                        con = S.db()
                        with con:
                            for it in parsed:
                                key = fid + "::" + it["guid"]
                                if con.execute("SELECT 1 FROM items WHERE id=?", (key,)).fetchone():
                                    continue
                                it.update({"id": key, "feed_id": fid, "feed_name": f.get("name"), "first_seen": S.now_iso()})
                                m = {p["id"]: h for p in cfg["profiles"] for h in [match_profile(p, it)] if h is not None}
                                con.execute("INSERT INTO items(id,feed_id,feed_name,guid,title,link,description,pub,pub_raw,first_seen,ncn,actions,matches,sent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                            (key, fid, f.get("name"), it["guid"], it["title"], it["link"], it["description"], it["pub"], it["pub_raw"],
                                             it["first_seen"], json.dumps(it["ncn"]), json.dumps(it["actions"]), json.dumps(m, ensure_ascii=False), "[]"))
                                it["matches"] = m
                                res["new"] += 1
                                summary["new_items"].append(it)
                                if m:
                                    summary["new_matches"] += 1
                        res["status"] = "OK: элементов в ленте %d, новых %d" % (len(parsed), res["new"])
                except ET.ParseError as e:
                    res["status"] = "ошибка разбора XML: %s" % e
                except Exception as e:
                    res["status"] = "ошибка: %s" % e
        st["last_status"] = res["status"]
        _save_state(fid, st)
        summary["feeds"].append(res)
        summary["new"] += res["new"]
    keep = int(cfg["settings"].get("retention_days", 730))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep)).isoformat()
    S.x("DELETE FROM items WHERE first_seen < ? AND id NOT IN (SELECT item_id FROM case_events WHERE item_id IS NOT NULL)", (cutoff,))
    # дела на контроле
    summary["case_events"] = match_cases_items(summary["new_items"], actor="system")
    S.kv_set("last_check", {"time": started, "new": summary["new"], "new_matches": summary["new_matches"],
                            "case_events": len(summary["case_events"]),
                            "feeds": [{"id": r["id"], "status": r["status"]} for r in summary["feeds"]]})
    S.log("Проверка лент (%s): новых %d, совпадений %d, событий по делам %d" % (actor, summary["new"], summary["new_matches"], len(summary["case_events"])))
    if summary["new"]:
        emit("items.new", {"count": summary["new"], "items": [_brief(i) for i in summary["new_items"]][:200]})
    if summary["new_matches"]:
        emit("items.matched", {"count": summary["new_matches"], "items": [_brief(i) for i in summary["new_items"] if i.get("matches")][:200]})
    out = dict(summary)
    out["new_items"] = [_brief(i) for i in summary["new_items"]]
    return out


def _brief(it):
    return {k: it.get(k) for k in ("id", "feed_id", "feed_name", "title", "link", "pub", "ncn", "actions", "matches")}


def list_items(matched=None, profile=None, feed=None, text=None, days=None, limit=2000, offset=0, unsent=None):
    sql, args = "SELECT * FROM items WHERE 1=1", []
    if feed:
        sql += " AND feed_id=?"
        args.append(feed)
    if days:
        sql += " AND COALESCE(pub, first_seen) >= ?"
        args.append((datetime.now(timezone.utc) - timedelta(days=float(days))).isoformat())
    sql += " ORDER BY COALESCE(pub, first_seen) DESC"
    rows = [item_from_row(r) for r in S.q(sql, args)]
    if matched:
        rows = [r for r in rows if r.get("matches")]
    if profile:
        rows = [r for r in rows if profile in (r.get("matches") or {})]
    if unsent:
        rows = [r for r in rows if any(p not in (r.get("sent") or []) for p in (r.get("matches") or {}))]
    if text:
        rows = [r for r in rows if has_term(item_text(r), text)]
    total = len(rows)
    return {"total": total, "items": rows[offset:offset + int(limit)]}


# ================================================================ cases (дела на контроле)

CASE_JSON = ("numbers", "ncn", "parties", "keywords", "tags")


def _case(r):
    return S.row(r, CASE_JSON)


def list_cases(status=None, with_counts=True):
    sql, args = "SELECT * FROM cases", []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, COALESCE(last_event_at, created_at) DESC"
    out = []
    for r in S.q(sql, args):
        c = _case(r)
        if with_counts:
            c["events_total"] = S.q("SELECT COUNT(*) n FROM case_events WHERE case_id=?", (c["id"],), one=True)["n"]
            c["events_unseen"] = S.q("SELECT COUNT(*) n FROM case_events WHERE case_id=? AND seen=0", (c["id"],), one=True)["n"]
        out.append(c)
    return out


def get_case(cid):
    c = _case(S.q("SELECT * FROM cases WHERE id=?", (cid,), one=True))
    if not c:
        raise UserError("Дело не найдено: %s" % cid)
    c["events"] = [dict(e) for e in S.q("SELECT * FROM case_events WHERE case_id=? ORDER BY COALESCE(date, created_at) DESC, created_at DESC", (cid,))]
    return c


def _clean_case(d):
    out = {}
    out["title"] = str(d.get("title") or "").strip()[:300]
    out["numbers"] = sorted(set(norm_action(x) for x in norm_list(d.get("numbers"))))
    out["ncn"] = sorted(set(norm_ncn(x) for x in norm_list(d.get("ncn"))))
    out["parties"] = norm_list(d.get("parties"))
    out["keywords"] = norm_list(d.get("keywords"))
    out["tags"] = norm_list(d.get("tags"))
    out["court"] = str(d.get("court") or "").strip()[:100]
    out["status"] = d.get("status") if d.get("status") in ("active", "closed") else "active"
    out["priority"] = d.get("priority") if d.get("priority") in ("high", "normal", "low") else "normal"
    out["notes"] = str(d.get("notes") or "")[:20000]
    rem = str(d.get("remind_at") or "").strip()
    if rem and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rem):
        raise UserError("Дата напоминания — в формате ГГГГ-ММ-ДД")
    out["remind_at"] = rem or None
    if not out["title"]:
        out["title"] = (out["numbers"] + out["ncn"] + out["parties"] + ["Дело"])[0]
    if not (out["numbers"] or out["ncn"] or out["parties"] or out["keywords"]):
        raise UserError("Укажите хотя бы один признак дела: номер дела, NCN, сторону или ключевые слова")
    return out


def add_case(d, actor="ui", backfill=True):
    c = _clean_case(d)
    for ex in list_cases(status="active", with_counts=False):
        same = (set(c["numbers"]) & set(ex["numbers"])) or (set(c["ncn"]) & set(ex["ncn"])) or \
               (not c["numbers"] and not c["ncn"] and c["parties"] and sorted(c["parties"]) == sorted(ex["parties"]))
        if same:
            res = get_case(ex["id"])
            res["already_exists"] = True
            return res
    cid = S.new_id("c_")
    ts = S.now_iso()
    S.x("INSERT INTO cases(id,title,numbers,ncn,parties,keywords,court,status,priority,notes,remind_at,tags,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, c["title"], json.dumps(c["numbers"]), json.dumps(c["ncn"]), json.dumps(c["parties"], ensure_ascii=False),
         json.dumps(c["keywords"], ensure_ascii=False), c["court"], c["status"], c["priority"], c["notes"], c["remind_at"],
         json.dumps(c["tags"], ensure_ascii=False), ts, ts, actor))
    add_event(cid, "status", "Поставлено на контроль", actor=actor, seen=1)
    found = 0
    if backfill:  # найти уже собранные решения и документы по делу
        items = [item_from_row(r) for r in S.q("SELECT * FROM items")]
        found += len(match_cases_items(items, only_case=cid, actor=actor, notify=False))
        for dr in S.q("SELECT id,name,text,meta FROM documents"):
            found += len(match_cases_document(dict(dr), only_case=cid, actor=actor, notify=False))
    S.audit(actor, "case.add", {"id": cid, "title": c["title"], "backfill_events": found})
    emit("case.added", {"case": get_case(cid)})
    return get_case(cid)


def update_case(cid, d, actor="ui"):
    old = get_case(cid)
    merged = {k: d.get(k, old.get(k)) for k in ("title", "numbers", "ncn", "parties", "keywords", "tags", "court", "status", "priority", "notes", "remind_at")}
    c = _clean_case(merged)
    S.x("UPDATE cases SET title=?,numbers=?,ncn=?,parties=?,keywords=?,court=?,status=?,priority=?,notes=?,remind_at=?,tags=?,updated_at=? WHERE id=?",
        (c["title"], json.dumps(c["numbers"]), json.dumps(c["ncn"]), json.dumps(c["parties"], ensure_ascii=False),
         json.dumps(c["keywords"], ensure_ascii=False), c["court"], c["status"], c["priority"], c["notes"], c["remind_at"],
         json.dumps(c["tags"], ensure_ascii=False), S.now_iso(), cid))
    if old["status"] != c["status"]:
        add_event(cid, "status", "Дело закрыто (снято с контроля)" if c["status"] == "closed" else "Дело снова на контроле", actor=actor, seen=1)
    S.audit(actor, "case.update", {"id": cid})
    return get_case(cid)


def delete_case(cid, actor="ui"):
    get_case(cid)
    S.x("DELETE FROM case_events WHERE case_id=?", (cid,))
    S.x("DELETE FROM cases WHERE id=?", (cid,))
    S.audit(actor, "case.delete", {"id": cid})
    return {"ok": True}


EVENT_TYPES = {"judgment": "Новое решение", "document": "Документ", "note": "Заметка", "hearing": "Слушание",
               "deadline": "Срок", "status": "Статус", "reminder": "Напоминание", "ai": "ИИ-анализ", "agent": "Агент"}


def add_event(cid, etype, title, details="", date_=None, link="", item_id=None, doc_id=None, actor="ui", seen=0):
    if etype not in EVENT_TYPES:
        raise UserError("Неизвестный тип события: %s (допустимо: %s)" % (etype, ", ".join(EVENT_TYPES)))
    if date_ and not re.match(r"^\d{4}-\d{2}-\d{2}", str(date_)):
        raise UserError("Дата события — в формате ГГГГ-ММ-ДД")
    eid = S.new_id("e_")
    ts = S.now_iso()
    try:
        S.x("INSERT INTO case_events(id,case_id,type,date,title,details,link,item_id,doc_id,actor,created_at,seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, cid, etype, date_ or ts, str(title)[:500], str(details or "")[:20000], link or "", item_id, doc_id, actor, ts, seen))
    except Exception as e:
        if "UNIQUE" in str(e):
            return None
        raise
    S.x("UPDATE cases SET last_event_at=?, updated_at=? WHERE id=?", (ts, ts, cid))
    ev = dict(S.q("SELECT * FROM case_events WHERE id=?", (eid,), one=True))
    if etype not in ("status",):
        emit("case.event", {"case_id": cid, "event": ev})
    return ev


def mark_seen(cid=None, event_ids=None, actor="ui"):
    if event_ids:
        for e in event_ids:
            S.x("UPDATE case_events SET seen=1 WHERE id=?", (e,))
    elif cid:
        S.x("UPDATE case_events SET seen=1 WHERE case_id=?", (cid,))
    return {"ok": True}


def _case_hits(c, numbers, ncns, title, text):
    hits = []
    for n in c["numbers"]:
        if n in numbers:
            hits.append(n)
    for n in c["ncn"]:
        if n in ncns:
            hits.append(n)
    for p in c["parties"]:
        if has_term(title, p):
            hits.append(p)
    if c["keywords"] and all(has_term(text, k) for k in c["keywords"]):
        hits.extend(c["keywords"])
    return hits


def match_cases_items(items, only_case=None, actor="system", notify=True):
    cases = [c for c in list_cases(with_counts=False) if c["status"] == "active" and (not only_case or c["id"] == only_case)]
    events = []
    for it in items:
        numbers = set(norm_action(a) for a in (it.get("actions") or []))
        ncns = set(norm_ncn(n) for n in (it.get("ncn") or []))
        text = item_text(it)
        for c in cases:
            hits = _case_hits(c, numbers, ncns, it.get("title") or "", text)
            if hits:
                ev = add_event(c["id"], "judgment", "Новое решение: " + (it.get("title") or ""),
                               details="Лента: %s\nСовпадения: %s\n%s" % (it.get("feed_name") or "", ", ".join(hits), it.get("description") or ""),
                               date_=it.get("pub"), link=it.get("link") or "", item_id=it["id"], actor=actor)
                if ev:
                    ev["case_title"] = c["title"]
                    events.append(ev)
    if events and notify:
        _notify("Изменения по делам на контроле", "%d %s" % (len(events), plural(len(events), "новое событие", "новых события", "новых событий")))
    return events


def match_cases_document(doc, only_case=None, actor="system", notify=True):
    meta = doc.get("meta")
    if isinstance(meta, str):
        meta = json.loads(meta or "{}")
    text = doc.get("text") or ""
    numbers = set(meta.get("acts") or []) | set(find_actions(text[:200000]))
    ncns = set(meta.get("ncn") or []) | set(find_ncn(text[:200000]))
    cases = [c for c in list_cases(with_counts=False) if c["status"] == "active" and (not only_case or c["id"] == only_case)]
    events = []
    for c in cases:
        hits = _case_hits(c, numbers, ncns, text[:3000], text)
        if hits:
            ev = add_event(c["id"], "document", "Документ упоминает дело: " + (doc.get("name") or ""),
                           details="Совпадения: " + ", ".join(sorted(set(hits))), doc_id=doc["id"], actor=actor)
            if ev:
                ev["case_title"] = c["title"]
                events.append(ev)
    if events and notify:
        _notify("Документ по делу на контроле", doc.get("name") or "")
    return events


def due_reminders(actor="scheduler"):
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for c in list_cases(status="active", with_counts=False):
        if c.get("remind_at") and c["remind_at"] <= today:
            key = "reminded:%s:%s" % (c["id"], c["remind_at"])
            if S.kv_get(key):
                continue
            ev = add_event(c["id"], "reminder", "Напоминание по делу (%s)" % ru_date(c["remind_at"]), details=c.get("notes") or "", actor=actor)
            S.kv_set(key, True)
            if ev:
                ev["case_title"] = c["title"]
                out.append(ev)
    if out:
        _notify("Напоминания по делам", ", ".join(e["case_title"] for e in out)[:200])
    return out


def unseen_events(limit=500):
    rows = S.q("SELECT e.*, c.title case_title FROM case_events e JOIN cases c ON c.id=e.case_id WHERE e.seen=0 ORDER BY e.created_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


# ================================================================ documents

def clean_text(t):
    t = str(t or "").replace("\r", "").replace("\u00a0", " ")
    if len(re.findall(r"(?m)^[ \t]*[A-V][ \t]*$", t)) >= 10:
        t = re.sub(r"(?m)^[ \t]*[A-V][ \t]*\n", "", t)
    t = re.sub(r"(?m)^[ \t]*-[ \t]*\d{1,4}[ \t]*-[ \t]*$", "", t)
    # одиночные буквы полей A–V в начале/конце строк (вывод pypdf)
    t = re.sub(r"(?m)^(?:[A-V] ){1,3}(?=\S)", "", t) if len(re.findall(r"(?m)^[A-V] \S", t)) >= 20 else t
    t = re.sub(r"(?m)^[ \t]+$", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


COURTS = [("COURT OF FINAL APPEAL", "Court of Final Appeal"), ("COURT OF APPEAL", "Court of Appeal"),
          ("COURT OF FIRST INSTANCE", "Court of First Instance"), ("DISTRICT COURT", "District Court"),
          ("FAMILY COURT", "Family Court"), ("LANDS TRIBUNAL", "Lands Tribunal"),
          ("COMPETITION TRIBUNAL", "Competition Tribunal"), ("MAGISTRATES", "Magistrates' Courts"),
          ("終審法院", "終審法院"), ("上訴法庭", "上訴法庭"), ("原訟法庭", "原訟法庭"), ("區域法院", "區域法院")]


def doc_meta(text):
    head = text[:8000]
    up = re.sub(r"\s+", " ", head.upper())
    court, best = "", 10 ** 9
    for k, v in COURTS:
        i = up.find(k)
        if 0 <= i < best:
            best, court = i, v

    def drx(lab):
        return re.compile(r"Date\s+of\s+(?:" + lab + r")\s*[:：]?\s*(?:(\d\s?\d?)\s+([A-Za-z]{3,9})\s*,?\s+(\d\s?\d\s?\d\s?\d)|(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}))", re.I)

    def fmt(m):
        if not m:
            return ""
        return m.group(4) or "%s %s %s" % (re.sub(r"\s", "", m.group(1)), m.group(2), re.sub(r"\s", "", m.group(3)))

    dt = fmt(drx(r"Judgment|Decision|Reasons\s+for\s+Judgment|Handing\s+Down|Sentence|Verdict|Ruling").search(head))
    if not dt:
        h = fmt(drx(r"Hearing").search(head))
        dt = ("слушание: " + h) if h else ""
    bm = re.search(r"(?:Before|Coram)\s*[:：]\s*([^\n]{3,200})", head, re.I)
    return {"court": court, "ncn": find_ncn(head), "acts": find_actions(head), "date": dt,
            "before": re.sub(r"\s+", " ", bm.group(1)).strip() if bm else ""}


def extract_text(name, data):
    n = (name or "").lower()
    if n.endswith(".pdf") or data[:5] == b"%PDF-":
        try:
            from pypdf import PdfReader
            import logging
            logging.getLogger("pypdf").setLevel(logging.ERROR)
        except ImportError:
            raise UserError("Модуль pypdf недоступен — PDF не может быть прочитан")
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    raise UserError("PDF защищён паролем")
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        except UserError:
            raise
        except Exception as e:
            raise UserError("Не удалось прочитать PDF: %s" % e)
    if n.endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml")
        except Exception as e:
            raise UserError("Не удалось прочитать DOCX: %s" % e)
        root = ET.fromstring(xml)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paras = []
        for p in root.iter(ns + "p"):
            paras.append("".join((t.text or "") if t.tag == ns + "t" else ("\t" if t.tag == ns + "tab" else "\n" if t.tag == ns + "br" else "") for t in p.iter()))
        return "\n".join(paras)
    raw = data.decode("utf-8", "replace") if not data.startswith(b"\xff\xfe") else data.decode("utf-16", "replace")
    if n.endswith((".htm", ".html")) or re.search(r"<html|<body|<p[ >]", raw[:2000], re.I):
        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?i)</?(p|div|br|li|tr|td|h[1-6]|table)[^>]*>", "\n", raw)
        raw = htmlmod.unescape(TAG_RE.sub(" ", raw))
        raw = re.sub(r"[ \t]+\n", "\n", raw)
    return raw


def add_document(name, data, actor="ui", save_file=True):
    if len(data) > 60 * 1024 * 1024:
        raise UserError("Файл больше 60 МБ")
    text = clean_text(extract_text(name, data))
    meta = doc_meta(text)
    did = S.new_id("d_")
    fpath = ""
    if save_file:
        safe = re.sub(r"[^\w.\-]+", "_", name or "document")[:120]
        fpath = S.path("documents", did + "_" + safe)
        with open(fpath, "wb") as f:
            f.write(data)
    S.x("INSERT INTO documents(id,name,size,path,text,meta,added_at,actor) VALUES(?,?,?,?,?,?,?,?)",
        (did, name, len(data), fpath, text, json.dumps(meta, ensure_ascii=False), S.now_iso(), actor))
    doc = get_document(did)
    doc["case_events"] = match_cases_document(doc, actor=actor)
    S.audit(actor, "document.add", {"id": did, "name": name, "chars": len(text)})
    emit("document.added", {"id": did, "name": name, "meta": meta, "case_events": len(doc["case_events"])})
    return doc


def get_document(did, with_text=True):
    r = S.q("SELECT * FROM documents WHERE id=?", (did,), one=True)
    if not r:
        raise UserError("Документ не найден: %s" % did)
    d = S.row(r, ("meta", "llm"))
    if not with_text:
        d.pop("text", None)
    return d


def list_documents():
    out = []
    for r in S.q("SELECT id,name,size,meta,added_at,actor,llm,length(text) chars FROM documents ORDER BY added_at DESC"):
        out.append(S.row(r, ("meta", "llm")))
    return out


def delete_document(did, actor="ui"):
    d = get_document(did, with_text=False)
    S.x("DELETE FROM documents WHERE id=?", (did,))
    try:
        if d.get("path"):
            import os
            os.remove(d["path"])
    except Exception:
        pass
    S.audit(actor, "document.delete", {"id": did})
    return {"ok": True}


def analyze_text(text, terms_any=(), terms_all=(), exclude=(), regex="", ctx=220, max_snips=12):
    terms = list(terms_any) + list(terms_all)
    ok = True
    if any(has_term(text, w) for w in exclude):
        ok = False
    if any(not has_term(text, w) for w in terms_all):
        ok = False
    if terms_any and not any(has_term(text, w) for w in terms_any):
        ok = False
    if regex:
        try:
            if not re.search(regex, text, re.I):
                ok = False
        except re.error as e:
            raise UserError("Ошибка regex: %s" % e)
    counts, snips = {}, []
    for w in terms:
        rx, c = term_re(w), 0
        for m in rx.finditer(text):
            c += 1
            if len(snips) < max_snips and not any(s["a"] <= m.start() < s["b"] for s in snips):
                a, b = max(0, m.start() - ctx), min(len(text), m.end() + ctx)
                snips.append({"term": w, "a": a, "b": b, "text": re.sub(r"\s+", " ", text[a:b]).strip()})
            if c >= 2000:
                break
        counts[w] = c
    return {"ok": ok if (terms or regex) else True, "counts": counts, "snippets": snips}


def analyze_documents(doc_ids=None, query=None, mode="any", profile_id=None, ctx=220):
    cfg = S.get_config()
    if profile_id:
        p = next((x for x in cfg["profiles"] if x["id"] == profile_id), None)
        if not p:
            raise UserError("Профиль не найден")
        t_any, t_all, ex, rx = norm_list(p.get("any_words")), norm_list(p.get("all_words")), norm_list(p.get("exclude_words")), p.get("case_regex") or ""
        label = "профиль: " + p["name"]
    else:
        w = norm_list(query)
        t_any, t_all = (w, []) if mode != "all" else ([], w)
        ex, rx = [], ""
        label = "; ".join(w) + (" (все слова)" if mode == "all" else "")
    ids = doc_ids or [r["id"] for r in S.q("SELECT id FROM documents ORDER BY added_at DESC")]
    res = []
    for did in ids:
        d = get_document(did)
        a = analyze_text(d["text"] or "", t_any, t_all, ex, rx, ctx)
        res.append({"id": did, "name": d["name"], "meta": d["meta"], "chars": len(d["text"] or ""), **a})
    res.sort(key=lambda r: (not r["ok"], r["name"] or ""))
    return {"query": label, "results": res}


# ================================================================ local search

def local_search(query, scopes=("items", "documents", "cases", "events"), limit=100):
    terms = norm_list(query) or ([query.strip()] if query and query.strip() else [])
    if not terms:
        raise UserError("Пустой запрос")
    out = {"query": query, "items": [], "documents": [], "cases": [], "events": []}
    if "items" in scopes:
        for r in S.q("SELECT * FROM items ORDER BY COALESCE(pub, first_seen) DESC"):
            it = item_from_row(r)
            if all(has_term(item_text(it), t) for t in terms):
                out["items"].append(it)
                if len(out["items"]) >= limit:
                    break
    if "documents" in scopes:
        for r in S.q("SELECT id,name,meta,text,added_at FROM documents ORDER BY added_at DESC"):
            t = r["text"] or ""
            if all(has_term(t, w) for w in terms) or all(has_term(r["name"] or "", w) for w in terms):
                a = analyze_text(t, terms, [], [], "", 160, 3)
                out["documents"].append({"id": r["id"], "name": r["name"], "meta": json.loads(r["meta"] or "{}"),
                                         "added_at": r["added_at"], "counts": a["counts"], "snippets": a["snippets"]})
                if len(out["documents"]) >= limit:
                    break
    if "cases" in scopes:
        for c in list_cases():
            blob = " ".join([c["title"], c.get("notes") or "", c.get("court") or ""] + c["numbers"] + c["ncn"] + c["parties"] + c["keywords"] + c["tags"])
            if all(has_term(blob, t) for t in terms):
                out["cases"].append(c)
    if "events" in scopes:
        for r in S.q("SELECT e.*, c.title case_title FROM case_events e JOIN cases c ON c.id=e.case_id ORDER BY e.created_at DESC"):
            if all(has_term((r["title"] or "") + " " + (r["details"] or ""), t) for t in terms):
                out["events"].append(dict(r))
                if len(out["events"]) >= limit:
                    break
    out["total"] = sum(len(out[k]) for k in ("items", "documents", "cases", "events"))
    return out


# ================================================================ meta search (ссылки на базы)

LRS = "https://legalref.judiciary.hk"


def query_string(p, court=False, quote_parties=True):
    a = []
    if p.get("phrase"):
        a.append('"%s"' % p["phrase"])
    words = norm_list(p.get("words"))
    if words:
        a.append(" ".join('"%s"' % w if " " in w else w for w in words))
    if p.get("case"):
        a.append(p["case"])
    if p.get("ncn"):
        a.append('"%s"' % p["ncn"])
    if p.get("parties"):
        a.append('"%s"' % p["parties"] if quote_parties else p["parties"])
    if court and p.get("court"):
        a.append('"%s"' % p["court"])
    return " ".join(a).strip()


def lrs_ncn_url(ncn):
    return LRS + "/lrs/common/search/jud_search_ncn.jsp?" + urlencode({"isadvsearch": "1", "ncnLanguage": "en", "ncnParagraph": "", "ncnValue": ncn,
                                                                      "query": "Go!", "selDatabase": "JU", "selall": "0", "txtSearch": ncn, "txtselectopt": "4"})


def lrs_parties_url(name):
    base = [("vm", "GO!"), ("day1", ""), ("isadvsearch", "1"), ("month", ""), ("selall2", "1"), ("selcourtname", ""), ("selcourtype", ""), ("stem", "1")]
    base += [("txtSearch", "")] + [("txtSearch%d" % i, name if i == 5 else "") for i in range(1, 10)]
    base += [("txtselectopt", "1")] + [("txtselectopt%d" % i, v) for i, v in zip(range(1, 10), ["2", "3", "5", "6", "7", "8", "9", "10", "4"])]
    base += [("year", "")] + [("selDatabase2", d) for d in ("JU", "RV", "RS", "PD")]
    return LRS + "/lrs/common/search/search_result_form.jsp?" + urlencode(base)


def google(q):
    return "https://www.google.com/search?q=" + quote(q)


BUILTIN_SOURCES = [
    {"id": "lrs", "name": "Legal Reference System (LRS)", "kind": "free",
     "desc": "Официальный первоисточник: решения всех уровней судов, Reasons for Verdict/Sentence, Press Summaries. Выложены не все решения."},
    {"id": "cfa", "name": "Court of Final Appeal (hkcfa.hk)", "kind": "free",
     "desc": "Решения CFA: текущий и прошлый месяц, архив с 01.01.2015; тексты — на LRS."},
    {"id": "hklii", "name": "HKLII", "kind": "free",
     "desc": "Бесплатный доступ к решениям и законодательству. Только личное использование; автоматический доступ запрещён — только ручной поиск."},
    {"id": "vlex", "name": "vLex", "kind": "paid",
     "desc": "Решения Гонконга с 1971 г., более 100 000 дел; e-mail-оповещения. Часть страниц открыта на vlex.hk."},
    {"id": "westlaw", "name": "Westlaw Hong Kong / Asia", "kind": "paid",
     "desc": "HKLRD, HKCFAR, Unreported Judgments (ежедневно), Case Analysis со статусом решений."},
    {"id": "lexis", "name": "Lexis+ Hong Kong", "kind": "paid",
     "desc": "Hong Kong Cases с 1842 г., HK Cases Unreported с 1905 г., ситатор CaseBase; оповещения."},
]


def source_links(p):
    q = query_string(p)
    qp = query_string(p, quote_parties=False)
    out = []
    for s in BUILTIN_SOURCES:
        acts = []
        sid = s["id"]
        if sid == "lrs":
            if p.get("ncn"):
                acts.append({"label": "По NCN", "url": lrs_ncn_url(p["ncn"])})
            if p.get("parties"):
                acts.append({"label": "По сторонам", "url": lrs_parties_url(p["parties"])})
            acts += [{"label": "Поиск LRS", "url": LRS + "/lrs/common/index.jsp?target=judgment&lan=en", "copy": qp},
                     {"label": "Новые решения", "url": LRS + "/lrs/common/index.jsp?target=newjudgments&lan=en"},
                     {"label": "Reasons for Verdict", "url": LRS + "/lrs/common/index.jsp?target=RV&lan=en"},
                     {"label": "Reasons for Sentence", "url": LRS + "/lrs/common/index.jsp?target=RS&lan=en"}]
        elif sid == "cfa":
            acts = [{"label": "Последние решения", "url": "https://www.hkcfa.hk/en/work/cases/recent/index.html"},
                    {"label": "Решения по делам", "url": "https://www.hkcfa.hk/en/work/cases/judgments/index.html"},
                    {"label": "Архив с 2015", "url": "https://www.hkcfa.hk/en/work/cases/archive/index.html"},
                    {"label": "Google по hkcfa.hk", "url": google("site:hkcfa.hk " + q)}]
        elif sid == "hklii":
            acts = [{"label": "Открыть поиск HKLII", "url": "https://www.hklii.hk/en/", "copy": qp},
                    {"label": "District Court", "url": "https://www.hklii.hk/en/cases/hkdc/"}]
        elif sid == "vlex":
            acts = [{"label": "vLex Hong Kong", "url": "https://vlex.com/hong-kong-law/", "copy": q},
                    {"label": "Google по vlex.hk", "url": google("site:vlex.hk " + q)}]
        elif sid == "westlaw":
            acts = [{"label": "Westlaw Asia (вход)", "url": "https://www.westlawasia.com/", "copy": q},
                    {"label": "Описание контента", "url": "https://www.thomsonreuters.com.hk/en/products-services/legal-overview/westlaw-hong-kong/content.html"}]
        elif sid == "lexis":
            acts = [{"label": "LexisNexis HK", "url": "https://www.lexisnexis.com/en-hk", "copy": q},
                    {"label": "Как настроить оповещения", "url": "https://www.lexisnexis.com/supportandtraining/hk/b/hk-resources/posts/alerts"}]
        out.append(dict(s, custom=False, actions=acts))
    for c in S.get_config().get("custom_sources") or []:
        tpl = c.get("search") or c.get("home")
        acts = []
        if tpl and "{q}" in tpl:
            acts.append({"label": "Искать", "url": tpl.replace("{q}", quote(q))})
        elif tpl:
            acts.append({"label": "Открыть поиск", "url": tpl, "copy": q})
        if c.get("home") and c["home"] != tpl:
            acts.append({"label": "Главная", "url": c["home"]})
        out.append({"id": "c_" + c["id"], "name": c["name"], "kind": c.get("kind") or "free", "desc": c.get("desc") or "",
                    "custom": True, "actions": acts})
    return {"query": q, "sources": out}


# ================================================================ mail

def smtp_ready():
    sm = S.get_config()["smtp"]
    return bool(sm.get("host") and norm_list(sm.get("to")) and (not sm.get("user") or S.has_secret("smtp_password")))


def send_mail(subject, text, html_body, to=None):
    sm = S.get_config()["smtp"]
    to = norm_list(to) or norm_list(sm.get("to"))
    if not sm.get("host") or not to:
        raise UserError("SMTP не настроен: укажите сервер и получателей (вкладка «Рассылка»)")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sm.get("from") or sm.get("user")
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    port, sec = int(sm.get("port") or 465), sm.get("security", "ssl")
    ctx = ssl.create_default_context()
    try:
        s = smtplib.SMTP_SSL(sm["host"], port, context=ctx, timeout=30) if sec == "ssl" else smtplib.SMTP(sm["host"], port, timeout=30)
        if sec == "starttls":
            s.starttls(context=ctx)
        try:
            if sm.get("user"):
                s.login(sm["user"], S.get_secret("smtp_password"))
            s.sendmail(msg["From"], to, msg.as_string())
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError:
        raise UserError("SMTP: неверный логин или пароль (для Gmail нужен пароль приложения)")
    except (smtplib.SMTPException, OSError) as e:
        raise UserError("SMTP: %s" % e)


NOTE = ("Источник: официальные RSS-ленты Legal Reference System, Судебная власть САР Гонконг. "
        "Только метаданные и ссылки; тексты решений — на сайте источника. Частное некоммерческое использование.")


def _wrap(title, body, note=NOTE, ai=""):
    aih = ("<div style='background:#f3f6fa;border-left:3px solid #0f7b8a;padding:8px 12px;margin:10px 0'><b>Резюме (сгенерировано ИИ, требует проверки)</b><br>%s</div>" % esc(ai).replace("\n", "<br>")) if ai else ""
    return "<div style='font:13px Arial'><h2 style='font:600 17px Arial'>%s</h2>%s%s<p style='color:#666;font-size:12px;margin-top:18px'>%s</p></div>" % (esc(title), aih, body, esc(note))


def build_digest(profile_ids=None, only_unsent=True):
    cfg = S.get_config()
    out = []
    items = [item_from_row(r) for r in S.q("SELECT * FROM items ORDER BY COALESCE(pub, first_seen) DESC")]
    for p in cfg["profiles"]:
        if profile_ids is not None and p["id"] not in profile_ids:
            continue
        if profile_ids is None and not p.get("email"):
            continue
        rows = [it for it in items if p["id"] in (it.get("matches") or {}) and (not only_unsent or p["id"] not in (it.get("sent") or []))]
        if rows:
            out.append((p, rows))
    return out


def send_digest(profile_ids=None, only_unsent=True, dry_run=False, actor="ui"):
    dig = build_digest(profile_ids, only_unsent)
    total = sum(len(r) for _, r in dig)
    if not total:
        return {"sent": 0, "count": 0, "message": "Новых совпадений для рассылки нет"}
    today = datetime.now().strftime("%d.%m.%Y")
    ph, pt = [], []
    for p, rows in dig:
        pt.append("\n== %s (%d) ==" % (p["name"], len(rows)))
        ph.append("<h3 style='font:600 15px Arial;margin:18px 0 6px'>%s — %d</h3><table cellpadding='6' style='border-collapse:collapse;font:13px Arial'>"
                  "<tr style='border-bottom:2px solid #0f2a44;text-align:left'><th>Дата</th><th>Лента</th><th>Решение / реквизиты</th><th>Совпадения</th></tr>" % (esc(p["name"]), len(rows)))
        for it in rows:
            d = ru_date(it.get("pub") or it.get("first_seen"))
            pt.append("- %s | %s | %s\n  %s" % (d, it.get("feed_name") or "", it.get("title") or "", it.get("link") or ""))
            ph.append("<tr style='border-bottom:1px solid #ddd;vertical-align:top'><td style='white-space:nowrap'>%s</td><td>%s</td><td><a href='%s'>%s</a><br><span style='color:#666'>%s</span></td><td>%s</td></tr>" % (
                esc(d), esc(it.get("feed_name")), esc(it.get("link")), esc(it.get("title")),
                esc(" · ".join((it.get("ncn") or []) + (it.get("actions") or []))), esc(", ".join((it.get("matches") or {}).get(p["id"], [])))))
        ph.append("</table>")
    ai = ""
    cfg = S.get_config()
    if cfg["llm"].get("enabled") and cfg["llm"].get("digest_summary"):
        try:
            from . import llm
            ai = llm.digest_summary(dig, actor=actor)
        except Exception as e:
            S.log("ИИ-резюме дайджеста не получено: %s" % e, "WARN")
    subject = "HK Case Law: %d %s по профилям — %s" % (total, plural(total, "новое решение", "новых решения", "новых решений"), today)
    html_body = _wrap("Новые решения судов Гонконга — " + today, "".join(ph), ai=ai)
    text = "Новые решения судов Гонконга по профилям поиска (%s)\n%s%s\n\n%s" % (today, ("\nРезюме (ИИ, требует проверки):\n" + ai + "\n") if ai else "", "\n".join(pt), NOTE)
    if dry_run:
        return {"sent": 0, "count": total, "preview_subject": subject, "preview_html": html_body}
    send_mail(subject, text, html_body)
    con = S.db()
    with con:
        for p, rows in dig:
            for it in rows:
                sent = set(it.get("sent") or [])
                sent.add(p["id"])
                cur = con.execute("SELECT sent FROM items WHERE id=?", (it["id"],)).fetchone()
                if cur:
                    sent |= set(json.loads(cur["sent"] or "[]"))
                    con.execute("UPDATE items SET sent=? WHERE id=?", (json.dumps(sorted(sent)), it["id"]))
    S.audit(actor, "digest.send", {"count": total})
    emit("digest.sent", {"count": total, "subject": subject})
    return {"sent": total, "count": total, "message": "Отправлено позиций: %d" % total}


def reset_sent(actor="ui"):
    S.x("UPDATE items SET sent='[]'")
    S.audit(actor, "digest.reset_sent", {})
    return {"ok": True}


def send_case_alerts(dry_run=False, actor="scheduler"):
    rows = [dict(r) for r in S.q("SELECT e.*, c.title case_title, c.priority FROM case_events e JOIN cases c ON c.id=e.case_id WHERE e.notified=0 AND e.type NOT IN ('status') AND c.status='active' ORDER BY c.title, e.created_at")]
    if not rows:
        return {"sent": 0, "count": 0, "message": "Новых событий по делам нет"}
    today = datetime.now().strftime("%d.%m.%Y")
    trs, txt = [], []
    for e in rows:
        trs.append("<tr style='border-bottom:1px solid #ddd;vertical-align:top'><td><b>%s</b></td><td>%s</td><td style='white-space:nowrap'>%s</td><td>%s%s</td></tr>" % (
            esc(e["case_title"]), esc(EVENT_TYPES.get(e["type"], e["type"])), esc(ru_date(e.get("date"))),
            ("<a href='%s'>%s</a>" % (esc(e["link"]), esc(e["title"]))) if e.get("link") else esc(e["title"]),
            ("<br><span style='color:#666'>%s</span>" % esc((e.get("details") or "")[:400]).replace("\n", "<br>")) if e.get("details") else ""))
        txt.append("- [%s] %s: %s %s" % (e["case_title"], EVENT_TYPES.get(e["type"], e["type"]), e["title"], e.get("link") or ""))
    body = ("<table cellpadding='6' style='border-collapse:collapse;font:13px Arial'><tr style='border-bottom:2px solid #0f2a44;text-align:left'>"
            "<th>Дело</th><th>Событие</th><th>Дата</th><th>Описание</th></tr>%s</table>" % "".join(trs))
    n = len(rows)
    subject = "HK Case Law: %d %s по делам на контроле — %s" % (n, plural(n, "новое событие", "новых события", "новых событий"), today)
    html_body = _wrap("Изменения по делам на контроле — " + today, body, note="Сформировано HK Case Law Monitor. Частное некоммерческое использование.")
    if dry_run:
        return {"sent": 0, "count": n, "preview_subject": subject, "preview_html": html_body}
    send_mail(subject, "Изменения по делам на контроле (%s)\n%s" % (today, "\n".join(txt)), html_body)
    con = S.db()
    with con:
        for e in rows:
            con.execute("UPDATE case_events SET notified=1 WHERE id=?", (e["id"],))
    S.audit(actor, "cases.alert_sent", {"count": n})
    return {"sent": n, "count": n, "message": "Отправлено событий: %d" % n}


def send_report(body, dry_run=False, actor="ui"):
    rows = body.get("rows")
    if rows is None and (body.get("doc_ids") or body.get("query") or body.get("profile_id")):
        a = analyze_documents(body.get("doc_ids"), body.get("query"), body.get("mode", "any"), body.get("profile_id"))
        rows = [{"file": r["name"], "ok": r["ok"], "court": r["meta"].get("court"), "ncn": r["meta"].get("ncn"), "acts": r["meta"].get("acts"),
                 "date": r["meta"].get("date"), "before": r["meta"].get("before"),
                 "counts": "; ".join("%s: %d" % (k, v) for k, v in r["counts"].items() if v) or "совпадений нет",
                 "snippets": [s["text"] for s in r["snippets"]]} for r in a["results"]]
        body = dict(body, query=a["query"])
    rows = (rows or [])[:300]
    if body.get("only_matching"):
        rows = [r for r in rows if r.get("ok")]
    if not rows:
        return {"sent": 0, "count": 0, "message": "Нет результатов для отправки"}
    today = datetime.now().strftime("%d.%m.%Y")
    n_ok = sum(1 for r in rows if r.get("ok"))
    th = "".join("<th align='left' style='border-bottom:2px solid #0f2a44;padding:6px'>%s</th>" % h for h in ("Документ", "Суд / реквизиты", "Дата / состав", "Совпадения и фрагменты"))
    trs, txt = [], []
    for r in rows:
        req = " · ".join([str(x) for x in (r.get("ncn") or []) + (r.get("acts") or [])][:12])
        trs.append("<tr style='border-bottom:1px solid #ddd;vertical-align:top'><td style='padding:6px'><b>%s</b><br><span style='color:%s'>%s</span></td><td style='padding:6px'>%s<br><span style='color:#666'>%s</span></td><td style='padding:6px'>%s<br><span style='color:#666'>%s</span></td><td style='padding:6px'>%s%s</td></tr>" % (
            esc(str(r.get("file", ""))[:200]), "#1d7a3e" if r.get("ok") else "#888", "соответствует" if r.get("ok") else "не соответствует",
            esc(r.get("court") or "—"), esc(req), esc(r.get("date") or "—"), esc(str(r.get("before") or "")[:300]), esc(r.get("counts") or ""),
            "".join("<div style='border-left:2px solid #ccc;padding-left:8px;margin:4px 0;color:#333'>…%s…</div>" % esc(str(x)[:700]) for x in (r.get("snippets") or [])[:6])))
        txt.append("- %s [%s] %s | %s | %s" % (r.get("file"), "да" if r.get("ok") else "нет", r.get("court") or "", req, r.get("counts") or ""))
    subject = "HK Case Law: анализ решений — %d из %d %s — %s" % (n_ok, len(rows), "соответствует" if n_ok % 10 == 1 and n_ok % 100 != 11 else "соответствуют", today)
    html_body = _wrap("Анализ решений — " + today, "<p>Запрос: <b>%s</b><br>Документов: %d, соответствуют: %d</p><table cellpadding='0' style='border-collapse:collapse;font:13px Arial;width:100%%'><tr>%s</tr>%s</table>" % (
        esc(body.get("query") or "—"), len(rows), n_ok, th, "".join(trs)), note="Отчёт сформирован HK Case Law Monitor по документам, загруженным оператором. Частное некоммерческое использование.")
    if dry_run:
        return {"sent": 0, "count": len(rows), "preview_subject": subject, "preview_html": html_body}
    send_mail(subject, "Анализ решений — %s\nЗапрос: %s\n\n%s" % (today, body.get("query") or "", "\n".join(txt)), html_body)
    S.audit(actor, "report.send", {"count": len(rows)})
    return {"sent": len(rows), "count": len(rows), "message": "Отчёт отправлен: документов %d" % len(rows)}


# ================================================================ notify & webhooks

def _notify(title, message):
    for fn in list(NOTIFIERS):
        try:
            fn(title, message)
        except Exception:
            pass


WEBHOOK_EVENTS = ["items.new", "items.matched", "case.added", "case.event", "document.added", "digest.sent", "cycle.completed", "error"]


def emit(event, data):
    try:
        hooks = [h for h in (S.get_config()["agent"].get("webhooks") or []) if h.get("enabled", True)
                 and (not h.get("events") or "*" in h["events"] or event in h["events"])]
    except Exception:
        return
    if not hooks:
        return
    payload = json.dumps({"event": event, "time": S.now_iso(), "data": data}, ensure_ascii=False, default=str).encode("utf-8")
    secret = S.get_secret("webhook_secret").encode("utf-8")
    sig = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest() if secret else ""

    def go(h):
        for attempt in (1, 2):
            try:
                code, _, _ = http_request(h["url"], data=payload, method="POST", timeout=15,
                                          headers={"Content-Type": "application/json", "X-HKCL-Event": event, "X-HKCL-Signature": sig})
                if code < 400:
                    return
            except Exception as e:
                S.log("Вебхук %s: %s" % (h.get("url"), e), "WARN")
            time.sleep(3)

    for h in hooks:
        threading.Thread(target=go, args=(h,), daemon=True).start()


# ================================================================ cycle

def run_cycle(actor="scheduler", force=False):
    """Полный цикл автоматизации."""
    cfg = S.get_config()
    auto = cfg["automation"]
    res = {"time": S.now_iso(), "actor": actor}
    try:
        if auto.get("check_feeds", True):
            ck = check_feeds(force=force, actor=actor)
            res["check"] = {k: ck[k] for k in ("new", "new_matches")}
            res["check"]["case_events"] = len(ck["case_events"])
        if auto.get("reminders", True):
            res["reminders"] = len(due_reminders(actor))
        if smtp_ready():
            if auto.get("case_alerts", True):
                try:
                    res["case_alerts"] = send_case_alerts(actor=actor)["count"]
                except UserError as e:
                    res["case_alerts_error"] = str(e)
            if auto.get("send_digest"):
                try:
                    res["digest"] = send_digest(actor=actor)["sent"]
                except UserError as e:
                    res["digest_error"] = str(e)
        S.kv_set("last_cycle", res)
        S.audit(actor, "cycle.run", res)
        emit("cycle.completed", res)
        return res
    except Exception as e:
        S.log("Ошибка цикла автоматизации: %s" % e, "ERROR")
        emit("error", {"where": "cycle", "error": str(e)})
        res["error"] = str(e)
        S.kv_set("last_cycle", res)
        return res
