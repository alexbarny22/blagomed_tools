# -*- coding: utf-8 -*-
"""
Серверная версия бота передачи в магазин (позиции «(МГ)»).
Веб-страница: передача товара + управление ботом (обновить, перезапустить, журнал).
Логика (OData, документы, цены) — та же, что в move_bot.py.

Запуск — server_run.bat (в цикле: обновление с GitHub -> web.py; при выходе — снова).
"""
import base64
import datetime
import socket
import subprocess
import urllib.parse
import functools
import json
import os
import threading
import time
import traceback

from flask import Flask, Response, jsonify, request

import config
import move_bot as mb

HERE = mb.HERE
VERSION_FILE = os.path.join(HERE, "version.txt")
PORT = int(getattr(config, "WEB_PORT", 8090))
PASSWORD = str(getattr(config, "WEB_PASSWORD", "") or "")


def version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "?"


# ====================================================================== ядро без окна
class Core:
    """Те же методы, что у окна move_bot.App, но без интерфейса."""

    BORROW = ["PRICE_DB_VERSION", "store_names", "find_pair", "load_stock_and_prices",
              "get_prices", "load_price_db", "save_price_db", "fetch_price_docs",
              "update_price_db", "pick_article", "sort_articles", "find_template", "vat_fields",
              "move_work"]

    def __init__(self):
        self.od = mb.OData()
        self.od.progress = self.progress
        self.settings = mb.load_settings()
        self.warehouses, self.noms, self.pairs = {}, {}, {}
        self.mg_keys, self.mg_by_name, self.mapping = set(), {}, {}
        self.org_from = self.org_to = self.price_type = None
        self.items, self.ready, self.error = [], False, ""
        self.lock = threading.Lock()
        self.busy, self.status = False, "Запуск…"
        self.messages = []          # журнал для страницы
        self.result = None          # итог последней передачи
        self._last_progress = 0.0

    # --- журнал
    def say(self, msg):
        mb.log(msg)
        self.messages.append(datetime.datetime.now().strftime("%H:%M:%S  ") + msg)
        del self.messages[:-200]

    def progress(self, msg):
        now = time.time()
        self.status = msg
        if now - self._last_progress >= 3:
            self._last_progress = now
            self.say(msg)

    # --- фоновые задачи (по одной за раз)
    def run(self, title, fn):
        if not self.lock.acquire(blocking=False):
            return False

        def worker():
            self.busy, self.status = True, title
            self.say(title)
            try:
                fn()
                self.status = "Готово"
            except Exception as e:
                self.error = str(e)
                self.status = "Ошибка"
                self.say("ОШИБКА: " + str(e).replace("\n", " "))
                mb.log(traceback.format_exc())
            finally:
                self.busy = False
                self.lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return True

    # --- справочники
    def init(self, force=False):
        od = self.od
        cache = {} if force else mb.load_cache()
        fresh = False
        if cache.get("saved"):
            age = datetime.datetime.now() - datetime.datetime.fromisoformat(cache["saved"])
            fresh = age.days < mb.CACHE_DAYS

        def missing():
            m = [x for x in mb.REQUIRED if x not in od.sets]
            if not any(r in od.sets for r in mb.STOCK_REGS):
                m.append("регистр остатков")
            return m

        cached_meta = False
        if fresh and cache.get("meta"):
            od.props = {k: set(v) for k, v in cache["meta"]["props"].items()}
            od.sets = set(cache["meta"]["sets"])
            cached_meta = not missing()
        if not cached_meta:
            od.props, od.sets = {}, set()
            od.load_meta()
            cache.pop("noms", None)
        if missing():
            raise RuntimeError("В OData-публикации 1С не включены: " + ", ".join(missing()))

        whs = od.get("Catalog_Склады", {"$select": "Ref_Key,Description,IsFolder,DeletionMark"})
        orgs = od.get("Catalog_Организации", {"$select": "Ref_Key,Description,DeletionMark"})
        pts = od.get("Catalog_ВидыЦен", {"$select": "Ref_Key,Description,DeletionMark"})

        noms, source = None, ""
        if fresh and cache.get("noms") is not None and cache.get("v") == mb.CACHE_VERSION:
            try:
                if od.count("Catalog_Номенклатура") == cache.get("count"):
                    noms, source = cache["noms"], "из сохранённого файла"
            except Exception:
                noms, source = cache["noms"], "из сохранённого файла"
        if noms is None:
            noms = od.get_all("Catalog_Номенклатура",
                              {"$select": "Ref_Key,Description,IsFolder,DeletionMark"},
                              label="Номенклатура", order="Ref_Key")
            source = "загружена из 1С"
            mb.save_cache({"saved": datetime.datetime.now().replace(microsecond=0).isoformat(),
                           "v": mb.CACHE_VERSION, "count": len(noms), "noms": noms,
                           "meta": {"props": {k: sorted(v) for k, v in od.props.items()},
                                    "sets": sorted(od.sets)}})

        self.warehouses = {w["Description"]: w["Ref_Key"] for w in whs
                           if not w.get("IsFolder") and not w.get("DeletionMark")}
        orgs = [o for o in orgs if not o.get("DeletionMark")]
        f = [o for o in orgs if mb.norm(o["Description"]) == mb.norm(config.ORGANIZATION_FROM)]
        t = [o for o in orgs if mb.norm(config.ORGANIZATION_TO) in mb.norm(o["Description"])]
        pt = [p for p in pts if not p.get("DeletionMark")
              and mb.norm(p["Description"]) == mb.norm(config.PRICE_TYPE)]
        if len(f) != 1 or len(t) != 1 or not pt:
            raise RuntimeError("Проверьте ORGANIZATION_FROM / ORGANIZATION_TO / PRICE_TYPE в config.py")
        self.org_from = (f[0]["Ref_Key"], f[0]["Description"])
        self.org_to = (t[0]["Ref_Key"], t[0]["Description"])
        self.price_type = pt[0]["Ref_Key"]

        self.noms, self.pairs, self.mg_keys, self.mg_by_name = {}, {}, set(), {}
        self.mapping = mb.load_mapping()
        for n in noms:
            if n.get("IsFolder") or n.get("DeletionMark"):
                continue
            name = n.get("Description") or ""
            nn = mb.norm(name)
            if mb.SUFFIX_RE.search(nn):
                self.pairs[mb.mg_base(nn)] = n["Ref_Key"]
                self.mg_keys.add(n["Ref_Key"])
                self.mg_by_name[nn] = n["Ref_Key"]
            else:
                self.noms[n["Ref_Key"]] = name

        s = self.settings
        if s.get("src_key") not in self.warehouses.values():
            src = next((n for n in sorted(self.warehouses) if "общ" in mb.norm(n)), None)
            if src:
                s["src_name"], s["src_key"] = src, self.warehouses[src]
        if s.get("dst_key") not in self.warehouses.values():
            stores = self.store_names()
            dst = next((n for n in stores if "ленин" in mb.norm(n)), stores[0] if stores else None)
            if dst:
                s["dst_name"], s["dst_key"] = dst, self.warehouses[dst]
        mb.save_settings(s)
        self.ready, self.error = True, ""
        self.say(f"Номенклатура {source}: карточек {config.SUFFIX} — {len(self.mg_keys)}.")

    # --- остатки и цены
    def refresh(self, force_prices=False):
        self._force_prices = force_prices
        stock, prices, extra = self.load_stock_and_prices()
        for n in extra:
            nn = mb.norm(n.get("Description"))
            if mb.SUFFIX_RE.search(nn):
                self.pairs[mb.mg_base(nn)] = n["Ref_Key"]
                self.mg_keys.add(n["Ref_Key"])
                self.mg_by_name[nn] = n["Ref_Key"]
            else:
                self.noms[n["Ref_Key"]] = n.get("Description") or ""
        self.last = (stock, prices)
        self.build_items()
        if getattr(self, "price_src", ""):
            self.say(self.price_src)
        self.say(f"Остатки обновлены: доступно к передаче {len(self.items)} позиций.")

    def build_items(self):
        stock, prices = self.last
        src, dst = self.settings["src_key"], self.settings["dst_key"]
        items = []
        for key, name in self.noms.items():
            st = stock.get((key, src))
            if not st or st[0] <= 0:
                continue
            pair = self.find_pair(name)
            if not pair:
                continue
            items.append({"key": key, "name": name, "pair": pair, "price": prices.get(key),
                          "free": st[1], "store": stock.get((pair, dst), [0.0, 0.0])[0]})
        items.sort(key=lambda x: mb.norm(x["name"]))
        self.items = items

    def set_dst(self, key):
        name = next((n for n, k in self.warehouses.items() if k == key), None)
        if name and key != self.settings.get("dst_key"):
            self.settings["dst_name"], self.settings["dst_key"] = name, key
            mb.save_settings(self.settings)
            if getattr(self, "last", None):
                self.build_items()

    def move(self, lines_in):
        by_key = {i["key"]: i for i in self.items}
        lines = []
        for l in lines_in:
            it = by_key.get(l.get("key"))
            qty = float(l.get("qty") or 0)
            if it and qty > 0:
                lines.append((it["key"], it["pair"], qty, it["name"]))
        if not lines:
            raise RuntimeError("Ничего не выбрано.")
        self.result = None
        res = self.move_work(lines)
        if res.get("problems"):
            self.result = {"ok": False, "text": "Ничего не создано:\n" + "\n".join(res["problems"])}
        else:
            self.result = {"ok": True, "text": f"Готово: {res['count']} поз. на "
                           f"{mb.money(res['total'])} ₽\n" +
                           "\n".join(f"{t} № {n} — проведён" for t, n in res["done"])}
        self.say(self.result["text"].replace("\n", "; "))
        self.refresh()


for _n in Core.BORROW:
    setattr(Core, _n, mb.App.__dict__[_n])

core = Core()

# ====================================================================== веб
app = Flask(__name__)


def auth(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not PASSWORD:
            return Response("В config.py не задан WEB_PASSWORD — страница закрыта.", 503)
        h = request.headers.get("Authorization", "")
        ok = False
        if h.startswith("Basic "):
            try:
                ok = base64.b64decode(h[6:]).decode("utf-8").split(":", 1)[1] == PASSWORD
            except Exception:
                ok = False
        if not ok:
            return Response("Нужен пароль", 401, {"WWW-Authenticate": 'Basic realm="move_bot"'})
        return fn(*a, **kw)
    return wrap


@app.get("/")
@auth
def index():
    with open(os.path.join(HERE, "web_page.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.get("/api/state")
@auth
def state():
    s = core.settings
    stores = [{"key": core.warehouses[n], "name": n} for n in core.store_names()
              if n in core.warehouses] if core.ready else []
    return jsonify({
        "version": version(), "ready": core.ready, "busy": core.busy, "status": core.status,
        "error": core.error, "messages": core.messages[-60:], "result": core.result,
        "src": s.get("src_name"), "dst": s.get("dst_key"), "stores": stores,
        "org": f"{core.org_from[1]} → {core.org_to[1]}" if core.org_from else "",
        "items": core.items,
    })


@app.post("/api/refresh")
@auth
def api_refresh():
    d = request.get_json(silent=True) or {}
    if d.get("dst"):
        core.set_dst(d["dst"])
    ok = core.run("Загружаю остатки и цены…", lambda: core.refresh(bool(d.get("force_prices"))))
    return jsonify({"started": ok})


@app.post("/api/dst")
@auth
def api_dst():
    d = request.get_json(silent=True) or {}
    if core.busy:
        return jsonify({"ok": False, "msg": "Подождите окончания загрузки"})
    core.set_dst(d.get("dst"))
    return jsonify({"ok": True})


@app.post("/api/move")
@auth
def api_move():
    d = request.get_json(silent=True) or {}
    if d.get("dst") and not core.busy:
        core.set_dst(d["dst"])
    ok = core.run("Проверяю остатки и создаю документы…", lambda: core.move(d.get("lines") or []))
    return jsonify({"started": ok})


@app.post("/api/reload_noms")
@auth
def api_reload_noms():
    ok = core.run("Загружаю номенклатуру из 1С заново…", lambda: (core.init(force=True), core.refresh()))
    return jsonify({"started": ok})


@app.get("/api/log")
@auth
def api_log():
    try:
        with open(mb.LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-400:]
    except Exception:
        lines = []
    return Response("".join(lines), mimetype="text/plain; charset=utf-8")


def _exit_later(code):
    def go():
        time.sleep(1.5)
        os._exit(code)
    threading.Thread(target=go, daemon=True).start()


@app.post("/api/restart")
@auth
def api_restart():
    """Перезапуск: server_run.bat сам проверит обновления на GitHub и запустит бота снова."""
    if core.busy:
        return jsonify({"ok": False, "msg": "Бот занят — дождитесь окончания операции"})
    mb.log("Перезапуск со страницы управления")
    _exit_later(0)
    return jsonify({"ok": True})


@app.post("/api/reinstall")
@auth
def api_reinstall():
    """Переустановка: заново скачать все файлы с GitHub (настройки и кэш сохраняются)."""
    if core.busy:
        return jsonify({"ok": False, "msg": "Бот занят — дождитесь окончания операции"})
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write("0")
    mb.log("Переустановка со страницы управления")
    _exit_later(0)
    return jsonify({"ok": True})


def find_odata_url():
    """IIS может быть привязан к конкретному адресу сервера, и 127.0.0.1 тогда отвечает 404.
    Пробуем адрес из config.py, затем имя и все адреса этого компьютера."""
    base = config.ODATA_URL.rstrip("/")
    u = urllib.parse.urlsplit(base)
    hosts = [u.hostname]
    try:
        name = socket.gethostname()
        hosts += [name] + socket.gethostbyname_ex(name)[2]
    except Exception:
        pass
    for h in ["server1c"] + hosts:
        if h not in hosts:
            hosts.append(h)
    seen = []
    for h in hosts:
        if not h or h in seen:
            continue
        seen.append(h)
        url = f"{u.scheme}://{h}{':' + str(u.port) if u.port else ''}{u.path}"
        try:
            r = core.od.s.get(url + "/odata/standard.odata/", timeout=15)
            if r.status_code in (200, 401):
                if url != base:
                    mb.log(f"1С отвечает по адресу {url} (адрес из config.py {base} не подошёл)")
                return url
            mb.log(f"Адрес {url}: HTTP {r.status_code}")
        except Exception as e:
            mb.log(f"Адрес {url}: нет ответа ({e})")
    return base


def fix_task():
    """Снимает ограничение «останавливать задачу через 3 дня» с задачи автозапуска."""
    if os.name != "nt":
        return
    ps = ("$t=Get-ScheduledTask -TaskName 'move_bot_web' -ErrorAction SilentlyContinue; "
          "if($t -and $t.Settings.ExecutionTimeLimit -ne 'PT0S'){"
          "$t.Settings.ExecutionTimeLimit='PT0S'; Set-ScheduledTask -InputObject $t | Out-Null}")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=60,
                       capture_output=True)
    except Exception as e:
        mb.log("Настройка задачи автозапуска не изменена: " + str(e))


def boot():
    def go():
        url = find_odata_url()
        core.od.base = url.rstrip("/") + "/odata/standard.odata/"
        core.init()
        core.refresh()
    core.run("Подключаюсь к 1С и загружаю справочники…", go)


def self_update():
    """Проверка обновлений при каждом запуске (нужно, если бот запускает панель ботов,
    а не server_run.bat). Если что-то обновилось — перезапускаемся уже с новыми файлами."""
    if os.environ.get("MOVE_BOT_UPDATED"):
        return
    before = version()
    try:
        import update
        update.main()
    except Exception as e:
        mb.log("Проверка обновлений не удалась: " + str(e))
        return
    if version() != before:
        mb.log(f"Установлено обновление {before} -> {version()}, перезапуск")
        os.environ["MOVE_BOT_UPDATED"] = "1"
        import sys
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    self_update()
    mb.log(f"Веб-бот запущен, версия {version()}, порт {PORT}")
    threading.Thread(target=fix_task, daemon=True).start()
    boot()
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
