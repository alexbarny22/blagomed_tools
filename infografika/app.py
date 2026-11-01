# -*- coding: utf-8 -*-
"""
Генератор инфографики Благомед — локальная программа в браузере.
Запуск: start.bat. Страница открывается сама: http://127.0.0.1:8765
Закрыть программу — закрыть чёрное окно.
"""
import os
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from flask import Flask, abort, jsonify, request, send_from_directory  # noqa: E402

import engine as E  # noqa: E402
import settings as S  # noqa: E402
import styles as ST  # noqa: E402

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.json.sort_keys = False       # порядок профилей — как в настройках
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def proj(pid):
    p = E.get(pid)
    if not p:
        abort(404)
    return p


def ok(**kw):
    return jsonify({"ok": True, **kw})


def fail(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.route("/static/<path:name>")
def static_files(name):
    return send_from_directory(os.path.join(HERE, "static"), name)


@app.route("/fonts/<path:name>")
def fonts(name):
    return send_from_directory(os.path.join(HERE, "fonts"), name)


@app.route("/files/<pid>/<path:name>")
def files(pid, name):
    resp = send_from_directory(os.path.join(E.PROJECTS, pid), name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- справочники
@app.route("/api/meta")
def meta():
    s = S.load()
    icons = [{"name": k, "label": v[1], "char": v[0]} for k, v in E.render.ICONS.items()]
    look = {k: [{"v": v, "t": t} for v, t in opts] for k, opts in E.render.LOOK_OPTIONS.items()}
    return ok(settings=s, styles=ST.all_styles(), history=E.history()[:40], key_ok=bool(S.key()), icons=icons,
              look=look)


@app.route("/api/models")
def models():
    ids = E.vsellm_models(force=True)
    if ids is None:
        return fail("vsellm не ответил — проверьте интернет и выключите VPN", 502)
    words = ("image", "banana", "seedream", "imagine", "flux", "imagen")
    return ok(image=sorted(i for i in ids if any(w in i.lower() for w in words)),
              text=sorted(i for i in ids if not any(w in i.lower() for w in words + ("embed", "whisper", "tts"))))


@app.route("/api/settings", methods=["POST"])
def save_settings():
    return ok(settings=S.save(request.get_json(force=True) or {}))


@app.route("/api/styles", methods=["POST"])
def save_style():
    d = request.get_json(force=True) or {}
    p = proj(d.get("project"))
    name = (d.get("name") or "").strip()
    if not name:
        return fail("Введите название стиля")
    st = ST.save_style(p.st["plan"]["style"], name)
    return ok(style=st, styles=ST.all_styles())


@app.route("/api/styles/delete", methods=["POST"])
def delete_style():
    ST.delete_style((request.get_json(force=True) or {}).get("name", ""))
    return ok(styles=ST.all_styles())


# ---------------------------------------------------------------- серии
@app.route("/api/projects", methods=["POST"])
def create():
    if not S.key():
        return fail("В config.py не вписан ключ vsellm (VSELLM_KEY)")
    photos = [(f.filename, f.read()) for f in request.files.getlist("photos")
              if f.filename.lower().endswith(IMG_EXT)]
    refs = [(f.filename, f.read()) for f in request.files.getlist("refs") if f.filename.lower().endswith(IMG_EXT)]
    if not photos:
        return fail("Добавьте хотя бы одно фото товара (jpg или png)")
    fields = {k: request.form.get(k, "") for k in
              ("product_text", "wish", "covers", "style_mode", "style_name", "style_text", "profile", "limit")}
    if fields["style_mode"] == "refs" and not refs:
        fields["style_mode"] = "auto"
    p = E.create(fields, photos, refs)
    return ok(id=p.id)


@app.route("/api/projects/<pid>")
def state(pid):
    p = proj(pid)
    with p.lock:
        st = dict(p.st)
    st["busy"] = bool(p.thread and p.thread.is_alive())
    return ok(state=st)


@app.route("/api/projects/<pid>/plan", methods=["POST"])
def save_plan(pid):
    """Правки на шаге утверждения: ключи, тексты слайдов, тексты карточки, стиль, профиль, потолок."""
    p = proj(pid)
    d = request.get_json(force=True) or {}
    with p.lock:
        plan = p.st["plan"]
        if "keywords" in d:
            plan["keywords"] = {k: [str(x).strip() for x in d["keywords"].get(k, []) if str(x).strip()]
                                for k in ("main", "extra", "synonyms")}
        if "slides" in d:
            for i, sl in enumerate(d["slides"][:5]):
                tgt = plan["slides"][i]
                for k in ("headline", "subtitle"):
                    if k in sl:
                        tgt[k] = str(sl[k])
                if "bullets" in sl:
                    tgt["bullets"] = [str(b).strip() for b in sl["bullets"] if str(b).strip()]
                if "dims" in sl:
                    tgt["dims"] = [x for x in sl["dims"] if str(x.get("value", "")).strip()]
                for k in ("hero_word", "hero_rest"):
                    if k in sl:
                        tgt[k] = str(sl[k])
                if "features" in sl:
                    tgt["features"] = [f for f in sl["features"] if str(f.get("title", "")).strip()][:3]
                if "badge" in sl:
                    b = sl["badge"] or {}
                    tgt["badge"] = b if str(b.get("value", "")).strip() else None
                if "icons" in sl:
                    tgt["icons"] = sl["icons"]
                for k in ("footer", "tag", "accent_word"):
                    if k in sl:
                        tgt[k] = str(sl[k] or "").strip()
                if "look" in sl:
                    tgt["look"] = E.render.clean_look(sl["look"])
        if "cover_variants" in d:
            for i, v in enumerate(d["cover_variants"]):
                if i < len(plan["cover_variants"]):
                    plan["cover_variants"][i]["subtitle"] = str(v.get("subtitle", ""))
        if "texts" in d:
            plan["texts"].update({k: str(v) for k, v in d["texts"].items()})
        if "style" in d:
            plan["style"].update({k: v for k, v in d["style"].items()
                                  if k in ("font", "caps", "ink", "accent", "plate", "typo", "badge_color")})
            if "look" in d["style"]:
                plan["style"]["look"] = {k: v for k, v in E.render.clean_look(d["style"]["look"]).items()
                                         if k in E.render.SERIES_KEYS}
        if "profile" in d:
            p.st["inputs"]["profile"] = d["profile"]
        if "limit" in d:
            try:
                p.st["limit"] = max(1.0, float(d["limit"]))
            except (TypeError, ValueError):
                pass
        p.save()
    return ok()


@app.route("/api/projects/<pid>/rewrite-texts", methods=["POST"])
def rewrite(pid):
    p = proj(pid)
    try:
        p.run(E.rewrite_texts_job, p)
    except RuntimeError as e:
        return fail(str(e))
    return ok()


@app.route("/api/projects/<pid>/generate", methods=["POST"])
def generate(pid):
    p = proj(pid)
    try:
        p.run(E.generate_job, p)
    except RuntimeError as e:
        return fail(str(e))
    return ok()


@app.route("/api/projects/<pid>/continue", methods=["POST"])
def cont(pid):
    p = proj(pid)
    try:
        if not p.st.get("plan"):
            p.run(E.plan_job, p)
        elif not p.st.get("slides"):
            p.set(status="plan_ready")
        else:
            p.run(E.generate_missing_job, p)
    except RuntimeError as e:
        return fail(str(e))
    return ok()


@app.route("/api/projects/<pid>/budget", methods=["POST"])
def budget(pid):
    p = proj(pid)
    d = request.get_json(force=True) or {}
    with p.lock:
        if d.get("action") == "stop":
            p.st["stop_requested"] = True
        else:
            try:
                add = float(d.get("amount", 50))
            except (TypeError, ValueError):
                return fail("Введите сумму числом")
            p.st["limit"] = round(p.st["limit"] + max(0.0, add), 2)
        p.save()
    p.resume.set()
    return ok()


@app.route("/api/projects/<pid>/slides/<key>/text", methods=["POST"])
def slide_text(pid, key):
    p = proj(pid)
    E.update_slide_text(p, key, request.get_json(force=True) or {})
    return ok()


@app.route("/api/projects/<pid>/slides/<key>/regen", methods=["POST"])
def slide_regen(pid, key):
    p = proj(pid)
    wish = (request.get_json(force=True) or {}).get("wish", "")
    try:
        p.run(E.regen_slide_job, p, key, wish)
    except RuntimeError as e:
        return fail(str(e))
    return ok()


@app.route("/api/projects/<pid>/style", methods=["POST"])
def restyle(pid):
    p = proj(pid)
    E.update_style(p, request.get_json(force=True) or {})
    return ok()


@app.route("/api/projects/<pid>/texts", methods=["POST"])
def save_texts(pid):
    p = proj(pid)
    with p.lock:
        p.st["plan"]["texts"].update({k: str(v) for k, v in (request.get_json(force=True) or {}).items()})
        p.save()
    E.export(p)
    return ok()


@app.route("/api/projects/<pid>/open", methods=["POST"])
def open_folder(pid):
    p = proj(pid)
    out = E.export(p)
    if os.name == "nt":
        os.startfile(out)       # noqa: S606
    return ok(path=out)


def main():
    s = S.load()
    port = int(s.get("port", 8765))
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Генератор инфографики запущен: {url}")
    print("  Страница откроется в браузере сама. Чтобы закрыть программу — закройте это окно.\n")
    threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
