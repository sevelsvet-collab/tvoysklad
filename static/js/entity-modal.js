// Всплывающее окно с карточкой сущности (контрагент, организация) поверх документа.
// Карточка открывается во встроенном режиме (?embed=1 — без верхнего меню).
(function () {
  function openEntityModal(url, onClose) {
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
    }
    el.querySelector('.entity-frame').src = url;
    const modal = new bootstrap.Modal(el);
    const handler = function () {
      el.removeEventListener('hidden.bs.modal', handler);
      if (onClose) onClose();
    };
    el.addEventListener('hidden.bs.modal', handler);
    modal.show();
  }
  window.openEntityModal = openEntityModal;

  function buildUrl(base, id) {
    // base вида "/counterparties/0/" — подставляем реальный id вместо 0
    return base.replace(/\/0\/(?:$|\?)/, '/' + id + '/') + '?embed=1';
  }

  // Обновить видимое имя в поле автодополнения (вдруг переименовали в карточке)
  function refreshAcLabel(ac) {
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

  document.addEventListener('click', function (e) {
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
