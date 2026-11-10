# -*- coding: utf-8 -*-
"""Проверка BAT-файлов: каталог запуска и отказ от чужой копии."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != 'nt',
                                reason='BAT-файлы проверяются только на Windows')


def _make_base(tmp_path: Path, name: str) -> Path:
    base = tmp_path / name
    (base / 'app' / 'web').mkdir(parents=True)
    (base / 'tools').mkdir()
    for rel in ('app/web/server.py', 'tools/web_control.py',
                'tools/check_env.py', 'tools/hash_password.py'):
        (base / rel).write_text('# заглушка для проверки BAT\n',
                                encoding='utf-8')
    (base / 'config.example.ini').write_text('[web]\nport = 8085\n',
                                             encoding='utf-8')
    (base / 'requirements.txt').write_text('', encoding='utf-8')
    (base / 'config.ini').write_text('[auth]\nuser = test\n',
                                     encoding='utf-8')
    for name_ in ('start.bat', 'stop.bat', 'env-check.bat'):
        shutil.copy2(ROOT / name_, base / name_)
    return base


def _run(base: Path, bat: str, *args: str, env_base: Path | None = None):
    env = os.environ.copy()
    env.pop('BLAGOMED_BASE', None)
    if env_base is not None:
        env['BLAGOMED_BASE'] = str(env_base)
    return subprocess.run(
        ['cmd', '/c', str(base / bat), *args],
        cwd=str(base), env=env, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=30)


def test_start_uses_its_own_directory_without_env(tmp_path):
    base = _make_base(tmp_path, 'own-copy')
    proc = _run(base, 'start.bat', '--check')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '[OK] Каталог запуска:' in proc.stdout
    assert str(base).lower() in proc.stdout.lower()


def test_start_respects_explicit_blagomed_base(tmp_path):
    script_base = _make_base(tmp_path, 'from-here')
    other_base = _make_base(tmp_path, 'explicit')
    proc = _run(script_base, 'start.bat', '--check', env_base=other_base)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert str(other_base).lower() in proc.stdout.lower()
    assert str(script_base).lower() not in proc.stdout.lower()


def test_start_rejects_missing_explicit_base(tmp_path):
    base = _make_base(tmp_path, 'script')
    missing = tmp_path / 'missing'
    proc = _run(base, 'start.bat', '--check', env_base=missing)
    assert proc.returncode == 2
    assert 'Не найден каталог установки' in proc.stdout


@pytest.mark.parametrize('bat', ['stop.bat', 'env-check.bat'])
def test_stop_and_env_check_use_same_base(tmp_path, bat):
    base = _make_base(tmp_path, 'controls')
    proc = _run(base, bat, '--check')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert str(base).lower() in proc.stdout.lower()


def test_install_bat_is_present_and_does_not_create_database():
    text = (ROOT / 'install.bat').read_text(encoding='utf-8')
    assert 'venv' in text and 'requirements.txt' in text
    assert 'config.ini' in text
    assert 'setup/create' not in text
    assert 'init_db' not in text