# -*- coding: utf-8 -*-
"""Ставит недостающие библиотеки. Уже установленные не трогает и в интернет за ними не ходит."""
import importlib
import subprocess
import sys

NEED = {"aiogram": "aiogram", "rapidfuzz": "rapidfuzz", "PIL": "pillow", "requests": "requests"}
if len(sys.argv) > 1 and sys.argv[1] == "test":
    NEED = {"PIL": "pillow", "requests": "requests"}

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
        print("\nНе удалось скачать библиотеки с pypi.org. Проверьте интернет; если включён VPN или прокси —"
              "\nпопробуйте выключить (или включить) и запустите снова.")
        sys.exit(1)
