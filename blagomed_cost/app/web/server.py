# -*- coding: utf-8 -*-
"""Локальное веб-приложение «Себестоимость МП» (Flask + waitress).

Только 127.0.0.1. Все расчёты — существующее ядро app.core.export/calc.
Первый запуск: БД не создаётся автоматически — требуется явное действие
пользователя на странице «База данных» (существующая БД не перезаписывается).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash

from app.core import export as core_export
from app.web import config as web_config
from app.web import prices, store
from app.web import uploads as upload_lib

log = logging.getLogger('blagomed.web')


def audit_user() -> str:
    """Логин из сессии для audit_log; вне запроса — 'web'."""
    return session.get('user') or 'web'


def _setup_logging(cfg) -> None:
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh = RotatingFileHandler(cfg.logs / 'web.log', maxBytes=2 * 1024 * 1024,
                             backupCount=3, encoding='utf-8')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


def _secret_key(cfg) -> str:
    path = Path(cfg.data_dir) / '.web_secret'
    if path.exists():
        return path.read_text(encoding='utf-8').strip()
    key = secrets.token_hex(32)
    path.write_text(key, encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def create_app(cfg=None) -> Flask:
    cfg = cfg or web_config.load_config()
    web_config.ensure_dirs(cfg)
    _setup_logging(cfg)
    app = Flask(__name__, template_folder='templates',
                static_folder='static')
    app.secret_key = _secret_key(cfg)
    app.config['MAX_CONTENT_LENGTH'] = cfg.max_upload_bytes + 1024 * 1024
    app.config['BLAGOMED_CFG'] = cfg
    app.config.update(SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Lax')

    @app.template_filter('money')
    def money(value):
        if value is None or value == '':
            return '—'
        try:
            return f'{Decimal(str(value)):,.2f}'.replace(
                ',', ' ').replace('.', ',')
        except Exception:                              # noqa: BLE001
            return str(value)

    @app.template_filter('dt')
    def dt(value):
        return str(value or '—')

    @app.template_filter('weight')
    def weight(value):
        return '—' if value is None else f'{int(value)} г'

    @app.context_processor
    def inject_globals():
        return {'cfg': cfg, 'db_ready': Path(cfg.db_path).exists(),
                'status_labels': store.STATUS_LABELS,
                'current_user': session.get('user'),
                'auth_configured': cfg.auth_configured}

    def get_con():
        return store.open_db(cfg)

    @app.before_request
    def require_login():
        if request.endpoint in {'static', 'login', 'healthz'}:
            return None
        if not cfg.auth_configured or not session.get('user'):
            return redirect(url_for('login'))
        setup_endpoints = {'setup', 'setup_create', 'setup_legacy',
                           'setup_verify', 'setup_migrate'}
        if (not Path(cfg.db_path).exists() and
                request.endpoint not in setup_endpoints):
            return redirect(url_for('setup'))
        return None

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if not cfg.auth_configured:
            return render_template(
                'login.html', error=None, auth_configured=False,
                config_path=str(cfg.config_ini))
        if session.get('user'):
            return redirect(url_for('index'))
        error = None
        if request.method == 'POST':
            user = (request.form.get('user') or '').strip()[:200]
            password = request.form.get('password') or ''
            ok = False
            try:
                ok = (secrets.compare_digest(user.encode('utf-8'),
                                            cfg.auth_user.encode('utf-8')) and
                      check_password_hash(cfg.auth_pass_hash, password))
            except ValueError:
                log.error('Некорректный хэш пароля в config.ini ([auth] '
                          'pass_hash); вход невозможен')
            if ok:
                session.clear()
                session['user'] = cfg.auth_user
                log.info('Вход выполнен: %s', cfg.auth_user)
                return redirect(url_for('index'))
            log.warning('Неудачная попытка входа: пользователь=%r', user)
            error = 'Неверный логин или пароль'
        return render_template('login.html', error=error,
                               auth_configured=True,
                               config_path=str(cfg.config_ini))

    @app.post('/logout')
    def logout():
        user = session.get('user')
        session.clear()
        if user:
            log.info('Выход: %s', user)
        flash('Вы вышли из интерфейса.', 'ok')
        return redirect(url_for('login'))

    @app.errorhandler(413)
    def too_large(_e):
        flash(f'Файл больше допустимого размера '
              f'({cfg.max_upload_mb} МБ). Загрузка отклонена.', 'error')
        return redirect(url_for('calc')), 413

    @app.errorhandler(404)
    def not_found(_e):
        return render_template('error.html', code=404,
                               message='Страница не найдена'), 404

    @app.errorhandler(500)
    def server_error(e):
        log.exception('Внутренняя ошибка: %s', e)
        return render_template('error.html', code=500,
                               message='Внутренняя ошибка. Подробности — в '
                                       'журнале logs/web.log'), 500

    @app.get('/healthz')
    def healthz():
        info = store.db_info(cfg)
        return jsonify({'ok': True, 'db_exists': info['exists'],
                        'db_integrity': info['integrity'],
                        'user_version': info['user_version']})

    @app.get('/')
    def index():
        info = store.db_info(cfg)
        result = {'info': info, 'runs': [],
                  'uploads': store.last_uploads(cfg),
                  'onec': prices.onec_status(cfg),
                  'products_total': 0, 'last_summary': None,
                  'counts': info['counts']}
        if info['exists'] and info['integrity'] == 'ok':
            con = get_con()
            try:
                result['runs'] = store.list_runs(con, cfg, limit=5)
                result['products_total'] = con.execute(
                    'SELECT COUNT(*) FROM products').fetchone()[0]
            finally:
                con.close()
            if result['runs']:
                result['last_summary'] = result['runs'][0]['meta'].get(
                    'summary')
        return render_template('index.html', **result)

    @app.get('/setup')
    def setup():
        info = store.db_info(cfg)
        legacy = Path(cfg.base_dir) / 'legacy'
        legacy_files = {n: (legacy / n).exists()
                        for n in ('wb_catalog.json', 'cost_history.json')}
        return render_template('setup.html', info=info, legacy=legacy_files,
                               onec=prices.onec_status(cfg),
                               migrations=store.pending_migrations(cfg))

    @app.post('/setup/create')
    def setup_create():
        try:
            store.create_db(cfg)
            log.info('Создана новая БД: %s', cfg.db_path)
            flash('База данных создана. Схема применена штатными миграциями, '
                  'проверка целостности пройдена.', 'ok')
        except Exception as e:                           # noqa: BLE001
            log.exception('Не удалось создать БД')
            flash(f'База не создана: {e}', 'error')
        return redirect(url_for('setup'))

    @app.post('/setup/legacy')
    def setup_legacy():
        try:
            result = store.migrate_legacy(cfg)
            log.info('Перенос legacy выполнен: %r', result)
            flash('Данные старого бота перенесены: ' +
                  ', '.join(f'{k}={v}' for k, v in result.items()), 'ok')
        except Exception as e:                           # noqa: BLE001
            log.exception('Перенос legacy не выполнен')
            flash(f'Перенос не выполнен: {e}', 'error')
        return redirect(url_for('setup'))

    @app.post('/setup/verify')
    def setup_verify():
        info = store.db_info(cfg)
        if info['integrity'] == 'ok':
            flash(f"Целостность БД: ok; версия схемы {info['user_version']}",
                  'ok')
        else:
            flash(f"Проверка целостности: {info['integrity'] or info['error']}",
                  'error')
        return redirect(url_for('setup'))

    @app.post('/setup/migrate')
    def setup_migrate():
        if request.form.get('confirm') != 'yes':
            flash('Миграция существующей БД требует явного подтверждения.',
                  'error')
            return redirect(url_for('setup'))
        try:
            n = store.migrate_schema(cfg)
            log.info('Применено миграций: %s', n)
            flash(f'Миграции применены: {n}. Перед изменением схемы создана '
                  'резервная копия в backup/.', 'ok')
        except Exception as e:                           # noqa: BLE001
            log.exception('Миграция не выполнена')
            flash(f'Миграция не выполнена: {e}', 'error')
        return redirect(url_for('setup'))

    app.extensions['blagomed_config'] = cfg
    _register_calc(app, cfg, get_con)
    _register_products(app, cfg, get_con)
    _register_expenses(app, cfg, get_con)
    _register_issues(app, cfg, get_con)
    _register_history(app, cfg, get_con)
    return app


def _upload_meta_path(cfg) -> Path:
    return Path(cfg.data_dir) / 'uploads.json'


def _load_upload_meta(cfg) -> dict:
    path = _upload_meta_path(cfg)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save_upload_meta(cfg, meta: dict) -> None:
    path = _upload_meta_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                    encoding='utf-8')


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _row_dict(r) -> dict:
    prev = r.previous_cost
    if isinstance(prev, Decimal):
        prev = str(prev)
    return {'marketplace': r.marketplace, 'row_number': r.row_number,
            'mp_article': r.mp_article,
            'supplier_article': r.supplier_article,
            'size': r.size, 'product_id': r.product_id, 'status': r.status,
            'reason': r.reason, 'previous_cost': prev, 'cost': r.cost,
            'ppu_approved': r.ppu_approved, 'notes': list(r.notes)}


def _status_counts(rows) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def _register_calc(app, cfg, get_con):
    @app.get('/calc')
    def calc():
        uploads = store.last_uploads(cfg)
        umeta = _load_upload_meta(cfg)
        info = {}
        for mp, path in uploads.items():
            if path is None:
                info[mp] = None
                continue
            rec = umeta.get(mp, {})
            info[mp] = {'name': path.name, 'original': rec.get('original'),
                        'sha256': rec.get('sha256'), 'rows': rec.get('rows'),
                        'size': path.stat().st_size,
                        'mtime': datetime.fromtimestamp(
                            path.stat().st_mtime).strftime('%d.%m.%Y %H:%M')}
        con = get_con()
        try:
            runs = store.list_runs(con, cfg, limit=5)
        finally:
            con.close()
        return render_template('calc.html', uploads=info, runs=runs,
                               price_modes=prices.PRICE_MODES,
                               onec=prices.onec_status(cfg),
                               today=store.today_iso())

    @app.post('/upload')
    def upload():
        mp = request.form.get('marketplace', 'auto')
        if mp not in ('auto', 'wb', 'ozon'):
            flash('Неизвестный маркетплейс загрузки', 'error')
            return redirect(url_for('calc'))
        files = [f for f in request.files.getlist('files')
                 if f is not None and f.filename]
        if not files:
            single = request.files.get('file')
            if single is not None and single.filename:
                files = [single]
        if not files:
            flash('Файл не выбран', 'error')
            return redirect(url_for('calc'))
        if len(files) > 2:
            flash('За один раз можно загрузить не более двух файлов '
                  '(WB и Ozon).', 'error')
            return redirect(url_for('calc'))
        meta = _load_upload_meta(cfg)
        saved = 0
        for file in files:
            try:
                info = upload_lib.save_upload(
                    file.stream, file.filename, cfg.uploads,
                    cfg.max_upload_bytes, None if mp == 'auto' else mp)
                meta[info['marketplace']] = {
                    'original': info['original_name'],
                    'sha256': info['sha256'], 'rows': info['rows'],
                    'path': str(info['path']),
                    'uploaded_at': datetime.now().isoformat(
                        timespec='seconds')}
                saved += 1
                log.info('Загружен %s: %s строк, файл %s',
                         info['marketplace'], info['rows'], info['path'])
                flash(f"Файл {info['original_name']} принят: "
                      f"{info['rows']} строк, формат "
                      f"{info['marketplace'].upper()} подтверждён.", 'ok')
            except upload_lib.UploadError as e:
                log.warning('Загрузка отклонена: %s', e)
                flash(f'Файл {file.filename} отклонён: {e}', 'error')
            except Exception as e:                       # noqa: BLE001
                log.exception('Ошибка загрузки')
                flash(f'Не удалось загрузить файл {file.filename}: {e}',
                      'error')
        if saved:
            _save_upload_meta(cfg, meta)
        return redirect(url_for('calc'))

    @app.post('/calc/run')
    def calc_run():
        uploads = store.last_uploads(cfg)
        if uploads['wb'] is None and uploads['ozon'] is None:
            flash('Не загружен ни один файл. Загрузите WB и/или Ozon XLSX.',
                  'error')
            return redirect(url_for('calc'))
        if uploads['wb'] is None or uploads['ozon'] is None:
            absent = 'Ozon' if uploads['ozon'] is None else 'Wildberries'
            flash(f'Файл {absent} отсутствует: расчёт будет выполнен только '
                  'по загруженной площадке (второй файл не имитируется).',
                  'warning')
        price_mode = request.form.get('price_mode', 'none')
        try:
            calc_date = store.parse_iso_date(
                request.form.get('calc_date'), field='Дата расчёта',
                default=store.today_iso())
            if price_mode not in prices.PRICE_MODES:
                raise store.WebStoreError('Неизвестный режим цен')
        except store.WebStoreError as e:
            flash(str(e), 'error')
            return redirect(url_for('calc'))
        con = get_con()
        try:
            fetch, price_message, source_label = prices.make_fetch_prices(
                cfg, con, price_mode)
            report = core_export.run_exports(
                con=con, calc_date=calc_date, wb_path=uploads['wb'],
                ozon_path=uploads['ozon'], out_dir=cfg.output,
                fetch_prices=fetch, source_1c=source_label, user=audit_user(),
                mode='strict', salt='', persist=True)
        except Exception as e:                           # noqa: BLE001
            log.exception('Расчёт не выполнен')
            flash(f'Расчёт не выполнен: {e}', 'error')
            return redirect(url_for('calc'))
        finally:
            con.close()
        umeta = _load_upload_meta(cfg)
        meta = {
            'calc_date': report.calc_date, 'run_status': report.status,
            'drafts': report.drafts, 'summary': report.summary,
            'status_counts': _status_counts(report.rows),
            'rows': [_row_dict(r) for r in report.rows],
            'files': {'wb': report.wb_path, 'ozon': report.ozon_path},
            'hashes': report.hashes, 'report_path': report.report_path,
            'price_mode': price_mode, 'price_message': price_message,
            'source_label': source_label,
            'onec': prices.onec_status(cfg),
            'uploads': {mp: ({'path': str(p), **umeta.get(mp, {})}
                             if p else None) for mp, p in uploads.items()},
            'created_at': datetime.now().isoformat(timespec='seconds'),
        }
        if report.run_id is not None:
            old = store.load_run_meta(cfg, report.run_id)
            if old:
                meta['created_at'] = old.get('created_at',
                                             meta['created_at'])
            store.save_run_meta(cfg, report.run_id, meta)
            log.info('Расчёт run_id=%s: %r', report.run_id, report.summary)
            flash(f'Расчёт выполнен (запуск №{report.run_id}). '
                  f"Успешно: {report.summary.get('ok', 0)} из "
                  f"{len(report.rows)} строк.", 'ok')
            return redirect(url_for('run_detail', run_id=report.run_id))
        flash('Расчёт выполнен, но история не создана.', 'error')
        return redirect(url_for('calc'))

    @app.get('/runs/<int:run_id>')
    def run_detail(run_id):
        con = get_con()
        try:
            run = store.get_run(con, run_id)
            meta = store.load_run_meta(cfg, run_id)
        finally:
            con.close()
        if run is None:
            abort(404)
        summary = meta.get('summary') or {}
        draft = bool(summary.get('skipped')) or bool(meta.get('drafts'))
        return render_template('run.html', run=run, meta=meta,
                               summary=summary, draft=draft,
                               rows=meta.get('rows', []))

    @app.get('/runs/<int:run_id>/download/<kind>')
    def download(run_id, kind):
        if kind not in ('wb', 'ozon', 'report'):
            abort(404)
        con = get_con()
        try:
            run = store.get_run(con, run_id)
            meta = store.load_run_meta(cfg, run_id)
        finally:
            con.close()
        if run is None:
            abort(404)
        raw = {'wb': run['wb_file'], 'ozon': run['ozon_file'],
               'report': meta.get('report_path')}.get(kind)
        if not raw:
            flash('Файл для этого запуска не найден в истории.', 'error')
            return redirect(url_for('run_detail', run_id=run_id))
        path = Path(raw)
        if not path.exists() or not _inside(path, cfg.output):
            flash('Файл отсутствует или находится вне рабочего каталога '
                  'output/.', 'error')
            return redirect(url_for('run_detail', run_id=run_id))
        summary = meta.get('summary') or {}
        draft = bool(summary.get('skipped')) or bool(meta.get('drafts'))
        if draft and request.args.get('confirm') != '1':
            return render_template('download_warning.html', run=run,
                                   summary=summary, kind=kind,
                                   filename=path.name)
        return send_file(path, as_attachment=True, download_name=path.name)


def _register_products(app, cfg, get_con):
    def _product_or_404(con, pid):
        row = store.get_product(con, pid)
        if row is None:
            abort(404)
        return row

    @app.get('/products')
    def products():
        q = (request.args.get('q') or '').strip()[:100]
        status = request.args.get('status', '')
        on_date = request.args.get('date') or store.today_iso()
        try:
            on_date = store.parse_iso_date(on_date, field='Дата')
        except store.WebStoreError as e:
            flash(str(e), 'error')
            on_date = store.today_iso()
        con = get_con()
        try:
            views = store.list_products(con, cfg, q=q, status=status,
                                        on_date=on_date)
        finally:
            con.close()
        return render_template('products.html', products=views, q=q,
                               status=status, on_date=on_date)

    @app.get('/products/<int:pid>')
    def product_card(pid):
        on_date = request.args.get('date') or store.today_iso()
        con = get_con()
        try:
            row = _product_or_404(con, pid)
            snap_run, snap_map = store.latest_snapshot_map(con)
            view = store.product_view(con, cfg, row, on_date, snap_run,
                                      snap_map)
            history = store.product_history(con, pid)
            kinds = [r['name'] for r in con.execute(
                'SELECT name FROM extra_kinds ORDER BY name')]
        finally:
            con.close()
        return render_template('product.html', p=view, history=history,
                               kinds=kinds, today=on_date)

    @app.post('/products/<int:pid>/manual')
    def product_manual(pid):
        action = request.form.get('action', 'set')
        con = get_con()
        try:
            _product_or_404(con, pid)
            if action == 'clear':
                store.set_manual_price(con, pid, None, store.today_iso(),
                                   user=audit_user())
                flash('Источник цены переключён на 1С/снимок расчёта.', 'ok')
            else:
                price = store.parse_money(request.form.get('price'),
                                          field='Ручная закупочная цена')
                valid_from = store.parse_iso_date(
                    request.form.get('valid_from'), field='Дата начала',
                    default=store.today_iso())
                store.set_manual_price(con, pid, price, valid_from,
                                   user=audit_user())
                flash(f'Ручная цена {price} ₽ сохранена с {valid_from}.',
                      'ok')
        except store.WebStoreError as e:
            flash(str(e), 'error')
        finally:
            con.close()
        return redirect(url_for('product_card', pid=pid))

    @app.post('/products/<int:pid>/ppu')
    def product_ppu(pid):
        con = get_con()
        try:
            _product_or_404(con, pid)
            weight = store.parse_weight(request.form.get('weight_g'))
            valid_from = store.parse_iso_date(
                request.form.get('valid_from'), field='Дата начала',
                default=store.today_iso())
            store.set_ppu_weight(con, pid, weight, valid_from,
                                 user=audit_user())
            flash(f'Вес ППУ {weight} г сохранён с {valid_from}.', 'ok')
        except store.WebStoreError as e:
            flash(str(e), 'error')
        finally:
            con.close()
        return redirect(url_for('product_card', pid=pid))

    @app.post('/products/<int:pid>/extras')
    def product_extras(pid):
        con = get_con()
        try:
            _product_or_404(con, pid)
            kind = (request.form.get('kind') or '').strip()
            rub = store.parse_money(request.form.get('rub'),
                                    field='Сумма доп. расхода')
            valid_from = store.parse_iso_date(
                request.form.get('valid_from'), field='Дата начала',
                default=store.today_iso())
            valid_to_raw = (request.form.get('valid_to') or '').strip()
            valid_to = (store.parse_iso_date(valid_to_raw,
                                             field='Дата окончания')
                        if valid_to_raw else None)
            store.add_extra(con, pid, kind, rub, valid_from, valid_to,
                            user=audit_user())
            flash(f'Доп. расход «{kind}» {rub} ₽ сохранён с {valid_from}.',
                  'ok')
        except store.WebStoreError as e:
            flash(str(e), 'error')
        finally:
            con.close()
        return redirect(url_for('product_card', pid=pid))

    @app.post('/extras/<int:eid>/close')
    def extra_close(eid):
        valid_to = request.form.get('valid_to')
        con = get_con()
        try:
            row = con.execute('SELECT product_id FROM extras WHERE id = ?',
                              (eid,)).fetchone()
            if row is None:
                abort(404)
            valid_to = store.parse_iso_date(valid_to,
                                            field='Дата окончания')
            store.close_extra(con, eid, valid_to, user=audit_user())
            flash(f'Статья доп. расхода закрыта с {valid_to}.', 'ok')
        except store.WebStoreError as e:
            flash(str(e), 'error')
            row = None
        finally:
            con.close()
        if row is None:
            return redirect(url_for('products'))
        return redirect(url_for('product_card', pid=row['product_id']))


def _register_expenses(app, cfg, get_con):
    @app.get('/expenses')
    def expenses():
        on_date = request.args.get('date') or store.today_iso()
        try:
            on_date = store.parse_iso_date(on_date, field='Дата')
        except store.WebStoreError as e:
            flash(str(e), 'error')
            on_date = store.today_iso()
        con = get_con()
        try:
            views = store.list_products(con, cfg, on_date=on_date)
            kinds = [r['name'] for r in con.execute(
                'SELECT name FROM extra_kinds ORDER BY name')]
            settings = store.settings_as_of(con, on_date)
            changes = con.execute(
                'SELECT at, user, entity, entity_id, new FROM audit_log'
                " WHERE entity IN ('extras', 'ppu_weights', 'settings')"
                ' ORDER BY id DESC LIMIT 40').fetchall()
        finally:
            con.close()
        return render_template('expenses.html', products=views, kinds=kinds,
                               changes=changes, today=on_date,
                               settings=settings)

    @app.post('/expenses/ppu')
    def expenses_ppu():
        try:
            pid = int(request.form.get('product_id', ''))
        except ValueError:
            flash('Выберите товар', 'error')
            return redirect(url_for('expenses'))
        con = get_con()
        try:
            if store.get_product(con, pid) is None:
                raise store.WebStoreError('Товар не найден')
            weight = store.parse_weight(request.form.get('weight_g'))
            valid_from = store.parse_iso_date(
                request.form.get('valid_from'), field='Дата начала',
                default=store.today_iso())
            store.set_ppu_weight(con, pid, weight, valid_from,
                                 user=audit_user())
            flash(f'Вес ППУ {weight} г сохранён с {valid_from}.', 'ok')
        except store.WebStoreError as e:
            flash(str(e), 'error')
        finally:
            con.close()
        return redirect(url_for('expenses'))

    @app.post('/expenses/extras')
    def expenses_extras():
        try:
            pid = int(request.form.get('product_id', ''))
        except ValueError:
            flash('Выберите товар', 'error')
            return redirect(url_for('expenses'))
        con = get_con()
        try:
            if store.get_product(con, pid) is None:
                raise store.WebStoreError('Товар не найден')
            kind = (request.form.get('kind') or '').strip()
            rub = store.parse_money(request.form.get('rub'),
                                    field='Сумма доп. расхода')
            valid_from = store.parse_iso_date(
                request.form.get('valid_from'), field='Дата начала',
                default=store.today_iso())
            valid_to_raw = (request.form.get('valid_to') or '').strip()
            valid_to = (store.parse_iso_date(valid_to_raw,
                                             field='Дата окончания')
                        if valid_to_raw else None)
            store.add_extra(con, pid, kind, rub, valid_from, valid_to,
                            user=audit_user())
            flash(f'Доп. расход «{kind}» {rub} ₽ сохранён с {valid_from}.',
                  'ok')
        except store.WebStoreError as e:
            flash(str(e), 'error')
        finally:
            con.close()
        return redirect(url_for('expenses'))

    @app.post('/expenses/settings')
    def expenses_settings():
        con = get_con()
        try:
            rate = store.parse_money(request.form.get('ppu_rub_per_kg'),
                                     field='Ставка ППУ, ₽/кг')
            margin = store.parse_money(request.form.get('margin_pct'),
                                       field='Наценка, %')
            valid_from = store.parse_iso_date(
                request.form.get('valid_from'), field='Дата начала',
                default=store.today_iso())
            store.set_setting(con, 'ppu_rub_per_kg', rate, valid_from,
                              user=audit_user())
            store.set_setting(con, 'margin_pct', margin, valid_from,
                              user=audit_user())
            flash(f'Ставка ППУ {rate} ₽/кг и наценка {margin}% сохранены с '
                  f'{valid_from}.', 'ok')
        except store.WebStoreError as e:
            flash(str(e), 'error')
        finally:
            con.close()
        return redirect(url_for('expenses'))


def _register_issues(app, cfg, get_con):
    issue_statuses = set(store.ISSUE_STATUSES)

    @app.get('/issues')
    def issues():
        con = get_con()
        try:
            runs = store.list_runs(con, cfg, limit=50)
            run_id = request.args.get('run_id', type=int)
            if run_id is None:
                last = store.last_done_run(con)
                run_id = last['id'] if last is not None else None
            run = store.get_run(con, run_id) if run_id else None
            meta = store.load_run_meta(cfg, run_id) if run_id else {}
        finally:
            con.close()
        rows = [r for r in meta.get('rows', [])
                if r.get('status') in issue_statuses]
        groups: dict[tuple, dict] = {}
        for r in rows:
            key = (r.get('supplier_article'), r.get('status'))
            g = groups.setdefault(key, {
                'supplier_article': r.get('supplier_article'),
                'mp_article': r.get('mp_article'), 'status': r.get('status'),
                'product_id': r.get('product_id'), 'count': 0,
                'reasons': set(), 'marketplaces': set()})
            g['count'] += 1
            if r.get('reason'):
                g['reasons'].add(r['reason'])
            g['marketplaces'].add(r.get('marketplace'))
        grouped = sorted(groups.values(),
                         key=lambda g: (g['status'],
                                        str(g['supplier_article'])))
        explanations = {
            'AMBIGUOUS': '1С вернула несколько подходящих объектов или цен. '
                         'Автоматический выбор чужой цены запрещён. Задайте '
                         'ручную цену либо оставьте позицию нерешённой.',
            'NO_MATCH': 'Артикул поставщика отсутствует в каталоге products. '
                        'Заведите товар в каталоге; ручную цену неизвестному '
                        'товару не назначаем.',
            'NOT_FOUND': 'Товар не найден в 1С. Проверьте артикул/каталог; '
                         'при необходимости задайте ручную цену.',
            'NEEDS_CONFIRM': 'Сопоставление или источник цены требует '
                             'подтверждения владельцем. Автоматически цена не '
                             'принимается.',
            'NO_PRICE': 'Товар найден, но закупочная цена на дату расчёта '
                        'отсутствует. Ноль вместо цены не подставляется.',
            'ERROR': 'Ошибка источника/расчёта (в т.ч. подключение к 1С). '
                     'Проверьте диагностику 1С и повторите расчёт.',
            'PPU_UNCONFIRMED': 'Вес ППУ не утверждён на дату расчёта; '
                               'себестоимость не фиксируется как проверенная.',
        }
        return render_template('issues.html', rows=rows, groups=grouped,
                               runs=runs, run=run, run_id=run_id,
                               meta=meta, explanations=explanations,
                               onec=prices.onec_status(cfg),
                               today=store.today_iso())

    @app.post('/issues/manual')
    def issues_manual():
        try:
            pid = int(request.form.get('product_id', ''))
        except ValueError:
            flash('Не выбран товар для ручной цены', 'error')
            return redirect(url_for('issues'))
        run_id = request.form.get('run_id', type=int)
        con = get_con()
        try:
            if store.get_product(con, pid) is None:
                raise store.WebStoreError('Товар не найден')
            price = store.parse_money(request.form.get('price'),
                                      field='Ручная цена')
            valid_from = store.parse_iso_date(
                request.form.get('valid_from'), field='Дата начала',
                default=store.today_iso())
            store.set_manual_price(con, pid, price, valid_from,
                                   user=audit_user())
            flash(f'Ручная цена {price} ₽ сохранена для товара {pid} с '
                  f'{valid_from}. Повторите расчёт.', 'ok')
        except store.WebStoreError as e:
            flash(str(e), 'error')
        finally:
            con.close()
        return redirect(url_for('issues', run_id=run_id) if run_id
                        else url_for('issues'))

    @app.post('/issues/skip')
    def issues_skip():
        pid = request.form.get('product_id', type=int)
        run_id = request.form.get('run_id', type=int)
        con = get_con()
        try:
            store.audit(con, audit_user(), 'issues', pid,
                        None, {'decision': 'left_unresolved',
                               'run_id': run_id})
            con.commit()
            flash('Позиция оставлена нерешённой; решение записано в аудит. '
                  'Остальные товары рассчитаны независимо.', 'ok')
        finally:
            con.close()
        return redirect(url_for('issues', run_id=run_id) if run_id
                        else url_for('issues'))


def _register_history(app, cfg, get_con):
    @app.get('/history')
    def history():
        con = get_con()
        try:
            runs = store.list_runs(con, cfg, limit=200)
            total_snapshots = con.execute(
                'SELECT COUNT(*) FROM cost_snapshots').fetchone()[0]
            legacy_runs = con.execute(
                "SELECT COUNT(*) FROM runs WHERE source_1c = 'legacy'"
            ).fetchone()[0]
        finally:
            con.close()
        return render_template('history.html', runs=runs,
                               total_snapshots=total_snapshots,
                               legacy_runs=legacy_runs)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='Себестоимость МП — локальный '
                                             'веб-интерфейс')
    ap.add_argument('--host', default=None,
                    help='только 127.0.0.1/localhost')
    ap.add_argument('--port', type=int, default=None)
    ap.add_argument('--db', default=None, help='путь к SQLite (тестовая БД)')
    ap.add_argument('--base', default=None, help='каталог установки')
    args = ap.parse_args(argv)
    if args.db:
        os.environ['BLAGOMED_DB'] = str(Path(args.db).resolve())
    if args.host:
        os.environ['BLAGOMED_HOST'] = args.host
    if args.port:
        os.environ['BLAGOMED_PORT'] = str(args.port)
    cfg = web_config.load_config(args.base)
    app = create_app(cfg)
    log.info('Старт: http://%s:%s (БД: %s)', cfg.host, cfg.port,
             cfg.db_path)
    from waitress import serve
    serve(app, host=cfg.host, port=cfg.port, threads=4,
          channel_timeout=600, ident='blagomed-cost')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())