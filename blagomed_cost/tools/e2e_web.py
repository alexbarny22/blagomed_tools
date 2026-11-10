# -*- coding: utf-8 -*-
"""Сквозное испытание веб-версии «Себестоимость МП» через реальные HTTP-запросы.

Запуск: python tools/e2e_web.py [--wb PATH] [--ozon PATH]

Проверяется backend, не имитации: чистая БД через /setup → загрузка реальных
XLSX → расчёт с историческими ценами → результаты → ручная цена/вес/расходы с
датами → повторный расчёт → скачивание и сверка ячеек → история → перезапуск →
сохранность → ошибки загрузки → отсутствие 1С → нерешённые строки не блокируют
остальные.

Все артефакты — только в output/e2e_web; рабочая БД data/cost.db не создаётся
и не изменяется. Оригиналы XLSX хешируются до/после.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import ROUND_FLOOR, Decimal
from http.cookiejar import CookieJar
from pathlib import Path

from werkzeug.security import generate_password_hash

BASE = Path(__file__).resolve().parents[1]
E2E = BASE / 'output' / 'e2e_web'
HOME = E2E / 'home'
DB = HOME / 'data' / 'cost.db'
PREPARED = BASE / 'output' / 'e2e_test' / 'db' / 'db_clean63.db'
DEFAULT_WB = Path(r'C:\bot\СебестоимостьМП\input'
                  r'\Благомед_Себестоимость_11.2026_ВБ.xlsx')
DEFAULT_OZ = Path(r'C:\bot\СебестоимостьМП\input'
                  r'\OZON_Себестоимость_11.2026.xlsx')
CALC_DATE = '2026-11-09'
AUTH_USER = 'e2e-user'
AUTH_PASSWORD = 'e2e-password-2026'
RESULTS = E2E / 'e2e_results.json'

sys.path.insert(0, str(BASE))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


class Checks:
    def __init__(self):
        self.items = []

    def check(self, name: str, ok: bool, detail=''):
        self.items.append({'check': name, 'ok': bool(ok), 'detail': detail})
        print(('PASS ' if ok else 'FAIL ') + name +
              (f' — {detail}' if detail else ''), flush=True)
        return ok

    def summary(self) -> dict:
        return {'total': len(self.items),
                'passed': sum(1 for i in self.items if i['ok']),
                'failed': [i for i in self.items if not i['ok']]}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip('/')
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, *, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=180) as r:
                return r.status, r.read(), r.geturl(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.geturl(), dict(e.headers)

    def get(self, path: str):
        return self.request('GET', path)

    def post(self, path: str, fields: dict):
        body = urllib.parse.urlencode(fields).encode('utf-8')
        return self.request('POST', path, data=body, headers={
            'Content-Type': 'application/x-www-form-urlencoded'})

    def login(self, user: str = AUTH_USER,
              password: str = AUTH_PASSWORD):
        return self.post('/login', {'user': user, 'password': password})

    def post_files(self, path: str, fields: dict, files):
        """files: [(filename, content), ...] в одном multipart-запросе."""
        boundary = '----blagomed' + uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                         f'name="{k}"\r\n\r\n{v}\r\n'.encode('utf-8'))
        for filename, content in files:
            parts.append(
                (f'--{boundary}\r\nContent-Disposition: form-data; '
                 f'name="files"; filename="{filename}"\r\n'
                 'Content-Type: application/vnd.openxmlformats-officedocument.'
                 'spreadsheetml.sheet\r\n\r\n').encode('utf-8') +
                content + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode('ascii'))
        body = b''.join(parts)
        return self.request('POST', path, data=body, headers={
            'Content-Type': f'multipart/form-data; boundary={boundary}'})

    def post_file(self, path: str, fields: dict, filename: str,
                  content: bytes):
        boundary = '----blagomed' + uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                         f'name="{k}"\r\n\r\n{v}\r\n'.encode('utf-8'))
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
             f'filename="{filename}"\r\nContent-Type: application/vnd.'
             'openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n'
             ).encode('utf-8') + content + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode('ascii'))
        body = b''.join(parts)
        return self.request('POST', path, data=body, headers={
            'Content-Type': f'multipart/form-data; boundary={boundary}'})


class Server:
    def __init__(self, port: int, max_mb: int = 1):
        self.port = port
        self.max_mb = max_mb
        self.proc = None
        self.log_path = E2E / 'logs' / 'server.log'

    def start(self):
        (E2E / 'logs').mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({'BLAGOMED_HOME': str(HOME), 'BLAGOMED_DB': str(DB),
                    'BLAGOMED_PORT': str(self.port),
                    'BLAGOMED_HOST': '127.0.0.1',
                    'BLAGOMED_MAX_UPLOAD_MB': str(self.max_mb),
                    'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'})
        with open(self.log_path, 'a', encoding='utf-8') as log:
            self.proc = subprocess.Popen(
                [sys.executable, '-m', 'app.web.server', '--base', str(HOME),
                 '--db', str(DB), '--port', str(self.port),
                 '--host', '127.0.0.1'],
                cwd=str(BASE), env=env, stdout=log,
                stderr=subprocess.STDOUT)
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError('Сервер завершился при старте; см. '
                                   + str(self.log_path))
            try:
                with urllib.request.urlopen(
                        f'http://127.0.0.1:{self.port}/healthz',
                        timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:                          # noqa: BLE001
                time.sleep(0.4)
        raise RuntimeError('Сервер не поднялся за 90 с; см. '
                           + str(self.log_path))

    def stop(self):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None


def db_con():
    import sqlite3 as _s
    con = _s.connect(str(DB))
    con.row_factory = _s.Row
    return con


def run_meta(run_id: int) -> dict:
    path = HOME / 'data' / 'history' / f'run_{run_id}.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def run_id_from(url: str) -> int:
    m = re.search(r'/runs/(\d+)', url or '')
    return int(m.group(1)) if m else 0


def page_cost(html: str):
    m = re.search(r'Себестоимость</dt>\s*<dd><b>(\d+)', html)
    return int(m.group(1)) if m else None


def expected_cost(pid: int, on_date: str):
    from app.core import calc
    con = db_con()
    try:
        manual = con.execute(
            'SELECT price FROM manual_prices WHERE product_id = ?'
            ' AND valid_from <= ? ORDER BY valid_from DESC, id DESC LIMIT 1',
            (pid, on_date)).fetchone()
        if manual is None:
            return None
        weight = con.execute(
            'SELECT weight_g FROM ppu_weights WHERE product_id = ?'
            ' AND valid_from <= ? ORDER BY valid_from DESC, id DESC LIMIT 1',
            (pid, on_date)).fetchone()
        extras = sum((e.rub for e in calc.get_extras(con, pid, on_date)),
                     Decimal('0'))
        st = calc.get_settings(con, on_date)
        total = (Decimal(str(manual['price'])) +
                 Decimal(int(weight['weight_g']) if weight else 0) /
                 Decimal(1000) * st['ppu_rub_per_kg'] + extras) * \
            (Decimal(1) + st['margin_pct'] / Decimal(100))
        return int(total.to_integral_value(rounding=ROUND_FLOOR))
    finally:
        con.close()


def upload_xlsx(client: Client, path: Path, marketplace: str):
    content = path.read_bytes()
    return client.post_file('/upload', {'marketplace': marketplace},
                            path.name, content)


def write_auth_config() -> Path:
    """Конфиг E2E: логин/хэш только в temp HOME, не в репозитории."""
    path = HOME / 'config.ini'
    path.write_text(
        '[web]\nhost = 127.0.0.1\n\n'
        '[auth]\n'
        f'user = {AUTH_USER}\n'
        f'pass_hash = {generate_password_hash(AUTH_PASSWORD)}\n',
        encoding='utf-8')
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='HTTP E2E веб-версии')
    ap.add_argument('--wb', default=str(DEFAULT_WB))
    ap.add_argument('--ozon', default=str(DEFAULT_OZ))
    args = ap.parse_args(argv)
    wb, oz = Path(args.wb), Path(args.ozon)
    checks = Checks()
    result = {'checks': checks.items, 'summary': {}, 'run_ids': {},
              'expected_baseline': {'rows': 182, 'ok': 118,
                                    'unresolved': 51, 'no_match': 13}}
    working_db_before = (BASE / 'data' / 'cost.db').exists()
    server = Server(free_port())

    if not PREPARED.exists():
        checks.check('подготовленная тестовая БД найдена', False,
                     str(PREPARED))
        return 1
    for p in (wb, oz):
        if not p.exists():
            checks.check('исходный файл найден', False, str(p))
            return 1

    if E2E.exists():
        shutil.rmtree(E2E)
    for d in ('data', 'uploads', 'output', 'logs'):
        (HOME / d).mkdir(parents=True, exist_ok=True)
    write_auth_config()
    (E2E / 'sources').mkdir(parents=True)
    wb_copy = E2E / 'sources' / wb.name
    oz_copy = E2E / 'sources' / oz.name
    shutil.copy2(wb, wb_copy)
    shutil.copy2(oz, oz_copy)
    hashes_before = {'wb': sha256(wb_copy), 'ozon': sha256(oz_copy)}
    client = Client(f'http://127.0.0.1:{server.port}')
    try:
        server.start()
        st, body, url, _ = client.get('/')
        checks.check('1a. без входа доступ закрыт (редирект на /login)',
                     st == 200 and url.rstrip('/').endswith('/login'),
                     f'HTTP {st}, {url}')
        st, body, _, _ = client.post('/login', {
            'user': AUTH_USER, 'password': 'wrong-password'})
        checks.check('1b. неверный пароль отклонён',
                     st == 200 and 'Неверный логин или пароль' in
                     body.decode('utf-8', 'replace'), f'HTTP {st}')
        st, body, url, _ = client.login()
        checks.check('1c. вход по логину и хэшу из config.ini выполнен',
                     st == 200 and '/login' not in url,
                     f'HTTP {st}, {url}')
        st, body, _, _ = client.get('/setup')
        checks.check('1. чистый запуск: интерфейс открывается',
                     st == 200 and not DB.exists(), f'HTTP {st}')
        checks.check('2a. база до создания отсутствует', not DB.exists(),
                     str(DB))
        client.post('/setup/create', {})
        st_e, body_e, _, _ = client.get('/expenses')
        expenses_new = body_e.decode('utf-8', 'replace')
        con = db_con()
        defaults = {r['key']: r['value'] for r in con.execute(
            "SELECT key, value FROM settings"
            " WHERE key IN ('ppu_rub_per_kg', 'margin_pct', 'alert_pct')")}
        con.close()
        checks.check('2e. новая БД: наценка 15% (не 0,15), ППУ 405, порог 10',
                     abs(defaults.get('margin_pct', 0) - 15) < 1e-9 and
                     abs(defaults.get('ppu_rub_per_kg', 0) - 405) < 1e-9 and
                     abs(defaults.get('alert_pct', 0) - 10) < 1e-9 and
                     '15,00%' in expenses_new and '405,00' in expenses_new,
                     f'settings={defaults}, HTTP {st_e}')
        st_h, body_h, _, _ = client.get('/healthz')
        hj = json.loads(body_h.decode('utf-8'))
        checks.check('2b. тестовая SQLite создана штатными миграциями',
                     hj.get('db_exists') and hj.get('db_integrity') == 'ok'
                     and hj.get('user_version') == 2,
                     f"version={hj.get('user_version')}, "
                     f"integrity={hj.get('db_integrity')}")
        st, body, _, _ = client.post('/setup/migrate', {})
        checks.check('2c. миграция без подтверждения заблокирована',
                     st == 200 and 'требует явного подтверждения' in
                     body.decode('utf-8', 'replace'), f'HTTP {st}')
        st, body, _, _ = client.post('/setup/migrate', {'confirm': 'yes'})
        txt = body.decode('utf-8', 'replace')
        st_v, body_v, _, _ = client.get('/healthz')
        vj = json.loads(body_v.decode('utf-8'))
        checks.check('2d. подтверждённая миграция не меняет актуальную БД',
                     st == 200 and vj.get('user_version') == 2 and
                     'Миграции применены: 0' in txt,
                     f"version={vj.get('user_version')}, HTTP {st}")
        server.stop()
        shutil.copy2(PREPARED, DB)
        server.start()
        st, body, _, _ = client.get('/')
        checks.check('3. главная страница после создания БД', st == 200,
                     f'HTTP {st}')

        st, body, _, _ = client.post_files('/upload',
                                           {'marketplace': 'auto'},
                                           [(wb_copy.name, wb_copy.read_bytes()),
                                            (oz_copy.name, oz_copy.read_bytes())])
        text = body.decode('utf-8', 'replace')
        checks.check('4. два файла (WB и Ozon) загружены одним запросом',
                     st == 200 and text.count('принят') == 2 and
                     len(list((HOME / 'uploads').glob('*.xlsx'))) == 2,
                     f'HTTP {st}, файлов=' +
                     str(len(list((HOME / 'uploads').glob('*.xlsx')))))
        checks.check('5. оба формата распознаны автоматически',
                     'формат WB подтверждён' in text and
                     'формат OZON подтверждён' in text)
        checks.check('5b. оригиналы не изменены после загрузки',
                     sha256(wb_copy) == hashes_before['wb'] and
                     sha256(oz_copy) == hashes_before['ozon'])

        st, body, url, _ = client.post('/calc/run', {
            'calc_date': CALC_DATE, 'price_mode': 'snapshot'})
        run1 = run_id_from(url)
        meta1 = run_meta(run1)
        s1 = meta1.get('summary') or {}
        result['run_ids']['snapshot'] = run1
        total1 = s1.get('wb_total', 0) + s1.get('ozon_total', 0)
        checks.check('6. расчёт выполнен, создан запуск',
                     st == 200 and run1 > 0 and bool(meta1),
                     f'HTTP {st}, run={run1}')
        checks.check('6b. 182 строки, 118 OK, 13 NO_MATCH',
                     total1 == 182 and s1.get('ok') == 118 and
                     s1.get('no_match') == 13, f'total={total1}, {s1}')
        checks.check('6c. нерешённые строки не блокируют остальные',
                     s1.get('ok', 0) > 0 and s1.get('skipped', 0) > 0,
                     f"ok={s1.get('ok')}, skipped={s1.get('skipped')}")
        run_html = client.get(f'/runs/{run1}')[1].decode('utf-8', 'replace')
        checks.check('7. страница результатов с предупреждением о черновике',
                     'черновик' in run_html and '118' in run_html)
        issue_html = client.get(f'/issues?run_id={run1}')[1].decode(
            'utf-8', 'replace')
        checks.check('7b. раздел «Проблемы» показывает нерешённые строки',
                     'NO_PRICE' in issue_html or 'AMBIGUOUS' in issue_html,
                     f'len={len(issue_html)}')
    except Exception:
        checks.check('E2E (часть 1) без исключений', False,
                     traceback.format_exc()[:4000])
        result['fatal'] = traceback.format_exc()[-4000:]
        server.stop()
        _write_result(result, checks, hashes_before, working_db_before,
                      wb_copy, oz_copy)
        return 1
    server.stop()
    return _continue(result, checks, hashes_before, working_db_before,
                     wb_copy, oz_copy, server)


def _continue(result, checks, hashes_before, working_db_before,
              wb_copy, oz_copy, server):
    from datetime import date
    from app.core.export import verify_ouput_only_target

    today = date.today().isoformat()
    client = Client(f'http://127.0.0.1:{server.port}')
    server.start()
    client.login()
    try:
        run1 = result['run_ids']['snapshot']
        meta1 = run_meta(run1)
        for kind, src, col in (('wb', wb_copy, 5), ('ozon', oz_copy, 4)):
            st_w, body_w, _, _ = client.get(f'/runs/{run1}/download/{kind}')
            warn = body_w.decode('utf-8', 'replace')
            checks.check(f'8a. {kind.upper()}: предупреждение о черновике',
                         st_w == 200 and 'черновик' in warn)
            st_d, body_d, _, _ = client.get(
                f'/runs/{run1}/download/{kind}?confirm=1')
            checks.check(f'8b. {kind.upper()}: файл скачан',
                         st_d == 200 and body_d[:2] == b'PK',
                         f'HTTP {st_d}, {len(body_d)} bytes')
            out_dir = E2E / 'downloads'
            out_dir.mkdir(exist_ok=True)
            dst = out_dir / f'run{run1}_{kind}.xlsx'
            dst.write_bytes(body_d)
            allow = {r['row_number']: r['cost'] for r in meta1['rows']
                     if r['marketplace'] == kind and r['status'] == 'OK'
                     and r['cost'] is not None}
            try:
                verify_ouput_only_target(src, dst, kind, col, allow)
                checks.check(f'8c. {kind.upper()}: изменены только ячейки '
                             'себестоимости', True, f'строк={len(allow)}')
            except Exception as e:                     # noqa: BLE001
                checks.check(f'8c. {kind.upper()}: сверка структуры', False,
                             str(e)[:500])
        issue_html = client.get(f'/issues?run_id={run1}')[1].decode(
            'utf-8', 'replace')
        pids = [int(x) for x in re.findall(r'/products/(\d+)', issue_html)]
        pid = pids[0] if pids else 0
        checks.check('9a. найдена проблемная позиция с товаром', pid > 0,
                     f'product_id={pid}')
        st, body, _, _ = client.post(f'/products/{pid}/manual', {
            'action': 'set', 'price': '500', 'valid_from': today})
        con = db_con()
        prod = con.execute('SELECT manual_price FROM products WHERE id = ?',
                           (pid,)).fetchone()
        mp_row = con.execute(
            'SELECT price FROM manual_prices WHERE product_id = ?'
            ' AND valid_from = ?', (pid, today)).fetchone()
        con.close()
        checks.check('9b. ручная цена сохранена и включена',
                     st == 200 and prod and prod['manual_price'] == 1 and
                     mp_row and abs(float(mp_row['price']) - 500) < 1e-9,
                     f"manual={prod['manual_price'] if prod else None}")
        page = client.get(f'/products/{pid}')[1].decode('utf-8', 'replace')
        exp = expected_cost(pid, today)
        checks.check('9c. карточка считает себестоимость по ручной цене',
                     page_cost(page) == exp,
                     f'page={page_cost(page)}, expected={exp}')
        client.post(f'/products/{pid}/ppu', {
            'weight_g': '100', 'valid_from': today})
        page = client.get(f'/products/{pid}')[1].decode('utf-8', 'replace')
        exp = expected_cost(pid, today)
        checks.check('10. вес ППУ сохранён и учтён в расчёте',
                     '100 г' in page and page_cost(page) == exp,
                     f'page={page_cost(page)}, expected={exp}')
        return _step_extras(result, checks, hashes_before, working_db_before,
                            wb_copy, oz_copy, server, client, today, run1,
                            pid, meta1)
    except Exception:
        checks.check('E2E (часть 2) без исключений', False,
                     traceback.format_exc()[:4000])
        result['fatal2'] = traceback.format_exc()[-4000:]
        server.stop()
        _write_result(result, checks, hashes_before, working_db_before,
                      wb_copy, oz_copy)
        return 1


def _step_extras(result, checks, hashes_before, working_db_before,
                 wb_copy, oz_copy, server, client, today, run1, pid, meta1):
    from datetime import date, timedelta
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    try:
        con = db_con()
        snap_before = [tuple(r) for r in con.execute(
            'SELECT product_id, price_1c, ppu_weight_g, extras_rub, cost'
            ' FROM cost_snapshots WHERE run_id = ? ORDER BY product_id',
            (run1,))]
        con.close()
        client.post(f'/products/{pid}/extras', {
            'kind': 'Поролон', 'rub': '50', 'valid_from': today})
        client.post(f'/products/{pid}/extras', {
            'kind': 'Поролон', 'rub': '75', 'valid_from': tomorrow})
        page_today = client.get(
            f'/products/{pid}?date={today}')[1].decode('utf-8', 'replace')
        page_tom = client.get(
            f'/products/{pid}?date={tomorrow}')[1].decode('utf-8', 'replace')
        exp_today = expected_cost(pid, today)
        exp_tom = expected_cost(pid, tomorrow)
        checks.check('11. статья расхода видна в карточке',
                     'Поролон' in page_today)
        checks.check('12. суммы по датам: сегодня 50, завтра 75',
                     page_cost(page_today) == exp_today and
                     page_cost(page_tom) == exp_tom and exp_tom > exp_today,
                     f'today={page_cost(page_today)}/{exp_today}, '
                     f'tomorrow={page_cost(page_tom)}/{exp_tom}')

        st, body, _, _ = client.post('/expenses/settings', {
            'ppu_rub_per_kg': '500', 'margin_pct': '20',
            'valid_from': tomorrow})
        page_set = client.get(
            f'/products/{pid}?date={tomorrow}')[1].decode('utf-8', 'replace')
        exp_set = expected_cost(pid, tomorrow)
        exp_page = client.get(
            f'/expenses?date={tomorrow}')[1].decode('utf-8', 'replace')
        con = db_con()
        aud = con.execute(
            "SELECT user, new FROM audit_log WHERE entity = 'settings'"
            ' ORDER BY id DESC LIMIT 1').fetchone()
        con.close()
        checks.check('12b. ставка ППУ/наценка версионированы по дате',
                     st == 200 and page_cost(page_set) == exp_set and
                     exp_set != exp_tom,
                     f'page={page_cost(page_set)}, expected={exp_set}')
        checks.check('12c. страница «Расходы» показывает новые параметры',
                     'Параметры расчёта' in exp_page and
                     '500,00' in exp_page and '20,00' in exp_page)
        checks.check('12d. изменение параметров записано в аудит с логином',
                     aud is not None and aud['user'] == AUTH_USER and
                     tomorrow in aud['new'],
                     f"user={aud['user'] if aud else None}")

        st, body, url, _ = client.post('/calc/run', {
            'calc_date': today, 'price_mode': 'none'})
        run2 = run_id_from(url)
        meta2 = run_meta(run2)
        s2 = meta2.get('summary') or {}
        result['run_ids']['manual_rerun'] = run2
        row2 = next((r for r in meta2.get('rows', [])
                     if r.get('product_id') == pid), None)
        checks.check('13. повторный расчёт учитывает ручную цену',
                     st == 200 and run2 > run1 and row2 is not None and
                     row2.get('status') == 'OK' and
                     row2.get('cost') == expected_cost(pid, today),
                     f"run={run2}, "
                     f"status={row2.get('status') if row2 else None}, "
                     f"cost={row2.get('cost') if row2 else None}")
        checks.check('13b. нерешённые строки остаются, расчёт не блокируется',
                     s2.get('skipped', 0) > 0 and s2.get('ok', 0) > 0, str(s2))
        con = db_con()
        snap_after = [tuple(r) for r in con.execute(
            'SELECT product_id, price_1c, ppu_weight_g, extras_rub, cost'
            ' FROM cost_snapshots WHERE run_id = ? ORDER BY product_id',
            (run1,))]
        con.close()
        checks.check('14. прошлый запуск не изменился после правок',
                     snap_before == snap_after and bool(snap_after),
                     f'снимков={len(snap_after)}')
        result['run2_summary'] = s2
        return _finalize(result, checks, hashes_before, working_db_before,
                         wb_copy, oz_copy, server, client, today, run1, pid)
    except Exception:
        checks.check('E2E (часть 3) без исключений', False,
                     traceback.format_exc()[:4000])
        result['fatal3'] = traceback.format_exc()[-4000:]
        server.stop()
        _write_result(result, checks, hashes_before, working_db_before,
                      wb_copy, oz_copy)
        return 1


def _finalize(result, checks, hashes_before, working_db_before,
              wb_copy, oz_copy, server, client, today, run1, pid):
    try:
        run2 = result['run_ids'].get('manual_rerun', 0)
        st, body, _, _ = client.post('/issues/skip', {
            'product_id': pid, 'run_id': run1})
        con = db_con()
        skipped = con.execute(
            "SELECT COUNT(*) FROM audit_log WHERE entity = 'issues'"
        ).fetchone()[0]
        con.close()
        checks.check('15. «оставить нерешённым» записано в аудит',
                     st == 200 and skipped >= 1, f'записей={skipped}')
        st, body, _, _ = client.get(f'/runs/{run2}/download/wb?confirm=1')
        checks.check('15b. результат повторного расчёта скачивается',
                     st == 200 and body[:2] == b'PK', f'{len(body)} bytes')
        hist = client.get('/history')[1].decode('utf-8', 'replace')
        ids_before = sorted(set(int(x) for x in re.findall(r'/runs/(\d+)',
                                                           hist)))
        checks.check('16. история содержит все запуски',
                     len(ids_before) >= 3 and run1 in ids_before and
                     run2 in ids_before, str(ids_before))
        server.stop()
        server.start()
        client = Client(f'http://127.0.0.1:{server.port}')
        client.login()
        st, body, _, _ = client.get('/healthz')
        hj = json.loads(body.decode('utf-8'))
        checks.check('17. перезапуск приложения: БД открывается',
                     st == 200 and hj.get('db_integrity') == 'ok',
                     f"integrity={hj.get('db_integrity')}")
        hist2 = client.get('/history')[1].decode('utf-8', 'replace')
        ids_after = sorted(set(int(x) for x in re.findall(r'/runs/(\d+)',
                                                          hist2)))
        checks.check('18a. история сохранена после перезапуска',
                     ids_after == ids_before, str(ids_after))
        page = client.get(f'/products/{pid}')[1].decode('utf-8', 'replace')
        checks.check('18b. ручная цена/вес/расход сохранены после перезапуска',
                     '500,00' in page and '100 г' in page and
                     'Поролон' in page)
        con = db_con()
        integrity = con.execute('PRAGMA integrity_check').fetchone()[0]
        con.close()
        checks.check('18c. целостность SQLite после перезапуска',
                     integrity == 'ok', integrity)
        return _step_negative(result, checks, hashes_before,
                              working_db_before, wb_copy, oz_copy, server,
                              client, today, run1, pid)
    except Exception:
        checks.check('E2E (часть 4) без исключений', False,
                     traceback.format_exc()[:4000])
        result['fatal4'] = traceback.format_exc()[-4000:]
        server.stop()
        _write_result(result, checks, hashes_before, working_db_before,
                      wb_copy, oz_copy)
        return 1


def _step_negative(result, checks, hashes_before, working_db_before,
                   wb_copy, oz_copy, server, client, today, run1, pid):
    import io
    from openpyxl import Workbook
    try:
        uploads_dir = HOME / 'uploads'
        st, body, _, _ = client.post_file(
            '/upload', {'marketplace': 'auto'}, 'bad.txt', b'hello')
        checks.check('19a. TXT отклонён', st == 200 and
                     'bad.txt отклонён' in body.decode('utf-8', 'replace'))

        st, body, _, _ = client.post_file(
            '/upload', {'marketplace': 'auto'}, 'bad.xlsx',
            b'PK\x03\x04broken-not-a-zip')
        txt = body.decode('utf-8', 'replace')
        checks.check('19b. повреждённый XLSX отклонён',
                     st == 200 and ('Не удалось открыть' in txt or
                                    'повреждён' in txt))

        wb_bad = Workbook()
        ws = wb_bad.active
        ws.title = 'Лист1'
        ws['A1'] = 'не MPSPACE'
        bio = io.BytesIO()
        wb_bad.save(bio)
        st, body, _, _ = client.post_file(
            '/upload', {'marketplace': 'auto'}, 'wrong.xlsx',
            bio.getvalue())
        checks.check('19c. чужой формат XLSX отклонён',
                     st == 200 and 'Формат отчёта не распознан' in
                     body.decode('utf-8', 'replace'))

        st, body, _, _ = client.post_file(
            '/upload', {'marketplace': 'wb'}, '../../evil.xlsx',
            wb_copy.read_bytes())
        escaped = [p for p in (HOME / 'evil.xlsx', E2E / 'evil.xlsx',
                               BASE / 'evil.xlsx') if p.exists()]
        checks.check('19d. обход пути при имени файла заблокирован',
                     not escaped and st == 200, f'escaped={escaped}')

        st, body, _, _ = client.post_file(
            '/upload', {'marketplace': 'auto'}, 'big.xlsx',
            b'PK\x03\x04' + b'0' * (1200 * 1024))
        txt = body.decode('utf-8', 'replace')
        checks.check('19e. превышение размера отклонено',
                     st == 413 or 'больше допустимого' in txt, f'HTTP {st}')

        oz_files = sorted((HOME / 'uploads').glob('ozon_*.xlsx'))
        moved = []
        try:
            for p in oz_files:
                bak = p.with_name(p.name + '.bak')
                p.rename(bak)
                moved.append((p, bak))
            st, body, url, _ = client.post('/calc/run', {
                'calc_date': today, 'price_mode': 'none'})
            run_single = run_id_from(url)
            meta_single = run_meta(run_single)
            s_single = meta_single.get('summary') or {}
            txt = body.decode('utf-8', 'replace')
            checks.check('19f. расчёт только по одной площадке (WB) без Ozon',
                         st == 200 and run_single > 0 and
                         s_single.get('ozon_total', 0) == 0 and
                         s_single.get('wb_total', 0) > 0 and
                         meta_single.get('files', {}).get('ozon') is None,
                         f"run={run_single}, {s_single}")
            checks.check('19g. отсутствие второго файла явно предупреждено',
                         'Файл Ozon отсутствует' in txt)
            result['run_ids']['single_platform'] = run_single
        finally:
            for p, bak in moved:
                if bak.exists():
                    bak.rename(p)

        st, body, _, _ = client.get('/calc')
        calc_html = body.decode('utf-8', 'replace')
        checks.check('20a. без 1С приложение работает и показывает диагностику',
                     st == 200 and ('config.ini не найден' in calc_html or
                                    'не заполнены' in calc_html))

        st, body, url, _ = client.post('/calc/run', {
            'calc_date': today, 'price_mode': '1c'})
        run3 = run_id_from(url)
        meta3 = run_meta(run3)
        s3 = meta3.get('summary') or {}
        result['run_ids']['onec_unavailable'] = run3
        checks.check('20b. расчёт без 1С не падает, ошибки видны',
                     st == 200 and run3 > 0 and bool(meta3) and
                     s3.get('errors', 0) > 0,
                     f"run={run3}, errors={s3.get('errors')}, "
                     f"ok={s3.get('ok')}")
        onec_msg = ((meta3.get('onec') or {}).get('message') or '')
        checks.check('20c. причина недоступности 1С указана',
                     bool(onec_msg) and ('не найден' in onec_msg or
                                         'не заполнены' in onec_msg),
                     onec_msg[:200])
        issues2 = client.get(f'/issues?run_id={run3}')[1].decode(
            'utf-8', 'replace')
        checks.check('20d. ошибки подключения показаны в «Проблемах»',
                     'ERROR' in issues2 and '1С' in issues2)
        row3 = next((r for r in meta3.get('rows', [])
                     if r.get('product_id') == pid), None)
        checks.check('20e. ручная цена работает и без 1С',
                     row3 is not None and row3.get('status') == 'OK',
                     f"status={row3.get('status') if row3 else None}")
        return _close(result, checks, hashes_before, working_db_before,
                      wb_copy, oz_copy, server, client, uploads_dir)
    except Exception:
        checks.check('E2E (часть 5) без исключений', False,
                     traceback.format_exc()[:4000])
        result['fatal5'] = traceback.format_exc()[-4000:]
        server.stop()
        _write_result(result, checks, hashes_before, working_db_before,
                      wb_copy, oz_copy)
        return 1


def _close(result, checks, hashes_before, working_db_before, wb_copy,
           oz_copy, server, client, uploads_dir):
    try:
        st, body, _, _ = client.get('/healthz')
        hj = json.loads(body.decode('utf-8')) if st == 200 else {}
        con = db_con()
        integrity = con.execute('PRAGMA integrity_check').fetchone()[0]
        runs_n = con.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
        con.close()
        checks.check('21a. итоговая целостность SQLite',
                     integrity == 'ok' and hj.get('db_integrity') == 'ok',
                     integrity)
        checks.check('21b. запуски сохранены в БД', runs_n >= 3,
                     f'runs={runs_n}')
        checks.check('22. оригиналы XLSX не изменены',
                     sha256(wb_copy) == hashes_before['wb'] and
                     sha256(oz_copy) == hashes_before['ozon'])
        checks.check('23. рабочая БД не создавалась/не изменялась',
                     (BASE / 'data' / 'cost.db').exists() ==
                     working_db_before)
        tmp = ([p.name for p in Path(uploads_dir).glob('.tmp_*')] +
               [p.name for p in (HOME / 'output').glob('.tmp_*')])
        checks.check('24. временные файлы после операций отсутствуют',
                     not tmp, str(tmp))
        result['artifacts'] = {'home': str(HOME),
                               'downloads': str(E2E / 'downloads'),
                               'results': str(RESULTS)}
        result['db_counts'] = {'runs': runs_n}
        prior = checks.summary()
        checks.check('25. все проверки пройдены',
                     not prior['failed'],
                     f"до этой проверки: {prior}")
        server.stop()
        _write_result(result, checks, hashes_before, working_db_before,
                      wb_copy, oz_copy)
        return 0 if not checks.summary()['failed'] else 1
    except Exception:
        checks.check('E2E (итог) без исключений', False,
                     traceback.format_exc()[:4000])
        result['fatal6'] = traceback.format_exc()[-4000:]
        server.stop()
        _write_result(result, checks, hashes_before, working_db_before,
                      wb_copy, oz_copy)
        return 1


def _write_result(result, checks, hashes_before, working_db_before,
                  wb_copy, oz_copy):
    result['checks'] = checks.items
    result['summary'] = checks.summary()
    result['hashes'] = {'before': hashes_before,
                        'after': {'wb': sha256(wb_copy),
                                  'ozon': sha256(oz_copy)}}
    result['working_db'] = {'before': working_db_before,
                            'after': (BASE / 'data' / 'cost.db').exists()}
    E2E.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                       encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
