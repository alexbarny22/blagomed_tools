#!/bin/bash
# Запуск на Timeweb (Crontab): обновление из GitHub + отчёт
cd "$(dirname "$0")"
python3 update.py
python3 direct_report.py
