# HK Case Law Monitor 2.0.1 для Windows

© 2026 Вяселев Руслан Равилевич / Vyaselev Ruslan Ravilevich. Бесплатно для частного некоммерческого использования (лицензионное соглашение — в установщике и в `src/app/web/legal/EULA.txt`).

**Скачать:** [HKCaseLawMonitor-Setup-2.0.1.exe](https://luxtendebant-ux.github.io/case-law/program/HKCaseLawMonitor-Setup-2.0.1.exe) (14507202 байт)
SHA-256: `8447239f3acc715c9b86b98a8060812aec1cbdbd30dce854b78bf00cf84a4e56`

## Изменения
- 2.0.1 (28.10.2026): исправлен запуск окна (ошибка «Cannot find win-arm64» в 2.0.0); при любом сбое окна программа открывается в браузере и продолжит работу в области уведомлений; в окне включено контекстное меню (копировать/вставить). Устанавливается поверх 2.0.0, данные сохраняются.

## Требования
- Windows 10 или 11, 64 бит. Права администратора не нужны: установка в `%LOCALAPPDATA%\Programs\HK Case Law Monitor`.
- Python и все компоненты встроены.
- Собственное окно — Microsoft Edge WebView2 Runtime (есть в Windows 11; при отсутствии программа предложит установить и пока откроется в браузере); .NET Framework 4.7.2+.
- Установщик без цифровой подписи: SmartScreen может предупредить — «Подробнее» → «Выполнить в любом случае».

## Возможности
Поиск по базам (LRS, hkcfa.hk, HKLII, vLex, Westlaw, Lexis+, свои источники) и по собранной базе; мониторинг RSS-лент LRS; дела на контроле с хронологией, напоминаниями и письмами об изменениях; анализ решений (PDF/DOCX/HTML/TXT); ИИ-помощник (Claude, ChatGPT, OpenAI-совместимые API, локальные модели); ИИ-агент (MCP, REST API + OpenAPI, вебхуки, командная строка, журнал аудита); автоматизация (расписание, Планировщик заданий Windows, автозапуск).

## Подключение ИИ-агента
- Claude Desktop (`claude_desktop_config.json`): `{"mcpServers": {"hk-case-law": {"command": "<папка программы>\\hkcl.exe", "args": ["mcp"]}}}`
- REST API: `http://127.0.0.1:8767/api/v1`, `Authorization: Bearer <токен>`, спецификация `/api/v1/openapi.json`.

## Исходный код и сборка
`src/app` — программа (Python), `src/build` — сборка: встраиваемый Python 3.13.12 (amd64), пакеты (pywebview, pythonnet, pystray, Pillow, pypdf), запускатели (MinGW-w64), установщик (NSIS).
