# -*- coding: utf-8 -*-
"""
Автообновление репрайсера из GitHub. Вызывается из app.py при каждом запуске
(и с ярлыка, и при автозапуске) и из 2_START.bat.

Сверяет version.txt; если на GitHub версия новее — скачивает файлы из manifest.txt.
В отличие от update.py других ботов, понимает подпапки (core/, templates/, static/).
Папки data/ (база с ключами Ozon, история цен) и venv/ никогда не трогаются.

GitHub из России открывается не везде одинаково (raw.githubusercontent.com часто режется),
поэтому файлы пробуются по очереди из нескольких адресов. Если ни один не ответил —
просто запускается текущая версия.
"""
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "alexbarny22/blagomed_tools"
BRANCH = "main"
FOLDER = "ozon_repricer"          # папка проекта внутри репозитория
HERE = os.path.dirname(os.path.abspath(__file__))
PROTECTED_DIRS = ("data/", "venv/")
PROTECTED_FILES = {"2_START.bat"}  # bat-файл может быть запущен прямо сейчас
UA = {"User-Agent": "blagomed-updater", "Cache-Control": "no-cache"}


def token():
    try:
        sys.path.insert(0, HERE)
        import config  # необязательный локальный файл
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


def safe_name(name):
    """Только относительные пути внутри папки программы, без data/ и venv/."""
    name = name.strip().replace("\\", "/")
    if not name or name.startswith("/") or ":" in name or ".." in name.split("/"):
        return None
    if name.startswith(PROTECTED_DIRS) or name in PROTECTED_FILES:
        return None
    return name


def install_requirements():
    """Если поменялся requirements.txt — доустановить библиотеки в venv."""
    exe = sys.executable
    console = os.path.join(os.path.dirname(exe), "python.exe")
    if os.path.basename(exe).lower() == "pythonw.exe" and os.path.exists(console):
        exe = console
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        subprocess.run([exe, "-m", "pip", "install", "-q", "-r", os.path.join(HERE, "requirements.txt")],
                       cwd=HERE, timeout=600, creationflags=flags, check=False)
    except Exception as e:
        print(f"Не удалось обновить библиотеки: {e}")


def main():
    """Возвращает (старая версия, новая версия), если обновление установлено, иначе None."""
    tok = token()
    try:
        remote = fetch("version.txt", tok).decode("utf-8").strip()
    except Exception as e:
        print(f"Проверка обновлений пропущена (нет связи с GitHub): {e}")
        return None
    old = local_version()
    if remote == old:
        print(f"Версия {remote} — актуальная (через {_good[0]}).")
        return None
    print(f"Найдено обновление: {old or '—'} -> {remote} (через {_good[0]})")
    try:
        files = []
        for line in fetch("manifest.txt", tok).decode("utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                n = safe_name(line)
                if n:
                    files.append(n)
        data = {name: fetch(name, tok) for name in files}
    except Exception as e:
        print(f"Обновление не скачалось, запускаю текущую версию: {e}")
        return None
    if not data:
        print("Список файлов обновления пуст — пропускаю.")
        return None
    # записываем только когда всё скачалось — чтобы не получить половину новой версии
    req_changed = False
    for name, body in data.items():
        target = os.path.join(HERE, *name.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            with open(target, "rb") as f:
                if f.read() == body:
                    continue
        except FileNotFoundError:
            pass
        tmp = target + ".new"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, target)
        print(f"  обновлён {name}")
        if name == "requirements.txt":
            req_changed = True
    if req_changed:
        install_requirements()
    with open(os.path.join(HERE, "version.txt"), "w", encoding="utf-8") as f:
        f.write(remote)
    print("Обновление установлено.")
    return old, remote


if __name__ == "__main__":
    main()
