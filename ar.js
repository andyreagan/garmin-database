/* ar.js — shared behaviour for the andyreagan.com dashboards.
   Canonical copy lives in ~/projects/2026/dashboard-kit; `./sync.sh` vendors
   it into each repo.  Edit it there, not in the vendored copies.

   Load in <head> (no defer) so the theme is applied before first paint:
     <html data-site="whoop">
     <meta name="ar-repo" content="whoop-database">
     <meta name="ar-generated" content="2026-11-09">
     <link rel="stylesheet" href="ar.css">
     <script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>
     <script src="ar.js"></script>

   Provides: the universe bar + footer, the light/dark toggle, and a few d3
   chart helpers (AR.lineChart, AR.barChart, AR.hbarChart, AR.scatter, AR.tip,
   AR.legend).  Charts are styled entirely with CSS variables, so a theme
   switch needs no re-render. */
(function () {
  'use strict';
  const AR = (window.AR = window.AR || {});

  // ── THEME ────────────────────────────────────────────────────────────────
  // No stored choice → follow the OS (handled in CSS).  An explicit choice is
  // kept in a .andyreagan.com cookie so it carries across the subdomains,
  // with localStorage as the fallback for localhost / file://.
  const KEY = 'ar-theme';
  const root = document.documentElement;
  const mq = window.matchMedia('(prefers-color-scheme: dark)');

  function stored() {
    const m = document.cookie.match(/(?:^|; )ar-theme=(light|dark)/);
    if (m) return m[1];
    try { const v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : null; }
    catch (e) { return null; }
  }
  function store(v) {
    const dom = /(^|\.)andyreagan\.com$/.test(location.hostname) ? '; domain=.andyreagan.com' : '';
    document.cookie = v
      ? `${KEY}=${v}; path=/; max-age=31536000; SameSite=Lax${dom}`
      : `${KEY}=; path=/; max-age=0${dom}`;
    try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch (e) {}
  }
  AR.theme = () => root.dataset.theme || (mq.matches ? 'dark' : 'light');
  function apply(v) {
    if (v) root.dataset.theme = v; else delete root.dataset.theme;
    const b = document.querySelector('.ar-theme');
    if (b) b.textContent = AR.theme() === 'dark' ? '☀ Light' : '☾ Dark';
    document.dispatchEvent(new CustomEvent('ar:theme', { detail: AR.theme() }));
  }
  AR.toggleTheme = function () {
    const next = AR.theme() === 'dark' ? 'light' : 'dark';
    // choosing what the OS already says clears the override → back to "follow system"
    const v = next === (mq.matches ? 'dark' : 'light') ? null : next;
    store(v); apply(v);
  };
  apply(stored());
  mq.addEventListener('change', () => apply(stored()));

  // ── UNIVERSE BAR + FOOTER ────────────────────────────────────────────────
  const SITES = [
    ['garmin',   'Garmin',   'https://garmin.andyreagan.com/'],
    ['whoop',    'WHOOP',    'https://whoop.andyreagan.com/'],
    ['strava',   'Strava',   'https://strava.andyreagan.com/'],
    ['health',   'Bloodwork','https://health.andyreagan.com/'],
    ['training', 'Training', 'https://training.andyreagan.com/'],
    null,
    ['tools',    'Tools',          'https://tools.andyreagan.com/'],
    ['viz',      'Visualizations', 'https://andyreagan.com/visualizations.html'],
    ['blog',     'Blog',           'https://andyreagan.com/blog.html'],
  ];
  const meta = n => (document.querySelector(`meta[name="${n}"]`) || {}).content || '';

  function chrome() {
    if (document.querySelector('.ar-bar')) return;
    const site = root.dataset.site || '';
    const bar = document.createElement('header');
    bar.className = 'ar-bar';
    bar.innerHTML =
      '<a class="ar-home" href="https://andyreagan.com/">andyreagan.com</a>' +
      '<nav class="ar-links">' +
      SITES.map(s => s
        ? `<a href="${s[2]}"${s[0] === site ? ' class="current" aria-current="page"' : ''}>${s[1]}</a>`
        : '<span class="ar-sep"></span>').join('') +
      '</nav>' +
      '<button class="ar-theme" type="button" title="Switch light / dark"></button>';
    document.body.prepend(bar);
    bar.querySelector('.ar-theme').addEventListener('click', AR.toggleTheme);

    const repo = meta('ar-repo'), gen = meta('ar-generated');
    const foot = document.createElement('footer');
    foot.className = 'ar-footer';
    foot.innerHTML =
      '<span>Part of <a href="https://andyreagan.com/">andyreagan.com</a></span>' +
      (repo ? `<span><a href="https://github.com/andyreagan/${repo}">Source on GitHub</a></span>` : '') +
      (gen ? `<span>Generated ${gen}</span>` : '');
    (document.querySelector('.main') || document.body).append(foot);
    apply(stored());
  }
  if (document.body) chrome(); else document.addEventListener('DOMContentLoaded', chrome);

  // ── TOOLTIP ──────────────────────────────────────────────────────────────
  let tipEl = null;
  AR.tip = {
    show(html, ev) {
      if (!tipEl) { tipEl = document.createElement('div'); tipEl.className = 'ar-tip'; document.body.append(tipEl); }
      tipEl.innerHTML = html;
      tipEl.style.display = 'block';
      const r = tipEl.getBoundingClientRect();
      let x = ev.clientX + 14, y = ev.clientY - r.height - 10;
      if (x + r.width > window.innerWidth - 8) x = ev.clientX - r.width - 14;
      if (y < 8) y = ev.clientY + 16;
      tipEl.style.left = Math.max(8, x) + 'px';
      tipEl.style.top = y + 'px';
    },
    hide() { if (tipEl) tipEl.style.display = 'none'; },
  };
  const sw = c => `<span class="sw" style="background:${c}"></span>`;
  AR.swatch = sw;

  AR.legend = function (el, items) {
    el = typeof el === 'string' ? document.querySelector(el) : el;
    el.classList.add('legend');
    el.innerHTML = items.map(i =>
      `<span class="legend-item"><span class="sw" style="background:${i.color}"></span>${i.name}</span>`).join('');
  };

  // ── CHART PLUMBING ───────────────────────────────────────────────────────
  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  // 'YYYY-MM-DD' | 'YYYY-MM' | 'YYYY-Qn' | 'YYYY' | Date → local Date (or null)
  AR.parseDate = function (l) {
    if (l instanceof Date) return l;
    let m = /^(\d{4})-(\d{2})(?:-(\d{2}))?/.exec(l || '');
    if (m) return new Date(+m[1], +m[2] - 1, m[3] ? +m[3] : 1);
    m = /^(\d{4})-Q(\d)$/.exec(l || '');
    if (m) return new Date(+m[1], (+m[2] - 1) * 3, 1);
    m = /^(\d{4})$/.exec(l || '');
    return m ? new Date(+m[1], 0, 1) : null;
  };
  AR.fmtDate = function (l) {
    if (typeof l === 'string' && !/^\d{4}-\d{2}-\d{2}/.test(l)) {
      const m = /^(\d{4})-(\d{2})$/.exec(l);
      return m ? `${MONTHS[+m[2] - 1]} ${m[1]}` : l;
    }
    const d = AR.parseDate(l);
    return d ? `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}` : String(l);
  };
  function timeTick(span) {
    return d => span > 400 * 864e5
      ? (d.getMonth() === 0 ? String(d.getFullYear()) : `${MONTHS[d.getMonth()]} ’${String(d.getFullYear()).slice(2)}`)
      : span > 60 * 864e5 ? `${MONTHS[d.getMonth()]}${d.getMonth() === 0 ? ' ’' + String(d.getFullYear()).slice(2) : ''}`
      : `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  }
  let uid = 0;

  // Draw now and again whenever the container's width changes (including the
  // first time a hidden tab becomes visible).  Re-calling a chart helper on
  // the same element replaces the previous chart.
  function mount(el, opts, draw) {
    el = typeof el === 'string' ? document.querySelector(el) : el;
    if (!el) return;
    if (el._arRO) el._arRO.disconnect();
    let lastW = -1;
    const run = () => {
      const w = Math.round(el.clientWidth);
      if (w === lastW) return;
      lastW = w;
      el.innerHTML = '';
      const W = w || opts.width || 560, H = opts.height || 150;
      const svg = d3.select(el).append('svg').attr('class', 'ar-chart')
        .attr('viewBox', `0 0 ${W} ${H}`).attr('height', H);
      draw(svg, W, H);
    };
    run();
    el._arRO = new ResizeObserver(run);
    el._arRO.observe(el);
  }
  AR.mount = mount;   // for hand-written d3 charts: AR.mount(el, {height}, (svg, W, H) => …)
  function emptyMsg(svg, W, H) {
    svg.append('text').attr('x', W / 2).attr('y', H / 2).attr('text-anchor', 'middle')
      .style('font-size', '12px').text('No data in range');
  }
  function yAxis(g, y, W, m, fmt, ticks) {
    g.append('g').attr('class', 'grid').attr('transform', `translate(${m.l},0)`)
      .call(d3.axisLeft(y).ticks(ticks || 4).tickSize(-(W - m.l - m.r)).tickFormat(fmt).tickPadding(6));
  }

  // ── LINE CHART ───────────────────────────────────────────────────────────
  // AR.lineChart(el, {
  //   x: [...labels],                 dates ('YYYY-MM-DD' / 'YYYY-MM' / 'YYYY-Qn') or anything (then evenly spaced)
  //   y: [...], color,                single series shorthand, or
  //   series: [{name, values, color, dashed, area, width, dots}],
  //   height, yFmt, yDomain:[lo,hi], zero, yTicks,
  //   band: {lo, hi},                 shaded reference range
  //   refLines: [{y, label}],         horizontal dashed lines
  //   markers: [{x, label}],          vertical event lines (x in same format as opts.x)
  //   dots: true | (v,i)=>color,      draw every point (sparse series), optionally coloured per point
  //   area: true,                     gradient fill under a single series (default on for one series)
  //   tip: (i)=>html,                 override tooltip body
  // })
  AR.lineChart = function (el, opts) {
    const series = opts.series || [{ name: opts.name || '', values: opts.y, color: opts.color || 'var(--accent)',
                                     area: opts.area !== false, dots: opts.dots }];
    const labels = opts.x;
    const yFmt = opts.yFmt || (v => d3.format('~g')(+v.toFixed(2)));
    const id = 'arg' + (++uid);
    mount(el, opts, (svg, W, H) => {
      const m = Object.assign({ t: 12, r: 12, b: 24, l: 44 }, opts.margin);
      const all = series.flatMap(s => s.values.filter(v => v != null && !isNaN(v)));
      if (!all.length) return emptyMsg(svg, W, H);

      const dates = labels.map(AR.parseDate);
      const isTime = dates.every(d => d);
      const x = isTime
        ? d3.scaleTime().domain(d3.extent(dates)).range([m.l, W - m.r])
        : d3.scaleLinear().domain([0, Math.max(labels.length - 1, 1)]).range([m.l, W - m.r]);
      const X = i => isTime ? x(dates[i]) : x(i);

      let lo = d3.min(all), hi = d3.max(all);
      if (opts.band) { lo = Math.min(lo, opts.band.lo ?? lo); hi = Math.max(hi, opts.band.hi ?? hi); }
      if (opts.zero) lo = Math.min(0, lo);
      if (lo === hi) { lo -= 1; hi += 1; }
      const pad = (hi - lo) * 0.06;
      const dom = opts.yDomain || [opts.zero && lo === 0 ? 0 : lo - pad, hi + pad];
      const y = d3.scaleLinear().domain(dom).range([H - m.b, m.t]);

      yAxis(svg, y, W, m, yFmt, opts.yTicks);
      const span = isTime ? x.domain()[1] - x.domain()[0] : 0;
      // tick count from the width; on spans of a few days ask for whole days so
      // the same date is never repeated, and never fall to a single tick
      const nT = Math.max(2, Math.floor((W - m.l - m.r) / 80)), days = span / 864e5;
      let tAxis = d3.axisBottom(x).ticks(days <= nT ? d3.timeDay.every(1) : nT);
      if (isTime && x.ticks(nT).length < 2) tAxis = d3.axisBottom(x).tickValues(x.domain());
      const xa = isTime
        ? tAxis.tickFormat(timeTick(span))
        : d3.axisBottom(x).ticks(Math.min(labels.length, Math.max(2, Math.floor((W - m.l - m.r) / 90))))
            .tickFormat(i => Number.isInteger(i) && labels[i] != null ? labels[i] : '');
      svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${H - m.b})`).call(xa.tickSize(0).tickPadding(8));

      if (opts.band) {
        const b0 = y(Math.min(opts.band.hi ?? dom[1], dom[1])), b1 = y(Math.max(opts.band.lo ?? dom[0], dom[0]));
        svg.append('rect').attr('class', 'band').attr('x', m.l).attr('width', W - m.l - m.r)
          .attr('y', b0).attr('height', Math.max(0, b1 - b0));
      }
      (opts.refLines || []).forEach(r => {
        svg.append('line').attr('class', 'ref-line').attr('x1', m.l).attr('x2', W - m.r).attr('y1', y(r.y)).attr('y2', y(r.y));
        if (r.label) svg.append('text').attr('x', W - m.r).attr('y', y(r.y) - 4).attr('text-anchor', 'end').text(r.label);
      });
      (opts.markers || []).forEach(k => {
        const d = AR.parseDate(k.x); if (!d || !isTime) return;
        const px = x(d); if (px < m.l || px > W - m.r) return;
        const g = svg.append('g');
        g.append('line').attr('class', 'ref-line').attr('x1', px).attr('x2', px).attr('y1', m.t).attr('y2', H - m.b);
        if (k.label) g.append('title').text(k.label);
      });

      series.forEach((s, si) => {
        const def = (v, i) => v != null && !isNaN(v);
        const idx = d3.range(s.values.length);
        if (s.area && series.length === 1) {
          const gr = svg.append('defs').append('linearGradient').attr('id', id).attr('x1', 0).attr('y1', 0).attr('x2', 0).attr('y2', 1);
          gr.append('stop').attr('offset', '0%').attr('stop-color', s.color).attr('stop-opacity', 0.18);
          gr.append('stop').attr('offset', '100%').attr('stop-color', s.color).attr('stop-opacity', 0);
          svg.append('path').attr('fill', `url(#${id})`)
            .attr('d', d3.area().defined(i => def(s.values[i])).x(X).y0(H - m.b).y1(i => y(s.values[i]))(idx));
        }
        svg.append('path').attr('fill', 'none').attr('stroke', s.color).attr('stroke-width', s.width || 2)
          .attr('stroke-linejoin', 'round').attr('stroke-linecap', 'round')
          .attr('stroke-dasharray', s.dashed ? '5 4' : null)
          .attr('d', d3.line().defined(i => def(s.values[i])).x(X).y(i => y(s.values[i]))(idx));
        const live = idx.filter(i => def(s.values[i]));
        if (s.dots) {
          svg.append('g').selectAll('circle').data(live).join('circle').attr('class', 'ring')
            .attr('cx', X).attr('cy', i => y(s.values[i])).attr('r', 4)
            .attr('fill', i => typeof s.dots === 'function' ? s.dots(s.values[i], i) : s.color);
        } else if (live.length) {
          // isolated points (no neighbours) would otherwise be invisible; plus an end marker
          const lone = live.filter(i => !def(s.values[i - 1]) && !def(s.values[i + 1]));
          svg.append('g').selectAll('circle').data(lone.concat(live[live.length - 1])).join('circle')
            .attr('cx', X).attr('cy', i => y(s.values[i])).attr('r', (i, k, n) => k === n.length - 1 ? 3.5 : 1.5)
            .attr('fill', s.color);
        }
      });

      // hover: crosshair + one tooltip for all series at the nearest x
      const hover = svg.append('g').style('display', 'none');
      hover.append('line').attr('class', 'crosshair').attr('y1', m.t).attr('y2', H - m.b);
      const hd = hover.selectAll('circle').data(series).join('circle').attr('class', 'ring').attr('r', 4).attr('fill', s => s.color);
      const xs = labels.map((_, i) => X(i));
      const has = labels.map((_, i) => series.some(s => s.values[i] != null && !isNaN(s.values[i])));
      svg.append('rect').attr('x', m.l).attr('y', m.t).attr('width', Math.max(0, W - m.l - m.r)).attr('height', Math.max(0, H - m.t - m.b))
        .attr('fill', 'transparent')
        .on('mousemove touchmove', ev => {
          const [px] = d3.pointer(ev, svg.node());
          let i = d3.bisectCenter(xs, px);
          // walk to nearest index that has data
          let a = i, b = i;
          while (a >= 0 && !has[a]) a--;
          while (b < has.length && !has[b]) b++;
          if (a < 0 && b >= has.length) return;
          i = a < 0 ? b : b >= has.length ? a : (px - xs[a] <= xs[b] - px ? a : b);
          hover.style('display', null);
          hover.select('line').attr('x1', xs[i]).attr('x2', xs[i]);
          hd.attr('cx', xs[i]).attr('cy', s => s.values[i] == null ? -99 : y(s.values[i]));
          const body = opts.tip ? opts.tip(i) :
            `<b>${AR.fmtDate(labels[i])}</b><br>` + series.filter(s => s.values[i] != null).map(s =>
              `${series.length > 1 ? sw(s.color) + '<span class="dim">' + s.name + '</span> ' : ''}${yFmt(s.values[i])}`).join('<br>');
          const e = ev.touches ? ev.touches[0] : ev;
          AR.tip.show(body, e);
        })
        .on('mouseleave touchend', () => { hover.style('display', 'none'); AR.tip.hide(); });
    });
  };

  // ── BAR CHART (vertical) ─────────────────────────────────────────────────
  // AR.barChart(el, {
  //   labels: [...], values: [...], color,          single series, or
  //   stack: [{name, values, color}],               stacked series
  //   height, yFmt, xFmt:(label)=>tick text, tip:(i)=>html, colors:(v,i)=>color,
  //   onClick:(i)=>{}
  // })
  AR.barChart = function (el, opts) {
    const labels = opts.labels;
    const stack = opts.stack || [{ name: '', values: opts.values, color: opts.color || 'var(--accent)' }];
    const yFmt = opts.yFmt || (v => d3.format('~s')(v));
    const xFmt = opts.xFmt || (l => l);
    mount(el, opts, (svg, W, H) => {
      const m = Object.assign({ t: 12, r: 8, b: 24, l: 44 }, opts.margin);
      const totals = labels.map((_, i) => d3.sum(stack, s => s.values[i] || 0));
      if (!labels.length || !(d3.max(totals) > 0)) return emptyMsg(svg, W, H);
      const x = d3.scaleBand().domain(d3.range(labels.length)).range([m.l, W - m.r])
        .paddingInner(labels.length > 80 ? 0.1 : 0.22).paddingOuter(0.1);
      const y = d3.scaleLinear().domain([0, d3.max(totals) * 1.05]).range([H - m.b, m.t]);
      yAxis(svg, y, W, m, yFmt, opts.yTicks);

      const every = Math.max(1, Math.ceil(labels.length / Math.max(2, Math.floor((W - m.l - m.r) / 64))));
      svg.append('g').attr('class', 'axis').selectAll('text')
        .data(d3.range(labels.length).filter(i => (labels.length - 1 - i) % every === 0)).join('text')
        .attr('x', i => x(i) + x.bandwidth() / 2).attr('y', H - m.b + 15).attr('text-anchor', 'middle').text(i => xFmt(labels[i]));

      const bw = x.bandwidth(), r = Math.min(3, bw / 2);
      const base = new Array(labels.length).fill(0);
      stack.forEach((s, si) => {
        const top = si === stack.length - 1;
        svg.append('g').selectAll('path').data(d3.range(labels.length).filter(i => s.values[i] > 0)).join('path')
          .attr('fill', i => opts.colors ? opts.colors(s.values[i], i) : s.color)
          .attr('d', i => {
            const y0 = y(base[i]), y1 = y(base[i] + s.values[i]), h = Math.max(0, y0 - y1 - (si ? 1 : 0)), rr = top ? Math.min(r, h) : 0;
            return `M${x(i)},${y1 + h}V${y1 + rr}q0,${-rr} ${rr},${-rr}h${bw - 2 * rr}q${rr},0 ${rr},${rr}V${y1 + h}Z`;
          });
        s.values.forEach((v, i) => { base[i] += v || 0; });
      });

      // one full-height hit column per bar
      const hl = svg.append('rect').attr('fill', 'var(--text)').attr('opacity', 0.06).attr('y', m.t)
        .attr('height', H - m.t - m.b).attr('width', x.step()).style('display', 'none').style('pointer-events', 'none');
      svg.append('g').selectAll('rect').data(d3.range(labels.length)).join('rect')
        .attr('x', i => x(i) - (x.step() - bw) / 2).attr('y', m.t).attr('width', x.step()).attr('height', H - m.t - m.b)
        .attr('fill', 'transparent').style('cursor', opts.onClick ? 'pointer' : null)
        .on('mousemove', (ev, i) => {
          hl.style('display', null).attr('x', x(i) - (x.step() - bw) / 2);
          AR.tip.show(opts.tip ? opts.tip(i) :
            `<b>${labels[i]}</b><br>` + (stack.length > 1
              ? stack.filter(s => s.values[i]).map(s => `${sw(s.color)}<span class="dim">${s.name}</span> ${yFmt(s.values[i])}`).join('<br>')
              : yFmt(totals[i])), ev);
        })
        .on('mouseleave', () => { hl.style('display', 'none'); AR.tip.hide(); })
        .on('click', (ev, i) => opts.onClick && opts.onClick(i));
    });
  };

  // ── RANKED HORIZONTAL BARS ───────────────────────────────────────────────
  // AR.hbarChart(el, { labels, values, color, fmt, rowHeight, labelWidth, tip, colors:(v,i)=>color })
  AR.hbarChart = function (el, opts) {
    const { labels, values } = opts;
    const fmt = opts.fmt || (v => d3.format(',')(v));
    const rowH = opts.rowHeight || 26, padL = opts.labelWidth || 150;
    const o = Object.assign({}, opts, { height: Math.max(rowH, rowH * labels.length) });
    mount(el, o, (svg, W, H) => {
      if (!labels.length) return emptyMsg(svg, W, H);
      const lo = Math.min(0, d3.min(values)), hi = Math.max(0, d3.max(values));
      const x = d3.scaleLinear().domain([lo, hi || 1]).range([padL, W - 56]);
      const g = svg.selectAll('g').data(d3.range(labels.length)).join('g').attr('transform', i => `translate(0,${i * rowH})`);
      g.append('text').attr('class', 'cat-label').attr('x', padL - 8).attr('y', rowH / 2)
        .attr('text-anchor', 'end').attr('dominant-baseline', 'middle').text(i => labels[i]);
      g.append('rect').attr('x', i => Math.min(x(0), x(values[i]))).attr('y', 5)
        .attr('width', i => Math.max(1, Math.abs(x(values[i]) - x(0)))).attr('height', rowH - 10).attr('rx', 3)
        .attr('fill', i => opts.colors ? opts.colors(values[i], i) : (opts.color || 'var(--accent)'));
      g.append('text').attr('class', 'val-label').attr('x', i => Math.max(x(0), x(values[i])) + 6).attr('y', rowH / 2)
        .attr('dominant-baseline', 'middle').text(i => fmt(values[i]));
      g.append('rect').attr('width', W).attr('height', rowH).attr('fill', 'transparent')
        .on('mousemove', (ev, i) => AR.tip.show(opts.tip ? opts.tip(i) : `<b>${labels[i]}</b><br>${fmt(values[i])}`, ev))
        .on('mouseleave', AR.tip.hide);
    });
  };

  // ── SCATTER ──────────────────────────────────────────────────────────────
  // AR.scatter(el, {
  //   points: [{x, y, label, color}], color, height, xFmt, yFmt, xLabel, yLabel,
  //   fit: {slope, intercept},        optional regression line
  //   connect: true,                  join the points in order (trajectory)
  //   tip: (p)=>html, r
  // })
  AR.scatter = function (el, opts) {
    const pts = opts.points.filter(p => p.x != null && p.y != null && !isNaN(p.x) && !isNaN(p.y));
    const xFmt = opts.xFmt || (v => d3.format('~g')(+v.toFixed(2)));
    const yFmt = opts.yFmt || (v => d3.format('~g')(+v.toFixed(2)));
    const o = Object.assign({ height: 260 }, opts);
    mount(el, o, (svg, W, H) => {
      const m = Object.assign({ t: 14, r: 14, b: opts.xLabel ? 40 : 26, l: 48 }, opts.margin);
      if (!pts.length) return emptyMsg(svg, W, H);
      const ext = (a, f) => { let [lo, hi] = d3.extent(a, f); if (lo === hi) { lo -= 1; hi += 1; } const p = (hi - lo) * 0.08; return [lo - p, hi + p]; };
      const x = d3.scaleLinear().domain(opts.xDomain || ext(pts, p => p.x)).range([m.l, W - m.r]);
      const y = d3.scaleLinear().domain(opts.yDomain || ext(pts, p => p.y)).range([H - m.b, m.t]);
      yAxis(svg, y, W, m, yFmt, 5);
      svg.append('g').attr('class', 'grid').attr('transform', `translate(0,${H - m.b})`)
        .call(d3.axisBottom(x).ticks(Math.max(2, Math.floor((W - m.l - m.r) / 80))).tickSize(-(H - m.t - m.b)).tickFormat(xFmt).tickPadding(6));
      if (opts.xLabel) svg.append('text').attr('class', 'axis-title').attr('x', (m.l + W - m.r) / 2).attr('y', H - 6).attr('text-anchor', 'middle').text(opts.xLabel);
      if (opts.yLabel) svg.append('text').attr('class', 'axis-title').attr('x', m.l).attr('y', m.t - 4).text(opts.yLabel);
      if (opts.fit) {
        const [x0, x1] = x.domain(), f = v => opts.fit.slope * v + opts.fit.intercept;
        svg.append('line').attr('class', 'ref-line').attr('x1', x(x0)).attr('x2', x(x1)).attr('y1', y(f(x0))).attr('y2', y(f(x1)));
      }
      const col = p => p.color || opts.color || 'var(--accent)';
      if (opts.connect) svg.append('path').attr('fill', 'none').attr('stroke', opts.color || 'var(--accent)').attr('stroke-width', 1.5)
        .attr('opacity', 0.5).attr('d', d3.line().x(p => x(p.x)).y(p => y(p.y))(pts));
      svg.append('g').selectAll('circle').data(pts).join('circle').attr('class', 'ring')
        .attr('cx', p => x(p.x)).attr('cy', p => y(p.y)).attr('r', opts.r || 4.5).attr('fill', col)
        .attr('fill-opacity', pts.length > 150 ? 0.55 : 1);
      // nearest-point hover (no pinpoint targets)
      const focus = svg.append('circle').attr('r', (opts.r || 4.5) + 3).attr('fill', 'none').attr('stroke', 'var(--text)')
        .attr('stroke-width', 1.5).style('display', 'none').style('pointer-events', 'none');
      svg.append('rect').attr('x', m.l).attr('y', m.t).attr('width', Math.max(0, W - m.l - m.r)).attr('height', Math.max(0, H - m.t - m.b))
        .attr('fill', 'transparent')
        .on('mousemove', ev => {
          const [px, py] = d3.pointer(ev, svg.node());
          let best = null, bd = 30;
          pts.forEach(p => { const d = Math.hypot(x(p.x) - px, y(p.y) - py); if (d < bd) { bd = d; best = p; } });
          if (!best) { focus.style('display', 'none'); AR.tip.hide(); return; }
          focus.style('display', null).attr('cx', x(best.x)).attr('cy', y(best.y));
          AR.tip.show(opts.tip ? opts.tip(best) :
            `${best.label ? '<b>' + best.label + '</b><br>' : ''}${xFmt(best.x)} · ${yFmt(best.y)}`, ev);
        })
        .on('mouseleave', () => { focus.style('display', 'none'); AR.tip.hide(); });
    });
  };
})();
