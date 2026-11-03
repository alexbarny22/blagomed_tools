"""Репрайсер Ozon — Благомед. Запуск: python app.py, затем откройте http://127.0.0.1:8765"""
import csv
import io
import json
import os
import socket
import sys
import threading
import webbrowser

from flask import Flask, jsonify, render_template, request, redirect, url_for, Response, flash

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import db  # noqa: E402
from core.engine import Repricer, MODES, status_of, controlled_price, _f  # noqa: E402
from core.ozon import make_client, OzonError  # noqa: E402

HOST, PORT = "127.0.0.1", 8765
BASE = os.path.dirname(os.path.abspath(__file__))

# при запуске без окна (pythonw) пишем вывод и ошибки в data/app.log
if sys.stdout is None or sys.stderr is None:
    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    _logf = open(os.path.join(BASE, "data", "app.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _logf

app = Flask(__name__)
app.secret_key = "blagomed-repricer-local"
db.init()
rp = Repricer()


@app.context_processor
def inject():
    s = db.get_settings()
    return {"S": s, "ST": rp.status, "MODES": MODES, "has_keys": bool(s["client_id"] and s["api_key"]),
            "VERSION": app_version()}


def app_version():
    try:
        with open(os.path.join(BASE, "version.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "—"


UPDATE_MARK = os.path.join(BASE, "data", "last_update.txt")


def self_update():
    """Проверка обновлений на GitHub при запуске. Если обновилось — перезапуск уже новой версией."""
    if "--no-update" in sys.argv:
        return
    try:
        import update
        res = update.main()
    except Exception as e:
        print(f"Автообновление не сработало: {e}")
        return
    if not res:
        return
    os.makedirs(os.path.dirname(UPDATE_MARK), exist_ok=True)
    with open(UPDATE_MARK, "w", encoding="utf-8") as f:
        f.write(f"{res[0] or '—'} -> {res[1]}")
    import subprocess
    subprocess.Popen([sys.executable, os.path.abspath(__file__)] + sys.argv[1:] + ["--no-update"], cwd=BASE)
    os._exit(0)


def log_update():
    try:
        with open(UPDATE_MARK, encoding="utf-8") as f:
            db.log("info", f"Программа обновлена с GitHub: версия {f.read().strip()}")
        os.remove(UPDATE_MARK)
    except FileNotFoundError:
        pass


def fmt(v):
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


app.jinja_env.filters["rub"] = fmt


# ---------------- страницы ----------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/product/<int:pid>")
def product(pid):
    p = db.q("SELECT * FROM products WHERE product_id=?", (pid,), one=True)
    if not p:
        flash("Товар не найден")
        return redirect(url_for("index"))
    s = db.get_settings()
    rule = db.get_rule(pid)
    st, st_text = status_of(p, rule, s["control_basis"])
    changes = db.q("SELECT * FROM changes WHERE product_id=? ORDER BY id DESC LIMIT 100", (pid,))
    actions = json.loads(p.get("actions") or "[]")
    return render_template("product.html", p=p, rule=rule, st=st, st_text=st_text, changes=changes,
                           actions=actions, controlled=controlled_price(p, s["control_basis"]))


@app.route("/changes")
def changes():
    kind = request.args.get("kind", "all")
    sql = "SELECT c.*, p.name FROM changes c LEFT JOIN products p ON p.product_id=c.product_id"
    if kind == "real":
        sql += " WHERE c.dry_run=0 AND c.ok=1"
    elif kind == "dry":
        sql += " WHERE c.dry_run=1"
    elif kind == "err":
        sql += " WHERE c.ok=0"
    rows = db.q(sql + " ORDER BY c.id DESC LIMIT 500")
    return render_template("changes.html", rows=rows, kind=kind)


@app.route("/events")
def events():
    rows = db.q("SELECT * FROM events ORDER BY id DESC LIMIT 300")
    return render_template("events.html", rows=rows)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        f = request.form
        for key in ("client_id", "interval_min", "cooldown_min", "max_step_pct", "max_changes_day",
                    "control_basis", "visibility", "history_days"):
            if key in f:
                db.set_setting(key, f.get(key, "").strip())
        if f.get("api_key", "").strip() and "•" not in f.get("api_key"):
            db.set_setting("api_key", f.get("api_key").strip())
        for key in ("dry_run", "auto_enabled", "push_min_to_ozon"):
            db.set_setting(key, "1" if f.get(key) else "0")
        db.log("info", "Настройки сохранены. Режим: " + ("тренировка" if f.get("dry_run") else "БОЕВОЙ"))
        flash("Настройки сохранены")
        return redirect(url_for("settings"))
    return render_template("settings.html")


# ---------------- API для интерфейса ----------------
@app.route("/api/products")
def api_products():
    s = db.get_settings()
    rules = {r["product_id"]: r for r in db.q("SELECT * FROM rules")}
    last = {r["product_id"]: r for r in db.q(
        "SELECT product_id, MAX(ts) AS ts FROM changes WHERE ok=1 AND dry_run=0 GROUP BY product_id")}
    out = []
    for p in db.q("SELECT * FROM products WHERE active=1 ORDER BY name"):
        rule = rules.get(p["product_id"]) or db.get_rule(p["product_id"])
        st, st_text = status_of(p, rule, s["control_basis"])
        out.append({
            "product_id": p["product_id"], "offer_id": p["offer_id"], "name": p["name"] or "",
            "price": p["price"], "old_price": p["old_price"], "seller_price": p["seller_price"],
            "buyer_est": p["buyer_est"], "ozon_min_price": p["ozon_min_price"], "color": p["color_index"],
            "controlled": controlled_price(p, s["control_basis"]),
            "mode": rule.get("mode") or "watch", "min": rule.get("min_price"), "max": rule.get("max_price"),
            "target": rule.get("target"), "tolerance": rule.get("tolerance") or 2,
            "status": st, "status_text": st_text, "last_change": (last.get(p["product_id"]) or {}).get("ts"),
            "n_actions": len(json.loads(p["actions"] or "[]")), "locked": bool(rule.get("locked")),
            "hidden": bool(rule.get("hidden")), "image": p.get("image") or "",
        })
    return jsonify(out)


@app.route("/api/status")
def api_status():
    s = db.get_settings()
    day = db.q("SELECT COUNT(*) n FROM changes WHERE ok=1 AND dry_run=0 AND ts >= datetime('now','localtime','-1 day')",
               one=True)["n"]
    return jsonify({**rp.status, "dry_run": s["dry_run"] == "1", "auto": s["auto_enabled"] == "1",
                    "changes_day": day, "has_keys": bool(s["client_id"] and s["api_key"])})


@app.route("/api/run", methods=["POST"])
def api_run():
    if rp.status["running"]:
        return jsonify({"ok": False, "message": "Проверка уже идёт"})
    rp.trigger()
    return jsonify({"ok": True, "message": "Проверка запущена"})


def _rule_from(d):
    mode = d.get("mode") if d.get("mode") in MODES else "watch"
    mn, mx, tg = _f(d.get("min")), _f(d.get("max")), _f(d.get("target"))
    tol = _f(d.get("tolerance")) or 2
    if mn and mx and mn > mx:
        raise ValueError("Минимум больше максимума")
    if mode == "hold" and not tg:
        raise ValueError("Для режима «Держать цену» укажите цену удержания")
    if mode == "guard" and not (mn or mx):
        raise ValueError("Для режима «Держать в коридоре» укажите минимум и/или максимум")
    return dict(mode=mode, min_price=mn, max_price=mx, target=tg, tolerance=tol)


@app.route("/api/rule", methods=["POST"])
def api_rule():
    d = request.get_json(force=True)
    pid = int(d["product_id"])
    try:
        r = _rule_from(d)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    old = db.get_rule(pid)
    if old.get("locked") and r["mode"] != "watch":
        return jsonify({"ok": False, "message": "Товар отмечен «Под MPSpace» — сначала снимите отметку"}), 400
    db.save_rule(pid, note=old.get("note"), **r)
    p = db.q("SELECT * FROM products WHERE product_id=?", (pid,), one=True)
    st, st_text = status_of(p, db.get_rule(pid), db.get_settings()["control_basis"]) if p else ("", "")
    return jsonify({"ok": True, "status": st, "status_text": st_text})


@app.route("/api/rules/bulk", methods=["POST"])
def api_rules_bulk():
    d = request.get_json(force=True)
    ids = [int(x) for x in d.get("ids") or []]
    s = db.get_settings()
    done, errors, skipped = 0, [], 0
    for pid in ids:
        p = db.q("SELECT * FROM products WHERE product_id=?", (pid,), one=True)
        if not p:
            continue
        rule = db.get_rule(pid)
        if rule.get("locked"):
            skipped += 1  # товары под MPSpace массовые операции не трогают
            continue
        cur = controlled_price(p, s["control_basis"]) or p["price"]
        if d.get("clear"):
            rule.update(mode="watch", min_price=None, max_price=None, target=None)
        if _f(d.get("min_pct")) is not None and cur:
            rule["min_price"] = round(cur * _f(d["min_pct"]) / 100)
        if _f(d.get("min_abs")) is not None:
            rule["min_price"] = _f(d["min_abs"])
        if _f(d.get("max_pct")) is not None and cur:
            rule["max_price"] = round(cur * _f(d["max_pct"]) / 100)
        if d.get("target_current") and cur:
            rule["target"] = round(cur)
        if _f(d.get("tolerance")) is not None:
            rule["tolerance"] = _f(d["tolerance"])
        if d.get("mode") in MODES:
            rule["mode"] = d["mode"]
        try:
            r = _rule_from({"mode": rule["mode"], "min": rule["min_price"], "max": rule["max_price"],
                            "target": rule["target"], "tolerance": rule["tolerance"]})
            db.save_rule(pid, note=rule.get("note"), **r)
            done += 1
        except ValueError as e:
            errors.append(f"{p['offer_id']}: {e}")
    return jsonify({"ok": True, "done": done, "errors": errors[:20], "skipped": skipped})


@app.route("/api/hide", methods=["POST"])
def api_hide():
    d = request.get_json(force=True)
    ids = [int(x) for x in d.get("ids") or []]
    hidden = bool(d.get("hidden"))
    for pid in ids:
        db.set_hidden(pid, hidden)
    db.log("info", f"{'Скрыто' if hidden else 'Возвращено'} в таблицах товаров: {len(ids)}")
    return jsonify({"ok": True, "done": len(ids)})


@app.route("/api/lock", methods=["POST"])
def api_lock():
    d = request.get_json(force=True)
    ids = [int(x) for x in d.get("ids") or []]
    locked = bool(d.get("locked"))
    for pid in ids:
        db.set_locked(pid, locked)
    db.log("info", f"Отметка «Под MPSpace» {'поставлена' if locked else 'снята'}: товаров {len(ids)}")
    return jsonify({"ok": True, "done": len(ids)})


def _period():
    """Период из запроса: days=1..31 или from/to (не больше 31 дня). Возвращает (from, to) строками."""
    from datetime import datetime, timedelta
    fmt = "%Y-%m-%d %H:%M:%S"
    now = datetime.now()
    try:
        if request.args.get("from"):
            f = datetime.strptime(request.args["from"][:19], fmt)
            t = datetime.strptime(request.args.get("to", now.strftime(fmt))[:19], fmt)
            t = min(t, now)
            if f >= t:
                f = t - timedelta(days=1)
            if t - f > timedelta(days=31, hours=1):
                f = t - timedelta(days=31)
        else:
            days = min(max(float(request.args.get("days", 7)), 1 / 24), 400)
            t, f = now, now - timedelta(days=days)
    except ValueError:
        t, f = now, now - timedelta(days=7)
    return f.strftime(fmt), t.strftime(fmt)


HIST_COLS = "ts, price, old_price, seller_price, buyer_est, ozon_min_price"


def _history(pid, f, t):
    rows = db.q(f"SELECT {HIST_COLS} FROM price_history WHERE product_id=? AND ts>=? AND ts<=? ORDER BY ts",
                (pid, f, t))
    before = db.q(f"SELECT {HIST_COLS} FROM price_history WHERE product_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
                  (pid, f), one=True)
    if before:
        rows.insert(0, before)
    return rows


def _changes(pid, f, t, with_dry):
    sql = ("SELECT ts, price_before, price_after, reason, dry_run, source FROM changes WHERE product_id=? "
           "AND ok=1 AND ts>=? AND ts<=? AND source != 'мин. цена'")
    if not with_dry:
        sql += " AND dry_run=0"
    return db.q(sql + " ORDER BY ts", (pid, f, t))


@app.route("/api/history/<int:pid>")
def api_history(pid):
    f, t = _period()
    rule = db.get_rule(pid)
    with_dry = db.get_settings()["dry_run"] == "1"
    return jsonify({"rows": _history(pid, f, t), "from": f, "to": t,
                    "changes": _changes(pid, f, t, with_dry),
                    "min": rule.get("min_price"), "max": rule.get("max_price"),
                    "target": rule.get("target") if rule.get("mode") == "hold" else None})


@app.route("/charts")
def charts():
    return render_template("charts.html")


@app.route("/api/charts")
def api_charts():
    """Все товары с историей за период + сводка изменений по дням/часам."""
    from datetime import datetime
    f, t = _period()
    s = db.get_settings()
    with_dry = s["dry_run"] == "1"
    rules = {r["product_id"]: r for r in db.q("SELECT * FROM rules")}
    hist = {}
    for r in db.q(f"SELECT product_id, {HIST_COLS} FROM price_history WHERE ts>=? AND ts<=? ORDER BY ts", (f, t)):
        hist.setdefault(r.pop("product_id"), []).append(r)
    before = {}
    for r in db.q(f"""SELECT h.product_id, {', '.join('h.' + c.strip() for c in HIST_COLS.split(','))}
                      FROM price_history h JOIN (SELECT product_id, MAX(id) AS mid FROM price_history
                      WHERE ts<? GROUP BY product_id) m ON m.mid=h.id""", (f,)):
        before[r.pop("product_id")] = r
    chg = {}
    sql = ("SELECT product_id, ts, price_before, price_after, reason, dry_run, source FROM changes WHERE ok=1 "
           "AND ts>=? AND ts<=? AND source != 'мин. цена'" + ("" if with_dry else " AND dry_run=0"))
    for r in db.q(sql + " ORDER BY ts", (f, t)):
        chg.setdefault(r.pop("product_id"), []).append(r)

    out = []
    for p in db.q("SELECT * FROM products WHERE active=1"):
        pid = p["product_id"]
        rows = hist.get(pid, [])
        if pid in before:
            rows = [before[pid]] + rows
        rule = rules.get(pid) or {}
        ctrl = [r["seller_price"] or r["price"] for r in rows if (r["seller_price"] or r["price"])]
        first, last = (ctrl[0], ctrl[-1]) if ctrl else (None, None)
        st, st_text = status_of(p, db.get_rule(pid), s["control_basis"])
        out.append({
            "product_id": pid, "offer_id": p["offer_id"], "name": p["name"] or "",
            "rows": rows, "changes": chg.get(pid, []),
            "min": rule.get("min_price"), "max": rule.get("max_price"),
            "target": rule.get("target") if rule.get("mode") == "hold" else None,
            "mode": rule.get("mode") or "watch", "locked": bool(rule.get("locked")),
            "hidden": bool(rule.get("hidden")), "image": p.get("image") or "",
            "first": first, "last": last,
            "delta_pct": round((last - first) / first * 100, 1) if first and last else 0,
            "lo": min(ctrl) if ctrl else None, "hi": max(ctrl) if ctrl else None,
            "n_points": len(rows), "status": st, "status_text": st_text,
        })

    # сводка: изменения цен по часам (период до 3 дней) или по дням
    span = datetime.strptime(t, "%Y-%m-%d %H:%M:%S") - datetime.strptime(f, "%Y-%m-%d %H:%M:%S")
    by_hour = span.total_seconds() <= 3 * 86400 + 60
    key_len = 13 if by_hour else 10
    buckets = {}
    for r in db.q("SELECT ts, dry_run FROM changes WHERE ok=1 AND ts>=? AND ts<=? AND source != 'мин. цена'", (f, t)):
        k = r["ts"][:key_len]
        b = buckets.setdefault(k, [0, 0])
        b[1 if r["dry_run"] else 0] += 1
    moved = {}
    for pid, rows in hist.items():
        # первая запись товара — это начальный снимок, а не изменение цены
        for r in (rows if pid in before else rows[1:]):
            moved.setdefault(r["ts"][:key_len], set()).add(pid)
    return jsonify({"from": f, "to": t, "products": out, "by_hour": by_hour,
                    "buckets": [{"k": k, "real": v[0], "dry": v[1]} for k, v in sorted(buckets.items())],
                    "moved": [{"k": k, "n": len(v)} for k, v in sorted(moved.items())]})


@app.route("/api/manual/<int:pid>", methods=["POST"])
def api_manual(pid):
    d = request.get_json(force=True)
    price, old, mn = _f(d.get("price")), _f(d.get("old_price")), _f(d.get("min_price"))
    if not price or price <= 0:
        return jsonify({"ok": False, "message": "Укажите цену"}), 400
    if old and old <= price:
        return jsonify({"ok": False, "message": "Зачёркнутая цена должна быть больше цены (или 0)"}), 400
    if db.is_locked(pid) and not d.get("force"):
        return jsonify({"ok": False, "need_force": True,
                        "message": "Товар под MPSpace — MPSpace может сразу перебить эту цену"}), 409
    try:
        ok, err = rp.manual_set(pid, price, old or 0, mn)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    return jsonify({"ok": ok, "message": "Цена отправлена в Ozon" if ok else f"Ozon отклонил: {err}"})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    db.log("info", "Репрайсер остановлен из интерфейса")
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return jsonify({"ok": True, "message": "Программа остановлена"})


@app.route("/api/test", methods=["POST"])
def api_test():
    d = request.get_json(force=True) or {}
    s = db.get_settings()
    key = d.get("api_key") or ""
    if not key or "•" in key:
        key = s["api_key"]
    try:
        items = make_client(d.get("client_id") or s["client_id"], key).get_all_prices("ALL")
        return jsonify({"ok": True, "message": f"Подключение работает. Товаров в Ozon: {len(items)}"})
    except OzonError as e:
        return jsonify({"ok": False, "message": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Ошибка: {e}"})


# ---------------- CSV правил (открывается в Excel) ----------------
CSV_HEAD = ["product_id", "Артикул", "Название", "Цена", "Контролируемая цена", "Режим (watch/guard/hold)",
            "Минимум", "Максимум", "Цена удержания", "Допуск %", "Под MPSpace (1 = да)"]


@app.route("/rules.csv")
def rules_csv():
    s = db.get_settings()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(CSV_HEAD)
    for p in db.q("SELECT * FROM products WHERE active=1 ORDER BY name"):
        r = db.get_rule(p["product_id"])
        c = controlled_price(p, s["control_basis"])
        num = lambda v: "" if v is None else str(int(round(v))) if float(v).is_integer() else str(v).replace(".", ",")
        w.writerow([p["product_id"], p["offer_id"], p["name"] or "", num(p["price"]), num(c), r["mode"],
                    num(r["min_price"]), num(r["max_price"]), num(r["target"]), num(r["tolerance"] or 2),
                    "1" if r.get("locked") else ""])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=rules_ozon.csv"})


@app.route("/rules/import", methods=["POST"])
def rules_import():
    f = request.files.get("file")
    if not f:
        flash("Файл не выбран")
        return redirect(url_for("index"))
    raw = f.read()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    delim = ";" if text.count(";") >= text.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    next(reader, None)
    ok, bad, locked_n = 0, [], 0
    num = lambda v: _f((v or "").replace(" ", "").replace("\xa0", "").replace(",", "."))
    for row in reader:
        if len(row) < 10 or not row[0].strip().isdigit():
            continue
        mode = (row[5] or "watch").strip().lower()
        mode = {"следить": "watch", "коридор": "guard", "удержание": "hold"}.get(mode, mode)
        pid = int(row[0])
        # из файла отметку можно только поставить; снять — только в интерфейсе
        if len(row) > 10 and row[10].strip() in ("1", "да", "Да", "x", "х", "+"):
            if not db.is_locked(pid):
                db.set_locked(pid, True)
                locked_n += 1
        if db.is_locked(pid):
            if mode != "watch":
                bad.append(f"{row[1]}: под MPSpace — режим не изменён")
            mode = "watch"
        try:
            r = _rule_from({"mode": mode, "min": num(row[6]), "max": num(row[7]), "target": num(row[8]),
                            "tolerance": num(row[9])})
            db.save_rule(pid, **r)
            ok += 1
        except ValueError as e:
            bad.append(f"{row[1]}: {e}")
    db.log("info", f"Импорт правил из CSV: загружено {ok}, отмечено «Под MPSpace» {locked_n}, "
                   f"с ошибками {len(bad)}")
    flash(f"Загружено правил: {ok}" + (f". Отмечено «Под MPSpace»: {locked_n}" if locked_n else "")
          + (f". Замечания: {'; '.join(bad[:10])}" if bad else ""))
    return redirect(url_for("index"))


def port_busy():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sck:
        return sck.connect_ex((HOST, PORT)) == 0


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    if port_busy():
        # репрайсер уже запущен — просто открываем интерфейс
        webbrowser.open(url)
        sys.exit(0)
    self_update()
    log_update()
    db.log("info", f"Репрайсер запущен, версия {app_version()}")
    rp.start()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Репрайсер Ozon работает: {url}  (не закрывайте это окно)")
    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT, threads=8)
    except ImportError:
        app.run(host=HOST, port=PORT, threaded=True)
