# -*- coding: utf-8 -*-
"""
Тест нейросетей на ваших фото — чтобы выбрать, какая лучше читает накладные.

Положите фото в папку test_photos так:
  test_photos\\накладная1\\стр1.jpg, стр2.jpg      (одна папка = одна накладная, все её страницы)
  test_photos\\накладная2.jpg                       (или просто файл, если страница одна)
Запуск: test_models.bat. Результат — на экране и в файле test_result.txt.
К 1С не подключается, в 1С ничего не пишет.
"""
import math
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import requests  # noqa: E402

import config  # noqa: E402
import recognize as R  # noqa: E402

R.OWN_INNS = set(getattr(config, "OWN_INNS", []))

IMG = (".jpg", ".jpeg", ".png", ".webp", ".heic")
out_lines = []


ANSI = re.compile(r"\033\[[0-9;]*m")


def money(x):
    return f"{x:,.2f}".replace(",", " ")


def p(s=""):
    print(s)
    out_lines.append(ANSI.sub("", s))      # в файл — без цветовых кодов


def list_models():
    m = next((x for x in config.MODELS if x.get("type", "openai") == "openai"), None)
    if not m or "ЗАМЕНИТЬ" in m.get("base_url", "") or not m.get("api_key"):
        print("Агрегатор (vsellm) ещё не настроен: в config.py заполните VSELLM_URL и VSELLM_KEY.\n"
              "Чтобы проверить GigaChat, запустите test_models.bat ещё раз и выберите пункт 1.")
        return
    try:
        r = requests.get(m["base_url"].rstrip("/") + "/models",
                         headers={"Authorization": "Bearer " + m["api_key"]}, timeout=60)
    except requests.RequestException as e:
        print(f"Не удалось связаться с {m['base_url']}: {e}\nПроверьте адрес VSELLM_URL в config.py.")
        return
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:500]}")
        return
    ids = sorted(x.get("id", "") for x in r.json().get("data", []))
    for word in ("claude", "gemini", "gpt", "qwen"):
        hit = [i for i in ids if word in i.lower()]
        if hit:
            print(f"\n--- {word} ---")
            print("\n".join(hit))
    print(f"\nВсего моделей: {len(ids)}. Скопируйте нужное имя в поле \"model\" в config.py")


def docs():
    root = os.path.join(HERE, "test_photos")
    if not os.path.isdir(root):
        os.makedirs(root)
        print("Создана папка test_photos — положите туда фото накладных и запустите снова.")
        return []
    res = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if os.path.isdir(path):
            files = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.lower().endswith(IMG)]
            if files:
                res.append((name, files))
        elif name.lower().endswith(IMG):
            res.append((name, [path]))
    return res


# цвета в консоли Windows (Windows Terminal и cmd на Windows 10/11 их понимают)
if os.name == "nt":
    os.system("")
G, Y, R_, C, D, B, X = ("\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[90m", "\033[1m", "\033[0m")
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Progress:
    """Живая строка: общий ползунок, какая модель работает и сколько секунд прошло."""
    def __init__(self, total):
        self.total, self.done, self.label, self.t0 = total, 0, "", time.time()
        self._stop = threading.Event()
        self._th = None

    def _line(self):
        w = 24
        el = time.time() - self.t0
        part = min(0.95, 1 - math.exp(-el / 45))          # оценка внутри текущей проверки
        frac = (self.done + part) / self.total if self.total else 1
        fill = int(w * frac)
        spin = SPIN[int(el * 8) % len(SPIN)]
        return (f"\r  {C}{'█' * fill}{D}{'░' * (w - fill)}{X} {B}{int(frac * 100):3d}%{X}  "
                f"{C}{spin}{X} {self.label} {D}{int(el)} с{X}   ")

    def _run(self):
        while not self._stop.wait(0.12):
            sys.stdout.write(self._line())
            sys.stdout.flush()

    def start(self, label):
        self.label, self.t0 = label, time.time()
        self._stop.clear()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join()
        self.done += 1
        sys.stdout.write("\r" + " " * 110 + "\r")      # стираем живую строку перед результатом
        sys.stdout.flush()


def main():
    if len(sys.argv) == 1:
        print("1 - проверить нейросети на фото из папки test_photos")
        print("2 - показать список моделей агрегатора")
        if input("Введите 1 или 2 и нажмите Enter: ").strip() == "2":
            sys.argv.append("list")
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        list_models()
        return
    models = [m for m in config.MODELS if m.get("enabled", True)
              and "ЗАМЕНИТЬ" not in m.get("base_url", "") + m.get("model", "") and m.get("api_key")]
    if not models:
        print("Нет ни одной настроенной нейросети: в config.py у нужной модели заполните ключ и \"enabled\": True.")
        return
    found = docs()
    if not found:
        return
    stats = {m["name"]: [0, 0, 0.0] for m in models}       # сошлось, всего, секунд
    total = len(found) * len(models)
    p(f"Накладных: {len(found)}, нейросетей: {len(models)} — всего проверок: {total}. "
      f"Каждая занимает 20–90 секунд.")
    bar = Progress(total)
    for name, files in found:
        p(f"\n{B}{C}▌ {name}{X} {D}({len(files)} фото){X}")
        raws = [open(f, "rb").read() for f in files]
        images = [R.prepare_image(b) for b in raws]
        for m in models:
            t0 = time.time()
            bar.start(f"{m['name']} читает «{name[:30]}»…")
            try:
                d = R.ask_model(m, images, raws)
            except Exception as e:
                bar.stop()
                p(f"  {R_}✖ {m['name']:<18}{X} ошибка: {str(e)[:200]}")
                stats[m["name"]][1] += 1
                continue
            bar.stop()
            sec = time.time() - t0
            chk = R.check(d)
            ok = chk["total_ok"] and not chk["bad_lines"] and not chk["inn_bad"]
            st = stats[m["name"]]
            st[0] += 1 if ok else 0
            st[1] += 1
            st[2] += sec
            mark = f"{G}✔ {m['name']:<18}{X} {G}всё сошлось{X}" if ok else f"{Y}⚠ {m['name']:<18}{X} {Y}есть замечания{X}"
            tot = d["total"].get("sum")
            p(f"  {mark}  {D}{sec:.0f} с{X}")
            p(f"     № {d.get('number')} от {d.get('date')} · ИНН {d['supplier'].get('inn') or '-'}"
              f"{f' {R_}(ошибка в ИНН){X}' if chk['inn_bad'] else ''} · итог {money(tot) if tot else '-'}")
            for x in d["lines"]:
                col = Y if x["n"] in chk["bad_lines"] else D
                p(f"     {col}{x['n']:>2}. {x['name'][:42]:<42} {x['qty']:>6g} × {money(x['price']):>10} = {money(x['sum']):>12}{X}")
            for x in chk["issues"][:6]:
                p(f"     {Y}• {x}{X}")
    p(f"\n{B}━━━━━━━━━━━━━━━━ ИТОГ ━━━━━━━━━━━━━━━━{X}")
    for n, (ok, tot, sec) in sorted(stats.items(), key=lambda kv: -kv[1][0]):
        if tot:
            col = G if ok == tot else (Y if ok else R_)
            p(f"  {col}{'●' * ok}{D}{'○' * (tot - ok)}{X}  {n:<18} без ошибок {col}{ok} из {tot}{X}"
              f"{D}, в среднем {sec / max(tot, 1):.0f} с{X}")
    p("\nПервой в списке MODELS в config.py поставьте ту, у которой больше «без ошибок».")
    with open(os.path.join(HERE, "test_result.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))


if __name__ == "__main__":
    main()
