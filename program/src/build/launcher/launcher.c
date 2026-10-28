/* HK Case Law Monitor — запускатель встроенного Python.
   © 2026 Вяселев Руслан Равилевич / Vyaselev Ruslan Ravilevich
   GUI-сборка (-DGUI):  python\pythonw.exe app\main.py <аргументы>
   CLI-сборка:          python\python.exe  app\main.py <аргументы>, наследует stdin/stdout (MCP). */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <wchar.h>

static const wchar_t *skip_argv0(const wchar_t *cl) {
    if (*cl == L'"') { cl++; while (*cl && *cl != L'"') cl++; if (*cl) cl++; }
    else { while (*cl && *cl != L' ' && *cl != L'\t') cl++; }
    while (*cl == L' ' || *cl == L'\t') cl++;
    return cl;
}

static void fail(const wchar_t *msg) {
#ifdef GUI
    MessageBoxW(NULL, msg, L"HK Case Law Monitor", MB_ICONERROR | MB_OK);
#else
    DWORD w; HANDLE h = GetStdHandle(STD_ERROR_HANDLE);
    WriteConsoleW(h, msg, (DWORD)wcslen(msg), &w, NULL);
#endif
}

#ifndef GUI
static HANDLE valid_or_nul(HANDLE h, BOOL write) {
    if (h && h != INVALID_HANDLE_VALUE) {
        SetLastError(0);
        DWORD t = GetFileType(h);
        if (t != FILE_TYPE_UNKNOWN || GetLastError() == NO_ERROR) {
            if (SetHandleInformation(h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)) return h;
        }
    }
    SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
    return CreateFileW(L"NUL", write ? GENERIC_WRITE : GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, NULL);
}
#endif

int run(void) {
    wchar_t exe[MAX_PATH * 2], dir[MAX_PATH * 2], py[MAX_PATH * 2], script[MAX_PATH * 2];
    DWORD n = GetModuleFileNameW(NULL, exe, MAX_PATH * 2);
    if (!n || n >= MAX_PATH * 2) { fail(L"Не удалось определить путь программы."); return 1; }
    wcscpy(dir, exe);
    wchar_t *slash = wcsrchr(dir, L'\\'); if (slash) *slash = 0;
#ifdef GUI
    swprintf(py, MAX_PATH * 2, L"%ls\\python\\pythonw.exe", dir);
#else
    swprintf(py, MAX_PATH * 2, L"%ls\\python\\python.exe", dir);
#endif
    swprintf(script, MAX_PATH * 2, L"%ls\\app\\main.py", dir);
    if (GetFileAttributesW(py) == INVALID_FILE_ATTRIBUTES || GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES) {
        fail(L"Файлы программы повреждены или удалены. Переустановите HK Case Law Monitor.\r\n"); return 2;
    }
    const wchar_t *args = skip_argv0(GetCommandLineW());
    size_t len = wcslen(py) + wcslen(script) + wcslen(args) + 16;
    wchar_t *cmd = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, len * sizeof(wchar_t));
    swprintf(cmd, len, L"\"%ls\" \"%ls\" %ls", py, script, args);

    /* изоляция встроенного Python от переменных другого Python на компьютере */
    const wchar_t *clear[] = {L"PYTHONHOME", L"PYTHONPATH", L"PYTHONSTARTUP", L"PYTHONUNBUFFERED", L"PYTHONLEGACYWINDOWSSTDIO",
                              L"PYTHONINSPECT", L"PYTHONOPTIMIZE", L"PYTHONDEBUG", L"PYTHONVERBOSE", L"PYTHONSAFEPATH", NULL};
    for (int i = 0; clear[i]; i++) SetEnvironmentVariableW(clear[i], NULL);
    SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
    SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    SetEnvironmentVariableW(L"HKCL_LAUNCHER", exe);

    STARTUPINFOW si; PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si)); si.cb = sizeof(si); ZeroMemory(&pi, sizeof(pi));
    BOOL inherit = FALSE;
#ifndef GUI
    HANDLE hin = valid_or_nul(GetStdHandle(STD_INPUT_HANDLE), FALSE);
    HANDLE hout = valid_or_nul(GetStdHandle(STD_OUTPUT_HANDLE), TRUE);
    HANDLE herr = valid_or_nul(GetStdHandle(STD_ERROR_HANDLE), TRUE);
    si.dwFlags = STARTF_USESTDHANDLES; si.hStdInput = hin; si.hStdOutput = hout; si.hStdError = herr;
    inherit = TRUE;
    SetConsoleCtrlHandler(NULL, TRUE);
#endif
    /* явный блок окружения без переменных другого Python */
    wchar_t *envsrc = GetEnvironmentStringsW();
    size_t total = 0; for (wchar_t *e = envsrc; *e; e += wcslen(e) + 1) total += wcslen(e) + 1;
    wchar_t *env = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, (total + 2) * sizeof(wchar_t)), *w = env;
    for (wchar_t *e = envsrc; *e; e += wcslen(e) + 1) {
        int skip = 0;
        for (int i = 0; clear[i]; i++) { size_t L = wcslen(clear[i]); if (!_wcsnicmp(e, clear[i], L) && e[L] == L'=') { skip = 1; break; } }
        if (!skip) { wcscpy(w, e); w += wcslen(e) + 1; }
    }
    *w = 0;
    FreeEnvironmentStringsW(envsrc);
    if (!CreateProcessW(py, cmd, NULL, NULL, inherit, CREATE_UNICODE_ENVIRONMENT, env, dir, &si, &pi)) {
        wchar_t m[512]; swprintf(m, 512, L"Не удалось запустить программу (код %lu).\r\n", GetLastError()); fail(m); return 3;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 0; GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
    return (int)code;
}

#ifdef GUI
int WINAPI wWinMain(HINSTANCE a, HINSTANCE b, PWSTR c, int d) { return run(); }
#else
int wmain(void) { return run(); }
#endif
