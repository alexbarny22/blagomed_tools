# -*- coding: utf-8 -*-
"""
Бот «Приход по фото» — запуск. Мессенджер выбирается в config.py:
  PLATFORM = "max"       — MAX (работает в России без VPN, можно запускать на сервере 1С)
  PLATFORM = "telegram"  — Telegram
Логика бота — в flow.py, переходники — max_bot.py и tg_bot.py.

При каждом запуске бот сам проверяет обновления на GitHub и ставит недостающие библиотеки —
поэтому из пульта BotPanel обновление = кнопка «Перезапуск». Кроме того, раз в час бот сам
проверяет GitHub и, если вышла новая версия и никто не проводит приход, обновляется и перезапускается.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)


def self_update():
    try:
        import update
        update.main()
    except Exception as ex:
        print(f"Проверка обновлений не удалась: {ex}")
    try:
        subprocess.call([sys.executable, os.path.join(HERE, "deps.py")])
    except Exception as ex:
        print(f"Проверка библиотек не удалась: {ex}")


if "--no-update" not in sys.argv:
    self_update()

import config  # noqa: E402

platform = str(getattr(config, "PLATFORM", "") or ("max" if getattr(config, "MAX_TOKEN", "") else "telegram")).lower()
if platform == "max":
    import max_bot as impl
else:
    import tg_bot as impl

if __name__ == "__main__":
    impl.run()
