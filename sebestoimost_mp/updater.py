"""Самообновление «Себестоимость МП» с GitHub.

Репозиторий публичный, токен не нужен. Скачивает ZIP ветки и перезаписывает
ТОЛЬКО код модуля. Никогда не трогает: config.ini, data/ (база, загрузки), venv/.
"""
import io
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = 'alexbarny22/blagomed_tools'
BRANCH = 'feature/sebestoimost-mp-v3'
MODULE = 'sebestoimost_mp'
API_URL = f'https://api.github.com/repos/{REPO}/commits/{BRANCH}'
ZIP_URL = f'https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}'
KEEP = {'config.ini', 'data', 'venv'}
UA = {'User-Agent': 'sebestoimost-mp-updater'}


def latest_commit(timeout=8):
    req = urllib.request.Request(API_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))['sha']


def current_commit(root):
    f = Path(root) / 'data' / 'version.json'
    try:
        return json.loads(f.read_text(encoding='utf-8'))['commit']
    except Exception:
        return None


def update(root):
    """Проверить и применить обновление. True = код обновлён."""
    root = Path(root)
    remote = latest_commit()
    if remote == current_commit(root):
        return False
    print('[ОБНОВЛЕНИЕ] новая версия', remote[:8], '- скачиваю...')
    req = urllib.request.Request(ZIP_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as resp:
        blob = resp.read()
    written = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            parts = name.split('/')
            if len(parts) < 3 or parts[1] != MODULE or not parts[-1]:
                continue
            rel = parts[2:]
            if rel[0] in KEEP:
                continue
            target = root.joinpath(*rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
            written += 1
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                    '-q', '-r', str(root / 'requirements.txt')], check=False)
    (root / 'data').mkdir(exist_ok=True)
    (root / 'data' / 'version.json').write_text(
        json.dumps({'commit': remote}, ensure_ascii=False), encoding='utf-8')
    print('[ОБНОВЛЕНИЕ] готово, файлов обновлено:', written)
    return True


def auto_update(root):
    """Вызов из run.py при старте; при обновлении - перезапуск процесса."""
    if update(root):
        print('[ОБНОВЛЕНИЕ] перезапуск...')
        os.execv(sys.executable, [sys.executable, str(Path(root) / 'run.py')])


if __name__ == '__main__':
    update(Path(__file__).resolve().parent)
