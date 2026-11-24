import os
from pathlib import Path
from flask import Flask
from app.db import close_db

def create_app(root: Path = None) -> Flask:
    if root is None:
        root = Path(__file__).parent.parent.resolve()
    app = Flask(__name__)
    app.config['DB_PATH'] = str(root / 'data' / 'sebestoimost.db')
    app.config['UPLOAD_DIR'] = str(root / 'data' / 'uploads')
    app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
    app.secret_key = 'blagomed-local-test'
    os.makedirs(root / 'data', exist_ok=True)
    os.makedirs(app.config['UPLOAD_DIR'], exist_ok=True)
    from app.web.routes import web_bp
    app.register_blueprint(web_bp)
    app.teardown_appcontext(close_db)
    return app
