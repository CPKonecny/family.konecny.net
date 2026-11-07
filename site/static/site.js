(function () {
  var root = document.documentElement;
  var buttons = document.querySelectorAll('[data-theme-set]');
  function apply(mode, save) {
    if (mode === 'system') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', mode);
    buttons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-theme-set') === mode)); });
    if (save) { try { if (mode === 'system') localStorage.removeItem('fk-theme'); else localStorage.setItem('fk-theme', mode); } catch (e) {} }
  }
  buttons.forEach(function (b) { b.addEventListener('click', function () { apply(b.getAttribute('data-theme-set'), true); }); });
  var current = root.getAttribute('data-theme');
  apply(current === 'light' || current === 'dark' ? current : 'system', false);

  // people index: filter as you type, diacritic-insensitive; ?q= from the home page's search box pre-fills it
  var filter = document.getElementById('people-filter');
  if (filter) {
    var items = Array.prototype.slice.call(document.querySelectorAll('[data-filter-list] li'));
    var sections = Array.prototype.slice.call(document.querySelectorAll('section.letter'));
    var letters = Array.prototype.slice.call(document.querySelectorAll('.letters a'));
    var empty = document.getElementById('filter-empty');
    var count = document.getElementById('filter-count');
    var norm = function (s) { return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase(); };
    var run = function () {
      var q = norm(filter.value.trim());
      var shown = 0;
      items.forEach(function (li) {
        var hit = !q || norm(li.getAttribute('data-k') || '').indexOf(q) !== -1;
        li.hidden = !hit; if (hit) shown++;
      });
      sections.forEach(function (s) { s.hidden = !s.querySelector('li:not([hidden])'); });
      letters.forEach(function (a) { var s = document.getElementById(a.getAttribute('href').slice(1)); a.classList.toggle('off', !!(s && s.hidden)); });
      if (empty) empty.hidden = shown > 0;
      if (count) count.textContent = q ? shown + (shown === 1 ? ' match' : ' matches') : '';
    };
    filter.addEventListener('input', run);
    try {
      var q0 = new URLSearchParams(location.search).get('q');
      if (q0) { filter.value = q0; run(); filter.focus(); }
    } catch (e) {}
  }

  // in-page jump links: mark the section in view
  var jumps = Array.prototype.slice.call(document.querySelectorAll('.jumps a[href^="#"]'));
  if (jumps.length && 'IntersectionObserver' in window) {
    var byId = {};
    jumps.forEach(function (a) { var el = document.getElementById(a.getAttribute('href').slice(1)); if (el) byId[el.id] = a; });
    var seen = {};
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { seen[e.target.id] = e.isIntersecting; });
      var first = Object.keys(byId).filter(function (id) { return seen[id]; })[0];
      jumps.forEach(function (a) { a.removeAttribute('aria-current'); });
      if (first) byId[first].setAttribute('aria-current', 'true');
    }, { rootMargin: '-10% 0px -70% 0px' });
    Object.keys(byId).forEach(function (id) { io.observe(document.getElementById(id)); });
  }

  document.querySelectorAll('a.mail[data-u]').forEach(function (a) {
    var addr = a.getAttribute('data-u') + '@' + a.getAttribute('data-d');
    a.href = 'mailto:' + addr + '?subject=' + encodeURIComponent(a.getAttribute('data-s') || '');
  });
})();
