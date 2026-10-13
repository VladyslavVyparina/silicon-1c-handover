"""Багатокроковий сценарій консультанта Олега: консультація → CRM → НП → оплата."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ChatDocument, ChatMessageResponse
from app.database.models import ConversationState, Order, Client, DeliveryInfo
from app.services.ai_consultant import AIConsultant
from app.services.order_service import OrderService
from app.services.pricing_calculator import PricingCalculator, format_money, format_quote_message
from app.services.delivery_rules import (
    DELIVERY_TYPE_ADDRESS,
    DELIVERY_TYPE_POSTOMAT,
    DELIVERY_TYPE_WAREHOUSE,
    build_recipient_name,
    validate_delivery_payload,
)
from app.services import chat_history

consultant = AIConsultant()
calculator = PricingCalculator()
order_service = OrderService()

MANAGER_HANDOFF_REPLY = None  # динамічно з імені менеджера


MANAGER_WAIT_REPLY = (
    "Передав запит менеджеру — він звʼяжеться з вами найближчим часом."
)


def manager_greeting_text(name: str | None) -> str:
    display = (name or "").strip() or "менеджер"
    return (
        f"Вітаю!\n"
        f"Мене звати {display}, я менеджер компанії Silicon.\n"
        f"Чим я можу вам допомогти?"
    )


async def _manager_greeting(db: AsyncSession) -> str:
    """Залишено для сумісності; ім'я береться з активного менеджера/власника."""
    from app.database.models import AdminUser

    result = await db.execute(
        select(AdminUser)
        .where(AdminUser.is_active == True, AdminUser.role == "manager")  # noqa: E712
        .order_by(AdminUser.id.desc())
        .limit(1)
    )
    manager = result.scalar_one_or_none()
    if not manager:
        result = await db.execute(
            select(AdminUser).where(AdminUser.is_active == True, AdminUser.role == "owner").limit(1)  # noqa: E712
        )
        manager = result.scalar_one_or_none()
    name = (manager.full_name if manager and manager.full_name else None) or "менеджер"
    return manager_greeting_text(name)

GREETING = (
    "Вітаю! Я Олег, консультант компанії Silicon.\n"
    "Допоможу підібрати продукцію, прорахувати та оформити замовлення.\n\n"
    "Що вас цікавить?"
)

ORDER_PROMPT = "Супер, оформлюємо 🙂 Напишіть, будь ласка, ім'я та прізвище одним повідомленням."

ORDER_CONFIRM_PROMPT = (
    "Все підходить? Можемо оформлювати замовлення?\n"
    "Якщо потрібен ще товар у це ж замовлення — напишіть який і кількість."
)

KNOWN_CITIES = {
    "київ": "Київ",
    "киев": "Київ",
    "львів": "Львів",
    "львов": "Львів",
    "одеса": "Одеса",
    "одесса": "Одеса",
    "харків": "Харків",
    "харьков": "Харків",
    "дніпро": "Дніпро",
    "днепр": "Дніпро",
    "запоріжжя": "Запоріжжя",
    "винниця": "Вінниця",
    "вінниця": "Вінниця",
    "житомир": "Житомир",
    "полtava": "Полтава",
    "чернігів": "Чернігів",
    "чернигов": "Чернігів",
    "суми": "Суми",
    "рівне": "Рівне",
    "ровно": "Рівне",
    "тернопіль": "Тернопіль",
    "івано-франківськ": "Івано-Франківськ",
    "луцьк": "Луцьк",
    "ужгород": "Ужгород",
    "хрещатик": "Хрещатик",
}

DELIVERY_TYPE_PROMPT = (
    "Куди зручніше відправити Новою поштою — на відділення, у поштомат чи за адресою?"
)

WAREHOUSE_PROMPT = "Який номер відділення чи поштомату?"

ADDRESS_PROMPT = "Напишіть, будь ласка, повну адресу доставки."

CRM_FIELD_QUESTIONS = {
    "person_name": "Напишіть, будь ласка, ім'я та прізвище одним повідомленням 🙂",
    "phone": "Який номер телефону зручніший для зв'язку?",
    "product_type": "Який саме товар беремо?",
    "quantity": "Яка кількість потрібна?",
}

DELIVERY_FIELD_ORDER = [
    ("recipient_last_name", "Прізвище отримувача?"),
    ("recipient_first_name", "А ім'я отримувача?"),
    ("recipient_patronymic", "І по батькові, будь ласка?"),
    ("recipient_phone", "Телефон отримувача?"),
    ("city", "У яке місто відправляємо?"),
    ("warehouse", "Який номер відділення чи поштомату?"),
    ("street_address", "Напишіть, будь ласка, повну адресу?"),
]

PAYMENT_PROMPT = (
    "Як зручніше оплатити?\n"
    "1) карткою онлайн (LiqPay)\n"
    "2) за рахунком (для ФОП/ТОВ)\n"
    "Напишіть «liqpay» або «рахунок»."
)

INVOICE_PROMPT = (
    "Для рахунку потрібні дані організації:\n"
    "1) Назва організації\n"
    "2) Код ЄДРПОУ\n"
    "3) ІПН (якщо є; якщо немає — напишіть «немає»)\n"
    "4) Адреса організації\n\n"
    "Почнімо з назви організації."
)

THANKS = "Замовлення прийняв. Якщо щось ще потрібно — пишіть."


async def _get_state(db: AsyncSession, chat_id: str) -> ConversationState:
    result = await db.execute(select(ConversationState).where(ConversationState.chat_id == chat_id))
    state = result.scalar_one_or_none()
    if state:
        return state
    state = ConversationState(chat_id=chat_id, step="idle", data_json="{}")
    db.add(state)
    await db.flush()
    return state


def _load_data(state: ConversationState) -> dict[str, Any]:
    try:
        return json.loads(state.data_json or "{}")
    except json.JSONDecodeError:
        return {}


def _save_data(state: ConversationState, data: dict[str, Any]) -> None:
    state.data_json = json.dumps(data, ensure_ascii=False)


def _parse_kv(text: str) -> dict[str, str]:
    """Парсить 'Ключ: значення' рядки та вільний текст з телефоном/email."""
    mapping = {
        "прізвище": "last_name",
        "фамилия": "last_name",
        "ім'я": "first_name",
        "імя": "first_name",
        "имя": "first_name",
        "по батькові": "patronymic",
        "по батькови": "patronymic",
        "отчество": "patronymic",
        "телефон": "phone",
        "phone": "phone",
        "email": "email",
        "емейл": "email",
        "пошта": "email",
        "місто": "city",
        "город": "city",
        "товар": "product_type",
        "кількість": "quantity",
        "количество": "quantity",
        "логотип": "logo",
        "коментар": "comment",
        "комментарий": "comment",
        "відділення": "warehouse",
        "отделение": "warehouse",
        "поштомат": "warehouse",
        "адреса": "street_address",
        "адрес": "street_address",
        "адреса організації": "company_address",
        "юридична адреса": "company_address",
        "компанія": "company_name",
        "компания": "company_name",
        "назва": "company_name",
        "назва організації": "company_name",
        "єдрпоу": "edrpou",
        "едрпоу": "edrpou",
        "edrpou": "edrpou",
        "іпн": "ipn",
        "ипн": "ipn",
        "інн": "ipn",
        "инн": "ipn",
        "ipn": "ipn",
        "iban": "bank_account",
        "р/р": "bank_account",
        "банк": "bank_name",
    }
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line and "—" not in line and "-" not in line[1:]:
            continue
        sep = ":" if ":" in line else ("—" if "—" in line else "-")
        key_raw, val = line.split(sep, 1)
        key = key_raw.strip().lower()
        val = val.strip()
        for uk, en in mapping.items():
            if uk in key:
                out[en] = val
                break
    phone = re.search(r"(\+?380\d{9}|\+?0\d{9})", text.replace(" ", ""))
    if phone and "phone" not in out:
        out["phone"] = phone.group(1)
    email = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
    if email and "email" not in out:
        out["email"] = email.group(0)
    return out


def _missing_crm_fields(data: dict[str, Any]) -> list[str]:
    """Контакти: ім'я+прізвище разом, телефон. Місто — на кроці доставки."""
    _sync_cart_fields(data)
    order: list[str] = []
    if not (data.get("first_name") and data.get("last_name")):
        order.append("person_name")
    if not data.get("phone"):
        order.append("phone")
    if not data.get("product_type"):
        order.append("product_type")
    if not data.get("quantity"):
        order.append("quantity")
    return order


def _split_person_name(raw: str) -> tuple[str, str]:
    """Ім'я та прізвище з одного рядка — як написав клієнт, без «виправлень»."""
    parts = [p for p in re.split(r"\s+", (raw or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    # «Ім'я Прізвище» або «Прізвище Ім'я» — беремо перше слово як ім'я, решту як прізвище
    return parts[0], " ".join(parts[1:])


def _extract_qty(text: str) -> str | None:
    lower = text.lower()
    # \b + без пробілів у числі — щоб «300x30 500 штук» не стало 30500
    m = re.search(r"(?<![xх×\d])(\d{1,6})\s*(?:шт|штук|шт\.|pcs)\b", lower)
    if m:
        return m.group(1)
    return None


def _is_reflective_text(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        w in lower
        for w in (
            "світловідб",
            "світло відб",
            "відбивн",
            "рефлект",
            "reflective",
            "reflect",
        )
    )


def _extract_product_hint(text: str) -> str | None:
    lower = text.lower()
    mapping = [
        ("rfid", "RFID-браслети"),
        ("світловідб", "світловідбиваючі браслети"),
        ("світло відб", "світловідбиваючі браслети"),
        ("відбивн", "світловідбиваючі браслети"),
        ("рефлект", "світловідбиваючі браслети"),
        ("reflective", "світловідбиваючі браслети"),
        ("tyvek", "браслети Tyvek"),
        ("slap", "slap-браслети"),
        ("еко-чаш", "еко-чашки"),
        ("килим", "силіконові килимки"),
        ("шапоч", "силіконові шапочки"),
        ("брелок", "брелоки"),
        ("голограм", "голограми"),
        ("шнур", "шнурки для бейджів"),
        ("силікон", "силіконові браслети"),
        ("браслет", "силіконові браслети"),
    ]
    # якщо в одному повідомленні і силікон, і світловідбиваючі — не обирати один «головний»
    if "силікон" in lower and _is_reflective_text(lower):
        return None
    for key, name in mapping:
        if key in lower:
            return name
    return None


def _extract_order_lines(text: str) -> list[dict[str, Any]]:
    """Кілька позицій з одного повідомлення: товар + кількість (+ мм)."""
    lower = (text or "").lower()
    patterns = [
        (r"світловідб\w*|світло\s*відб\w*|відбивн\w*|рефлект\w*|reflective", "світловідбиваючий"),
        (r"силікон\w*", "силіконовий"),
        (r"rfid", "RFID"),
        (r"tyvek", "tyvek"),
        (r"slap", "slap"),
    ]
    hits: list[dict[str, Any]] = []
    for pat, ptype in patterns:
        for m in re.finditer(pat, lower):
            hits.append({"start": m.start(), "end": m.end(), "product_type": ptype})
    if not hits:
        return []
    hits.sort(key=lambda h: h["start"])
    cleaned: list[dict[str, Any]] = []
    for h in hits:
        if cleaned and h["start"] < cleaned[-1]["end"]:
            continue
        cleaned.append(h)

    lines: list[dict[str, Any]] = []
    for i, h in enumerate(cleaned):
        seg_end = cleaned[i + 1]["start"] if i + 1 < len(cleaned) else len(lower)
        segment = lower[h["start"] : seg_end]
        qty_raw = _extract_qty(segment)
        if not qty_raw:
            interest = re.search(r"(?:цікавить|потрібн\w*|бер[еи]мо)\s+(\d{2,5})\b", segment)
            if interest:
                qty_raw = interest.group(1)
        if not qty_raw:
            # «браслети 300x30 500» без слова «штук» — беремо останнє число, що не розмір
            flat_sizes: set[str] = set(re.findall(r"(\d+)\s*мм", segment))
            for a, b in re.findall(r"(\d+)\s*[xх×]\s*(\d+)", segment):
                flat_sizes.add(a)
                flat_sizes.add(b)
            candidates = [n for n in re.findall(r"\b(\d{2,5})\b", segment) if n not in flat_sizes]
            if candidates:
                qty_raw = candidates[-1]
        if not qty_raw:
            continue
        qty = int(qty_raw)
        size_m = re.search(r"(\d+)\s*мм", segment)
        size_mm = size_m.group(1) if size_m else None
        lines.append({
            "product_type": h["product_type"],
            "quantity": qty,
            "size_mm": size_mm,
        })
    return lines


def _sync_cart_fields(data: dict[str, Any]) -> None:
    cart = list(data.get("cart") or [])
    if not cart:
        return
    data["quantity"] = str(sum(int(r.get("quantity") or 0) for r in cart))
    data["total_amount"] = round(sum(float(r.get("total_amount") or 0) for r in cart), 2)
    if len(cart) == 1:
        data["product_type"] = cart[0].get("product_name") or data.get("product_type")
        data["unit_price"] = cart[0].get("unit_price")
        if cart[0].get("size_mm"):
            data["size_mm"] = cart[0]["size_mm"]
    else:
        data["product_type"] = "; ".join(
            f"{r.get('product_name')} × {r.get('quantity')}" for r in cart
        )
        data["unit_price"] = None
    data["quote_ready"] = True


def _build_cart_from_lines(data: dict[str, Any], lines: list[dict[str, Any]]) -> dict | None:
    """Повністю зібрати кошик з розпарсених позицій. Повертає останній успішний прорахунок."""
    cart: list[dict[str, Any]] = []
    last: dict | None = None
    for line in lines:
        result = calculator.calculate(
            product_type=str(line["product_type"]),
            quantity=int(line["quantity"]),
            size_mm=str(line["size_mm"]) if line.get("size_mm") else None,
        )
        if not result.get("success"):
            continue
        size_mm = line.get("size_mm")
        if not size_mm and result.get("product_key"):
            m = re.search(r"(\d+)mm", str(result["product_key"]))
            if m:
                size_mm = m.group(1)
        cart.append(_cart_line_from_quote(result, str(size_mm) if size_mm else None))
        last = result
    if not cart or not last:
        return None
    data["cart"] = cart
    _sync_cart_fields(data)
    return last


def _apply_product_hint(data: dict[str, Any], hint: str, *, clear_cart: bool = False) -> None:
    """Оновити товар у стані; скинути несумісні ціну/розмір при зміні типу."""
    old = str(data.get("product_type") or "")
    if hint == old:
        return
    old_l, new_l = old.lower(), hint.lower()
    family_changed = (
        _is_reflective_text(old_l) != _is_reflective_text(new_l)
        or ("силікон" in old_l) != ("силікон" in new_l)
        or (bool(old) and hint != old)
    )
    data["product_type"] = hint
    if family_changed:
        data.pop("size_mm", None)
        data.pop("total_amount", None)
        data.pop("unit_price", None)
        data.pop("quote_ready", None)
        # старий тираж не переносимо на інший товар
        if _is_reflective_text(old_l) != _is_reflective_text(new_l) or (
            ("силікон" in old_l) != ("силікон" in new_l)
        ):
            data.pop("quantity", None)
            if clear_cart:
                data.pop("cart", None)


def _merge_order_hints(data: dict[str, Any], text: str) -> None:
    """Підтягнути товар/кількість з фрази клієнта (актуальне повідомлення важливіше за старе)."""
    # «ще світловідбиваючі…» — кошик не чіпаємо; запам'ятовуємо товар для наступної кількості
    if _wants_add_to_cart(text):
        prod = _extract_product_hint(text)
        if prod:
            data["pending_product_type"] = prod
        qty = _extract_qty(text)
        if qty:
            data["quantity"] = qty
        return
    # спочатку товар (може скинути qty), потім кількість з цього ж повідомлення
    prod = _extract_product_hint(text)
    if prod:
        _apply_product_hint(data, prod, clear_cart=False)
        data.pop("pending_product_type", None)
    qty = _extract_qty(text)
    if qty:
        data["quantity"] = qty


def _extract_edrpou(text: str) -> str | None:
    m = re.search(r"\b(\d{8,10})\b", text or "")
    return m.group(1) if m else None


async def _load_saved_profile(db: AsyncSession, chat_id: str) -> dict[str, Any]:
    """Дані клієнта/доставки з минулих замовлень (Client + DeliveryInfo)."""
    profile: dict[str, Any] = {}
    client = (
        await db.execute(select(Client).where(Client.chat_id == str(chat_id)))
    ).scalar_one_or_none()
    if client:
        for key in ("last_name", "first_name", "phone", "city"):
            val = getattr(client, key, None)
            if val:
                profile[key] = val
    if client:
        delivery = (
            await db.execute(select(DeliveryInfo).where(DeliveryInfo.client_id == client.id))
        ).scalar_one_or_none()
        if delivery:
            if delivery.delivery_type:
                profile["delivery_type"] = delivery.delivery_type
            if delivery.warehouse:
                profile["warehouse"] = delivery.warehouse
            if delivery.street_address:
                profile["street_address"] = delivery.street_address
            if delivery.city and not profile.get("city"):
                profile["city"] = delivery.city
            if delivery.recipient_phone and not profile.get("phone"):
                profile["phone"] = delivery.recipient_phone
            if delivery.recipient_last_name and not profile.get("last_name"):
                profile["last_name"] = delivery.recipient_last_name
            if delivery.recipient_first_name and not profile.get("first_name"):
                profile["first_name"] = delivery.recipient_first_name
            if delivery.recipient_patronymic:
                profile["recipient_patronymic"] = delivery.recipient_patronymic
    return profile


def _normalize_words(text: str) -> str:
    """Правильна орфографія для відображення: Київ, не КИЇв."""
    raw = (text or "").strip()
    if not raw:
        return raw
    lower = raw.lower().replace("ї", "ї")
    if lower in KNOWN_CITIES:
        return KNOWN_CITIES[lower]
    parts = []
    for word in raw.split():
        key = word.lower()
        if key in KNOWN_CITIES:
            parts.append(KNOWN_CITIES[key])
        elif word.isupper() or word.islower() or (word[:1].islower() and any(c.isupper() for c in word[1:])):
            parts.append(word[:1].upper() + word[1:].lower())
        else:
            parts.append(word[:1].upper() + word[1:] if word else word)
    return " ".join(parts)


def _wants_add_to_cart(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        p in lower
        for p in (
            "ще ",
            "а ще",
            "і ще",
            "також",
            "плюс",
            "додай",
            "додати",
            "і світловідб",
            "і силікон",
            "ще товар",
            "ще браслет",
        )
    )


def _cart_line_from_quote(result: dict, size_mm: str | None = None) -> dict[str, Any]:
    return {
        "product_key": result.get("product_key"),
        "product_name": result.get("product_name") or result.get("product_type") or "Товар",
        "quantity": int(result.get("quantity") or 0),
        "unit_price": float(result.get("unit_price") or 0),
        "total_amount": float(result.get("total_amount") or 0),
        "production_days": result.get("production_days"),
        "size_mm": size_mm,
    }


def _apply_quote_to_cart(data: dict[str, Any], result: dict, text: str = "") -> list[dict]:
    """Оновити кошик: замінити або додати позицію (кілька товарів в одному замовленні)."""
    size_mm = data.get("size_mm")
    if not size_mm and result.get("product_key"):
        m = re.search(r"(\d+)mm", str(result["product_key"]))
        if m:
            size_mm = m.group(1)
    item = _cart_line_from_quote(result, str(size_mm) if size_mm else None)
    cart = list(data.get("cart") or [])
    append = _wants_add_to_cart(text) or (
        bool(cart)
        and item["product_name"] != cart[-1].get("product_name")
        and _extract_product_hint(text) is not None
    )
    if append and cart:
        # той самий товар з новою кількістю — оновити рядок
        same_idx = next(
            (i for i, row in enumerate(cart) if row.get("product_name") == item["product_name"]),
            None,
        )
        if same_idx is not None and not _wants_add_to_cart(text):
            cart[same_idx] = item
        else:
            cart.append(item)
    else:
        cart = [item]
    data["cart"] = cart
    data["product_type"] = item["product_name"] if len(cart) == 1 else "; ".join(
        f"{r['product_name']} × {r['quantity']}" for r in cart
    )
    data["quantity"] = str(sum(int(r.get("quantity") or 0) for r in cart))
    data["total_amount"] = round(sum(float(r.get("total_amount") or 0) for r in cart), 2)
    data["unit_price"] = item["unit_price"] if len(cart) == 1 else None
    if item.get("size_mm"):
        data["size_mm"] = item["size_mm"]
    data["quote_ready"] = True
    return cart


def _format_quote_reply(result: dict, data: dict[str, Any]) -> str:
    cart = list(data.get("cart") or [])
    return format_quote_message(result, with_confirm=True, cart=cart if len(cart) > 1 else None)


def _format_payment_message(data: dict[str, Any], url: str) -> str:
    cart = list(data.get("cart") or [])
    lines = ["Оплата онлайн через сервіс LiqPay:"]
    if cart:
        for row in cart:
            lines.append(
                f"{row.get('product_name')} × {row.get('quantity')} шт — "
                f"{format_money(row.get('total_amount'))} грн"
            )
        if len(cart) > 1:
            lines.append(f"Разом: {format_money(data.get('total_amount'))} грн")
    else:
        product = data.get("product_type") or "Замовлення"
        qty = data.get("quantity")
        total = format_money(data.get("total_amount"))
        qty_bit = f" × {qty} шт" if qty else ""
        lines.append(f"{product}{qty_bit} — {total} грн")
    lines.extend(
        [
            "",
            f"Посилання на оплату: {url}",
            "",
            "Після оплати повідомте мене, що ви оплатили.",
            "Дякую!",
        ]
    )
    return "\n".join(lines)


def _format_profile_confirm(profile: dict[str, Any]) -> str:
    lines = ["Бачу ваші дані з минулого разу:"]
    name_bits = [
        _normalize_words(profile["first_name"]) if profile.get("first_name") else None,
        _normalize_words(profile["last_name"]) if profile.get("last_name") else None,
    ]
    uniq = []
    for x in name_bits:
        if x and x not in uniq:
            uniq.append(x)
    if uniq:
        lines.append(f"- Ім'я та прізвище: {' '.join(uniq)}")
    if profile.get("phone"):
        lines.append(f"- Телефон: {profile['phone']}")
    if profile.get("city"):
        lines.append(f"- Місто: {_normalize_words(str(profile['city']))}")
    dtype = profile.get("delivery_type")
    if dtype == DELIVERY_TYPE_ADDRESS and profile.get("street_address"):
        addr = _normalize_words(str(profile["street_address"]))
        lines.append(f"- Доставка: Адреса — {addr}")
    elif profile.get("warehouse"):
        where = "Поштомат" if dtype == DELIVERY_TYPE_POSTOMAT else "Відділення"
        lines.append(f"- Доставка: {where} № {profile['warehouse']} Нової пошти")
    lines.append("")
    lines.append("Вони актуальні?")
    return "\n".join(lines)


def _apply_profile_to_data(data: dict[str, Any], profile: dict[str, Any]) -> None:
    for key in ("last_name", "first_name", "phone", "city"):
        if profile.get(key):
            data[key] = profile[key]
    if profile.get("delivery_type"):
        data["saved_delivery_type"] = profile["delivery_type"]
    if profile.get("warehouse"):
        data["saved_warehouse"] = profile["warehouse"]
    if profile.get("street_address"):
        data["saved_street_address"] = profile["street_address"]
    if profile.get("recipient_patronymic"):
        data["saved_patronymic"] = profile["recipient_patronymic"]


async def _begin_order_collection(
    db: AsyncSession, state: ConversationState, data: dict, chat_id: str
) -> ChatMessageResponse:
    """Старт оформлення: підтвердити старі дані або збирати нові."""
    data.pop("quote_ready", None)
    # товар/кількість з поточної розмови лишаємо; контакти — підтверджуємо з профілю
    profile = await _load_saved_profile(db, chat_id)
    if profile.get("last_name") and profile.get("phone"):
        data["_profile"] = profile
        state.step = "confirm_crm"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=_format_profile_confirm(profile),
            action="confirm_client_data",
        )

    state.step = "collect_crm"
    _save_data(state, data)
    await db.commit()
    missing = _missing_crm_fields(data)
    if missing:
        return ChatMessageResponse(
            response=_next_crm_question(data) or ORDER_PROMPT,
            action="collect_order_data",
        )
    return ChatMessageResponse(response=ORDER_PROMPT, action="collect_order_data")


def _next_crm_question(data: dict[str, Any]) -> str:
    missing = _missing_crm_fields(data)
    if not missing:
        return ""
    key = missing[0]
    if key == "quantity":
        prod = data.get("product_type") or "товару"
        return f"Яка кількість потрібна для «{prod}»? Напишіть число, наприклад: 100"
    return CRM_FIELD_QUESTIONS.get(key, f"Уточніть: {key}")


def _looks_like_question(text: str) -> bool:
    t = text.strip().lower()
    if "?" in t:
        return True
    starters = (
        "а які", "які є", "що є", "що у вас", "які у вас", "покажи", "розкажи",
        "хочу прорахувати", "хочу вибрати", "хочу подивитись", "які товари",
        "що пропонуєте", "які браслети", "який асортимент",
    )
    return any(t.startswith(s) or s in t for s in starters)


def _wants_consult_not_order(text: str) -> bool:
    lower = text.lower()
    consult_signals = (
        "прорахувати", "розрахувати", "вибрати", "підібрати", "які є",
        "що є", "асортимент", "каталог", "прайс", "ціна", "коштує",
        "які у вас", "що у вас", "консультац",
    )
    order_only = (
        "оформити замовлення",
        "оформлюємо замовлення",
        "хочу оформити",
        "готовий замовити",
        "оформлюйте",
        "беріть замовлення",
    )
    if any(s in lower for s in order_only):
        return False
    if any(s in lower for s in consult_signals):
        return True
    # «прорахувати та замовити» — спочатку консультація
    if "замовити" in lower and any(s in lower for s in ("прораху", "вибра", "підібр", "товар")):
        return True
    return False


def _is_order_confirm(text: str) -> bool:
    lower = text.lower().strip()
    if any(
        n in lower
        for n in ("не треба", "не хочу", "не зараз", "пізніше", "ще подумаю")
    ):
        return False
    if lower in {
        "так",
        "да",
        "yes",
        "y",
        "ок",
        "ok",
        "добре",
        "давайте",
        "оформлюємо",
        "оформлюй",
        "замовити",
        "хочу замовити",
        "оформити",
        "оформити замовлення",
        "+",
    }:
        return True
    # коротке «так/да, …» без нового товару/кількості (не «Так, я вище писав силіконові…»)
    if (lower.startswith("так,") or lower.startswith("да,")) and len(lower) <= 48:
        if _extract_product_hint(lower) or _extract_qty(lower) or _is_flow_aside(lower):
            return False
        if any(w in lower for w in ("вище", "писав", "силікон", "світловідб", "браслет", "штук")):
            return False
        return True
    return any(
        p in lower
        for p in (
            "оформлюємо",
            "оформимо",
            "давайте оформи",
            "хочу оформити",
            "готовий замовити",
            "беру",
            "замовляю",
        )
    )


def _strip_echo(reply: str, user_text: str) -> str:
    """Прибрати повторення відповіді клієнта з початку відповіді бота."""
    if not reply or not user_text:
        return reply
    r = reply.strip()
    u = user_text.strip()
    patterns = [
        f'Ви написали: "{u}"',
        f"Ви написали: «{u}»",
        f'Ви сказали: "{u}"',
        f"Ви сказали: «{u}»",
        f'"{u}"',
        f"«{u}»",
        u,
    ]
    for p in patterns:
        if r.lower().startswith(p.lower()):
            r = r[len(p):].lstrip(" \n.,:;!-—")
            break
    lines = r.splitlines()
    if lines and lines[0].strip().lower() == u.lower():
        r = "\n".join(lines[1:]).strip()
    return r or reply


def _is_explicit_order_start(text: str) -> bool:
    lower = text.lower().strip()
    if _wants_consult_not_order(text):
        return False
    exact = {
        "замовити",
        "хочу замовити",
        "оформити замовлення",
        "оформлюємо",
        "оформлюємо замовлення",
        "готовий замовити",
        "оформлюйте",
    }
    if lower in exact:
        return True
    return any(
        p in lower
        for p in (
            "оформити замовлення",
            "хочу замовити",
            "хочу оформити замовлення",
            "давайте оформимо",
            "оформлюємо замовлення",
        )
    )


def _parse_person_name(text: str) -> tuple[str | None, str | None]:
    """Не змінюємо написання клієнта."""
    return None, None


def _extract_warehouse_number(text: str) -> str:
    m = re.search(r"(\d+)\s*(?:відділен|отделен|post|п)?", text.lower())
    if m:
        return m.group(1)
    digits = re.findall(r"\d+", text)
    return digits[0] if digits else text.strip()


def _extract_city(text: str) -> str | None:
    lower = (text or "").lower()
    cities = {
        "київ": "Київ",
        "киев": "Київ",
        "львів": "Львів",
        "львов": "Львів",
        "харків": "Харків",
        "харьков": "Харків",
        "одеса": "Одеса",
        "одесса": "Одеса",
        "дніпро": "Дніпро",
        "днепр": "Дніпро",
        "запоріжжя": "Запоріжжя",
        "вінниця": "Вінниця",
        "полтава": "Полтава",
        "чернігів": "Чернігів",
        "черкаси": "Черкаси",
        "суми": "Суми",
        "миколаїв": "Миколаїв",
        "хмельницький": "Хмельницький",
        "рівне": "Рівне",
        "івано-франківськ": "Івано-Франківськ",
        "тернопіль": "Тернопіль",
        "луцьк": "Луцьк",
        "ужгород": "Ужгород",
        "кривий ріг": "Кривий Ріг",
    }
    for key, name in cities.items():
        if key in lower:
            return name
    m = re.search(r"(?:місто|город)\s+([А-Яа-яA-Za-zІіЇїЄєҐґ\-]+)", text or "", re.I)
    if m:
        return m.group(1).strip().capitalize()
    return None


def _extract_total_uah(text: str) -> float | None:
    """Витягнути «разом 36 000 грн» / «17000 грн»."""
    lower = (text or "").lower().replace("\u00a0", " ")
    patterns = (
        r"(?:разом|загалом|сума|всього)\s*[:\-]?\s*([\d\s]+(?:[.,]\d+)?)\s*грн",
        r"([\d\s]+(?:[.,]\d+)?)\s*грн\s*(?:разом|загалом)",
    )
    for p in patterns:
        m = re.search(p, lower)
        if m:
            raw = re.sub(r"\s+", "", m.group(1)).replace(",", ".")
            try:
                return float(raw)
            except ValueError:
                continue
    return None


def _is_flow_aside(text: str) -> bool:
    """Коментар/уточнення серед сценарію, не відповідь на поле."""
    lower = text.lower().strip()
    markers = (
        "ви вже",
        "ти вже",
        "вже прорах",
        "вже порахув",
        "вище сум",
        "я вже",
        "все надав",
        "вже все",
        "вже дав",
        "зачекай",
        "почекай",
        "навіщо ще",
        "нащо ще",
        "я ж написав",
        "я ж сказав",
    )
    return any(m in lower for m in markers)


def _has_saved_delivery(data: dict[str, Any]) -> bool:
    city = data.get("city")
    if data.get("saved_street_address") and city:
        return True
    if data.get("saved_warehouse") and city:
        return True
    return False


def _apply_crm_answer(data: dict[str, Any], text: str) -> None:
    """Спочатку key:value, інакше — відповідь на наступне порожнє поле (як написав клієнт)."""
    parsed = _parse_kv(text)
    data.update({k: v for k, v in parsed.items() if v})
    if parsed.get("first_name") and parsed.get("last_name"):
        pass
    elif parsed.get("first_name") and not data.get("last_name"):
        data["last_name"] = parsed["first_name"]
    _merge_order_hints(data, text)
    missing = _missing_crm_fields(data)
    if not missing:
        return
    if parsed and "person_name" not in missing:
        return
    if not text.strip():
        return

    key = missing[0]
    val = text.strip()

    if key == "email" or val.lower() in (
        "не потрібен",
        "не потрібний",
        "не треба",
        "немає",
        "нет",
        "no",
        "-",
        "пропустити",
    ):
        if key == "email" or "email" in missing:
            data["email"] = ""
            if key == "email":
                return

    if key == "product_type" and _looks_like_question(val):
        data["_abort_to_consult"] = val
        return

    if key == "person_name":
        fn, ln = _split_person_name(val)
        if fn:
            data["first_name"] = fn
            data["last_name"] = ln or fn
        return

    if key == "quantity":
        q = _extract_qty(val)
        if not q:
            digits = re.search(r"\b(\d{1,6})\b", val)
            if digits and not _looks_like_question(val) and len(val.strip()) < 40:
                q = digits.group(1)
        if q:
            data["quantity"] = q
        return

    if key == "product_type":
        hint = _extract_product_hint(val)
        data["product_type"] = hint or val
        return

    # зберігаємо РІВНО як написав клієнт
    data[key] = val


def _delivery_fields_needed(delivery_type: str) -> list[str]:
    base = ["recipient_last_name", "recipient_first_name", "recipient_phone", "city"]
    if delivery_type == DELIVERY_TYPE_ADDRESS:
        return base + ["recipient_patronymic", "street_address"]
    return base + ["warehouse"]


def _delivery_value(data: dict[str, Any], delivery: dict[str, Any], key: str) -> str | None:
    if delivery.get(key):
        return delivery.get(key)
    aliases = {
        "recipient_last_name": "last_name",
        "recipient_first_name": "first_name",
        "recipient_phone": "phone",
        "city": "city",
    }
    alias = aliases.get(key)
    if alias and data.get(alias):
        return data.get(alias)
    return None


def _next_delivery_question(data: dict[str, Any], delivery: dict[str, Any], delivery_type: str) -> str | None:
    needed = _delivery_fields_needed(delivery_type)
    questions = dict(DELIVERY_FIELD_ORDER)
    for key in needed:
        val = _delivery_value(data, delivery, key)
        if val:
            delivery[key] = val
            continue
        return questions.get(key, f"Уточніть: {key}")
    return None


def _apply_delivery_answer(delivery: dict[str, Any], data: dict[str, Any], delivery_type: str, text: str) -> None:
    parsed = _parse_kv(text)
    if parsed.get("last_name"):
        delivery["recipient_last_name"] = parsed["last_name"]
    if parsed.get("first_name"):
        delivery["recipient_first_name"] = parsed["first_name"]
    if parsed.get("patronymic"):
        delivery["recipient_patronymic"] = parsed["patronymic"]
    if parsed.get("phone"):
        delivery["recipient_phone"] = parsed["phone"]
    if parsed.get("city"):
        delivery["city"] = _normalize_words(parsed["city"])
        data["city"] = delivery["city"]
    if parsed.get("warehouse"):
        delivery["warehouse"] = parsed["warehouse"]
    if parsed.get("street_address"):
        delivery["street_address"] = parsed["street_address"]

    # з вільного тексту: «45 відділення Київ»
    city_hint = _extract_city(text)
    if city_hint:
        delivery["city"] = _normalize_words(city_hint)
        data["city"] = delivery["city"]
    if re.search(r"\d+", text) and (
        "відділен" in text.lower()
        or "поштомат" in text.lower()
        or delivery_type in (DELIVERY_TYPE_WAREHOUSE, DELIVERY_TYPE_POSTOMAT)
    ):
        wh = _extract_warehouse_number(text)
        if wh and wh.isdigit():
            delivery["warehouse"] = wh

    # підтягнути вже відомі CRM-поля
    for key in _delivery_fields_needed(delivery_type):
        val = _delivery_value(data, delivery, key)
        if val:
            delivery[key] = val

    if parsed:
        return

    for key in _delivery_fields_needed(delivery_type):
        if delivery.get(key):
            continue
        raw = text.strip()
        if key == "warehouse":
            delivery[key] = _extract_warehouse_number(raw)
        elif key == "city":
            city = _extract_city(raw)
            delivery[key] = _normalize_words(city or raw)
            data["city"] = delivery[key]
        else:
            delivery[key] = raw
        break


async def process_message(db: AsyncSession, chat_id: str, text: str) -> ChatMessageResponse:
    text = text.strip()
    lower = text.lower()
    state = await _get_state(db, chat_id)
    data = _load_data(state)
    # запам'ятовуємо товар/кількість з будь-якого повідомлення (як написав клієнт)
    _merge_order_hints(data, text)
    _save_data(state, data)
    await db.commit()

    if lower in ("/start", "старт", "привіт", "вітаю", "hello", "hi"):
        state.step = "idle"
        _save_data(state, {})
        await db.commit()
        return ChatMessageResponse(response=GREETING, action="greeting")

    if lower in ("скасувати", "відміна", "/cancel", "скасувати замовлення"):
        state.step = "idle"
        _save_data(state, {})
        await db.commit()
        return ChatMessageResponse(
            response="Добре, оформлення скасовано. Чим ще можу допомогти?",
            action="cancelled",
        )

    # --- Запит менеджера ---
    if any(
        w in lower
        for w in (
            "менеджер",
            "зв'язати",
            "звязати",
            "живий консультант",
            "оператор",
            "людина",
            "call me",
            "переключ",
        )
    ):
        ticket = await chat_history.create_manager_ticket(db, state.chat_id, text)
        return ChatMessageResponse(
            response=MANAGER_WAIT_REPLY,
            action="manager_handoff",
            data={"ticket_id": ticket.id},
        )

    # Клієнт хоче повернутись до підбору/прорахунку — виходимо зі сценарію
    scenario_steps = {
        "collect_crm",
        "collect_delivery_type",
        "collect_delivery",
        "collect_payment",
        "collect_invoice",
        "lookup_edrpou",
    }
    if state.step in scenario_steps and _wants_consult_not_order(text):
        state.step = "idle"
        _save_data(state, data)
        await db.commit()
        result = await consultant.consult(db, text, chat_id)
        if result.get("escalate_to_manager"):
            ticket = await chat_history.create_manager_ticket(db, chat_id, text)
            return ChatMessageResponse(
                response=result.get("response") or MANAGER_WAIT_REPLY,
                action="manager_handoff",
                data={"ticket_id": ticket.id},
            )
        return ChatMessageResponse(response=result["response"], action="consult")

    # --- Active scenario steps ---
    if state.step == "confirm_crm":
        if _is_order_confirm(text) or lower in ("актуально", "ті самі", "ті ж", "так само"):
            profile = data.pop("_profile", {}) or {}
            _apply_profile_to_data(data, profile)
            state.step = "collect_crm"
            _save_data(state, data)
            await db.commit()
            missing = _missing_crm_fields(data)
            if missing:
                return ChatMessageResponse(
                    response=_next_crm_question(data),
                    action="collect_order_data",
                )
            # все є — одразу в CRM step щоб створити order (порожнє повідомлення не підходить)
            return await _step_collect_crm(db, state, data, text=".")
        if any(w in lower for w in ("ні", "нет", "no", "не актуаль", "нові", "змінил")):
            data.pop("_profile", None)
            for k in ("last_name", "first_name", "phone", "city", "email"):
                data.pop(k, None)
            for k in (
                "saved_delivery_type",
                "saved_warehouse",
                "saved_street_address",
                "saved_patronymic",
            ):
                data.pop(k, None)
            state.step = "collect_crm"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response="Добре, введемо нові. Напишіть ім'я та прізвище одним повідомленням.",
                action="collect_order_data",
            )
        return ChatMessageResponse(
            response="Напишіть «так», якщо дані актуальні, або «ні» — щоб ввести нові.",
            action="confirm_client_data",
        )

    if state.step == "await_order_confirm":
        if _is_order_confirm(text) or _is_explicit_order_start(text):
            return await _begin_order_collection(db, state, data, chat_id)
        # новий товар/тираж замість «так» — перераховуємо, не зациклюємось на «Оформлюємо?»
        new_product = _extract_product_hint(text)
        new_qty = _extract_qty(text)
        if new_product or new_qty or _wants_consult_not_order(text) or any(
            w in lower for w in ("ні", "нет", "no", "пізніше", "ще подумаю", "інш", "ще ")
        ):
            state.step = "idle"
            _save_data(state, data)
            await db.commit()
            if any(w in lower for w in ("ні", "нет", "no", "пізніше", "ще подумаю")) and not (
                new_product or new_qty
            ):
                return ChatMessageResponse(
                    response="Добре. Можу ще допомогти з підбором або прорахунком — що цікавить?",
                    action="consult",
                )
            # перейти в idle-гілку нижче (прорахунок / консультація)
            return await process_message(db, chat_id, text)
        return ChatMessageResponse(
            response=ORDER_CONFIRM_PROMPT,
            action="await_order_confirm",
        )

    if state.step == "collect_crm":
        return await _step_collect_crm(db, state, data, text)
    if state.step == "collect_delivery_type":
        return await _step_delivery_type(db, state, data, text)
    if state.step == "collect_delivery":
        return await _step_collect_delivery(db, state, data, text)
    if state.step == "collect_payment":
        return await _step_collect_payment(db, state, data, text)
    if state.step == "collect_invoice":
        return await _step_collect_invoice(db, state, data, text)
    if state.step == "lookup_edrpou":
        return await _step_lookup_edrpou(db, state, data, text)

    # --- Intent: start order (лише явний старт, не «прорахувати та замовити») ---
    if _is_explicit_order_start(text) or (
        data.get("quote_ready") and _is_order_confirm(text)
    ):
        return await _begin_order_collection(db, state, data, chat_id)

    # --- ЄДРПОУ / контрагент 1С / рахунок на реквізити ---
    edrpou_intent = await _maybe_handle_edrpou_invoice(db, state, data, text, lower)
    if edrpou_intent:
        return edrpou_intent

    # --- Кілька товарів в одному повідомленні (силікон + світловідбиваючі тощо) ---
    order_lines = _extract_order_lines(text)
    if len(order_lines) >= 2:
        multi = _build_cart_from_lines(data, order_lines)
        if multi:
            state.step = "await_order_confirm"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=_format_quote_reply(multi, data),
                action="await_order_confirm",
                data=multi,
            )

    # --- Pricing shortcut ---
    qty_match = re.search(r"(\d+)\s*шт", lower) or (
        re.search(r"\b(\d+)\b", lower) if any(w in lower for w in ["ціна", "коштує", "прорахун", "розрахун", "прайс", "браслет"]) else None
    )
    size_match = re.search(r"(\d+)\s*мм", lower)
    if any(w in lower for w in ["ціна", "коштує", "прорахун", "розрахун", "прайс"]) and qty_match:
        hint = _extract_product_hint(text)
        if hint and _is_reflective_text(hint):
            product_type = "світловідбиваючий"
        elif hint and "силікон" in hint.lower():
            product_type = "силіконовий"
        elif _is_reflective_text(lower):
            product_type = "світловідбиваючий"
        elif _is_reflective_text(str(data.get("product_type") or "")):
            product_type = "світловідбиваючий"
        else:
            product_type = str(data.get("product_type") or "силіконовий")
        result = calculator.calculate(
            product_type=product_type,
            quantity=int(qty_match.group(1)),
            size_mm=size_match.group(1) if size_match else None,
        )
        if result.get("success"):
            if size_match:
                data["size_mm"] = size_match.group(1)
            elif result.get("product_key"):
                m = re.search(r"(\d+)mm", str(result["product_key"]))
                if m:
                    data["size_mm"] = m.group(1)
            _apply_quote_to_cart(data, result, text)
            state.step = "await_order_confirm"
            _save_data(state, data)
            await db.commit()
            msg = _format_quote_reply(result, data)
            return ChatMessageResponse(response=msg, action="await_order_confirm", data=result)
        return ChatMessageResponse(response=result.get("error", "Не вдалося розрахувати."))

    # Не ловити «відділення» / «нова пошта» як старт доставки під час звичайної розмови —
    # лише явні фрази про оформлення доставки.
    if any(
        p in lower
        for p in (
            "оформити доставку",
            "дані для доставки",
            "хочу доставку",
            "створити ттн",
        )
    ):
        state.step = "collect_delivery_type"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=DELIVERY_TYPE_PROMPT,
            action="collect_delivery_data",
        )

    if any(w in lower for w in ["оплатити", "liqpay", "лікпей", "лиг", "ліг", "посилання на оплату"]):
        order_id = data.get("order_id")
        if order_id and data.get("total_amount"):
            pay = await order_service.create_payment(db, int(order_id))
            if pay.get("success") and pay.get("payment_url"):
                return ChatMessageResponse(
                    response=_format_payment_message(data, pay["payment_url"]),
                    action="payment_created",
                    data=pay,
                )
        return ChatMessageResponse(
            response=(
                "Оплата в нас онлайн через LiqPay. "
                "Спершу оформлюємо замовлення — напишіть «замовити», і я все зберу."
            ),
            action="payment_info",
        )

    # «1000 штук» / «силіконові 100 штук» — рахуємо самі (товар з повідомлення важливіший за старий стан)
    qty_only = _extract_qty(text)
    hint_now = _extract_product_hint(text) or data.get("pending_product_type")
    if isinstance(hint_now, str):
        hint_now = hint_now
    else:
        hint_now = None
    cart_names = " ".join(str(r.get("product_name") or "") for r in (data.get("cart") or [])).lower()
    same_in_cart = bool(hint_now) and (
        ("силікон" in hint_now.lower() and "силікон" in cart_names)
        or (_is_reflective_text(hint_now) and _is_reflective_text(cart_names))
        or hint_now.lower() in cart_names
    )
    adding_line = bool(data.get("cart")) and bool(hint_now) and (
        _wants_add_to_cart(text) or bool(data.get("pending_product_type")) or not same_in_cart
    )
    # виправлення («я вище писав силіконові…») — замінити кошик, не додавати
    if hint_now and any(w in lower for w in ("вище", "писав", "не те", "неправильно", "помилк")):
        adding_line = False

    if hint_now and not adding_line:
        _apply_product_hint(data, hint_now, clear_cart=True)
        if qty_only:
            data["quantity"] = qty_only
    elif qty_only:
        data["quantity"] = qty_only

    if qty_only and (data.get("product_type") or hint_now or data.get("cart")) and not any(
        w in lower for w in ("ціна", "коштує", "прорахун", "прайс")
    ):
        size_mm = data.get("size_mm")
        size_m = re.search(r"(\d+)\s*мм", lower)
        if size_m:
            size_mm = size_m.group(1)
            data["size_mm"] = size_mm

        if hint_now and _is_reflective_text(str(hint_now)):
            product_for_calc = "світловідбиваючий"
            size_mm = None
        elif hint_now and "силікон" in str(hint_now).lower():
            product_for_calc = "силіконовий"
        elif _is_reflective_text(lower) and not hint_now:
            product_for_calc = "світловідбиваючий"
            size_mm = None
        elif _is_reflective_text(str(data.get("product_type") or "")) and not hint_now:
            product_for_calc = "світловідбиваючий"
            size_mm = None
        else:
            product_for_calc = str(hint_now or data.get("product_type") or "силіконовий")
            if "силікон" in product_for_calc.lower():
                product_for_calc = "силіконовий"

        calc = calculator.calculate(
            product_type=product_for_calc,
            quantity=int(qty_only),
            size_mm=str(size_mm) if size_mm else None,
        )
        if calc.get("success"):
            if hint_now and not adding_line:
                data.pop("cart", None)
            cart_text = text if (adding_line or _wants_add_to_cart(text)) else text
            if adding_line and not _wants_add_to_cart(cart_text):
                cart_text = f"ще {text}"
            _apply_quote_to_cart(data, calc, cart_text)
            data.pop("pending_product_type", None)
            state.step = "await_order_confirm"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=_format_quote_reply(calc, data),
                action="await_order_confirm",
                data=calc,
            )

    result = await consultant.consult(db, text, chat_id)
    reply = _strip_echo(result.get("response") or "", text)
    if result.get("escalate_to_manager"):
        ticket = await chat_history.create_manager_ticket(db, state.chat_id, text)
        return ChatMessageResponse(
            response=reply or MANAGER_WAIT_REPLY,
            action="manager_handoff",
            data={"ticket_id": ticket.id},
        )

    # огляд асортименту/прайсу — не плутати з конкретним прорахунком
    if result.get("source") == "assortment":
        return ChatMessageResponse(response=reply, action="consult")

    # детермінований / AI прорахунок з прайсу
    pricing = result.get("pricing")
    if pricing and pricing.get("success"):
        if pricing.get("product_key"):
            m = re.search(r"(\d+)mm", str(pricing["product_key"]))
            if m:
                data["size_mm"] = m.group(1)
        _apply_quote_to_cart(data, pricing, text)
        state.step = "await_order_confirm"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=_format_quote_reply(pricing, data),
            action="await_order_confirm",
            data=pricing,
        )

    # після конкретного прорахунку в тексті — завжди вмикаємо сценарій оформлення
    lower_reply = reply.lower()
    is_catalog_dump = bool(
        (
            re.search(r"(?m)^\s*1[\.\)]\s+", reply)
            and re.search(r"(?m)^\s*[23][\.\)]\s+", reply)
        )
        or "основний асортимент" in lower_reply
        or ("асортимент" in lower_reply and "орієнтовн" in lower_reply)
    )
    price_hits = len(re.findall(r"\d+[.,]\d+\s*грн|\d+\s*грн", lower_reply))
    looks_like_quote = (
        not is_catalog_dump
        and price_hits <= 4
        and any(w in lower_reply for w in ("грн", "ціна", "сума", "кошт"))
        and (
            data.get("quantity")
            or any(w in lower_reply for w in ("шт", "штук", "тираж"))
        )
    )
    if looks_like_quote:
        qty = re.search(r"(\d+)\s*шт", lower) or re.search(r"(\d+)\s*шт", lower_reply)
        if qty and not data.get("quantity"):
            data["quantity"] = qty.group(1)
        size_m = re.search(r"(\d+)\s*мм", lower) or re.search(r"(\d+)\s*мм", lower_reply)
        if size_m:
            data["size_mm"] = size_m.group(1)
        extracted = _extract_total_uah(reply)
        if extracted:
            data["total_amount"] = extracted
        # якщо товар відомий — перерахувати і зберегти точну суму з прайсу
        if data.get("product_type") and data.get("quantity"):
            try:
                qn = int(re.sub(r"\D", "", str(data["quantity"])) or "0")
            except ValueError:
                qn = 0
            if qn:
                recalc = calculator.calculate(
                    product_type=str(data["product_type"]),
                    quantity=qn,
                    size_mm=str(data["size_mm"]) if data.get("size_mm") else None,
                )
                if recalc.get("success"):
                    _apply_quote_to_cart(data, recalc, text)
                    reply = _format_quote_reply(recalc, data)
        _merge_order_hints(data, text)
        data["quote_ready"] = True
        state.step = "await_order_confirm"
        _save_data(state, data)
        await db.commit()
        if "можемо оформлювати" not in reply.lower() and "все підходить" not in reply.lower():
            reply = f"{reply.rstrip()}\n\n{ORDER_CONFIRM_PROMPT}"
        return ChatMessageResponse(response=reply, action="await_order_confirm")

    _save_data(state, data)
    await db.commit()
    return ChatMessageResponse(response=reply, action="consult")


async def _step_collect_crm(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    _apply_crm_answer(data, text)
    if data.pop("_abort_to_consult", None):
        state.step = "idle"
        _save_data(state, data)
        await db.commit()
        result = await consultant.consult(db, text, state.chat_id)
        if result.get("escalate_to_manager"):
            ticket = await chat_history.create_manager_ticket(db, state.chat_id, text)
            return ChatMessageResponse(
                response=result.get("response") or MANAGER_WAIT_REPLY,
                action="manager_handoff",
                data={"ticket_id": ticket.id},
            )
        return ChatMessageResponse(response=result["response"], action="consult")

    missing = _missing_crm_fields(data)
    if missing:
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=_next_crm_question(data),
            action="collect_order_data",
            data={"missing": missing, "collected": data},
        )

    try:
        qty = int(re.search(r"\d+", str(data["quantity"])).group())  # type: ignore[union-attr]
    except (AttributeError, ValueError, TypeError):
        data.pop("quantity", None)
        _save_data(state, data)
        await db.commit()
        prod = data.get("product_type") or "товару"
        return ChatMessageResponse(
            response=f"Потрібно число штук для «{prod}». Наприклад: 100 або 300",
            action="collect_order_data",
        )

    if data.get("city"):
        data["city"] = _normalize_words(str(data["city"]))
    data["first_name"] = _normalize_words(str(data["first_name"]))
    data["last_name"] = _normalize_words(str(data["last_name"]))

    cart = list(data.get("cart") or [])
    size_mm = data.get("size_mm")
    if not size_mm:
        m = re.search(r"(\d+)\s*мм", str(data.get("product_type") or ""))
        if m:
            size_mm = m.group(1)

    if not cart:
        pricing = calculator.calculate(
            product_type=str(data["product_type"]),
            quantity=qty,
            size_mm=str(size_mm) if size_mm else None,
        )
        if not pricing.get("success") and "силікон" in str(data["product_type"]).lower():
            pricing = calculator.calculate(
                product_type=str(data["product_type"]),
                quantity=qty,
                size_mm=str(size_mm or "12"),
            )
        if pricing.get("success"):
            _apply_quote_to_cart(data, pricing, "")
            cart = list(data.get("cart") or [])

    if cart:
        product_label = "; ".join(f"{r['product_name']} × {r['quantity']}" for r in cart)
        qty = sum(int(r.get("quantity") or 0) for r in cart)
        total = round(sum(float(r.get("total_amount") or 0) for r in cart), 2)
        unit_price = cart[0].get("unit_price") if len(cart) == 1 else None
        days = max((int(r.get("production_days") or 0) for r in cart), default=None) or None
    else:
        product_label = data.get("product_type")
        unit_price = data.get("unit_price")
        total = data.get("total_amount")
        days = None
        if data.get("total_amount"):
            try:
                total = float(data["total_amount"])
            except (TypeError, ValueError):
                pass

    client_payload = {
        "first_name": data["first_name"],
        "last_name": data["last_name"],
        "phone": data["phone"],
        "email": data.get("email") or None,
        "city": data.get("city") or None,
        "channel": "telegram",
        "order": {
            "title": product_label,
            "product_type": product_label,
            "quantity": qty,
            "comment": data.get("comment") or data.get("logo"),
        },
    }
    reg = await order_service.register_client_in_crm(db, state.chat_id, client_payload)
    data["product_type"] = product_label

    order = await order_service.create_order(
        db,
        state.chat_id,
        {
            "product_type": product_label,
            "quantity": qty,
            "unit_price": unit_price,
            "total_amount": total,
            "production_days": days,
            "comment": data.get("comment"),
            "params": {"logo": data.get("logo"), "size_mm": size_mm, "cart": cart},
            "client": client_payload,
        },
    )
    data["order_id"] = order.id
    data["client_id"] = reg.get("client_id")
    data["total_amount"] = total
    data["unit_price"] = unit_price

    # якщо доставка вже підтверджена з профілю — одразу LiqPay
    if _has_saved_delivery(data):
        dtype = data.get("saved_delivery_type") or DELIVERY_TYPE_WAREHOUSE
        data["delivery_type"] = dtype
        draft: dict[str, Any] = {}
        if data.get("saved_warehouse"):
            draft["warehouse"] = data["saved_warehouse"]
        if data.get("saved_street_address"):
            draft["street_address"] = data["saved_street_address"]
        if data.get("saved_patronymic"):
            draft["recipient_patronymic"] = data["saved_patronymic"]
        data["delivery_draft"] = draft
        state.step = "collect_delivery"
        _save_data(state, data)
        await db.commit()
        return await _step_collect_delivery(db, state, data, text="ок")

    state.step = "collect_delivery_type"
    _save_data(state, data)
    await db.commit()

    price_note = ""
    if total:
        price_note = f" Разом {total:g} грн — ця сума піде в оплату."
    else:
        price_note = ""

    return ChatMessageResponse(
        response=f"Ок.{price_note}\n\n{DELIVERY_TYPE_PROMPT}",
        action="collect_delivery_data",
        data={"order_id": order.id, "crm": reg},
    )


async def _step_delivery_type(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    lower = text.lower().strip()

    # клієнт каже «я вже все дав» / «ви вже порахували»
    if _is_flow_aside(text):
        if _has_saved_delivery(data) and data.get("order_id"):
            dtype = data.get("saved_delivery_type") or DELIVERY_TYPE_WAREHOUSE
            data["delivery_type"] = dtype
            draft: dict[str, Any] = {}
            if data.get("saved_warehouse"):
                draft["warehouse"] = data["saved_warehouse"]
            if data.get("saved_street_address"):
                draft["street_address"] = data["saved_street_address"]
            data["delivery_draft"] = draft
            state.step = "collect_delivery"
            _save_data(state, data)
            await db.commit()
            return await _step_collect_delivery(db, state, data, text="ок")
        if data.get("total_amount"):
            return ChatMessageResponse(
                response=(
                    f"Так, сума {format_money(data['total_amount'])} грн уже зафіксована. "
                    f"{DELIVERY_TYPE_PROMPT}"
                ),
                action="collect_delivery_data",
            )
        return ChatMessageResponse(
            response=f"Зрозумів. {DELIVERY_TYPE_PROMPT}",
            action="collect_delivery_data",
        )

    city_hint = _extract_city(text)
    if city_hint:
        data["city"] = _normalize_words(city_hint)
    wh_hint = None
    # не плутати вибір меню «1/2/3» з номером відділення
    if lower not in ("1", "2", "3") and re.search(r"\d+", lower):
        wh_hint = _extract_warehouse_number(text)

    if lower in ("1", "відділення", "отделение", "warehouse") or "відділен" in lower:
        dtype = DELIVERY_TYPE_WAREHOUSE
    elif lower in ("2", "поштомат", "postomat") or "поштомат" in lower:
        dtype = DELIVERY_TYPE_POSTOMAT
    elif (
        lower in ("3", "адресна", "адресна доставка", "адреса", "address")
        or "адрес" in lower
    ):
        dtype = DELIVERY_TYPE_ADDRESS
    elif "нов" in lower and "пошт" in lower:
        return ChatMessageResponse(
            response="Зрозумів, Нова пошта. На відділення, у поштомат чи за адресою?",
            action="collect_delivery_data",
        )
    elif wh_hint and ("відділен" in lower or "поштомат" in lower or city_hint):
        dtype = DELIVERY_TYPE_POSTOMAT if "поштомат" in lower else DELIVERY_TYPE_WAREHOUSE
    else:
        return ChatMessageResponse(
            response="На відділення, у поштомат чи за адресою зручніше?",
            action="collect_delivery_data",
        )

    data["delivery_type"] = dtype
    data["delivery_draft"] = {}
    draft = {}
    if data.get("saved_warehouse") and dtype in (DELIVERY_TYPE_WAREHOUSE, DELIVERY_TYPE_POSTOMAT):
        draft["warehouse"] = data["saved_warehouse"]
    if wh_hint and dtype in (DELIVERY_TYPE_WAREHOUSE, DELIVERY_TYPE_POSTOMAT):
        draft["warehouse"] = wh_hint
        data["saved_warehouse"] = wh_hint
    if data.get("saved_street_address") and dtype == DELIVERY_TYPE_ADDRESS:
        draft["street_address"] = data["saved_street_address"]
        if data.get("saved_patronymic"):
            draft["recipient_patronymic"] = data["saved_patronymic"]
    state.step = "collect_delivery"
    q = _next_delivery_question(data, draft, dtype)
    data["delivery_draft"] = draft
    _save_data(state, data)
    await db.commit()
    if not q:
        return await _step_collect_delivery(db, state, data, text="ок")
    prompt = q or (ADDRESS_PROMPT if dtype == DELIVERY_TYPE_ADDRESS else WAREHOUSE_PROMPT)
    return ChatMessageResponse(
        response=f"Ок. {prompt}",
        action="collect_delivery_data",
        data={"delivery_type": dtype},
    )


async def _step_collect_delivery(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    delivery_type = data.get("delivery_type", DELIVERY_TYPE_WAREHOUSE)
    delivery = data.get("delivery_draft") or {}

    if _is_flow_aside(text) and text.strip().lower() != "ок":
        next_q = _next_delivery_question(data, delivery, str(delivery_type))
        if not next_q and data.get("order_id"):
            return await _step_collect_delivery(db, state, data, text="ок")
        total = data.get("total_amount")
        extra = f" Сума {float(total):g} грн уже в замовленні." if total else ""
        return ChatMessageResponse(
            response=f"Зрозумів.{extra} {next_q or DELIVERY_TYPE_PROMPT}",
            action="collect_delivery_data",
            data={"delivery_type": delivery_type},
        )

    _apply_delivery_answer(delivery, data, str(delivery_type), text)
    data["delivery_draft"] = delivery

    next_q = _next_delivery_question(data, delivery, str(delivery_type))
    if next_q:
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=next_q,
            action="collect_delivery_data",
            data={"delivery_type": delivery_type},
        )

    payload = {
        "delivery_type": delivery_type,
        "recipient_last_name": delivery.get("recipient_last_name") or data.get("last_name"),
        "recipient_first_name": delivery.get("recipient_first_name") or data.get("first_name"),
        "recipient_patronymic": delivery.get("recipient_patronymic"),
        "recipient_phone": delivery.get("recipient_phone") or data.get("phone"),
        "city": delivery.get("city") or data.get("city"),
        "warehouse": delivery.get("warehouse"),
        "street_address": delivery.get("street_address"),
        "weight_kg": 1.0,
        "description": (
            f"{data.get('product_type') or 'Замовлення Silicon'}"
            f" × {data.get('quantity') or '?'} шт"
            f" (#{data.get('order_id', '')})"
        ),
    }
    payload["recipient_name"] = build_recipient_name(payload)

    ok, errors = validate_delivery_payload(payload)
    if not ok:
        return ChatMessageResponse(
            response=errors[0] if errors else "Уточніть дані доставки.",
            action="collect_delivery_data",
            data={"errors": errors},
        )

    delivery_row = await order_service.save_delivery_info(db, state.chat_id, payload)
    data["delivery_id"] = delivery_row.id
    data.pop("delivery_draft", None)
    order_id = data.get("order_id")
    total = data.get("total_amount")

    # одразу LiqPay з прорахованою сумою
    if order_id:
        result = await db.execute(select(Order).where(Order.id == int(order_id)))
        order = result.scalar_one_or_none()
        if order:
            if total and not order.total_amount:
                order.total_amount = float(total)
                await db.commit()
            elif order.total_amount and not total:
                total = float(order.total_amount)
                data["total_amount"] = total
            # якщо суми все ще немає — спробувати перерахувати з прайсу
            if not total and order.product_type and order.quantity:
                size_mm = data.get("size_mm")
                recalc = calculator.calculate(
                    product_type=str(order.product_type),
                    quantity=int(order.quantity),
                    size_mm=str(size_mm) if size_mm else None,
                )
                if recalc.get("success"):
                    total = float(recalc["total_amount"])
                    order.total_amount = total
                    order.unit_price = recalc.get("unit_price")
                    if recalc.get("product_name"):
                        order.product_type = recalc["product_name"]
                    data["total_amount"] = total
                    await db.commit()

    # після доставки — вибір: LiqPay або рахунок PDF
    state.step = "collect_payment"
    _save_data(state, data)
    await db.commit()
    if order_id and total:
        return ChatMessageResponse(
            response=PAYMENT_PROMPT,
            action="collect_payment",
            data={"order_id": order_id, "total_amount": total, "delivery_id": delivery_row.id},
        )
    return ChatMessageResponse(
        response=(
            "Замовлення прийняв. Для цієї позиції потрібен індивідуальний прорахунок — "
            "після підтвердження суми запропоную оплату карткою або за рахунком."
        ),
        action="order_submitted",
        data={"delivery_id": delivery_row.id},
    )


async def _step_collect_payment(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    """Вибір: LiqPay (1) або рахунок на реквізити / ЄДРПОУ (2)."""
    lower = text.lower().strip()
    order_id = data.get("order_id")

    wants_invoice = any(
        w in lower
        for w in ("2", "рахунок", "реквізит", "єдрпоу", "edrpou", "едрпоу", "безготів", "фоп", "тов")
    )
    if wants_invoice:
        state.step = "collect_invoice"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=INVOICE_PROMPT,
            action="collect_invoice_data",
        )

    wants_pay = any(
        w in lower
        for w in (
            "1",
            "liqpay",
            "лікпей",
            "лиг",
            "ліг",
            "карт",
            "карта",
            "онлайн",
            "так",
            "оплат",
            "посилан",
        )
    )
    if not wants_pay:
        return ChatMessageResponse(
            response=PAYMENT_PROMPT,
            action="collect_payment",
        )

    if not order_id:
        state.step = "idle"
        await db.commit()
        return ChatMessageResponse(
            response="Спочатку оформлюємо замовлення — напишіть «замовити».",
            action="payment_info",
        )

    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if order and not order.total_amount and data.get("total_amount"):
        order.total_amount = float(data["total_amount"])
        await db.commit()

    if not order or not order.total_amount:
        state.step = "idle"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response="Поки немає підтвердженої суми — після прорахунку надішлю посилання LiqPay.",
            action="order_submitted",
        )

    pay = await order_service.create_payment(db, int(order_id))
    state.step = "idle"
    _save_data(state, data)
    await db.commit()
    if not pay.get("success"):
        return ChatMessageResponse(
            response="Не вийшло сформувати LiqPay зараз. Можу виставити рахунок за ЄДРПОУ — напишіть «рахунок».",
            action="order_submitted",
        )
    url = pay.get("payment_url") or ""
    if not url.startswith("http"):
        return ChatMessageResponse(
            response="Не вийшло сформувати LiqPay зараз. Можу виставити рахунок за ЄДРПОУ — напишіть «рахунок».",
            action="order_submitted",
        )
    pay_text = _format_payment_message(data, url)
    pay_text += "\n\nЯкщо потрібен рахунок для ФОП/ТОВ — напишіть ЄДРПОУ або «рахунок»."
    return ChatMessageResponse(
        response=pay_text,
        action="payment_created",
        data=pay,
    )


async def _maybe_handle_edrpou_invoice(
    db: AsyncSession,
    state: ConversationState,
    data: dict,
    text: str,
    lower: str,
) -> ChatMessageResponse | None:
    """Пошук контрагента в 1С за ЄДРПОУ/назвою або старт рахунку на реквізити."""
    mentions_edrpou = any(
        w in lower for w in ("єдрпоу", "едрпоу", "edrpou", "єрдпу", "ердпу", "єрдпоу")
    )
    wants_lookup = mentions_edrpou or any(
        w in lower for w in ("контрагент", "знайти фоп", "пошук фоп", "перевір фоп", "знайти тов")
    )
    wants_invoice = any(
        w in lower
        for w in (
            "рахунок на реквізит",
            "оплата на реквізит",
            "безготівков",
            "рахунок для фоп",
            "рахунок для тов",
            "виставити рахунок",
        )
    )
    # окремо «рахунок», якщо не про банківський рахунок у контексті IBAN
    if "рахунок" in lower and "iban" not in lower and not wants_invoice:
        if any(w in lower for w in ("потрібен", "хочу", "зробіть", "зробити", "на реквізит", "фоп", "тов")):
            wants_invoice = True

    if not wants_lookup and not wants_invoice:
        return None

    edrpou = _extract_edrpou(text)

    if wants_lookup or (mentions_edrpou and edrpou):
        if not edrpou:
            # якщо є назва компанії після ключових слів — шукаємо за назвою
            name_guess = None
            for sep in ("назвою", "назва", "компані", "фоп", "тов"):
                if sep in lower:
                    parts = re.split(sep, text, maxsplit=1, flags=re.IGNORECASE)
                    if len(parts) > 1 and parts[1].strip():
                        name_guess = parts[1].strip(" :—-.")
                        break
            if name_guess and len(name_guess) >= 3:
                found = await order_service.onec.find_counterparty(name=name_guess)
                if found:
                    return ChatMessageResponse(
                        response=(
                            f"У 1С є контрагент: {found.get('name')}, "
                            f"ЄДРПОУ {found.get('edrpou') or '—'}."
                        ),
                        action="edrpou_found",
                        data=found,
                    )
                return ChatMessageResponse(
                    response=(
                        f"За назвою «{name_guess}» у 1С не знайшов. "
                        "Надішліть код ЄДРПОУ — перевірю ще раз."
                    ),
                    action="edrpou_not_found",
                )
            state.step = "lookup_edrpou"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response="Напишіть код ЄДРПОУ (8–10 цифр) — перевірю контрагента в 1С.",
                action="collect_edrpou",
            )
        found = await order_service.onec.find_counterparty(edrpou=edrpou)
        if found:
            data["edrpou"] = edrpou
            data["company_name"] = found.get("name")
            data["onec_counterparty_id"] = found.get("id")
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=(
                    f"Знайшов у 1С: {found.get('name')}, ЄДРПОУ {found.get('edrpou')}. "
                    "Дублікат не створюю. Потрібен рахунок на цю компанію?"
                ),
                action="edrpou_found",
                data=found,
            )
        data["edrpou"] = edrpou
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=(
                f"За ЄДРПОУ {edrpou} у 1С контрагента немає. "
                "Можу створити при виставленні рахунку — напишіть назву ФОП/ТОВ або «рахунок»."
            ),
            action="edrpou_not_found",
        )

    if wants_invoice:
        state.step = "collect_invoice"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=INVOICE_PROMPT,
            action="collect_invoice_data",
        )
    return None


async def _step_lookup_edrpou(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    edrpou = _extract_edrpou(text)
    if not edrpou:
        return ChatMessageResponse(
            response="Потрібен код ЄДРПОУ — 8 або 10 цифр.",
            action="collect_edrpou",
        )
    found = await order_service.onec.find_counterparty(edrpou=edrpou)
    state.step = "idle"
    if found:
        data["edrpou"] = edrpou
        data["company_name"] = found.get("name")
        data["onec_counterparty_id"] = found.get("id")
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=(
                f"Знайшов у 1С: {found.get('name')}, ЄДРПОУ {found.get('edrpou')}. "
                "Дублікат не створюю. Потрібен рахунок?"
            ),
            action="edrpou_found",
            data=found,
        )
    data["edrpou"] = edrpou
    _save_data(state, data)
    await db.commit()
    return ChatMessageResponse(
        response=(
            f"За ЄДРПОУ {edrpou} у 1С немає. "
            "Напишіть назву компанії — створю контрагента при рахунку, без дублікатів."
        ),
        action="edrpou_not_found",
    )


def _skip_optional(val: str) -> bool:
    return val.lower().strip() in (
        "немає",
        "нема",
        "нет",
        "no",
        "-",
        "пропустити",
        "skip",
        "відсутній",
        "відсутній іпн",
        "без іпн",
    )


async def _step_collect_invoice(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    """Збір: назва → ЄДРПОУ → ІПН → адреса → 1С + PDF-рахунок."""
    parsed = _parse_kv(text)
    for k in ("company_name", "edrpou", "ipn", "company_address", "street_address"):
        if parsed.get(k):
            if k == "street_address" and not data.get("company_address"):
                data["company_address"] = parsed[k]
            elif k != "street_address":
                data[k] = parsed[k]

    val = text.strip()
    edrpou_in_text = _extract_edrpou(text)

    # Відповідь на поточне порожнє поле (цифри ІПН не плутати з ЄДРПОУ)
    if val and not parsed:
        if not data.get("company_name"):
            data["company_name"] = val
        elif not data.get("edrpou"):
            data["edrpou"] = edrpou_in_text or val
        elif "ipn" not in data:
            data["ipn"] = "" if _skip_optional(val) else val
        elif not data.get("company_address"):
            data["company_address"] = val
    elif edrpou_in_text and not data.get("edrpou"):
        data["edrpou"] = edrpou_in_text

    if not data.get("company_name"):
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response="Напишіть назву організації (ФОП/ТОВ).",
            action="collect_invoice_data",
        )
    if not data.get("edrpou") or not re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or "")):
        data.pop("edrpou", None)
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response="Напишіть код ЄДРПОУ організації (8–10 цифр).",
            action="collect_invoice_data",
        )
    # перевірка в 1С після ЄДРПОУ
    if not data.get("_edrpou_checked"):
        found = await order_service.onec.find_counterparty(edrpou=str(data["edrpou"]))
        data["_edrpou_checked"] = True
        if found:
            data["onec_counterparty_id"] = found.get("id")
            if found.get("name") and data.get("company_name") in (None, "", found.get("name")):
                data["company_name"] = found.get("name")
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=(
                    f"У 1С вже є контрагент: {found.get('name')}, "
                    f"ЄДРПОУ {found.get('edrpou') or data['edrpou']}. "
                    "Дублікат не створюю. Напишіть ІПН (або «немає»)."
                ),
                action="collect_invoice_data",
                data=found,
            )
    if "ipn" not in data:
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response="Напишіть ІПН організації (якщо немає — «немає»).",
            action="collect_invoice_data",
        )
    if not data.get("company_address"):
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response="Напишіть адресу організації.",
            action="collect_invoice_data",
        )

    order_id = data.get("order_id")
    cart = list(data.get("cart") or [])
    items = []
    if cart:
        for row in cart:
            items.append({
                "product": row.get("product_name"),
                "qty": row.get("quantity"),
                "amount": row.get("total_amount"),
                "unit_price": row.get("unit_price"),
            })
    requisites = {
        "company_name": data["company_name"],
        "edrpou": data["edrpou"],
        "ipn": data.get("ipn") or "",
        "company_address": data["company_address"],
        "items": items,
    }

    if not order_id:
        await order_service.register_client_in_crm(
            db,
            state.chat_id,
            {
                "company_name": data["company_name"],
                "edrpou": data["edrpou"],
                "channel": "telegram",
            },
        )
        # створити контрагента в 1С навіть без order
        cp = await order_service.onec.create_counterparty({
            "name": data["company_name"],
            "edrpou": data["edrpou"],
            "ipn": data.get("ipn"),
            "company_address": data["company_address"],
        })
        state.step = "idle"
        _save_data(state, data)
        await db.commit()
        created = "створено" if cp.get("created") else "знайдено існуючого"
        return ChatMessageResponse(
            response=(
                f"Контрагента в 1С {created} (перевірка за ЄДРПОУ). "
                "Коли буде замовлення — виставлю рахунок PDF на ці реквізити."
            ),
            action="invoice_queued",
            data=cp,
        )

    inv = await order_service.create_invoice(db, int(order_id), requisites)
    state.step = "idle"
    _save_data(state, data)
    await db.commit()
    if not inv.get("success"):
        return ChatMessageResponse(
            response="Не вдалося виставити рахунок зараз. Спробуйте ще раз або напишіть менеджеру.",
            action="order_submitted",
        )
    created = "створено нового" if inv.get("counterparty_created") else "підтягнуто існуючого"
    docs = None
    pdf_path = inv.get("invoice_pdf_path")
    if pdf_path:
        docs = [
            ChatDocument(
                filename=inv.get("invoice_pdf_filename") or Path(pdf_path).name,
                path=pdf_path,
            )
        ]
    return ChatMessageResponse(
        response=(
            f"Рахунок {inv.get('invoice_number')} готовий у PDF (з печаткою та підписом). "
            f"Контрагента в 1С {created}.\n\n"
            "Можна оплатити за цим рахунком або карткою онлайн — напишіть «liqpay», "
            "якщо зручніше LiqPay."
        ),
        action="invoice_created",
        data=inv,
        documents=docs,
    )
