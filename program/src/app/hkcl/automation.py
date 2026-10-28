# -*- coding: utf-8 -*-
"""Автоматизация: встроенный планировщик, Планировщик заданий Windows (без прав администратора),
автозапуск при входе в систему (HKCU\\...\\Run), проверка обновлений."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

from . import VERSION, UPDATE_URL, APP_ID
from . import store as S
from . import core

IS_WIN = sys.platform.startswith("win")
TASK_NAME = "HKCaseLawMonitor AutoCheck"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STATE = {"next_run": None, "running": False, "thread": None}


def install_dir():
    # <install>\python\pythonw.exe  ->  <install>
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if os.path.basename(exe_dir).lower() == "python":
        return os.path.dirname(exe_dir)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def launcher_exe():
    return os.path.join(install_dir(), "HKCaseLawMonitor.exe")


# ------------------------------------------------------------------ расписание

def _parse_times(times):
    out = []
    for t in times or []:
        try:
            hh, mm = str(t).strip().split(":")
            hh, mm = int(hh), int(mm)
            if 0 <= hh < 24 and 0 <= mm < 60:
                out.append((hh, mm))
        except Exception:
            continue
    return sorted(set(out))


def validate_automation(a):
    a["mode"] = a.get("mode") if a.get("mode") in ("daily", "interval") else "daily"
    try:
        a["interval_hours"] = max(0.25, min(168.0, float(a.get("interval_hours") or 6)))
    except Exception:
        raise core.UserError("Интервал — число часов")
    times = _parse_times(a.get("times"))
    if a["mode"] == "daily" and not times:
        raise core.UserError("Укажите время запуска в формате ЧЧ:ММ")
    a["times"] = ["%02d:%02d" % t for t in times]
    wd = sorted(set(int(d) for d in (a.get("weekdays") or []) if str(d).isdigit() and 1 <= int(d) <= 7))
    a["weekdays"] = wd or [1, 2, 3, 4, 5, 6, 7]
    return a


def _last_cycle_dt():
    lc = S.kv_get("last_cycle") or {}
    try:
        return datetime.fromisoformat(lc["time"]).astimezone().replace(tzinfo=None)
    except Exception:
        return None


def next_run(now=None):
    a = S.get_config()["automation"]
    if not a.get("enabled") or a.get("paused"):
        return None
    now = now or datetime.now()
    last = _last_cycle_dt()
    if a.get("mode") == "interval":
        hrs = float(a.get("interval_hours") or 6)
        return (last + timedelta(hours=hrs)) if last else now
    times = _parse_times(a.get("times")) or [(8, 0)]
    days = set(a.get("weekdays") or [1, 2, 3, 4, 5, 6, 7])
    # пропущенный запуск сегодня/вчера (программа была закрыта) — догоняем
    for back in (0, 1):
        d = (now - timedelta(days=back)).date()
        if d.isoweekday() not in days:
            continue
        for hh, mm in reversed(times):
            t = datetime(d.year, d.month, d.day, hh, mm)
            if t <= now and (last is None or last < t):
                return now
            if t <= now:
                break
    for add in range(0, 8):
        d = (now + timedelta(days=add)).date()
        if d.isoweekday() not in days:
            continue
        for hh, mm in times:
            t = datetime(d.year, d.month, d.day, hh, mm)
            if t > now:
                return t
    return None


def _loop():
    last_update_check = 0
    while STATE["thread_alive"]:
        try:
            nr = next_run()
            STATE["next_run"] = nr.isoformat(timespec="minutes") if nr else None
            if nr and datetime.now() >= nr and not STATE["running"]:
                STATE["running"] = True
                try:
                    core.run_cycle(actor="scheduler")
                finally:
                    STATE["running"] = False
            if S.get_config()["settings"].get("check_updates") and time.time() - last_update_check > 86400:
                last_update_check = time.time()
                threading.Thread(target=check_update, daemon=True).start()
        except Exception as e:
            S.log("Планировщик: %s" % e, "ERROR")
        for _ in range(20):
            if not STATE["thread_alive"]:
                return
            time.sleep(1)


def start_scheduler():
    if STATE.get("thread") and STATE["thread"].is_alive():
        return
    STATE["thread_alive"] = True
    STATE["thread"] = threading.Thread(target=_loop, name="hkcl-scheduler", daemon=True)
    STATE["thread"].start()


def stop_scheduler():
    STATE["thread_alive"] = False


def status():
    a = S.get_config()["automation"]
    nr = next_run()
    return {"enabled": a.get("enabled"), "paused": a.get("paused"), "mode": a.get("mode"),
            "next_run": nr.isoformat(timespec="minutes") if nr else None, "running": STATE["running"],
            "last_cycle": S.kv_get("last_cycle"), "last_check": S.kv_get("last_check"),
            "windows_task": windows_task_status(), "autostart": autostart_status(), "is_windows": IS_WIN}


# ------------------------------------------------------------------ Windows: Планировщик заданий

def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def task_xml(a):
    exe = launcher_exe()
    start = datetime.now().strftime("%Y-%m-%dT")
    triggers = []
    if a.get("mode") == "interval":
        hrs = float(a.get("interval_hours") or 6)
        minutes = int(round(hrs * 60))
        triggers.append("<TimeTrigger><StartBoundary>%s00:00:00</StartBoundary><Enabled>true</Enabled>"
                        "<Repetition><Interval>PT%dM</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>" % (start, minutes))
    else:
        names = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday"}
        days = "".join("<%s />" % names[d] for d in (a.get("weekdays") or [1, 2, 3, 4, 5, 6, 7]))
        for hh, mm in _parse_times(a.get("times")) or [(8, 0)]:
            triggers.append("<CalendarTrigger><StartBoundary>%s%02d:%02d:00</StartBoundary><Enabled>true</Enabled>"
                            "<ScheduleByWeek><DaysOfWeek>%s</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger>" % (start, hh, mm, days))
    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            '<RegistrationInfo><Author>%s</Author><Description>HK Case Law Monitor: автоматическая проверка лент, дел на контроле и рассылка.</Description></RegistrationInfo>'
            '<Triggers>%s</Triggers>'
            '<Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>'
            '<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
            '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>'
            '<IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings><AllowStartOnDemand>true</AllowStartOnDemand>'
            '<Enabled>true</Enabled><Hidden>false</Hidden><ExecutionTimeLimit>PT1H</ExecutionTimeLimit><Priority>7</Priority></Settings>'
            '<Actions Context="Author"><Exec><Command>%s</Command><Arguments>--run-once</Arguments><WorkingDirectory>%s</WorkingDirectory></Exec></Actions>'
            '</Task>') % (_xml_escape(APP_ID), "".join(triggers), _xml_escape(exe), _xml_escape(install_dir()))


def _schtasks(args):
    flags = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
    p = subprocess.run(["schtasks"] + args, capture_output=True, creationflags=flags)
    out = (p.stdout or b"") + (p.stderr or b"")
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return p.returncode, out.decode(enc)
        except Exception:
            continue
    return p.returncode, out.decode("utf-8", "replace")


def install_windows_task(actor="ui"):
    if not IS_WIN:
        raise core.UserError("Планировщик заданий доступен только в Windows")
    a = validate_automation(S.get_config()["automation"])
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-16") as f:
            f.write(task_xml(a))
        code, out = _schtasks(["/Create", "/TN", TASK_NAME, "/XML", tmp, "/F"])
    finally:
        os.remove(tmp)
    if code != 0:
        raise core.UserError("Не удалось создать задачу Windows: " + out.strip())
    S.update_config(lambda c: c["automation"].__setitem__("windows_task", True))
    S.audit(actor, "automation.windows_task.install", {"mode": a["mode"]})
    return windows_task_status()


def remove_windows_task(actor="ui"):
    if not IS_WIN:
        raise core.UserError("Планировщик заданий доступен только в Windows")
    code, out = _schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    S.update_config(lambda c: c["automation"].__setitem__("windows_task", False))
    S.audit(actor, "automation.windows_task.remove", {"code": code})
    return windows_task_status()


def windows_task_status():
    if not IS_WIN:
        return {"available": False, "installed": False}
    code, out = _schtasks(["/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    if code != 0:
        return {"available": True, "installed": False}
    info = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    nxt = next((v for k, v in info.items() if k.lower().startswith(("next run", "время следующего"))), "")
    last = next((v for k, v in info.items() if k.lower().startswith(("last run time", "время прошлого"))), "")
    return {"available": True, "installed": True, "next_run": nxt, "last_run": last}


# ------------------------------------------------------------------ Windows: автозапуск

def set_autostart(enabled, actor="ui"):
    if not IS_WIN:
        raise core.UserError("Автозапуск доступен только в Windows")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, '"%s" --background' % launcher_exe())
        else:
            try:
                winreg.DeleteValue(k, APP_ID)
            except FileNotFoundError:
                pass
    S.update_config(lambda c: c["automation"].__setitem__("autostart", bool(enabled)))
    S.audit(actor, "automation.autostart", {"enabled": bool(enabled)})
    return autostart_status()


def autostart_status():
    if not IS_WIN:
        return {"available": False, "enabled": False}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, APP_ID)
            return {"available": True, "enabled": True, "command": v}
    except OSError:
        return {"available": True, "enabled": False}


# ------------------------------------------------------------------ обновления

def _ver(v):
    return tuple(int(x) for x in str(v).split(".")[:3] if x.isdigit())


def check_update():
    try:
        code, _, body = core.http_request(UPDATE_URL, timeout=15)
        if code != 200:
            return {"ok": False, "error": "HTTP %d" % code}
        j = json.loads(body)
        info = {"ok": True, "current": VERSION, "latest": j.get("version"), "url": j.get("url"), "notes": j.get("notes", ""),
                "available": _ver(j.get("version", "0")) > _ver(VERSION), "checked": S.now_iso()}
        S.kv_set("update", info)
        if info["available"]:
            core._notify("Доступна новая версия", "HK Case Law Monitor %s" % info["latest"])
        return info
    except Exception as e:
        return {"ok": False, "error": str(e)}
