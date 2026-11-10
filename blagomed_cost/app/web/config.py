# -*- coding: utf-8 -*-
"""Конфигурация локального веб-приложения «Себестоимость МП».

Все пути абсолютные и определяются от каталога установки (BLAGOMED_HOME)
или от корня проекта. Секреты (пароль 1С) остаются только в config.ini и
никогда не выводятся в интерфейс/логи.
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

ALLOWED_HOSTS = {'127.0.0.1', 'localhost', '0.0.0.0'}


class ConfigError(RuntimeError):
    """Некорректная конфигурация веб-приложения."""


@dataclass(frozen=True)
class WebConfig:
    base_dir: Path            # каталог установки (C:\Blagomed\SebestoimostMP)
    data_dir: Path
    db_path: Path
    uploads: Path
    output: Path
    logs: Path
    history_dir: Path
    config_ini: Path
    host: str = '127.0.0.1'
    port: int = 8000
    max_upload_mb: int = 25
    auth_user: str = ''
    auth_pass_hash: str = ''

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def auth_configured(self) -> bool:
        return bool(self.auth_user and self.auth_pass_hash)


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, '').strip()
    return Path(raw).expanduser().resolve() if raw else default.resolve()


def _read_ini(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding='utf-8')
    return cfg


def load_config(base_dir: str | Path | None = None) -> WebConfig:
    base = Path(base_dir).resolve() if base_dir else _env_path(
        'BLAGOMED_HOME', Path(__file__).resolve().parents[2])
    data = _env_path('BLAGOMED_DATA', base / 'data')
    uploads = _env_path('BLAGOMED_UPLOADS', base / 'uploads')
    output = _env_path('BLAGOMED_OUTPUT', base / 'output')
    logs = _env_path('BLAGOMED_LOGS', base / 'logs')
    ini = Path(os.environ.get('BLAGOMED_CONFIG_INI', '').strip() or
               base / 'config.ini').resolve()

    cfg = _read_ini(ini)
    db_raw = os.environ.get('BLAGOMED_DB', '').strip()
    if not db_raw and cfg.has_option('web', 'db_path'):
        db_raw = cfg.get('web', 'db_path').strip()
    db_path = (Path(db_raw).expanduser().resolve()
               if db_raw else (data / 'cost.db').resolve())

    host = os.environ.get('BLAGOMED_HOST', '').strip()
    if not host and cfg.has_option('web', 'host'):
        host = cfg.get('web', 'host').strip()
    host = host or '127.0.0.1'
    if host not in ALLOWED_HOSTS:
        raise ConfigError(
            f'Хост {host!r} запрещён: допустимы 127.0.0.1/localhost '
            f'(этот компьютер) или 0.0.0.0 (локальная сеть, только '
            f'со входом). Допустимые значения: '
            f'{", ".join(sorted(ALLOWED_HOSTS))}.')
    port_raw = os.environ.get('BLAGOMED_PORT', '').strip()
    if not port_raw and cfg.has_option('web', 'port'):
        port_raw = cfg.get('web', 'port')
    try:
        port = int(port_raw or 8000)
    except ValueError:
        raise ConfigError(f'Некорректный порт: {port_raw!r}') from None
    if not (1 <= port <= 65535):
        raise ConfigError(f'Порт вне диапазона 1..65535: {port}')
    try:
        max_mb = int(os.environ.get('BLAGOMED_MAX_UPLOAD_MB', '25'))
    except ValueError:
        raise ConfigError('BLAGOMED_MAX_UPLOAD_MB должен быть целым') from None
    if not (1 <= max_mb <= 200):
        raise ConfigError('BLAGOMED_MAX_UPLOAD_MB вне диапазона 1..200')

    auth_user = os.environ.get('BLAGOMED_AUTH_USER', '').strip()
    if not auth_user and cfg.has_option('auth', 'user'):
        auth_user = cfg.get('auth', 'user').strip()
    auth_hash = os.environ.get('BLAGOMED_AUTH_HASH', '').strip()
    if not auth_hash and cfg.has_option('auth', 'pass_hash'):
        auth_hash = cfg.get('auth', 'pass_hash').strip()
    if auth_user in ('ЛОГИН',):
        auth_user = ''
    if auth_hash in ('ВСТАВИТЬ_СЮДА',):
        auth_hash = ''

    return WebConfig(
        base_dir=base, data_dir=data, db_path=db_path, uploads=uploads,
        output=output, logs=logs, history_dir=data / 'history',
        config_ini=ini, host=host, port=port, max_upload_mb=max_mb,
        auth_user=auth_user, auth_pass_hash=auth_hash)


def ensure_dirs(cfg: WebConfig) -> None:
    for p in (cfg.data_dir, cfg.uploads, cfg.output, cfg.logs,
              cfg.history_dir):
        p.mkdir(parents=True, exist_ok=True)