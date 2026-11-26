/* family.konecny.net — the tree explorer (/tree/)

   One scene, three ways of looking at it. Everything is drawn on one canvas from /data/tree.json, which
   site/build.py writes from the published bundle: names and family links for everyone, and years, towns and
   review states only for people the site shows in full.

   - Long view: depth is time. The person you stand with is at the front; each generation behind them shares the
     width of the view (father's side left, mother's side right) and narrows to the branch you walk into.
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
          fanMix: 0, fanT: 0, aAnc: 1, aDesc: 0, tween: 1, fade: 1, flying: false, panX: 0, win: { L: 0, w: 1 }, liveFrom: 1950, tMin: 1500, tMax: 2000, line: null, hits: [], touched: false, kin: null, pick: null, me: null };
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

/* ---------- how two people are related ----------
   Blood first: where their ancestries meet, and how many generations each is from there. Failing that, by one
   marriage or partnership at either end. Children under 18 are never one of the two, and cannot be on the way:
   everyone between two people and the ancestors they share is a parent of someone.
   A person's ancestry is the family they were born into, as everywhere in the tree. Where someone has a second family
   on record, an adoptive one, that is followed only when nothing else joins the two people, and the relationship is
   then said to be by adoption. */
function adopters(pid) {             // the parents of a second family, where the child is recorded as adopted
  var own = birthFam(pid), out = [];
  P[pid].pf.forEach(function (fid) {
    var f = F[fid]; if (!f || fid === own) return;
    var k = f.k.filter(function (k) { return kidId(k) === pid; })[0];
    if (k && typeof k !== 'string' && (k[1] === 'Adopted' || k[2] === 'Adopted')) [f.a, f.b].forEach(function (x) { if (x && out.indexOf(x) < 0) out.push(x); });
  });
  return out;
}
function ups(id, all) {              // every ancestor of a person by the fewest generations: id -> [generations, the child on the way]
  var m = {}, q = [id]; m[id] = [0, null];
  while (q.length) { var x = q.shift(); (all ? parents(x).concat(adopters(x)) : parents(x)).forEach(function (p) { if (p && !m[p]) { m[p] = [m[x][0] + 1, x]; q.push(p); } }); }
  return m;
}
function wayDown(m, top) { var out = []; for (var x = top; x != null; x = m[x][1]) out.push(x); return out; }      // from a shared ancestor down to the person
function blood(a, b, all) {
  var A = ups(a, all), B = ups(b, all), best = null, tops = [];
  Object.keys(A).forEach(function (c) { if (B[c] && (best == null || A[c][0] + B[c][0] < best)) best = A[c][0] + B[c][0]; });
  if (best == null) return null;
  Object.keys(A).forEach(function (c) { if (B[c] && A[c][0] + B[c][0] === best) tops.push(c); });
  tops.sort(function (x, y) { return Math.abs(A[x][0] - B[x][0]) - Math.abs(A[y][0] - B[y][0]) || (pathLine[y] ? 1 : 0) - (pathLine[x] ? 1 : 0) || (P[y].g === 'M' ? 1 : 0) - (P[x].g === 'M' ? 1 : 0); });
  var top = tops[0], i = A[top][0], j = B[top][0], half = false;
  tops = tops.filter(function (c) { return A[c][0] === i && B[c][0] === j; });
  var wa = wayDown(A, top), wb = wayDown(B, top);
  if (tops.length === 1 && i && j) {                        // one shared ancestor and two different partners of theirs: a half relation
    var other = function (kid) { return parents(kid).filter(function (x) { return x && x !== top; })[0]; }, oa = other(wa[1]), ob = other(wb[1]);
    half = !all && !!(oa && ob && oa !== ob);
  }
  var k = { i: i, j: j, tops: tops, half: half, wa: wa, wb: wb };
  if (all) { var kb = blood(a, b); if (kb && kb.i === i && kb.j === j) return kb; k.adopt = true; }      // by adoption only where birth does not give the same
  return k;
}
var kinMemo2 = {};
function kinOf(a, b) {
  var key = a + '|' + b;
  if (!kinMemo2[key]) {
    var k = kinFind(a, b);
    if (k.kind === 'none') k = kinFind(a, b, true);         // nothing by birth or marriage: is there a link by adoption?
    kinMemo2[key] = k;
  }
  return Object.assign({}, kinMemo2[key]);
}
function kinFind(a, b, all) {
  if (a === b) return { kind: 'same' };
  var k = blood(a, b, all), best = null;
  if (k) { k.kind = 'blood'; k.x = a; k.y = b; return k; }
  if (partners(a).indexOf(b) >= 0) return { kind: 'pair' };
  var take = function (k, kind, x, y) { if (k && (!best || k.i + k.j < best.i + best.j)) { k.kind = kind; k.x = x; k.y = y; best = k; } };
  partners(a).forEach(function (s) { take(blood(s, b, all), 'wedA', s, b); });
  partners(b).forEach(function (s) { take(blood(a, s, all), 'wedB', a, s); });
  if (!best) partners(a).forEach(function (sa) { partners(b).forEach(function (sb) { take(blood(sa, sb, all), 'wedAB', sa, sb); }); });
  if (best) return best;
  // the family of someone who married in: a blood relative of a, their partner, and that partner's blood relative b
  var A = ups(a, all), kin = {}, q = Object.keys(A), cost = Infinity;
  q.forEach(function (x) { kin[x] = 1; });
  while (q.length) children(q.pop()).forEach(function (c) { if (!kin[c]) { kin[c] = 1; q.push(c); } });
  Object.keys(kin).forEach(function (x) {
    partners(x).forEach(function (y) {
      if (kin[y]) return; var k2 = blood(y, b, all); if (!k2) return;
      var k1 = blood(a, x, all); if (!k1) return;         // x is among the children by an adoption, and this pass follows birth
      var c = k1.i + k1.j + k2.i + k2.j;
      if (c < cost) { cost = c; k1.kind = 'wedM'; k1.x = a; k1.y = x; k1.m = y; k1.k2 = k2; best = k1; }
    });
  });
  return best || { kind: 'none' };
}
function greats(n, word) { return n <= 0 ? word : n === 1 ? 'great-' + word : n + '× great-' + word; }
function relName(i, j, g, half) {    // what someone is to you, when you are i generations below where you meet and they are j
  var m = g === 'M', f = g === 'F', h = half ? 'half ' : '';
  if (!j) return i === 1 ? (m ? 'father' : f ? 'mother' : 'parent') : greats(i - 2, 'grand' + (m ? 'father' : f ? 'mother' : 'parent'));
  if (!i) return j === 1 ? (m ? 'son' : f ? 'daughter' : 'child') : greats(j - 2, 'grand' + (m ? 'son' : f ? 'daughter' : 'child'));
  if (i === 1 && j === 1) return (half ? 'half-' : '') + (m ? 'brother' : f ? 'sister' : 'brother or sister');
  if (j === 1) return h + greats(i - 2, m ? 'uncle' : f ? 'aunt' : 'uncle or aunt');
  if (i === 1) return h + greats(j - 2, m ? 'nephew' : f ? 'niece' : 'nephew or niece');
  var deg = Math.min(i, j) - 1, rem = Math.abs(i - j);
  return h + (['', 'first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh', 'eighth', 'ninth', 'tenth'][deg] || deg + 'th') + ' cousin' + (rem ? (rem === 1 ? ' once' : rem === 2 ? ' twice' : ' ' + rem + ' times') + ' removed' : '');
}
function pairWord(x, y) {            // what y is to x: husband or wife where a marriage of two people shown in full is on record, partner otherwise
  var wed = P[x].fs.some(function (fid) { var f = F[fid]; return f.w && (f.a === y || f.b === y); });
  return wed ? (P[y].g === 'M' ? 'husband' : P[y].g === 'F' ? 'wife' : 'partner') : 'partner';
}
function firstName(id) { var p = P[id]; return p.f ? p.f.split(' ')[0] : p.n; }
function whose(id) { return id === S.me ? 'your' : firstName(id) + '’s'; }
function kinWhat(k, a, b) {          // what b is to a, without saying whose: ["great-uncle"], or ["great-uncle, Name", "wife"] for "the wife of … great-uncle, Name"
  var by = function (g) { return g.adopt ? ' by adoption' : ''; };
  var r = k.tops ? relName(k.i, k.j, P[k.y].g, k.half) + by(k) : '';
  if (k.kind === 'pair') return [pairWord(a, b)];
  if (k.kind === 'blood') return [r];
  if (k.kind === 'wedA') return [pairWord(a, k.x) + '’s ' + r];
  if (k.kind === 'wedB') return [r + ', ' + P[k.y].n, pairWord(k.y, b)];
  if (k.kind === 'wedM') return [r, relName(k.k2.i, k.k2.j, P[b].g, k.k2.half) + by(k.k2) + ' of ' + P[k.m].n + ', the ' + pairWord(k.y, k.m)];      // "the father of Marie Nováková, the wife of … great-uncle"
  if (k.x === k.y) return [pairWord(a, k.x) + ', ' + P[k.x].n, pairWord(k.x, b)];      // two partners of one person: "the wife of Marjorie's husband, Frank Konecny"
  return [pairWord(a, k.x) + '’s ' + r + ', ' + P[k.y].n, pairWord(k.y, b)];
}
function kinSay(k, a, b) {           // in words: what b is to a. "short" leaves out whose, where that is plain from the place it is shown
  var me = b === S.me, who = me ? 'You are' : P[b].n + ' is';
  if (k.kind === 'same') return { say: (me ? 'This is you' : 'This is ' + P[b].n) + '.' };
  if (k.kind === 'none') return { say: 'The tree shows no relationship between ' + P[a].n + ' and ' + P[b].n + ', by blood, by adoption or through one marriage.' };
  var w = kinWhat(k, a, b), o = { say: who + ' ' + (w[1] ? 'the ' + w[1] + ' of ' : '') + whose(a) + ' ' + w[0] + '.', short: w[1] ? w[1] + ' of ' + whose(a) + ' ' + w[0] : w[0] };
  if (k.tops && k.i && k.j && !k.adopt) o.meet = (k.kind !== 'blood' ? firstName(k.x) + ' and ' + firstName(k.y) + ' both descend from ' : a === S.me || me ? 'You both descend from ' : 'They both descend from ') + k.tops.map(function (t) { return P[t].n; }).join(' and ') + '.';
  return o;
}
function refPerson() { return S.me && P[S.me] ? S.me : D.root; }      // who relationships are named from: you, once you have said who you are

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
  S.kin = null;
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
    var tot = 0, at = a; n.kids.forEach(function (k) { tot += 1 + Math.sqrt(k.size - 1); });
    n.kids.forEach(function (k) { var w = (b - a) * (1 + Math.sqrt(k.size - 1)) / tot; iv(k, at, at + w); at += w; });
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
  S.trail = null; S.from = null;
}
function pos(n) {                    // where a person is now, in ground units: tree place, fan place, or on the way between
  var x = lerp(n.x, n.fx, S.fanMix), z = lerp(n.z, n.fz, S.fanMix);
  if (S.tween < 1 && n.from) { var k = S.tween * S.tween * (3 - 2 * S.tween); x = lerp(n.from.x, x, k); z = lerp(n.from.z, z, k); }
  return { x: x, z: z };
}

/* ---------- the route the camera follows ---------- */
function setPath(via, ids) {
  var chain = [], n;
  if (ids) chain = ids.filter(function (id) { return S.nodes[id] && P[id].st !== 'm'; });
  else {
    n = S.nodes[via] || S.nodes[S.focus];
    var kind = n.kind, f = S.from && S.nodes[S.from];
    if (kind === 'focus') {
      kind = S.lens === 'long' && !S.back ? 'desc' : 'anc';
      if (f && f.kind === kind) n = f;                     // from the centre, the way you came is the way on, when it is the way you face
    }
    for (var c = n; c; c = c.toward) chain.unshift(c.id);
    while (true) {                   // carry on to the far end of that branch, taking the deeper side at each fork
      var next = null;
      if (kind === 'anc') n.par.forEach(function (q) { if (q && (!next || q.reach > next.reach)) next = q; });
      else n.kids.forEach(function (k) { if (k.p.st !== 'm' && (!next || k.reach > next.reach)) next = k; });   // never a child under 18: they have no name here
      if (!next) break; chain.push(next.id); n = next;
    }
  }
  S.path = chain; S.onPath = {}; chain.forEach(function (id) { S.onPath[id] = 1; });
  S.stations = chain.map(function (id, i) {
    var n = S.nodes[id], base = i > 0 ? S.nodes[chain[i - 1]] : null;
    return { id: id, z: n.z, x: n.x, L: base ? base.u0 : 0, w: base ? base.u1 - base.u0 : 1 };
  });
}
function windowAt(z) {               // the share of the fan in view at depth z: it narrows smoothly from one station's to the next
  var st = S.stations, i, a, b;
  for (i = 0; i < st.length - 1; i++) {
    a = st[i]; b = st[i + 1];
    if ((z - a.z) * (z - b.z) <= 0 && a.z !== b.z) {
      var u = smooth(0, 1, (z - a.z) / (b.z - a.z)), r = b.w / a.w;
      if (Math.abs(1 - r) < 1e-6) return { L: lerp(a.L, b.L, u), w: a.w };
      var k = Math.pow(r, u); return { L: a.L + (b.L - a.L) * (1 - k) / (1 - r), w: a.w * k };
    }
  }
  a = Math.abs(z - st[0].z) < Math.abs(z - st[st.length - 1].z) ? st[0] : st[st.length - 1];
  return { L: a.L, w: a.w };
}
function stationAt(z) { var best = null, d = Infinity; S.stations.forEach(function (s) { var e = Math.abs(s.z - z); if (e < d) { d = e; best = s; } }); return best; }

/* ---------- camera ---------- */
function longParams(s0) {            // a long lens held low: the person in focus near the bottom, the horizon near the top
  var yH = (W < 700 ? 0.2 : 0.17) * H, yT = clamp(H - 86 - 96, 0.45 * H, (W < 700 ? 0.61 : 0.62) * H), r1 = 0.6, th = 0.2, Fp = 1000;
  for (var i = 0; i < 8; i++) { Fp = s0 * (r1 / (1 - r1)) * GEN * KZ * Math.cos(th); th = Math.atan((H / 2 - yH) / Fp); }
  return { theta: th, F: Fp, yT: yT, s: s0 };
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
    camT.theta = longP.theta; camT.F = longP.F; camT.yT = longP.yT; camT.s = longP.s; camT.tz = n.z; camT.tx = n.x;
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
  var dir = S.lens === 'long' && !S.back ? -1 : 1;                   // looking ahead from an ancestor flips the ground
  var pin = 0.13 * tilt;
  var boxes = [], late = [], walls = [];
  var free = function (b) { for (var i = 0; i < boxes.length; i++) { var o = boxes[i]; if (b.x < o.x + o.w && b.x + b.w > o.x && b.y < o.y + o.h && b.y + b.h > o.y) return false; } return true; };
  function proj(x, z, y) {
    var dz = dir * (z - cam.tz) + Dz, hh = h - (y || 0), zv = hh * sin + dz * cos;
    if (zv <= NEAR) return null;
    var k = cam.F / zv; return { x: W / 2 + (x - cam.tx) * k, y: H / 2 - (-hh * cos + dz * sin) * k, s: k };
  }
  var win = S.win, HsF = W / 2 - 26, HsN = Math.min(clamp(0.24 * W, 150, 290), HsF);
  function span(d) { return HsN + (HsF - HsN) * (1 - Math.exp(-Math.max(0, d) / 9)); }
  function place(x, z, uc, y) {
    var q = proj(x, z, y); if (!q) return null;
    if (tilt > 0) q.x = lerp(q.x, W / 2 + clamp(((uc - win.L) / win.w) * 2 - 1 - S.panX, -9, 9) * span(dir * (z - cam.tz)), tilt);
    return q;
  }
  function fog(s) { if (!tilt) return 1; var q = s / cam.s; return lerp(1, clamp(0.22 + 0.78 * smooth(0.07, 0.5, q), 0, 1) * (1 - smooth(1.9, 2.7, q)), tilt); }

  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.globalAlpha = 1; ctx.fillStyle = col.paper; ctx.fillRect(0, 0, W, H);
  var timeA = 1 - smooth(0, 0.5, S.fanMix), fanA = tilt < 0.02 ? smooth(0.55, 1, S.fanMix) : 0;
  var yH = tilt > 0.3 ? H / 2 - cam.F * Math.tan(th) : -1e4;         // the horizon, once the camera is low enough to see one

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
    var w = pos(n), g = place(w.x, w.z, n.uc, 0); if (!g) return;
    var hd = pin ? place(w.x, w.z, n.uc, pin) : g; if (!hd) return;
    var v = { n: n, gx: g.x, gy: g.y, x: hd.x, y: hd.y, s: g.s, a: a * fog(g.s) * S.fade, w: w };
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
      if (tilt === 1) {                                          // in the long view, a link wholly outside the window is not drawn
        var f1 = ((n.uc - win.L) / win.w) * 2 - 1 - S.panX, f2 = ((t.uc - win.L) / win.w) * 2 - 1 - S.panX;
        if ((f1 < -1.25 && f2 < -1.25) || (f1 > 1.25 && f2 > 1.25)) return;
      }
      var both = S.trail && S.trail[n.id] && S.trail[t.id], on = (S.onPath[n.id] && S.onPath[t.id]) || (S.kin && both), home = !on && both, wa = pos(n), wb = pos(t), pts = [], i, k, e, q, sMax = 0;
      var steps = ((a || b).s < 22 && tilt === 1) ? 5 : 12;
      for (i = 0; i <= steps; i++) { k = i / steps; e = k * k * (3 - 2 * k); q = place(lerp(wb.x, wa.x, e), lerp(wb.z, wa.z, k), lerp(t.uc, n.uc, e), 0); if (q) { pts.push(q); sMax = Math.max(sMax, q.s); } }
      if (pts.length < 2 || sMax < 1.2) return;
      var al = kindA * timeA * fog(pts[pts.length - 1].s) * S.fade;
      if (pts[0].x < -40 && pts[pts.length - 1].x < -40 || pts[0].x > W + 40 && pts[pts.length - 1].x > W + 40) return;
      ctx.globalAlpha = al * (on ? 0.95 : home ? 0.82 : col.dark ? 0.5 : 0.42);
      ctx.strokeStyle = S.mode === 'evidence' ? col.muted : (col[n.col] || col.neutral);
      ctx.lineWidth = clamp(pts[pts.length - 1].s * (on ? 0.05 : home ? 0.037 : 0.026), on ? 1.4 : home ? 1.1 : 0.6, on ? 5 : home ? 4 : 3);
      ctx.beginPath(); ctx.moveTo(pts[0].x, pts[0].y); for (i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y); ctx.stroke();
    });
    // where the trail stops, no parents on record: a brick wall. A bar across the end of the line, red and with a
    // question mark where a research card is open on that person; tapping it opens the question in the panel
    ctx.setLineDash([2, 4]); ctx.lineWidth = 1;
    S.list.forEach(function (n) {
      if (n.kind === 'desc' || !P2[n.id]) return; var v = P2[n.id]; if (v.s < 9) return;
      if (n.par[0] || n.par[1] || n.dup[0] || n.dup[1]) return;
      var wa = v.a * S.aAnc * timeA; if (wa < 0.02) return;      // turned round, the ancestors are out of the picture and so are their walls
      var e = place(v.w.x, v.w.z + dir * 1.5, n.uc, pin); if (!e) return;            // from the person, a little way further back
      var need = clamp(v.s * 0.085, 1.7, 8.5) + clamp(v.s * 0.06, 4, 11);
      if (v.y - e.y < need) e.y = v.y - need;
      ctx.globalAlpha = wa * 0.7; ctx.strokeStyle = col.muted; ctx.beginPath(); ctx.moveTo(v.x, v.y); ctx.lineTo(e.x, e.y); ctx.stroke();
      if (n.p.st === 'f' && e.x > -20 && e.x < W + 20) walls.push({ id: n.id, x: e.x, y: e.y, s: v.s, a: wa, q: !!(n.p.rq && n.p.rq.length) });
    });
    ctx.setLineDash([]);
  }

  // haze toward the horizon
  if (tilt > 0.3) {
    var hz = ctx.createLinearGradient(0, yH - 8, 0, yH + 46); hz.addColorStop(0, col.paper); hz.addColorStop(1, col.paper0);
    ctx.globalAlpha = smooth(0.3, 0.8, tilt); ctx.fillStyle = col.paper; ctx.fillRect(0, 0, W, Math.max(0, yH - 8)); ctx.fillStyle = hz; ctx.fillRect(0, yH - 8, W, 54);
  }

  if (timeA > 0.01) {                // the walls themselves, in front of the haze
    ctx.lineCap = 'butt';
    walls.forEach(function (w) {
      var half = clamp(w.s * 0.075, 3, 9), th = clamp(w.s * 0.024, 1.5, 3.5);
      ctx.globalAlpha = w.a * (w.q ? 1 : 0.8); ctx.strokeStyle = w.q && S.mode !== 'evidence' ? col.needs : col.muted; ctx.lineWidth = th;
      ctx.beginPath(); ctx.moveTo(w.x - half, w.y); ctx.lineTo(w.x + half, w.y); ctx.stroke();
      if (w.q && w.s >= 30) {
        var fs = clamp(w.s * 0.11, 9.5, 13);
        ctx.fillStyle = S.mode !== 'evidence' ? col.needs : col.muted; ctx.font = '800 ' + fs + 'px ' + DISP; ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'; ctx.fillText('?', w.x, w.y - th);
        boxes.push({ x: w.x - half, y: w.y - th - fs, w: half * 2, h: fs + th });
      }
    });
    ctx.lineCap = 'round';
  }

  // people, far to near
  var dotA = 1 - fanA; S.hits = [];
  if (dotA > 0.5 && S.aAnc > 0.5) walls.forEach(function (w) { if (w.s >= 22) S.hits.push({ id: w.id, x: w.x, y: w.y - 4, r: 5, s: w.s, wall: w.q ? 2 : 1 }); });
  vis.sort(function (a, b) { return a.s - b.s; });
  if (dotA > 0.01) vis.forEach(function (v) {
    var n = v.n, p = n.p, s = v.s, r = clamp(s * 0.085, 1.7, 8.5), c = nodeColour(n), al = v.a * dotA;
    if (p.st === 'm') r *= 0.7;
    if (tilt > 0 && n.kind !== 'focus') r = lerp(r, Math.min(r, Math.max(1.5, ((n.u1 - n.u0) / win.w) * span(dir * (v.w.z - cam.tz)) * 0.84)), tilt);
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
      if (s >= 132) {
        var need = Math.max(tw('800 15.5px ' + DISP, p.f && p.s ? p.s : p.n), tw('500 12px ' + DISP, p.f && p.s ? p.f : '')) + 24;
        var cw = clamp(Math.max(s - 16, need), 116, Math.min(190, tilt > 0.5 ? HsN - 12 : 190)), ch = (p.st === 'f' ? 78 : 64) + (p.x && S.mode === 'lines' ? 13 : 0);
        b = { x: clamp(v.x - cw / 2, 6, W - cw - 6), y: v.y + v.r + 8, w: cw, h: ch };
        if (free(b)) { boxes.push(b); cards.push({ v: v, b: b }); return; }
      }
      if (s < 30 && !v.key) return;
      var big = s >= 60, fs = big ? clamp(11.5 + (s - 60) / 40, 11.5, 13.5) : clamp(10 + (s - 22) / 30, 10, 11.5), rows = [], yr = p.st === 'f' && (p.bt || p.dt) ? years(p) : '';
      if (v.key && fs < 11) fs = 11;
      fs = Math.round(fs * 2) / 2;
      if (big && p.f && p.s) rows.push([p.f.split(' ')[0], '500 ' + (fs - 1.5) + 'px ' + DISP, fs + 0.5, col.muted], [p.s, '700 ' + fs + 'px ' + DISP, fs + 3, p.st === 'f' ? col.ink : col.muted]);
      else rows.push([shortName(p), (big || v.key ? '700 ' : '600 ') + fs + 'px ' + DISP, fs + 3, p.st === 'f' ? col.ink : col.muted]);
      if (big && yr) rows.push([yr, '500 9.5px ' + MONO, 12, col.muted]);
      /* A name goes above its point, else below. The people at the front, and the line being walked, matter most:
         theirs may also sit beside the point, lose its years, and at the very front be cut short as a last resort. */
      var near = s >= 45 || v.key, sets = [rows], spots = near ? [0, 1, 2, 3] : [0, 1], al = 'center', bw, bh, i, j;
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
    var end = S.stations[S.stations.length - 1].z, step = 2.1 * dt * (end > camT.tz ? 1 : -1);
    if (Math.abs(end - camT.tz) <= Math.abs(step)) { camT.tz = end; stopFly(); } else camT.tz += step;
    busy = true;
  }
  if (S.lens === 'long') follow();
  var e = reduce ? 1 : 1 - Math.exp(-dt / 0.14);
  for (k in camT) { var d = camT[k] - cam[k]; if (Math.abs(d) > (k === 'theta' ? 1e-4 : k === 's' || k === 'F' || k === 'yT' ? 0.02 : 1e-3)) { cam[k] += d * e; busy = true; } else cam[k] = camT[k]; }
  var mv = function (cur, tgt, rate) { if (cur === tgt) return cur; busy = true; var s2 = reduce ? 1 : dt * rate; return Math.abs(tgt - cur) <= s2 ? tgt : cur + Math.sign(tgt - cur) * s2; };
  S.fanMix = mv(S.fanMix, S.fanT, 1.5); S.tween = mv(S.tween, 1, 1.5); S.fade = mv(S.fade, 1, 3);
  var wt = windowAt(cam.tz), we = reduce ? 1 : 1 - Math.exp(-dt / 0.09), wc = S.win.L + S.win.w / 2, tc = wt.L + wt.w / 2, lw = Math.log(S.win.w), ltw = Math.log(wt.w);
  if (Math.abs(lw - ltw) > 1e-4 || Math.abs(wc - tc) > S.win.w * 1e-4) { lw += (ltw - lw) * we; wc += (tc - wc) * we; S.win = { L: wc - Math.exp(lw) / 2, w: Math.exp(lw) }; busy = true; } else S.win = wt;
  var wantA = S.lens === 'long' ? (S.back ? 1 : 0) : 1, wantD = S.lens === 'long' ? (S.back ? 0 : 1) : S.lens === 'fan' ? 0 : 1;
  S.aAnc = mv(S.aAnc, wantA, 3.5); S.aDesc = mv(S.aDesc, wantD, 3.5);
  draw(); hud();
  if (busy) raf = requestAnimationFrame(tick);
}
function follow() {                  // in the long view the person you are passing is the person in the panel
  var st = stationAt(camT.tz); if (st && st.id !== S.sel) { S.sel = st.id; panel(); }
}
function yearNow() { return TNOW - camT.tz / KZ; }
var shown = {};
function put(sel, prop, val) { if (shown[sel + prop] !== val) { shown[sel + prop] = val; var el = $(sel); if (prop === 'display') el.style.display = val; else if (prop.indexOf('aria-') === 0) el.setAttribute(prop, val); else el[prop] = val; } }
function standing() { return S.nodes[S.sel] && P[S.sel].st !== 'm' ? S.sel : S.focus; }      // the person the long view is passing
function hud() {
  var long = S.lens === 'long';
  put('#tx-year', 'display', long ? '' : 'none'); put('#tx-when', 'hidden', !long);
  if (!long) return;
  var y = yearNow(), n = S.nodes[S.sel], live = y > S.liveFrom + 9.5;
  put('#tx-yr', 'textContent', live ? 'Now' : String(Math.round(y)));
  var ev = null; D.events.forEach(function (e) { if (!live && Math.abs(e.year - y) < 4 && e.year <= S.liveFrom) ev = e; });
  var g = n ? Math.abs(n.gen) : 0;
  put('#tx-yrsub', 'textContent', ev ? ev.year + ' · ' + ev.label : !S.touched ? 'Drag or scroll to travel ' + (S.back ? 'back' : 'on') + ' · tap a person' : live ? 'The living: names only, no dates' : !g ? 'Standing with ' + shortName(P[S.focus]) : g + (g === 1 ? ' generation ' : ' generations ') + (S.back ? 'back' : 'on'));
  var a = S.back ? S.tMin : S.anc.root.t, b = S.back ? S.anc.root.t : S.tMax;
  if (!S.sliding) put('#tx-slide', 'value', String(Math.round(1000 * clamp((y - a) / Math.max(1, b - a), 0, 1))));
  var here = standing(), cnt = kinCount(here, !S.back), what = S.back ? 'descendant' : 'ancestor';
  var say = cnt ? 'Turn around: the ' + cnt + ' ' + what + (cnt === 1 ? '' : 's') + ' of ' + P[here].n : 'No ' + what + 's on record for ' + P[here].n;
  put(TURN, 'textContent', cnt ? cnt + ' ' + what + (cnt === 1 ? '' : 's') : 'No ' + what + 's');
  put('#tx-desc', 'disabled', !cnt); put('#tx-desc', 'title', say); put('#tx-desc', 'aria-label', say);
  put('#tx-desc', 'hidden', false);                        // a page kept from the day the button came and went with the person
}

/* ---------- panel ---------- */
function relation(n) {
  if (!n || n.kind === 'focus') return 'In focus';
  var g = Math.abs(n.gen), sex = n.p.g, base = n.kind === 'anc' ? (sex === 'M' ? 'father' : sex === 'F' ? 'mother' : 'parent') : (sex === 'M' ? 'son' : sex === 'F' ? 'daughter' : 'child');
  var w = g === 1 ? base : g === 2 ? 'grand' + base : g === 3 ? 'great-grand' + base : (g - 2) + '× great-grand' + base;
  var own = whose(S.focus);
  return own.charAt(0).toUpperCase() + own.slice(1) + ' ' + w;
}
function thread(slug) { var l = lineOf[slug]; return 'var(--t-' + slug + ', var(--t-' + (l ? l.color : 'neutral') + ', var(--t-neutral)))'; }
function chip(id) { var p = P[id]; if (p.st === 'm') return ''; return '<button type="button" data-go="' + id + '" style="--thread: ' + thread(lineColour(id)) + '">' + esc(kinName(p)) + '</button>'; }
function chipList(ids) {
  return ids.map(function (i) {
    return P[i].st !== 'm' ? chip(i) : '<a class="tx-minor" href="' + D.priv + '/person/' + i + '" rel="noopener" title="Not named on this site. Opens in the research database (family login)">Living child</a>';
  }).join('');
}
function kinCard() {
  var k = S.kin, t = kinSay(k, k.a, k.b), h = '<div class="tx-rel"><button type="button" class="tx-rel-x" data-kin-done aria-label="Done">×</button><p class="eyebrow">How they are related</p><p class="tx-rel-say">' + esc(t.say) + '</p>';
  if (t.meet) h += '<p class="tx-note">' + esc(t.meet) + '</p>';
  if (!S.me && k.a === D.root) h += '<p class="tx-note">Not ' + esc(firstName(D.root)) + '? Find your own name and choose “This is me”: relationships are then named from you.</p>';
  if (k.tops) {                      // the way from one to the other: up to where they meet and down again, "=" for a couple
    var btn = function (id, top) { return '<button type="button" data-walk="' + id + '"' + (top ? ' class="tx-top"' : '') + '>' + esc(id === S.me ? 'You' : firstName(id)) + '</button>'; };
    var leg = function (g) { return g.wa.slice().reverse().map(function (id) { return btn(id, g.tops.indexOf(id) >= 0); }).concat(g.wb.slice(1).map(function (id) { return '\u0000' + btn(id); })).join('<i title="child of">↑</i>').replace(/<i title="child of">↑<\/i>\u0000/g, '<i title="parent of">↓</i>'); };
    var eq = '<i title="partner of">=</i>', way = (k.x !== k.a ? btn(k.a) + eq : '') + leg(k);
    if (k.kind === 'wedM') way += eq + leg(k.k2); else if (k.y !== k.b) way += eq + btn(k.b);
    h += '<p class="tx-rel-way">' + way + '</p>';
  }
  return h + '<p class="tx-rel-acts"><button type="button" class="tx-link" data-kin-other>Compare with someone else</button></p></div>';
}
function panel() {
  var id = S.sel && P[S.sel].st !== 'm' ? S.sel : S.focus, p = P[id], n = S.nodes[id], c = n ? n.col : lineColour(id), l = lineOf[c], h = '';      // never a child under 18
  var url = BASE + '/person/' + id + '/', ev = n ? n.ev : evidence(p), more = app.classList.contains('tx-more'), ref = refPerson();
  if (S.kin) h += kinCard();
  h += '<div class="tx-who" style="--thread: ' + thread(c) + '"><span class="tx-band"></span><div>';
  h += '<p class="eyebrow">' + esc(id === S.me ? (id === S.focus ? 'You · in focus' : 'You') : relation(n)) + (l ? ' · ' + esc(l.short) + ' line' : '') + '</p>';
  h += '<h2><a href="' + url + '"' + EXT + '>' + esc(p.n) + '</a></h2>';
  if (p.st === 'f') {
    var pl = [place(p.bp), place(p.dp)].filter(function (s, i, a) { return s && a.indexOf(s) === i; });
    h += '<p class="tx-span">' + esc(years(p)) + (pl.length ? ' <span class="tx-place">' + esc(pl.join(' → ')) + '</span>' : '') + '</p>';
  } else h += '<p class="tx-span tx-note">Name and family links only</p>';
  if (id !== ref && S.focus !== ref && !S.kin) {             // the centre is someone else: say what this person is to you, or to the person the site starts from
    var kr = kinOf(ref, id);
    if (kr.kind !== 'none') h += '<p class="tx-toyou">' + (ref === S.me ? 'To you: ' : 'To ' + esc(firstName(ref)) + ': ') + '<button type="button" class="tx-link" data-kin-show>' + esc(kinSay(kr, ref, id).short) + '</button></p>';
  }
  h += '</div></div><div class="tx-acts">';
  if (id !== S.focus) h += '<button type="button" class="tx-btn" data-stand>Stand here</button>';
  h += '<a class="tx-btn tx-alt" href="' + url + '"' + EXT + '>Open their page</a>';
  h += '<button type="button" class="tx-btn tx-alt tx-more-btn" data-more aria-expanded="' + more + '">' + (more ? 'Less' : 'More') + '</button></div><div class="tx-rest">';
  if (p.st !== 'f') h += '<p class="tx-note">Shown this way for the living, and for anyone with no dates on record yet.</p>';
  if (p.st === 'f') {
    if (p.x) h += '<p class="tx-note">' + (p.x.t === 'Departure' || p.x.t === 'Emigration' ? 'Left ' : 'Arrived ') + (p.x.p ? esc(place(p.x.p)) + ', ' : '') + p.x.y + '.</p>';
    var e = p.ev, lab = { human: 'reviewed', ai: 'AI-checked', un: 'unreviewed', needs: 'needs a look', none: 'no facts yet' }[ev];
    h += '<p class="tx-note"><span class="stamp ' + (ev === 'none' ? 'un' : ev) + '">' + lab + '</span>' + (e[0] ? e[0] + (e[0] === 1 ? ' fact: ' : ' facts: ') + [e[1] ? e[1] + ' reviewed' : '', e[2] ? e[2] + ' AI-checked' : '', e[4] ? e[4] + ' flagged' : '', (e[3] - e[4]) > 0 ? (e[3] - e[4]) + ' unreviewed' : ''].filter(Boolean).join(', ') : '') + '</p>';
    var stop = n && n.kind !== 'desc' && !n.par[0] && !n.par[1] && !n.dup[0] && !n.dup[1];
    if (stop) h += '<p class="tx-note tx-q">The trail stops here: no parents on record.' + (p.rq && p.rq.length ? '' : ' No research card is open on it yet.') + '</p>';
    (p.rq || []).forEach(function (q, i) { h += '<p class="tx-note' + (stop || i ? '' : ' tx-q') + '">Open question: <a href="' + BASE + '/research/#' + q[0] + '"' + EXT + '>' + esc(q[1]) + '</a></p>'; });
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
  var first = esc(firstName(S.focus));
  h += '<p class="tx-about">' + (S.lens === 'fan' ? 'Each ring out is a generation back, father’s side to the left and mother’s to the right.'
    : S.lens === 'tree' ? 'Older is up and younger is down, father to the left and mother to the right.'
    : S.back ? 'The tree grows upward from ' + first + ': father to the left, mother to the right, and the further back someone was born the further up they stand.'
    : 'The tree is turned round on ' + first + ': everyone who came after rises above, the youngest furthest up.') + ' Living relatives are placed by generation, never against a year.</p>';
  h += '<p class="tx-about">Tap someone to walk to them. ' + (S.lens === 'long' ? 'Double-tap anyone to turn the tree round on them, from their ancestors to their descendants and back again. The button below the picture does the same for whoever you are with.'
    : 'Double-tap, or Stand here, puts them at the centre.') + '</p>';
  h += '<p class="tx-about">' + (id === S.me ? 'This is you: the tree opens on you and names everyone’s relationship to you. It is remembered on this device only. <button type="button" class="tx-link" data-notme>This is not me</button>'
    : (S.me ? '' : 'Are you in the tree? Find your own name, then: ') + '<button type="button" class="tx-link" data-me>This is me</button>') + '</p></div>';
  $('#tx-panel').innerHTML = h;
}

/* ---------- controls ---------- */
function chips() {
  var on = S.mode === 'evidence', h = '<button type="button" class="tx-tog" data-evid aria-pressed="' + on + '">Evidence</button>';
  h += '<button type="button" class="tx-tog" data-kin aria-pressed="' + !!S.kin + '">How related?</button>';
  if (S.me && S.focus !== S.me) h += '<button type="button" class="tx-tog" data-home>Start from me</button>';
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
  $('#tx-fly').hidden = !long; $('#tx-fly').textContent = S.flying ? 'Pause' : (S.back ? 'Fly back' : 'Fly on'); $('#tx-fly').setAttribute('aria-pressed', String(S.flying));
  $('#tx-fit').textContent = long ? 'Reset' : 'Fit';
  var noA = !S.anc.root.par[0] && !S.anc.root.par[1], noD = !S.desc.root.kids.length, who = P[S.focus].n;
  var none = long && noA && noD ? 'No parents or children on record for ' + who + '.' : long && !S.back && noD ? 'No children on record for ' + who + '.' : long && S.back && noA ? 'No parents on record for ' + who + '. Turn around to see who came after.' : S.lens === 'fan' && noA ? 'No parents on record for ' + who + '. Try the Long view or the Tree view.' : '';
  $('#tx-empty').hidden = !none; $('#tx-empty').textContent = none;
  chips();
  var hash = S.kin && S.lens !== 'fan' ? '#kin-' + S.kin.a + '-' + S.kin.b + (S.lens === 'tree' ? '-tree' : '')
    : '#' + (long && !S.back ? 'ahead' : S.lens) + '-' + S.focus + (S.line ? '-' + S.line : '');      // "ahead": the long view turned round
  try { if (S.pushNext && !S.routing && location.hash && location.hash !== hash) history.pushState(null, '', hash); else history.replaceState(null, '', hash); } catch (e) {}
  S.pushNext = false;
}
function stopFly() { if (S.flying) { S.flying = false; sync(); } }
function setLens(l) { stopFly(); S.lens = l; S.panX = 0; S.whole = false; if (!S.nodes[S.sel]) S.sel = S.focus; repath(S.sel); aim(false); sync(); panel(); }
function focusOn(id, back) {         // stand with someone: they become the centre. You keep looking the way you were, if anyone is that way
  if (!P[id] || P[id].st === 'm') return;
  var want = back == null ? S.back : back;
  stopFly(); S.pushNext = id !== S.focus; build(id); S.sel = id; S.panX = 0; S.whole = false;
  var a = S.anc.root.reach, d = S.desc.root.reach;
  S.back = want ? a > 0 || d === 0 : d === 0;
  setPath(id); aim(false); sync(); panel();
}
/* How are we related? The tree is turned round on the ancestor the two share, and the way up to each of them is lit:
   one is the route, the other marked the same. Where one is the other's ancestor, the elder is the centre. */
function showKin(a, b) {
  if (!P[a] || !P[b] || P[a].st === 'm' || P[b].st === 'm') return;
  var k = kinOf(a, b); k.a = a; k.b = b;
  stopFly(); S.pick = null;
  if (k.tops) {
    var top = k.tops[0], main = k.wa.length > 1 ? k.wa : k.wb;
    if (S.lens === 'fan') S.lens = 'long';
    S.pushNext = true; build(top); S.sel = top; S.panX = 0; S.whole = false; S.back = false;
    S.trail = {}; k.wa.concat(k.wb).forEach(function (id) { S.trail[id] = 1; });
    S.from = main[main.length - 1]; k.main = main;
    S.kin = k; setPath(null, main); aim(false);
  } else { S.pushNext = !S.kin; S.kin = k; }
  sync(); panel(); go();
}
function repath(via) {               // the route again: while a relationship is shown it stays the line between the two
  if (S.kin && S.kin.main && S.lens !== 'fan') setPath(null, S.kin.main); else setPath(via);
}
function askKin(other) {             // from the button under the picture: the person you are with, and you (or whoever the site starts from)
  var b = S.kin ? S.kin.b : standing(), a = refPerson();
  if (S.kin && !other) { S.kin = null; sync(); panel(); go(); return; }
  if (other || a === b) { S.pick = b; var q = $('#tx-q'); q.value = ''; q.placeholder = 'Related to whom?'; q.focus(); return; }
  showKin(a, b);
}
function setMe(id) {                 // "This is me": kept on this device only
  S.me = id && P[id] && P[id].st !== 'm' ? id : null;
  try { if (S.me) localStorage.setItem('tx-me', S.me); else localStorage.removeItem('tx-me'); } catch (e) {}
  sync(); panel();
}
function openWall() {                // a tap on a brick wall: the panel opens at what is known about it, and the research card if there is one
  app.classList.add('tx-more'); panel();
  var q = $('#tx-panel .tx-q'); if (q && q.scrollIntoView) q.scrollIntoView({ block: 'center' });
}
/* Turning round. The tree always grows upward; turning it round on someone changes what grows: from that person's
   ancestors to everyone who came after them, and back again. Walk back to an ancestor and turn, and their whole family
   rises ahead of you, the way you came still marked; turn again on someone further along and you look back from there. */
function turn(id) {
  if (S.lens !== 'long') return;
  var here = id && S.nodes[id] && P[id].st !== 'm' ? id : standing(), was = S.focus, back = !S.back;
  if (!kinCount(here, back)) return;                       // no one on record that way
  stopFly();
  if (here !== was) {                                      // a new centre: keep the way you came to it
    S.pushNext = true; build(here);
    var n = S.nodes[was];
    if (n) { S.from = was; S.trail = {}; for (var c = n; c; c = c.toward) S.trail[c.id] = 1; }
  } else if (S.kin) { S.kin = null; S.trail = null; S.from = null; }      // turning away from a relationship puts it away
  S.sel = here; S.back = back; S.panX = 0; S.line = null;
  setPath(here); aim(false); sync(); panel();
}
function select(id) {                // walk to someone in the picture; the route now runs through them
  if (!S.nodes[id] || P[id].st === 'm') return;
  stopFly(); S.sel = id; S.line = null;
  var n = S.nodes[id];
  if (S.lens === 'long' && (S.back ? n.kind === 'desc' : n.kind === 'anc')) return;
  setPath(id); S.panX = 0;
  if (S.lens === 'long') camT.tz = n.z;
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
  if (S.lens === 'long') ['theta', 'F', 'yT', 's'].forEach(function (k) { cam[k] = camT[k] = longP[k]; });
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
  var best = null, bd = Infinity; S.hitWall = 0;
  S.hits.forEach(function (h) {
    if (h.box) { if (x >= h.box.x && x <= h.box.x + h.box.w && y >= h.box.y && y <= h.box.y + h.box.h && bd > 0.5) { best = h.id; bd = 0.5; S.hitWall = 0; } return; }
    var d = Math.hypot(h.x - x, h.y - y); if (d <= Math.max(h.r + 9, 17) && d < bd) { bd = d; best = h.id; S.hitWall = h.wall || 0; }
  });
  return best;
}
function zoomBy(f, px, py) {
  if (S.lens === 'long') { S.zoomLong = clamp((S.zoomLong || baseScale()) * f, 90, 320); aim(false); return; }
  px = px == null ? W / 2 : px; py = py == null ? H / 2 : py;
  var s0 = camT.s, s1 = clamp(s0 * f, 2.5, 260), wx = camT.tx + (px - W / 2) / s0, wzz = camT.tz + (H / 2 - py) / s0;
  camT.s = s1; camT.tx = wx - (px - W / 2) / s1; camT.tz = wzz - (H / 2 - py) / s1; S.moved = true; go();
}
function travel(dz) {                // move through time in the long view, staying on the route
  stopFly();
  var zs = S.stations.map(function (s) { return s.z; }), lo = Math.min.apply(null, zs), hi = Math.max.apply(null, zs);
  camT.tz = clamp(camT.tz + dz, lo - 0.3, hi + 0.3); go();
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
      S.panX = clamp(S.panX - dx / (W / 2 - 26), -1.6, 1.6); travel((S.back ? 1 : -1) * dy / (cam.s * h / zvT));
    } else { camT.tx -= dx / cam.s; camT.tz += dy / cam.s; cam.tx = camT.tx; cam.tz = camT.tz; S.moved = true; go(); }
  });
  function up(e) {
    var d = drag; delete ptr[e.pointerId]; if (Object.keys(ptr).length < 2) pinch = 0; cv.classList.remove('tx-drag');
    if (!d || d.moved || Object.keys(ptr).length) { drag = null; return; }
    drag = null;
    var now = e.timeStamp || Date.now(), again = tapId && now - tapT < 400 && Math.hypot(e.offsetX - tapAt.x, e.offsetY - tapAt.y) < 30;      // when the finger lifted, not when this ran: the first tap's own work must not eat into the time allowed
    if (again) {                     // a second tap in the same place means the person first tapped, who has started to move:
      var who = tapId; tapId = null; // the tree turns round on them. (In the flat views, which show both ways at once, they become the centre.)
      if (S.lens === 'long') turn(who); else if (who !== S.focus) focusOn(who);
      return;
    }
    var id = hit(e.offsetX, e.offsetY), wall = S.hitWall;
    tapId = wall ? null : id; tapT = now; tapAt = { x: e.offsetX, y: e.offsetY };      // a tap on a brick wall opens its question; it is never half of a double tap
    if (id) select(id);
    if (id && wall) openWall();
  }
  cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', function (e) { delete ptr[e.pointerId]; drag = null; pinch = 0; cv.classList.remove('tx-drag'); });
  cv.addEventListener('wheel', function (e) {
    e.preventDefault(); touched();
    var dy = e.deltaMode === 1 ? e.deltaY * 30 : e.deltaY, dx = e.deltaMode === 1 ? e.deltaX * 30 : e.deltaX;
    if (e.ctrlKey || e.metaKey) { zoomBy(Math.exp(-dy * 0.01), e.offsetX, e.offsetY); return; }
    if (S.lens === 'long') { S.panX = clamp(S.panX + dx / (W / 2 - 26), -1.6, 1.6); travel((S.back ? 1 : -1) * dy * 0.011); }
    else zoomBy(Math.exp(-dy * 0.0022), e.offsetX, e.offsetY);
  }, { passive: false });
  cv.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;        // the browser's own shortcuts: zoom, new tab
    var k = e.key, long = S.lens === 'long', used = true;
    if (k === 'ArrowUp') long ? travel(S.back ? 1.4 : -1.4) : (camT.tz += 60 / cam.s, go());
    else if (k === 'ArrowDown') long ? travel(S.back ? -1.4 : 1.4) : (camT.tz -= 60 / cam.s, go());
    else if (k === 'ArrowLeft') long ? (S.panX = clamp(S.panX - 0.25, -1.6, 1.6), go()) : (camT.tx -= 60 / cam.s, go());
    else if (k === 'ArrowRight') long ? (S.panX = clamp(S.panX + 0.25, -1.6, 1.6), go()) : (camT.tx += 60 / cam.s, go());
    else if (k === '+' || k === '=') zoomBy(1.3); else if (k === '-') zoomBy(1 / 1.3);
    else if (k === 'Enter' && S.sel && S.sel !== S.focus) focusOn(S.sel);
    else if (k === 't' || k === 'T') turn();
    else used = false;
    if (used) { e.preventDefault(); touched(); }
  });
  function touched() { if (!S.touched) { S.touched = true; go(); } }
  app.querySelectorAll('[data-lens]').forEach(function (b) { b.addEventListener('click', function () { setLens(b.getAttribute('data-lens')); touched(); }); });
  $('#tx-desc').addEventListener('click', function () { touched(); turn(); });
  $('#tx-fly').addEventListener('click', function () {
    touched(); if (S.flying) { stopFly(); return; }
    var end = S.stations[S.stations.length - 1]; if (Math.abs(end.z - camT.tz) < 0.2) { camT.tz = S.stations[0].z; cam.tz = camT.tz; }
    if (reduce) { camT.tz = end.z; go(); return; }
    S.flying = true; S.panX = 0; sync(); go();
  });
  $('#tx-zin').addEventListener('click', function () { zoomBy(1.35); }); $('#tx-zout').addEventListener('click', function () { zoomBy(1 / 1.35); });
  $('#tx-fit').addEventListener('click', function () { stopFly(); S.zoomLong = 0; S.panX = 0; if (S.lens === 'long') { S.sel = S.focus; S.line = null; repath(S.focus); panel(); chips(); } else S.whole = true; aim(false); });
  $('#tx-slide').addEventListener('input', function () {
    S.sliding = true; shown['#tx-slidevalue'] = this.value; stopFly(); touched();
    var a = S.back ? S.tMin : S.anc.root.t, b = S.back ? S.anc.root.t : S.tMax, y = a + (b - a) * this.value / 1000;
    var zs = S.stations.map(function (s) { return s.z; }); camT.tz = clamp(wz(y), Math.min.apply(null, zs), Math.max.apply(null, zs)); go();
  });
  $('#tx-slide').addEventListener('change', function () { S.sliding = false; });
  $('#tx-chips').addEventListener('click', function (e) {
    if (e.target.closest('[data-evid]')) { S.mode = S.mode === 'evidence' ? 'lines' : 'evidence'; sync(); go(); return; }
    if (e.target.closest('[data-kin]')) { touched(); askKin(); return; }
    if (e.target.closest('[data-home]')) { touched(); focusOn(S.me, true); return; }
    var b = e.target.closest('[data-line]'); if (b) { touched(); followLine(b.getAttribute('data-line')); }
  });
  $('#tx-panel').addEventListener('click', function (e) {
    var b = e.target.closest('[data-go]'); if (b) { focusOn(b.getAttribute('data-go')); return; }
    b = e.target.closest('[data-walk]'); if (b) { var w = b.getAttribute('data-walk'); if (S.nodes[w]) select(w); else focusOn(w); return; }
    if (e.target.closest('[data-kin-show]')) { showKin(refPerson(), standing()); return; }
    if (e.target.closest('[data-kin-other]')) { askKin(true); return; }
    if (e.target.closest('[data-kin-done]')) { S.kin = null; sync(); panel(); go(); return; }
    if (e.target.closest('[data-me]')) { setMe(standing()); return; }
    if (e.target.closest('[data-notme]')) { setMe(null); return; }
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
  res.addEventListener('click', function (e) {
    var b = e.target.closest('[data-id]'); if (!b) return; res.hidden = true; q.value = ''; touched();
    var id = b.getAttribute('data-id'), was = S.pick; unpick();
    if (was) showKin(id, was); else focusOn(id, true);            // "Related to whom?": what the person you were with is to the one you chose
  });
  function unpick() { S.pick = null; q.placeholder = 'Find a person'; }
  q.addEventListener('blur', function () { setTimeout(function () { if (S.pick && document.activeElement !== q) unpick(); }, 250); });
  document.addEventListener('pointerdown', function (e) { if (!e.target.closest('.tx-find')) res.hidden = true; });
  // theme and size
  new MutationObserver(function () { readTheme(); wCache = {}; go(); }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class', 'style'] });
  var mq = matchMedia('(prefers-color-scheme: dark)'), onScheme = function () { readTheme(); go(); };
  if (mq.addEventListener) mq.addEventListener('change', onScheme); else if (mq.addListener) mq.addListener(onScheme);
  new ResizeObserver(resize).observe(stage);
  new ResizeObserver(function () { S.footH = $('.tx-foot').offsetHeight; go(); }).observe($('.tx-foot'));
  window.addEventListener('hashchange', route);
}
function route() {                  // #long-I0141 opens a view on a person, #ahead-I0141 the long view turned round on them; #long-I0141-wood also follows that line
  var k = /^#kin-(I\d+)(?:-(I\d+))?(?:-(tree))?$/.exec(location.hash || '');
  if (k && !k[2]) { k[2] = k[1]; k[1] = refPerson(); }     // #kin-I0233, as person pages link: what that person is to you, or to the person the site starts from
  if (k && P[k[1]] && P[k[2]] && P[k[1]].st !== 'm' && P[k[2]].st !== 'm' && k[1] !== k[2]) {  // #kin-I0141-I0233: what the second is to the first
    if (S.kin && S.kin.a === k[1] && S.kin.b === k[2] && S.lens === (k[3] || 'long')) return true;
    S.routing = true; S.lens = k[3] || 'long'; if (!S.focus) { build(k[1]); S.sel = k[1]; setPath(k[1]); } showKin(k[1], k[2]); S.routing = false;
    return true;
  }
  if (k && P[k[2]] && P[k[2]].st !== 'm') { S.routing = true; S.lens = 'long'; focusOn(k[2], true); S.routing = false; return true; }      // the same person twice: just that person
  var m = /^#(long|ahead|tree|fan)-(I\d+)(?:-([a-z]+))?$/.exec(location.hash || '');
  if (!m || !P[m[2]] || P[m[2]].st === 'm') return false;
  var lens = m[1] === 'ahead' ? 'long' : m[1], back = m[1] !== 'ahead';
  if (m[2] === S.focus && lens === S.lens && (lens !== 'long' || back === S.back) && (m[3] || null) === S.line && !S.kin) return true;
  S.routing = true; S.lens = lens; focusOn(m[2], lens === 'long' ? back : null);
  if (m[3] && lineOf[m[3]]) followLine(m[3]);
  S.routing = false;
  return true;
}

function start(data) {
  D = data; P = D.P; F = D.F;
  D.lines.forEach(function (l) { lineOf[l.slug] = l; l.path.forEach(function (id) { if (!pathLine[id]) pathLine[id] = l.slug; }); });
  readTheme(); resize(); wire();
  try { S.me = localStorage.getItem('tx-me'); } catch (e) {}
  if (!P[S.me] || P[S.me].st === 'm') S.me = null;
  if (!route()) { var home = refPerson(); build(home); S.sel = home; setPath(home); sync(); panel(); }
  aim(true);
  if (document.fonts && document.fonts.load) Promise.all(['700 13px "Bricolage Grotesque"', '800 15px "Bricolage Grotesque"', '500 12px "Bricolage Grotesque"', '600 condensed 10px "Bricolage Grotesque"', '500 10px "IBM Plex Mono"']
    .map(function (f) { return document.fonts.load(f, 'Konečný Štěpán'); })).then(function () { wCache = {}; go(); }).catch(function () {});
}
if (app.hasAttribute('data-debug')) window.__tx = { S: S, cam: cam, camT: camT, select: select, focusOn: focusOn, turn: turn, kinOf: kinOf, kinSay: kinSay, showKin: showKin, setMe: setMe, people: function () { return P; } };
fetch(app.getAttribute('data-src')).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(start).catch(function (err) {
  $('#tx-panel').innerHTML = '<p class="tx-note">The tree did not load (' + esc(err.message || err) + '). Reload the page to try again.</p>';
  if (window.console) console.error(err);
});
})();
