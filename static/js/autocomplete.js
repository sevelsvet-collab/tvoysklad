// Живой поиск с подсказками (как в МойСклад): контрагенты, товары.
//
// Разметка:
//   <div class="ac" data-ac-url="/api/..." data-ac-create-url="/api/.../quick-create/"
//        data-ac-create-label="Создать новый товар" data-ac-params='{"price":"sale"}'
//        data-ac-dyn='{"warehouse":"[name=warehouse]"}'>
//     <input type="hidden" name="..." class="ac-value">
//     <input type="text" class="form-control ac-input">
//   </div>
//
// События на корневом .ac: 'ac:select' (detail = выбранный элемент), 'ac:clear'.
(function () {
  const DEBOUNCE_MS = 250;
  const instances = [];

  function csrfToken() {
    const field = document.querySelector('input[name=csrfmiddlewaretoken]');
    if (field) return field.value;
    const m = document.cookie.match(/csrftoken=([^;]+)/);
    return m ? m[1] : '';
  }

  function fmtStock(value) {
    if (value === null || value === undefined) return '';
    return Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 3 });
  }

  // Элемент меню. Данные с сервера и ввод пользователя — только как текст:
  // в названии товара или контрагента может быть что угодно.
  function menuNode(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  class Autocomplete {
    constructor(root) {
      this.root = root;
      this.valueInput = root.querySelector('.ac-value');
      this.textInput = root.querySelector('.ac-input');
      this.url = root.dataset.acUrl;
      this.createUrl = root.dataset.acCreateUrl || '';
      this.createLabel = root.dataset.acCreateLabel || 'Создать';
      this.innLabel = root.dataset.acInnLabel || '';  // подпись при вводе ИНН (контрагенты)
      this.staticParams = JSON.parse(root.dataset.acParams || '{}');
      this.dynParams = JSON.parse(root.dataset.acDyn || '{}');
      this.items = [];
      this.canCreate = false;
      this.activeIndex = -1;
      this.lastLabel = this.textInput.value;
      this.timer = null;
      this.searchRequest = 0;  // номер последнего запроса: опоздавшие ответы не учитываются
      this.searchPending = false;  // поиск ещё идёт: список не соответствует введённому
      this.enterWaiting = false;   // Enter нажат раньше ответа — выбор после него

      // Меню живёт в <body>: иначе его обрезает прокрутка таблицы документа
      this.menu = document.createElement('div');
      this.menu.className = 'ac-menu d-none';
      document.body.appendChild(this.menu);

      root._ac = this;  // для всплывающей карточки: подставить/обновить значение
      this.bind();

      // Значение предзаполнено (напр. из ?partner=), но текст пуст — подтянуть имя
      if (this.valueInput.value && !this.textInput.value) {
        this.prefillLabel(this.valueInput.value);
      }
    }

    async fetchById(id) {
      const p = this.params();
      p.delete('q');
      p.set('id', id);
      const sep = this.url.includes('?') ? '&' : '?';
      const resp = await fetch(this.url + sep + p.toString());
      const json = await resp.json();
      return (json.results || [])[0] || null;
    }

    async prefillLabel(id) {
      try {
        const item = await this.fetchById(id);
        if (item) this.setLabel(item);
      } catch (e) { /* тихо: поле останется пустым, значение всё равно сохранится */ }
    }

    setLabel(item) {
      this.textInput.value = item.label || item.name || '';
      this.lastLabel = this.textInput.value;
      this.root.dispatchEvent(new CustomEvent('ac:label', { detail: item, bubbles: true }));
    }

    // Выбрать значение по id — например, товар, только что созданный в карточке.
    // Возвращает, удалось ли; сбой запроса — исключение.
    async selectById(id) {
      const item = await this.fetchById(id);
      if (!item) return false;
      this.choose(item);
      return true;
    }

    // Сообщение под полем — например, товар создан, но подставить его не вышло
    showError(text) {
      let el = this.root.querySelector('.ac-error');
      if (!el) {
        el = document.createElement('div');
        el.className = 'ac-error text-danger small mt-1';
        this.root.appendChild(el);
      }
      el.textContent = text;
    }

    clearError() {
      const el = this.root.querySelector('.ac-error');
      if (el) el.remove();
    }

    // Обновить подпись выбранного значения (переименовали в карточке)
    async refreshLabel() {
      if (!this.valueInput.value) return;
      try {
        const item = await this.fetchById(this.valueInput.value);
        if (item) this.setLabel(item);
      } catch (e) { /* подпись останется прежней */ }
    }

    bind() {
      this.textInput.setAttribute('autocomplete', 'off');
      this.textInput.addEventListener('input', () => this.schedule());
      this.textInput.addEventListener('focus', () => {
        clearTimeout(this.blurTimer);  // вернулись в поле: закрытие после ухода отменяется
        closeOthers(this);
        this.schedule(0);
      });
      // Уход из поля закрывает подсказки (выбор ловится на mousedown — раньше blur)
      this.textInput.addEventListener('blur', () => {
        this.blurTimer = setTimeout(() => this.close(true), 150);
      });
      this.textInput.addEventListener('keydown', (e) => this.onKeyDown(e));
      document.addEventListener('click', (e) => {
        if (!this.root.contains(e.target) && !this.menu.contains(e.target)) this.close(true);
      });
      window.addEventListener('resize', () => this.position());
      window.addEventListener('scroll', () => this.position(), true);
    }

    position() {
      if (this.menu.classList.contains('d-none')) return;
      const r = this.textInput.getBoundingClientRect();
      const width = Math.max(r.width, 300);
      const spaceBelow = window.innerHeight - r.bottom;
      const height = Math.min(this.menu.scrollHeight, 320);
      const openUp = spaceBelow < height + 16 && r.top > spaceBelow;

      this.menu.style.width = width + 'px';
      this.menu.style.left = Math.min(r.left, window.innerWidth - width - 8) + 'px';
      this.menu.style.top = openUp ? (r.top - height - 2) + 'px' : (r.bottom + 2) + 'px';
    }

    params() {
      const p = new URLSearchParams({ q: this.textInput.value.trim(), ...this.staticParams });
      Object.entries(this.dynParams).forEach(([name, selector]) => {
        const el = document.querySelector(selector);
        if (el && el.value) p.set(name, el.value);
      });
      return p;
    }

    schedule(delay = DEBOUNCE_MS) {
      clearTimeout(this.timer);
      this.searchPending = true;
      this.timer = setTimeout(() => this.load(), delay);
    }

    async load() {
      this.searchRequest += 1;
      const request = this.searchRequest;
      try {
        const resp = await fetch(`${this.url}?${this.params()}`, {
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
        });
        const data = await resp.json();
        if (request !== this.searchRequest) return;
        this.searchPending = false;
        const enterWaiting = this.enterWaiting;
        this.enterWaiting = false;
        this.items = data.results || [];
        this.canCreate = Boolean(data.can_create && this.createUrl);
        // Сканер штрихкодов: точный код → сразу выбираем товар
        if (data.auto_select) {
          const scanned = this.items.find((it) => String(it.id) === String(data.auto_select));
          if (scanned) { this.choose(scanned); return; }
        }
        this.activeIndex = this.items.length ? 0 : -1;
        this.render();
        if (enterWaiting) this.acceptActive();
      } catch (e) {
        if (request !== this.searchRequest) return;
        this.searchPending = false;
        this.enterWaiting = false;
        this.showMenuMessage('Ошибка поиска');
        this.menu.classList.remove('d-none');
      }
    }

    showMenuMessage(text) {
      this.menu.replaceChildren(menuNode('div', 'ac-empty text-danger', text));
    }

    render() {
      const query = this.textInput.value.trim();
      const nodes = [];

      this.items.forEach((item, i) => {
        const row = menuNode('div', i === this.activeIndex ? 'ac-item active' : 'ac-item');
        row.dataset.index = i;
        const main = menuNode('div', 'ac-main');
        main.appendChild(menuNode('div', 'ac-label', item.label || item.name));
        if (item.details) main.appendChild(menuNode('div', 'ac-sub', item.details));
        row.appendChild(main);
        if (item.stock !== null && item.stock !== undefined) {
          const cls = item.stock > 0 ? 'ac-stock' : 'ac-stock ac-stock-zero';
          row.appendChild(menuNode('span', cls, fmtStock(item.stock) + (item.unit ? ' ' + item.unit : '')));
        }
        nodes.push(row);
      });

      if (!this.items.length) {
        nodes.push(menuNode('div', 'ac-empty', query ? 'Ничего не найдено' : 'Начните вводить название'));
      }
      if (this.canCreate && query) {
        const isInn = this.innLabel && /^\d{10}(\d{2})?$/.test(query);
        const label = isInn ? this.innLabel : this.createLabel;
        const icon = isInn ? 'bi-magic' : 'bi-plus-lg';
        const create = menuNode('div', 'ac-create');
        create.dataset.create = '1';
        create.appendChild(menuNode('i', `bi ${icon} me-1`));
        create.appendChild(document.createTextNode(`${label}: «${query}»`));
        nodes.push(create);
      }

      this.menu.replaceChildren(...nodes);
      this.menu.classList.remove('d-none');
      this.position();

      this.menu.querySelectorAll('.ac-item').forEach((el) => {
        el.addEventListener('mousedown', (e) => {
          e.preventDefault();
          this.choose(this.items[Number(el.dataset.index)]);
        });
      });
      const createEl = this.menu.querySelector('.ac-create');
      if (createEl) {
        createEl.addEventListener('mousedown', (e) => {
          e.preventDefault();
          this.createNew();
        });
      }
    }

    onKeyDown(e) {
      const isOpen = !this.menu.classList.contains('d-none');
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!isOpen) return this.schedule(0);
        const delta = e.key === 'ArrowDown' ? 1 : -1;
        this.activeIndex = Math.max(0, Math.min(this.items.length - 1, this.activeIndex + delta));
        this.render();
      } else if (e.key === 'Enter') {
        // Сканер вводит код и сразу жмёт Enter: выбирать надо по ответу на этот код,
        // а не из списка, оставшегося от прежнего запроса
        if (this.searchPending) {
          e.preventDefault();
          this.enterWaiting = true;
        } else if (isOpen) {
          e.preventDefault();
          this.acceptActive();
        }
      } else if (e.key === 'Escape') {
        this.close(true);
      }
    }

    acceptActive() {
      if (this.activeIndex >= 0 && this.items[this.activeIndex]) {
        this.choose(this.items[this.activeIndex]);
      } else if (this.canCreate && this.textInput.value.trim()) {
        this.createNew();
      }
    }

    choose(item) {
      if (!item) return;
      this.clearError();
      this.valueInput.value = item.id;
      this.textInput.value = item.label || item.name;
      this.lastLabel = this.textInput.value;
      this.close();
      this.root.dispatchEvent(new CustomEvent('ac:select', { detail: item, bubbles: true }));
    }

    async createNew() {
      const name = this.textInput.value.trim();
      if (!name) return;
      if (this.root.dataset.acCreatePage) {
        // Создание через полную карточку во всплывающем окне (entity-modal.js)
        this.close();
        this.root.dispatchEvent(new CustomEvent('ac:create', { detail: { name }, bubbles: true }));
        return;
      }
      const body = new URLSearchParams({ name, ...this.staticParams });
      Object.entries(this.dynParams).forEach(([key, selector]) => {
        const el = document.querySelector(selector);
        if (el && el.value) body.set(key, el.value);
      });
      const priceField = this.root.closest('tr') && this.root.closest('tr').querySelector('.line-price');
      if (priceField && priceField.value) body.set('price_value', priceField.value);

      try {
        const resp = await fetch(this.createUrl, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRFToken': csrfToken(),
          },
          body: body.toString(),
        });
        const data = await resp.json();
        if (!data.ok) {
          this.showMenuMessage(data.error || 'Не удалось создать');
          return;
        }
        this.choose(data.product || data.counterparty);
      } catch (e) {
        this.showMenuMessage('Ошибка создания');
      }
    }

    close(restore = false) {
      // Закрытое меню не откроют ни отложенный поиск, ни опоздавший ответ: иначе
      // кнопка «Создать» прежней строки появлялась, когда пользователь уже в другой
      clearTimeout(this.timer);
      this.searchRequest += 1;
      this.searchPending = false;
      this.enterWaiting = false;
      this.menu.classList.add('d-none');
      if (restore) {
        // Текст без выбранного значения не должен «висеть» — возвращаем прежний
        if (!this.valueInput.value) {
          this.textInput.value = '';
        } else if (this.textInput.value !== this.lastLabel) {
          this.textInput.value = this.lastLabel;
        }
      }
    }
  }

  function closeOthers(current) {
    instances.forEach((ac) => {
      if (ac !== current) ac.close(true);
    });
  }

  function init(scope) {
    (scope || document).querySelectorAll('.ac').forEach((root) => {
      if (!root.dataset.acReady) {
        root.dataset.acReady = '1';
        instances.push(new Autocomplete(root));
      }
    });
  }

  window.initAutocomplete = init;
  document.addEventListener('DOMContentLoaded', () => init(document));
  init(document);
})();
