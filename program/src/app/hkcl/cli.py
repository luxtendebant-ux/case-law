# -*- coding: utf-8 -*-
"""Командная строка hkcl.exe — для ИИ-агентов, скриптов и администрирования. Вывод — JSON."""

import argparse
import json
import sys

from . import APP_NAME, VERSION, COPYRIGHT
from . import store as S


def out(obj):
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False, indent=1, default=str) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def build_parser():
    ap = argparse.ArgumentParser(prog="hkcl", description="%s %s — командная строка. %s" % (APP_NAME, VERSION, COPYRIGHT))
    ap.add_argument("--data", help="папка данных (по умолчанию %%APPDATA%%\\HKCaseLawMonitor)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("version", help="версия")
    sub.add_parser("mcp", help="MCP-сервер (stdio) для ИИ-агентов")
    sp = sub.add_parser("serve", help="запустить API без окна")
    sp.add_argument("--port", type=int, default=8767)
    sp.add_argument("--allow-feed-host", action="append", help=argparse.SUPPRESS)
    sub.add_parser("status", help="состояние программы")
    c = sub.add_parser("check", help="проверить ленты")
    c.add_argument("--force", action="store_true")
    sub.add_parser("cycle", help="полный цикл автоматизации")
    d = sub.add_parser("digest", help="дайджест по профилям")
    d.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("search", help="поиск по собранной базе")
    s.add_argument("query")
    l = sub.add_parser("links", help="ссылки поиска по базам")
    l.add_argument("--words", default="")
    l.add_argument("--phrase", default="")
    l.add_argument("--parties", default="")
    l.add_argument("--ncn", default="")
    l.add_argument("--case", default="")
    cs = sub.add_parser("cases", help="дела на контроле")
    cs.add_argument("action", choices=["list", "get", "add", "unseen"])
    cs.add_argument("--id")
    cs.add_argument("--title")
    cs.add_argument("--number", action="append")
    cs.add_argument("--ncn", action="append")
    cs.add_argument("--party", action="append")
    cs.add_argument("--keyword", action="append")
    cs.add_argument("--notes")
    dc = sub.add_parser("doc", help="загрузить документ")
    dc.add_argument("path")
    sub.add_parser("token", help="показать токен API")
    sub.add_parser("shutdown", help="завершить запущенную программу")
    un = sub.add_parser("uninstall", help="удалить программу (мастер удаления; --silent — без вопросов, данные сохраняются)")
    un.add_argument("--silent", action="store_true")
    sub.add_parser("openapi", help="спецификация OpenAPI")
    sc = sub.add_parser("schedule", help="задача в Планировщике заданий Windows")
    sc.add_argument("action", choices=["install", "remove", "status"])
    au = sub.add_parser("autostart", help="автозапуск при входе в Windows")
    au.add_argument("action", choices=["on", "off", "status"])
    return ap


def call(method, path, body=None, query=None):
    from .mcp import Backend
    return Backend(actor="cli").call(method, path, body or {}, query or {})


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.data:
        S.set_data_dir(a.data)
    if not a.cmd:
        ap.print_help()
        return 0
    try:
        if a.cmd == "version":
            out({"app": APP_NAME, "version": VERSION, "copyright": COPYRIGHT})
        elif a.cmd == "mcp":
            from . import mcp
            mcp.serve()
        elif a.cmd == "serve":
            from . import core, app
            for h in a.allow_feed_host or []:
                core.TEST_FEED_HOSTS.add(h.lower())
            port = app.serve(a.port, block=False)
            sys.stderr.write("API: http://127.0.0.1:%d/  токен: %s\n" % (port, S.api_token()))
            sys.stderr.flush()
            import time
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        elif a.cmd == "status":
            out(call("GET", "/api/v1/status"))
        elif a.cmd == "check":
            out(call("POST", "/api/v1/feeds/check", {"force": a.force}))
        elif a.cmd == "cycle":
            out(call("POST", "/api/v1/automation/run", {}))
        elif a.cmd == "digest":
            out(call("POST", "/api/v1/digest", {"dry_run": a.dry_run}))
        elif a.cmd == "search":
            out(call("GET", "/api/v1/search", query={"q": a.query}))
        elif a.cmd == "links":
            out(call("GET", "/api/v1/sources/links", query={k: getattr(a, k) for k in ("words", "phrase", "parties", "ncn", "case")}))
        elif a.cmd == "cases":
            if a.action == "list":
                out(call("GET", "/api/v1/cases"))
            elif a.action == "get":
                out(call("GET", "/api/v1/cases/%s" % a.id))
            elif a.action == "unseen":
                out(call("GET", "/api/v1/events/unseen"))
            else:
                out(call("POST", "/api/v1/cases", {"title": a.title, "numbers": a.number, "ncn": a.ncn, "parties": a.party,
                                                   "keywords": a.keyword, "notes": a.notes}))
        elif a.cmd == "doc":
            out(call("POST", "/api/v1/documents", {"path": a.path}))
        elif a.cmd == "shutdown":
            from . import app
            port = app.running_instance()
            if port:
                try:
                    app.call_instance(port, "POST", "/api/v1/shutdown", {}, timeout=5)
                except Exception:
                    pass
            out({"ok": True, "was_running": bool(port)})
        elif a.cmd == "uninstall":
            import os
            import subprocess
            from . import app, automation
            exe = os.path.join(automation.install_dir(), "Uninstall.exe")
            if not os.path.isfile(exe):
                raise RuntimeError("Модуль удаления не найден: %s" % exe)
            port = app.running_instance()
            if port:
                try:
                    app.call_instance(port, "POST", "/api/v1/shutdown", {}, timeout=5)
                except Exception:
                    pass
            subprocess.Popen([exe] + (["/S"] if a.silent else []), cwd=os.path.dirname(exe), close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            out({"ok": True, "message": "Запущено удаление программы" + (" (без вопросов, данные сохраняются)" if a.silent else "")})
        elif a.cmd == "token":
            out({"token": S.api_token()})
        elif a.cmd == "openapi":
            out(call("GET", "/api/v1/openapi.json"))
        elif a.cmd == "schedule":
            from . import automation
            out(automation.windows_task_status() if a.action == "status" else
                automation.install_windows_task("cli") if a.action == "install" else automation.remove_windows_task("cli"))
        elif a.cmd == "autostart":
            from . import automation
            out(automation.autostart_status() if a.action == "status" else automation.set_autostart(a.action == "on", "cli"))
        return 0
    except Exception as e:
        out({"error": str(e)})
        return 1
