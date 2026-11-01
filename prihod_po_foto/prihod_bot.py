# -*- coding: utf-8 -*-
"""
Бот «Приход по фото» — запуск. Мессенджер выбирается в config.py:
  PLATFORM = "max"       — MAX (работает в России без VPN, можно запускать на сервере 1С)
  PLATFORM = "telegram"  — Telegram
Логика бота — в flow.py, переходники — max_bot.py и tg_bot.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

platform = str(getattr(config, "PLATFORM", "") or ("max" if getattr(config, "MAX_TOKEN", "") else "telegram")).lower()
if platform == "max":
    import max_bot as impl
else:
    import tg_bot as impl

if __name__ == "__main__":
    impl.run()
