"""Сборка текста договора из ответов конструктора.

В текстах используются понятные плашки — [ИНН контрагента], [Сумма прописью].
Никакого программного синтаксиса: пользователь вставляет плашку кнопкой,
а видит её как обычные слова в квадратных скобках.
"""
import re

PLACEHOLDER_RE = re.compile(r"\[([^\[\]]{1,60})\]")


def _fmt_amount(value):
    """1234567.5 → «1 234 567,50» (разряды пробелом, копейки через запятую)."""
    if value is None:
        return "____"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _num(value):
    """Число без лишних нулей: 0.100 → 0,1; 10.00 → 10."""
    if value is None:
        return ""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _vat_label(org):
    """Формулировка про НДС по настройкам организации.

    Подставляется в предложение «Цена товара указывается …», поэтому текст
    начинается с предлога.
    """
    if not org or not getattr(org, "vat_payer", True):
        return "без НДС в связи с применением упрощённой системы налогообложения"
    rate = getattr(org, "default_vat_rate", "20")
    if rate in ("none", "", None):
        return "без НДС"
    return f"с учётом НДС по ставке {rate}%"


def _basis_by_kind(kind, ogrn=""):
    """Чем подтверждаются полномочия — по форме контрагента."""
    if kind == "entrepreneur":
        return f"свидетельства о государственной регистрации ОГРНИП {ogrn}".strip()
    if kind == "person":
        return "паспорта"
    return "Устава"


# Частые должности в родительном падеже — чтобы выходило «в лице Генерального директора»
_POSITION_GENITIVE = {
    "генеральный директор": "Генерального директора",
    "директор": "Директора",
    "исполнительный директор": "Исполнительного директора",
    "коммерческий директор": "Коммерческого директора",
    "управляющий": "Управляющего",
    "президент": "Президента",
    "начальник": "Начальника",
    "индивидуальный предприниматель": "Индивидуального предпринимателя",
    "руководитель": "Руководителя",
}


def _position_genitive(position):
    if not position:
        return ""
    return _POSITION_GENITIVE.get(position.strip().lower(), position.strip())


def _is_female(fio_parts):
    """Женское отчество — «…овна», «…ична»; иначе считаем мужским."""
    if len(fio_parts) < 3:
        return False
    return fio_parts[2].lower().endswith(("овна", "евна", "ична", "инична"))


def _genitive_surname(word, female):
    low = word.lower()
    if low.endswith(("ова", "ева", "ёва", "ина", "ына")):
        return word[:-1] + "ой"
    if low.endswith(("ская", "цкая")):
        return word[:-2] + "ой"
    if low.endswith(("ов", "ев", "ёв", "ин", "ын")):
        return word if female else word + "а"
    if low.endswith(("ский", "цкий", "ний")):
        return word[:-2] + "ого"
    if low.endswith("ой"):
        return word[:-2] + "ого"
    if low.endswith(("ко", "их", "ых", "аго", "ago", "е", "и", "о", "у", "ю", "ы", "э")):
        return word  # несклоняемые
    if low.endswith("а"):
        return word if female else word[:-1] + "ы"
    if low.endswith("я"):
        return word if female else word[:-1] + "и"
    if low.endswith("ь"):
        return word if female else word[:-1] + "я"
    if low.endswith("й"):
        return word if female else word[:-1] + "я"
    return word if female else word + "а"  # согласный: Кузнец → Кузнеца


def _genitive_name(word, female):
    """Имя и отчество в родительном падеже."""
    low = word.lower()
    if len(word) <= 2 or word.endswith("."):
        return word  # инициалы не склоняем
    if low.endswith(("а", "я")):
        if low.endswith(("ья", "ия")):
            return word[:-1] + "и"
        return word[:-1] + ("ы" if low.endswith("а") else "и")
    if female:
        return word
    if low.endswith("й"):
        return word[:-1] + "я"
    if low.endswith("ь"):
        return word[:-1] + "я"
    return word + "а"


def genitive_fio(fio):
    """«Иванов Иван Иванович» → «Иванова Ивана Ивановича».

    Правила покрывают обычные русские фамилии, имена и отчества. Инициалы
    и незнакомые окончания остаются как есть — лучше не склонить, чем
    исказить фамилию в договоре.
    """
    parts = (fio or "").split()
    if not parts:
        return ""
    female = _is_female(parts)
    result = [_genitive_surname(parts[0], female)]
    result += [_genitive_name(word, female) for word in parts[1:]]
    return " ".join(result)


def _party_intro(name, director, basis, position=""):
    """«в лице Генерального директора Иванова Ивана Ивановича,
    действующего на основании Устава»"""
    if not director:
        return ""
    female = _is_female(director.split())
    who = f"{_position_genitive(position)} {genitive_fio(director)}".strip()
    acting = "действующей" if female else "действующего"
    return f"в лице {who}, {acting} на основании {basis}"


def tidy(text):
    """Убирает следы незаполненных данных: двойные запятые и пустые скобки."""
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"«\s*»", "", text)
    text = re.sub(r" +,", ",", text)
    text = re.sub(r",\s*\.", ".", text)
    return text


def build_values(contract):
    """Значения для всех плашек конкретного договора."""
    from apps.documents.money import amount_to_words

    org = contract.organization
    cp = contract.counterparty
    bank = None
    if cp:
        bank = cp.bank_accounts.filter(is_default=True).first() or cp.bank_accounts.first()

    org_basis = (getattr(org, "signatory_basis", "") or "Устава") if org else "Устава"
    cp_basis = _basis_by_kind(getattr(cp, "kind", "legal"), getattr(cp, "ogrn", "")) if cp else ""

    values = {
        # Договор
        "Номер договора": contract.number or "____",
        "Дата договора": f"{contract.date:%d.%m.%Y}" if contract.date else "«___» __________ ____ г.",
        "Место подписания": getattr(contract, "place", "") or "____________",
        "Предмет договора": contract.subject or "",
        "Срок действия": contract.validity_display,
        "Дата начала": f"{contract.valid_from:%d.%m.%Y}" if contract.valid_from else "____",
        "Дата окончания": f"{contract.valid_to:%d.%m.%Y}" if contract.valid_to else "____",
        "Сумма": _fmt_amount(contract.amount),
        "Сумма прописью": amount_to_words(contract.amount) if contract.amount is not None else "____",
        "Отсрочка, дней": contract.payment_delay_days if contract.payment_delay_days else "____",
        "Предоплата, %": _num(contract.prepayment_percent),
        # Поставка, претензии, ответственность
        "Место поставки": contract.delivery_place or "склад Покупателя",
        "Срок поставки, дней": contract.delivery_days if contract.delivery_days else "____",
        "Неустойка, % в день": _num(contract.penalty_rate) or "0,1",
        "Предельная неустойка, %": _num(contract.penalty_cap_percent) or "10",
        "Срок ответа на претензию, дней": contract.claim_days if contract.claim_days else "10",
        "Гарантия, месяцев": contract.warranty_months if contract.warranty_months else "12",
        "Ставка НДС": _vat_label(org),
        # Товар
        "Состояние товара": contract.get_goods_condition_display() if contract.goods_condition else "",
        "Описание состояния": contract.condition_phrase or "соответствующим условиям Договора",
        "Сведения о товаре": contract.goods_details or "",
        # Наша сторона
        "Наша организация": (org.full_name or org.name) if org else "",
        "Наше краткое название": org.name if org else "",
        "Наш ИНН": org.inn if org else "",
        "Наш КПП": org.kpp if org else "",
        "Наш ОГРН": org.ogrn if org else "",
        "Наш адрес": org.legal_address if org else "",
        "Наш телефон": org.phone if org else "",
        "Наш банк": org.bank_name if org else "",
        "Наш расчётный счёт": org.bank_account if org else "",
        "Наш БИК": org.bik if org else "",
        "Наш корр. счёт": org.corr_account if org else "",
        "Наш руководитель": org.director_name if org else "",
        "Должность нашего руководителя": (org.director_position or "") if org else "",
        "Мы в лице": _party_intro(
            org.name if org else "", org.director_name if org else "",
            org_basis, org.director_position if org else "",
        ),
        # Контрагент
        "Контрагент": (cp.full_name or cp.name) if cp else "",
        "Краткое название контрагента": cp.name if cp else "",
        "ИНН контрагента": cp.inn if cp else "",
        "КПП контрагента": cp.kpp if cp else "",
        "ОГРН контрагента": cp.ogrn if cp else "",
        "Адрес контрагента": cp.legal_address if cp else "",
        "Телефон контрагента": cp.phone if cp else "",
        "Банк контрагента": bank.bank_name if bank else "",
        "Расчётный счёт контрагента": bank.account if bank else "",
        "БИК контрагента": bank.bik if bank else "",
        "Корр. счёт контрагента": bank.corr_account if bank else "",
        "Руководитель контрагента": cp.director_name if cp else "",
        "Контрагент в лице": _party_intro(
            cp.name if cp else "", cp.director_name if cp else "", cp_basis,
        ),
    }
    return {k: ("" if v is None else str(v)) for k, v in values.items()}


# Плашки, сгруппированные для панели «Вставить данные»
PLACEHOLDER_GROUPS = [
    ("Договор", ["Номер договора", "Дата договора", "Место подписания", "Предмет договора",
                 "Срок действия", "Дата начала", "Дата окончания"]),
    ("Деньги", ["Сумма", "Сумма прописью", "Отсрочка, дней", "Предоплата, %", "Ставка НДС"]),
    ("Товар", ["Состояние товара", "Описание состояния", "Сведения о товаре"]),
    ("Поставка и претензии", ["Место поставки", "Срок поставки, дней", "Гарантия, месяцев",
                              "Неустойка, % в день", "Предельная неустойка, %",
                              "Срок ответа на претензию, дней"]),
    ("Наша сторона", ["Наша организация", "Наше краткое название", "Мы в лице", "Наш ИНН", "Наш КПП",
                      "Наш ОГРН", "Наш адрес", "Наш телефон", "Наш банк", "Наш расчётный счёт",
                      "Наш БИК", "Наш корр. счёт", "Наш руководитель", "Должность нашего руководителя"]),
    ("Контрагент", ["Контрагент", "Краткое название контрагента", "Контрагент в лице",
                    "ИНН контрагента", "КПП контрагента", "ОГРН контрагента", "Адрес контрагента",
                    "Телефон контрагента", "Банк контрагента", "Расчётный счёт контрагента",
                    "БИК контрагента", "Корр. счёт контрагента", "Руководитель контрагента"]),
]


def fill_placeholders(text, values):
    """Заменяет [плашки] значениями. Незнакомая плашка остаётся как есть —
    пользователь сразу видит опечатку, ничего не ломается."""
    if not text:
        return ""

    def replace(match):
        key = match.group(1).strip()
        return values.get(key, match.group(0))

    return PLACEHOLDER_RE.sub(replace, text)


def _sections_of(contract, values):
    """Разделы договора с проставленной нумерацией — общий источник
    для текстового предпросмотра, PDF и Word."""
    answers = (
        contract.answers.select_related("question", "option")
        .order_by("question__order", "id")
    )
    sections = []
    current = None
    for answer in answers:
        text = answer.text.strip()
        if not text:
            continue  # «Пункт не предусмотрен»
        title = answer.question.section or ""
        if current is None or current["title"] != title:
            current = {"number": len(sections) + 1, "title": title, "items": []}
            sections.append(current)
        for line in tidy(fill_placeholders(text, values)).split("\n"):
            line = line.strip()
            if not line:
                continue
            current["items"].append({
                "number": f"{current['number']}.{len(current['items']) + 1}",
                "text": line,
            })
    return sections


def build_contract_text(contract):
    """Собирает готовый договор обычным текстом — для предпросмотра в карточке.

    Разделы и пункты нумеруются автоматически, в формулировках номера
    писать не нужно.
    """
    template = contract.template
    if not template:
        return ""

    values = build_values(contract)
    parts = []

    title = template.title or (template.get_kind_display() if template.kind else "ДОГОВОР")
    parts.append(f"{title} № {values['Номер договора']}".strip())
    parts.append("")
    parts.append(f"г. {values['Место подписания']}{' ' * 40}{values['Дата договора']}")
    parts.append("")

    if template.intro:
        parts.append(tidy(fill_placeholders(template.intro, values)).strip())
        parts.append("")

    sections = _sections_of(contract, values)
    for section in sections:
        parts.append("")
        parts.append(f"{section['number']}. {section['title'].upper()}"
                     if section["title"] else f"{section['number']}.")
        for item in section["items"]:
            parts.append(f"{item['number']}. {item['text']}")

    if template.outro:
        parts.append("")
        parts.append(f"{len(sections) + 1}. АДРЕСА, РЕКВИЗИТЫ И ПОДПИСИ СТОРОН")
        parts.append(fill_placeholders(template.outro, values))

    return "\n".join(parts).strip() + "\n"


# Как называются стороны в блоке подписей — по виду договора
_PARTY_ROLES = {
    "supply": ("ПОСТАВЩИК", "ПОКУПАТЕЛЬ"),
    "sale": ("ПРОДАВЕЦ", "ПОКУПАТЕЛЬ"),
    "service": ("ИСПОЛНИТЕЛЬ", "ЗАКАЗЧИК"),
}

# (подпись строки, плашка нашей стороны, плашка контрагента)
_REQUISITE_ROWS = [
    ("ИНН", "Наш ИНН", "ИНН контрагента"),
    ("КПП", "Наш КПП", "КПП контрагента"),
    ("ОГРН", "Наш ОГРН", "ОГРН контрагента"),
    ("Адрес", "Наш адрес", "Адрес контрагента"),
    ("Телефон", "Наш телефон", "Телефон контрагента"),
    ("Банк", "Наш банк", "Банк контрагента"),
    ("Р/с", "Наш расчётный счёт", "Расчётный счёт контрагента"),
    ("БИК", "Наш БИК", "БИК контрагента"),
    ("К/с", "Наш корр. счёт", "Корр. счёт контрагента"),
]


def _party(role, values, name_key, signer_key, side):
    """Реквизиты одной стороны для таблицы подписей. Пустые строки не выводим."""
    rows = []
    for label, our_key, their_key in _REQUISITE_ROWS:
        value = values.get(our_key if side == "our" else their_key, "")
        if value:
            rows.append((label, value))
    return {
        "role": role,
        "name": values.get(name_key, ""),
        "rows": rows,
        "signer": values.get(signer_key, ""),
    }


def build_contract_blocks(contract):
    """Структурированный договор для PDF и Word: заголовок, преамбула,
    пронумерованные разделы и таблица реквизитов."""
    template = contract.template
    if not template:
        return None

    values = build_values(contract)
    intro = [
        tidy(fill_placeholders(block, values)).strip().replace("\n", " ")
        for block in (template.intro or "").split("\n\n") if block.strip()
    ]
    sections = _sections_of(contract, values)

    roles = _PARTY_ROLES.get(template.kind)
    parties = None
    outro_text = ""
    if roles:
        parties = (
            _party(roles[0], values, "Наша организация", "Наш руководитель", "our"),
            _party(roles[1], values, "Контрагент", "Руководитель контрагента", "their"),
        )
    elif template.outro:
        outro_text = fill_placeholders(template.outro, values)

    return {
        "title": template.title or (template.get_kind_display() if template.kind else "ДОГОВОР"),
        "number": values["Номер договора"],
        "place": values["Место подписания"],
        "date": values["Дата договора"],
        "intro": intro,
        "sections": sections,
        "requisites_number": len(sections) + 1,
        "parties": parties,
        "outro_text": outro_text,
    }


def default_option(question):
    """Вариант, отмеченный при создании договора.

    Необязательный пункт без явного варианта по умолчанию в договор
    не попадает — иначе редкие условия (маркировка, trade-in) вошли бы
    в текст сами собой.
    """
    default = question.options.filter(is_default=True).first()
    if default is None and not question.allow_none:
        default = question.options.first()
    return default


def sync_answers(contract):
    """Создаёт недостающие ответы по вопросам шаблона (варианты по умолчанию)."""
    from .models import ContractAnswer

    if not contract.template:
        return
    existing = set(contract.answers.values_list("question_id", flat=True))
    for question in contract.template.questions.prefetch_related("options"):
        if question.id in existing:
            continue
        default = default_option(question)
        ContractAnswer.objects.create(
            contract=contract, question=question, option=default,
            is_skipped=default is None and question.allow_none,
            body_snapshot=default.body if default else "",
        )


# Обратная совместимость со старым названием
def render_contract_body(contract):
    return build_contract_text(contract)
