// Списки: переход в карточку по клику на строку, чекбоксы и счётчик отмеченных,
// раскрытие панели фильтров и быстрые периоды «вч · сег · нед · мес».
(function () {
  // ---------- Клик по строке — карточка ----------
  document.addEventListener('click', (e) => {
    const row = e.target.closest('tr[data-url]');
    if (!row) return;
    if (e.target.closest('a, button, input, label, select, textarea, .list-check-col')) return;
    if (String(window.getSelection ? window.getSelection() : '')) return;  // выделяли текст
    window.location = row.dataset.url;
  });

  // ---------- Отмеченные строки ----------
  function refreshSelection() {
    const checks = document.querySelectorAll('.list-check');
    const n = document.querySelectorAll('.list-check:checked').length;
    document.querySelectorAll('.list-bulk-count').forEach((el) => {
      el.textContent = n;
      el.classList.toggle('active', n > 0);
    });
    document.querySelectorAll('.list-bulk-menu').forEach((el) => { el.disabled = n === 0; });
    const all = document.querySelector('[data-list-select-all]');
    if (all) {
      all.checked = n > 0 && n === checks.length;
      all.indeterminate = n > 0 && n < checks.length;
    }
  }

  document.addEventListener('change', (e) => {
    if (e.target.matches('[data-list-select-all]')) {
      document.querySelectorAll('.list-check').forEach((c) => { c.checked = e.target.checked; });
      refreshSelection();
    } else if (e.target.matches('.list-check')) {
      refreshSelection();
    }
  });

  // ---------- Панель фильтров ----------
  function pad(n) { return String(n).padStart(2, '0'); }
  function iso(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }

  function periodRange(kind) {
    const today = new Date();
    const start = new Date(today);
    const end = new Date(today);
    if (kind === 'yesterday') { start.setDate(today.getDate() - 1); end.setDate(today.getDate() - 1); }
    if (kind === 'week') { start.setDate(today.getDate() - ((today.getDay() + 6) % 7)); }  // с понедельника
    if (kind === 'month') { start.setDate(1); }
    return [iso(start), iso(end)];
  }

  document.addEventListener('click', (e) => {
    const toggle = e.target.closest('[data-filter-toggle]');
    if (toggle) {
      const panel = document.getElementById('filter-panel');
      if (!panel) return;
      panel.classList.toggle('d-none');
      const open = !panel.classList.contains('d-none');
      toggle.setAttribute('aria-expanded', String(open));
      if (open) { const first = panel.querySelector('input, select'); if (first) first.focus(); }
      return;
    }
    const link = e.target.closest('[data-period]');
    if (link) {
      e.preventDefault();
      const form = link.closest('form');
      const name = link.dataset.periodFor;
      const [from, to] = periodRange(link.dataset.period);
      form.querySelector('[name="' + name + '_from"]').value = from;
      form.querySelector('[name="' + name + '_to"]').value = to;
    }
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refreshSelection);
  else refreshSelection();
})();
