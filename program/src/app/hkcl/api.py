# -*- coding: utf-8 -*-
"""Локальный HTTP-сервер: интерфейс программы + REST API v1 для окна программы и ИИ-агентов."""

import base64
import hmac
import io
import json
import os
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import APP_NAME, VERSION, COPYRIGHT, AUTHOR_RU, AUTHOR_EN, HOMEPAGE
from . import store as S
from . import core, automation, llm
from .core import UserError

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
ROUTES = []
SERVER = {"httpd": None, "port": None, "on_shutdown": None, "on_show": None}


def route(method, pattern, perm="read", summary="", ui_only=False):
    rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append({"method": method, "pattern": pattern, "rx": rx, "fn": fn, "perm": perm, "summary": summary, "ui_only": ui_only})
        return fn
    return deco


# ================================================================ helpers

PUBLIC_CFG_SECTIONS = ("feeds", "profiles", "smtp", "custom_sources", "extra_feed_hosts", "settings", "automation", "agent", "llm")


def public_config():
    cfg = S.get_config()
    out = {k: cfg.get(k) for k in PUBLIC_CFG_SECTIONS}
    out["secrets"] = {k: S.has_secret(k) for k in ("smtp_password", "llm_api_key", "webhook_secret")}
    return out


def apply_config(body, actor):
    cfg = S.get_config()
    changed = []
    if "feeds" in body:
        hosts_cfg = dict(cfg, extra_feed_hosts=body.get("extra_feed_hosts", cfg.get("extra_feed_hosts")))
        new = core.validate_feeds(list(body["feeds"]), hosts_cfg)
        if actor != "ui":
            old = {f["id"]: f for f in cfg["feeds"]}
            for f in new:
                if f.get("robots_override") and not old.get(f.get("id"), {}).get("robots_override"):
                    _check_override_allowed(actor)
                    S.audit(actor, "feed.robots_override", {"feed": f.get("name"), "url": f.get("url")})
        cfg["feeds"] = new
        changed.append("feeds")
    if "extra_feed_hosts" in body:
        cfg["extra_feed_hosts"] = core.clean_hosts(body["extra_feed_hosts"])
        changed.append("extra_feed_hosts")
    if "profiles" in body:
        cfg["profiles"] = core.validate_profiles(list(body["profiles"]))
        changed.append("profiles")
    if "custom_sources" in body:
        cfg["custom_sources"] = core.validate_sources(list(body["custom_sources"]))
        changed.append("custom_sources")
    if "smtp" in body:
        sm = dict(cfg["smtp"], **{k: v for k, v in body["smtp"].items() if k in ("host", "port", "security", "user", "from", "to")})
        sm["to"] = core.norm_list(sm.get("to"))
        sm["port"] = int(sm.get("port") or 465)
        if sm.get("security") not in ("ssl", "starttls", "none"):
            raise UserError("SMTP: защита — ssl, starttls или none")
        cfg["smtp"] = sm
        changed.append("smtp")
    for sec in ("settings", "automation", "agent", "llm"):
        if sec in body and isinstance(body[sec], dict):
            if sec == "agent" and actor != "ui":
                raise UserError("Настройки доступа агента меняет только оператор в окне программы")
            cfg[sec] = dict(cfg[sec], **body[sec])
            changed.append(sec)
    if "automation" in body:
        automation.validate_automation(cfg["automation"])
    if "llm" in body:
        if cfg["llm"].get("provider") not in llm.PROVIDERS:
            raise UserError("Неизвестный провайдер LLM")
        cfg["llm"]["max_tokens"] = max(64, min(64000, int(cfg["llm"].get("max_tokens") or 2000)))
    if "agent" in body:
        if cfg["agent"].get("permissions") not in ("full", "standard", "readonly"):
            raise UserError("Права агента: full, standard или readonly")
        hooks = []
        for h in cfg["agent"].get("webhooks") or []:
            if not re.match(r"^https?://", str(h.get("url") or "")):
                raise UserError("Адрес вебхука должен начинаться с http(s)://")
            hooks.append({"id": h.get("id") or S.new_id("w_"), "url": h["url"], "enabled": bool(h.get("enabled", True)),
                          "events": [e for e in (h.get("events") or ["*"]) if e == "*" or e in core.WEBHOOK_EVENTS]})
        cfg["agent"]["webhooks"] = hooks
    S.save_config(cfg)
    for name in ("smtp_password", "llm_api_key", "webhook_secret"):
        if body.get(name):
            S.set_secret(name, body[name])
            changed.append("secret:" + name)
        if body.get("clear_" + name):
            S.set_secret(name, "")
            changed.append("clear:" + name)
    if "profiles" in changed:
        core.recompute_matches(cfg)
    S.audit(actor, "config.update", {"sections": changed})
    return public_config()


def _check_override_allowed(actor):
    ag = S.get_config()["agent"]
    if actor != "ui" and not (ag.get("permissions") == "full" and ag.get("allow_robots_override")):
        raise PermissionError("Решение по robots.txt агенту не разрешено (настройки ИИ-агента)")


def b(body, key, default=None):
    v = body.get(key, default)
    return v


# ================================================================ routes

@route("GET", "/api/v1/ping", "public", "Проверка доступности (без токена)")
def r_ping(h, body, qs, actor):
    return {"app": APP_NAME, "version": VERSION}


@route("GET", "/api/v1/status", "read", "Состояние программы")
def r_status(h, body, qs, actor):
    cfg = S.get_config()
    return {"app": APP_NAME, "version": VERSION, "data_dir": S.data_dir(), "port": SERVER["port"],
            "items": S.q("SELECT COUNT(*) n FROM items", one=True)["n"],
            "matched": S.q("SELECT COUNT(*) n FROM items WHERE matches!='{}'", one=True)["n"],
            "cases_active": S.q("SELECT COUNT(*) n FROM cases WHERE status='active'", one=True)["n"],
            "events_unseen": S.q("SELECT COUNT(*) n FROM case_events WHERE seen=0", one=True)["n"],
            "documents": S.q("SELECT COUNT(*) n FROM documents", one=True)["n"],
            "smtp_ready": core.smtp_ready(), "llm_enabled": bool(cfg["llm"].get("enabled")),
            "automation": automation.status(), "update": S.kv_get("update"),
            "allowed_hosts": sorted(core.allowed_hosts(cfg)), "user_agent": core.USER_AGENT}


@route("GET", "/api/v1/config", "read", "Конфигурация (без секретов)")
def r_cfg(h, body, qs, actor):
    return public_config()


@route("POST", "/api/v1/config", "write", "Изменение разделов конфигурации; секреты: smtp_password, llm_api_key, webhook_secret")
def r_cfg_set(h, body, qs, actor):
    return apply_config(body, actor)


@route("GET", "/api/v1/feeds", "read", "Ленты и их состояние")
def r_feeds(h, body, qs, actor):
    return {"feeds": S.get_config()["feeds"], "state": core.feed_states(), "allowed_hosts": sorted(core.allowed_hosts())}


@route("POST", "/api/v1/feeds/check", "write", "Проверить ленты сейчас {force}")
def r_check(h, body, qs, actor):
    return core.check_feeds(force=bool(body.get("force")), actor=actor)


@route("POST", "/api/v1/feeds/robots", "read", "Статус robots.txt для URL {url}")
def r_robots(h, body, qs, actor):
    ok, why = core.feed_url_ok(body.get("url", ""))
    if not ok:
        return {"status": "rejected", "note": why}
    st, note = core.robots_status(body["url"])
    return {"status": st, "note": note}


@route("POST", "/api/v1/feeds/{fid}/override", "operator", "Решение оператора: опрашивать ленту при запрете robots.txt {enabled}")
def r_override(h, body, qs, actor, fid):
    _check_override_allowed(actor)
    enabled = bool(body.get("enabled"))

    def fn(c):
        f = next((x for x in c["feeds"] if x["id"] == fid), None)
        if not f:
            raise UserError("Лента не найдена")
        f["robots_override"] = enabled
        f["override_at"] = S.now_iso() if enabled else None
        f["override_by"] = actor if enabled else None
    S.update_config(fn)
    S.audit(actor, "feed.robots_override", {"feed": fid, "enabled": enabled})
    return {"ok": True, "feed_id": fid, "robots_override": enabled}


@route("GET", "/api/v1/items", "read", "Решения из лент: matched, profile, feed, text, days, unsent, limit, offset")
def r_items(h, body, qs, actor):
    g = lambda k, d=None: qs.get(k, [d])[0]
    return core.list_items(matched=g("matched") == "1", profile=g("profile"), feed=g("feed"), text=g("text"),
                           days=g("days") or None, limit=int(g("limit", 2000)), offset=int(g("offset", 0)), unsent=g("unsent") == "1")


@route("GET", "/api/v1/profiles", "read", "Профили поиска")
def r_profiles(h, body, qs, actor):
    return {"profiles": S.get_config()["profiles"]}


@route("POST", "/api/v1/profiles", "write", "Заменить список профилей {profiles}")
def r_profiles_set(h, body, qs, actor):
    return {"profiles": apply_config({"profiles": body.get("profiles") or []}, actor)["profiles"]}


@route("GET", "/api/v1/sources/links", "read", "Ссылки поиска по базам: words, phrase, parties, ncn, case, court")
def r_links(h, body, qs, actor):
    p = {k: qs.get(k, [""])[0] for k in ("words", "phrase", "parties", "ncn", "case", "court")}
    return core.source_links(p)


@route("GET", "/api/v1/search", "read", "Поиск по собранной базе: q (термины через «;»), scopes")
def r_search(h, body, qs, actor):
    sc = tuple(x for x in (qs.get("scopes", ["items,documents,cases,events"])[0]).split(",") if x)
    return core.local_search(qs.get("q", [""])[0], sc, int(qs.get("limit", ["100"])[0]))


# ---- cases

@route("GET", "/api/v1/cases", "read", "Дела на контроле: status=active|closed")
def r_cases(h, body, qs, actor):
    return {"cases": core.list_cases(status=qs.get("status", [None])[0])}


@route("POST", "/api/v1/cases", "write", "Поставить дело на контроль {title, numbers, ncn, parties, keywords, court, priority, notes, remind_at, tags}")
def r_case_add(h, body, qs, actor):
    return core.add_case(body, actor=actor)


@route("POST", "/api/v1/cases/from-item", "write", "Поставить на контроль по решению из ленты {item_id}")
def r_case_from_item(h, body, qs, actor):
    r = S.q("SELECT * FROM items WHERE id=?", (body.get("item_id"),), one=True)
    if not r:
        raise UserError("Решение не найдено")
    it = core.item_from_row(r)
    parties = re.sub(r"^\s*[A-Z]{3,6}\s?\d+[A-Z]?/\d{4}\s*", "", it.get("title") or "").strip()
    return core.add_case({"title": it.get("title"), "numbers": it.get("actions"), "ncn": it.get("ncn"),
                          "parties": [parties] if parties and len(parties) < 200 and not it.get("actions") and not it.get("ncn") else [],
                          "notes": "Поставлено на контроль по решению из ленты «%s»." % (it.get("feed_name") or "")}, actor=actor)


@route("GET", "/api/v1/cases/{cid}", "read", "Дело и хронология событий")
def r_case(h, body, qs, actor, cid):
    return core.get_case(cid)


@route("POST", "/api/v1/cases/{cid}", "write", "Изменить дело (частично)")
def r_case_upd(h, body, qs, actor, cid):
    return core.update_case(cid, body, actor=actor)


@route("DELETE", "/api/v1/cases/{cid}", "operator", "Удалить дело")
def r_case_del(h, body, qs, actor, cid):
    return core.delete_case(cid, actor=actor)


@route("POST", "/api/v1/cases/{cid}/events", "write", "Добавить событие {type: note|hearing|deadline|agent|..., title, details, date, link}")
def r_case_ev(h, body, qs, actor, cid):
    core.get_case(cid)
    ev = core.add_event(cid, body.get("type") or "note", body.get("title") or "Событие", body.get("details") or "",
                        body.get("date"), body.get("link") or "", actor=actor, seen=1 if actor == "ui" else 0)
    S.audit(actor, "case.event", {"case": cid, "type": body.get("type")})
    return ev


@route("POST", "/api/v1/cases/{cid}/seen", "write", "Отметить события просмотренными {event_ids?}")
def r_case_seen(h, body, qs, actor, cid):
    return core.mark_seen(cid, body.get("event_ids"), actor)


@route("GET", "/api/v1/events/unseen", "read", "Непросмотренные события по всем делам")
def r_unseen(h, body, qs, actor):
    return {"events": core.unseen_events()}


# ---- documents

@route("GET", "/api/v1/documents", "read", "Загруженные документы")
def r_docs(h, body, qs, actor):
    return {"documents": core.list_documents()}


@route("POST", "/api/v1/documents", "write", "Загрузить документ {name, content_base64} или {path} (локальный файл)")
def r_doc_add(h, body, qs, actor):
    if body.get("path"):
        p = os.path.abspath(str(body["path"]))
        if not os.path.isfile(p):
            raise UserError("Файл не найден: %s" % p)
        with open(p, "rb") as f:
            data = f.read()
        name = body.get("name") or os.path.basename(p)
    else:
        try:
            data = base64.b64decode(body.get("content_base64") or "", validate=False)
        except Exception:
            raise UserError("content_base64: некорректные данные")
        name = body.get("name") or "document"
    if not data:
        raise UserError("Пустой файл")
    d = core.add_document(name, data, actor=actor)
    d["text_preview"] = (d.pop("text", "") or "")[:1500]
    if S.get_config()["automation"].get("llm_summarize_new_docs") and S.get_config()["llm"].get("enabled"):
        threading.Thread(target=lambda: _safe(llm.summarize_document, d["id"], "management", "scheduler"), daemon=True).start()
    return d


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception as e:
        S.log("Фоновая задача ИИ: %s" % e, "WARN")


@route("GET", "/api/v1/documents/{did}", "read", "Документ с текстом (text=0 — без текста)")
def r_doc(h, body, qs, actor, did):
    return core.get_document(did, with_text=qs.get("text", ["1"])[0] != "0")


@route("DELETE", "/api/v1/documents/{did}", "operator", "Удалить документ")
def r_doc_del(h, body, qs, actor, did):
    return core.delete_document(did, actor=actor)


@route("POST", "/api/v1/documents/analyze", "read", "Анализ документов {doc_ids?, query?, mode: any|all, profile_id?, ctx?}")
def r_doc_an(h, body, qs, actor):
    return core.analyze_documents(body.get("doc_ids"), body.get("query"), body.get("mode", "any"), body.get("profile_id"), int(body.get("ctx") or 220))


# ---- mail

@route("POST", "/api/v1/digest", "write", "Дайджест по профилям {dry_run, profiles?, all?}")
def r_digest(h, body, qs, actor):
    return core.send_digest(body.get("profiles"), not body.get("all"), bool(body.get("dry_run")), actor=actor)


@route("POST", "/api/v1/digest/reset", "write", "Сбросить отметки отправки")
def r_digest_reset(h, body, qs, actor):
    return core.reset_sent(actor)


@route("POST", "/api/v1/alerts/cases", "write", "Письмо об изменениях по делам {dry_run}")
def r_alerts(h, body, qs, actor):
    return core.send_case_alerts(bool(body.get("dry_run")), actor=actor)


@route("POST", "/api/v1/report", "write", "Отчёт анализа документов {doc_ids|query|profile_id|rows, only_matching, dry_run}")
def r_report(h, body, qs, actor):
    return core.send_report(body, bool(body.get("dry_run")), actor=actor)


@route("POST", "/api/v1/mail/test", "write", "Тестовое письмо")
def r_mail_test(h, body, qs, actor):
    core.send_mail("HK Case Law Monitor: тестовое письмо", "Тест SMTP-настроек прошёл успешно.", "<p>Тест SMTP-настроек прошёл успешно.</p>")
    return {"ok": True, "message": "Тестовое письмо отправлено"}


# ---- automation

@route("GET", "/api/v1/automation", "read", "Состояние автоматизации")
def r_auto(h, body, qs, actor):
    return automation.status()


@route("POST", "/api/v1/automation/run", "write", "Запустить цикл автоматизации сейчас {force}")
def r_auto_run(h, body, qs, actor):
    return core.run_cycle(actor=actor, force=bool(body.get("force")))


@route("POST", "/api/v1/automation/windows-task", "write", "Задача в Планировщике Windows {install: true|false}")
def r_wtask(h, body, qs, actor):
    return automation.install_windows_task(actor) if body.get("install") else automation.remove_windows_task(actor)


@route("POST", "/api/v1/automation/autostart", "write", "Автозапуск при входе в Windows {enabled}")
def r_autostart(h, body, qs, actor):
    return automation.set_autostart(bool(body.get("enabled")), actor)


# ---- llm

@route("POST", "/api/v1/llm/test", "write", "Проверить подключение к LLM")
def r_llm_test(h, body, qs, actor):
    return llm.test_connection(actor)


@route("POST", "/api/v1/llm/summarize", "write", "ИИ-резюме документа {doc_id, audience: management|legal}")
def r_llm_sum(h, body, qs, actor):
    return llm.summarize_document(body.get("doc_id"), body.get("audience", "management"), actor)


@route("POST", "/api/v1/llm/extract", "write", "ИИ-извлечение структурированных данных {doc_id}")
def r_llm_ext(h, body, qs, actor):
    return llm.extract_document(body.get("doc_id"), actor)


@route("POST", "/api/v1/llm/ask", "write", "Вопрос по документу {doc_id, question}")
def r_llm_ask(h, body, qs, actor):
    return llm.ask_document(body.get("doc_id"), body.get("question"), actor)


@route("POST", "/api/v1/llm/expand", "write", "Расширить поисковый запрос {query}")
def r_llm_exp(h, body, qs, actor):
    return llm.expand_query(body.get("query"), actor)


@route("POST", "/api/v1/llm/case-brief", "write", "ИИ-справка по делу {case_id}")
def r_llm_brief(h, body, qs, actor):
    return llm.case_brief(body.get("case_id"), actor)


@route("GET", "/api/v1/llm/history", "read", "Журнал обращений к LLM")
def r_llm_hist(h, body, qs, actor):
    return {"runs": llm.history()}


# ---- agent / audit / service

@route("GET", "/api/v1/audit", "read", "Журнал аудита действий")
def r_audit(h, body, qs, actor):
    lim = int(qs.get("limit", ["300"])[0])
    who = qs.get("actor", [None])[0]
    sql, args = "SELECT * FROM audit", []
    if who:
        sql += " WHERE actor=?"
        args.append(who)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(lim)
    return {"audit": [S.row(r, ("details",)) for r in S.q(sql, args)]}


@route("GET", "/api/v1/agent/info", "read", "Сведения для подключения агента", ui_only=True)
def r_agent_info(h, body, qs, actor):
    inst = automation.install_dir()
    hkcl = os.path.join(inst, "hkcl.exe")
    return {"token": S.api_token(), "base_url": "http://127.0.0.1:%d/api/v1" % SERVER["port"],
            "openapi": "http://127.0.0.1:%d/api/v1/openapi.json" % SERVER["port"],
            "mcp_command": hkcl, "mcp_args": ["mcp"],
            "claude_desktop_config": {"mcpServers": {"hk-case-law": {"command": hkcl, "args": ["mcp"]}}},
            "webhook_events": core.WEBHOOK_EVENTS, "cli": hkcl + " --help"}


@route("POST", "/api/v1/agent/token/rotate", "operator", "Выпустить новый токен API", ui_only=True)
def r_rotate(h, body, qs, actor):
    tok = S.api_token(rotate=True)
    S.audit(actor, "agent.token_rotate", {})
    return {"token": tok}


@route("POST", "/api/v1/webhooks/test", "write", "Тестовое событие вебхука")
def r_wh_test(h, body, qs, actor):
    core.emit("cycle.completed", {"test": True, "actor": actor})
    return {"ok": True, "message": "Тестовое событие отправлено на включённые вебхуки"}


@route("GET", "/api/v1/update/check", "read", "Проверить обновления")
def r_update(h, body, qs, actor):
    return automation.check_update()


@route("GET", "/api/v1/about", "public", "О программе")
def r_about(h, body, qs, actor):
    return {"app": APP_NAME, "version": VERSION, "copyright": COPYRIGHT, "author_ru": AUTHOR_RU, "author_en": AUTHOR_EN,
            "homepage": HOMEPAGE, "data_dir": S.data_dir(), "install_dir": automation.install_dir()}


@route("GET", "/api/v1/backup", "read", "Резервная копия данных (zip)", ui_only=True)
def r_backup(h, body, qs, actor):
    buf = io.BytesIO()
    S.db().execute("PRAGMA wal_checkpoint(FULL)")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("config.json", "hkcl.sqlite3"):
            p = os.path.join(S.data_dir(), name)
            if os.path.exists(p):
                z.write(p, name)
    S.audit(actor, "backup", {})
    return ("application/zip", buf.getvalue(), "hkcl_backup.zip")


@route("POST", "/api/v1/app/open-folder", "read", "Открыть папку данных", ui_only=True)
def r_open_folder(h, body, qs, actor):
    p = S.data_dir()
    if os.name == "nt":
        os.startfile(p)
    return {"ok": True, "path": p}


@route("POST", "/api/v1/app/show", "read", "Показать окно программы", ui_only=True)
def r_show(h, body, qs, actor):
    if SERVER.get("on_show"):
        threading.Thread(target=SERVER["on_show"], daemon=True).start()
    return {"ok": True}


@route("POST", "/api/v1/app/open-url", "read", "Открыть ссылку в системном браузере {url}", ui_only=True)
def r_open_url(h, body, qs, actor):
    u = str(body.get("url") or "")
    if not re.match(r"^https?://", u):
        raise UserError("Недопустимая ссылка")
    import webbrowser
    webbrowser.open(u)
    return {"ok": True}


def uninstaller_path():
    p = os.path.join(automation.install_dir(), "Uninstall.exe")
    return p if os.path.isfile(p) else None


@route("GET", "/api/v1/app/uninstall", "read", "Доступность удаления программы", ui_only=True)
def r_uninstall_info(h, body, qs, actor):
    return {"available": bool(uninstaller_path()), "install_dir": automation.install_dir(), "data_dir": S.data_dir()}


@route("POST", "/api/v1/app/uninstall", "operator", "Запустить удаление программы", ui_only=True)
def r_uninstall(h, body, qs, actor):
    exe = uninstaller_path()
    if not exe:
        raise UserError("Программа запущена не из установленной копии: модуль удаления (Uninstall.exe) не найден")
    import subprocess
    S.audit(actor, "app.uninstall", {"exe": exe})
    # мастер удаления сам предложит удалить данные; программа завершается, чтобы освободить файлы
    subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    threading.Timer(1.0, lambda: (SERVER["on_shutdown"] or shutdown)()).start()
    return {"ok": True, "message": "Запущен мастер удаления; программа завершается"}


@route("POST", "/api/v1/shutdown", "operator", "Завершить программу", ui_only=False)
def r_shutdown(h, body, qs, actor):
    S.audit(actor, "app.shutdown", {})
    threading.Timer(0.5, lambda: (SERVER["on_shutdown"] or shutdown)()).start()
    return {"ok": True}


@route("GET", "/api/v1/openapi.json", "public", "Спецификация OpenAPI")
def r_openapi(h, body, qs, actor):
    paths = {}
    for r in ROUTES:
        if r["ui_only"]:
            continue
        op = {"summary": r["summary"], "operationId": r["fn"].__name__[2:],
              "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}},
                            "400": {"description": "Ошибка ввода {error}"}, "401": {"description": "Нет токена"}, "403": {"description": "Нет прав"}}}
        if r["perm"] != "public":
            op["security"] = [{"bearer": []}]
        params = [{"name": n, "in": "path", "required": True, "schema": {"type": "string"}} for n in re.findall(r"\{(\w+)\}", r["pattern"])]
        if params:
            op["parameters"] = params
        if r["method"] == "POST":
            op["requestBody"] = {"content": {"application/json": {"schema": {"type": "object"}}}}
        op["x-permission"] = r["perm"]
        paths.setdefault(r["pattern"], {})[r["method"].lower()] = op
    return {"openapi": "3.0.3", "info": {"title": APP_NAME + " API", "version": VERSION,
                                         "description": "Локальный API (127.0.0.1). Авторизация: Authorization: Bearer <токен> (окно «ИИ-агент»). "
                                                        "Права: read / write / operator; режим агента задаётся оператором."},
            "servers": [{"url": "http://127.0.0.1:%d" % (SERVER["port"] or 8767)}],
            "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}}, "paths": paths}


# ================================================================ handler

PERM_RANK = {"public": 0, "read": 1, "write": 2, "operator": 3}
AGENT_MAX = {"readonly": 1, "standard": 2, "full": 3}


class Handler(BaseHTTPRequestHandler):
    server_version = "HKCaseLawMonitor/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _auth(self):
        tok = S.api_token()
        given = self.headers.get("Authorization", "")
        given = given[7:] if given.lower().startswith("bearer ") else self.headers.get("X-HKCL-Token", "")
        return bool(given) and hmac.compare_digest(given, tok)

    def _actor(self):
        a = (self.headers.get("X-HKCL-Actor") or "agent").lower()
        return a if a in ("ui", "agent", "mcp", "cli", "scheduler") else "agent"

    def _static(self, rel):
        rel = rel.lstrip("/") or "index.html"
        full = os.path.abspath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.abspath(WEB_DIR)) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        with open(full, "rb") as f:
            data = f.read()
        ctype = {"html": "text/html; charset=utf-8", "js": "application/javascript", "css": "text/css", "ico": "image/x-icon",
                 "png": "image/png", "svg": "image/svg+xml", "json": "application/json", "txt": "text/plain; charset=utf-8"}.get(rel.rsplit(".", 1)[-1], "application/octet-stream")
        if rel == "index.html":
            boot = "<script>window.HKCL=%s;</script>" % json.dumps({"token": S.api_token(), "version": VERSION, "app": APP_NAME,
                                                                     "embedded": self.headers.get("X-HKCL-Embedded") == "1"})
            data = data.replace(b"<!--HKCL_BOOT-->", boot.encode("utf-8"), 1)
        return self._send(200, data, ctype, {"Content-Security-Policy": "default-src 'self' 'unsafe-inline' data: blob:; connect-src 'self'; img-src 'self' data: blob:; frame-src 'self' data: blob:; object-src 'none'"} if rel == "index.html" else None)

    def _handle(self, method):
        # тело читается до любых проверок: иначе остаток тела ломает следующий запрос в keep-alive-соединении
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 80 * 1024 * 1024:
            self.close_connection = True
            return self._send(413, {"error": "слишком большой запрос"})
        self._raw = self.rfile.read(n) if n > 0 else b""
        if not self._host_ok():
            return self._send(403, {"error": "недопустимый Host"})
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            if method != "GET":
                return self._send(405, {"error": "method"})
            return self._static(u.path)
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://127.0.0.1:%d" % SERVER["port"], "http://localhost:%d" % SERVER["port"], "null"):
            return self._send(403, {"error": "чужой Origin"})
        for r in ROUTES:
            if r["method"] != method:
                continue
            m = r["rx"].match(u.path)
            if not m:
                continue
            actor = self._actor()
            if r["perm"] != "public":
                if not self._auth():
                    return self._send(401, {"error": "нужен токен: Authorization: Bearer <токен> (окно программы → «ИИ-агент»)"})
                if actor != "ui":
                    ag = S.get_config()["agent"]
                    if not ag.get("enabled", True):
                        return self._send(403, {"error": "доступ агентов отключён оператором"})
                    if r["ui_only"]:
                        return self._send(403, {"error": "операция доступна только в окне программы"})
                    if PERM_RANK[r["perm"]] > AGENT_MAX.get(ag.get("permissions", "full"), 3):
                        return self._send(403, {"error": "недостаточно прав агента для операции (%s)" % r["perm"]})
            try:
                raw = self._raw
                body = json.loads(raw) if raw and raw.strip()[:1] in (b"{", b"[") else {}
                if not isinstance(body, dict):
                    body = {"items": body}
                res = r["fn"](self, body, parse_qs(u.query), actor, **m.groupdict())
                if actor != "ui" and method != "GET" and r["perm"] in ("write", "operator"):
                    S.audit(actor, "api " + method + " " + r["pattern"], {"args": m.groupdict()})
                if isinstance(res, tuple):
                    ctype, data, fname = res
                    return self._send(200, data, ctype, {"Content-Disposition": 'attachment; filename="%s"' % fname})
                return self._send(200, res)
            except UserError as e:
                return self._send(400, {"error": str(e)})
            except PermissionError as e:
                return self._send(403, {"error": str(e)})
            except json.JSONDecodeError:
                return self._send(400, {"error": "тело запроса — не JSON"})
            except Exception as e:
                S.log("API %s %s: %r" % (method, u.path, e), "ERROR")
                return self._send(500, {"error": "внутренняя ошибка: %s" % e})
        return self._send(404, {"error": "нет такого метода API: %s %s" % (method, u.path)})

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("POST")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # в Windows SO_REUSEADDR небезопасен

    def handle_error(self, request, client_address):
        import sys as _s
        e = _s.exc_info()[1]
        if isinstance(e, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        S.log("HTTP: %r" % (e,), "ERROR")


def start(port):
    last = None
    for p in [port] + list(range(port + 1, port + 20)):
        try:
            httpd = Server(("127.0.0.1", p), Handler)
            break
        except OSError as e:
            last = e
    else:
        raise RuntimeError("Не удалось занять порт: %s" % last)
    SERVER.update(httpd=httpd, port=httpd.server_address[1])
    threading.Thread(target=httpd.serve_forever, name="hkcl-http", daemon=True).start()
    S.runtime_write({"port": SERVER["port"], "pid": os.getpid(), "started": S.now_iso(), "version": VERSION})
    S.log("Сервер API: http://127.0.0.1:%d" % SERVER["port"])
    return SERVER["port"]


def shutdown():
    try:
        if SERVER["httpd"]:
            SERVER["httpd"].shutdown()
    finally:
        S.runtime_write({})
        os._exit(0)
