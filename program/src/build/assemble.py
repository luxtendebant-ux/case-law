import os, shutil, zipfile, glob, re, io
B = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(B)
D = os.path.join(B, "dist", "HKCaseLawMonitor")
shutil.rmtree(os.path.join(B, "dist"), ignore_errors=True); os.makedirs(D)
# 1. Python
py = os.path.join(D, "python"); os.makedirs(py)
zipfile.ZipFile(os.path.join(B, "dl", "python-3.13.12-embed-amd64.zip")).extractall(py)
pth = [f for f in os.listdir(py) if f.endswith("._pth")][0]
open(os.path.join(py, pth), "w").write("python313.zip\n.\n..\\app\n..\\lib\n")
# 2. lib
lib = os.path.join(D, "lib"); os.makedirs(lib)
notices = []
pylic = open(os.path.join(py, "LICENSE.txt"), encoding="utf-8", errors="replace").read()
notices.append(("Python 3.13.12 (Python Software Foundation License)", pylic))
for whl in sorted(glob.glob(os.path.join(B, "wheels", "*.whl"))):
    name = os.path.basename(whl)
    if name.startswith("python_xlib"):
        continue
    z = zipfile.ZipFile(whl)
    lic_txt = []
    meta = ""
    for n in z.namelist():
        if ".dist-info/" in n:
            base = n.split("/")[-1].upper()
            if base == "METADATA":
                meta = z.read(n).decode("utf-8", "replace")
            if re.match(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", base) or "/licenses/" in n.lower():
                lic_txt.append(z.read(n).decode("utf-8", "replace"))
            continue
        z.extract(n, lib)
    m = re.search(r"^Name: (.+)$", meta, re.M); v = re.search(r"^Version: (.+)$", meta, re.M)
    l = re.search(r"^License(?:-Expression)?: (.+)$", meta, re.M)
    title = "%s %s (%s)" % (m.group(1) if m else name, v.group(1) if v else "", l.group(1).strip() if l else "см. текст")
    notices.append((title, "\n\n".join(lic_txt) or "Лицензия: " + (l.group(1) if l else "см. метаданные пакета")))
# Пакеты копируются целиком: pywebview при запуске обращается к компонентам всех архитектур (win-arm64/x64/x86).
notices.append(("SheetJS Community Edition 0.18.5 (Apache License 2.0)",
                "Copyright (C) 2012-present SheetJS LLC. Licensed under the Apache License, Version 2.0: http://www.apache.org/licenses/LICENSE-2.0"))
notices.append(("Microsoft Edge WebView2 SDK (Microsoft.Web.WebView2.Core.dll, Microsoft.Web.WebView2.WinForms.dll, WebView2Loader.dll; поставляются в составе pywebview)",
                "Copyright (C) Microsoft Corporation. Распространяется на условиях лицензии Microsoft для WebView2 SDK: https://www.nuget.org/packages/Microsoft.Web.WebView2/ (раздел License). "
                "Среда выполнения Microsoft Edge WebView2 Runtime в состав программы не входит и устанавливается средствами Windows/Microsoft."))
# 3. app
app = os.path.join(D, "app")
shutil.copytree(os.path.join(ROOT, "app", "hkcl"), os.path.join(app, "hkcl"), ignore=shutil.ignore_patterns("__pycache__"))
shutil.copytree(os.path.join(ROOT, "app", "web"), os.path.join(app, "web"))
shutil.copy(os.path.join(ROOT, "app", "main.py"), app)
shutil.copy(os.path.join(B, "app.ico"), os.path.join(app, "web", "app.ico"))
txt = "СТОРОННИЕ КОМПОНЕНТЫ / THIRD-PARTY NOTICES\nHK Case Law Monitor 2.0.0 — © 2026 Вяселев Руслан Равилевич / Vyaselev Ruslan Ravilevich\n\n" + \
      "В состав программы входят компоненты третьих лиц, распространяемые на условиях их лицензий:\n\n" + \
      "\n".join("  * " + t for t, _ in notices) + "\n\n" + "\n\n".join("=" * 78 + "\n" + t + "\n" + "=" * 78 + "\n" + body.strip() for t, body in notices) + "\n"
open(os.path.join(app, "web", "legal", "THIRD-PARTY-NOTICES.txt"), "w", encoding="utf-8").write(txt)
open(os.path.join(ROOT, "app", "web", "legal", "THIRD-PARTY-NOTICES.txt"), "w", encoding="utf-8").write(txt)
open(os.path.join(D, "THIRD-PARTY-NOTICES.txt"), "w", encoding="utf-8-sig").write(txt)
open(os.path.join(B, "EULA.txt"), "w", encoding="utf-8-sig").write(open(os.path.join(ROOT, "app", "web", "legal", "EULA.txt"), encoding="utf-8").read())
open(os.path.join(D, "EULA.txt"), "w", encoding="utf-8-sig").write(open(os.path.join(ROOT, "app", "web", "legal", "EULA.txt"), encoding="utf-8").read())
# 4. exe, icon
for f in ("HKCaseLawMonitor.exe", "hkcl.exe"):
    shutil.copy(os.path.join(B, "launcher", f), D)
shutil.copy(os.path.join(B, "app.ico"), D)
total = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(D) for f in fs)
print("собрано: %s, %.1f МБ, компонентов в notices: %d" % (D, total / 1e6, len(notices)))
for t, _ in notices: print(" -", t[:100])
