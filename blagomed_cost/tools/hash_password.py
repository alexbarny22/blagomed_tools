# -*- coding: utf-8 -*-
"""Создаёт хэш пароля для локального веб-интерфейса.

Использование:
    python tools/hash_password.py "пароль"
    python tools/hash_password.py            # пароль запрашивается скрытно

Полученную строку вставить в config.ini в раздел [auth] как pass_hash.
Сам пароль в config.ini и git не попадает.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from werkzeug.security import generate_password_hash


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Хэш пароля для [auth] pass_hash')
    ap.add_argument('password', nargs='?', help='пароль (иначе будет запрошен)')
    args = ap.parse_args(argv)
    password = args.password
    if password is None:
        password = getpass.getpass('Пароль: ')
        again = getpass.getpass('Повторите пароль: ')
        if password != again:
            print('Пароли не совпадают', file=sys.stderr)
            return 2
    if not password:
        print('Пустой пароль недопустим', file=sys.stderr)
        return 2
    print(generate_password_hash(password))
    print('\nВставьте строку выше в config.ini:\n[auth]\n'
          'user = ЛОГИН\npass_hash = <хэш>', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())