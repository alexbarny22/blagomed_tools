import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(name):
    data = (ROOT / name).read_bytes()
    # OEM-кодировка вывода: файл обязан декодироваться как cp866
    text = data.decode('cp866')
    return data, text


def test_cmd_files_exist():
    for name in ('INSTALL.cmd', 'START.cmd', 'STOP.cmd'):
        assert (ROOT / name).is_file(), name


def test_crlf_only():
    for name in ('INSTALL.cmd', 'START.cmd', 'STOP.cmd'):
        data, _ = _read(name)
        assert b'\r\n' in data, name
        assert b'\n' not in data.replace(b'\r\n', b''), f'голый LF в {name}'


def test_oem_codepage():
    for name in ('INSTALL.cmd', 'START.cmd', 'STOP.cmd'):
        _, text = _read(name)
        assert 'chcp 866' in text, name


def test_install_venv_and_requirements():
    _, text = _read('INSTALL.cmd')
    assert '-m venv venv' in text
    assert re.search(r'pip install .*-r requirements\.txt', text)


def test_install_never_overwrites_config_and_db():
    _, text = _read('INSTALL.cmd')
    assert re.search(r'if not exist config\.ini', text), 'config.ini нельзя перезаписывать'
    assert 'if exist data\\sebestoimost.db' in text, 'БД нельзя перезаписывать'


def test_start_uses_venv():
    _, text = _read('START.cmd')
    assert 'venv\\Scripts\\python.exe' in text
    assert 'run.py' in text


def test_stop_kills_run_py():
    _, text = _read('STOP.cmd')
    assert 'run\\.py' in text or 'run.py' in text
    assert 'Stop-Process' in text
