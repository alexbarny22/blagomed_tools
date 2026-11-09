# -*- coding: utf-8 -*-
"""Проверка окружения перед запуском «Себестоимость МП» (Windows).

    python tools/check_env.py [--base DIR]

Коды: 0 — всё обязательноe в порядке (могут быть предупреждения),
1 — есть ошибки, 2 — неверный каталог/конфигурация.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

problems: list[str] = []
warnings: list[str] = []


def ok(msg: str) -> None:
    print(f'[OK]   {msg}')


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f'[WARN] {msg}')


def fail(msg: str) -> None:
    problems.append(msg)
    print(f'[FAIL] {msg}')


def check_imports() -> None:
    for mod in ('flask', 'waitress', 'openpyxl', 'werkzeug'):
        try:
            __import__(mod)
            ok(f'модуль {mod} импортируется')
        except Exception as e:                          # noqa: BLE001
            fail(f'модуль {mod} недоступен: {e}')
    try:
        __import__('win32com.client')
        ok('pywin32 доступен (режим 1С COM)')
    except Exception as e:                              # noqa: BLE001
        warn(f'pywin32/COM недоступен ({e}); 1С COM не будет работать, '
             'ручные цены и снимки доступны')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Проверка окружения')
    ap.add_argument('--base', default=os.environ.get('BLAGOMED_BASE',
                                                     str(BASE)))
    args = ap.parse_args(argv)
    base = Path(args.base).expanduser().resolve()
    print(f'Каталог установки: {base}')
    if not (base / 'app' / 'web' / 'server.py').exists():
        fail('не найден app/web/server.py — проверьте --base')
        return 2
    v = sys.version_info
    ok(f'Python {v.major}.{v.minor}.{v.micro} ({sys.executable})') \
        if v >= (3, 10) else fail(f'Python {v.major}.{v.minor} старше 3.10')
    check_imports()

    ini = base / 'config.ini'
    if ini.exists():
        ok(f'config.ini найден: {ini}')
    else:
        warn(f'config.ini отсутствует: скопируйте config.example.ini в '
             f'{ini} и заполните [auth]/[1C]')
    try:
        from app.web.config import ConfigError, load_config
        from app.web import prices, store
        cfg = load_config(base)
        ok(f'конфигурация загружена; адрес {cfg.host}:{cfg.port}')
    except ConfigError as e:
        fail(f'конфигурация: {e}')
        return 1
    except Exception as e:                              # noqa: BLE001
        fail(f'конфигурация не загружена: {e}')
        return 1
    if cfg.auth_configured:
        ok(f'вход настроен для пользователя {cfg.auth_user!r}')
    else:
        fail('вход не настроен: [auth] user/pass_hash отсутствуют или '
             'содержат заготовки; без входа интерфейс закрыт')
    onec = prices.onec_status(cfg)
    if onec['configured']:
        ok(f"1С настроена: {onec['server']}/{onec['base']} (цена: "
           f"{onec['price_type']})")
    else:
        warn('1С не настроена: ' + onec['message'])

    for name in ('data', 'uploads', 'output', 'logs', 'backup'):
        p = base / name
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / '.write_probe'
            probe.write_text('ok', encoding='utf-8')
            probe.unlink()
            ok(f'каталог {name} доступен для записи: {p}')
        except OSError as e:
            fail(f'каталог {name} недоступен для записи: {e}')
    free = shutil.disk_usage(base).free
    if free >= 2 * 1024 ** 3:
        ok(f'свободно на диске: {free / (1024 ** 3):.1f} ГБ')
    else:
        warn(f'мало места на диске: {free / (1024 ** 3):.1f} ГБ')

    if cfg.db_path.exists():
        info = store.db_info(cfg)
        if info['integrity'] == 'ok':
            ok(f"БД: {cfg.db_path}; schema={info['user_version']}, "
               f"размер={info['size']} байт")
        else:
            fail(f"БД {cfg.db_path}: integrity={info['integrity']} "
                 f"({info.get('error')})")
    else:
        warn(f'БД ещё не создана: {cfg.db_path}; создайте её на странице '
             '«База данных» после входа')

    try:
        with socket.socket() as s:
            s.bind((cfg.host, cfg.port))
        ok(f'порт {cfg.port} свободен')
    except OSError as e:
        warn(f'порт {cfg.port} занят ({e}); остановите старый процесс '
             'или измените port в config.ini')

    print()
    print(f'Итог: ошибок {len(problems)}, предупреждений {len(warnings)}')
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())