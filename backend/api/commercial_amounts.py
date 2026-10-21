"""Source-grounded selling prices, distinct from contracts and cash receipts."""

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db.models import F

from .deduplication import normalize_deal_name


SALE_AMOUNT_RULES = """
Обязательное правило суммы сделки: явно названную сумму продажи / цену продажи / стоимость нашего КП конкретного объекта извлекай как project.sale_amount. Подписание договора, согласование заказчиком и ручное подтверждение для sale_amount НЕ нужны. Например «Сумма продажи: 29 428 679 тг; Планируемая прибыль: 3 998 607 тг» означает sale_amount=29428679, а не 3998607. «Наше КП направлено заказчику на сумму 18 000 000 тг» означает sale_amount=18000000. Даже при неподписанном договоре сумма продажи должна попасть в поле суммы сделки CRM, а не только в current_action.
contract_amount означает именно явно названную сумму договора; sale_amount не подтверждает договор и не является платежом. Не извлекай сумму продажи из прибыли, маржи, себестоимости, аванса, поступления, остатка, счёта на частичную оплату, бюджета клиента или цены конкурента. Не вычисляй полную цену по проценту платежа. Неизвестную сумму пропусти, не возвращай 0. Цену связывай только с разделом конкретного объекта, не с другими объектами того же отчёта. evidence_message_id для sale_amount должен указывать оригинал с этой ценой, а не поздний ответ о статусе. Более поздняя явно сообщённая цена заменяет прежнюю; переписка без цены её не обнуляет. Округлённый пересказ 29.43 млн ранее указанной точной цены 29428679 не является изменением цены; сохраняй точный оригинал, если не сообщено явное изменение. Эти правила обязательны независимо от сохранённого пользовательского промпта.
"""

# Labels are intentionally about our commercial offer, never just an arbitrary
# monetary number elsewhere in a long sales report.
SALE_LABEL = re.compile(
    r"сумм[аыуе]\s+продаж[и]?|цен[ауы]\s+(?:продаж[и]?|реализаци[и])"
    r"|(?:сумм[аыуе]|стоимост[ьи]|цен[ауы])\s+(?:по\s+)?(?:(?:наш\w*|первоначальн\w*|коммерческ\w*)\s+){0,3}(?:кп\b|предложени\w*)"
    r"|выигран\w*\s+нами\s+с\s+предложени\w*\s+на\s+сумму\s*"
    r"|(?:наш\w*\s+)?(?:кп\b|коммерческ\w*\s+предложени\w*)[^\n;.!?]{0,100}?\b(?:на\s+(?:сумму\s+)?|сумм[аыуе]\s*[:—-]?\s*)",
    re.I,
)
NON_SALE = re.compile(
    r"конкурент|друг\w*\s+поставщик|(?:предложени\w*|кп)\s+(?:от\s+)?поставщик"
    r"|себестоим|прибыл|марж|аванс|предоплат|оплат|поступлен|поступил|получил|получен"
    r"|плат[её]ж|рассрочк|остаток|бюджет|сч[её]т\s+на|закуп|затрат|не\s+(?:известн|определен)",
    re.I,
)
APPROXIMATE = re.compile(r"\b(?:около|порядка|примерно|почти|ориентировочно|от|до)\b", re.I)
OBJECT_START = re.compile(r"^(?:[-*•\d.)\s]+)?(?:жк|бц|мжд|пгу|зру|бмк|цтп|тц|бтп|объект|проект)\b", re.I)
ACTION_WORD = re.compile(r"\b(?:сегодня|вчера|завтра|сумма|цена|стоимость|следующ|планиру|занимал|направ|переда|подготов|отправ|соглас|получ|принят|договор|кп|заказчик|вечером|утром|совместно|после|будет|необходим|работа|выполн|прибыль|маржа|аванс|оплата)\w*", re.I)

CONTRACT_PRICE = re.compile(
    r'\b(?:сумм[аыуе]|стоимост[ьи]|цен[аыуе])\s+(?:по\s+)?(?:договор\w*|контракт\w*)\b'
    r'|\b(?:договор\w*|контракт\w*)\s+(?:заключ[её]н\w*\s+)?на\s+(?:сумму\s+)?', re.I)


def contract_price_evidence(value, quotes):
    """A selling price and a separate signature do not prove contract money."""
    from .autonomous import MONEY, _money_matches
    from .evidence_scope import statements
    for quote in quotes:
        for clause in statements(quote):
            for label in CONTRACT_PRICE.finditer(clause):
                if re.search(r'\b(?:нет|без|неизвест\w*|не\s+указан\w*)\s*$', clause[:label.start()], re.I):
                    continue
                tail = clause[label.end():]
                number = MONEY.search(tail)
                if number is None or APPROXIMATE.search(tail[:number.start()]):
                    continue
                if re.search(r'\b(?:не|нет|без|неизвест\w*|неопредел\w*)\b', tail[:number.start()], re.I):
                    continue
                # A nearer advance/profit/sale label owns its number, even if
                # the same clause also mentions a signed contract.
                if re.search(r'аванс|прибыл|марж|продаж|себестоим|плат[её]ж|оплат|остаток|бюджет', tail[:number.start()], re.I):
                    continue
                suffix = re.split(r'\d', tail[number.end():], maxsplit=1)[0]
                literal = tail[number.start():number.end()] + suffix
                explicit_price = bool(re.search(r'сумм|стоимост|цен', label.group(), re.I))
                if _money_matches(Decimal(str(value)), literal, monetary_unit=not explicit_price):
                    return clause.strip()
    return None


def _named(label, text):
    return bool(label and re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", text))


def sale_evidence(candidate, data, primary):
    """Return the literal own-price clause in this object's source section.

    Full-message evidence is retained by the extraction protocol. Validation
    therefore scopes it to object headings and then to individual clauses;
    finding the number somewhere in the whole report is not sufficient.
    """
    from .autonomous import MONEY, _money_matches
    from .models import Project, ProjectAlias

    value = data["sale_amount"]
    if value <= 0:
        return None
    labels = {normalize_deal_name(data.get("object_name", ""))}
    if candidate.project_id:
        labels.update((candidate.project.normalized_name, normalize_deal_name(candidate.project.name)))
        labels.update(candidate.project.aliases.values_list("normalized_name", flat=True))
    labels.discard("")
    others = set(Project.objects.filter(team_id=candidate.team_id).exclude(pk=candidate.project_id).values_list("normalized_name", flat=True))
    others.update(ProjectAlias.objects.filter(project__team_id=candidate.team_id).exclude(project_id=candidate.project_id).values_list("normalized_name", flat=True))
    others = {label for label in others - labels if len(label) >= 4}
    active = bool(candidate.project_id and primary.project_id == candidate.project_id)
    competitor_section = False
    for line in re.split(r"\n+|;\s*", primary.content):
        normalized = normalize_deal_name(line, max_length=None)
        target = any(_named(label, normalized) for label in labels)
        foreign = any(_named(label, normalized) for label in others if not any(label in own for own in labels))
        if target and foreign:
            # A single sentence naming two different objects does not tell us
            # which one owns a price; require separate clauses/sections.
            active = False
            continue
        if target:
            active = True
            competitor_section = False
        elif foreign or OBJECT_START.search(line) or (0 < len(line.strip()) < 100 and not re.search(r"[\d:.,!?]", line) and not ACTION_WORD.search(line)):
            active = False
        if not active:
            continue
        for clause in re.split(r"(?<=[.!?])\s+|,\s+(?:а|но|и)\s+", line):
            own_offer = re.search(r"наш\w*\s+(?:кп|цен|предложени)|выигран\w*\s+нами\s+с\s+предложени", clause, re.I)
            if re.search(r"конкурент|друг\w*\s+поставщик", clause, re.I) and not own_offer:
                competitor_section = True
            elif own_offer:
                competitor_section = False
            if competitor_section:
                continue
            if "?" in clause:
                continue
            for label in SALE_LABEL.finditer(clause):
                # A profit/payment label *after* the selling price belongs to
                # another number. It must not reject a valid own price, nor may
                # its number satisfy the price check.
                prefix = re.split(r"[,;:]\s*", clause[:label.start()])[-1]
                suffix = clause[label.end():]
                # Another semantic monetary label ends the selling-price
                # clause even without punctuation in a pasted report.
                suffix = re.split(r"прибыл|марж|себестоим|аванс|предоплат|оплат|плат[её]ж|рассрочк|поступ|остаток|бюджет|конкурент", suffix, maxsplit=1, flags=re.I)[0]
                if NON_SALE.search(prefix + label.group()) or re.search(r"конкурент|друг\w*\s+поставщик|(?:кп|предложени\w*)\s+(?:от\s+)?поставщик", clause[:label.end()] + suffix, re.I):
                    continue
                first_number = MONEY.search(suffix)
                if first_number is None or APPROXIMATE.search(suffix[:first_number.start()]):
                    continue
                # Only the nearest literal belongs to this price label. Even
                # an unrecognised later money role must not donate its amount.
                tail = re.split(r"\d", suffix[first_number.end():], maxsplit=1)[0]
                literal = suffix[max(0, first_number.start() - 5):first_number.end()] + tail
                currencies = {code for code, pattern in {"KZT": r"тенге|\bтг\b|₸|\bKZT\b", "USD": r"доллар|\bUSD\b|\$", "EUR": r"евро|\bEUR\b|€", "RUB": r"рубл|\bRUB\b|₽"}.items() if re.search(pattern, literal, re.I)}
                if currencies and currencies != {data.get("currency", "KZT")}:
                    continue
                if _money_matches(value, literal, monetary_unit=True):
                    return clause.strip()
    return None


def rounded_restatement(project, proposed, clause):
    """A close rounded million-token is a retelling, absent a price change."""
    from .autonomous import MONEY

    previous = commercial_amount(project)
    if previous is None or previous <= 0 or previous == proposed:
        return False
    if re.search(r"новая|новую|новой|пересмотр|измен|скорректир|сниз|увелич|теперь|уточнил|уточнили|окончательн|итогов", clause, re.I):
        return False
    if abs(previous - proposed) / previous >= Decimal("0.001"):
        return False
    for token in MONEY.finditer(clause):
        if not (token["scale"] or "").casefold().startswith(("млн", "миллион")):
            continue
        number = token["number"].replace(" ", "").replace("\u00a0", "").replace("\u202f", "").replace(",", ".")
        try:
            literal = Decimal(number)
        except InvalidOperation:
            continue
        if literal * 1_000_000 != proposed:
            continue
        quantum = Decimal(1_000_000) * Decimal(10) ** literal.as_tuple().exponent
        if quantum > 1 and (previous / quantum).quantize(Decimal(1), rounding=ROUND_HALF_UP) * quantum == proposed:
            return True
    return False


def commercial_amount(project):
    """Latest sourced sale/contract price. Unknown values never erase it."""
    from .models import FieldAssertion

    known = set(project.whatsapp_fields)
    allowed = []
    if project.sale_amount is not None and "sale_amount" in known:
        allowed.append("sale_amount")
    if project.contract_known and "contract_amount" in known:
        allowed.append("contract_amount")
    assertions = FieldAssertion.objects.filter(
        project_id=project.pk, field_name__in=allowed,
        fact_event__is_superseded=False,
    ).select_related("fact_event").order_by(F("fact_event__occurred_at").desc(nulls_last=True), "-id")
    for assertion in assertions:
        try:
            value = Decimal(str(assertion.value))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if value > 0:
            return value
    if "sale_amount" in allowed and project.sale_amount > 0:
        return project.sale_amount
    if "contract_amount" in allowed and project.contract_amount > 0:
        return project.contract_amount
    return None
