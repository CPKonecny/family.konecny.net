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

  var filter = document.getElementById('people-filter');
  if (filter) {
    var items = Array.prototype.slice.call(document.querySelectorAll('[data-filter-list] li'));
    var sections = Array.prototype.slice.call(document.querySelectorAll('section.letter'));
    var empty = document.getElementById('filter-empty');
    var norm = function (s) { return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase(); };
    filter.addEventListener('input', function () {
      var q = norm(filter.value.trim());
      var shown = 0;
      items.forEach(function (li) {
        var hit = !q || norm(li.getAttribute('data-k') || '').indexOf(q) !== -1;
        li.hidden = !hit; if (hit) shown++;
      });
      sections.forEach(function (s) { s.hidden = !s.querySelector('li:not([hidden])'); });
      if (empty) empty.hidden = shown > 0;
    });
  }
})();
