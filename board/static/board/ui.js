/* Small page behaviours shared by every screen: toasts, confirms, menus, live regions. */
(function () {
  'use strict';
  var toasts = document.getElementById('toasts');

  window.sbToast = function (text, bad) {
    var el = document.createElement('div');
    el.className = 'toast';
    var icon = document.createElement('span');
    icon.className = 'ms fill';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = bad ? 'error' : 'info';
    el.appendChild(icon);
    el.appendChild(document.createTextNode(text));
    toasts.appendChild(el);
    fade(el, bad ? 6000 : 2600);
  };
  function fade(el, after) {
    setTimeout(function () { el.classList.add('gone'); setTimeout(function () { el.remove(); }, 450); }, after);
  }
  Array.prototype.forEach.call(document.querySelectorAll('[data-toast]'), function (el) { fade(el, 4200); });

  document.addEventListener('submit', function (event) {
    var ask = event.target.getAttribute('data-confirm');
    if (ask && !window.confirm(ask)) event.preventDefault();
  });
  document.addEventListener('change', function (event) {
    if (event.target.hasAttribute('data-autosubmit')) event.target.form.submit();
  });
  // Pop-over menus close when you click elsewhere or press Escape.
  document.addEventListener('click', function (event) {
    Array.prototype.forEach.call(document.querySelectorAll('details.menu[open], details.me[open]'), function (menu) {
      if (!menu.contains(event.target)) menu.removeAttribute('open');
    });
  });
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    Array.prototype.forEach.call(document.querySelectorAll('details.menu[open], details.me[open]'), function (menu) { menu.removeAttribute('open'); });
  });
  // Whole run rows are clickable.
  document.addEventListener('click', function (event) {
    var row = event.target.closest && event.target.closest('tr.row');
    if (row && !event.target.closest('a')) { var link = row.querySelector('a'); if (link) window.location = link.href; }
  });

  // While a run is waiting or running, refetch the page and swap the live regions.
  function refresh() {
    if (document.hidden || !document.querySelector('[data-live-poll]')) return;
    if (document.querySelector('details.peek[open]')) return;
    fetch(window.location.href, { headers: { 'X-Requested-With': 'fetch' } }).then(function (r) { return r.text(); }).then(function (html) {
      var fresh = new DOMParser().parseFromString(html, 'text/html').querySelectorAll('[data-live]');
      var mine = document.querySelectorAll('[data-live]');
      if (fresh.length !== mine.length) return;
      for (var i = 0; i < mine.length; i++) if (mine[i].innerHTML !== fresh[i].innerHTML) mine[i].replaceWith(fresh[i]);
    }).catch(function () {});
  }
  if (document.querySelector('[data-live]')) setInterval(refresh, 3000);
})();
