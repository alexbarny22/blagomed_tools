# -*- coding: utf-8 -*-
"""Ставит недостающие библиотеки. Уже установленные не трогает и в интернет за ними не ходит."""
import importlib
import subprocess
import sys

NEED = {"aiogram": "aiogram", "rapidfuzz": "rapidfuzz", "PIL": "pillow", "requests": "requests"}
if len(sys.argv) > 1 and sys.argv[1] == "config":
    # первый запуск: создаём config.py из образца и открываем в Блокноте
    import os
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = os.path.join(here, "config.py")
    if not os.path.exists(cfg):
        shutil.copy(os.path.join(here, "config.example.py"), cfg)
        print("Создан config.py — заполните его в Блокноте, сохраните и запустите start.bat снова.")
        subprocess.call(["notepad", cfg])
        sys.exit(2)
    sys.exit(0)
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
