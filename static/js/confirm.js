// Подтверждение действий окном программы вместо системного confirm() браузера.
//
// Разметка: <button data-confirm="Удалить документ?" data-confirm-ok="Удалить">…</button>
// data-confirm-ok — надпись на кнопке согласия (по умолчанию «Удалить»),
// data-confirm-safe — не красить кнопку в красный (для безопасных действий).
// После «Да» исходная кнопка нажимается снова — со своим formaction, form=… и т.п.
(function () {
  let pending = null;

  function modalEl() {
    let el = document.getElementById('confirmModal');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'confirmModal';
    el.className = 'modal fade';
    el.tabIndex = -1;
    el.setAttribute('aria-hidden', 'true');
    el.innerHTML =
      '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">' +
      '<div class="modal-header py-2"><h6 class="modal-title mb-0">Подтверждение</h6>' +
      '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Закрыть"></button></div>' +
      '<div class="modal-body confirm-text"></div>' +
      '<div class="modal-footer py-2">' +
      '<button type="button" class="btn btn-outline-secondary btn-sm" data-bs-dismiss="modal">Отмена</button>' +
      '<button type="button" class="btn btn-danger btn-sm confirm-ok">Удалить</button>' +
      '</div></div></div>';
    document.body.appendChild(el);
    el.querySelector('.confirm-ok').addEventListener('click', () => {
      const target = pending;
      pending = null;
      bootstrap.Modal.getOrCreateInstance(el).hide();
      if (target) {
        target.dataset.confirmed = '1';
        target.click();
      }
    });
    el.addEventListener('shown.bs.modal', () => el.querySelector('.confirm-ok').focus());
    return el;
  }

  // Ловим клик раньше всех (фаза перехвата), чтобы действие не ушло без согласия
  document.addEventListener('click', (e) => {
    const target = e.target.closest('[data-confirm]');
    if (!target) return;
    if (target.dataset.confirmed === '1') {   // повторный клик после «Да» — пропускаем
      delete target.dataset.confirmed;
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    pending = target;
    const el = modalEl();
    el.querySelector('.confirm-text').textContent = target.dataset.confirm;
    const ok = el.querySelector('.confirm-ok');
    ok.textContent = target.dataset.confirmOk || 'Удалить';
    ok.className = 'btn btn-sm confirm-ok ' + (target.hasAttribute('data-confirm-safe') ? 'btn-primary' : 'btn-danger');
    bootstrap.Modal.getOrCreateInstance(el).show();
  }, true);
})();
