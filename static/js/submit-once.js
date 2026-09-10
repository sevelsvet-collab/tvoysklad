// Защита от двойной отправки форм: повторный клик по «Сохранить», пока
// сервер отвечает, создавал второй документ.
//
// Слушаем на window — после всех остальных обработчиков: если форму
// остановила валидация (defaultPrevented), защиту не ставим. Кнопки
// блокируем на следующем тике — иначе потерялось бы значение нажатой
// кнопки (action=save / save_post).
(function () {
  const RESET_MS = 15000;  // на случай ответа без перехода на новую страницу

  function submitButtons(form) {
    const inside = Array.from(form.querySelectorAll('button[type=submit], input[type=submit]'));
    const outside = form.id
      ? Array.from(document.querySelectorAll('[type=submit][form="' + form.id + '"]'))
      : [];
    return inside.concat(outside);
  }

  function release(form) {
    delete form.dataset.submitting;
    submitButtons(form).forEach((b) => { b.disabled = false; });
  }

  window.addEventListener('submit', function (e) {
    const form = e.target;
    if (e.defaultPrevented) return;
    if ((form.method || '').toLowerCase() !== 'post') return;
    if (form.target === '_blank' || form.hasAttribute('data-allow-resubmit')) return;
    if (form.dataset.submitting) { e.preventDefault(); return; }
    form.dataset.submitting = '1';
    setTimeout(() => submitButtons(form).forEach((b) => { b.disabled = true; }), 0);
    setTimeout(() => release(form), RESET_MS);
  });

  // Возврат кнопкой «Назад» из кэша браузера — форма снова доступна
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-submitting]').forEach(release);
  });
})();
