// Контрагент платежа: какой тип искать и с каким создавать, решает назначение
// для взаиморасчётов (при «не указано» — ещё и счёт-основание), а не только
// направление денег. Правило приходит со страницы (payment-counterparty-rules) —
// то же, что проверяет форма на сервере (PaymentForm.clean).
//
// Выбранный контрагент, который новому назначению не подходит, снимается с
// сообщением под полем: скрыто он не остаётся. Тип контрагента не меняется.
(function () {
  const rulesNode = document.getElementById('payment-counterparty-rules');
  const valueInput = document.querySelector('.ac .ac-value[name=counterparty]');
  if (!rulesNode || !valueInput) return;
  const rules = JSON.parse(rulesNode.textContent);
  const root = valueInput.closest('.ac');
  const ac = root._ac;
  const purpose = document.querySelector('select[name=settlement_purpose]');
  const invoice = document.querySelector('[name=invoice]');
  let check = 0;  // номер последней проверки выбранного: опоздавшие не учитываются

  function currentRule() {
    const byPurpose = rules.purposes[purpose ? purpose.value : ''] || rules.purposes[''];
    return byPurpose[invoice && invoice.value ? 'invoice' : 'none'] || byPurpose.none;
  }

  function fits(item, rule) {
    return !rule.type || item.partner_type === rule.type || item.partner_type === 'both';
  }

  function refuse(item, rule) {
    check += 1;
    ac.valueInput.value = '';
    ac.textInput.value = '';
    ac.lastLabel = '';
    // Текст — только как текст; функции замены: «$&» в названии не раскрывается
    ac.showError(rule.message
      .replace('{name}', () => item.label || item.name || '')
      .replace('{type}', () => rules.type_labels[item.partner_type] || ''));
  }

  async function verifySelected() {
    const id = ac.valueInput.value;
    const ticket = ++check;
    if (!id || !currentRule().type) return;
    let item;
    try {
      item = await ac.fetchById(id);
    } catch (e) {
      return;  // сервер всё равно проверит при сохранении
    }
    // назначение снова сменили или выбрали другого — эта проверка устарела
    if (ticket !== check || ac.valueInput.value !== id) return;
    const rule = currentRule();
    if (item && !fits(item, rule)) refuse(item, rule);
  }

  function apply(verify) {
    const rule = currentRule();
    ac.setParams({ type: rule.type }, { type: rule.create });
    if (verify) {
      ac.clearError();
      verifySelected();
    }
  }

  // Выбор из списка подходит всегда (список отфильтрован); «Создать» может вернуть
  // уже существующего контрагента другого типа — его не подставляем
  root.addEventListener('ac:select', (e) => {
    const item = e.detail;
    if (item.partner_type === undefined) {
      verifySelected();
    } else if (!fits(item, currentRule())) {
      refuse(item, currentRule());
    } else {
      check += 1;
    }
  });
  if (purpose) purpose.addEventListener('change', () => apply(true));
  if (invoice) invoice.addEventListener('change', () => apply(true));
  apply(false);  // показанная сервером форма уже проверена: только условия поиска
})();
