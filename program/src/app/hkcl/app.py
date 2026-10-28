# -*- coding: utf-8 -*-
"""Запуск программы: собственное окно (Microsoft Edge WebView2), значок в области уведомлений,
фоновый режим, однократный цикл для Планировщика заданий, единственный экземпляр."""

import json
import os
import sys
import threading
import time
import webbrowser
from urllib import request as urlreq

from . import APP_NAME, APP_ID, VERSION
from . import store as S
from . import core, api, automation

IS_WIN = sys.platform.startswith("win")
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON = os.path.join(APP_DIR, "web", "app.ico")
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
UI = {"window": None, "tray": None, "quitting": False}


def message_box(text, title=APP_NAME, kind=0x40):
    if IS_WIN:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(None, text, title, kind | 0x10000)
    sys.stderr.write("%s: %s\n" % (title, text))
    return 0


def running_instance():
    rt = S.runtime_read()
    if not rt.get("port"):
        return None
    try:
        with urlreq.urlopen("http://127.0.0.1:%d/api/v1/ping" % rt["port"], timeout=2) as r:
            if json.loads(r.read()).get("app") == APP_NAME:
                return rt["port"]
    except Exception:
        return None
    return None


def call_instance(port, method, path, body=None, timeout=600, actor="ui"):
    data = json.dumps(body or {}).encode() if method != "GET" else None
    req = urlreq.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method,
                         headers={"Authorization": "Bearer " + S.api_token(), "X-HKCL-Actor": actor, "Content-Type": "application/json"})
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def webview2_available():
    if not IS_WIN:
        return False
    import winreg
    import platform
    keys = ["{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
            "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}", "{65C35B14-6C1D-4122-AC46-7148CC9D6497}"]
    for k in keys:
        for hive, wow in ((winreg.HKEY_CURRENT_USER, False), (winreg.HKEY_LOCAL_MACHINE, platform.machine().lower() != "x86")):
            p = r"SOFTWARE\%sMicrosoft\EdgeUpdate\Clients\%s" % ("WOW6432Node\\" if wow else "", k)
            try:
                with winreg.OpenKey(hive, p) as key:
                    v, _ = winreg.QueryValueEx(key, "pv")
                    if v and v != "0.0.0.0" and int(str(v).split(".")[0]) >= 86:
                        return True
            except OSError:
                continue
    return False


# ------------------------------------------------------------------ трей

def _tray_image():
    from PIL import Image
    return Image.open(ICON)


def start_tray(port):
    try:
        import pystray
    except Exception as e:
        S.log("Значок в трее недоступен: %s" % e, "WARN")
        return None

    def show(icon=None, item=None):
        show_window(port)

    def check(icon=None, item=None):
        def go():
            res = core.run_cycle(actor="ui", force=True)
            ck = res.get("check") or {}
            notify("Проверка завершена", "Новых решений: %s, совпадений: %s, событий по делам: %s" % (ck.get("new", 0), ck.get("new_matches", 0), ck.get("case_events", 0)))
        threading.Thread(target=go, daemon=True).start()

    def toggle_pause(icon, item):
        S.update_config(lambda c: c["automation"].__setitem__("paused", not c["automation"].get("paused")))

    def paused(item):
        return bool(S.get_config()["automation"].get("paused"))

    def open_folder(icon=None, item=None):
        if IS_WIN:
            os.startfile(S.data_dir())

    def quit_app(icon=None, item=None):
        quit_all()

    menu = pystray.Menu(
        pystray.MenuItem("Открыть HK Case Law Monitor", show, default=True),
        pystray.MenuItem("Проверить сейчас", check),
        pystray.MenuItem("Приостановить автоматизацию", toggle_pause, checked=paused),
        pystray.MenuItem("Папка данных", open_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", quit_app))
    try:
        icon = pystray.Icon(APP_ID, _tray_image(), APP_NAME + " " + VERSION, menu)
        threading.Thread(target=icon.run, name="hkcl-tray", daemon=True).start()
        UI["tray"] = icon
        core.NOTIFIERS.append(notify)
        return icon
    except Exception as e:
        S.log("Не удалось создать значок в трее: %s" % e, "WARN")
        return None


def notify(title, message):
    t = UI.get("tray")
    if t and S.get_config()["automation"].get("notify", True):
        try:
            t.notify(message[:250], title[:60])
        except Exception:
            pass


# ------------------------------------------------------------------ окно

def show_window(port=None):
    w = UI.get("window")
    if w is not None:
        try:
            w.show()
            w.restore()
            return
        except Exception:
            pass
    webbrowser.open("http://127.0.0.1:%d/" % (port or api.SERVER["port"]))


def quit_all():
    UI["quitting"] = True
    try:
        if UI.get("tray"):
            UI["tray"].stop()
    except Exception:
        pass
    try:
        if UI.get("window"):
            UI["window"].destroy()
    except Exception:
        pass
    automation.stop_scheduler()
    threading.Timer(1.0, api.shutdown).start()


def run_gui(hidden=False):
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VyaselevRR.HKCaseLawMonitor")
        except Exception:
            pass
    port = running_instance()
    if port:  # уже запущено — показать окно существующего экземпляра
        if not hidden:
            try:
                call_instance(port, "POST", "/api/v1/app/show")
            except Exception:
                webbrowser.open("http://127.0.0.1:%d/" % port)
        return 0
    port = api.start(S.get_config().get("settings", {}).get("port") or 8767)
    automation.start_scheduler()
    tray = start_tray(port)
    api.SERVER["on_show"] = lambda: show_window(port)
    api.SERVER["on_shutdown"] = quit_all
    url = "http://127.0.0.1:%d/" % port
    use_window = True
    try:
        import webview
    except Exception as e:
        S.log("pywebview недоступен: %s" % e, "ERROR")
        use_window = False
    if use_window and IS_WIN and not webview2_available():
        use_window = False
        if not hidden:
            r = message_box("Для собственного окна программы нужен компонент Microsoft Edge WebView2 Runtime.\n\n"
                            "Он бесплатный и ставится без прав администратора. Открыть страницу загрузки Microsoft?\n\n"
                            "Пока компонент не установлен, программа откроется в браузере по умолчанию.", kind=0x24)
            if r == 6:
                webbrowser.open(WEBVIEW2_URL)
    if not use_window:
        if not hidden:
            webbrowser.open(url)
        while not UI["quitting"]:
            time.sleep(1)
        return 0
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    start_hidden = hidden or S.get_config()["settings"].get("start_minimized")
    w = webview.create_window(APP_NAME + " " + VERSION, url, width=1320, height=880, min_size=(900, 600),
                              hidden=bool(start_hidden), text_select=True)
    UI["window"] = w

    def on_closing():
        if UI["quitting"]:
            return True
        if S.get_config()["settings"].get("minimize_to_tray", True) and UI.get("tray"):
            threading.Thread(target=w.hide, daemon=True).start()
            notify(APP_NAME, "Программа продолжает работать в области уведомлений")
            return False
        quit_all()
        return True

    w.events.closing += on_closing
    webview.start(gui="edgechromium" if IS_WIN else None, private_mode=False, icon=ICON,
                  storage_path=S.path("webview", ".keep") and os.path.join(S.data_dir(), "webview"))
    if not UI["quitting"]:
        quit_all()
    return 0


def run_once():
    port = running_instance()
    if port:
        try:
            res = call_instance(port, "POST", "/api/v1/automation/run", {}, actor="scheduler")
            S.log("Цикл по расписанию выполнен запущенной программой: %s" % json.dumps(res, ensure_ascii=False)[:300])
            return 0
        except Exception as e:
            S.log("Не удалось передать цикл запущенной программе: %s" % e, "WARN")
    res = core.run_cycle(actor="scheduler")
    return 1 if res.get("error") else 0


def serve(port=None, block=True):
    p = api.start(port or 8767)
    automation.start_scheduler()
    if block:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return p
