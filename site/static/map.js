/* family.konecny.net — the map and the lifelines (/map/)

   Two ways of looking at the same years, sharing one year control. Everything is drawn on one canvas from
   /data/lives.json, which site/build.py writes from the published bundle: for each person shown in full, the years
   of birth and death and their dated events at a place that has coordinates. The living are not in that file.

   - Map: where everyone was in the year on the slider. A person is at the place of their latest dated record, and
     before their first located record at that first place. Long moves leave an arc that fades over the years after.
     The outlines (/static/atlas.json) are Natural Earth's and the US Census Bureau's, public domain, drawn here:
     no map service is asked for anything. Borders are today's.
   - Lifelines: every dated life as a bar on one time scale, grouped by line, with the world's events ruled across. */
(function () {
'use strict';
var app = document.getElementById('mx'); if (!app) return;
var $ = function (s) { return app.querySelector(s); };
var cv = $('#mx-cv'), ctx = cv.getContext('2d'), stage = $('#mx-stage');
var BASE = app.getAttribute('data-base') || '', EXT = BASE ? ' target="_blank" rel="noopener"' : '';      // empty on the site; the site's address in a stand-alone copy
var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
var D, PL = {}, PEO = [], BYID = {}, MOVES = [], LINES = [], lineOf = {}, land = null, lakes = null, borders = null;
var W = 0, H = 0, dpr = 1, col = {}, raf = 0, last = 0, hashT = 0, hashWant = '';
var clamp = function (v, a, b) { return v < a ? a : v > b ? b : v; };
var lerp = function (a, b, k) { return a + (b - a) * k; };
var esc = function (s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
var DISP = '"Bricolage Grotesque", "Helvetica Neue", Arial, sans-serif', MONO = '"IBM Plex Mono", ui-monospace, Menlo, monospace';
var S = { lens: 'map', year: 1913, y0: 1600, y1: 2000, playing: false, line: null, place: null, person: null, region: 'all', hits: [], touched: false, moved: false,
          t0: 1560, t1: 2030, scroll: 0, rows: [], lanes: 0, heads: [] };
var cam = { x: 0, y: 0, k: 300 }, camT = { x: 0, y: 0, k: 300 };
var REGIONS = [['all', 'Everywhere', [-124, 25, 21, 60]], ['america', 'North America', [-124, 25, -66, 50]], ['midwest', 'The Midwest', [-97.5, 37, -81, 48.5]],
               ['east', 'New England', [-80, 39, -68.5, 46.5]], ['europe', 'Europe', [-10.5, 45, 21, 58.5]], ['bohemia', 'Bohemia and Moravia', [12, 48.5, 18.9, 51.1]]];

/* ---------- the projection: an equal-area cone over the North Atlantic, turned so that north is up where you look ---------- */
var RAD = Math.PI / 180, P1 = 30 * RAD, P2 = 58 * RAD, L0 = -45 * RAD;
var PN = (Math.sin(P1) + Math.sin(P2)) / 2, PC = Math.cos(P1) * Math.cos(P1) + 2 * PN * Math.sin(P1), R0 = Math.sqrt(PC - 2 * PN * Math.sin(45 * RAD)) / PN;
function prj(lon, lat) { var r = Math.sqrt(Math.max(0, PC - 2 * PN * Math.sin(lat * RAD))) / PN, t = PN * (lon * RAD - L0); return [r * Math.sin(t), R0 - r * Math.cos(t)]; }
function turn(c) { return Math.atan2(c.x, R0 - c.y); }
function toScreen(p, c) { c = c || cam; var a = -turn(c), ca = Math.cos(a), sa = Math.sin(a), vx = p[0] - c.x, vy = p[1] - c.y; return [W / 2 + (vx * ca - vy * sa) * c.k, H / 2 - (vx * sa + vy * ca) * c.k]; }
function km(a, b) {                  // between two places, along the great circle
  var p = Math.PI / 180, h = Math.pow(Math.sin((b[2] - a[2]) * p / 2), 2) + Math.cos(a[2] * p) * Math.cos(b[2] * p) * Math.pow(Math.sin((b[1] - a[1]) * p / 2), 2);
  return 12742 * Math.asin(Math.sqrt(h));
}
function along(a, b, t) {            // a point on the great circle from place a to place b, as [lon, lat]
  var v = function (q) { var la = q[2] * RAD, lo = q[1] * RAD; return [Math.cos(la) * Math.cos(lo), Math.cos(la) * Math.sin(lo), Math.sin(la)]; }, A = v(a), B = v(b);
  var w = Math.acos(clamp(A[0] * B[0] + A[1] * B[1] + A[2] * B[2], -1, 1)); if (w < 1e-6) return [a[1], a[2]];
  var s1 = Math.sin((1 - t) * w) / Math.sin(w), s2 = Math.sin(t * w) / Math.sin(w), x = s1 * A[0] + s2 * B[0], y = s1 * A[1] + s2 * B[1], z = s1 * A[2] + s2 * B[2];
  return [Math.atan2(y, x) / RAD, Math.atan2(z, Math.sqrt(x * x + y * y)) / RAD];
}
function fitBox(b, snap) {           // look at a region: its middle in the middle of the room the controls leave, north up
  var c = prj((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), t = { x: c[0], y: c[1], k: 1 }, x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9, i, p;
  var top = 76, bot = (S.footH || 86) + 6, side = 14, save = [W, H];
  for (i = 0; i <= 8; i++) [[lerp(b[0], b[2], i / 8), b[1]], [lerp(b[0], b[2], i / 8), b[3]], [b[0], lerp(b[1], b[3], i / 8)], [b[2], lerp(b[1], b[3], i / 8)]].forEach(function (q) {
    W = 0; H = 0; p = toScreen(prj(q[0], q[1]), t); x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]); y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]);
  });
  W = save[0]; H = save[1];
  t.k = Math.min((W - side * 2) / (x1 - x0), (H - top - bot) / (y1 - y0));
  var dx = (x0 + x1) / 2 * t.k, dy = (y0 + y1) / 2 * t.k + (top - bot) / 2, a = turn(t), ca = Math.cos(a), sa = Math.sin(a), ux = dx / t.k, uy = -dy / t.k;
  camT.x = t.x + ux * ca - uy * sa; camT.y = t.y + ux * sa + uy * ca; camT.k = t.k;
  if (snap || reduce) { cam.x = camT.x; cam.y = camT.y; cam.k = camT.k; }
  go();
}

/* ---------- the data ---------- */
function prepare() {
  LINES = D.lines; LINES.forEach(function (l) { lineOf[l.slug] = l; });
  // one mark for one town: the database's several records of a town a few kilometres apart, and any two on the same spot,
  // are gathered under the one most used; a house number or a note in brackets is not part of a town's name
  var tidy = function (s) { return (s || '').replace(/^house\s+\d+[a-z]?\s+/i, '').replace(/\s+č\.?\s*p\.?\s*\d+[a-z]?$/i, '').replace(/\s*\(.*\)$/, ''); };
  var uses = {}, same = {}, reps = [];
  D.people.forEach(function (p) { p.e.forEach(function (e) { uses[e[1]] = (uses[e[1]] || 0) + 1; }); });
  Object.keys(D.places).sort(function (a, b) { return (uses[b] || 0) - (uses[a] || 0) || (a < b ? -1 : 1); }).forEach(function (id) {
    var p = D.places[id], me = [tidy(p[0]), p[2], p[1]];
    for (var i = 0; i < reps.length; i++) { var d = km(reps[i].q, me); if (d < 1.2 || (d < 30 && reps[i].q[0] === me[0])) { same[id] = reps[i].id; return; } }
    reps.push({ id: id, q: me }); same[id] = id; PL[id] = [me[0], me[1], me[2], prj(me[1], me[2])];      // [town, longitude, latitude, where that is on the page]
  });
  var lo = 9999, hi = 0, far = {};
  D.people.forEach(function (p) {
    var ev = p.e.map(function (e) { return [e[0], same[e[1]], e[2]]; }).sort(function (a, b) { return a[0] - b[0]; }), stops = [];
    ev.forEach(function (e) { if (p.d != null && e[0] > p.d) return; if (!stops.length || stops[stops.length - 1][1] !== e[1]) stops.push([e[0], e[1]]); });      // a burial does not move anyone
    if (!stops.length && ev.length) stops.push([ev[0][0], ev[0][1]]);
    var start = p.b != null ? p.b : ev.length ? ev[0][0] : p.d, end = p.d != null ? p.d : ev.length ? Math.max(ev[ev.length - 1][0], start) : p.b;
    var o = { id: p.id, n: p.n, c: lineOf[p.c] ? p.c : '', b: p.b, d: p.d, start: start, end: end, stops: stops };
    PEO.push(o); BYID[p.id] = o; lo = Math.min(lo, start); hi = Math.max(hi, end);
    for (var i = 1; i < stops.length; i++) {
      var a = PL[stops[i - 1][1]], b = PL[stops[i][1]], d = km(a, b); if (d < 40) continue;
      var pts = []; if (d > 300) for (var j = 0; j <= 24; j++) { var q = along(a, b, j / 24); pts.push(prj(q[0], q[1])); }
      MOVES.push({ p: o, y: stops[i][0], a: a, b: b, km: d, pts: pts }); if (d > 2000) far[stops[i][0]] = (far[stops[i][0]] || 0) + 1;
    }
  });
  S.y0 = Math.floor(lo / 10) * 10; S.y1 = Math.ceil(hi / 10) * 10; S.t0 = S.y0 - 8; S.t1 = S.y1 + 8;
  S.year = Math.round((S.y0 + S.y1) / 2);                  // to begin with: the year the most people crossed an ocean
  Object.keys(far).forEach(function (y) { if (!far[S.year] || far[y] > far[S.year]) S.year = +y; });
  layoutLives();
}
function shown(p) { return !S.line || p.c === S.line; }
function where(p, y) {               // where a person is in a year: a place, or on the way between two
  if (y < p.start || y >= p.end + 1 || !p.stops.length) return null;
  var st = p.stops, i = 0; while (i + 1 < st.length && st[i + 1][0] <= y) i++;
  if (i + 1 < st.length && st[i + 1][0] - y <= 1 && y >= st[0][0]) {
    var a = PL[st[i][1]], b = PL[st[i + 1][1]];
    if (km(a, b) >= 40) { var q = along(a, b, 1 - (st[i + 1][0] - y)); return { at: null, xy: prj(q[0], q[1]) }; }
  }
  return { at: st[i][1] };
}
function layoutLives() {             // each line's people in as few rows as hold them without touching
  var groups = LINES.map(function (l) { return { slug: l.slug, name: l.short, list: [] }; }).concat([{ slug: '', name: 'Others', list: [] }]), lane = 0;
  PEO.forEach(function (p) { if (p.start == null || !shown(p)) return; (groups.filter(function (g) { return g.slug === p.c; })[0] || groups[groups.length - 1]).list.push(p); });
  S.rows = []; S.heads = [];
  groups = groups.filter(function (g) { return g.list.length; });
  groups.forEach(function (g) { g.first = Math.min.apply(null, g.list.map(function (p) { return p.start; })); });
  groups.sort(function (a, b) { return a.first - b.first; });      // the line that goes furthest back at the top
  groups.forEach(function (g) {
    if (!g.list.length) return;
    g.list.sort(function (a, b) { return a.start - b.start || a.end - b.end; });
    var ends = []; S.heads.push({ lane: lane, name: g.name, slug: g.slug, n: g.list.length }); lane += 1.6;
    g.list.forEach(function (p) {
      var k = 0; while (k < ends.length && ends[k] + 4 > p.start - (p.b == null ? 12 : 0)) k++;
      ends[k] = p.end + (p.d == null ? 12 : 0); S.rows.push({ p: p, lane: lane + k });
    });
    lane += ends.length + 1.2;
  });
  S.lanes = lane;
}

/* ---------- drawing ---------- */
function mix(a, b, k) { var h = function (c) { var m = /^#(..)(..)(..)$/.exec(c); return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [128, 128, 128]; }, x = h(a), y = h(b); return 'rgb(' + [0, 1, 2].map(function (i) { return Math.round(lerp(x[i], y[i], k)); }).join(',') + ')'; }
function readTheme() {
  var cs = getComputedStyle(document.documentElement), g = function (n) { return cs.getPropertyValue(n).trim(); };
  col = { paper: g('--paper'), ink: g('--ink'), muted: g('--muted'), rule: g('--rule'), soft: g('--rule-soft'), card: g('--card'), sea: g('--sea'), neutral: g('--t-neutral') };
  LINES.forEach(function (l) { col[l.slug] = g('--t-' + l.slug) || g('--t-' + l.color) || col.neutral; });
  col.water = mix(col.paper, col.sea, 0.11); col.land = mix(col.paper, col.card, 0.75); col.coast = mix(col.rule, col.sea, 0.25);
}
var wCache = {};
function tw(font, text) { var k = font + '|' + text; if (!(k in wCache)) { ctx.font = font; wCache[k] = ctx.measureText(text).width; } return wCache[k]; }
function colourOf(p) { return col[p.c] || col.neutral; }
function label(t, x, y, font, fill, al, a) {
  ctx.font = font; ctx.textAlign = al || 'left'; ctx.textBaseline = 'middle'; ctx.globalAlpha = a == null ? 1 : a; ctx.lineJoin = 'round'; ctx.lineWidth = 3.2; ctx.strokeStyle = col.paper;
  ctx.strokeText(t, x, y); ctx.fillStyle = fill; ctx.fillText(t, x, y);
}
function draw() {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.globalAlpha = 1; S.hits = [];
  if (S.lens === 'map') drawMap(); else drawLives();
  ctx.globalAlpha = 1;
}
function drawMap() {
  var y = S.year, i;
  ctx.fillStyle = col.water; ctx.fillRect(0, 0, W, H);
  if (land) {
    ctx.save(); ctx.translate(W / 2, H / 2); ctx.scale(cam.k, -cam.k); ctx.rotate(-turn(cam)); ctx.translate(-cam.x, -cam.y);
    ctx.lineWidth = 1 / cam.k; ctx.lineJoin = 'round';
    ctx.fillStyle = col.land; ctx.fill(land); ctx.strokeStyle = col.coast; ctx.stroke(land);
    ctx.fillStyle = col.water; ctx.fill(lakes); ctx.stroke(lakes);
    ctx.globalAlpha = 0.55; ctx.strokeStyle = col.rule; ctx.stroke(borders);
    ctx.restore();
  }
  // long moves of the last years: an arc from the old place to the new, fading
  ctx.lineCap = 'round';
  MOVES.forEach(function (m) {
    var age = y - m.y; if (age < -1 || age > 16 || !m.pts.length || !shown(m.p)) return;
    var upTo = age < 0 ? Math.round((age + 1) * 24) : 24; if (upTo < 1) return;
    ctx.globalAlpha = (age < 0 ? 0.75 : 0.75 * (1 - age / 16)); ctx.strokeStyle = colourOf(m.p); ctx.lineWidth = 1.6;
    ctx.beginPath(); for (i = 0; i <= upTo; i++) { var q = toScreen(m.pts[i]); if (i) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); } ctx.stroke();
  });
  // who is where
  var at = {}, going = [], n = 0;
  PEO.forEach(function (p) {
    if (!shown(p)) return; var w = where(p, y); if (!w) return; n++;
    if (!w.at) { going.push([p, w.xy]); return; }
    var g = at[w.at] || (at[w.at] = { id: w.at, n: 0, by: {}, who: [] }); g.n++; g.by[p.c] = (g.by[p.c] || 0) + 1; g.who.push(p);
  });
  var spots = Object.keys(at).map(function (id) { var g = at[id], s = toScreen(PL[id][3]); g.x = s[0]; g.y = s[1]; return g; });
  var zoom = clamp(Math.pow(cam.k / (S.kAll || cam.k), 0.28), 1, 2.4);
  spots.sort(function (a, b) { return b.n - a.n; });
  spots.forEach(function (g) {
    if (g.x < -30 || g.x > W + 30 || g.y < -30 || g.y > H + 30) return;
    var r = (2.6 + 2 * Math.sqrt(g.n)) * zoom, a0 = -Math.PI / 2, keys = Object.keys(g.by);
    g.r = r; ctx.globalAlpha = 0.92;
    keys.forEach(function (k) { var a1 = a0 + Math.PI * 2 * g.by[k] / g.n; ctx.fillStyle = col[k] || col.neutral; ctx.beginPath(); ctx.moveTo(g.x, g.y); ctx.arc(g.x, g.y, r, a0, a1 + 0.02); ctx.closePath(); ctx.fill(); a0 = a1; });
    ctx.globalAlpha = 1; ctx.lineWidth = g.id === S.place ? 2.2 : 1; ctx.strokeStyle = g.id === S.place ? col.ink : col.paper; ctx.beginPath(); ctx.arc(g.x, g.y, r + (g.id === S.place ? 2.5 : 0), 0, 7); ctx.stroke();
    S.hits.push({ place: g.id, x: g.x, y: g.y, r: r });
  });
  going.forEach(function (t) { var s = toScreen(t[1]); ctx.globalAlpha = 1; ctx.fillStyle = colourOf(t[0]); ctx.beginPath(); ctx.arc(s[0], s[1], 3.4 * zoom, 0, 7); ctx.fill(); ctx.lineWidth = 1; ctx.strokeStyle = col.paper; ctx.stroke(); });
  // names of the places with the most people, wherever there is room
  var boxes = [{ x: 0, y: 0, w: Math.min(210, W * 0.55), h: 78 }, { x: W - 232, y: 0, w: 232, h: 52 }, { x: 0, y: H - (S.footH || 86) + 12, w: W, h: 90 }], shownNames = 0;
  var free = function (b) { for (var i = 0; i < boxes.length; i++) { var o = boxes[i]; if (b.x < o.x + o.w && b.x + b.w > o.x && b.y < o.y + o.h && b.y + b.h > o.y) return false; } return true; };
  spots.forEach(function (g) { if (g.r) boxes.push({ x: g.x - g.r, y: g.y - g.r, w: g.r * 2, h: g.r * 2 }); });
  spots.slice().sort(function (a, b) { return (b.id === S.place) - (a.id === S.place) || b.n - a.n; }).forEach(function (g) {
    if (!g.r || (shownNames >= (W < 700 ? 9 : 16) && g.id !== S.place)) return;
    var f = (g.id === S.place ? '700 ' : '600 ') + '11.5px ' + DISP, t = PL[g.id][0], w = tw(f, t) + 6, opts = [{ x: g.x + g.r + 3, y: g.y - 7, al: 'left' }, { x: g.x - g.r - 3 - w, y: g.y - 7, al: 'right' }, { x: g.x - w / 2, y: g.y - g.r - 16, al: 'center' }, { x: g.x - w / 2, y: g.y + g.r + 3, al: 'center' }];
    for (var j = 0; j < opts.length; j++) {
      var b = { x: opts[j].x, y: opts[j].y, w: w, h: 14 }; if (b.x < 2 || b.x + w > W - 2 || !free(b)) continue;
      boxes.push(b); shownNames++; label(t, opts[j].al === 'left' ? b.x + 3 : opts[j].al === 'right' ? b.x + w - 3 : b.x + w / 2, b.y + 7, f, col.ink, opts[j].al, 1); break;
    }
  });
  S.now = { n: n, spots: spots, going: going.length };
}
function drawLives() {
  var pad = 10, top = 118, bot = (S.footH || 86) + 4, x = function (t) { return pad + (t - S.t0) / (S.t1 - S.t0) * (W - pad * 2); }, pxy = (W - pad * 2) / (S.t1 - S.t0);
  var LH = clamp(pxy * 3.2, 8, 17), room = H - top - bot, maxScroll = Math.max(0, S.lanes * LH - room + 8);
  S.scroll = clamp(S.scroll, 0, maxScroll); S.LH = LH; S.pxy = pxy;
  var yOf = function (lane) { return top + lane * LH - S.scroll; };
  ctx.fillStyle = col.paper; ctx.fillRect(0, 0, W, H);
  // the years: a rule every ten, fifty or hundred, whichever has room
  var step = pxy * 10 >= 26 ? 10 : pxy * 50 >= 34 ? 50 : 100, t, xx;
  for (t = Math.ceil(S.t0 / 10) * 10; t <= S.t1; t += 10) {
    xx = Math.round(x(t)) + 0.5; var major = t % step === 0; if (!major && pxy * 10 < 7) continue;
    ctx.globalAlpha = major ? 0.9 : 0.5; ctx.strokeStyle = major ? col.rule : col.soft; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(xx, top - 8); ctx.lineTo(xx, H - bot); ctx.stroke();
  }
  D.events.forEach(function (e) { xx = Math.round(x(e.year)) + 0.5; if (xx < 0 || xx > W) return; ctx.globalAlpha = 0.75; ctx.strokeStyle = col.sea; ctx.setLineDash([5, 5]); ctx.beginPath(); ctx.moveTo(xx, top - 8); ctx.lineTo(xx, H - bot); ctx.stroke(); ctx.setLineDash([]); });
  // the lives
  var y = S.year, named = [];
  S.rows.forEach(function (r) {
    var p = r.p, yy = yOf(r.lane); if (yy < top - LH || yy > H - bot) return;
    var a = x(p.start), b = Math.max(x(p.end + 1), a + 2), alive = y >= p.start && y < p.end + 1, c = colourOf(p), h = Math.max(3, LH - 3), sel = p.id === S.person;
    if (b < -40 || a > W + 40) return;
    ctx.globalAlpha = alive || sel ? 1 : 0.42; ctx.fillStyle = c;
    if (p.b == null) { var g1 = ctx.createLinearGradient(a - 12 * pxy, 0, a, 0); g1.addColorStop(0, 'rgba(0,0,0,0)'); g1.addColorStop(1, c); ctx.fillStyle = g1; ctx.fillRect(a - 12 * pxy, yy, 12 * pxy, h); ctx.fillStyle = c; }
    if (p.d == null) { var g2 = ctx.createLinearGradient(b, 0, b + 12 * pxy, 0); g2.addColorStop(0, c); g2.addColorStop(1, 'rgba(0,0,0,0)'); ctx.fillStyle = g2; ctx.fillRect(b, yy, 12 * pxy, h); ctx.fillStyle = c; }
    ctx.fillRect(a, yy, b - a, h);
    if (sel) { ctx.globalAlpha = 1; ctx.strokeStyle = col.ink; ctx.lineWidth = 2; ctx.strokeRect(a - 2, yy - 2, b - a + 4, h + 4); }
    if (LH >= 13) { var f = '600 9.5px ' + DISP, nm = p.n; if (tw(f, nm) + 8 <= b - a) named.push([nm, Math.max(a, 2) + 4, yy + h / 2 + 0.5, f]); }
    S.hits.push({ person: p.id, x0: a - (p.b == null ? 12 * pxy : 0), x1: b + (p.d == null ? 12 * pxy : 0), y0: yy - 1.5, y1: yy + h + 1.5 });
  });
  ctx.globalAlpha = 1; ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
  named.forEach(function (n) { if (n[1] > W) return; ctx.font = n[3]; ctx.fillStyle = col.paper; ctx.fillText(n[0], n[1], n[2]); });
  // the line names, and the head of the scale
  S.heads.forEach(function (hd) { var yy = yOf(hd.lane) + LH * 0.6; if (yy < top || yy > H - bot) return; label((hd.name + '  ' + hd.n).toUpperCase(), pad, yy, '600 condensed 10.5px ' + DISP, col[hd.slug] || col.muted, 'left', 1); });
  ctx.globalAlpha = 1; ctx.fillStyle = col.paper; ctx.fillRect(0, 0, W, top - 8);
  var lastX = -99;
  for (t = Math.ceil(S.t0 / step) * step; t <= S.t1; t += step) { xx = x(t); if (xx < 26 || xx > W - 14 || xx - lastX < 30) continue; lastX = xx; ctx.globalAlpha = 0.9; ctx.font = '500 10px ' + MONO; ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic'; ctx.fillStyle = col.muted; ctx.fillText(String(t), xx, top - 12); }
  // the year you are looking at
  xx = Math.round(x(y)) + 0.5; ctx.globalAlpha = 1; ctx.strokeStyle = col.ink; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(xx, top - 8); ctx.lineTo(xx, H - bot); ctx.stroke();
  ctx.fillStyle = col.ink; ctx.beginPath(); ctx.moveTo(xx - 5, top - 14); ctx.lineTo(xx + 5, top - 14); ctx.lineTo(xx, top - 7); ctx.closePath(); ctx.fill();
  if (maxScroll > 0) { ctx.globalAlpha = 0.35; ctx.fillStyle = col.muted; ctx.fillRect(W - 4, top + room * S.scroll / (maxScroll + room), 3, room * room / (maxScroll + room)); }
  var n = 0, by = {}; S.rows.forEach(function (r) { if (y >= r.p.start && y < r.p.end + 1) { n++; by[r.p.c] = (by[r.p.c] || 0) + 1; } });
  S.now = { n: n, by: by };
}

/* ---------- animation, the year, the panel ---------- */
function go() { if (!raf) { last = 0; raf = requestAnimationFrame(tick); } }
function tick(now) {
  raf = 0; var dt = last ? Math.min(0.05, (now - last) / 1000) : 0.016, busy = false, k; last = now;
  if (S.playing) { S.year += dt * (S.speed || 7); if (S.year >= S.y1) { S.year = S.y1; S.playing = false; sync(); } busy = true; }
  var e = reduce ? 1 : 1 - Math.exp(-dt / 0.16);
  for (k in camT) { var d = camT[k] - cam[k]; if (Math.abs(d) > Math.abs(camT[k]) * 1e-4 + 1e-7) { cam[k] += d * e; busy = true; } else cam[k] = camT[k]; }
  draw(); hud();
  if (busy) raf = requestAnimationFrame(tick);
}
var shownV = {}, lastKey = '';
function put(sel, prop, val) { if (shownV[sel + prop] !== val) { shownV[sel + prop] = val; $(sel)[prop] = val; } }
function hud() {
  var y = Math.floor(S.year), ev = null; D.events.forEach(function (e) { if (Math.abs(e.year - y) <= 2) ev = e; });
  put('#mx-yr', 'textContent', String(y));
  var n = S.now ? S.now.n : 0, who = n === 1 ? '1 person' : n + ' people';
  put('#mx-yrsub', 'textContent', ev ? ev.year + ' · ' + ev.label : !S.touched ? 'Press Play, or slide the year' : S.lens === 'map' ? who + ' on the map' : who + ' alive');
  if (!S.sliding) put('#mx-slide', 'value', String(y));
  var key = S.lens + y + '|' + S.place + '|' + S.person + '|' + S.line + '|' + n; if (key !== lastKey) { lastKey = key; panel(); }
  var h = '#' + S.lens + '-' + y;                           // the address follows the year, once it has settled
  if (h !== hashWant) { hashWant = h; clearTimeout(hashT); hashT = setTimeout(function () { try { if (location.hash !== hashWant) history.replaceState(null, '', hashWant); } catch (e2) {} }, S.playing ? 900 : 250); }
}
function personChip(p) { return '<a href="' + BASE + '/person/' + p.id + '/"' + EXT + ' style="--thread: var(--t-' + (p.c || 'neutral') + ', var(--t-neutral))">' + esc(p.n) + '</a>'; }
function span(p) { return (p.b != null ? p.b : '?') + '–' + (p.d != null ? p.d : '?'); }
function panel() {
  var y = Math.floor(S.year), h = '', now = S.now || { n: 0, spots: [], by: {} };
  if (S.lens === 'map' && S.place && PL[S.place]) {
    var g = (now.spots || []).filter(function (s) { return s.id === S.place; })[0], who = g ? g.who : [];
    h += '<div class="tx-who"><span class="tx-band"></span><div><p class="eyebrow">In ' + y + ' · ' + (who.length === 1 ? '1 person here' : who.length + ' people here') + '</p><h2><a href="' + BASE + '/place/' + S.place + '/"' + EXT + '>' + esc(PL[S.place][0]) + '</a></h2></div></div>';
    h += '<div class="tx-rest mx-rest"><div class="mx-who">' + (who.length ? who.slice().sort(function (a, b) { return a.start - b.start; }).map(function (p) { return personChip(p) + ' <small>' + span(p) + '</small>'; }).join('') : '<p class="tx-note">No one on record here in ' + y + '. Slide the year to find when they were.</p>') + '</div>';
    h += '<p class="tx-about"><button type="button" class="tx-link" data-clear>Back to everywhere in ' + y + '</button></p></div>';
  } else if (S.lens === 'lives' && S.person && BYID[S.person]) {
    var p = BYID[S.person], l = lineOf[p.c], st = p.stops.map(function (s) { return PL[s[1]][0]; }).filter(function (s, i, a) { return s && a.indexOf(s) === i; });
    h += '<div class="tx-who" style="--thread: var(--t-' + (p.c || 'neutral') + ', var(--t-neutral))"><span class="tx-band"></span><div><p class="eyebrow">' + (l ? esc(l.short) + ' line' : 'A life') + '</p><h2><a href="' + BASE + '/person/' + p.id + '/"' + EXT + '>' + esc(p.n) + '</a></h2>';
    h += '<p class="tx-span">' + span(p) + (st.length ? ' <span class="tx-place">' + esc(st.slice(0, 3).join(' → ')) + '</span>' : '') + '</p></div></div>';
    h += '<div class="tx-acts"><a class="tx-btn tx-alt" href="' + BASE + '/person/' + p.id + '/"' + EXT + '>Open their page</a><a class="tx-btn tx-alt" href="' + BASE + '/tree/#long-' + p.id + '"' + EXT + '>In the tree</a>' + (p.stops.length ? '<button type="button" class="tx-btn tx-alt" data-onmap>On the map</button>' : '') + '</div>';
    h += '<div class="tx-rest mx-rest"><p class="tx-about"><button type="button" class="tx-link" data-clear>Back to everyone alive in ' + y + '</button></p></div>';
  } else if (S.lens === 'map') {
    h += '<div class="tx-who"><span class="tx-band"></span><div><p class="eyebrow">On the map in ' + y + '</p><h2>' + (now.n === 1 ? '1 person' : now.n + ' people') + ' in ' + (now.spots.length === 1 ? '1 place' : now.spots.length + ' places') + '</h2></div></div>';
    h += '<div class="tx-rest mx-rest"><div class="tx-kin"><div><dd>' + now.spots.slice(0, 14).map(function (s) { return '<button type="button" data-place="' + s.id + '">' + esc(PL[s.id][0]) + ' <small>' + s.n + '</small></button>'; }).join('') + '</dd></div></div>';
    h += '<p class="tx-about">Each person is shown at the place of their latest dated record, and before their first located record at that first place. A long move leaves an arc for a few years. Coastlines and borders are today’s.</p>';
    h += '<p class="tx-about">The living are not on the map: this site shows no dates or places for them. ' + D.people.length + ' people have a date on record; ' + PEO.filter(function (p) { return p.stops.length; }).length + ' of them a place the map can find.</p></div>';
  } else {
    h += '<div class="tx-who"><span class="tx-band"></span><div><p class="eyebrow">Alive in ' + y + '</p><h2>' + (now.n === 1 ? '1 person' : now.n + ' people') + '</h2></div></div>';
    h += '<div class="tx-rest mx-rest"><div class="tx-kin"><div><dd>' + LINES.concat([{ slug: '', short: 'Others' }]).filter(function (l) { return now.by[l.slug]; }).map(function (l) { return '<button type="button" data-line="' + l.slug + '" style="--thread: var(--t-' + (l.slug || 'neutral') + ', var(--t-neutral))">' + esc(l.short) + ' <small>' + now.by[l.slug] + '</small></button>'; }).join('') + '</dd></div></div>';
    h += '<p class="tx-about">Every life with a date on record, birth to death, in the colour of its line. A bar that fades at one end has no date there. Tap a life for the person; drag to move through the years, pinch or use + and − to stretch them.</p>';
    h += '<p class="tx-about">The living are not here: this site shows no dates for them.</p></div>';
  }
  $('#mx-panel').innerHTML = h;
}
function chips() {
  var h = '';
  if (S.lens === 'map') { h += '<span class="tx-lbl">Go to</span>'; REGIONS.forEach(function (r) { h += '<button type="button" class="tx-chip mx-reg" data-region="' + r[0] + '" aria-pressed="' + (S.region === r[0]) + '">' + r[1] + '</button>'; }); }
  h += '<span class="tx-lbl">' + (S.lens === 'map' ? 'Show' : 'Show only') + '</span>';
  LINES.forEach(function (l) { h += '<button type="button" class="tx-chip" data-line="' + l.slug + '" aria-pressed="' + (S.line === l.slug) + '" style="--thread: var(--t-' + l.slug + ', var(--t-' + l.color + ', var(--t-neutral)))"><i></i>' + esc(l.short) + '</button>'; });
  $('#mx-chips').innerHTML = h;
}
function sync() {
  app.querySelectorAll('[data-lens]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-lens') === S.lens)); });
  $('#mx-play').textContent = S.playing ? 'Pause' : 'Play'; $('#mx-play').setAttribute('aria-pressed', String(S.playing));
  $('#mx-fit').textContent = S.lens === 'map' ? 'Fit' : 'All years';
  cv.setAttribute('aria-label', S.lens === 'map' ? 'A map of where the family was in the year on the slider. The places and the people in them are listed after it.' : 'Every dated life as a bar on one time scale. The people alive in the year on the slider are counted after it.');
  chips(); lastKey = ''; go();
}
function setYear(y) { S.year = clamp(y, S.y0, S.y1); go(); }
function setLens(l) { S.lens = l; S.playing = false; if (l === 'map' && !S.moved) region(S.region, true); sync(); }
function region(id, snap) { var r = REGIONS.filter(function (x) { return x[0] === id; })[0] || REGIONS[0]; S.region = r[0]; S.moved = false; fitBox(r[2], snap); if (r[0] === 'all') S.kAll = camT.k; chips(); }
function setLine(slug) { S.line = S.line === slug ? null : slug; S.person = null; layoutLives(); sync(); }
function zoomBy(f, px, py) {
  if (S.lens === 'lives') { var pad = 10, t = S.t0 + ((px == null ? W / 2 : px) - pad) / (W - pad * 2) * (S.t1 - S.t0), w = clamp((S.t1 - S.t0) / f, 30, S.y1 - S.y0 + 16); S.t0 = t - (t - S.t0) * w / (S.t1 - S.t0); S.t1 = S.t0 + w; go(); return; }
  px = px == null ? W / 2 : px; py = py == null ? H / 2 : py;
  var k1 = clamp(camT.k * f, (S.kAll || 100) * 0.6, (S.kAll || 100) * 260), a = turn(camT), ca = Math.cos(a), sa = Math.sin(a), move = function (k, s) { var ux = (px - W / 2) / k * s, uy = -(py - H / 2) / k * s; camT.x += ux * ca - uy * sa; camT.y += ux * sa + uy * ca; };
  move(camT.k, 1); camT.k = k1; move(k1, -1); S.moved = true; S.region = ''; chips(); go();
}

/* ---------- controls ---------- */
function resize() {
  var r = stage.getBoundingClientRect(); if (r.width < 10 || r.height < 10) return;
  dpr = Math.min(window.devicePixelRatio || 1, 2.5); W = r.width; H = r.height; cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  if (!D) return;
  var keep = S.region; fitBox(REGIONS[0][2], true); S.kAll = camT.k; if (!S.moved) region(keep || 'all', true); go();
}
function hit(x, y) {
  var best = null, bd = 1e9;
  S.hits.forEach(function (h) {
    if (h.person) { if (x >= h.x0 && x <= h.x1 && y >= h.y0 - 3 && y <= h.y1 + 3) { var d = Math.abs(y - (h.y0 + h.y1) / 2); if (d < bd) { bd = d; best = h; } } return; }
    var d2 = Math.hypot(h.x - x, h.y - y); if (d2 <= Math.max(h.r + 8, 16) && d2 < bd) { bd = d2; best = h; }
  });
  return best;
}
function wire() {
  var ptr = {}, drag = null, pinch = 0;
  var touched = function () { if (!S.touched) { S.touched = true; go(); } };
  cv.addEventListener('pointerdown', function (e) {
    cv.setPointerCapture(e.pointerId); ptr[e.pointerId] = { x: e.offsetX, y: e.offsetY }; var ids = Object.keys(ptr);
    if (ids.length === 1) drag = { x: e.offsetX, y: e.offsetY, sx: e.offsetX, sy: e.offsetY, moved: false };
    else if (ids.length === 2) { var a = ptr[ids[0]], b = ptr[ids[1]]; pinch = Math.hypot(a.x - b.x, a.y - b.y); drag = null; }
    touched();
  });
  cv.addEventListener('pointermove', function (e) {
    if (!ptr[e.pointerId]) return; ptr[e.pointerId] = { x: e.offsetX, y: e.offsetY }; var ids = Object.keys(ptr);
    if (ids.length === 2 && pinch) { var a = ptr[ids[0]], b = ptr[ids[1]], d = Math.hypot(a.x - b.x, a.y - b.y); if (d > 0) { zoomBy(d / pinch, (a.x + b.x) / 2, (a.y + b.y) / 2); cam.k = camT.k; cam.x = camT.x; cam.y = camT.y; pinch = d; } return; }
    if (!drag) return;
    var dx = e.offsetX - drag.x, dy = e.offsetY - drag.y; if (!drag.moved && Math.hypot(e.offsetX - drag.sx, e.offsetY - drag.sy) < 6) return;
    drag.moved = true; cv.classList.add('tx-drag'); drag.x = e.offsetX; drag.y = e.offsetY;
    if (S.lens === 'lives') { var dt = dx / S.pxy; S.t0 -= dt; S.t1 -= dt; S.scroll -= dy; go(); return; }
    var an = turn(camT), ca = Math.cos(an), sa = Math.sin(an), ux = -dx / cam.k, uy = dy / cam.k;
    camT.x += ux * ca - uy * sa; camT.y += ux * sa + uy * ca; cam.x = camT.x; cam.y = camT.y; S.moved = true; if (S.region) { S.region = ''; chips(); } go();
  });
  function up(e) {
    var d = drag; delete ptr[e.pointerId]; if (Object.keys(ptr).length < 2) pinch = 0; cv.classList.remove('tx-drag');
    if (!d || d.moved || Object.keys(ptr).length) { drag = null; return; }
    drag = null; var h = hit(e.offsetX, e.offsetY);
    if (S.lens === 'map') { S.place = h ? h.place : null; lastKey = ''; go(); return; }
    if (h) S.person = h.person; else { S.person = null; S.playing = false; setYear(Math.round(S.t0 + (e.offsetX - 10) / S.pxy)); sync(); }
    lastKey = ''; go();
  }
  cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', function (e) { delete ptr[e.pointerId]; drag = null; pinch = 0; cv.classList.remove('tx-drag'); });
  cv.addEventListener('wheel', function (e) {
    e.preventDefault(); touched(); var dy = e.deltaMode === 1 ? e.deltaY * 30 : e.deltaY, dx = e.deltaMode === 1 ? e.deltaX * 30 : e.deltaX;
    if (S.lens === 'map' || e.ctrlKey || e.metaKey) { zoomBy(Math.exp(-dy * (e.ctrlKey || e.metaKey ? 0.01 : 0.0022)), e.offsetX, e.offsetY); return; }
    S.scroll += dy; var dt = dx / S.pxy; S.t0 += dt; S.t1 += dt; go();
  }, { passive: false });
  cv.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return; var k = e.key, used = true;
    if (k === 'ArrowLeft') { S.playing = false; setYear(Math.floor(S.year) - (e.shiftKey ? 10 : 1)); sync(); }
    else if (k === 'ArrowRight') { S.playing = false; setYear(Math.floor(S.year) + (e.shiftKey ? 10 : 1)); sync(); }
    else if (k === 'ArrowUp' && S.lens === 'lives') { S.scroll -= 40; go(); } else if (k === 'ArrowDown' && S.lens === 'lives') { S.scroll += 40; go(); }
    else if (k === '+' || k === '=') zoomBy(1.3); else if (k === '-') zoomBy(1 / 1.3);
    else if (k === ' ' || k === 'Enter') $('#mx-play').click();
    else used = false;
    if (used) { e.preventDefault(); touched(); }
  });
  app.querySelectorAll('[data-lens]').forEach(function (b) { b.addEventListener('click', function () { touched(); setLens(b.getAttribute('data-lens')); }); });
  $('#mx-play').addEventListener('click', function () { touched(); S.playing = !S.playing; if (S.playing && S.year >= S.y1 - 0.5) S.year = S.y0; if (S.playing && reduce) { S.playing = false; setYear(Math.floor(S.year) + 25); } sync(); });
  $('#mx-zin').addEventListener('click', function () { zoomBy(1.5); }); $('#mx-zout').addEventListener('click', function () { zoomBy(1 / 1.5); });
  $('#mx-fit').addEventListener('click', function () { if (S.lens === 'map') region('all'); else { S.t0 = S.y0 - 8; S.t1 = S.y1 + 8; S.scroll = 0; go(); } });
  $('#mx-slide').addEventListener('input', function () { S.sliding = true; S.playing = false; touched(); setYear(+this.value); sync(); });
  $('#mx-slide').addEventListener('change', function () { S.sliding = false; });
  $('#mx-chips').addEventListener('click', function (e) {
    var b = e.target.closest('[data-region]'); if (b) { touched(); region(b.getAttribute('data-region')); return; }
    b = e.target.closest('[data-line]'); if (b) { touched(); setLine(b.getAttribute('data-line')); }
  });
  $('#mx-panel').addEventListener('click', function (e) {
    var b = e.target.closest('[data-place]'); if (b) { S.place = b.getAttribute('data-place'); var p = PL[S.place]; if (S.region !== 'all' || S.moved) { camT.x = p[3][0]; camT.y = p[3][1]; S.moved = true; } lastKey = ''; go(); return; }
    b = e.target.closest('[data-line]'); if (b) { setLine(b.getAttribute('data-line')); return; }
    if (e.target.closest('[data-clear]')) { S.place = null; S.person = null; lastKey = ''; go(); return; }
    if (e.target.closest('[data-onmap]')) { var q = BYID[S.person], w = where(q, S.year) || where(q, q.stops[0][0]); S.lens = 'map'; if (!where(q, S.year)) S.year = q.stops[0][0]; S.place = w && w.at ? w.at : q.stops[0][1]; S.person = null; region('all'); sync(); }
  });
  new MutationObserver(function () { readTheme(); wCache = {}; go(); }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class', 'style'] });
  var mq = matchMedia('(prefers-color-scheme: dark)'), onScheme = function () { readTheme(); go(); };
  if (mq.addEventListener) mq.addEventListener('change', onScheme); else if (mq.addListener) mq.addListener(onScheme);
  new ResizeObserver(resize).observe(stage);
  new ResizeObserver(function () { S.footH = $('.tx-foot').offsetHeight; go(); }).observe($('.tx-foot'));
  window.addEventListener('hashchange', function () { if (route()) sync(); });
}
function route() {                   // #map-1913 and #lives-1913: the view and the year
  var m = /^#(map|lives)-(\d{4})$/.exec(location.hash || ''); if (!m) return false;
  S.lens = m[1]; S.year = clamp(+m[2], S.y0, S.y1); return true;
}
function shapes(a) {                 // the outlines, unpacked and projected once
  var path = function (rings, close) {
    var p = new Path2D();
    rings.forEach(function (r) { var x = r[0], y = r[1], q = prj(x / 100, y / 100); p.moveTo(q[0], q[1]); for (var i = 2; i < r.length; i += 2) { x += r[i]; y += r[i + 1]; q = prj(x / 100, y / 100); p.lineTo(q[0], q[1]); } if (close) p.closePath(); });
    return p;
  };
  land = path(a.land, true); lakes = path(a.lakes, true); borders = path(a.lines, false); go();
}
function start(data) {
  D = data; prepare(); readTheme(); route();
  $('#mx-slide').min = S.y0; $('#mx-slide').max = S.y1; $('#mx-end').textContent = String(S.y1);
  resize(); wire(); sync();
  fetch(app.getAttribute('data-atlas')).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(shapes).catch(function (err) { if (window.console) console.error(err); });
  if (document.fonts && document.fonts.load) Promise.all(['600 11.5px "Bricolage Grotesque"', '500 10px "IBM Plex Mono"'].map(function (f) { return document.fonts.load(f, 'Mostiště'); })).then(function () { wCache = {}; go(); }).catch(function () {});
}
if (app.hasAttribute('data-debug')) window.__mx = { S: S, cam: cam, camT: camT, where: where, people: function () { return PEO; }, places: function () { return PL; } };
fetch(app.getAttribute('data-src')).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(start).catch(function (err) {
  $('#mx-panel').innerHTML = '<p class="tx-note">The map did not load (' + esc(err.message || err) + '). Reload the page to try again.</p>';
  if (window.console) console.error(err);
});
})();
