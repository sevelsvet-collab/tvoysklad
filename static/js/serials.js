// Серийные номера в строках документа — для товаров с учётом по серийным номерам.
//
// Режим таблицы строк (data-serial-mode на .doc-lines):
//   enter         — приход: номера вводятся или сканируются (Enter добавляет);
//   pick          — расход: номера выбираются из тех, что лежат на складе (обязательно);
//   pick-optional — счёт: то же, но необязательно.
// Номера хранятся в скрытом поле строки (textarea.line-serials), по одному в строке.
// Строгая проверка — на сервере при проведении документа.
(function () {
  function parse(text) {
    const seen = new Set();
    const out = [];
    (text || '').split(/[\n\r,;]+/).forEach((part) => {
      const n = part.trim();
      if (n && !seen.has(n)) { seen.add(n); out.push(n); }
    });
    return out;
  }
  function rootOf(el) { return el.closest('.doc-lines'); }
  function modeOf(tr) { const root = rootOf(tr); return root ? root.dataset.serialMode || '' : ''; }
  function tracked(tr) { const ac = tr.querySelector('.ac'); return !!(ac && ac.dataset.trackSerials === '1'); }
  function need(tr) {
    const q = tr.querySelector('.line-qty');
    return Math.round(parseFloat(String(q ? q.value : '0').replace(',', '.')) || 0);
  }

  // Кнопка «S/N: 2 из 3»: зелёная — всё указано, красная — не хватает (серая — в счёте)
  function refreshRow(tr) {
    const wrap = tr.querySelector('.line-serial-wrap');
    const field = tr.querySelector('.line-serials');
    if (!wrap || !field) return;
    const mode = modeOf(tr);
    const show = !!mode && tracked(tr);
    wrap.classList.toggle('d-none', !show);
    if (!show) return;
    const count = parse(field.value).length;
    const required = need(tr);
    const ok = count === required;
    const btn = wrap.querySelector('.line-serial-btn');
    wrap.querySelector('.line-serial-label').textContent = 'S/N: ' + count + ' из ' + required;
    btn.classList.toggle('text-success', ok);
    btn.classList.toggle('text-danger', !ok && mode !== 'pick-optional');
    btn.classList.toggle('text-secondary', !ok && mode === 'pick-optional');
  }

  // ---------- Окно ввода / выбора номеров ----------

  let current = null;  // { tr, mode, selected: [], available: [] }

  function modalEl() {
    let el = document.getElementById('serialModal');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'serialModal';
    el.className = 'modal fade';
    el.tabIndex = -1;
    el.innerHTML =
      '<div class="modal-dialog modal-dialog-scrollable"><div class="modal-content">' +
      '<div class="modal-header py-2"><h6 class="modal-title mb-0"></h6>' +
      '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Закрыть"></button></div>' +
      '<div class="modal-body">' +
      '<div class="small text-muted mb-2 sn-hint"></div>' +
      '<input type="text" class="form-control form-control-sm sn-input" autocomplete="off">' +
      '<div class="sn-list mt-2"></div></div>' +
      '<div class="modal-footer py-2"><span class="me-auto small sn-count"></span>' +
      '<button type="button" class="btn btn-outline-secondary btn-sm" data-bs-dismiss="modal">Отмена</button>' +
      '<button type="button" class="btn btn-success btn-sm sn-done">Готово</button></div>' +
      '</div></div>';
    document.body.appendChild(el);
    el.querySelector('.sn-input').addEventListener('keydown', onInputKey);
    el.querySelector('.sn-input').addEventListener('input', () => { if (current && current.mode !== 'enter') render(); });
    el.querySelector('.sn-done').addEventListener('click', save);
    el.addEventListener('shown.bs.modal', () => el.querySelector('.sn-input').focus());
    return el;
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function render() {
    const el = modalEl();
    const list = el.querySelector('.sn-list');
    const required = need(current.tr);
    let html = '';
    if (current.mode === 'enter') {
      current.selected.forEach((n, i) => {
        html += '<div class="d-flex justify-content-between align-items-center border-bottom py-1">' +
          '<span class="font-monospace">' + escapeHtml(n) + '</span>' +
          '<button type="button" class="btn btn-link btn-sm text-danger p-0 sn-remove" data-i="' + i + '">' +
          '<i class="bi bi-x-lg"></i></button></div>';
      });
      if (!current.selected.length) html = '<div class="text-muted small">Номеров пока нет</div>';
    } else {
      const filter = el.querySelector('.sn-input').value.trim().toLowerCase();
      const all = Array.from(new Set(current.selected.concat(current.available))).sort();
      const shown = all.filter((n) => !filter || n.toLowerCase().includes(filter));
      shown.forEach((n) => {
        const id = 'sn-' + btoa(unescape(encodeURIComponent(n))).replace(/=/g, '');
        html += '<div class="form-check"><input class="form-check-input sn-check" type="checkbox" id="' + id + '"' +
          ' value="' + escapeHtml(n) + '"' + (current.selected.includes(n) ? ' checked' : '') + '>' +
          '<label class="form-check-label font-monospace" for="' + id + '">' + escapeHtml(n) + '</label></div>';
      });
      if (!all.length) html = '<div class="text-muted small">' + current.emptyText + '</div>';
      else if (!shown.length) html = '<div class="text-muted small">Нет номеров по запросу</div>';
    }
    list.innerHTML = html;
    list.querySelectorAll('.sn-remove').forEach((b) => b.addEventListener('click', () => {
      current.selected.splice(Number(b.dataset.i), 1);
      render();
    }));
    list.querySelectorAll('.sn-check').forEach((c) => c.addEventListener('change', () => {
      if (c.checked) { if (!current.selected.includes(c.value)) current.selected.push(c.value); }
      else current.selected = current.selected.filter((n) => n !== c.value);
      counter();
    }));
    counter();
    function counter() {
      const cnt = el.querySelector('.sn-count');
      cnt.textContent = 'Указано ' + current.selected.length + ' из ' + required;
      cnt.classList.toggle('text-danger', current.selected.length !== required && current.mode !== 'pick-optional');
      cnt.classList.toggle('text-success', current.selected.length === required);
    }
  }

  // Enter в поле: при вводе — добавить номер (можно вставить сразу список),
  // при выборе — отметить номер, совпавший со сканированным
  function onInputKey(e) {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const input = e.target;
    const typed = parse(input.value);
    if (!typed.length) return;
    if (current.mode === 'enter') {
      typed.forEach((n) => { if (!current.selected.includes(n)) current.selected.push(n); });
      input.value = '';
    } else {
      const known = new Set(current.selected.concat(current.available));
      const missing = typed.filter((n) => !known.has(n));
      typed.filter((n) => known.has(n)).forEach((n) => { if (!current.selected.includes(n)) current.selected.push(n); });
      modalEl().querySelector('.sn-hint').innerHTML = missing.length
        ? '<span class="text-danger">Нет на складе: ' + escapeHtml(missing.join(', ')) + '</span>'
        : current.hint;
      input.value = '';
    }
    render();
  }

  function open(tr) {
    const mode = modeOf(tr);
    const root = rootOf(tr);
    const el = modalEl();
    const productId = tr.querySelector('.line-product').value;
    const link = tr.querySelector('.line-product-link');
    const title = (link && link.textContent.trim()) || 'Товар';
    current = { tr, mode, selected: parse(tr.querySelector('.line-serials').value), available: [] };
    el.querySelector('.modal-title').textContent = 'Серийные номера — ' + title;
    const input = el.querySelector('.sn-input');
    input.value = '';
    if (mode === 'enter') {
      current.hint = 'Введите или отсканируйте номер и нажмите Enter. Можно вставить сразу список.';
      input.placeholder = 'Серийный номер';
      el.querySelector('.sn-hint').textContent = current.hint;
      render();
    } else {
      current.hint = (mode === 'pick-optional' ? 'Необязательно. ' : '') +
        'Отметьте номера, которые уходят, или отсканируйте номер и нажмите Enter.';
      input.placeholder = 'Поиск или сканирование номера';
      el.querySelector('.sn-hint').textContent = current.hint;
      const wh = document.querySelector('[name=' + (root.dataset.warehouseField || 'warehouse') + ']');
      if (!wh || !wh.value) {
        current.emptyText = 'Сначала выберите склад в шапке документа';
        render();
      } else {
        current.emptyText = 'На этом складе нет номеров этого товара';
        el.querySelector('.sn-list').innerHTML = '<div class="text-muted small">Загружаем номера…</div>';
        fetch(root.dataset.serialsUrl + '?product=' + encodeURIComponent(productId) + '&warehouse=' + encodeURIComponent(wh.value))
          .then((r) => r.json())
          .then((data) => { if (current && current.tr === tr) { current.available = data.serials || []; render(); } })
          .catch(() => { current.emptyText = 'Не удалось загрузить номера'; render(); });
      }
    }
    bootstrap.Modal.getOrCreateInstance(el).show();
  }

  function save() {
    if (!current) return;
    const field = current.tr.querySelector('.line-serials');
    field.value = current.selected.join('\n');
    field.dispatchEvent(new Event('change', { bubbles: true }));  // для защиты несохранённых изменений
    refreshRow(current.tr);
    bootstrap.Modal.getOrCreateInstance(modalEl()).hide();
  }

  // ---------- События ----------

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.line-serial-btn');
    if (btn) { e.preventDefault(); open(btn.closest('.line-row')); }
  });

  // Выбрали товар в строке: учитывается ли он по номерам; другой товар — прежние номера не годятся
  document.addEventListener('ac:select', (e) => {
    const tr = e.target.closest('.line-row');
    if (!tr || !tr.querySelector('.line-serials')) return;
    const item = e.detail || {};
    e.target.dataset.trackSerials = item.track_serials ? '1' : '';
    if (tr.dataset.snProduct && tr.dataset.snProduct !== String(item.id)) {
      tr.querySelector('.line-serials').value = '';
    }
    tr.dataset.snProduct = String(item.id);
    refreshRow(tr);
  });

  document.addEventListener('input', (e) => {
    if (e.target.classList && e.target.classList.contains('line-qty')) {
      const tr = e.target.closest('.line-row');
      if (tr) refreshRow(tr);
    }
  });

  function init() {
    document.querySelectorAll('.doc-lines .line-row').forEach((tr) => {
      const p = tr.querySelector('.line-product');
      if (p && p.value) tr.dataset.snProduct = p.value;
      refreshRow(tr);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
