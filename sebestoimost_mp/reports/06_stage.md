# Этап 6 — Windows-установщик — PASS

Дата: 2026-11-24. Разработчик: kimi (мелкие файлы — без Polza, по правилам экономии).

## Объём
- `INSTALL.cmd` — проверка Python (py -3 / python), venv, pip install -r requirements.txt;
  config.ini создаётся из примера ТОЛЬКО если отсутствует; существующая БД не перезаписывается.
- `START.cmd` — запуск через venv\Scripts\python.exe run.py.
- `STOP.cmd` — остановка процесса python с run.py в командной строке (PowerShell/CIM).
- Требования ТЗ: CRLF во всех трёх файлах, OEM-вывод (файлы в cp866 + chcp 866), пути с пробелами в кавычках.

## Проверки
- pytest 54/54 (47 прежних + 7 новых tests/test_installer.py: наличие, CRLF, chcp 866, venv/requirements, защита config.ini и БД, старт через venv, стоп по run.py).

## Решение контроллера
PASS. current_stage -> 7.
