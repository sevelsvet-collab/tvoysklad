// Всплывающее окно с карточкой сущности (контрагент, организация, товар) поверх документа.
// Карточка открывается во встроенном режиме (?embed=1 — без верхнего меню).
(function () {
  let current = null;  // { token, modal, onClose, onSaved, closing, closeWanted } открытого окна
  let pending = null;  // окно, запрошенное, пока прежнее ещё закрывается
  let openings = 0;

  // Метка открытия: окно возвращает её в сообщении «сохранено» (apps/core/embed_window.py)
  function newToken() {
    openings += 1;
    return Date.now().toString(36) + '-' + openings;
  }

  function withToken(url, token) {
    const parsed = new URL(url, window.location.origin);
    parsed.searchParams.set('modal', token);
    return parsed.pathname + parsed.search + parsed.hash;
  }

  // Bootstrap молча игнорирует hide() во время анимации открытия —
  // закрытие повторяется, когда окно откроется до конца (shown.bs.modal).
  function closeCurrent() {
    if (!current) return;
    current.closeWanted = true;
    current.modal.hide();
  }

  function modalElement() {
    let el = document.getElementById('entityModal');
    if (!el) {
      el = document.createElement('div');
      el.id = 'entityModal';
      el.className = 'modal fade';
      el.tabIndex = -1;
      el.innerHTML =
        '<div class="modal-dialog modal-dialog-centered" style="max-width:1120px">' +
        '<div class="modal-content" style="height:88vh">' +
        '<div class="modal-header py-2">' +
        '<h6 class="modal-title mb-0">Карточка</h6>' +
        '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Закрыть"></button></div>' +
        '<div class="modal-body p-0"><iframe class="entity-frame" style="width:100%;height:100%;border:0"></iframe></div>' +
        '</div></div>';
      document.body.appendChild(el);
      el.addEventListener('shown.bs.modal', function () {
        if (current && current.closeWanted) current.modal.hide();
      });
      el.addEventListener('hide.bs.modal', function () {
        if (current) current.closing = true;
      });
      el.addEventListener('hidden.bs.modal', function () {
        el.querySelector('.entity-frame').src = 'about:blank';  // не мелькать старой карточкой
        const done = current;
        current = null;
        if (done && done.onClose) done.onClose();
        if (pending) {
          const next = pending;
          pending = null;
          openEntityModal(next.url, next.options);
        }
      });
    }
    return el;
  }

  // openEntityModal(url, onClose) или openEntityModal(url, { title, onClose, onSaved })
  function openEntityModal(url, options) {
    if (current) {
      // Окно одно: новое откроется, когда прежнее закроется
      pending = { url: url, options: options };
      closeCurrent();
      return;
    }
    const opts = typeof options === 'function' ? { onClose: options } : (options || {});
    const el = modalElement();
    const token = newToken();
    el.querySelector('.modal-title').textContent = opts.title || 'Карточка';
    el.querySelector('.entity-frame').src = withToken(url, token);
    // Клик мимо окна не закрывает его: введённое в карточке не должно пропасть случайно
    const modal = bootstrap.Modal.getOrCreateInstance(el, { backdrop: 'static' });
    current = { token: token, modal: modal, onClose: opts.onClose, onSaved: opts.onSaved };
    modal.show();
  }
  window.openEntityModal = openEntityModal;

  // Карточка в окне сообщает о сохранении (templates/catalog/product_form.html)
  window.addEventListener('message', function (e) {
    if (e.origin !== window.location.origin) return;
    const data = e.data || {};
    if (data.type !== 'entity-saved' || !current || current.closing) return;
    // Только актуальное окно: само окно документа и метка этого открытия.
    // Пользователь уже закрывает окно (current.closing) — это отмена, товар не выбирается.
    const frame = document.querySelector('#entityModal .entity-frame');
    if (!frame || e.source !== frame.contentWindow || data.token !== current.token) return;
    const onSaved = current.onSaved;
    current.onSaved = null;
    if (onSaved) {
      onSaved(data);
      closeCurrent();
    }
  });

  // Товар уже создан: при сбое выбора просим найти его, а не создавать снова
  function selectCreated(ac, data) {
    if (!ac._ac) return;
    const failed = function () {
      ac._ac.showError('Товар «' + (data.name || '') + '» создан, но подставить его в строку не удалось. ' +
        'Найдите его поиском по названию — создавать заново не нужно.');
    };
    ac._ac.selectById(data.id).then(function (selected) {
      if (!selected) failed();
    }).catch(failed);
  }

  function buildUrl(base, id) {
    // base вида "/counterparties/0/" — подставляем реальный id вместо 0
    return base.replace(/\/0\/(?:$|\?)/, '/' + id + '/') + '?embed=1';
  }

  // Обновить видимое имя в поле автодополнения (вдруг переименовали в карточке)
  function refreshAcLabel(ac) {
    if (ac._ac) { ac._ac.refreshLabel(); return; }
    const valueEl = ac.querySelector('.ac-value');
    const inputEl = ac.querySelector('.ac-input');
    const id = valueEl && valueEl.value;
    if (!id || !ac.dataset.acUrl) return;
    const sep = ac.dataset.acUrl.includes('?') ? '&' : '?';
    fetch(ac.dataset.acUrl + sep + 'id=' + encodeURIComponent(id))
      .then((r) => r.json())
      .then((j) => { const it = (j.results || [])[0]; if (it) inputEl.value = it.name || it.label || ''; })
      .catch(() => {});
  }

  // «Создать новый товар» в строке документа — полная карточка во всплывающем окне
  document.addEventListener('ac:create', function (e) {
    const ac = e.target;
    const base = ac.dataset.acCreatePage;
    if (!base) return;
    const params = new URLSearchParams({ embed: '1', name: e.detail.name });
    const priceSource = ac._ac && ac._ac.staticParams.price;
    if (priceSource) params.set('price_source', priceSource);
    const row = ac.closest('tr');
    const price = row && row.querySelector('.line-price');
    // В окно предлагаем только цену пользователя, не подставленную от прежнего товара
    if (price && parseFloat(price.value) && !price.dataset.priceAuto) params.set('price', price.value);
    openEntityModal(base + '?' + params.toString(), {
      title: 'Новый товар',
      onSaved: function (data) { selectCreated(ac, data); },
    });
  });

  document.addEventListener('click', function (e) {
    // Товар в строке документа — ссылка на карточку
    const productLink = e.target.closest('.line-product-link');
    if (productLink) {
      e.preventDefault();
      const ac = productLink.closest('.ac');
      const id = ac.querySelector('.ac-value').value;
      if (!id) return;
      openEntityModal(buildUrl(ac.dataset.editBase, id), {
        title: 'Товар',
        onSaved: function () { refreshAcLabel(ac); },
        onClose: function () { refreshAcLabel(ac); },
      });
      return;
    }
    // Карандаш у контрагента (поле автодополнения)
    const acEdit = e.target.closest('.ac-edit');
    if (acEdit) {
      e.preventDefault();
      const ac = acEdit.closest('.ac');
      const id = ac.querySelector('.ac-value').value;
      if (!id) { return; }  // контрагент не выбран
      openEntityModal(buildUrl(acEdit.dataset.editBase, id), () => refreshAcLabel(ac));
      return;
    }
    // Карандаш у организации (обычный select рядом с кнопкой)
    const entEdit = e.target.closest('.entity-edit');
    if (entEdit) {
      e.preventDefault();
      const select = document.getElementById(entEdit.dataset.target);
      const id = select && select.value;
      if (!id) { return; }
      openEntityModal(buildUrl(entEdit.dataset.editBase, id));
    }
  });
})();
