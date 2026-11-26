/* family.konecny.net — the tree explorer (/tree/)

   One scene, three ways of looking at it. Everything is drawn on one canvas from /data/tree.json, which
   site/build.py writes from the published bundle: names and family links for everyone, and years, towns and
   review states only for people the site shows in full.

   - Long view: older is always up, younger always down. The person you are with is the largest; their ancestors
     recede above them, each generation sharing the width of the view (father's side left, mother's side right),
     and the descendants of the person at the centre spread below. The view narrows to the branch you walk into.
   - Tree: the same people from straight above, ancestors up and descendants down, on a true time scale.
   - Fan: ancestors in rings, each wedge sized by how much of that branch is known.

   Living people and children under 18 have no public dates. They are placed by generation from their nearest
   dated relatives, and no year line is drawn from the first of them onward. Nothing here reads the full bundle. */
(function () {
'use strict';
var app = document.getElementById('tx'); if (!app) return;
var $ = function (s) { return app.querySelector(s); };
var BASE = app.getAttribute('data-base') || '';          // empty on the site; the site's address in a stand-alone copy
var EXT = BASE ? ' target="_blank" rel="noopener"' : '';
var cv = $('#tx-cv'), ctx = cv.getContext('2d'), stage = $('#tx-stage');
var TURN = $('#tx-turn') ? '#tx-turn' : '#tx-desc';      // the turn button's label; a page kept from before it had one still works
var TNOW = 2030;                 // depth is measured back from here; nothing is drawn against it
var KZ = 0.15;                   // world units per year (one unit sideways = one person)
var GEN = 28;                    // nominal years per generation, used only where no date is public
var RW = 1, R0 = 0.95, FAN_SPAN = 290 * Math.PI / 180;
var TOP = Math.PI / 2;
var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
var D, P, F, pathLine = {}, lineOf = {};
var W = 0, H = 0, dpr = 1, col = {}, raf = 0, last = 0;
var clamp = function (v, a, b) { return v < a ? a : v > b ? b : v; };
var lerp = function (a, b, k) { return a + (b - a) * k; };
var smooth = function (a, b, v) { var k = clamp((v - a) / (b - a), 0, 1); return k * k * (3 - 2 * k); };
var fold = function (s) { return s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase(); };
var esc = function (s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
var wz = function (t) { return (TNOW - t) * KZ; };

var S = { focus: null, sel: null, lens: 'long', back: true, mode: 'lines', nodes: {}, list: [], anc: null, desc: null, path: [], onPath: {}, stations: [],
          fanMix: 0, fanT: 0, aAnc: 1, aDesc: 1, tween: 1, fade: 1, flying: false, panX: 0, win: { L: 0, w: 1 }, liveFrom: 1950, tMin: 1500, tMax: 2000, line: null, hits: [], touched: false };
var cam = { tx: 0, tz: 0, s: 60, theta: TOP, F: 1000, yT: 300 }, camT = { tx: 0, tz: 0, s: 60, theta: TOP, F: 1000, yT: 300 };
var longP = { theta: 0.2, F: 1000, yT: 300, s: 170 };

/* ---------- the family graph ---------- */
function kidId(k) { return typeof k === 'string' ? k : k[0]; }
function kidRel(k) { return typeof k === 'string' ? '' : (k[1] || k[2]); }
function birthFam(pid) {            // the family a person was born into; an adoptive family only if it is the only one
  var p = P[pid], first = null;
  for (var i = 0; i < p.pf.length; i++) {
    var f = F[p.pf[i]]; if (!f) continue;
    if (!first) first = p.pf[i];
    var k = f.k.filter(function (k) { return kidId(k) === pid; })[0];
    if (!k || !kidRel(k)) return p.pf[i];
  }
  return first;
}
function parents(pid) { var fid = birthFam(pid); return fid ? [F[fid].a, F[fid].b] : [null, null]; }
function children(pid) { var out = []; P[pid].fs.forEach(function (fid) { F[fid].k.forEach(function (k) { var id = kidId(k); if (out.indexOf(id) < 0) out.push(id); }); }); return out; }
function partners(pid) { var out = []; P[pid].fs.forEach(function (fid) { var f = F[fid], o = f.a === pid ? f.b : f.a; if (o && out.indexOf(o) < 0) out.push(o); }); return out; }
function siblings(pid) { var fid = birthFam(pid); return fid ? F[fid].k.map(kidId).filter(function (x) { return x !== pid; }) : []; }
function shortName(p) { if (!p.f || !p.s) return p.n; return p.f.split(' ')[0] + ' ' + p.s; }            // in the picture: first name, birth surname
function kinName(p) { if (!p.f || !p.m) return shortName(p); return p.f.split(' ')[0] + (p.s ? ' (' + p.s + ') ' : ' ') + p.m; }   // in lists: as the site shows a married name
function place(s) { return (s || '').replace(/^house\s+\d+[a-z]?\s+/i, '').replace(/\s+č\.?\s*p\.?\s*\d+[a-z]?$/i, '').replace(/\s*\(.*\)$/, ''); }
function years(p) { if (p.st !== 'f') return 'name only'; if (!p.bt && !p.dt) return 'dates unknown'; return (p.bt || '?') + '–' + (p.dt || '?'); }
function lineColour(pid) { return pathLine[pid] || P[pid].c || 'neutral'; }
var kinMemo = {};
function kinCount(id, back) {       // how many people are on record before (back) or after someone
  var key = (back ? 'a' : 'd') + id;
  if (!(key in kinMemo)) {
    var seen = {}, q = [id], n = 0;
    while (q.length) (back ? parents(q.pop()) : children(q.pop())).forEach(function (k) { if (k && !seen[k]) { seen[k] = 1; n++; q.push(k); } });
    kinMemo[key] = n;
  }
  return kinMemo[key];
}

/* Where a person sits in time: the year of birth when it is public. Living people and undated people have no
   public year, so they are placed by generation from the nearest dated relatives and never drawn against a year. */
var tMemo = {};
function tOf(pid, depth) {
  if (pid in tMemo) return tMemo[pid];
  var p = P[pid], t = null; depth = depth || 0;
  if (p.by) t = p.by;
  else {
    tMemo[pid] = null;
    if (p.st === 'f') partners(pid).some(function (q) { if (P[q].by) { t = P[q].by + (p.g === 'M' ? -2 : p.g === 'F' ? 2 : 0); return true; } });
    if (t == null && depth < 40) {
      var ts = parents(pid).filter(Boolean).map(function (x) { return tOf(x, depth + 1); }).filter(function (v) { return v != null; });
      if (ts.length) t = ts.reduce(function (a, b) { return a + b; }, 0) / ts.length + GEN;
    }
    if (t == null) { var ks = children(pid).map(function (k) { return P[k].by; }).filter(Boolean); if (ks.length) t = Math.min.apply(null, ks) - GEN + 3; }
    if (t == null && p.dy) t = p.dy - 62;
  }
  tMemo[pid] = t; return t;
}

/* ---------- building the scene around one person ---------- */
function buildAnc(focus) {
  var root = { id: focus, gen: 0, par: [null, null], child: null, kids: [] }, seen = {}, list = [root], q = [root];
  seen[focus] = 1;
  while (q.length) {
    var n = q.shift(), pr = parents(n.id);
    n.dup = [false, false];
    for (var i = 0; i < 2; i++) {
      var pid = pr[i]; if (!pid) continue;
      if (seen[pid]) { n.dup[i] = true; continue; }     // the same ancestor by a second route: drawn once
      seen[pid] = 1;
      var m = { id: pid, gen: n.gen + 1, par: [null, null], child: n, toward: n, side: i, kind: 'anc' };
      n.par[i] = m; list.push(m); q.push(m);
    }
  }
  (function reach(n) { var r = 0; n.par.forEach(function (q) { if (q) r = Math.max(r, 1 + reach(q)); }); n.reach = r; return r; })(root);
  return { root: root, list: list };
}
function buildDesc(focus) {
  var root = { id: focus, gen: 0, kids: [], parent: null }, seen = {}, list = [root];
  seen[focus] = 1;
  (function rec(n) {
    children(n.id).forEach(function (k) {
      if (seen[k]) return; seen[k] = 1;
      var m = { id: k, gen: n.gen - 1, kids: [], parent: n, toward: n, kind: 'desc', par: [null, null] };
      n.kids.push(m); list.push(m); rec(m);
    });
  })(root);
  (function reach(n) { var r = 0; n.kids.forEach(function (k) { r = Math.max(r, 1 + reach(k)); }); n.reach = r; return r; })(root);
  return { root: root, list: list };
}
function layoutAnc(root) {            // tidy tree: father left, mother right, couples side by side, subtrees as close as they fit
  (function rec(n) {
    var f = n.par[0], m = n.par[1], i;
    if (f) rec(f); if (m) rec(m);
    if (!f && !m) { n.L = [0, 0]; n.R = [0, 0]; return; }
    if (f && m) {
      var s = 1, d = Math.min(f.R.length, m.L.length);
      for (i = 0; i < d; i++) s = Math.max(s, f.R[i] - m.L[i] + (i === 0 ? 1 : 1.3));
      f.dx = -s / 2; m.dx = s / 2; n.L = [0]; n.R = [0];
      for (i = 0; i < Math.max(f.L.length, m.L.length); i++) {
        n.L.push(Math.min(i < f.L.length ? f.L[i] - s / 2 : Infinity, i < m.L.length ? m.L[i] + s / 2 : Infinity));
        n.R.push(Math.max(i < f.R.length ? f.R[i] - s / 2 : -Infinity, i < m.R.length ? m.R[i] + s / 2 : -Infinity));
      }
    } else {
      var q = f || m, off = f ? -0.5 : 0.5; q.dx = off;
      n.L = [0].concat(q.L.map(function (v) { return v + off; })); n.R = [0].concat(q.R.map(function (v) { return v + off; }));
      n.L[1] = Math.min(n.L[1], -0.5); n.R[1] = Math.max(n.R[1], 0.5);      // the empty place beside a lone parent stays empty
    }
  })(root);
  (function put(n, x) { n.x = x; n.par.forEach(function (q) { if (q) put(q, x + q.dx); }); })(root, 0);
}
function layoutDesc(root) {
  (function rec(n) {
    n.kids.forEach(rec);
    if (!n.kids.length) { n.L = [0]; n.R = [0]; return; }
    var aL = [], aR = [];
    n.kids.forEach(function (k, i) {
      var off = 0, j;
      if (i > 0) { off = -Infinity; for (j = 0; j < Math.min(aR.length, k.L.length); j++) off = Math.max(off, aR[j] - k.L[j] + 1); }
      k.ox = off;
      for (j = 0; j < k.L.length; j++) { aL[j] = aL[j] === undefined ? k.L[j] + off : Math.min(aL[j], k.L[j] + off); aR[j] = aR[j] === undefined ? k.R[j] + off : Math.max(aR[j], k.R[j] + off); }
    });
    var mid = (n.kids[0].ox + n.kids[n.kids.length - 1].ox) / 2;
    n.kids.forEach(function (k) { k.dx = k.ox - mid; });
    n.L = [0].concat(aL.map(function (v) { return v - mid; })); n.R = [0].concat(aR.map(function (v) { return v - mid; }));
  })(root);
  (function put(n, x) { n.x = x; n.kids.forEach(function (k) { put(k, x + k.dx); }); })(root, 0);
}
function layoutFan(root) {            // wedges sized by what is known, damped so the near rings stay balanced
  var GH = 0.38, GAMMA = 0.62;
  (function w(n) { var f = n.par[0], m = n.par[1]; if (!f && !m) { n.w = 1; return; } if (f) w(f); if (m) w(m); n.w = (f ? f.w : GH) + (m ? m.w : GH); })(root);
  (function a(n, a0, a1) {
    n.a0 = a0; n.a1 = a1; var f = n.par[0], m = n.par[1]; n.ghost = null;
    if (!f && !m) return;
    var wf = Math.pow(f ? f.w : GH, GAMMA), wm = Math.pow(m ? m.w : GH, GAMMA), mid = a0 + (a1 - a0) * wf / (wf + wm);
    if (f) a(f, a0, mid); else if (!n.dup[0]) n.ghost = [a0, mid];
    if (m) a(m, mid, a1); else if (!n.dup[1]) n.ghost = [mid, a1];
  })(root, -FAN_SPAN / 2, FAN_SPAN / 2);
}
function evidence(p) {               // the state of a person's facts, by the same rule the person page's summary bar uses
  if (p.st !== 'f') return 'living';
  var e = p.ev || [0, 0, 0, 0, 0];
  if (!e[0]) return 'none'; if (e[4]) return 'needs'; if (e[1] === e[0]) return 'human'; if (e[1] + e[2]) return 'ai'; return 'un';
}

function build(focus) {
  var old = {}; S.list.forEach(function (n) { old[n.id] = pos(n); });
  var from = old[focus] || (S.sel && old[S.sel]) || null;
  var anc = buildAnc(focus), desc = buildDesc(focus), root = anc.root;
  root.kind = 'focus'; root.t = tOf(focus); if (root.t == null) root.t = 1960;
  anc.list.forEach(function (n) { if (!n.child) return; var t = tOf(n.id); if (t == null) t = n.child.t - GEN; n.t = Math.min(t, n.child.t - 15); });
  desc.root.t = root.t;
  desc.list.forEach(function (n) { if (!n.parent) return; var t = tOf(n.id); if (t == null) t = n.parent.t + GEN; n.t = Math.max(t, n.parent.t + 15); });
  layoutAnc(root); layoutDesc(desc.root); layoutFan(root);
  (function iv(n, a, b) { n.u0 = a; n.u1 = b; n.uc = (a + b) / 2; if (n.par[0]) iv(n.par[0], a, n.uc); if (n.par[1]) iv(n.par[1], n.uc, b); })(root, 0, 1);
  (function cnt(n) { n.size = 1; n.kids.forEach(function (k) { n.size += cnt(k); }); return n.size; })(desc.root);
  (function iv(n, a, b) {
    if (n !== desc.root) { n.u0 = a; n.u1 = b; n.uc = (a + b) / 2; }
    var tot = 0, at = a; n.kids.forEach(function (k) { tot += 1.6 + Math.sqrt(k.size - 1); });
    n.kids.forEach(function (k) { var w = (b - a) * (1.6 + Math.sqrt(k.size - 1)) / tot; iv(k, at, at + w); at += w; });
  })(desc.root, 0, 1);
  root.kids = desc.root.kids; root.kids.forEach(function (k) { k.parent = root; k.toward = root; });
  S.anc = anc; S.desc = desc; S.nodes = {}; S.list = [];
  var rootCol = lineColour(focus);
  anc.list.forEach(function (n) { n.col = pathLine[n.id] || (n.child ? n.child.col : rootCol); add(n); });
  desc.list.forEach(function (n) { if (n.gen === 0) return; n.col = pathLine[n.id] || P[n.id].c || n.parent.col; add(n); });
  function add(n) {
    var p = P[n.id]; n.p = p; n.ev = evidence(p); n.z = wz(n.t);
    if (n.kind === 'anc' || n.kind === 'focus') { var r = n.gen ? R0 + (n.gen - 0.5) * RW : 0, a = (n.a0 + n.a1) / 2; n.fx = r * Math.sin(a); n.fz = wz(root.t) + r * Math.cos(a); }
    else { n.fx = 0; n.fz = wz(root.t); }
    n.from = old[n.id] || from; S.nodes[n.id] = n; S.list.push(n);
  }
  // no year lines from the first living or undated-by-privacy person onward
  var live = S.list.filter(function (n) { return n.p.st !== 'f' && n.t >= 1900; }).map(function (n) { return n.t; });
  var dated = S.list.filter(function (n) { return n.p.by; }).map(function (n) { return n.p.by; });
  S.tMin = Math.min.apply(null, S.list.map(function (n) { return n.t; })); S.tMax = Math.max.apply(null, S.list.map(function (n) { return n.t; }));
  var lastDated = dated.length ? Math.max.apply(null, dated) : S.tMin;
  S.liveFrom = Math.floor((live.length ? Math.min(Math.min.apply(null, live), lastDated + 1) : lastDated + 10) / 10) * 10;
  S.focus = focus; S.rootZ = wz(root.t); S.tween = from && !reduce ? 0 : 1; S.line = null; S.fade = reduce ? 1 : 0; S.win = { L: 0, w: 1 };
  S.trail = null; S.from = null; S.up = null; S.down = null;
}
function pos(n) {                    // where a person is now, in ground units: tree place, fan place, or on the way between
  var x = lerp(n.x, n.fx, S.fanMix), z = lerp(n.z, n.fz, S.fanMix);
  if (S.tween < 1 && n.from) { var k = S.tween * S.tween * (3 - 2 * S.tween); x = lerp(n.from.x, x, k); z = lerp(n.from.z, z, k); }
  return { x: x, z: z };
}

/* ---------- the routes the camera follows ----------
   Two of them, both starting from the person at the centre: one up through the ancestors to the far end of a branch,
   one down through the descendants. Travelling moves along them in time; each person on a route is a station. */
function chainFrom(n, kind) {        // from the centre through n and on to the far end of that branch, taking the deeper side at each fork
  var chain = [], next;
  for (var c = n; c; c = c.toward) chain.unshift(c.id);
  while (true) {
    next = null;
    if (kind === 'anc') n.par.forEach(function (q) { if (q && (!next || q.reach > next.reach)) next = q; });
    else n.kids.forEach(function (k) { if (k.p.st !== 'm' && (!next || k.reach > next.reach)) next = k; });   // never a child under 18: they have no name here
    if (!next) break; chain.push(next.id); n = next;
  }
  return chain;
}
function setPath(via, ids, fresh) {
  var root = S.nodes[S.focus], n = S.nodes[via], f = S.from && S.nodes[S.from];
  if (ids) S.up = ids.filter(function (id) { return S.nodes[id] && P[id].st !== 'm'; });      // a named line: always a way back
  else if (n && n.kind === 'anc') S.up = chainFrom(n, 'anc');
  else if (n && n.kind === 'desc') S.down = chainFrom(n, 'desc');
  else if (fresh) S.up = S.down = null;                     // Reset: both routes as they were to begin with
  // to begin with, a route runs the way you came, if you came that way, and otherwise along the longest branch
  if (!S.up || S.up[0] !== S.focus) S.up = chainFrom(f && f.kind === 'anc' ? f : root, 'anc');
  if (!S.down || S.down[0] !== S.focus) S.down = chainFrom(f && f.kind === 'desc' ? f : root, 'desc');
  var st = function (chain) {
    return chain.map(function (id, i) { var m = S.nodes[id], base = i > 0 ? S.nodes[chain[i - 1]] : null; return { id: id, z: m.z, L: base ? base.u0 : 0, w: base ? base.u1 - base.u0 : 1 }; });
  };
  S.stUp = st(S.up); S.stDown = st(S.down);
  S.path = S.up.slice().reverse().concat(S.down.slice(1));  // oldest first
  S.stations = S.stUp.slice().reverse().concat(S.stDown.slice(1));
  S.onPath = {}; S.path.forEach(function (id) { S.onPath[id] = 1; });
}
function windowAt(z) {               // the share of the width in view at depth z: it narrows smoothly from one station's to the next
  var st = z >= S.rootZ ? S.stUp : S.stDown, i, a, b;
  for (i = 0; i < st.length - 1; i++) {
    a = st[i]; b = st[i + 1];
    if ((z - a.z) * (z - b.z) <= 0 && a.z !== b.z) {
      var u = smooth(0, 1, (z - a.z) / (b.z - a.z)), r = b.w / a.w;
      if (Math.abs(1 - r) < 1e-6) return { L: lerp(a.L, b.L, u), w: a.w };
      var k = Math.pow(r, u); return { L: a.L + (b.L - a.L) * (1 - k) / (1 - r), w: a.w * k };
    }
  }
  a = st[st.length - 1];
  return { L: a.L, w: a.w };
}
function stationAt(z) { var best = null, d = Infinity; S.stations.forEach(function (s) { var e = Math.abs(s.z - z); if (e < d) { d = e; best = s; } }); return best; }
function routeEnds() { return { old: S.stUp[S.stUp.length - 1].z, young: S.stDown[S.stDown.length - 1].z }; }

/* ---------- camera ---------- */
function longParams(s0) {            // a long lens held low: the horizon near the top, each generation back six tenths the size of the one before
  var yH = horizonY(), r1 = 0.6, th = 0.2, Fp = 1000;
  for (var i = 0; i < 8; i++) { Fp = s0 * (r1 / (1 - r1)) * GEN * KZ * Math.cos(th); th = Math.atan((H / 2 - yH) / Fp); }
  return { theta: th, F: Fp, yH: yH, s: s0 };
}
/* The long view is an hourglass. The person you are with sits on a line across it, their card in a band below that
   line; ancestors recede above the line and descendants below the band. How far down the line sits depends on how
   much family there is each way, so a person with no children on record still has the whole height for the past. */
function small() { return H < 520; }                     // a short picture, as on a phone with the browser's bars showing: names, not cards
function horizonY() { return (W < 700 ? (small() ? 0.18 : 0.2) : 0.17) * H; }
function bandH() { return small() ? 62 : 104; }
function floorY() { return H - (S.footH || 86) - 4; }
function hingeY() {
  var n = S.nodes[S.sel] || S.nodes[S.focus], up, down;
  if (n.kind === 'desc') { up = S.anc.root.reach - n.gen; down = n.reach; } else { up = n.reach; down = n.gen + S.desc.root.reach; }
  var yH = horizonY(), T = Math.max(60, floorY() - bandH() - yH);
  var wU = 1 - Math.pow(0.55, up), wD = [0, 0.25, 0.43, 0.58, 0.72][Math.min(down, 4)], lam = wU + wD > 0 ? wD / (wU + wD) : 0;
  var below = down ? Math.max(T * lam, Math.min([0, 60, 90, 110, 110][Math.min(down, 4)], T * 0.42)) : 0;
  var above = up ? Math.max(T - below, Math.min(T, 64)) : down ? 44 : T;
  return Math.min(yH + above, Math.max(0.66 * H, yH + 64));
}
function baseScale() { return clamp(W * 0.44, 150, 215); }
function bounds(kind) {
  var b = { x0: Infinity, x1: -Infinity, z0: Infinity, z1: -Infinity };
  S.list.forEach(function (n) {
    if (kind === 'fan' && n.kind === 'desc') return;
    var x = kind === 'fan' ? n.fx : n.x, z = kind === 'fan' ? n.fz : n.z, pad = kind === 'fan' ? RW * 0.55 : 0;
    b.x0 = Math.min(b.x0, x - pad); b.x1 = Math.max(b.x1, x + pad); b.z0 = Math.min(b.z0, z - pad); b.z1 = Math.max(b.z1, z + pad);
  });
  return b;
}
function fit(kind) {
  var b = bounds(kind), top = 58, bot = 78, side = 26;
  var bw = Math.max(b.x1 - b.x0, 2), bh = Math.max(b.z1 - b.z0, 2);
  var s = clamp(Math.min((W - side * 2) / bw, (H - top - bot) / bh), 3, 150);
  camT.s = s; camT.tx = (b.x0 + b.x1) / 2; camT.theta = TOP; camT.yT = H / 2;
  camT.tz = (b.z0 + b.z1) / 2 + ((top - bot) / 2) / s;
}
/* The fan opens on the rings whose names can be read; Fit shows the whole of it. */
function fanHome() {
  var top = 14, bot = 58, s = clamp(W / 10.5, 36, 48);
  if (camT.s >= s) return;
  camT.s = s; camT.tx = 0; camT.tz = S.rootZ + (top + (H - top - bot) * 0.55 - H / 2) / s;
}
function frame() { fit(S.lens); if (S.lens === 'fan' && !S.whole) fanHome(); }
function aim(snap) {                 // point the camera for the current view
  longP = longParams(S.zoomLong || baseScale()); S.moved = false;
  if (S.lens === 'long') {
    var n = S.nodes[S.sel] || S.nodes[S.focus];
    camT.theta = longP.theta; camT.F = longP.F; camT.yT = hingeY(); camT.s = longP.s; camT.tz = n.z; camT.tx = n.x;
  } else { camT.F = longP.F; frame(); }
  S.fanT = S.lens === 'fan' ? 1 : 0;
  if (snap || reduce) { for (var k in camT) cam[k] = camT[k]; S.fanMix = S.fanT; S.tween = 1; S.fade = 1; S.win = windowAt(cam.tz); }
  go();
}

/* ---------- drawing ---------- */
function readTheme() {
  var cs = getComputedStyle(document.documentElement), g = function (n) { return cs.getPropertyValue(n).trim(); };
  col = { paper: g('--paper'), ink: g('--ink'), muted: g('--muted'), rule: g('--rule'), soft: g('--rule-soft'), card: g('--card'), sea: g('--sea'), needs: g('--stamp-needs'), neutral: g('--t-neutral') };
  D.lines.forEach(function (l) { col[l.slug] = g('--t-' + l.slug) || g('--t-' + l.color) || col.neutral; });
  var m = /^#(..)(..)(..)$/.exec(col.paper), rgb = m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [238, 239, 233];
  col.dark = rgb[0] + rgb[1] + rgb[2] < 380; col.paper0 = 'rgba(' + rgb.join(',') + ',0)';
}
var DISP = '"Bricolage Grotesque", "Helvetica Neue", Arial, sans-serif', MONO = '"IBM Plex Mono", ui-monospace, Menlo, monospace';
var wCache = {};
function tw(font, text) { var k = font + '|' + text; if (!(k in wCache)) { ctx.font = font; wCache[k] = ctx.measureText(text).width; } return wCache[k]; }
function fitText(font, text, max) {
  if (tw(font, text) <= max) return text;
  var t = text; while (t.length > 2 && tw(font, t + '…') > max) t = t.slice(0, -1);
  return t.replace(/\s+$/, '') + '…';
}
function rr(x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }
function nodeColour(n) {
  if (S.mode === 'evidence') return n.ev === 'needs' ? col.needs : n.ev === 'un' || n.ev === 'living' || n.ev === 'none' ? col.muted : col.ink;
  return n.p.st === 'f' ? col[n.col] || col.neutral : col.muted;
}

function draw() {
  var th = cam.theta, sin = Math.sin(th), cos = Math.cos(th);
  var zvT = cam.F / cam.s, yvT = (H / 2 - cam.yT) / cam.s;
  var h = zvT * sin - yvT * cos, Dz = zvT * cos + yvT * sin, NEAR = zvT * 0.2;
  var tilt = clamp((TOP - th) / (TOP - longP.theta), 0, 1);          // 0 looking straight down … 1 the long view
  var pin = 0.13 * tilt;
  var boxes = [], late = [];
  var free = function (b) { for (var i = 0; i < boxes.length; i++) { var o = boxes[i]; if (b.x < o.x + o.w && b.x + b.w > o.x && b.y < o.y + o.h && b.y + b.h > o.y) return false; } return true; };
  var yH = tilt > 0.3 ? H / 2 - cam.F * Math.tan(th) : -1e4;         // the horizon, once the camera is low enough to see one
  /* Older than the person you are with: the ground receding to the horizon. Younger: below them, after a band that
     holds their card, so that older is up and younger is down wherever you are. People are the size they would be the
     same distance above, and the generations below are spread over the room there is, down to the youngest. People
     born within fifteen years after the person you are with sit on the way down the band; a child is always below it. */
  var gB = bandH(), gL = Math.max(0, floorY() - cam.yT - gB), gE = Math.max(cam.tz - wz(S.tMax), 3 * GEN * KZ), gD = 15 * KZ, glass = smooth(0, 0.6, tilt);
  function persp(x, d, y) {
    var dz = d + Dz, hh = h - (y || 0), zv = hh * sin + dz * cos;
    if (zv <= NEAR) return null;
    var k = cam.F / zv; return { x: W / 2 + (x - cam.tx) * k, y: H / 2 - (-hh * cos + dz * sin) * k, s: k };
  }
  function proj(x, z, y) {
    var d = z - cam.tz;
    if (d >= 0 || !glass) return persp(x, d, y);
    var k = persp(x, -d, 0).s, m = { x: W / 2 + (x - cam.tx) * k, y: cam.yT + gB * smooth(0, gD, -d) + gL * Math.min(1.1, -2 * d / (gE - d)) - (y || 0) * cos * k, s: k };
    if (glass >= 1) return m;
    var p = persp(x, d, y); if (!p) return m;                        // on the way between the tree view and this one
    return { x: lerp(p.x, m.x, glass), y: lerp(p.y, m.y, glass), s: lerp(p.s, m.s, glass) };
  }
  var win = S.win, HsF = W / 2 - 26, HsN = Math.min(clamp(0.24 * W, 150, 290), HsF), FULL = { L: 0, w: 1 }, camUp = cam.tz >= S.rootZ - 1e-6;
  function span(d) { return HsN + (HsF - HsN) * (1 - Math.exp(-Math.abs(d) / 9)); }
  function winOf(kind, z) {          // the share of the width a person is measured against
    if ((kind === 'desc') === camUp) return FULL;                    // beyond the person at the centre: the whole of their family that way
    var behind = kind === 'desc' ? z - cam.tz : cam.tz - z;          // between you and the centre: the route's own share at that depth
    if (behind <= 0) return win;
    var w2 = windowAt(z), k = smooth(0, 1.2, behind);
    return { L: lerp(win.L, w2.L, k), w: lerp(win.w, w2.w, k) };
  }
  function frac(uc, w) { return ((uc - w.L) / w.w) * 2 - 1 - S.panX; }
  function place(x, z, uc, y, kind) {
    var q = proj(x, z, y); if (!q) return null;
    if (tilt > 0) q.x = lerp(q.x, W / 2 + clamp(frac(uc, winOf(kind, z)), -9, 9) * span(z - cam.tz), tilt);
    return q;
  }
  function fog(s) { if (!tilt) return 1; var q = s / cam.s; return lerp(1, clamp(0.22 + 0.78 * smooth(0.07, 0.5, q), 0, 1) * (1 - smooth(1.9, 2.7, q)), tilt); }

  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.globalAlpha = 1; ctx.fillStyle = col.paper; ctx.fillRect(0, 0, W, H);
  var timeA = 1 - smooth(0, 0.5, S.fanMix), fanA = tilt < 0.02 ? smooth(0.55, 1, S.fanMix) : 0;

  // year lines: a rule every ten, fifty or hundred years, whichever has room
  if (timeA > 0.01) {
    ctx.lineWidth = 1; var lastLbl = 1e9, lastEv = 1e9, y, p, p2, gap;
    var from = Math.floor((S.tMin - 30) / 10) * 10;
    for (y = S.liveFrom; y >= from; y -= 10) {
      p = proj(cam.tx, wz(y)); if (!p || p.y < yH + 2 || p.y > H + 2) continue;
      p2 = proj(cam.tx, wz(y - 10)); gap = p2 ? Math.abs(p.y - p2.y) : 40;
      var cent = y % 100 === 0, half = y % 50 === 0;
      if (!(cent || (half && gap * 5 >= 15) || gap >= 15)) continue;
      ctx.globalAlpha = timeA * (cent ? 0.95 : half ? 0.7 : 0.55); ctx.strokeStyle = cent || half ? col.rule : col.soft;
      ctx.beginPath(); ctx.moveTo(0, Math.round(p.y) + 0.5); ctx.lineTo(W, Math.round(p.y) + 0.5); ctx.stroke();
      if (Math.abs(p.y - lastLbl) >= 13 && (cent || (half && gap * 5 >= 30) || gap >= 30) && p.y > 14 && p.y < H - 74) {
        late.push({ t: String(y), f: '500 10px ' + MONO, x: 8, y: p.y - 2, al: 'left', c: col.muted, a: timeA * 0.9 }); lastLbl = p.y;
      }
    }
    D.events.slice().sort(function (a, b) { return b.year - a.year; }).forEach(function (ev) {
      if (ev.year > S.liveFrom || ev.year < from) return;
      p = proj(cam.tx, wz(ev.year)); if (!p || p.y < yH + 12 || p.y > H - 74) return;
      ctx.globalAlpha = timeA * 0.75; ctx.strokeStyle = col.sea; ctx.setLineDash([5, 5]);
      ctx.beginPath(); ctx.moveTo(0, Math.round(p.y) + 0.5); ctx.lineTo(W, Math.round(p.y) + 0.5); ctx.stroke(); ctx.setLineDash([]);
      if (Math.abs(p.y - lastEv) < 13) return; lastEv = p.y;
      var f = '600 condensed 10.5px ' + DISP, t = fitText(f, (ev.year + '  ' + ev.label).toUpperCase(), Math.min(W - 70, 420));
      late.push({ t: t, f: f, x: W - 10, y: p.y - 2, al: 'right', c: col.sea, a: timeA, sp: '0.8px' });
    });
  }

  // everyone's place on screen this frame
  var vis = [], P2 = {};
  S.list.forEach(function (n) {
    var a = n.kind === 'desc' ? S.aDesc : n.kind === 'anc' ? S.aAnc : 1; if (a < 0.01) return;
    var w = pos(n), g = place(w.x, w.z, n.uc, 0, n.kind); if (!g) return;
    var hd = pin ? place(w.x, w.z, n.uc, pin, n.kind) : g; if (!hd) return;
    var wn = tilt > 0 ? winOf(n.kind, w.z) : win;
    var v = { n: n, gx: g.x, gy: g.y, x: hd.x, y: hd.y, s: g.s, a: a * fog(g.s) * S.fade, w: w, fr: frac(n.uc, wn), ww: wn.w, low: tilt > 0.5 && w.z < cam.tz - 0.05 };
    P2[n.id] = v;
    if (v.x < -260 || v.x > W + 260 || v.y < -160 || v.y > H + 220) return;
    vis.push(v);
  });

  // fan: wedges
  S.wedge = null;
  if (fanA > 0.01) drawFan(fanA, proj);

  // links, the route brighter
  if (timeA > 0.01) {
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    S.list.forEach(function (n) {
      var t = n.toward; if (!t) return; var a = P2[n.id], b = P2[t.id]; if (!a && !b) return;
      var kindA = n.kind === 'desc' ? S.aDesc : S.aAnc; if (kindA < 0.01) return;
      if (tilt === 1 && a && b && ((a.fr < -1.25 && b.fr < -1.25) || (a.fr > 1.25 && b.fr > 1.25))) return;      // in the long view, a link wholly outside the view is not drawn
      var wa = pos(n), wb = pos(t), on = S.onPath[n.id] && S.onPath[t.id], home = !on && S.trail && S.trail[n.id] && S.trail[t.id], pts = [], i, k, e, q, sMax = 0;
      var steps = ((a || b).s < 22 && tilt === 1) ? 5 : 12;
      for (i = 0; i <= steps; i++) { k = i / steps; e = k * k * (3 - 2 * k); q = place(lerp(wb.x, wa.x, e), lerp(wb.z, wa.z, k), lerp(t.uc, n.uc, e), 0, n.kind); if (q) { pts.push(q); sMax = Math.max(sMax, q.s); } }
      if (pts.length < 2 || sMax < 1.2) return;
      var al = kindA * timeA * fog(pts[pts.length - 1].s) * S.fade;
      if (pts[0].x < -40 && pts[pts.length - 1].x < -40 || pts[0].x > W + 40 && pts[pts.length - 1].x > W + 40) return;
      ctx.globalAlpha = al * (on ? 0.95 : home ? 0.82 : col.dark ? 0.5 : 0.42);
      ctx.strokeStyle = S.mode === 'evidence' ? col.muted : (col[n.col] || col.neutral);
      ctx.lineWidth = clamp(pts[pts.length - 1].s * (on ? 0.05 : home ? 0.037 : 0.026), on ? 1.4 : home ? 1.1 : 0.6, on ? 5 : home ? 4 : 3);
      ctx.beginPath(); ctx.moveTo(pts[0].x, pts[0].y); for (i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y); ctx.stroke();
    });
    // where the trail stops: no parents on record
    ctx.setLineDash([2, 4]); ctx.lineWidth = 1;
    S.list.forEach(function (n) {
      if (n.kind === 'desc' || !P2[n.id]) return; var v = P2[n.id]; if (v.s < 9) return;
      if (n.par[0] || n.par[1] || n.dup[0] || n.dup[1]) return;
      var e = place(v.w.x, v.w.z + 1.5, n.uc, 0, n.kind); if (!e) return;
      ctx.globalAlpha = v.a * S.aAnc * timeA * 0.7; ctx.strokeStyle = col.muted; ctx.beginPath(); ctx.moveTo(v.gx, v.gy); ctx.lineTo(e.x, e.y); ctx.stroke();
    });
    ctx.setLineDash([]);
  }

  // haze toward the horizon
  if (tilt > 0.3) {
    var hz = ctx.createLinearGradient(0, yH - 8, 0, yH + 46); hz.addColorStop(0, col.paper); hz.addColorStop(1, col.paper0);
    ctx.globalAlpha = smooth(0.3, 0.8, tilt); ctx.fillStyle = col.paper; ctx.fillRect(0, 0, W, Math.max(0, yH - 8)); ctx.fillStyle = hz; ctx.fillRect(0, yH - 8, W, 54);
  }

  // people, far to near
  var dotA = 1 - fanA; S.hits = [];
  vis.sort(function (a, b) { return a.s - b.s; });
  if (dotA > 0.01) vis.forEach(function (v) {
    var n = v.n, p = n.p, s = v.s, r = clamp(s * 0.085, 1.7, 8.5), c = nodeColour(n), al = v.a * dotA;
    if (p.st === 'm') r *= 0.7;
    if (tilt > 0 && n.kind !== 'focus') r = lerp(r, Math.min(r, Math.max(1.5, ((n.u1 - n.u0) / v.ww) * span(v.w.z - cam.tz) * 0.84)), tilt);
    if (pin && s > 30) {             // a pin standing on its year
      ctx.globalAlpha = al * 0.55; ctx.strokeStyle = c; ctx.lineWidth = clamp(s * 0.012, 0.6, 1.6);
      ctx.beginPath(); ctx.moveTo(v.gx, v.gy); ctx.lineTo(v.x, v.y); ctx.stroke();
      ctx.globalAlpha = al * 0.22; ctx.fillStyle = col.ink; ctx.beginPath(); ctx.ellipse(v.gx, v.gy, r * 0.9, r * 0.3, 0, 0, 7); ctx.fill();
    }
    if (col.dark && p.st === 'f' && S.mode === 'lines' && s < 60) { ctx.globalAlpha = al * 0.2; ctx.fillStyle = c; ctx.beginPath(); ctx.arc(v.x, v.y, r * 2.3, 0, 7); ctx.fill(); }
    ctx.globalAlpha = al; ctx.lineWidth = Math.max(1, r * 0.3);
    var hollow = p.st !== 'f' || (S.mode === 'evidence' && n.ev !== 'human');
    if (hollow) {
      ctx.fillStyle = col.paper; ctx.beginPath(); ctx.arc(v.x, v.y, r, 0, 7); ctx.fill();
      if (r > 3 && (p.st !== 'f' || n.ev === 'un' || n.ev === 'none')) ctx.setLineDash([r * 0.55, r * 0.5]);
      ctx.strokeStyle = S.mode === 'evidence' && n.ev === 'none' ? col.rule : c; ctx.beginPath(); ctx.arc(v.x, v.y, r, 0, 7); ctx.stroke(); ctx.setLineDash([]);
    } else { ctx.fillStyle = c; ctx.beginPath(); ctx.arc(v.x, v.y, r, 0, 7); ctx.fill(); }
    if (p.x && r > 4 && S.mode === 'lines') { ctx.strokeStyle = col.sea; ctx.setLineDash([3, 3]); ctx.lineWidth = 1.3; ctx.beginPath(); ctx.arc(v.x, v.y, r + 3.5, 0, 7); ctx.stroke(); ctx.setLineDash([]); }
    if (n.id === S.sel) { ctx.strokeStyle = col.ink; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(v.x, v.y, r + (p.x && r > 4 ? 7 : 3.5), 0, 7); ctx.stroke(); }
    v.r = r; if (p.st !== 'm') S.hits.push({ id: n.id, x: v.x, y: v.y, r: r, s: s });
  });

  // names: whoever is nearest gets a card, the next rows a name, the rest stay points
  if (dotA > 0.3) {
    var tips = {}; D.lines.forEach(function (l) { tips[l.path[l.path.length - 1]] = 1; });
    var order = vis.filter(function (v) { return v.a > 0.2 && v.n.p.st !== 'm' && v.x > 8 && v.x < W - 8 && v.y > -20 && v.y < H + 20; });
    if (tilt > 0.5) { boxes.push({ x: 0, y: 0, w: Math.min(200, W * 0.5), h: 92 }); boxes.push({ x: W - 262, y: 0, w: 262, h: 52 }); }
    else boxes.push({ x: W - 262, y: 0, w: 262, h: 52 });
    vis.forEach(function (v) { if (v.r >= 3.2 && v.a > 0.2) boxes.push({ x: v.x - v.r, y: v.y - v.r, w: v.r * 2, h: v.r * 2 }); });
    order.forEach(function (v) {
      var n = v.n, came = !!(S.trail && S.trail[n.id]); v.key = n.id === S.sel || n.kind === 'focus' || !!S.onPath[n.id] || came || !!tips[n.id] || (tilt < 0.5 && Math.abs(n.gen) <= 2);
      v.pri = (n.id === S.sel ? 4000 : 0) + (n.kind === 'focus' ? 3000 : 0) + (S.onPath[n.id] ? 40 : 0) + (came ? 30 : 0) + (tips[n.id] ? 25 : 0) + v.s;
    });
    order.sort(function (a, b) { return b.pri - a.pri; });
    var cards = [], foot = tilt > 0.5 && S.footH ? { x: 0, y: H - S.footH + 14, w: W, h: S.footH } : null;
    order.forEach(function (v) {
      var n = v.n, p = n.p, s = v.s, b;
      if (foot && v.pri < 3000) { boxes.push(foot); foot = null; }      // only the person stood with may run under the controls
      if (s >= 132 && !(tilt > 0.5 && small())) {
        var need = Math.max(tw('800 15.5px ' + DISP, p.f && p.s ? p.s : p.n), tw('500 12px ' + DISP, p.f && p.s ? p.f : '')) + 24;
        var cw = clamp(Math.max(s - 16, need), 116, Math.min(190, tilt > 0.5 ? HsN - 12 : 190)), ch = (p.st === 'f' ? 78 : 64) + (p.x && S.mode === 'lines' ? 13 : 0);
        b = { x: clamp(v.x - cw / 2, 6, W - cw - 6), y: v.y + v.r + 8, w: cw, h: ch };
        if (free(b)) { boxes.push(b); cards.push({ v: v, b: b }); return; }
      }
      if (s < 30 && !v.key) return;
      var big = s >= 60, fs = big ? clamp(11.5 + (s - 60) / 40, 11.5, 13.5) : clamp(10 + (s - 22) / 30, 10, 11.5), rows = [], yr = p.st === 'f' && (p.bt || p.dt) ? years(p) : '';
      if (v.key && fs < 11) fs = 11;
      if (s >= 132 && tilt > 0.5 && small()) fs = 15;          // where there are no cards, the people you are with have the largest names
      fs = Math.round(fs * 2) / 2;
      if (big && p.f && p.s) rows.push([p.f.split(' ')[0], '500 ' + (fs - 1.5) + 'px ' + DISP, fs + 0.5, col.muted], [p.s, '700 ' + fs + 'px ' + DISP, fs + 3, p.st === 'f' ? col.ink : col.muted]);
      else rows.push([shortName(p), (big || v.key ? '700 ' : '600 ') + fs + 'px ' + DISP, fs + 3, p.st === 'f' ? col.ink : col.muted]);
      if (big && yr) rows.push([yr, '500 9.5px ' + MONO, 12, col.muted]);
      /* A name goes above its point, else below. The people at the front, and the line being walked, matter most:
         theirs may also sit beside the point, lose its years, and at the very front be cut short as a last resort. */
      var near = s >= 45 || v.key, sets = [rows], spots = v.low || s >= 132 ? (near ? [1, 0, 2, 3] : [1, 0]) : near ? [0, 1, 2, 3] : [0, 1], al = 'center', bw, bh, i, j;      // below you, and beside you where there are no cards, a name hangs under its point
      if (near && rows.length > 2) sets.push(rows.slice(0, 2));
      if (big && rows.length > 1) sets.push(sets[sets.length - 1].map(function (r) { return [fitText(r[1], r[0], 58), r[1], r[2], r[3]]; }));
      b = null;
      for (i = 0; i < sets.length && !b; i++) {
        bw = 0; bh = 2; sets[i].forEach(function (r) { bw = Math.max(bw, tw(r[1], r[0])); bh += r[2]; }); bw += 8;
        for (j = 0; j < spots.length && !b; j++) {
          var k = spots[j], c = k < 2 ? { x: v.x - bw / 2, y: k ? v.y + v.r + 4 : v.y - v.r - 4 - bh, w: bw, h: bh }
            : { x: k === 2 ? v.x + v.r + 3 : v.x - v.r - 3 - bw, y: v.y - Math.min(bh / 2, 6), w: bw, h: bh };
          if (c.x < 3 || c.x + bw > W - 3) { if (k > 1 || !v.key) continue; c.x = clamp(c.x, 3, W - bw - 3); }
          if (free(c)) { b = c; rows = sets[i]; al = k < 2 ? 'center' : k === 2 ? 'left' : 'right'; }
        }
      }
      if (!b) return;
      boxes.push(b);
      var tx = al === 'center' ? b.x + bw / 2 : al === 'left' ? b.x + 4 : b.x + bw - 4, ty = b.y + 1;
      ctx.globalAlpha = clamp(v.a * 1.6, 0.55, 1) * dotA; ctx.textAlign = al; ctx.textBaseline = 'top'; ctx.lineJoin = 'round'; ctx.lineWidth = 3.5; ctx.strokeStyle = col.paper;
      rows.forEach(function (r) { ctx.font = r[1]; ctx.strokeText(r[0], tx, ty); ctx.fillStyle = r[3]; ctx.fillText(r[0], tx, ty); ty += r[2]; });
      S.hits.push({ id: n.id, x: v.x, y: b.y + bh / 2, r: 1, s: s, box: b });
    });
    cards.reverse().forEach(function (c) { drawCard(c.v, c.b, dotA); });
  }
  // years and world events, wherever the names left room
  late.forEach(function (l) {
    var w = tw(l.f, l.t) + (l.sp ? l.t.length * 0.8 : 0), b = { x: l.al === 'left' ? l.x : l.x - w, y: l.y - 12, w: w, h: 13 };
    if (!free(b)) return; boxes.push(b);
    ctx.font = l.f; ctx.textAlign = l.al; ctx.textBaseline = 'bottom'; ctx.globalAlpha = l.a; ctx.fillStyle = l.c; ctx.lineJoin = 'round';
    if ('letterSpacing' in ctx) ctx.letterSpacing = l.sp || '0px';
    ctx.lineWidth = 3; ctx.strokeStyle = col.paper; ctx.strokeText(l.t, l.x, l.y); ctx.fillText(l.t, l.x, l.y);
    if ('letterSpacing' in ctx) ctx.letterSpacing = '0px';
  });
  ctx.globalAlpha = 1;
}

function drawCard(v, b, dotA) {
  var n = v.n, p = n.p, c = S.mode === 'evidence' ? nodeColour(n) : (col[n.col] || col.neutral), sel = n.id === S.sel, x = b.x, y = b.y, pad = 11, mw = b.w - pad - 8;
  ctx.globalAlpha = Math.min(1, v.a * 1.2) * dotA;
  ctx.fillStyle = col.card; rr(x, y, b.w, b.h, 6); ctx.fill();
  ctx.lineWidth = sel ? 2 : 1; ctx.strokeStyle = sel ? col.ink : col.rule; rr(x, y, b.w, b.h, 6); ctx.stroke();
  ctx.fillStyle = c; rr(x, y, 5, b.h, 2.5); ctx.fill();
  ctx.strokeStyle = sel ? col.ink : col.muted; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(clamp(v.x, x + 10, x + b.w - 10), y); ctx.lineTo(v.x, v.y + v.r); ctx.stroke();
  ctx.textAlign = 'left'; ctx.textBaseline = 'top';
  var given = p.f && p.s ? p.f : '', sur = p.f && p.s ? p.s : p.n, f1 = '500 12px ' + DISP, f2 = '800 15.5px ' + DISP, fy = '500 10.5px ' + MONO, fp = '600 condensed 10px ' + DISP, ty = y + 8;
  ctx.fillStyle = col.muted; ctx.font = f1; ctx.fillText(fitText(f1, given, mw), x + pad, ty); ty += 15;
  ctx.fillStyle = col.ink; ctx.font = f2; ctx.fillText(fitText(f2, sur, mw), x + pad, ty); ty += 21;
  ctx.fillStyle = col.muted; ctx.font = fy; ctx.fillText(fitText(fy, years(p), mw), x + pad, ty); ty += 15;
  if (p.st !== 'f') return void S.hits.push({ id: n.id, x: v.x, y: y + b.h / 2, r: 1, s: v.s, box: b });
  var line = '';
  if (S.mode === 'evidence' && p.st === 'f') { var e = p.ev; line = e[0] ? (e[1] ? e[1] + ' reviewed · ' : '') + e[2] + ' of ' + e[0] + ' AI-checked' + (e[4] ? ' · ' + e[4] + ' flagged' : '') : 'no facts yet'; ctx.fillStyle = n.ev === 'needs' ? col.needs : col.muted; }
  else { line = [place(p.bp), place(p.dp)].filter(function (s, i, a) { return s && a.indexOf(s) === i; }).join(' → '); ctx.fillStyle = col.sea; }
  if ('letterSpacing' in ctx) ctx.letterSpacing = '0.7px';
  ctx.font = fp; ctx.fillText(fitText(fp, line.toUpperCase(), mw), x + pad, ty); ty += 13;
  if (p.x && S.mode === 'lines') ctx.fillText(fitText(fp, ('≈ ' + (p.x.t === 'Departure' || p.x.t === 'Emigration' ? 'left ' : 'arrived ') + (p.x.p ? place(p.x.p) + ' ' : '') + p.x.y).toUpperCase(), mw), x + pad, ty);
  if ('letterSpacing' in ctx) ctx.letterSpacing = '0px';
  S.hits.push({ id: n.id, x: v.x, y: y + b.h / 2, r: 1, s: v.s, box: b });
}

function drawFan(A, proj) {
  var c0 = proj(0, S.rootZ, 0); if (!c0) return; var s = cam.s, cx = c0.x, cy = c0.y;
  S.wedge = { cx: cx, cy: cy, s: s };
  ctx.lineJoin = 'round';
  function wedge(a0, a1, g) { var r0 = (R0 + (g - 1) * RW) * s, r1 = r0 + RW * s; ctx.beginPath(); ctx.arc(cx, cy, r1, a0 - TOP, a1 - TOP); ctx.arc(cx, cy, r0, a1 - TOP, a0 - TOP, true); ctx.closePath(); }
  var lit = S.line ? S.onPath : null;                        // following a line: it stays bright and the rest steps back
  S.anc.list.forEach(function (n) {
    if (!n.gen) return; var p = n.p, c = nodeColour(n), living = p.st !== 'f', dim = lit && !lit[n.id] ? 0.34 : 1;
    wedge(n.a0, n.a1, n.gen);
    if (living) { ctx.globalAlpha = A; ctx.fillStyle = col.card; ctx.fill(); }
    else { ctx.globalAlpha = A * dim * (S.mode === 'evidence' ? { human: 1, ai: 0.7, needs: 0.85, un: 0.4, none: 0.16 }[n.ev] : clamp(0.95 - n.gen * 0.022, 0.6, 0.95)); ctx.fillStyle = c; ctx.fill(); }
    ctx.globalAlpha = A; ctx.lineWidth = s > 14 ? 1.5 : 0.75; ctx.strokeStyle = col.paper; ctx.stroke();
    if (living) { ctx.setLineDash([4, 3]); ctx.lineWidth = 1; ctx.strokeStyle = col.muted; ctx.stroke(); ctx.setLineDash([]); }
    if (n.ghost && s > 9) {          // one parent on record, the other not
      wedge(n.ghost[0], n.ghost[1], n.gen + 1); ctx.globalAlpha = A * 0.8; ctx.setLineDash([3, 3]); ctx.lineWidth = 1; ctx.strokeStyle = col.muted; ctx.stroke(); ctx.setLineDash([]);
    }
  });
  var sel = S.nodes[S.sel];
  if (sel && sel.gen > 0 && sel.kind === 'anc') {            // the chosen wedge, and the way down from it to the centre
    ctx.strokeStyle = col.ink;
    for (var w = sel.toward; w && w.gen > 0; w = w.toward) { wedge(w.a0, w.a1, w.gen); ctx.globalAlpha = A * 0.85; ctx.lineWidth = 1.25; ctx.stroke(); }
    wedge(sel.a0, sel.a1, sel.gen); ctx.globalAlpha = A; ctx.lineWidth = 2.5; ctx.stroke();
  }
  // names inside the wedges: along the curve where the wedge is wide, along the spoke where it is narrow
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  function arcText(str, r, a, font) {                      // centred on angle a at radius r; upright in the lower half too
    var low = Math.abs(a) > TOP, total = tw(font, str), dirn = low ? -1 : 1, at = a - dirn * total / (2 * r), i, w0 = 0, w1;
    ctx.font = font;
    for (i = 0; i < str.length; i++) {
      w1 = tw(font, str.slice(0, i + 1)); var mid = at + dirn * ((w0 + w1) / 2) / r; w0 = w1;
      ctx.save(); ctx.translate(cx + r * Math.sin(mid), cy - r * Math.cos(mid)); ctx.rotate(low ? mid + Math.PI : mid); ctx.fillText(str[i], 0, 0); ctx.restore();
    }
  }
  S.anc.list.forEach(function (n) {
    if (!n.gen) return; var p = n.p, a = (n.a0 + n.a1) / 2, rm = (R0 + (n.gen - 0.5) * RW) * s, arc = rm * (n.a1 - n.a0), rad = RW * s;
    var x = cx + rm * Math.sin(a), y = cy - rm * Math.cos(a);
    if (x < -rad || x > W + rad || y < -rad || y > H + rad) return;
    var living = p.st !== 'f', back = lit && !lit[n.id], ink = living || back ? col.ink : (S.mode === 'evidence' && (n.ev === 'un' || n.ev === 'none') ? col.ink : col.paper), name = shortName(p), fs, f, low = Math.abs(a) > TOP ? -1 : 1;
    ctx.globalAlpha = A * (back ? 0.62 : 1); ctx.fillStyle = ink;
    if (arc >= rad * 1.15) {
      fs = Math.floor(clamp(Math.min(rad / 3.1, arc / 7.5), 0, 15) * 2) / 2; if (fs < 8.5) return;
      f = '700 ' + fs + 'px ' + DISP;
      var parts = p.f && p.s ? [p.f.split(' ')[0], p.s] : [name], three = parts.length === 2 && rad >= fs * 3.3 && !living, two = parts.length === 2 && rad >= fs * 2.4;
      if (three || two) {
        var f0 = '500 ' + Math.round(fs * 1.72) / 2 + 'px ' + DISP, off = three ? fs * 0.95 : fs * 0.5;
        arcText(fitText(f0, parts[0], arc - 8), rm + low * off, a, f0);
        arcText(fitText(f, parts[1], arc - 8), rm + low * (three ? -0.08 * fs : -off * 1.05), a, f);
        if (three) { var fm = '500 ' + Math.max(8, Math.round(fs * 1.4) / 2) + 'px ' + MONO; arcText(years(p), rm - low * fs * 1.05, a, fm); }
      } else arcText(fitText(f, name, arc - 8), rm, a, f);
    } else {
      fs = Math.floor(clamp(Math.min(arc - 2, rad / 6.2), 0, 12.5) * 2) / 2; if (fs < 8) return;
      f = '600 ' + fs + 'px ' + DISP; var rot = a - TOP; if (a < 0) rot += Math.PI;
      ctx.save(); ctx.translate(x, y); ctx.rotate(rot); ctx.font = f; ctx.fillText(fitText(f, name, rad - 8), 0, 0); ctx.restore();
    }
  });
  // the person at the centre
  var root = S.anc.root, rp = root.p, R = R0 * s;
  ctx.globalAlpha = A; ctx.beginPath(); ctx.arc(cx, cy, Math.max(R - 1, 3), 0, 7); ctx.fillStyle = col.card; ctx.fill();
  ctx.lineWidth = S.sel === root.id ? 3 : 2; ctx.strokeStyle = S.sel === root.id ? col.ink : (col[root.col] || col.neutral); ctx.stroke();
  if (R >= 24) {
    var fz = Math.round(clamp(R / 3.4, 9, 16)); ctx.fillStyle = col.ink;
    ctx.font = '500 ' + Math.round(fz * 1.64) / 2 + 'px ' + DISP; ctx.fillText(fitText(ctx.font, rp.f && rp.s ? rp.f.split(' ')[0] : '', R * 1.7), cx, cy - fz * 0.55);
    ctx.font = '800 ' + fz + 'px ' + DISP; ctx.fillText(fitText(ctx.font, rp.f && rp.s ? rp.s : rp.n, R * 1.75), cx, cy + fz * 0.5);
  }
}

/* ---------- animation ---------- */
function go() { if (!raf) { last = 0; raf = requestAnimationFrame(tick); } }
function tick(now) {
  raf = 0; var dt = last ? Math.min(0.05, (now - last) / 1000) : 0.016; last = now; var busy = false, k;
  if (S.flying) {
    var end = S.back ? routeEnds().old : routeEnds().young, step = 2.1 * dt * (end > camT.tz ? 1 : -1);
    if (Math.abs(end - camT.tz) <= Math.abs(step)) { camT.tz = end; stopFly(); } else camT.tz += step;
    busy = true;
  }
  if (S.lens === 'long') { follow(); camT.yT = hingeY(); }
  var e = reduce ? 1 : 1 - Math.exp(-dt / 0.14);
  for (k in camT) { var d = camT[k] - cam[k]; if (Math.abs(d) > (k === 'theta' ? 1e-4 : k === 's' || k === 'F' || k === 'yT' ? 0.02 : 1e-3)) { cam[k] += d * e; busy = true; } else cam[k] = camT[k]; }
  var mv = function (cur, tgt, rate) { if (cur === tgt) return cur; busy = true; var s2 = reduce ? 1 : dt * rate; return Math.abs(tgt - cur) <= s2 ? tgt : cur + Math.sign(tgt - cur) * s2; };
  S.fanMix = mv(S.fanMix, S.fanT, 1.5); S.tween = mv(S.tween, 1, 1.5); S.fade = mv(S.fade, 1, 3);
  var wt = windowAt(cam.tz), we = reduce ? 1 : 1 - Math.exp(-dt / 0.09), wc = S.win.L + S.win.w / 2, tc = wt.L + wt.w / 2, lw = Math.log(S.win.w), ltw = Math.log(wt.w);
  if (Math.abs(lw - ltw) > 1e-4 || Math.abs(wc - tc) > S.win.w * 1e-4) { lw += (ltw - lw) * we; wc += (tc - wc) * we; S.win = { L: wc - Math.exp(lw) / 2, w: Math.exp(lw) }; busy = true; } else S.win = wt;
  S.aDesc = mv(S.aDesc, S.lens === 'fan' ? 0 : 1, 3.5);       // the fan is of ancestors only
  draw(); hud();
  if (busy) raf = requestAnimationFrame(tick);
}
function follow() {                  // in the long view the person you are passing is the person in the panel
  var st = stationAt(camT.tz); if (st && st.id !== S.sel) { S.sel = st.id; panel(); }
}
function yearNow() { return TNOW - camT.tz / KZ; }
var shown = {};
function put(sel, prop, val) { if (shown[sel + prop] !== val) { shown[sel + prop] = val; var el = $(sel); if (prop === 'display') el.style.display = val; else if (/^(aria|data)-/.test(prop)) el.setAttribute(prop, val); else el[prop] = val; } }
function standing() { return S.nodes[S.sel] && P[S.sel].st !== 'm' ? S.sel : S.focus; }      // the person you are with in the long view
function flyBack() {                 // which way Fly goes: back, unless you are at the far end already or have been heading down
  var e = routeEnds(), up = e.old - camT.tz > 0.2, down = camT.tz - e.young > 0.2;
  return up && (S.back || !down);
}
function hud() {
  var long = S.lens === 'long';
  put('#tx-year', 'display', long ? '' : 'none'); put('#tx-when', 'hidden', !long);
  if (!long) return;
  var y = yearNow(), n = S.nodes[S.sel], live = y > S.liveFrom + 9.5;
  put('#tx-yr', 'textContent', live ? 'Now' : String(Math.round(y)));
  var ev = null; D.events.forEach(function (e) { if (!live && Math.abs(e.year - y) < 4 && e.year <= S.liveFrom) ev = e; });
  var g = n ? Math.abs(n.gen) : 0;
  put('#tx-yrsub', 'textContent', ev ? ev.year + ' · ' + ev.label : !S.touched ? 'Older is up, younger down · drag or tap' : live ? 'The living: names only, no dates' : !g ? 'Standing with ' + shortName(P[S.focus]) : g + (g === 1 ? ' generation ' : ' generations ') + (n.kind === 'desc' ? 'on' : 'back'));
  put('#tx-fly', 'textContent', S.flying ? 'Pause' : flyBack() ? 'Fly back' : 'Fly on');
  var e = routeEnds(), a = TNOW - e.old / KZ, b = TNOW - e.young / KZ;
  if (!S.sliding) put('#tx-slide', 'value', String(Math.round(1000 * clamp((y - a) / Math.max(1, b - a), 0, 1))));
  // the button under the picture: the rest of the family of the person you are with, which opens when they are put at the centre
  var here = standing(), up = S.nodes[here].kind === 'desc', cnt = here === S.focus ? 0 : kinCount(here, up), what = (up ? 'ancestor' : 'descendant') + (cnt === 1 ? '' : 's');
  put('#tx-desc', 'hidden', !cnt);
  if (cnt) {
    var say = 'Show all ' + cnt + ' ' + what + ' of ' + P[here].n + (up ? ', above' : ', below');
    put(TURN, 'textContent', cnt + ' ' + what); put('#tx-desc', 'title', say); put('#tx-desc', 'aria-label', say); put('#tx-desc', 'data-way', up ? 'up' : 'down');
  }
}

/* ---------- panel ---------- */
function relation(n) {
  if (!n || n.kind === 'focus') return 'In focus';
  var g = Math.abs(n.gen), sex = n.p.g, base = n.kind === 'anc' ? (sex === 'M' ? 'father' : sex === 'F' ? 'mother' : 'parent') : (sex === 'M' ? 'son' : sex === 'F' ? 'daughter' : 'child');
  var w = g === 1 ? base : g === 2 ? 'grand' + base : g === 3 ? 'great-grand' + base : (g - 2) + '× great-grand' + base;
  var fp = P[S.focus], first = fp.f ? fp.f.split(' ')[0] : fp.n;
  return first + '’s ' + w;
}
function thread(slug) { var l = lineOf[slug]; return 'var(--t-' + slug + ', var(--t-' + (l ? l.color : 'neutral') + ', var(--t-neutral)))'; }
function chip(id) { var p = P[id]; if (p.st === 'm') return ''; return '<button type="button" data-go="' + id + '" style="--thread: ' + thread(lineColour(id)) + '">' + esc(kinName(p)) + '</button>'; }
function chipList(ids) {
  return ids.map(function (i) {
    return P[i].st !== 'm' ? chip(i) : '<a class="tx-minor" href="' + D.priv + '/person/' + i + '" rel="noopener" title="Not named on this site. Opens in the research database (family login)">Living child</a>';
  }).join('');
}
function panel() {
  var id = S.sel && P[S.sel].st !== 'm' ? S.sel : S.focus, p = P[id], n = S.nodes[id], c = n ? n.col : lineColour(id), l = lineOf[c], h = '';      // never a child under 18
  var url = BASE + '/person/' + id + '/', ev = n ? n.ev : evidence(p), more = app.classList.contains('tx-more');
  h += '<div class="tx-who" style="--thread: ' + thread(c) + '"><span class="tx-band"></span><div>';
  h += '<p class="eyebrow">' + esc(relation(n)) + (l ? ' · ' + esc(l.short) + ' line' : '') + '</p>';
  h += '<h2><a href="' + url + '"' + EXT + '>' + esc(p.n) + '</a></h2>';
  if (p.st === 'f') {
    var pl = [place(p.bp), place(p.dp)].filter(function (s, i, a) { return s && a.indexOf(s) === i; });
    h += '<p class="tx-span">' + esc(years(p)) + (pl.length ? ' <span class="tx-place">' + esc(pl.join(' → ')) + '</span>' : '') + '</p>';
  } else h += '<p class="tx-span tx-note">Name and family links only</p>';
  h += '</div></div><div class="tx-acts">';
  if (id !== S.focus) h += '<button type="button" class="tx-btn" data-stand>Stand here</button>';
  h += '<a class="tx-btn tx-alt" href="' + url + '"' + EXT + '>Open their page</a>';
  h += '<button type="button" class="tx-btn tx-alt tx-more-btn" data-more aria-expanded="' + more + '">' + (more ? 'Less' : 'More') + '</button></div><div class="tx-rest">';
  if (p.st !== 'f') h += '<p class="tx-note">Shown this way for the living, and for anyone with no dates on record yet.</p>';
  if (p.st === 'f') {
    if (p.x) h += '<p class="tx-note">' + (p.x.t === 'Departure' || p.x.t === 'Emigration' ? 'Left ' : 'Arrived ') + (p.x.p ? esc(place(p.x.p)) + ', ' : '') + p.x.y + '.</p>';
    var e = p.ev, lab = { human: 'reviewed', ai: 'AI-checked', un: 'unreviewed', needs: 'needs a look', none: 'no facts yet' }[ev];
    h += '<p class="tx-note"><span class="stamp ' + (ev === 'none' ? 'un' : ev) + '">' + lab + '</span>' + (e[0] ? e[0] + (e[0] === 1 ? ' fact: ' : ' facts: ') + [e[1] ? e[1] + ' reviewed' : '', e[2] ? e[2] + ' AI-checked' : '', e[4] ? e[4] + ' flagged' : '', (e[3] - e[4]) > 0 ? (e[3] - e[4]) + ' unreviewed' : ''].filter(Boolean).join(', ') : '') + '</p>';
    if (n && n.kind !== 'desc' && !n.par[0] && !n.par[1]) h += '<p class="tx-note">The trail stops here: no parents on record.</p>';
    (p.rq || []).forEach(function (q) { h += '<p class="tx-note">Open question: <a href="' + BASE + '/research/#' + q[0] + '"' + EXT + '>' + esc(q[1]) + '</a></p>'; });
  }
  var groups = [];
  p.pf.forEach(function (fid) {
    var f = F[fid], k = f.k.filter(function (k) { return kidId(k) === id; })[0], rel = k ? kidRel(k) : '';
    var ids = [f.a, f.b].filter(Boolean); if (ids.length) groups.push([rel === 'Adopted' ? 'Adoptive parents' : rel ? rel + ' parents' : 'Parents', ids]);
  });
  if (partners(id).length) groups.push(['Partner', partners(id)]);
  if (children(id).length) groups.push(['Children', children(id)]);
  if (siblings(id).length) groups.push(['Brothers and sisters', siblings(id)]);
  if (groups.length) {
    h += '<dl class="tx-kin">';
    groups.forEach(function (g) { h += '<div><dt>' + g[0] + '</dt><dd>' + chipList(g[1]) + '</dd></div>'; });
    h += '</dl>';
    if (groups.some(function (g) { return g[1].some(function (i) { return P[i].st === 'm'; }); })) h += '<p class="tx-small">Children under 18 are shown as “Living child”, without their names. Each one opens in the research database, for family members who have the login.</p>';
  }
  h += '<p class="tx-about">' + (S.lens === 'fan' ? 'Each ring out is a generation back, father’s side to the left and mother’s to the right.' : 'Older is up and younger is down, father to the left and mother to the right.') + ' Living relatives are placed by generation, never against a year.</p>';
  h += '<p class="tx-about">Tap someone to walk to them. Double-tap, or Stand here, puts them at the centre: all their ancestors open above them and all their descendants below.</p></div>';
  $('#tx-panel').innerHTML = h;
}

/* ---------- controls ---------- */
function chips() {
  var on = S.mode === 'evidence', h = '<button type="button" class="tx-tog" data-evid aria-pressed="' + on + '">Evidence</button>';
  if (on) {
    var k = S.lens === 'fan' ? 'tx-key tx-fill' : 'tx-key';
    [['human', 'reviewed'], ['', 'AI-checked'], ['needs', 'needs a look'], ['un', 'unreviewed'], ['none', 'none yet']].forEach(function (e) { h += '<span class="' + k + '"><i class="' + e[0] + '"></i>' + e[1] + '</span>'; });
  } else {
    h += '<span class="tx-lbl">Follow a line</span>';
    D.lines.forEach(function (l) { h += '<button type="button" class="tx-chip" data-line="' + l.slug + '" aria-pressed="' + (S.line === l.slug) + '" style="--thread: ' + thread(l.slug) + '"><i></i>' + esc(l.short) + '</button>'; });
  }
  $('#tx-chips').innerHTML = h;
}
function sync() {
  app.querySelectorAll('[data-lens]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-lens') === S.lens)); });
  var long = S.lens === 'long';
  $('#tx-fly').hidden = !long; $('#tx-fly').setAttribute('aria-pressed', String(S.flying));
  $('#tx-fit').textContent = long ? 'Reset' : 'Fit';
  var noA = !S.anc.root.par[0] && !S.anc.root.par[1], noD = !S.desc.root.kids.length, who = P[S.focus].n;
  var none = S.lens !== 'fan' && noA && noD ? 'No parents or children on record for ' + who + '.' : S.lens === 'fan' && noA ? 'No parents on record for ' + who + '. Try the Long view or the Tree view.' : '';
  $('#tx-empty').hidden = !none; $('#tx-empty').textContent = none;
  chips();
  var hash = '#' + S.lens + '-' + S.focus + (S.line ? '-' + S.line : '');
  try { if (S.pushNext && !S.routing && location.hash && location.hash !== hash) history.pushState(null, '', hash); else history.replaceState(null, '', hash); } catch (e) {}
  S.pushNext = false;
}
function stopFly() { if (S.flying) { S.flying = false; sync(); } }
function setLens(l) { stopFly(); S.lens = l; S.panX = 0; S.whole = false; if (!S.nodes[S.sel]) S.sel = S.focus; setPath(S.sel); aim(false); sync(); panel(); }
function focusOn(id) {               // stand with someone: they become the centre, all their ancestors above them and all their descendants below
  if (!P[id] || P[id].st === 'm') return;
  var was = S.focus;
  stopFly(); S.pushNext = id !== was; build(id); S.sel = id; S.panX = 0; S.whole = false; S.back = true;
  var n = was && was !== id ? S.nodes[was] : null;          // the way you came stays marked, and is the route that way to begin with
  if (n) { S.from = was; S.trail = {}; for (var c = n; c; c = c.toward) S.trail[c.id] = 1; }
  setPath(id); aim(false); sync(); panel();
}
function openHere() { var here = standing(); if (S.lens === 'long' && here !== S.focus) focusOn(here); }      // the button under the picture
function select(id) {                // walk to someone in the picture; the route on their side of the centre now runs through them
  if (!S.nodes[id] || P[id].st === 'm') return;
  stopFly(); S.sel = id; S.line = null;
  var n = S.nodes[id];
  setPath(id); S.panX = 0;
  if (S.lens === 'long') { camT.tz = n.z; if (n.kind !== 'focus') S.back = n.kind === 'anc'; }
  sync(); panel(); go();
}
function followLine(slug) {
  var l = D.lines.filter(function (x) { return x.slug === slug; })[0]; if (!l) return;
  var i = l.path.indexOf(S.focus);
  if (i < 0) { stopFly(); build(l.path[0]); i = 0; S.sel = l.path[0]; }
  S.back = true; S.panX = 0; S.line = slug; setPath(null, l.path.slice(i));
  if (S.lens === 'long') { S.sel = S.focus; aim(false); S.flying = !reduce; if (reduce) { S.sel = l.path[l.path.length - 1]; camT.tz = S.nodes[S.sel].z; } }
  else { S.sel = l.path[l.path.length - 1]; S.whole = true; aim(false); }
  sync(); panel(); go();
}

function resize() {
  var r = stage.getBoundingClientRect(); if (r.width < 10 || r.height < 10) return;
  dpr = Math.min(window.devicePixelRatio || 1, 2.5); W = r.width; H = r.height;
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  if (!S.focus) return;
  longP = longParams(S.zoomLong || baseScale());
  if (S.lens === 'long') { longP.yT = hingeY(); ['theta', 'F', 'yT', 's'].forEach(function (k) { cam[k] = camT[k] = longP[k]; }); }
  else if (!S.moved) { frame(); for (var k in camT) cam[k] = camT[k]; }
  else cam.yT = camT.yT = H / 2;
  go();
}
function hit(x, y) {
  if (S.lens === 'fan' && S.wedge && S.fanMix > 0.9) {
    var w = S.wedge, dx = (x - w.cx) / w.s, dy = (w.cy - y) / w.s, r = Math.sqrt(dx * dx + dy * dy), a = Math.atan2(dx, dy);
    if (r <= R0) return S.focus;
    var g = Math.floor((r - R0) / RW) + 1, f = null;
    S.anc.list.forEach(function (n) { if (n.gen === g && a >= n.a0 && a <= n.a1) f = n.id; });
    return f;
  }
  var best = null, bd = Infinity;
  S.hits.forEach(function (h) {
    if (h.box) { if (x >= h.box.x && x <= h.box.x + h.box.w && y >= h.box.y && y <= h.box.y + h.box.h && bd > 0.5) { best = h.id; bd = 0.5; } return; }
    var d = Math.hypot(h.x - x, h.y - y); if (d <= Math.max(h.r + 9, 17) && d < bd) { bd = d; best = h.id; }
  });
  return best;
}
function zoomBy(f, px, py) {
  if (S.lens === 'long') { S.zoomLong = clamp((S.zoomLong || baseScale()) * f, 90, 320); aim(false); return; }
  px = px == null ? W / 2 : px; py = py == null ? H / 2 : py;
  var s0 = camT.s, s1 = clamp(s0 * f, 2.5, 260), wx = camT.tx + (px - W / 2) / s0, wzz = camT.tz + (H / 2 - py) / s0;
  camT.s = s1; camT.tx = wx - (px - W / 2) / s1; camT.tz = wzz - (H / 2 - py) / s1; S.moved = true; go();
}
function travel(dz) {                // move through time in the long view, staying on the routes: more is older, which is up
  stopFly(); if (dz) S.back = dz > 0;
  var e = routeEnds(); camT.tz = clamp(camT.tz + dz, e.young - 0.3, e.old + 0.3); go();
}

function wire() {
  var ptr = {}, drag = null, pinch = 0, tapT = 0, tapId = null, tapAt = null;
  cv.addEventListener('pointerdown', function (e) {
    cv.setPointerCapture(e.pointerId); ptr[e.pointerId] = { x: e.offsetX, y: e.offsetY };
    var ids = Object.keys(ptr);
    if (ids.length === 1) drag = { x: e.offsetX, y: e.offsetY, sx: e.offsetX, sy: e.offsetY, moved: false };
    else if (ids.length === 2) { var a = ptr[ids[0]], b = ptr[ids[1]]; pinch = Math.hypot(a.x - b.x, a.y - b.y); drag = null; }
    touched();
  });
  cv.addEventListener('pointermove', function (e) {
    if (!ptr[e.pointerId]) return; ptr[e.pointerId] = { x: e.offsetX, y: e.offsetY };
    var ids = Object.keys(ptr);
    if (ids.length === 2 && pinch) {
      var a = ptr[ids[0]], b = ptr[ids[1]], d = Math.hypot(a.x - b.x, a.y - b.y);
      if (d > 0) { zoomBy(d / pinch, (a.x + b.x) / 2, (a.y + b.y) / 2); pinch = d; }
      return;
    }
    if (!drag) return;
    var dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
    if (!drag.moved && Math.hypot(e.offsetX - drag.sx, e.offsetY - drag.sy) < 6) return;
    drag.moved = true; cv.classList.add('tx-drag'); drag.x = e.offsetX; drag.y = e.offsetY;
    if (S.lens === 'long') {
      var zvT = cam.F / cam.s, h = zvT * Math.sin(cam.theta) - ((H / 2 - cam.yT) / cam.s) * Math.cos(cam.theta);
      S.panX = clamp(S.panX - dx / (W / 2 - 26), -1.6, 1.6); travel(dy / (cam.s * h / zvT));                 // the picture follows the finger: pull it down and older people come to you
    } else { camT.tx -= dx / cam.s; camT.tz += dy / cam.s; cam.tx = camT.tx; cam.tz = camT.tz; S.moved = true; go(); }
  });
  function up(e) {
    var d = drag; delete ptr[e.pointerId]; if (Object.keys(ptr).length < 2) pinch = 0; cv.classList.remove('tx-drag');
    if (!d || d.moved || Object.keys(ptr).length) { drag = null; return; }
    drag = null;
    var now = Date.now(), again = tapId && now - tapT < 400 && Math.hypot(e.offsetX - tapAt.x, e.offsetY - tapAt.y) < 30;
    if (again) {                     // a second tap in the same place means the person first tapped, who has started to move:
      var who = tapId; tapId = null; // put them at the centre
      if (who !== S.focus) focusOn(who);
      return;
    }
    var id = hit(e.offsetX, e.offsetY);
    tapId = id; tapT = now; tapAt = { x: e.offsetX, y: e.offsetY };
    if (id) select(id);
  }
  cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', function (e) { delete ptr[e.pointerId]; drag = null; pinch = 0; cv.classList.remove('tx-drag'); });
  cv.addEventListener('wheel', function (e) {
    e.preventDefault(); touched();
    var dy = e.deltaMode === 1 ? e.deltaY * 30 : e.deltaY, dx = e.deltaMode === 1 ? e.deltaX * 30 : e.deltaX;
    if (e.ctrlKey || e.metaKey) { zoomBy(Math.exp(-dy * 0.01), e.offsetX, e.offsetY); return; }
    if (S.lens === 'long') { S.panX = clamp(S.panX + dx / (W / 2 - 26), -1.6, 1.6); travel(-dy * 0.011); }            // as a page scrolls: down the wheel is down the picture, to younger people
    else zoomBy(Math.exp(-dy * 0.0022), e.offsetX, e.offsetY);
  }, { passive: false });
  cv.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;        // the browser's own shortcuts: zoom, new tab
    var k = e.key, long = S.lens === 'long', used = true;
    if (k === 'ArrowUp') long ? travel(1.4) : (camT.tz += 60 / cam.s, go());
    else if (k === 'ArrowDown') long ? travel(-1.4) : (camT.tz -= 60 / cam.s, go());
    else if (k === 'ArrowLeft') long ? (S.panX = clamp(S.panX - 0.25, -1.6, 1.6), go()) : (camT.tx -= 60 / cam.s, go());
    else if (k === 'ArrowRight') long ? (S.panX = clamp(S.panX + 0.25, -1.6, 1.6), go()) : (camT.tx += 60 / cam.s, go());
    else if (k === '+' || k === '=') zoomBy(1.3); else if (k === '-') zoomBy(1 / 1.3);
    else if (k === 'Enter' && S.sel && S.sel !== S.focus) focusOn(S.sel);
    else used = false;
    if (used) { e.preventDefault(); touched(); }
  });
  function touched() { if (!S.touched) { S.touched = true; go(); } }
  app.querySelectorAll('[data-lens]').forEach(function (b) { b.addEventListener('click', function () { setLens(b.getAttribute('data-lens')); touched(); }); });
  $('#tx-desc').addEventListener('click', function () { touched(); openHere(); });
  $('#tx-fly').addEventListener('click', function () {
    touched(); if (S.flying) { stopFly(); return; }
    var e = routeEnds(), back = flyBack(), end = back ? e.old : e.young;
    if (Math.abs(end - camT.tz) < 0.2) return;             // no one either way
    S.back = back;
    if (reduce) { camT.tz = end; go(); return; }
    S.flying = true; S.panX = 0; sync(); go();
  });
  $('#tx-zin').addEventListener('click', function () { zoomBy(1.35); }); $('#tx-zout').addEventListener('click', function () { zoomBy(1 / 1.35); });
  $('#tx-fit').addEventListener('click', function () { stopFly(); S.zoomLong = 0; S.panX = 0; if (S.lens === 'long') { S.sel = S.focus; S.line = null; S.back = true; setPath(S.focus, null, true); panel(); chips(); } else S.whole = true; aim(false); });
  $('#tx-slide').addEventListener('input', function () {
    S.sliding = true; shown['#tx-slidevalue'] = this.value; stopFly(); touched();
    var e = routeEnds(), a = TNOW - e.old / KZ, b = TNOW - e.young / KZ, y = a + (b - a) * this.value / 1000;
    camT.tz = clamp(wz(y), e.young, e.old); go();
  });
  $('#tx-slide').addEventListener('change', function () { S.sliding = false; });
  $('#tx-chips').addEventListener('click', function (e) {
    if (e.target.closest('[data-evid]')) { S.mode = S.mode === 'evidence' ? 'lines' : 'evidence'; sync(); go(); return; }
    var b = e.target.closest('[data-line]'); if (b) { touched(); followLine(b.getAttribute('data-line')); }
  });
  $('#tx-panel').addEventListener('click', function (e) {
    var b = e.target.closest('[data-go]'); if (b) { focusOn(b.getAttribute('data-go')); return; }
    if (e.target.closest('[data-stand]')) focusOn(S.sel);
    if (e.target.closest('[data-more]')) { app.classList.toggle('tx-more'); panel(); }
  });
  // find a person
  var q = $('#tx-q'), res = $('#tx-results'), index = Object.keys(P).filter(function (id) { return P[id].st !== 'm'; }).map(function (id) { return { id: id, k: fold(P[id].n) }; });
  function search() {
    var v = fold(q.value.trim()); if (v.length < 2) { res.hidden = true; return; }
    var hits = index.filter(function (o) { return o.k.indexOf(v) >= 0; }).slice(0, 12);
    res.innerHTML = hits.length ? hits.map(function (o) { var p = P[o.id]; return '<li><button type="button" data-id="' + o.id + '">' + esc(p.n) + '<small>' + esc(years(p)) + '</small></button></li>'; }).join('') : '<li><button type="button" disabled>No one by that name</button></li>';
    res.hidden = false;
  }
  q.addEventListener('input', search); q.addEventListener('focus', search);
  q.addEventListener('keydown', function (e) { if (e.key === 'Escape') { res.hidden = true; q.blur(); } if (e.key === 'Enter') { var b = res.querySelector('[data-id]'); if (b) b.click(); } });
  res.addEventListener('click', function (e) { var b = e.target.closest('[data-id]'); if (!b) return; res.hidden = true; q.value = ''; touched(); focusOn(b.getAttribute('data-id')); });
  document.addEventListener('pointerdown', function (e) { if (!e.target.closest('.tx-find')) res.hidden = true; });
  // theme and size
  new MutationObserver(function () { readTheme(); wCache = {}; go(); }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class', 'style'] });
  var mq = matchMedia('(prefers-color-scheme: dark)'), onScheme = function () { readTheme(); go(); };
  if (mq.addEventListener) mq.addEventListener('change', onScheme); else if (mq.addListener) mq.addListener(onScheme);
  new ResizeObserver(resize).observe(stage);
  new ResizeObserver(function () { S.footH = $('.tx-foot').offsetHeight; go(); }).observe($('.tx-foot'));
  window.addEventListener('hashchange', route);
}
function route() {                  // #long-I0141 opens a view on a person; #long-I0141-wood also follows that line
  var m = /^#(long|ahead|tree|fan)-(I\d+)(?:-([a-z]+))?$/.exec(location.hash || '');
  if (!m || !P[m[2]] || P[m[2]].st === 'm') return false;
  var lens = m[1] === 'ahead' ? 'long' : m[1];              // "ahead" was the long view turned round, before older was always up
  if (m[2] === S.focus && lens === S.lens && (m[3] || null) === S.line) return true;
  S.routing = true; S.lens = lens; focusOn(m[2]);
  if (m[3] && lineOf[m[3]]) followLine(m[3]);
  S.routing = false;
  return true;
}

function start(data) {
  D = data; P = D.P; F = D.F;
  D.lines.forEach(function (l) { lineOf[l.slug] = l; l.path.forEach(function (id) { if (!pathLine[id]) pathLine[id] = l.slug; }); });
  readTheme(); resize(); wire();
  if (!route()) { build(D.root); S.sel = D.root; setPath(D.root); sync(); panel(); }
  aim(true);
  if (document.fonts && document.fonts.load) Promise.all(['700 13px "Bricolage Grotesque"', '800 15px "Bricolage Grotesque"', '500 12px "Bricolage Grotesque"', '600 condensed 10px "Bricolage Grotesque"', '500 10px "IBM Plex Mono"']
    .map(function (f) { return document.fonts.load(f, 'Konečný Štěpán'); })).then(function () { wCache = {}; go(); }).catch(function () {});
}
if (app.hasAttribute('data-debug')) window.__tx = { S: S, cam: cam, camT: camT, select: select, focusOn: focusOn, open: openHere, people: function () { return P; } };
fetch(app.getAttribute('data-src')).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(start).catch(function (err) {
  $('#tx-panel').innerHTML = '<p class="tx-note">The tree did not load (' + esc(err.message || err) + '). Reload the page to try again.</p>';
  if (window.console) console.error(err);
});
})();
