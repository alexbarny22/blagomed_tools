/* Общий график цен (Chart.js, ось времени без внешних адаптеров). */
(function () {
  const pad = n => String(n).padStart(2, '0');
  const toMs = ts => new Date(ts.replace(' ', 'T')).getTime();
  const rub = v => Math.round(v).toLocaleString('ru-RU') + ' ₽';

  function fmtTick(ms, spanDays) {
    const d = new Date(ms);
    if (spanDays <= 1.5) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (spanDays <= 4) return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:00`;
    return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
  }
  function fmtFull(ms) {
    const d = new Date(ms);
    return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // Ровные отметки времени: каждый час / 3 ч / 6 ч / сутки / неделю — по длине периода
  function niceTicks(fromMs, toMs_, maxTicks) {
    const H = 3600e3, D = 24 * H;
    const steps = [H, 2 * H, 3 * H, 6 * H, 12 * H, D, 2 * D, 3 * D, 7 * D];
    const step = steps.find(st => (toMs_ - fromMs) / st <= maxTicks) || 7 * D;
    const t = new Date(fromMs);
    if (step >= D) {
      t.setHours(0, 0, 0, 0);
      if (t.getTime() < fromMs) t.setDate(t.getDate() + 1);
    } else {
      t.setMinutes(0, 0, 0);
      if (t.getTime() < fromMs) t.setHours(t.getHours() + 1);
      while (t.getHours() % (step / H) !== 0) t.setHours(t.getHours() + 1);  // кратно местным часам
    }
    const out = [];
    while (t.getTime() <= toMs_ && out.length < 50) {
      out.push({value: t.getTime()});
      if (step >= D) t.setDate(t.getDate() + step / D); else t.setHours(t.getHours() + step / H);
    }
    return out;
  }

  // Ступенчатая линия: значение держится до следующей точки, в начале периода — последнее известное значение.
  function series(rows, key, fromMs, toMs_) {
    const pts = [];
    for (const r of rows) {
      const v = r[key];
      if (v === null || v === undefined || v === 0) continue;
      pts.push({x: Math.max(toMs(r.ts), fromMs), y: v});
    }
    if (pts.length) pts.push({x: toMs_, y: pts[pts.length - 1].y});
    return pts;
  }

  /**
   * d: {rows, min, max, target, changes, from, to}
   * opts: {mini: bool}
   */
  window.renderPriceChart = function (canvas, d, opts) {
    opts = opts || {};
    const mini = !!opts.mini;
    const fromMs = toMs(d.from), toMs_ = toMs(d.to);
    const spanDays = (toMs_ - fromMs) / 864e5;
    const line = (label, data, color, extra) => Object.assign({
      label, data, borderColor: color, backgroundColor: color, stepped: 'before',
      pointRadius: 0, pointHoverRadius: 4, borderWidth: 2, spanGaps: true,
    }, extra || {});
    const flat = (label, v, color) => v ? {
      label, data: [{x: fromMs, y: v}, {x: toMs_, y: v}], borderColor: color,
      borderDash: [6, 4], pointRadius: 0, pointHoverRadius: 0, borderWidth: 1.5,
    } : null;

    const ds = [
      line('Со скидками продавца', series(d.rows, 'seller_price', fromMs, toMs_), '#0f7b5f', {borderWidth: mini ? 2 : 3}),
      mini ? null : line('Цена', series(d.rows, 'price', fromMs, toMs_), '#2f6fb0', {borderWidth: 1.5}),
      mini ? null : line('≈ Покупатель', series(d.rows, 'buyer_est', fromMs, toMs_), '#9b59b6', {borderWidth: 1.5, hidden: true}),
      flat('Минимум', d.min, '#c0392b'),
      flat('Максимум', d.max, '#b7791f'),
      flat('Цель', d.target, '#12322a'),
    ].filter(Boolean);

    // отметки изменений, сделанных программой
    const ch = (d.changes || []).filter(c => toMs(c.ts) >= fromMs);
    if (ch.length) {
      ds.push({
        label: 'Изменение цены', type: 'scatter', showLine: false,
        data: ch.map(c => ({x: toMs(c.ts), y: c.price_after, c})),
        pointStyle: 'triangle', pointRadius: mini ? 4 : 6, pointHoverRadius: 8,
        backgroundColor: ch.map(c => c.dry_run ? '#e0a526' : '#2f6fb0'), borderColor: '#fff', borderWidth: 1,
      });
    }

    if (canvas._chart) canvas._chart.destroy();
    canvas._chart = new Chart(canvas, {
      type: 'line', data: {datasets: ds},
      options: {
        maintainAspectRatio: false, animation: mini ? false : {duration: 250}, parsing: false, normalized: true,
        interaction: {mode: 'nearest', axis: 'x', intersect: false},
        scales: {
          x: {type: 'linear', min: fromMs, max: toMs_,
            afterBuildTicks: ax => { ax.ticks = niceTicks(fromMs, toMs_, mini ? 4 : 9); },
            ticks: {maxTicksLimit: mini ? 4 : 8, callback: v => fmtTick(v, spanDays), font: {size: mini ? 10 : 12}},
            grid: {display: !mini}},
          y: {grace: '8%', ticks: {maxTicksLimit: mini ? 4 : 8, callback: v => Math.round(v).toLocaleString('ru-RU') + (mini ? '' : ' ₽'),
            font: {size: mini ? 10 : 12}}},
        },
        plugins: {
          legend: {display: !mini, position: 'bottom', labels: {usePointStyle: true, boxHeight: 8}},
          tooltip: {callbacks: {
            title: items => items.length ? fmtFull(items[0].parsed.x) : '',
            label: c => {
              const raw = c.raw || {};
              if (raw.c) return `${raw.c.dry_run ? 'Тренировка' : 'Изменено'}: ${rub(raw.c.price_before)} → ${rub(raw.c.price_after)} (${raw.c.reason})`;
              return `${c.dataset.label}: ${rub(c.parsed.y)}`;
            }}},
        },
      },
    });
    return canvas._chart;
  };

  // Кнопки периодов + свой диапазон дат. onChange({days} | {from, to})
  window.periodPicker = function (root, onChange, initialDays) {
    const presets = [[1, '1 день'], [3, '3 дня'], [7, 'Неделя'], [14, '2 недели'], [30, 'Месяц']];
    root.innerHTML = presets.map(([d, l]) => `<button type="button" class="btn small" data-d="${d}">${l}</button>`).join('')
      + `<span class="custom"><input type="date" class="pp-from" title="с"> — <input type="date" class="pp-to" title="по">
         <button type="button" class="btn small pp-go">Показать</button></span>`;
    const btns = root.querySelectorAll('button[data-d]');
    const mark = d => btns.forEach(b => b.classList.toggle('on', +b.dataset.d === d));
    btns.forEach(b => b.onclick = () => { mark(+b.dataset.d); onChange({days: +b.dataset.d}); });
    const today = new Date(); const iso = x => `${x.getFullYear()}-${pad(x.getMonth() + 1)}-${pad(x.getDate())}`;
    const f = root.querySelector('.pp-from'), t = root.querySelector('.pp-to');
    t.value = iso(today); t.max = iso(today); f.max = iso(today);
    f.value = iso(new Date(today.getTime() - 7 * 864e5));
    root.querySelector('.pp-go').onclick = () => {
      if (!f.value || !t.value || f.value > t.value) { alert('Укажите даты «с» и «по»'); return; }
      const days = (new Date(t.value) - new Date(f.value)) / 864e5 + 1;
      if (days > 31) { alert('Период — не больше месяца'); return; }
      mark(-1); onChange({from: f.value + ' 00:00:00', to: t.value + ' 23:59:59'});
    };
    mark(initialDays); onChange({days: initialDays});
  };
  window.periodQuery = p => p.days ? `days=${p.days}` : `from=${encodeURIComponent(p.from)}&to=${encodeURIComponent(p.to)}`;
})();
