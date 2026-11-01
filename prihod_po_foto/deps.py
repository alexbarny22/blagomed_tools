# -*- coding: utf-8 -*-
"""Ставит недостающие библиотеки. Уже установленные не трогает и в интернет за ними не ходит."""
import importlib
import subprocess
import sys

NEED = {"rapidfuzz": "rapidfuzz", "PIL": "pillow", "requests": "requests", "openpyxl": "openpyxl"}
# библиотека мессенджера — только та, что нужна (MAX или Telegram)
try:
    import os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import config as _cfg
    _pl = str(getattr(_cfg, "PLATFORM", "") or ("max" if getattr(_cfg, "MAX_TOKEN", "") else "telegram")).lower()
except Exception:
    _pl = "max"
if _pl == "max":
    NEED["maxapi"] = "maxapi>=1.2.2"
else:
    NEED["aiogram"] = "aiogram"
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
