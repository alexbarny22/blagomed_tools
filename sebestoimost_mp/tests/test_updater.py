import io
import json
import zipfile

import updater


def _fake_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        p = 'blagomed_tools-feature-x/sebestoimost_mp/'
        z.writestr(p + 'app/new_module.py', 'NEW = 1\n')
        z.writestr(p + 'config.ini', 'HACKED\n')
        z.writestr(p + 'data/sebestoimost.db', b'HACKED')
        z.writestr(p + 'venv/fake.py', 'HACKED\n')
        z.writestr(p + 'requirements.txt', 'flask\n')
        z.writestr('blagomed_tools-feature-x/other/top.py', 'x')
    return buf.getvalue()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_update_preserves_config_data_venv(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'config.ini').write_text('SECRET', encoding='utf-8')
    (tmp_path / 'data' / 'sebestoimost.db').write_bytes(b'DB')
    (tmp_path / 'venv').mkdir()
    (tmp_path / 'venv' / 'keep.py').write_text('KEEP', encoding='utf-8')
    monkeypatch.setattr(updater, 'latest_commit', lambda timeout=8: 'abc123')
    monkeypatch.setattr(updater.urllib.request, 'urlopen',
                        lambda req, timeout=8: _Resp(_fake_zip()))
    calls = []
    monkeypatch.setattr(updater.subprocess, 'run', lambda *a, **k: calls.append(a))

    assert updater.update(tmp_path) is True
    assert (tmp_path / 'app' / 'new_module.py').read_text() == 'NEW = 1\n'
    assert (tmp_path / 'config.ini').read_text(encoding='utf-8') == 'SECRET'
    assert (tmp_path / 'data' / 'sebestoimost.db').read_bytes() == b'DB'
    assert (tmp_path / 'venv' / 'keep.py').read_text(encoding='utf-8') == 'KEEP'
    assert not (tmp_path / 'other').exists()
    v = json.loads((tmp_path / 'data' / 'version.json').read_text(encoding='utf-8'))
    assert v['commit'] == 'abc123'
    assert calls, 'pip install -r requirements.txt должен запускаться'


def test_update_skipped_when_same_commit(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'version.json').write_text(
        json.dumps({'commit': 'same'}), encoding='utf-8')
    monkeypatch.setattr(updater, 'latest_commit', lambda timeout=8: 'same')

    def boom(*a, **k):
        raise AssertionError('скачивания быть не должно')

    monkeypatch.setattr(updater.urllib.request, 'urlopen', boom)
    assert updater.update(tmp_path) is False


def test_current_commit_missing(tmp_path):
    assert updater.current_commit(tmp_path) is None
