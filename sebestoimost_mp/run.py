import sys
import threading
import webbrowser
from pathlib import Path
from app import create_app
from app.db import init_db

def main():
    try:
        root = Path(__file__).parent.resolve()
        try:
            from updater import auto_update
            auto_update(root)
        except Exception as e:
            print(f"[ОБНОВЛЕНИЕ] пропущено: {e}")
        db_path = root / 'data' / 'sebestoimost.db'
        init_db(str(db_path))
        # Одноразовое перечитывание сохранённых эталонов после миграции v4
        # (профили Ozon стали читать артикул поставщика — пересвязать товары).
        try:
            import sqlite3 as _sqlite3
            from app.products_sync import resync_templates_if_needed
            _conn = _sqlite3.connect(str(db_path))
            try:
                resync_templates_if_needed(
                    _conn, log=lambda m: print(f'[ЭТАЛОНЫ] {m}'))
            finally:
                _conn.close()
        except Exception as e:
            print(f'[ЭТАЛОНЫ] перечитывание пропущено: {e}')
        app = create_app(root)
        print(f"[INFO] База данных: {db_path}")
        print("[INFO] Запуск сервера на http://127.0.0.1:8085")
        threading.Timer(1.0, lambda: webbrowser.open('http://127.0.0.1:8085')).start()
        from waitress import serve
        serve(app, host='127.0.0.1', port=8085)
    except Exception as e:
        print(f"[ОШИБКА] Не удалось запустить приложение: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
