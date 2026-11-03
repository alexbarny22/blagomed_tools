# -*- coding: utf-8 -*-
"""Ставит недостающие библиотеки. Уже установленные не трогает и в интернет за ними не ходит."""
import importlib
import subprocess
import sys

NEED = {"vk_api": "vk_api", "requests": "requests", "tenacity": "tenacity",
        "gspread": "gspread", "google.oauth2": "google-auth"}

missing = []
for mod, pkg in NEED.items():
    try:
        importlib.import_module(mod)
    except ImportError:
        missing.append(pkg)

if missing:
    print("Устанавливаю библиотеки:", ", ".join(missing))
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                          "--timeout", "60", "--retries", "2"] + missing)
    if rc != 0:
        print("\nНе удалось скачать библиотеки с pypi.org. Проверьте интернет и запустите снова.")
        sys.exit(1)
