// Табличная часть документа (стиль МойСклад): нумерация, автострока,
// показ остатка/доступного, скидка в % или ₽, пересчёт сумм.
(function () {
  function money(n) {
    return (Math.round(n * 100) / 100).toLocaleString('ru-RU', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  }
  function qtyFmt(n) {
    return n.toLocaleString('ru-RU', { maximumFractionDigits: 3 });
  }
  function num(el) {
    if (!el) return 0;
    return parseFloat(String(el.value).replace(/\s/g, '').replace(',', '.')) || 0;
  }
  function trimNum(n) {
    return String(parseFloat(n.toFixed(3)));
  }

  function rowHasProduct(tr) {
    var p = tr.querySelector('.line-product');
    return !!(p && p.value);
  }
  function isActiveRow(tr) {
    var del = tr.querySelector('input[type=checkbox][name$="-DELETE"]');
    return !tr.classList.contains('d-none') && !(del && del.checked);
  }
  function discountPercent(tr) {
    var d = tr.querySelector('.line-discount-value');
    return d ? (parseFloat(d.value) || 0) : 0;
  }
  function rowBase(tr) {
    return num(tr.querySelector('.line-qty')) * num(tr.querySelector('.line-price'));
  }

  function rowValues(tr) {
    var qty = num(tr.querySelector('.line-qty'));
    var priceEl = tr.querySelector('.line-price');
    var sumCell = tr.querySelector('.line-sum');
    var sum = 0;
    if (priceEl) {
      var pct = discountPercent(tr);
      sum = qty * num(priceEl) * (1 - pct / 100);
      if (sum < 0) sum = 0;
      if (sumCell) sumCell.textContent = money(sum);
    }
    return { qty: qty, sum: sum };
  }

  function recalc(root) {
    var totalSum = 0, totalQty = 0, count = 0;
    root.querySelectorAll('.line-row').forEach(function (tr) {
      var numEl = tr.querySelector('.line-num');
      if (!isActiveRow(tr) || !rowHasProduct(tr)) {
        if (numEl) numEl.textContent = '';
        return;
      }
      count += 1;
      if (numEl) numEl.textContent = count;
      var v = rowValues(tr);
      totalSum += v.sum; totalQty += v.qty;
    });
    var ts = root.querySelector('.total-sum'); if (ts) ts.textContent = money(totalSum);
    var tq = root.querySelector('.total-qty'); if (tq) tq.textContent = qtyFmt(totalQty);
    var tc = root.querySelector('.total-count'); if (tc) tc.textContent = count;
  }

  // ---------- Остаток / Доступно ----------

  function warehouseSelect(root) {
    var name = root.dataset.warehouseField || 'warehouse';
    return document.querySelector('[name=' + name + ']');
  }

  function fillStockCell(el, val) {
    if (!el) return;
    if (val === null || val === undefined) { el.textContent = '—'; el.className = el.className.replace(/\btext-(danger|muted)\b/g, ''); return; }
    el.textContent = qtyFmt(val);
    el.classList.toggle('text-danger', val <= 0);
    el.classList.toggle('text-muted', val > 0);
  }

  function renderRowStock(tr, stock, available, unit) {
    var unitEl = tr.querySelector('.line-unit');
    if (unitEl && unit) unitEl.textContent = unit;
    fillStockCell(tr.querySelector('.line-available'), available === undefined ? stock : available);
    fillStockCell(tr.querySelector('.line-stock'), stock);
  }

  function refreshStocks(root) {
    var url = root.dataset.stockUrl;
    if (!url) return;
    var wh = warehouseSelect(root);
    var whId = wh && wh.value;
    var rowsById = {};
    root.querySelectorAll('.line-row').forEach(function (tr) {
      var p = tr.querySelector('.line-product');
      if (isActiveRow(tr) && p && p.value) rowsById[p.value] = tr;
    });
    var ids = Object.keys(rowsById);
    if (!ids.length) return;
    fetch(url + '?ids=' + ids.join(',') + (whId ? '&warehouse=' + whId : ''))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        Object.keys(rowsById).forEach(function (pid) {
          var info = data.stock[pid];
          if (info) renderRowStock(rowsById[pid], info.stock, info.available, info.unit);
        });
      })
      .catch(function () {});
  }

  // ---------- Скидка (% ⇄ ₽) ----------

  function bindDiscount(tr, root) {
    var input = tr.querySelector('.line-discount-input');
    var hidden = tr.querySelector('.line-discount-value');
    var modeBtn = tr.querySelector('.line-discount-mode');
    if (!input || !hidden || !modeBtn) return null;

    function syncFromInput() {
      var v = parseFloat(input.value) || 0;
      if (v < 0) v = 0;
      if (modeBtn.dataset.mode === 'percent') {
        if (v > 100) v = 100;
        hidden.value = trimNum(v);
      } else {
        var base = rowBase(tr);
        var pct = base > 0 ? (v / base * 100) : 0;
        if (pct > 100) pct = 100;
        hidden.value = trimNum(pct);
      }
      recalc(root);
    }

    input.addEventListener('input', syncFromInput);
    modeBtn.addEventListener('click', function () {
      var base = rowBase(tr);
      var pct = parseFloat(hidden.value) || 0;
      if (modeBtn.dataset.mode === 'percent') {
        modeBtn.dataset.mode = 'money';
        modeBtn.textContent = '₽';
        input.value = (base * pct / 100).toFixed(2);
      } else {
        modeBtn.dataset.mode = 'percent';
        modeBtn.textContent = '%';
        input.value = trimNum(pct);
      }
    });

    // При изменении количества/цены: пересчитать процент (если скидка в ₽) и сумму
    return function onBaseChange() {
      if (modeBtn.dataset.mode === 'money') syncFromInput();
      else recalc(root);
    };
  }

  // ---------- Строка ----------

  function bindRow(tr, root) {
    var price = tr.querySelector('.line-price');
    var qty = tr.querySelector('.line-qty');
    var onBaseChange = bindDiscount(tr, root);

    var ac = tr.querySelector('.ac');
    if (ac) {
      ac.addEventListener('ac:select', function (e) {
        var item = e.detail || {};
        if (price && item.price && !parseFloat(price.value)) price.value = item.price;
        if (qty && !parseFloat(qty.value)) qty.value = 1;
        renderRowStock(tr, item.stock, item.available, item.unit);
        recalc(root);
        ensureTrailingRow(root, true);
      });
    }
    [qty, price].forEach(function (el) {
      if (el) el.addEventListener('input', function () {
        if (onBaseChange) onBaseChange(); else recalc(root);
      });
    });
    var del = tr.querySelector('.line-del');
    if (del) del.addEventListener('click', function () {
      var chk = tr.querySelector('input[type=checkbox][name$="-DELETE"]');
      if (chk) chk.checked = true;
      tr.classList.add('d-none');
      recalc(root);
    });
  }

  function lastActiveRow(root) {
    var rows = root.querySelectorAll('.line-row');
    for (var i = rows.length - 1; i >= 0; i--) {
      if (isActiveRow(rows[i])) return rows[i];
    }
    return null;
  }

  function ensureTrailingRow(root, focus) {
    var last = lastActiveRow(root);
    if (!last || rowHasProduct(last)) return addRow(root, focus);
    return null;
  }

  function addRow(root, focus) {
    var tbody = root.querySelector('.lines-body');
    var totalForms = root.querySelector('input[name$="-TOTAL_FORMS"]');
    var tmpl = root.querySelector('.empty-form');
    var idx = parseInt(totalForms.value, 10);
    var tr = document.createElement('tr');
    tr.className = 'line-row';
    tr.innerHTML = tmpl.innerHTML.replace(/__prefix__/g, idx);
    tbody.appendChild(tr);
    totalForms.value = idx + 1;
    bindRow(tr, root);
    if (window.initAutocomplete) window.initAutocomplete(tr);
    if (focus) {
      var search = tr.querySelector('.ac-input');
      if (search) search.focus();
    }
    recalc(root);
    return tr;
  }

  document.querySelectorAll('.doc-lines').forEach(function (root) {
    root.querySelectorAll('.line-row').forEach(function (tr) { bindRow(tr, root); });
    var addBtn = root.querySelector('.add-line');
    if (addBtn) addBtn.addEventListener('click', function () { addRow(root, true); });
    ensureTrailingRow(root, false);
    recalc(root);
    refreshStocks(root);

    var wh = warehouseSelect(root);
    if (wh) wh.addEventListener('change', function () { refreshStocks(root); });
  });
})();
