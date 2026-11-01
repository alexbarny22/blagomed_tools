"use strict";
// ============================================================ утилиты
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const rub = x => (Number(x) || 0).toFixed(2).replace(".", ",") + " ₽";
const el = html => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const FONTS = ["Montserrat", "Manrope", "Golos Text", "PT Serif"];
const KIND_TITLES = {cover: "Обложка", problem: "Проблема и решение", specs: "Характеристики и размеры", usage: "Как использовать", trust: "Комплектация и уход"};
const STATUS = {
  planning: ["Пишу сценарий", "work"], writing: ["Переписываю тексты", "work"], plan_ready: ["Ждёт утверждения", "warn"],
  generating: ["Рисую слайды", "work"], paused_budget: ["Пауза: потолок", "warn"], done: ["Готово", "done"],
  error: ["Ошибка", "err"], interrupted: ["Прервано", "err"],
};
let META = null;

function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
async function api(url, opts = {}) {
  if (opts.json !== undefined) { opts.method = opts.method || "POST"; opts.headers = {"Content-Type": "application/json"}; opts.body = JSON.stringify(opts.json); }
  let r;
  try { r = await fetch(url, opts); } catch (e) { toast("Программа не отвечает — окно с программой закрыто?"); throw e; }
  const d = await r.json().catch(() => ({ok: false, error: "Ошибка сервера " + r.status}));
  if (!d.ok) { toast(d.error || "Ошибка"); throw new Error(d.error); }
  return d;
}
async function loadMeta() { META = await api("/api/meta"); return META; }
function modal(html, wide = false) {
  const m = $("#modal"), b = $("#modal-box");
  b.className = "modal-box" + (wide ? " wide" : ""); b.innerHTML = html; m.classList.remove("hidden");
  return b;
}
function closeModal() { $("#modal").classList.add("hidden"); $("#modal-box").innerHTML = ""; }
$("#modal").addEventListener("click", e => { if (e.target.id === "modal" && !$("#modal-box").dataset.sticky) closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#modal-box").dataset.sticky) closeModal(); });
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); }
  catch { const a = document.createElement("textarea"); a.value = t; document.body.appendChild(a); a.select(); document.execCommand("copy"); a.remove(); }
  toast("Скопировано");
}
function priceOf(model) { return Number((META.settings.image_prices || {})[model] ?? 8); }
function estimate(profile, covers) {
  const st = META.settings, models = st.profiles[profile] || [];
  if (!models.length) return 0;
  let img = 0;
  for (let i = 0; i < covers; i++) img += priceOf(models[i % models.length]);
  img += 4 * priceOf(models[0]);
  const pt = (st.profile_text || {})[profile] || {};
  const tp = m => (st.text_prices || {})[m] || [221, 1105];
  const sp = tp(pt.script || st.script_model), cp = tp(pt.check || st.check_model);
  const text = (16000 * sp[0] + 9000 * sp[1]) / 1e6;
  const checks = st.check_enabled ? (covers + 4) * (3000 * cp[0] + 300 * cp[1]) / 1e6 : 0;
  return img + text + checks;
}
function profileLimit(profile) { return ((META.settings.profile_text || {})[profile] || {}).limit; }

// ============================================================ маршруты
let poller = null;
async function route() {
  clearInterval(poller); poller = null; closeModal();
  const h = location.hash || "#/new";
  $$("nav a").forEach(a => a.classList.toggle("on", h.startsWith("#/" + a.dataset.nav)));
  const v = $("#view");
  try { await loadMeta(); } catch { v.innerHTML = '<div class="empty">Программа не отвечает. Запустите start.bat.</div>'; return; }
  if (h.startsWith("#/p/")) return viewProject(v, h.slice(4));
  if (h.startsWith("#/history")) return viewHistory(v);
  if (h.startsWith("#/settings")) return viewSettings(v);
  return viewNew(v);
}
window.addEventListener("hashchange", route);
route();

// ============================================================ новая серия
const form = {photos: [], refs: [], mode: "auto", styleName: "", covers: 1, profile: null, limit: null, text: "", wish: "", styleText: ""};

function dropZone(box, key, hint) {
  const drop = $(".drop", box), input = $("input[type=file]", box), thumbs = $(".thumbs", box);
  const redraw = () => {
    thumbs.innerHTML = "";
    form[key].forEach((f, i) => {
      const t = el(`<div class="thumb"><img><button title="Убрать">×</button></div>`);
      $("img", t).src = URL.createObjectURL(f);
      $("button", t).onclick = () => { form[key].splice(i, 1); redraw(); };
      thumbs.appendChild(t);
    });
  };
  const add = files => { for (const f of files) if (/\.(jpe?g|png|webp)$/i.test(f.name)) form[key].push(f); redraw(); };
  drop.onclick = () => input.click();
  input.onchange = () => { add(input.files); input.value = ""; };
  drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); add(e.dataTransfer.files); };
  redraw();
}

function styleCard(s, on) {
  return `<div class="style-card ${on ? "on" : ""}" data-name="${esc(s.name)}">
    <div class="sw"><i style="background:${esc(s.plate)}"></i><i style="background:${esc(s.ink)}"></i><i style="background:${esc(s.accent)}"></i></div>
    <div class="nm" style="font-family:'${esc(s.font)}',sans-serif">${esc(s.name)}</div>
    <div class="why">${esc(s.why || "")}${s.builtin ? "" : " · ваш"}</div></div>`;
}

function viewNew(v) {
  const s = META.settings;
  if (!form.profile || !s.profiles[form.profile]) form.profile = s.profile in s.profiles ? s.profile : Object.keys(s.profiles)[0];
  if (form.limit === null) form.limit = s.limit_rub;
  const lastStyle = (META.history.find(x => x.style && x.style.name) || {}).style;
  v.innerHTML = `
  <h1>Новая серия</h1>
  <p class="sub">5 слайдов для WB и Ozon + тексты карточки. Сначала вы утвердите сценарий и тексты — только потом рисуем.</p>
  ${META.key_ok ? "" : '<div class="banner err">В config.py не вписан ключ vsellm (VSELLM_KEY). Впишите его и перезапустите start.bat.</div>'}
  <div class="grid2">
    <div class="card" id="c-product">
      <h2>1. Товар</h2>
      <label>Фото товара — 2–4 шт., лучше на белом фоне и с разных сторон</label>
      <div class="drop"><b>Перетащите фото сюда</b> или нажмите, чтобы выбрать<input type="file" accept="image/*" multiple hidden></div>
      <div class="thumbs"></div>
      <label>Описание товара</label>
      <textarea id="f-text" rows="10" placeholder="Название: Чехол для калоприемника с поясом&#10;&#10;Вставьте сюда описание из карточки WB/Ozon, характеристики, размеры.&#10;Очень полезно: несколько отзывов покупателей (ваших или конкурентов) — из них берутся реальные «боли».">${esc(form.text)}</textarea>
    </div>
    <div class="card" id="c-style">
      <h2>2. Стиль</h2>
      <div class="seg" id="f-mode">
        <button data-m="auto">Автоподбор</button><button data-m="refs">Как у конкурентов</button>
        <button data-m="preset">Из библиотеки</button><button data-m="text">Описать словами</button>
      </div>
      <div id="mode-body"></div>
    </div>
  </div>
  <div class="card">
    <h2>3. Параметры</h2>
    <div class="grid3">
      <div><label>Вариантов обложки (для A/B)</label><div class="seg" id="f-covers"><button data-c="1">1</button><button data-c="2">2</button><button data-c="3">3</button></div></div>
      <div><label>Профиль нейросетей</label><select id="f-profile">${Object.keys(s.profiles).map(p => `<option ${p === form.profile ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></div>
      <div><label>Потолок на серию, ₽</label><input type="number" id="f-limit" min="10" step="10" value="${esc(form.limit)}"></div>
    </div>
    <div class="row" style="margin-top:16px">
      <div class="est" id="f-est"></div><div class="spacer"></div>
      <button class="btn primary big" id="f-go">Написать сценарий →</button>
    </div>
  </div>`;
  dropZone($("#c-product"), "photos");
  $("#f-text").oninput = e => form.text = e.target.value;
  const est = () => {
    const e = estimate(form.profile, form.covers), m = META.settings.profiles[form.profile] || [];
    $("#f-est").innerHTML = `Прогноз: <b>≈ ${rub(e)}</b> на серию без перегенераций · ${m.map(x => esc(x.split("/").pop()) + " " + rub(priceOf(x))).join(", ")} за картинку`;
  };
  const setMode = m => {
    form.mode = m;
    $$("#f-mode button").forEach(b => b.classList.toggle("on", b.dataset.m === m));
    const body = $("#mode-body");
    if (m === "auto") body.innerHTML = `<p class="muted" style="margin-top:14px">Сценарист сам подберёт стиль под товар и аудиторию. Перед генерацией вы увидите, какой стиль выбран, и сможете поменять цвета и шрифт.</p>
      <label>Пожелание (не обязательно)</label><input type="text" id="f-wish" value="${esc(form.wish)}" placeholder="например: светлые тона, без людей">`;
    if (m === "refs") {
      body.innerHTML = `<label>Слайды конкурентов, стиль которых нравится (1–3)</label>
        <div class="drop"><b>Перетащите слайды сюда</b> или нажмите<input type="file" accept="image/*" multiple hidden></div><div class="thumbs"></div>
        <label>Пожелание</label><input type="text" id="f-wish" value="${esc(form.wish)}" placeholder="например: хочу как этот, но фон светлее">
        <p class="muted" style="font-size:13px">Заимствуем стиль — палитру, свет, тип фона, шрифт. Чужие фото, тексты и логотипы не копируются.</p>`;
      dropZone(body, "refs");
    }
    if (m === "preset") {
      const all = META.styles.slice();
      if (lastStyle && !all.some(x => x.name === lastStyle.name)) all.unshift({...lastStyle, why: "как в прошлой серии", builtin: false});
      if (!form.styleName && all.length) form.styleName = all[0].name;
      body.innerHTML = `<div class="styles">${all.map(x => styleCard(x, x.name === form.styleName)).join("")}</div>
        <label>Пожелание (не обязательно)</label><input type="text" id="f-wish" value="${esc(form.wish)}">`;
      $$(".style-card", body).forEach(c => c.onclick = () => { form.styleName = c.dataset.name; $$(".style-card", body).forEach(x => x.classList.toggle("on", x === c)); });
    }
    if (m === "text") body.innerHTML = `<label>Опишите стиль словами</label><textarea id="f-styletext" rows="4" placeholder="например: зелёный, спокойный, для пожилых, много воздуха">${esc(form.styleText)}</textarea>`;
    const w = $("#f-wish"); if (w) w.oninput = e => form.wish = e.target.value;
    const st = $("#f-styletext"); if (st) st.oninput = e => form.styleText = e.target.value;
  };
  $$("#f-mode button").forEach(b => b.onclick = () => setMode(b.dataset.m));
  setMode(form.mode);
  const setCovers = c => { form.covers = c; $$("#f-covers button").forEach(b => b.classList.toggle("on", +b.dataset.c === c)); est(); };
  $$("#f-covers button").forEach(b => b.onclick = () => setCovers(+b.dataset.c));
  setCovers(form.covers);
  $("#f-profile").onchange = e => {
    form.profile = e.target.value;
    const pl = profileLimit(form.profile);
    if (pl && Number(form.limit) < pl) { form.limit = pl; $("#f-limit").value = pl; toast(`Для профиля «${form.profile}» потолок поднят до ${pl} ₽`); }
    est();
  };
  $("#f-limit").oninput = e => form.limit = e.target.value;
  $("#f-go").onclick = async () => {
    if (!form.photos.length) return toast("Добавьте фото товара");
    if (form.mode === "refs" && !form.refs.length) return toast("Добавьте слайды конкурентов или выберите другой режим стиля");
    const fd = new FormData();
    form.photos.forEach(f => fd.append("photos", f));
    if (form.mode === "refs") form.refs.forEach(f => fd.append("refs", f));
    fd.append("product_text", form.text); fd.append("wish", form.wish); fd.append("covers", form.covers);
    fd.append("style_mode", form.mode); fd.append("style_name", form.styleName); fd.append("style_text", form.styleText);
    fd.append("profile", form.profile); fd.append("limit", form.limit);
    $("#f-go").disabled = true;
    try {
      const d = await api("/api/projects", {method: "POST", body: fd});
      Object.assign(form, {photos: [], refs: [], text: "", wish: "", styleText: ""});
      location.hash = "#/p/" + d.id;
    } catch { $("#f-go").disabled = false; }
  };
}

// ============================================================ серия
function viewProject(v, pid) {
  let last = {status: null, planShown: false, resultsShown: false, textsKey: ""};
  v.innerHTML = `<div class="phead"><div><h1 id="p-title">…</h1><span class="pill" id="p-status"></span></div><div class="spacer"></div>
    <div class="money card" style="margin:0;padding:12px 16px"><div class="nums"><span>Потрачено <b id="p-spent">0 ₽</b></span><span>потолок <b id="p-limit">—</b></span></div>
    <div class="bar"><i id="p-bar" style="width:0"></i></div></div></div>
    <div id="p-banner"></div>
    <div class="layout"><div class="card"><h2>Ход работы</h2><ul class="feed" id="p-feed"></ul></div><div id="p-main"></div></div>`;
  const tick = async () => {
    let st;
    try { st = (await api("/api/projects/" + pid)).state; } catch { return; }
    $("#p-title").textContent = st.title;
    const [label, cls] = STATUS[st.status] || [st.status, ""];
    const pill = $("#p-status"); pill.textContent = label; pill.className = "pill " + cls;
    $("#p-spent").textContent = rub(st.spent); $("#p-limit").textContent = rub(st.limit);
    const frac = Math.min(1, (st.spent || 0) / (st.limit || 1));
    const bar = $("#p-bar"); bar.style.width = (frac * 100).toFixed(1) + "%";
    bar.style.background = frac < .7 ? "var(--good)" : frac < .9 ? "var(--warn)" : "var(--bad)";
    renderFeed(st.feed || []);
    renderBanner(st);
    const main = $("#p-main");
    if (st.status === "planning" || (st.status === "writing" && !st.plan)) {
      if (last.status !== st.status) main.innerHTML = `<div class="card"><h2>Сценарист работает</h2><p class="muted">Изучает товар, подбирает ключевые запросы и стиль, пишет сценарий 5 слайдов и тексты карточки. Обычно 1–2 минуты.</p>
        <div class="shimmer" style="height:18px;border-radius:8px;margin:10px 0"></div><div class="shimmer" style="height:18px;border-radius:8px;margin:10px 0;width:80%"></div><div class="shimmer" style="height:18px;border-radius:8px;width:60%"></div></div>`;
      last.planShown = false;
    } else if (st.plan && (st.status === "plan_ready" || st.status === "writing" || (!st.slides.length && ["error", "interrupted"].includes(st.status)))) {
      if (!last.planShown || (last.status === "writing" && st.status === "plan_ready")) { renderPlan(main, st); last.planShown = true; }
      $$("#plan-go, #plan-rewrite", main).forEach(b => b.disabled = st.busy);
      last.resultsShown = false;
    } else if (st.slides && st.slides.length) {
      if (!last.resultsShown) { renderResultsShell(main, st); last.resultsShown = true; }
      updateSlides(st);
      const tk = JSON.stringify(st.plan.texts || {});
      if (tk !== last.textsKey && !main.contains(document.activeElement)) { renderTexts(st); last.textsKey = tk; }
      $("#r-open").disabled = false;
      last.planShown = false;
    }
    if (st.status === "paused_budget") showBudget(pid, st); else if ($("#modal-box").dataset.budget) { delete $("#modal-box").dataset.sticky; delete $("#modal-box").dataset.budget; closeModal(); }
    last.status = st.status;
  };
  tick(); poller = setInterval(tick, 1200);
}

function renderFeed(items) {
  const ul = $("#p-feed");
  const icon = k => k === "done" ? "✓" : k === "warn" ? "!" : k === "err" ? "×" : "";
  items.forEach((it, i) => {
    let li = ul.children[i];
    const html = `<span class="ic">${icon(it.kind)}</span><span>${esc(it.text)}</span>${it.cost ? `<span class="cost">${rub(it.cost)}</span>` : ""}`;
    if (!li) { li = el(`<li></li>`); ul.appendChild(li); }
    if (li.dataset.h !== html + it.kind) { li.className = it.kind; li.innerHTML = html; li.dataset.h = html + it.kind; }
  });
  while (ul.children.length > items.length) ul.lastChild.remove();
}

function renderBanner(st) {
  const b = $("#p-banner");
  let html = "";
  if (st.status === "interrupted") html = `<div class="banner warn">Работа была прервана (программу закрыли). <span class="spacer"></span><button class="btn primary" data-act="continue">Продолжить</button></div>`;
  if (st.status === "error") html = `<div class="banner err">Ошибка: ${esc(st.error || "")}<span class="spacer"></span><button class="btn" data-act="continue">Попробовать ещё раз</button></div>`;
  const missing = (st.slides || []).filter(x => !x.final).length;
  if (st.status === "done" && missing && !st.busy) html = `<div class="banner warn">Не нарисовано слайдов: ${missing}. <span class="spacer"></span><button class="btn primary" data-act="continue">Дорисовать недостающие</button></div>`;
  if (b.dataset.h === html) return;
  b.dataset.h = html; b.innerHTML = html;
  const btn = $("[data-act=continue]", b);
  if (btn) btn.onclick = async () => { await api(`/api/projects/${st.id}/continue`, {json: {}}); toast("Продолжаю"); };
}

function showBudget(pid, st) {
  const box = $("#modal-box");
  if (box.dataset.budget) return;
  const b = st.budget || {};
  modal(`<h2>Потолок ${rub(st.limit)} достигнут</h2>
    <p>Следующий шаг — <b>${esc(b.what || "")}</b>, нужно ещё ≈ <b>${rub(b.need)}</b>. Осталось ${rub(b.left)}.</p>
    <div class="row"><button class="btn primary" data-add="50">+50 ₽</button><button class="btn" data-add="100">+100 ₽</button>
    <input type="number" id="b-custom" placeholder="своя сумма" style="width:140px"><button class="btn" id="b-custom-go">Добавить</button></div>
    <p class="muted" style="font-size:13px;margin-top:14px">Или остановите — всё готовое сохранится, недостающее можно дорисовать позже.</p>
    <button class="btn danger" id="b-stop">Остановить и сохранить готовое</button>`);
  box.dataset.sticky = "1"; box.dataset.budget = "1";
  const send = async json => { await api(`/api/projects/${pid}/budget`, {json}); delete box.dataset.sticky; delete box.dataset.budget; closeModal(); };
  $$("[data-add]", box).forEach(x => x.onclick = () => send({action: "add", amount: +x.dataset.add}));
  $("#b-custom-go", box).onclick = () => { const v = +$("#b-custom", box).value; if (v > 0) send({action: "add", amount: v}); };
  $("#b-stop", box).onclick = () => send({action: "stop"});
}

// ---------- утверждение сценария
function chipsEditor(box, list) {
  const draw = () => {
    box.innerHTML = list.map((k, i) => `<span class="chip">${esc(k)}<button data-i="${i}" title="Убрать">×</button></span>`).join("") + `<input class="chip-add" placeholder="+ добавить запрос">`;
    $$("button", box).forEach(b => b.onclick = () => { list.splice(+b.dataset.i, 1); draw(); box.dispatchEvent(new Event("change", {bubbles: true})); });
    const inp = $("input", box);
    inp.onkeydown = e => { if (e.key === "Enter" && inp.value.trim()) { list.push(inp.value.trim()); draw(); $("input", box).focus(); box.dispatchEvent(new Event("change", {bubbles: true})); } };
  };
  draw();
}

function counter(inp, max) {
  const c = el(`<div class="counter"></div>`);
  inp.after(c);
  const upd = () => { const n = inp.value.length; c.textContent = `${n} / ${max}`; c.classList.toggle("over", n > max); };
  inp.addEventListener("input", upd); upd();
}

function iconSelect(cur, attr) {
  return `<select ${attr}>${(META.icons || []).map(i => `<option value="${i.name}" ${i.name === cur ? "selected" : ""}>${esc(i.label)}</option>`).join("")}</select>`;
}
function lookSelect(key, cur, attr, inherit) {
  const opts = (META.look || {})[key] || [];
  return `<select ${attr}>${inherit ? `<option value="">как в серии</option>` : ""}${opts.map(o => `<option value="${o.v}" ${o.v === cur ? "selected" : ""}>${esc(o.t)}</option>`).join("")}</select>`;
}
function coverFields(sl) {
  const f = sl.features || [];
  return `<div class="grid2" style="gap:10px"><div><label>Главное слово (крупно)</label><input type="text" data-c="hero_word" value="${esc(sl.hero_word || "")}"></div>
    <div><label>Уточнение</label><input type="text" data-c="hero_rest" value="${esc(sl.hero_rest || "")}"></div></div>
    <label>Преимущества с иконками (до 4) — пустое название = не показывать</label>
    ${[0, 1, 2, 3].map(i => `<div class="grid3" style="gap:8px;margin-bottom:6px" data-feat="${i}">${iconSelect((f[i] || {}).icon || "check_circle", 'data-ff="icon"')}
      <input type="text" data-ff="title" placeholder="жирно: Свобода" value="${esc((f[i] || {}).title || "")}"><input type="text" data-ff="text" placeholder="пояснение (можно пусто)" value="${esc((f[i] || {}).text || "")}"></div>`).join("")}`;
}
function extraFields(sl) {
  const b = sl.badge || {}, lk = sl.look || {};
  return `<details class="look-box"${sl.kind === "cover" ? " open" : ""}><summary><b>Вид слайда, бейдж, слоган</b></summary>
    <div class="grid3" style="gap:10px">
      <div><label>Заголовок</label>${lookSelect("head", lk.head, 'data-lk="head"', true)}</div>
      <div><label>Фото</label>${lookSelect("photo", lk.photo, 'data-lk="photo"', true)}</div>
      <div><label>Пункты / преимущества</label>${lookSelect("feats", lk.feats, 'data-lk="feats"', true)}</div>
      <div><label>Выравнивание</label>${lookSelect("align", lk.align, 'data-lk="align"', true)}</div>
      <div><label>Подзаголовок</label>${lookSelect("sub", lk.sub, 'data-lk="sub"', true)}</div>
      <div><label>Форма бейджа</label>${lookSelect("badge", lk.badge, 'data-lk="badge"', true)}</div>
    </div>
    <div class="grid3" style="gap:10px">
      <div><label>Слово акцентом (для «двухцветного»)</label><input type="text" data-x="accent_word" value="${esc(sl.accent_word || "")}"></div>
      <div><label>Метка-табличка</label><input type="text" data-x="tag" placeholder="например РАЗМЕРЫ" value="${esc(sl.tag || "")}"></div>
      <div><label>Слоган внизу</label><input type="text" data-x="footer" value="${esc(sl.footer || "")}"></div>
    </div>
    <label>Бейдж — только настоящая цифра или факт (пусто = без бейджа)</label>
    <div class="grid3" style="gap:8px">${iconSelect(b.icon || "check_circle", 'data-bd="icon"')}<input type="text" data-bd="value" placeholder="2500 / объём больше" value="${esc(b.value || "")}"><input type="text" data-bd="unit" placeholder="мл" value="${esc(b.unit || "")}"></div>
    <div class="grid2" style="gap:8px;margin-top:6px"><input type="text" data-bd="top" placeholder="текст по верхней дуге (для «по дуге»)" value="${esc(b.top || "")}"><input type="text" data-bd="bottom" placeholder="текст по нижней дуге" value="${esc(b.bottom || "")}"></div>
  </details>`;
}
function readCover(box) {
  const o = {};
  $$("[data-c]", box).forEach(x => o[x.dataset.c] = x.value);
  if ($("[data-feat]", box)) o.features = $$("[data-feat]", box).map(r => ({icon: $("[data-ff=icon]", r).value, title: $("[data-ff=title]", r).value.trim(), text: $("[data-ff=text]", r).value.trim()})).filter(f => f.title);
  return o;
}
function readExtras(box) {
  const o = {look: {}};
  $$("[data-lk]", box).forEach(x => { if (x.value) o.look[x.dataset.lk] = x.value; });
  $$("[data-x]", box).forEach(x => o[x.dataset.x] = x.value);
  const v = ($("[data-bd=value]", box) || {}).value || "";
  o.badge = v.trim() ? {icon: $("[data-bd=icon]", box).value, value: v.trim(), unit: $("[data-bd=unit]", box).value.trim(),
    top: $("[data-bd=top]", box).value.trim(), bottom: $("[data-bd=bottom]", box).value.trim()} : null;
  return o;
}

function styleEditor(style) {
  const ty = style.typo || {fill: "gradient", colors: [style.accent, style.ink]};
  const tc = ty.colors || [style.accent, style.ink];
  const lk = style.look || {};
  const color = (k, n, v) => `<div><label>${n}</label><div class="color-inp"><input type="color" ${k} value="${esc(v || "#000000")}"><span class="muted">${esc(v || "")}</span></div></div>`;
  return `<div class="grid3" style="gap:10px">
    <div><label>Шрифт</label><select data-st="font">${FONTS.map(f => `<option ${f === style.font ? "selected" : ""}>${f}</option>`).join("")}</select></div>
    <div><label>Заливка заголовков</label><select data-ty="fill"><option value="gradient" ${ty.fill !== "solid" ? "selected" : ""}>Градиент</option><option value="solid" ${ty.fill === "solid" ? "selected" : ""}>Сплошная</option></select></div>
    <div><label>Регистр заголовков</label>${lookSelect("case", lk.case || "sentence", 'data-sl="case"')}</div>
    <div><label>Выравнивание</label>${lookSelect("align", lk.align || "left", 'data-sl="align"')}</div>
    <div><label>Подзаголовок</label>${lookSelect("sub", lk.sub || "plain", 'data-sl="sub"')}</div>
    <div><label>Иконки преимуществ</label>${lookSelect("feat_style", lk.feat_style || "filled", 'data-sl="feat_style"')}</div>
    <div><label>Форма бейджа</label>${lookSelect("badge", lk.badge || "circle", 'data-sl="badge"')}</div>
    <div><label>Размеры</label>${lookSelect("dims", lk.dims || "pill", 'data-sl="dims"')}</div>
    <div><label>Разрядка букв</label><select data-sl="tracking">${[0, 4, 8, 12].map(v => `<option value="${v}" ${Number(lk.tracking || 0) === v ? "selected" : ""}>${v ? "+" + v : "нет"}</option>`).join("")}</select></div>
  </div>
  <div class="row" style="gap:18px;margin:10px 0 2px">
    ${[["italic", "Курсив"], ["stroke", "Обводка"], ["shadow", "Тень"]].map(([k, n]) => `<label style="margin:0"><input type="checkbox" data-sb="${k}" ${lk[k] ? "checked" : ""}> ${n}</label>`).join("")}
  </div>
  <div class="grid3" style="gap:10px">
    ${color('data-tc="0"', "Градиент: верх", tc[0])}${color('data-tc="1"', "Градиент: низ", tc[1])}${color('data-st="ink"', "Текст", style.ink)}
    ${color('data-st="accent"', "Акцент", style.accent)}${color('data-st="plate"', "Плашки и фон", style.plate)}${color('data-st="badge_color"', "Бейдж", style.badge_color || style.accent)}
  </div>`;
}
function readStyle(box) {
  const o = {look: {}};
  $$("[data-st]", box).forEach(x => o[x.dataset.st] = x.value);
  o.typo = {colors: $$("[data-tc]", box).map(x => x.value), fill: ($("[data-ty=fill]", box) || {}).value};
  $$("[data-sl]", box).forEach(x => o.look[x.dataset.sl] = x.dataset.sl === "tracking" ? Number(x.value) : x.value);
  $$("[data-sb]", box).forEach(x => o.look[x.dataset.sb] = x.checked);
  return o;
}

function renderPlan(main, st) {
  const plan = st.plan, s = META.settings, t = plan.texts || {};
  const kw = {main: [...plan.keywords.main], extra: [...plan.keywords.extra], synonyms: [...plan.keywords.synonyms]};
  const slideBox = (sl, i) => `<div class="plan-slide" data-i="${i}"><div class="row"><span class="n">${i + 1}</span><b>${KIND_TITLES[sl.kind]}</b></div>
      <label>Заголовок</label><input type="text" data-f="headline" value="${esc(sl.headline)}">
      ${sl.kind !== "specs" && sl.kind !== "trust" ? `<label>Подзаголовок</label><input type="text" data-f="subtitle" value="${esc(sl.subtitle)}">` : ""}
      ${["problem", "specs", "trust"].includes(sl.kind) ? `<label>Пункты — каждый с новой строки</label><textarea data-f="bullets" rows="3">${esc((sl.bullets || []).join("\n"))}</textarea>` : ""}
      ${sl.kind === "specs" ? `<div class="grid2" style="gap:10px"><div><label>Ширина (линия снизу)</label><input type="text" data-dim="x" value="${esc(((sl.dims || []).find(d => d.axis === "x") || {}).value || "")}" placeholder="например 18 см"></div>
        <div><label>Высота (линия сбоку)</label><input type="text" data-dim="y" value="${esc(((sl.dims || []).find(d => d.axis === "y") || {}).value || "")}" placeholder="пусто — без линии"></div></div>` : ""}
      ${i === 0 ? `<div class="cover-hero" style="margin-top:6px">${coverFields(sl)}</div>` : ""}
      <div class="extras">${extraFields(sl)}</div>
      ${i === 0 ? plan.cover_variants.map((cv, j) => `<label>Подзаголовок обложки ${"BC"[j]}</label><input type="text" data-cv="${j}" value="${esc(cv.subtitle)}">`).join("") : ""}
    </div>`;
  main.innerHTML = `
  <div class="card"><div class="row"><h2 style="margin:0">Стиль: ${esc(plan.style.name || "")}</h2><span class="spacer"></span><button class="btn small" id="pl-savestyle">Сохранить стиль в библиотеку</button></div>
    <p class="muted" style="margin:6px 0 0">${esc(plan.style.why || "")}</p><div id="pl-style">${styleEditor(plan.style)}</div></div>
  <div class="card"><h2>Ключевые запросы</h2><p class="muted" style="margin-top:-6px">Уберите лишнее крестиком, своё — впишите и Enter. После правок нажмите «Переписать тексты».</p>
    <div class="kw-group"><h3>Главные</h3><div class="chips" id="kw-main"></div></div>
    <div class="kw-group"><h3>Уточняющие</h3><div class="chips" id="kw-extra"></div></div>
    <div class="kw-group"><h3>Синонимы</h3><div class="chips" id="kw-synonyms"></div></div></div>
  <div class="card"><h2>Сценарий слайдов</h2>${plan.slides.map(slideBox).join("")}</div>
  <div class="card texts" id="pl-texts"><div class="row"><h2 style="margin:0">Тексты карточки</h2><span class="spacer"></span><button class="btn" id="plan-rewrite">Переписать тексты под ключи</button></div>
    <h3>Wildberries</h3><label>Название</label><input type="text" data-t="wb_title" value="${esc(t.wb_title)}">
    <label>Описание</label><textarea data-t="wb_description" rows="9">${esc(t.wb_description)}</textarea>
    <h3>Ozon</h3><label>Название</label><input type="text" data-t="ozon_title" value="${esc(t.ozon_title)}">
    <label>Аннотация (описание)</label><textarea data-t="ozon_annotation" rows="9">${esc(t.ozon_annotation)}</textarea>
    <label>#Хештеги</label><textarea data-t="ozon_hashtags" rows="2">${esc(t.ozon_hashtags)}</textarea></div>
  <div class="card"><h2>Запуск</h2><div class="grid3">
    <div><label>Профиль нейросетей</label><select id="pl-profile">${Object.keys(s.profiles).map(p => `<option ${p === st.inputs.profile ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></div>
    <div><label>Потолок на серию, ₽</label><input type="number" id="pl-limit" value="${esc(st.limit)}" min="10" step="10"></div>
    <div><label>&nbsp;</label><div class="est" id="pl-est"></div></div></div>
    <div class="row" style="margin-top:16px"><span class="muted">Будет нарисовано ${st.inputs.covers + 4} картинок: ${st.inputs.covers > 1 ? st.inputs.covers + " варианта обложки" : "обложка"} + 4 слайда.</span><span class="spacer"></span>
    <button class="btn primary big" id="plan-go">Запустить генерацию →</button></div></div>`;
  ["main", "extra", "synonyms"].forEach(k => chipsEditor($("#kw-" + k, main), kw[k]));
  const lim = {wb_title: s.wb_title_max, wb_description: s.wb_desc_max, ozon_title: s.ozon_title_max, ozon_annotation: s.ozon_desc_max};
  $$("[data-t]", main).forEach(x => lim[x.dataset.t] && counter(x, lim[x.dataset.t]));
  const est = () => $("#pl-est").innerHTML = `≈ <b>${rub(estimate($("#pl-profile").value, st.inputs.covers))}</b> без перегенераций`;
  est();
  const collect = () => ({
    keywords: kw,
    slides: $$(".plan-slide", main).map(b => {
      const o = {};
      $$("[data-f]", b).forEach(x => o[x.dataset.f] = x.dataset.f === "bullets" ? x.value.split("\n") : x.value);
      const dims = $$("[data-dim]", b).map(x => ({axis: x.dataset.dim, value: x.value.trim()})).filter(d => d.value);
      if ($("[data-dim]", b)) o.dims = dims;
      if ($(".cover-hero", b)) Object.assign(o, readCover($(".cover-hero", b)));
      Object.assign(o, readExtras($(".extras", b)));
      return o;
    }),
    cover_variants: $$("[data-cv]", main).map(x => ({subtitle: x.value})),
    texts: Object.fromEntries($$("[data-t]", main).map(x => [x.dataset.t, x.value])),
    style: readStyle($("#pl-style")), profile: $("#pl-profile").value, limit: $("#pl-limit").value,
  });
  let saveT;
  const save = () => api(`/api/projects/${st.id}/plan`, {json: collect()});
  main.oninput = main.onchange = () => { clearTimeout(saveT); saveT = setTimeout(save, 700); est(); };
  $$("input[type=color]", main).forEach(x => x.addEventListener("input", () => { const sp = x.nextElementSibling; if (sp && sp.tagName === "SPAN") sp.textContent = x.value; }));
  $("#pl-savestyle").onclick = async () => {
    const name = prompt("Название стиля для библиотеки:", plan.style.name || "Мой стиль");
    if (!name) return;
    await save(); await api("/api/styles", {json: {project: st.id, name}}); toast("Стиль сохранён в библиотеку");
  };
  $("#plan-rewrite").onclick = async () => { clearTimeout(saveT); await save(); await api(`/api/projects/${st.id}/rewrite-texts`, {json: {}}); toast("Переписываю тексты…"); };
  $("#plan-go").onclick = async () => {
    clearTimeout(saveT); await save();
    $("#plan-go").disabled = true;
    try { await api(`/api/projects/${st.id}/generate`, {json: {}}); } catch { $("#plan-go").disabled = false; }
  };
}

// ---------- результат
function renderResultsShell(main, st) {
  main.innerHTML = `
  <div class="card"><div class="row"><h2 style="margin:0">Слайды</h2><span class="spacer"></span>
    <button class="btn small" id="r-style">Стиль серии</button><button class="btn primary small" id="r-open" disabled>Открыть папку с готовым</button></div>
    <p class="muted" style="margin:6px 0 14px;font-size:13px">Нажмите на слайд, чтобы открыть крупно. «Текст и вид» — правка надписей и вёрстки (вид заголовка, бейдж, слоган) бесплатно; «Переделать» — новая картинка с вашим пожеланием; «Стиль серии» — шрифт, цвета, эффекты сразу для всех слайдов.</p>
    <div class="slides" id="r-slides"></div></div>
  <div class="card texts" id="r-texts"></div>`;
  $("#r-open").onclick = async () => { const d = await api(`/api/projects/${st.id}/open`, {json: {}}); toast("Папка: " + d.path, 4000); };
  $("#r-style").onclick = async () => {
    const cur = (await api("/api/projects/" + st.id)).state;
    const box = modal(`<h2>Стиль серии</h2><p class="muted">Применится ко всем слайдам сразу — бесплатно, нейросеть не нужна.</p><div id="rs">${styleEditor(cur.plan.style)}</div>
      <div class="row" style="margin-top:16px"><span class="spacer"></span><button class="btn" onclick="closeModal()">Отмена</button><button class="btn primary" id="rs-go">Применить</button></div>`);
    $("#rs-go", box).onclick = async () => { await api(`/api/projects/${st.id}/style`, {json: readStyle($("#rs", box))}); closeModal(); toast("Слайды перерисованы"); };
  };
}

function updateSlides(st) {
  const wrap = $("#r-slides");
  st.slides.forEach(sl => {
    let card = wrap.querySelector(`[data-k="${sl.key}"]`);
    if (!card) {
      card = el(`<div class="slide" data-k="${sl.key}"><div class="pic"></div><div class="meta"></div></div>`);
      wrap.appendChild(card);
    }
    const sig = [sl.final, sl.status, sl.cost, sl.attempts, sl.note, st.busy].join("|");
    if (card.dataset.sig === sig) return;
    card.dataset.sig = sig;
    const pic = $(".pic", card);
    const working = ["work", "check"].includes(sl.status);
    if (sl.final) {
      let img = $("img", pic);
      if (!img) { pic.innerHTML = "<img>"; img = $("img", pic); }
      if (img.dataset.src !== sl.final) { img.src = `/files/${st.id}/${sl.final}`; img.dataset.src = sl.final; }
      pic.onclick = () => modal(`<img class="zoom" src="/files/${st.id}/${sl.final}"><div class="row" style="margin-top:12px"><b>${esc(sl.title)}</b><span class="spacer"></span><button class="btn" onclick="closeModal()">Закрыть</button></div>`, true);
      if (working) pic.insertAdjacentHTML("beforeend", `<div class="ph" style="background:rgba(255,255,255,.7)">Переделываю…</div>`);
    } else {
      pic.onclick = null;
      const txt = sl.status === "work" ? "Рисую…" : sl.status === "check" ? "Проверяю качество…" : sl.status === "fail" ? "Не получилось" :
        sl.status === "skip" ? "Не нарисован — кнопка «Дорисовать» вверху" : "В очереди";
      pic.innerHTML = `<div class="ph ${working ? "shimmer" : ""}">${txt}</div>`;
    }
    $(".meta", card).innerHTML = `<div class="row"><b>${esc(sl.title)}</b><span class="spacer"></span><span class="muted">${rub(sl.cost)}</span></div>
      <div class="muted" style="font-size:12px">${esc((sl.used_model || sl.model || "").split("/").pop())}${sl.attempts > 1 ? " · попыток: " + sl.attempts : ""}</div>
      ${sl.note ? `<div class="note">${esc(sl.note)}</div>` : ""}
      <div class="acts"><button class="btn small" data-a="text" ${sl.final ? "" : "disabled"}>Текст и вид</button><button class="btn small" data-a="regen" ${st.busy ? "disabled" : ""}>Переделать</button></div>`;
    $("[data-a=text]", card).onclick = () => editSlideText(st.id, sl);
    $("[data-a=regen]", card).onclick = () => regenSlide(st.id, sl, st);
  });
}

function editSlideText(pid, sl) {
  const dim = a => ((sl.dims || []).find(d => d.axis === a) || {}).value || "";
  const box = modal(`<h2>${esc(sl.title)}: текст и вид</h2><p class="muted">Картинка останется та же, поменяются надписи и вёрстка. Бесплатно.</p>
    <label>Заголовок</label><input type="text" id="e-h" value="${esc(sl.headline)}">
    ${sl.kind !== "specs" && sl.kind !== "trust" ? `<label>Подзаголовок</label><input type="text" id="e-s" value="${esc(sl.subtitle)}">` : ""}
    ${["problem", "specs", "trust"].includes(sl.kind) ? `<label>Пункты — каждый с новой строки</label><textarea id="e-b" rows="4">${esc((sl.bullets || []).join("\n"))}</textarea>` : ""}
    ${sl.kind === "cover" ? `<div id="e-cover">${coverFields(sl)}</div>` : ""}
    ${sl.kind === "specs" ? `<div class="grid2" style="gap:10px"><div><label>Ширина</label><input type="text" id="e-x" value="${esc(dim("x"))}"></div><div><label>Высота</label><input type="text" id="e-y" value="${esc(dim("y"))}"></div></div>` : ""}
    <div id="e-extra">${extraFields(sl).replace("<details class=\"look-box\">", "<details class=\"look-box\" open>")}</div>
    <div class="row" style="margin-top:16px"><span class="spacer"></span><button class="btn" onclick="closeModal()">Отмена</button><button class="btn primary" id="e-go">Сохранить</button></div>`, true);
  $("#e-go", box).onclick = async () => {
    const d = {headline: $("#e-h", box).value};
    if ($("#e-s", box)) d.subtitle = $("#e-s", box).value;
    if ($("#e-b", box)) d.bullets = $("#e-b", box).value.split("\n");
    if ($("#e-cover", box)) Object.assign(d, readCover($("#e-cover", box)));
    Object.assign(d, readExtras($("#e-extra", box)));
    if ($("#e-x", box)) d.dims = [{axis: "x", value: $("#e-x", box).value}, {axis: "y", value: $("#e-y", box).value}];
    await api(`/api/projects/${pid}/slides/${sl.key}/text`, {json: d}); closeModal(); toast("Текст обновлён");
  };
}

function regenSlide(pid, sl, st) {
  const price = priceOf(sl.used_model || sl.model);
  const box = modal(`<h2>Переделать: ${esc(sl.title)}</h2>
    <label>Что изменить? (можно пусто — просто другой вариант)</label>
    <textarea id="r-wish" rows="3" placeholder="например: фон светлее, человек постарше, товар крупнее"></textarea>
    <p class="muted" style="font-size:13px">Стоимость ≈ ${rub(price)} за попытку (+ проверка). Осталось до потолка: ${rub(st.limit - st.spent)}.</p>
    <div class="row"><span class="spacer"></span><button class="btn" onclick="closeModal()">Отмена</button><button class="btn primary" id="r-go">Переделать</button></div>`);
  $("#r-go", box).onclick = async () => { await api(`/api/projects/${pid}/slides/${sl.key}/regen`, {json: {wish: $("#r-wish", box).value}}); closeModal(); toast("Переделываю…"); };
}

function renderTexts(st) {
  const box = $("#r-texts"), t = st.plan.texts || {}, s = META.settings;
  const kw = st.plan.keywords;
  const field = (k, name, rows, max) => `<div class="copy-row"><label>${name}</label><button class="btn small" data-copy="${k}">Копировать</button></div>
    ${rows ? `<textarea data-t="${k}" rows="${rows}">${esc(t[k])}</textarea>` : `<input type="text" data-t="${k}" value="${esc(t[k])}">`}`;
  box.innerHTML = `<div class="row"><h2 style="margin:0">Тексты карточки</h2><span class="spacer"></span><button class="btn small" id="t-save" disabled>Сохранить правки</button></div>
    <h3>Wildberries</h3>${field("wb_title", "Название", 0)}${field("wb_description", "Описание", 9)}
    <h3>Ozon</h3>${field("ozon_title", "Название", 0)}${field("ozon_annotation", "Аннотация (описание)", 9)}${field("ozon_hashtags", "#Хештеги", 2)}
    <h3>Ключевые запросы</h3><p class="muted" style="font-size:13.5px">${esc([...kw.main, ...kw.extra, ...kw.synonyms].join(", "))}</p>`;
  const lim = {wb_title: s.wb_title_max, wb_description: s.wb_desc_max, ozon_title: s.ozon_title_max, ozon_annotation: s.ozon_desc_max};
  $$("[data-t]", box).forEach(x => { if (lim[x.dataset.t]) counter(x, lim[x.dataset.t]); x.addEventListener("input", () => $("#t-save").disabled = false); });
  $$("[data-copy]", box).forEach(b => b.onclick = () => copyText($(`[data-t="${b.dataset.copy}"]`, box).value));
  $("#t-save").onclick = async () => {
    await api(`/api/projects/${st.id}/texts`, {json: Object.fromEntries($$("[data-t]", box).map(x => [x.dataset.t, x.value]))});
    $("#t-save").disabled = true; toast("Тексты сохранены");
  };
}

// ============================================================ история
function viewHistory(v) {
  const h = META.history;
  v.innerHTML = `<h1>История</h1><p class="sub">Все серии хранятся в папке projects. Нажмите, чтобы открыть.</p>
    ${h.length ? `<div class="hist">${h.map(x => `<a href="#/p/${esc(x.id)}"><div class="h"><div class="im" style="${x.thumb ? `background-image:url('/files/${esc(x.id)}/${esc(x.thumb)}')` : ""}"></div>
      <div class="t"><b>${esc(x.title)}</b>${new Date(x.created * 1000).toLocaleString("ru-RU", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"})} · ${rub(x.spent)}<br>
      <span class="pill ${(STATUS[x.status] || [])[1] || ""}" style="margin-top:6px">${esc((STATUS[x.status] || [x.status])[0])}</span></div></div></a>`).join("")}</div>` : '<div class="empty">Пока ни одной серии. <a href="#/new">Создать первую</a></div>'}`;
}

// ============================================================ настройки
function viewSettings(v) {
  const s = JSON.parse(JSON.stringify(META.settings));
  const prices = s.image_prices;
  v.innerHTML = `<h1>Настройки</h1><p class="sub">Сохраняются в settings.json рядом с программой. Ключ vsellm — в config.py.</p>
  <div class="grid2">
    <div class="card"><h2>Профили нейросетей-художников</h2>
      <p class="muted" style="font-size:13px;margin-top:-6px">По одной модели в строке. Первая рисует слайды, варианты обложки берут модели по очереди, при браке — следующая.</p>
      ${Object.entries(s.profiles).map(([n, l]) => `<label>${esc(n)}</label><textarea data-prof="${esc(n)}" rows="2">${esc(l.join("\n"))}</textarea>`).join("")}
      <label>Профиль по умолчанию</label><select id="s-profile">${Object.keys(s.profiles).map(p => `<option ${p === s.profile ? "selected" : ""}>${esc(p)}</option>`).join("")}</select>
      <div class="row" style="margin-top:12px"><button class="btn small" id="s-models">Показать модели vsellm</button></div><div id="s-models-out" class="muted" style="font-size:12.5px;margin-top:8px"></div>
    </div>
    <div class="card"><h2>Цены за картинку, ₽</h2>
      <p class="muted" style="font-size:13px;margin-top:-6px">Фактические — смотрите в журнале списаний vsellm. vsellm берёт деньги и за неудачные попытки.</p>
      <table class="prices" id="s-prices">${Object.entries(prices).map(([m, p]) => `<tr><td>${esc(m)}</td><td style="width:110px"><input type="number" step="0.01" data-price="${esc(m)}" value="${p}"></td></tr>`).join("")}
      <tr><td><input type="text" id="s-newmodel" placeholder="новая модель, например google/..."></td><td><input type="number" step="0.01" id="s-newprice" placeholder="₽"></td></tr></table>
    </div>
  </div>
  <div class="grid2">
    <div class="card"><h2>Тексты и проверка</h2>
      <label>Сценарист и тексты карточки</label><input type="text" id="s-script" value="${esc(s.script_model)}">
      <label>Контролёр качества</label><input type="text" id="s-check" value="${esc(s.check_model)}">
      <label><input type="checkbox" id="s-checkon" ${s.check_enabled ? "checked" : ""}> Проверять каждый слайд (≈ 0,6 ₽ за проверку)</label>
      <div class="grid2" style="gap:10px"><div><label>Попыток на слайд</label><input type="number" id="s-att" min="1" max="4" value="${s.max_attempts}"></div>
      <div><label>Рисовать одновременно</label><input type="number" id="s-par" min="1" max="4" value="${s.parallel}"></div></div>
    </div>
    <div class="card"><h2>Лимиты</h2>
      <label>Потолок на серию по умолчанию, ₽</label><input type="number" id="s-limit" value="${s.limit_rub}">
      <div class="grid2" style="gap:10px">
        <div><label>Название WB, символов</label><input type="number" id="s-wbt" value="${s.wb_title_max}"></div>
        <div><label>Название Ozon, символов</label><input type="number" id="s-ozt" value="${s.ozon_title_max}"></div>
        <div><label>Описание WB, символов</label><input type="number" id="s-wbd" value="${s.wb_desc_max}"></div>
        <div><label>Аннотация Ozon, символов</label><input type="number" id="s-ozd" value="${s.ozon_desc_max}"></div>
      </div>
    </div>
  </div>
  <div class="card"><h2>Мои стили</h2><div class="styles" id="s-styles"></div></div>
  <div class="row"><span class="spacer"></span><button class="btn primary big" id="s-save">Сохранить настройки</button></div>`;
  const drawStyles = () => {
    const mine = META.styles.filter(x => !x.builtin);
    $("#s-styles").innerHTML = mine.length ? mine.map(x => styleCard(x, false).replace("</div></div>", `</div><button class="btn small danger" data-del="${esc(x.name)}" style="margin-top:8px">Удалить</button></div>`)).join("") : '<p class="muted">Пока нет. Сохраните стиль на шаге утверждения сценария.</p>';
    $$("[data-del]").forEach(b => b.onclick = async e => { e.stopPropagation(); if (!confirm("Удалить стиль «" + b.dataset.del + "»?")) return; META.styles = (await api("/api/styles/delete", {json: {name: b.dataset.del}})).styles; drawStyles(); });
  };
  drawStyles();
  $("#s-models").onclick = async () => {
    $("#s-models-out").textContent = "Загружаю…";
    try { const d = await api("/api/models"); $("#s-models-out").innerHTML = "<b>Картинки:</b> " + d.image.map(esc).join(", "); }
    catch { $("#s-models-out").textContent = "vsellm не ответил"; }
  };
  $("#s-save").onclick = async () => {
    const profiles = {};
    $$("[data-prof]").forEach(x => profiles[x.dataset.prof] = x.value.split("\n").map(y => y.trim()).filter(Boolean));
    const image_prices = {};
    $$("[data-price]").forEach(x => image_prices[x.dataset.price] = +x.value);
    const nm = $("#s-newmodel").value.trim(); if (nm) image_prices[nm] = +$("#s-newprice").value || 8;
    await api("/api/settings", {json: {
      profiles, image_prices, profile: $("#s-profile").value, script_model: $("#s-script").value.trim(), check_model: $("#s-check").value.trim(),
      check_enabled: $("#s-checkon").checked, max_attempts: +$("#s-att").value, parallel: +$("#s-par").value, limit_rub: +$("#s-limit").value,
      wb_title_max: +$("#s-wbt").value, ozon_title_max: +$("#s-ozt").value, wb_desc_max: +$("#s-wbd").value, ozon_desc_max: +$("#s-ozd").value,
    }});
    toast("Настройки сохранены"); route();
  };
}
