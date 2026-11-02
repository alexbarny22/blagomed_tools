# -*- coding: utf-8 -*-
"""
Автообновление бота из GitHub. Запускается из start.bat перед ботом.
Сверяет version.txt; если на GitHub версия новее — скачивает файлы из manifest.txt.
config.py, settings.json, кэш и журнал никогда не трогаются.

GitHub из России открывается не везде одинаково (raw.githubusercontent.com часто режется),
поэтому файлы пробуются по очереди из нескольких адресов. Если ни один не ответил —
просто запускается текущая версия.
"""
import os
import ssl
import sys
import urllib.request

REPO = "alexbarny22/blagomed_tools"
BRANCH = "main"
FOLDER = "direct_analytics"          # папка проекта внутри репозитория
HERE = os.path.dirname(os.path.abspath(__file__))
PROTECTED = {"config.py", "data_cache.json", "direct_report.log", "start.bat"}
UA = {"User-Agent": "blagomed-updater", "Cache-Control": "no-cache"}


def token():
    try:
        sys.path.insert(0, HERE)
        import config
        return getattr(config, "GITHUB_TOKEN", "") or ""
    except Exception:
        return ""


def sources(name, tok):
    path = f"{FOLDER}/{name}"
    api = {"Accept": "application/vnd.github.raw", **UA}
    if tok:
        api["Authorization"] = "Bearer " + tok
    yield "api.github.com", f"https://api.github.com/repos/{REPO}/contents/{path}?ref={BRANCH}", api
    yield "raw.githubusercontent.com", f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{path}", UA
    yield "github.com", f"https://github.com/{REPO}/raw/{BRANCH}/{path}", UA
    if not tok:  # зеркало (только для публичного репозитория, может отставать на несколько минут)
        yield "jsdelivr", f"https://cdn.jsdelivr.net/gh/{REPO}@{BRANCH}/{path}", UA


def _get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except (ssl.SSLError, urllib.error.URLError) as e:
        # запасной путь — requests (свои сертификаты), если установлен
        try:
            import requests
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            return r.content
        except ImportError:
            raise e


_good = []   # источник, который сработал — дальше пробуем его первым


def fetch(name, tok):
    errors = []
    srcs = list(sources(name, tok))
    if _good:
        srcs.sort(key=lambda s: s[0] != _good[0])
    for title, url, headers in srcs:
        try:
            data = _get(url, headers)
            if title not in _good:
                _good[:] = [title]
            return data
        except Exception as e:
            errors.append(f"{title}: {str(e)[:120]}")
    raise RuntimeError("; ".join(errors))


def local_version():
    try:
        with open(os.path.join(HERE, "version.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def main():
    tok = token()
    try:
        remote = fetch("version.txt", tok).decode("utf-8").strip()
    except Exception as e:
        print(f"Проверка обновлений пропущена (нет связи с GitHub): {e}")
        return
    if remote == local_version():
        print(f"Версия {remote} — актуальная (через {_good[0]}).")
        return
    print(f"Найдено обновление: {local_version() or '—'} -> {remote} (через {_good[0]})")
    try:
        files = [x.strip() for x in fetch("manifest.txt", tok).decode("utf-8").splitlines()
                 if x.strip() and not x.startswith("#")]
        data = {}
        for name in files:
            if name in PROTECTED or "/" in name or "\\" in name or ".." in name:
                continue
            data[name] = fetch(name, tok)
    except Exception as e:
        print(f"Обновление не скачалось, запускаю текущую версию: {e}")
        return
    # записываем только когда всё скачалось — чтобы не получить половину новой версии
    for name, body in data.items():
        tmp = os.path.join(HERE, name + ".new")
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, os.path.join(HERE, name))
        print(f"  обновлён {name}")
    with open(os.path.join(HERE, "version.txt"), "w", encoding="utf-8") as f:
        f.write(remote)
    print("Обновление установлено.")


if __name__ == "__main__":
    main()
