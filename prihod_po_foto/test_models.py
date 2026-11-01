# -*- coding: utf-8 -*-
"""
Тест нейросетей на ваших фото — чтобы выбрать, какая лучше читает накладные.

Положите фото в папку test_photos так:
  test_photos\\накладная1\\стр1.jpg, стр2.jpg      (одна папка = одна накладная, все её страницы)
  test_photos\\накладная2.jpg                       (или просто файл, если страница одна)
Запуск: test_models.bat. Результат — на экране и в файле test_result.txt.
К 1С не подключается, в 1С ничего не пишет.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import requests  # noqa: E402

import config  # noqa: E402
import recognize as R  # noqa: E402

IMG = (".jpg", ".jpeg", ".png", ".webp", ".heic")
out_lines = []


def p(s=""):
    print(s)
    out_lines.append(s)


def list_models():
    m = next((x for x in config.MODELS if x.get("type", "openai") == "openai"), None)
    if not m:
        print("В MODELS нет агрегатора")
        return
    r = requests.get(m["base_url"].rstrip("/") + "/models", headers={"Authorization": "Bearer " + m["api_key"]},
                     timeout=60)
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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        list_models()
        return
    models = [m for m in config.MODELS if m.get("enabled", True)]
    found = docs()
    if not found:
        return
    stats = {m["name"]: [0, 0, 0.0] for m in models}       # сошлось, всего, секунд
    for name, files in found:
        p(f"\n================ {name} ({len(files)} фото)")
        images = [R.prepare_image(open(f, "rb").read()) for f in files]
        for m in models:
            t0 = time.time()
            try:
                d = R.ask_model(m, images)
            except Exception as e:
                p(f"  {m['name']:<16} ОШИБКА: {str(e)[:200]}")
                stats[m["name"]][1] += 1
                continue
            sec = time.time() - t0
            chk = R.check(d)
            ok = chk["total_ok"] and not chk["bad_lines"]
            st = stats[m["name"]]
            st[0] += 1 if ok else 0
            st[1] += 1
            st[2] += sec
            p(f"  {m['name']:<16} {'✔ СОШЛОСЬ' if ok else '✖ есть ошибки'}  строк {len(d['lines'])}, "
              f"итог {d['total'].get('sum')}, ИНН {d['supplier'].get('inn') or '-'}, {sec:.0f} с")
            for x in chk["issues"][:6]:
                p(f"       - {x}")
    p("\n================ ИТОГ")
    for n, (ok, tot, sec) in stats.items():
        if tot:
            p(f"  {n:<16} без ошибок {ok} из {tot}, в среднем {sec / max(tot, 1):.0f} с")
    p("\nПервой в списке MODELS в config.py поставьте ту, у которой больше «без ошибок».")
    with open(os.path.join(HERE, "test_result.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))


if __name__ == "__main__":
    main()
