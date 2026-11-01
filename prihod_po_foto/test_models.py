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

R.OWN_INNS = set(getattr(config, "OWN_INNS", []))

IMG = (".jpg", ".jpeg", ".png", ".webp", ".heic")
out_lines = []


def p(s=""):
    print(s)
    out_lines.append(s)


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
    for name, files in found:
        p(f"\n================ {name} ({len(files)} фото)")
        raws = [open(f, "rb").read() for f in files]
        images = [R.prepare_image(b) for b in raws]
        for m in models:
            t0 = time.time()
            try:
                d = R.ask_model(m, images, raws)
            except Exception as e:
                p(f"  {m['name']:<16} ОШИБКА: {str(e)[:200]}")
                stats[m["name"]][1] += 1
                continue
            sec = time.time() - t0
            chk = R.check(d)
            ok = chk["total_ok"] and not chk["bad_lines"] and not chk["inn_bad"]
            st = stats[m["name"]]
            st[0] += 1 if ok else 0
            st[1] += 1
            st[2] += sec
            p(f"  {m['name']:<16} {'✔ СОШЛОСЬ' if ok else '✖ есть ошибки'}  строк {len(d['lines'])}, "
              f"итог {d['total'].get('sum')}, ИНН {d['supplier'].get('inn') or '-'}"
              f"{'' if not chk['inn_bad'] else ' (ОШИБКА В ИНН)'}, №{d.get('number')} от {d.get('date')}, {sec:.0f} с")
            for x in d["lines"]:
                p(f"       {x['n']}. {x['name'][:45]:<45} {x['qty']:g} × {x['price']:.2f} = {x['sum']:.2f}")
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
