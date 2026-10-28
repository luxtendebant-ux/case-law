# -*- coding: utf-8 -*-
"""Подключение языковых моделей: Anthropic (Claude), OpenAI (ChatGPT), OpenAI-совместимые API
(DeepSeek, OpenRouter, Mistral, локальные Ollama / LM Studio и др.).
Результаты ИИ всегда помечаются как требующие проверки оператором."""

import json
import re

from . import store as S
from .core import UserError, http_request, get_document, get_case, add_event, norm_list, ru_date

PROVIDERS = {
    "anthropic": {"name": "Anthropic (Claude)", "base": "https://api.anthropic.com", "key_required": True},
    "openai": {"name": "OpenAI (ChatGPT)", "base": "https://api.openai.com/v1", "key_required": True},
    "compatible": {"name": "OpenAI-совместимый API (DeepSeek, OpenRouter, Mistral, Ollama, LM Studio…)", "base": "", "key_required": False},
}

SYSTEM = ("Ты — помощник юриста по комплаенсу, работающий с судебными решениями Гонконга. "
          "Отвечай по-русски, точно и кратко. Опирайся только на переданный текст. "
          "Если в тексте нет нужных сведений — прямо так и напиши, ничего не додумывай. "
          "Ссылайся на номера абзацев решения, где это возможно.")

MAX_INPUT = 60000


def settings():
    return S.get_config()["llm"]


def _need(cfg, sends_text=True):
    if not cfg.get("enabled"):
        raise UserError("ИИ-помощник отключён (вкладка «ИИ-помощник»)")
    if sends_text and not cfg.get("consent"):
        raise UserError("Нет согласия на передачу текстов внешнему сервису LLM (вкладка «ИИ-помощник»)")
    prov = cfg.get("provider")
    if prov not in PROVIDERS:
        raise UserError("Неизвестный провайдер LLM: %s" % prov)
    if not cfg.get("model"):
        raise UserError("Не указана модель LLM")
    if PROVIDERS[prov]["key_required"] and not S.has_secret("llm_api_key"):
        raise UserError("Не задан API-ключ LLM")
    if prov == "compatible" and not cfg.get("base_url"):
        raise UserError("Для OpenAI-совместимого API укажите базовый адрес (например http://localhost:11434/v1)")


def _err(body):
    try:
        j = json.loads(body.decode("utf-8", "replace"))
        e = j.get("error")
        if isinstance(e, dict):
            return e.get("message") or json.dumps(e, ensure_ascii=False)
        return str(e or j)[:500]
    except Exception:
        return body[:300].decode("utf-8", "replace")


def complete(user, system=SYSTEM, max_tokens=None, task="chat", target="", actor="ui", sends_text=True):
    cfg = settings()
    _need(cfg, sends_text)
    prov, model = cfg["provider"], cfg["model"]
    mt = int(max_tokens or cfg.get("max_tokens") or 2000)
    temp = float(cfg.get("temperature") if cfg.get("temperature") is not None else 0.2)
    timeout = int(cfg.get("timeout") or 120)
    key = S.get_secret("llm_api_key")
    base = (cfg.get("base_url") or PROVIDERS[prov]["base"]).rstrip("/")
    out, err = "", ""
    try:
        if prov == "anthropic":
            body = {"model": model, "max_tokens": mt, "system": system, "temperature": temp,
                    "messages": [{"role": "user", "content": user}]}
            code, _, resp = http_request(base + "/v1/messages", data=json.dumps(body).encode("utf-8"), method="POST", timeout=timeout,
                                         headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            if code != 200:
                raise UserError("Anthropic API: HTTP %d — %s" % (code, _err(resp)))
            j = json.loads(resp)
            out = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        else:
            body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": temp}
            body["max_completion_tokens" if prov == "openai" else "max_tokens"] = mt
            hdr = {"content-type": "application/json"}
            if key:
                hdr["Authorization"] = "Bearer " + key
            for attempt in range(3):
                code, _, resp = http_request(base + "/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST", timeout=timeout, headers=hdr)
                if code == 400:
                    msg = _err(resp)
                    if "temperature" in msg and "temperature" in body:
                        body.pop("temperature")
                        continue
                    if "max_completion_tokens" in msg and "max_completion_tokens" in body:
                        body["max_tokens"] = body.pop("max_completion_tokens")
                        continue
                    if "max_tokens" in msg and "max_tokens" in body:
                        body["max_completion_tokens"] = body.pop("max_tokens")
                        continue
                break
            if code != 200:
                raise UserError("%s: HTTP %d — %s" % (PROVIDERS[prov]["name"], code, _err(resp)))
            j = json.loads(resp)
            out = (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        if not out.strip():
            raise UserError("Модель вернула пустой ответ")
        return out.strip()
    except UserError as e:
        err = str(e)
        raise
    except Exception as e:
        err = "Ошибка обращения к LLM: %s" % e
        raise UserError(err)
    finally:
        try:
            S.x("INSERT INTO llm_runs(id,ts,task,target,provider,model,input_chars,output,ok,error,actor) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (S.new_id("l_"), S.now_iso(), task, target, prov, model, len(user), out[:20000], 0 if err else 1, err, actor))
        except Exception:
            pass


def test_connection(actor="ui"):
    ans = complete("Ответь одним словом: «готов».", system="Ты — тестовый ответчик.", max_tokens=20, task="test", actor=actor, sends_text=False)
    return {"ok": True, "answer": ans, "provider": settings()["provider"], "model": settings()["model"]}


def _doc_text(d):
    t = d.get("text") or ""
    if len(t) <= MAX_INPUT:
        return t, False
    return t[:MAX_INPUT - 12000] + "\n\n[…текст сокращён…]\n\n" + t[-12000:], True


def _save_doc_llm(did, key, value):
    d = get_document(did, with_text=False)
    llm = d.get("llm") or {}
    llm[key] = {"text": value, "at": S.now_iso(), "model": settings().get("model"), "provider": settings().get("provider")}
    S.x("UPDATE documents SET llm=? WHERE id=?", (json.dumps(llm, ensure_ascii=False), did))


def _to_cases(did, title, text, actor):
    for r in S.q("SELECT DISTINCT case_id FROM case_events WHERE doc_id=?", (did,)):
        try:
            add_event(r["case_id"], "ai", title, details=text + "\n\n(сгенерировано ИИ, требует проверки)", doc_id=None, actor=actor, seen=0)
        except Exception:
            pass


def summarize_document(did, audience="management", actor="ui"):
    d = get_document(did)
    text, cut = _doc_text(d)
    if audience == "management":
        task = ("Подготовь резюме судебного решения для высшего руководства компании (не юристов): "
                "1) о чём спор и кто стороны; 2) чем закончилось (итог); 3) почему это важно для бизнеса, "
                "особенно в части санкций и экспортного контроля, если это есть в тексте; 4) практические выводы и риски. "
                "Без юридического аппарата и процессуальных подробностей, 8–12 предложений.")
    else:
        task = ("Подготовь юридическое резюме решения: суд, стороны, номер дела, дата; фактические обстоятельства; "
                "вопросы права; позиция суда с указанием абзацев; итог; значение для практики.")
    meta = d.get("meta") or {}
    hdr = "Документ: %s\nСуд: %s; NCN: %s; номер дела: %s; дата: %s%s\n\nТЕКСТ:\n" % (
        d["name"], meta.get("court"), ", ".join(meta.get("ncn") or []), ", ".join(meta.get("acts") or []), meta.get("date"),
        "\n(текст сокращён: передано начало и окончание)" if cut else "")
    out = complete(task + "\n\n" + hdr + text, task="summarize_" + audience, target=did, actor=actor)
    _save_doc_llm(did, "summary_" + audience, out)
    _to_cases(did, "ИИ-резюме документа: " + d["name"], out, actor)
    S.audit(actor, "llm.summarize", {"doc": did, "audience": audience})
    return {"doc_id": did, "audience": audience, "summary": out, "truncated": cut, "note": "Сгенерировано ИИ, требует проверки"}


EXTRACT_FIELDS = ["court", "case_numbers", "ncn", "date_of_decision", "judges", "parties", "outcome",
                  "legal_issues", "sanctions_regimes", "jurisdictions", "key_paragraphs", "relevance_for_sanctions_compliance"]


def extract_document(did, actor="ui"):
    d = get_document(did)
    text, cut = _doc_text(d)
    task = ("Извлеки из решения структурированные данные. Верни ТОЛЬКО JSON-объект без пояснений с полями: "
            + ", ".join(EXTRACT_FIELDS) +
            ". Списки — массивы строк; если сведений нет — null. outcome и relevance_for_sanctions_compliance — по-русски, кратко.")
    raw = complete(task + "\n\nТЕКСТ:\n" + text, task="extract", target=did, actor=actor)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except Exception:
        data = None
    if data is None:
        raise UserError("Модель вернула не JSON: " + raw[:300])
    _save_doc_llm(did, "extract", data)
    S.audit(actor, "llm.extract", {"doc": did})
    return {"doc_id": did, "data": data, "truncated": cut, "note": "Сгенерировано ИИ, требует проверки"}


def ask_document(did, question, actor="ui"):
    if not str(question or "").strip():
        raise UserError("Пустой вопрос")
    d = get_document(did)
    text, cut = _doc_text(d)
    out = complete("Вопрос: %s\n\nОтветь только по тексту решения ниже, со ссылками на абзацы.\n\nТЕКСТ:\n%s" % (question, text),
                   task="ask", target=did, actor=actor)
    S.audit(actor, "llm.ask", {"doc": did, "q": question[:200]})
    return {"doc_id": did, "question": question, "answer": out, "truncated": cut, "note": "Сгенерировано ИИ, требует проверки"}


def expand_query(query, actor="ui"):
    if not str(query or "").strip():
        raise UserError("Пустой запрос")
    raw = complete("Пользователь ищет судебную практику Гонконга по теме: «%s». Предложи до 15 поисковых терминов "
                   "на английском и традиционном китайском (названия ордонансов с номерами Cap., устойчивые юридические выражения, "
                   "синонимы). Верни ТОЛЬКО JSON: {\"terms\": [\"...\"], \"comment\": \"...\"}" % query,
                   task="expand_query", target=query[:100], actor=actor, sends_text=False)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0))
        terms = [str(t).strip() for t in data.get("terms", []) if str(t).strip()][:15]
    except Exception:
        terms, data = [t.strip(" -•\"") for t in raw.splitlines() if t.strip()][:15], {}
    return {"query": query, "terms": terms, "comment": data.get("comment", "") if isinstance(data, dict) else "",
            "note": "Сгенерировано ИИ, требует проверки"}


def case_brief(cid, actor="ui"):
    c = get_case(cid)
    ev = "\n".join("- %s [%s] %s: %s" % (ru_date(e.get("date")), e["type"], e["title"], (e.get("details") or "")[:500]) for e in c["events"][:80])
    out = complete("Подготовь краткую справку о состоянии дела для руководства: что известно, последние изменения, "
                   "что требует внимания. Только по данным ниже.\n\nДело: %s\nНомера: %s\nNCN: %s\nСтороны: %s\nЗаметки: %s\n\nСобытия:\n%s" % (
                       c["title"], ", ".join(c["numbers"]), ", ".join(c["ncn"]), ", ".join(c["parties"]), c.get("notes") or "", ev),
                   task="case_brief", target=cid, actor=actor)
    add_event(cid, "ai", "ИИ-справка по делу", details=out + "\n\n(сгенерировано ИИ, требует проверки)", actor=actor, seen=0)
    return {"case_id": cid, "brief": out, "note": "Сгенерировано ИИ, требует проверки"}


def digest_summary(dig, actor="scheduler"):
    lines = []
    for p, rows in dig:
        for it in rows[:60]:
            lines.append("- [%s] %s — %s" % (p["name"], it.get("title"), (it.get("description") or "")[:300]))
    return complete("Ниже — список новых судебных решений Гонконга (только заголовки и аннотации из официальной ленты). "
                    "Напиши 3–5 предложений для руководства: что появилось и что заслуживает внимания. "
                    "Не делай выводов, которых нет в аннотациях.\n\n" + "\n".join(lines),
                    task="digest_summary", actor=actor, max_tokens=600, sends_text=False)


def history(limit=100):
    return [dict(r) for r in S.q("SELECT id,ts,task,target,provider,model,input_chars,ok,error,actor,substr(output,1,500) output FROM llm_runs ORDER BY ts DESC LIMIT ?", (limit,))]
