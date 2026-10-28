# -*- coding: utf-8 -*-
"""MCP-сервер (Model Context Protocol, транспорт stdio) для автономных ИИ-агентов.
Подключение (Claude Desktop, claude_desktop_config.json):
  {"mcpServers": {"hk-case-law": {"command": "<папка программы>\\hkcl.exe", "args": ["mcp"]}}}
"""

import json
import sys
from urllib.parse import urlencode, parse_qs

from . import VERSION
from . import store as S

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "HK Case Law Monitor — поиск и мониторинг судебной практики Гонконга. "
    "Источник новых решений — официальные RSS-ленты Legal Reference System (метаданные и ссылки). "
    "Тексты решений агент получает только из документов, загруженных в программу (hk_add_document). "
    "Порядок работы: hk_status → hk_check_feeds → hk_list_judgments(matched) → hk_add_case / hk_case_from_judgment → "
    "hk_unseen_events → hk_send_case_alerts. Результаты LLM помечены как требующие проверки человеком. "
    "HKLII автоматически не опрашивается (правила сайта)."
)

S_STR = {"type": "string"}
S_BOOL = {"type": "boolean"}
S_INT = {"type": "integer"}
S_LIST = {"type": "array", "items": {"type": "string"}}


def obj(props=None, req=None):
    o = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if req:
        o["required"] = req
    return o


CASE_PROPS = {"title": S_STR, "numbers": dict(S_LIST, description="Номера дел, напр. HCCT 19/2023"),
              "ncn": dict(S_LIST, description="Нейтральные ссылки, напр. [2023] HKCFI 2409"),
              "parties": dict(S_LIST, description="Стороны (ищутся в заголовках решений)"),
              "keywords": dict(S_LIST, description="Все слова обязательны"), "court": S_STR,
              "priority": {"type": "string", "enum": ["high", "normal", "low"]}, "notes": S_STR,
              "remind_at": dict(S_STR, description="ГГГГ-ММ-ДД"), "tags": S_LIST,
              "status": {"type": "string", "enum": ["active", "closed"]}}

# name: (description, schema, method, path_template, query_keys)
TOOLS = {
    "hk_status": ("Состояние программы: число решений, дел, событий, автоматизация, SMTP, LLM.", obj(), "GET", "/api/v1/status", ()),
    "hk_search_sources": ("Сформировать ссылки поиска по базам (LRS, hkcfa.hk, HKLII, vLex, Westlaw, Lexis+, свои источники). Ссылки открывает человек или браузерный агент.",
                          obj({"words": dict(S_STR, description="термины через «;»"), "phrase": S_STR, "parties": S_STR, "ncn": S_STR, "case": S_STR, "court": S_STR}),
                          "GET", "/api/v1/sources/links", ("words", "phrase", "parties", "ncn", "case", "court")),
    "hk_search_local": ("Поиск по собранной базе: решения из лент, загруженные документы, дела, события. Термины через «;» (все обязательны).",
                        obj({"q": S_STR, "scopes": dict(S_STR, description="items,documents,cases,events"), "limit": S_INT}, ["q"]),
                        "GET", "/api/v1/search", ("q", "scopes", "limit")),
    "hk_check_feeds": ("Проверить RSS-ленты сейчас: новые решения, совпадения с профилями, события по делам на контроле.", obj({"force": S_BOOL}), "POST", "/api/v1/feeds/check", ()),
    "hk_list_feeds": ("Ленты, статус robots.txt и последнего опроса.", obj(), "GET", "/api/v1/feeds", ()),
    "hk_add_feed": ("Добавить RSS-ленту (https; разрешённые хосты; HKLII запрещён).", obj({"name": S_STR, "url": S_STR}, ["name", "url"]), "X", "add_feed", ()),
    "hk_set_feed_override": ("Решение по ленте, если robots.txt запрещает опрос (требует прав full и разрешения оператора).",
                             obj({"feed_id": S_STR, "enabled": S_BOOL}, ["feed_id", "enabled"]), "POST", "/api/v1/feeds/{feed_id}/override", ()),
    "hk_list_judgments": ("Решения из лент с фильтрами.", obj({"matched": S_BOOL, "profile": S_STR, "feed": S_STR, "text": S_STR, "days": S_INT, "unsent": S_BOOL, "limit": S_INT}),
                          "GET", "/api/v1/items", ("matched", "profile", "feed", "text", "days", "unsent", "limit")),
    "hk_list_profiles": ("Профили поиска (критерии отбора решений).", obj(), "GET", "/api/v1/profiles", ()),
    "hk_save_profile": ("Создать или изменить профиль (по id).", obj({"profile": {"type": "object", "properties": {
        "id": S_STR, "name": S_STR, "enabled": S_BOOL, "any_words": S_LIST, "all_words": S_LIST, "exclude_words": S_LIST,
        "case_regex": S_STR, "email": S_BOOL, "feeds": S_LIST}}}, ["profile"]), "X", "save_profile", ()),
    "hk_list_cases": ("Дела на контроле с числом непросмотренных событий.", obj({"status": {"type": "string", "enum": ["active", "closed"]}}), "GET", "/api/v1/cases", ("status",)),
    "hk_get_case": ("Дело и полная хронология событий.", obj({"case_id": S_STR}, ["case_id"]), "GET", "/api/v1/cases/{case_id}", ()),
    "hk_add_case": ("Поставить дело на контроль. Нужен хотя бы один признак: номер дела, NCN, сторона или ключевые слова.", obj(CASE_PROPS), "POST", "/api/v1/cases", ()),
    "hk_case_from_judgment": ("Поставить на контроль дело по решению из ленты.", obj({"item_id": S_STR}, ["item_id"]), "POST", "/api/v1/cases/from-item", ()),
    "hk_update_case": ("Изменить дело (частично), в т.ч. закрыть: status=closed.", obj(dict(CASE_PROPS, case_id=S_STR), ["case_id"]), "POST", "/api/v1/cases/{case_id}", ()),
    "hk_add_case_event": ("Добавить событие в хронологию дела.", obj({"case_id": S_STR, "type": {"type": "string", "enum": ["note", "hearing", "deadline", "agent", "judgment", "document"]},
                                                                      "title": S_STR, "details": S_STR, "date": dict(S_STR, description="ГГГГ-ММ-ДД"), "link": S_STR}, ["case_id", "title"]),
                          "POST", "/api/v1/cases/{case_id}/events", ()),
    "hk_unseen_events": ("Непросмотренные события по всем делам (изменения).", obj(), "GET", "/api/v1/events/unseen", ()),
    "hk_mark_seen": ("Отметить события дела просмотренными.", obj({"case_id": S_STR, "event_ids": S_LIST}, ["case_id"]), "POST", "/api/v1/cases/{case_id}/seen", ()),
    "hk_add_document": ("Загрузить решение (PDF/DOCX/HTML/TXT): path к локальному файлу или name+content_base64. Автоматически извлекаются суд, NCN, номер дела, дата, состав и связи с делами.",
                        obj({"path": S_STR, "name": S_STR, "content_base64": S_STR}), "POST", "/api/v1/documents", ()),
    "hk_list_documents": ("Загруженные документы.", obj(), "GET", "/api/v1/documents", ()),
    "hk_get_document": ("Документ (метаданные и текст).", obj({"doc_id": S_STR, "include_text": S_BOOL}, ["doc_id"]), "X", "get_document", ()),
    "hk_analyze_documents": ("Поиск терминов в документах с фрагментами.", obj({"doc_ids": S_LIST, "query": S_STR, "mode": {"type": "string", "enum": ["any", "all"]}, "profile_id": S_STR}),
                             "POST", "/api/v1/documents/analyze", ()),
    "hk_send_digest": ("Дайджест новых решений по профилям на e-mail (dry_run — предпросмотр).", obj({"dry_run": S_BOOL}), "POST", "/api/v1/digest", ()),
    "hk_send_case_alerts": ("Письмо об изменениях по делам на контроле.", obj({"dry_run": S_BOOL}), "POST", "/api/v1/alerts/cases", ()),
    "hk_send_report": ("Отчёт анализа документов на e-mail.", obj({"doc_ids": S_LIST, "query": S_STR, "profile_id": S_STR, "only_matching": S_BOOL, "dry_run": S_BOOL}), "POST", "/api/v1/report", ()),
    "hk_run_cycle": ("Полный цикл автоматизации: ленты → дела → напоминания → письма.", obj({"force": S_BOOL}), "POST", "/api/v1/automation/run", ()),
    "hk_automation_status": ("Расписание и следующие запуски.", obj(), "GET", "/api/v1/automation", ()),
    "hk_llm_summarize": ("ИИ-резюме документа (management — для руководства, legal — юридическое).", obj({"doc_id": S_STR, "audience": {"type": "string", "enum": ["management", "legal"]}}, ["doc_id"]), "POST", "/api/v1/llm/summarize", ()),
    "hk_llm_extract": ("ИИ-извлечение структурированных данных из решения (JSON).", obj({"doc_id": S_STR}, ["doc_id"]), "POST", "/api/v1/llm/extract", ()),
    "hk_llm_ask": ("Вопрос к тексту решения через подключённую LLM.", obj({"doc_id": S_STR, "question": S_STR}, ["doc_id", "question"]), "POST", "/api/v1/llm/ask", ()),
    "hk_llm_expand_query": ("Подобрать поисковые термины (англ./кит.) через LLM.", obj({"query": S_STR}, ["query"]), "POST", "/api/v1/llm/expand", ()),
    "hk_llm_case_brief": ("ИИ-справка о состоянии дела по его хронологии.", obj({"case_id": S_STR}, ["case_id"]), "POST", "/api/v1/llm/case-brief", ()),
    "hk_audit": ("Журнал аудита действий (кто, что, когда).", obj({"limit": S_INT, "actor": S_STR}), "GET", "/api/v1/audit", ("limit", "actor")),
}


# ------------------------------------------------------------------ исполнители

class Backend:
    """HTTP к запущенной программе или прямой вызов маршрутов в этом процессе."""

    def __init__(self, actor="mcp"):
        self.actor = actor
        self.base = None
        rt = S.runtime_read()
        if rt.get("port"):
            try:
                from urllib import request as r
                with r.urlopen("http://127.0.0.1:%d/api/v1/ping" % rt["port"], timeout=2) as resp:
                    if json.loads(resp.read()).get("app"):
                        self.base = "http://127.0.0.1:%d" % rt["port"]
            except Exception:
                self.base = None

    def call(self, method, path, body=None, query=None):
        query = {k: v for k, v in (query or {}).items() if v not in (None, "", False)}
        query = {k: ("1" if v is True else v) for k, v in query.items()}
        if self.base:
            from urllib import request as r, error as e
            url = self.base + path + ("?" + urlencode(query) if query else "")
            data = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
            req = r.Request(url, data=data, method=method, headers={"Authorization": "Bearer " + S.api_token(), "X-HKCL-Actor": self.actor,
                                                                    "Content-Type": "application/json"})
            try:
                with r.urlopen(req, timeout=600) as resp:
                    return json.loads(resp.read())
            except e.HTTPError as err:
                try:
                    msg = json.loads(err.read()).get("error")
                except Exception:
                    msg = str(err)
                raise RuntimeError(msg)
        return direct_call(method, path, body or {}, {k: [str(v)] for k, v in query.items()}, self.actor)


def direct_call(method, path, body, qs, actor):
    from . import api
    from .core import UserError
    for rt in api.ROUTES:
        if rt["method"] != method:
            continue
        m = rt["rx"].match(path)
        if not m:
            continue
        ag = S.get_config()["agent"]
        if rt["perm"] != "public" and actor != "ui":
            if not ag.get("enabled", True):
                raise RuntimeError("доступ агентов отключён оператором")
            if rt["ui_only"]:
                raise RuntimeError("операция доступна только в окне программы")
            if api.PERM_RANK[rt["perm"]] > api.AGENT_MAX.get(ag.get("permissions", "full"), 3):
                raise RuntimeError("недостаточно прав агента (%s)" % rt["perm"])
        try:
            res = rt["fn"](None, body, qs, actor, **m.groupdict())
        except (UserError, PermissionError) as e:
            raise RuntimeError(str(e))
        if method != "GET" and rt["perm"] in ("write", "operator"):
            S.audit(actor, "mcp " + method + " " + rt["pattern"], {"args": m.groupdict()})
        return res
    raise RuntimeError("нет метода %s %s" % (method, path))


def run_tool(be, name, args):
    if name not in TOOLS:
        raise RuntimeError("Неизвестный инструмент: %s" % name)
    desc, schema, method, path, qkeys = TOOLS[name]
    args = dict(args or {})
    if method == "X":
        if path == "add_feed":
            cfg = be.call("GET", "/api/v1/config")
            feeds = cfg["feeds"] + [{"name": args["name"], "url": args["url"], "enabled": True, "robots_override": False}]
            return {"feeds": be.call("POST", "/api/v1/config", {"feeds": feeds})["feeds"]}
        if path == "save_profile":
            prof = args["profile"]
            profs = be.call("GET", "/api/v1/profiles")["profiles"]
            idx = next((i for i, p in enumerate(profs) if prof.get("id") and p["id"] == prof["id"]), None)
            if idx is None:
                profs.append(prof)
            else:
                profs[idx] = dict(profs[idx], **prof)
            return be.call("POST", "/api/v1/profiles", {"profiles": profs})
        if path == "get_document":
            return be.call("GET", "/api/v1/documents/%s" % args["doc_id"], query={"text": "1" if args.get("include_text", True) else "0"})
    for key in [k for k in list(args) if "{" + k + "}" in path]:
        path = path.replace("{" + key + "}", str(args.pop(key)))
    if method == "GET":
        return be.call("GET", path, query={k: args.get(k) for k in qkeys})
    return be.call(method, path, args)


# ------------------------------------------------------------------ протокол

def _write(msg):
    sys.stdout.buffer.write((json.dumps(msg, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def serve():
    be = None
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            _write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            mid, method, params = m.get("id"), m.get("method"), m.get("params") or {}
            if mid is None:      # уведомление
                continue
            try:
                if method == "initialize":
                    pv = params.get("protocolVersion")
                    result = {"protocolVersion": pv if pv in PROTOCOLS else PROTOCOLS[0],
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "hk-case-law-monitor", "title": "HK Case Law Monitor", "version": VERSION},
                              "instructions": INSTRUCTIONS}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": [{"name": n, "description": t[0], "inputSchema": t[1]} for n, t in TOOLS.items()]}
                elif method == "tools/call":
                    be = be or Backend("mcp")
                    try:
                        out = run_tool(be, params.get("name"), params.get("arguments"))
                        text = json.dumps(out, ensure_ascii=False, default=str)
                        if len(text) > 400000:
                            text = text[:400000] + "…[обрезано]"
                        result = {"content": [{"type": "text", "text": text}], "isError": False}
                    except Exception as e:
                        result = {"content": [{"type": "text", "text": "Ошибка: %s" % e}], "isError": True}
                elif method in ("resources/list", "prompts/list"):
                    result = {method.split("/")[0]: []}
                else:
                    _write({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: %s" % method}})
                    continue
                _write({"jsonrpc": "2.0", "id": mid, "result": result})
            except Exception as e:
                _write({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})
