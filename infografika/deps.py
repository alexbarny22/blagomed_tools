# -*- coding: utf-8 -*-
"""Ставит недостающие библиотеки, докачивает шрифты и при первом запуске создаёт config.py."""
import importlib
import os
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

if len(sys.argv) > 1 and sys.argv[1] == "config":
    cfg = os.path.join(HERE, "config.py")
    if not os.path.exists(cfg):
        shutil.copy(os.path.join(HERE, "config.example.py"), cfg)
        print("Создан config.py — впишите ключ vsellm (VSELLM_KEY), сохраните и запустите start.bat снова.")
        subprocess.call(["notepad", cfg])
        sys.exit(2)
    sys.exit(0)

NEED = {"openai": "openai", "PIL": "pillow", "requests": "requests", "flask": "flask"}
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
        print("\nНе удалось скачать библиотеки с pypi.org. Проверьте интернет; если включён VPN —"
              "\nпопробуйте выключить его и запустите снова.")
        sys.exit(1)

# Шрифты с кириллицей (Google Fonts, лицензия OFL) — если папки fonts нет или чего-то не хватает
FONTS = {
    "Montserrat.ttf": "montserrat/Montserrat%5Bwght%5D.ttf",
    "Manrope.ttf": "manrope/Manrope%5Bwght%5D.ttf",
    "GolosText.ttf": "golostext/GolosText%5Bwght%5D.ttf",
    "PT_Serif-Web-Bold.ttf": "ptserif/PT_Serif-Web-Bold.ttf",
    "PT_Serif-Web-Regular.ttf": "ptserif/PT_Serif-Web-Regular.ttf",
}
ICON_URL = "https://raw.githubusercontent.com/google/material-design-icons/master/font/MaterialIcons-Regular.ttf"
FONTS["MaterialIcons-Regular.ttf"] = ICON_URL     # иконки (Apache 2.0)
FONTS["MaterialIconsOutlined-Regular.otf"] = ICON_URL.replace("MaterialIcons-Regular.ttf", "MaterialIconsOutlined-Regular.otf")
fdir = os.path.join(HERE, "fonts")
os.makedirs(fdir, exist_ok=True)
for name, path in FONTS.items():
    dst = os.path.join(fdir, name)
    if os.path.exists(dst) and os.path.getsize(dst) > 10000:
        continue
    try:
        print("Скачиваю шрифт", name)
        url = path if path.startswith("http") else "https://raw.githubusercontent.com/google/fonts/main/ofl/" + path
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        with open(dst, "wb") as f:
            f.write(data)
    except Exception as e:      # noqa: BLE001
        print(f"  не скачался ({e}) — будет запасной шрифт Windows")
