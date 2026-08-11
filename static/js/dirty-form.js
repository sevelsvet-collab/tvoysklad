// Защита от потери несохранённых изменений на формах редактирования.
// Если пользователь изменил форму и уходит (кнопка «Закрыть», пункт меню,
// закрытие вкладки) — предлагаем сохранить.
(function () {
  let dirty = false;
  let submitting = false;
  let dirtyForm = null;

  function isEditForm(form) {
    if (!form || (form.method || '').toLowerCase() !== 'post') return false;
    if (form.id === 'doc-action') return false;          // пустая форма действий
    if (form.dataset.noDirty !== undefined) return false; // явно отключено
    // отслеживаем только формы с реальными полями ввода
    return !!form.querySelector(
      'input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea'
    );
  }

  function markDirty(e) {
    const form = e.target && e.target.form;
    if (isEditForm(form)) { dirty = true; dirtyForm = form; }
  }

  document.addEventListener('input', markDirty);
  document.addEventListener('change', markDirty);
  // Любая отправка формы (сохранить/провести/удалить) снимает защиту
  document.addEventListener('submit', () => { submitting = true; });

  // Закрытие вкладки / обновление / внешний переход — нативное предупреждение
  window.addEventListener('beforeunload', (e) => {
    if (dirty && !submitting) { e.preventDefault(); e.returnValue = ''; }
  });

  // Клик по внутренней ссылке (Закрыть, меню) — своё окно с выбором
  document.addEventListener('click', (e) => {
    const a = e.target.closest('a[href]');
    if (!a || !dirty || submitting) return;
    const href = a.getAttribute('href');
    if (!href || href[0] === '#' || href.startsWith('javascript')) return;
    if (a.target === '_blank' || a.hasAttribute('data-bs-toggle')) return;
    if (a.hasAttribute('hx-get') || a.hasAttribute('hx-post')) return;  // htmx — не навигация
    e.preventDefault();
    e.stopPropagation();
    showUnsavedModal(href);
  }, true);

  function showUnsavedModal(href) {
    let el = document.getElementById('unsavedModal');
    if (!el) {
      el = document.createElement('div');
      el.id = 'unsavedModal';
      el.className = 'modal fade';
      el.tabIndex = -1;
      el.innerHTML =
        '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">' +
        '<div class="modal-header"><h5 class="modal-title">Несохранённые изменения</h5>' +
        '<button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>' +
        '<div class="modal-body">Вы внесли изменения, но не сохранили их. Что сделать?</div>' +
        '<div class="modal-footer">' +
        '<button type="button" class="btn btn-outline-danger me-auto" id="um-discard">Уйти без сохранения</button>' +
        '<button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Отмена</button>' +
        '<button type="button" class="btn btn-success" id="um-save"><i class="bi bi-check-lg me-1"></i>Сохранить</button>' +
        '</div></div></div>';
      document.body.appendChild(el);
    }
    const modal = new bootstrap.Modal(el);
    el.querySelector('#um-discard').onclick = () => { submitting = true; window.location = href; };
    el.querySelector('#um-save').onclick = () => {
      modal.hide();
      // requestSubmit проверит обязательные поля; при ошибке форма не уйдёт,
      // submit-обработчик снимет защиту только для реально отправленной формы
      if (dirtyForm) dirtyForm.requestSubmit();
    };
    modal.show();
  }
})();
