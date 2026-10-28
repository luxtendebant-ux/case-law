# -*- coding: utf-8 -*-
"""HK Case Law Monitor — точка входа.
  (без аргументов)   окно программы
  --background       запуск в области уведомлений (автозапуск)
  --run-once         один цикл автоматизации (Планировщик заданий Windows)
  <команда>          командная строка (hkcl.exe): mcp, status, check, cases …
© 2026 Вяселев Руслан Равилевич / Vyaselev Ruslan Ravilevich"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
LIB = os.path.join(os.path.dirname(HERE), "lib")
if os.path.isdir(LIB) and LIB not in sys.path:
    sys.path.insert(1, LIB)


def _fix_streams():
    # pythonw.exe: stdout/stderr = None
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def main():
    _fix_streams()
    args = sys.argv[1:]
    if args and args[0] == "--data" and len(args) > 1:
        from hkcl import store
        store.set_data_dir(args[1])
        args = args[2:]
    if not args or args[0] in ("--gui", "--background"):
        from hkcl import app
        try:
            return app.run_gui(hidden=bool(args) and args[0] == "--background")
        except Exception as e:
            import traceback
            from hkcl import store
            store.log("Сбой запуска окна: %s\n%s" % (e, traceback.format_exc()), "ERROR")
            app.message_box("Не удалось запустить программу:\n%s\n\nПодробности — в журнале:\n%s" % (e, store.path("logs", "hkcl.log")), kind=0x10)
            return 1
    if args[0] == "--run-once":
        from hkcl import app
        return app.run_once()
    from hkcl import cli
    return cli.main(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
