# -*- coding: utf-8 -*-
"""Управление локальным веб-сервером «Себестоимость МП» (Windows).

    python tools/web_control.py start [--base DIR]
    python tools/web_control.py stop  [--base DIR]
    python tools/web_control.py status [--base DIR]

Сервер запускается отдельным процессом, PID пишется в data/web.pid,
stdout/stderr — в logs/server.out. Остановка — только процесса из PID-файла
после проверки командной строки (защита от остановки чужого процесса).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from app.web.config import ConfigError, load_config  # noqa: E402

IS_WINDOWS = os.name == 'nt'
CREATE_DETACHED = (getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) |
                   getattr(subprocess, 'DETACHED_PROCESS', 0)) if IS_WINDOWS \
    else 0


def paths(base: Path) -> dict:
    return {'pid': base / 'data' / 'web.pid',
            'out': base / 'logs' / 'server.out',
            'base': base}


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return None


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        out = subprocess.run(
            ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
            capture_output=True, text=True, timeout=10,
            encoding='oem', errors='replace')
        return str(pid) in (out.stdout or '')
    except (OSError, subprocess.SubprocessError):
        return False


def server_command_line(pid: int) -> str:
    if not IS_WINDOWS:
        return ''
    try:
        ps = ("(Get-CimInstance Win32_Process -Filter "
              f"\"ProcessId={pid}\").CommandLine")
        out = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                             capture_output=True, text=True, timeout=20,
                             encoding='utf-8', errors='replace')
        return (out.stdout or '').strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def health(cfg) -> tuple[bool, str]:
    host = '127.0.0.1' if cfg.host in ('0.0.0.0', '::') else cfg.host
    url = f'http://{host}:{cfg.port}/healthz'
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode('utf-8'))
        return True, json.dumps(data, ensure_ascii=False)
    except Exception as e:                             # noqa: BLE001
        return False, str(e)


def cmd_start(base: Path) -> int:
    p = paths(base)
    pid = read_pid(p['pid'])
    if alive(pid):
        print(f'Сервер уже запущен: PID {pid}')
        return 0
    p['pid'].parent.mkdir(parents=True, exist_ok=True)
    p['out'].parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg = load_config(base)
    except ConfigError as e:
        print(f'Конфигурация не загружена: {e}', file=sys.stderr)
        return 2
    if not cfg.auth_configured:
        print('Вход не настроен. Заполните [auth] user/pass_hash в '
              f'{cfg.config_ini}\n'
              'Хэш: python tools/hash_password.py "<пароль>"',
              file=sys.stderr)
        return 2
    env = os.environ.copy()
    env.update({'BLAGOMED_HOME': str(base), 'PYTHONUTF8': '1',
                'PYTHONIOENCODING': 'utf-8'})
    with open(p['out'], 'ab') as log:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'app.web.server', '--base', str(base)],
            cwd=str(base), env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=CREATE_DETACHED)
    p['pid'].write_text(str(proc.pid), encoding='utf-8')
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            p['pid'].unlink(missing_ok=True)
            print(f'Сервер завершился с кодом {proc.returncode}. '
                  f'Журнал: {p["out"]}', file=sys.stderr)
            return 1
        ok, detail = health(cfg)
        if ok:
            print(f'Сервер запущен: http://{cfg.host}:{cfg.port} '
                  f'(PID {proc.pid})')
            print(f'Журнал: {p["out"]}')
            return 0
        time.sleep(0.5)
    subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                   capture_output=True)
    p['pid'].unlink(missing_ok=True)
    print(f'Сервер не ответил за 40 с. Журнал: {p["out"]}', file=sys.stderr)
    return 1


def cmd_stop(base: Path) -> int:
    p = paths(base)
    pid = read_pid(p['pid'])
    if not pid:
        print('PID-файл не найден: сервер не запущен через start.bat')
        return 0
    if not alive(pid):
        p['pid'].unlink(missing_ok=True)
        print('Процесс уже завершён; PID-файл удалён.')
        return 0
    cmd = server_command_line(pid)
    if 'app.web.server' not in cmd:
        print(f'PID {pid} не принадлежит веб-серверу; остановка отменена. '
              f'Командная строка: {cmd or "недоступна"}', file=sys.stderr)
        return 3
    subprocess.run(['taskkill', '/PID', str(pid), '/T'], capture_output=True)
    deadline = time.time() + 3
    while time.time() < deadline:
        if not alive(pid):
            p['pid'].unlink(missing_ok=True)
            print(f'Сервер остановлен (PID {pid}).', flush=True)
            return 0
        time.sleep(0.3)
    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                   capture_output=True)
    time.sleep(1)
    if alive(pid):
        print(f'Не удалось остановить PID {pid}', file=sys.stderr, flush=True)
        return 1
    p['pid'].unlink(missing_ok=True)
    print(f'Сервер остановлен принудительно (PID {pid}).', flush=True)
    return 0


def cmd_status(base: Path) -> int:
    p = paths(base)
    pid = read_pid(p['pid'])
    print(f'Каталог: {base}')
    print(f'PID: {pid if alive(pid) else "не запущен"}')
    try:
        cfg = load_config(base)
    except ConfigError as e:
        print(f'Конфигурация: ошибка — {e}')
        return 1
    ok, detail = health(cfg)
    print(f'Адрес: http://{cfg.host}:{cfg.port}')
    print(f'healthz: {"OK" if ok else "НЕДОСТУПЕН"} — {detail}')
    print(f'Вход: {"настроен" if cfg.auth_configured else "НЕ НАСТРОЕН"}')
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Управление веб-сервером')
    ap.add_argument('action', choices=('start', 'stop', 'status'))
    ap.add_argument('--base', default=os.environ.get('BLAGOMED_BASE',
                                                     str(BASE)))
    args = ap.parse_args(argv)
    base = Path(args.base).expanduser().resolve()
    if not (base / 'app' / 'web' / 'server.py').exists():
        print(f'Каталог установки не найден: {base}', file=sys.stderr)
        return 2
    return {'start': cmd_start, 'stop': cmd_stop,
            'status': cmd_status}[args.action](base)


if __name__ == '__main__':
    raise SystemExit(main())