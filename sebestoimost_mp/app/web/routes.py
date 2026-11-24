from flask import Blueprint, render_template, jsonify
from datetime import date

web_bp = Blueprint('web', __name__, template_folder='templates')

@web_bp.route('/')
def index():
    today = date.today().isoformat()
    return render_template('index.html', today=today)

@web_bp.route('/health')
def health():
    return jsonify({'status': 'ok'})
