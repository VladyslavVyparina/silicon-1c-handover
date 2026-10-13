import json
import re
from pathlib import Path

from app.config import get_settings

settings = get_settings()


def format_money(value: float | int | str | None) -> str:
    """1150.0 → «1150», 11.5 → «11.5» (без зайвих нулів)."""
    if value is None:
        return "0"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    text = f"{n:.2f}".rstrip("0").rstrip(".")
    return text


def format_quote_message(result: dict, *, with_confirm: bool = True, cart: list | None = None) -> str:
    """Формулювання прорахунку для клієнта."""
    name = (result.get("product_name") or "товар").strip()
    soft = name[0].lower() + name[1:] if name else name
    qty = result.get("quantity")
    unit = format_money(result.get("unit_price"))
    total = format_money(result.get("total_amount"))
    days = result.get("production_days") or settings.default_production_days

    parts = [
        f"Ціна на {soft}, {qty} шт. буде:",
        "",
        f"{unit} грн/шт, загалом {total} грн.",
        f"Термін виготовлення: {days} робочих днів.",
    ]
    if cart and len(cart) > 1:
        parts.extend(["", "У замовленні зараз:"])
        for item in cart:
            parts.append(
                f"- {item.get('product_name')} × {item.get('quantity')} шт — "
                f"{format_money(item.get('total_amount'))} грн"
            )
        parts.append(f"Разом: {format_money(sum(float(i.get('total_amount') or 0) for i in cart))} грн.")
    if with_confirm:
        parts.extend(
            [
                "",
                "Все підходить? Можемо оформлювати замовлення?",
                "Якщо потрібен ще товар у це ж замовлення — напишіть який і кількість.",
            ]
        )
    return "\n".join(parts)


class PricingCalculator:
    """Розрахунок вартості та термінів виготовлення брендованої продукції."""

    def __init__(self, pricing_path: str | None = None):
        path = Path(pricing_path or settings.pricing_data_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent.parent / path
        with open(path, encoding="utf-8") as f:
            self._data = json.load(f)

    def _resolve_product_key(self, product_type: str, size_mm: str | None = None) -> str | None:
        products = self._data["products"]
        if product_type in products:
            return product_type
        lower = (product_type or "").lower()
        if not size_mm:
            m = re.search(r"(\d+)\s*мм", lower)
            if m:
                size_mm = m.group(1)
        reflective = any(
            w in lower for w in ("світловідб", "відбивн", "рефлект", "reflective", "reflect")
        )
        if size_mm and not reflective:
            key = self._data["size_to_product_key"].get(size_mm)
            if key and (
                "силікон" in lower
                or "silicone" in lower
                or "браслет" in lower
            ):
                return key
        mapping = {
            "силіконовий": {
                "12": "silicone_bracelet_12mm",
                "5": "silicone_bracelet_5mm",
                "20": "silicone_bracelet_20mm",
            },
            "силікон": {
                "12": "silicone_bracelet_12mm",
                "5": "silicone_bracelet_5mm",
                "20": "silicone_bracelet_20mm",
            },
            "silicone": {
                "12": "silicone_bracelet_12mm",
                "5": "silicone_bracelet_5mm",
                "20": "silicone_bracelet_20mm",
            },
            "світловідб": "reflective_bracelet_300",
            "відбивн": "reflective_bracelet_300",
            "рефлект": "reflective_bracelet_300",
            "reflective": "reflective_bracelet_300",
            "reflect": "reflective_bracelet_300",
        }
        for keyword, value in mapping.items():
            if keyword in lower:
                if isinstance(value, dict):
                    return value.get(size_mm or "12")
                return value
        return None

    def get_unit_price(self, product_key: str, quantity: int) -> float | None:
        product = self._data["products"].get(product_key)
        if not product:
            return None
        for tier in product["tiers"]:
            min_qty = tier["min_qty"]
            max_qty = tier["max_qty"]
            if quantity >= min_qty and (max_qty is None or quantity <= max_qty):
                return tier["price_per_unit"]
        return None

    def calculate(
        self,
        product_type: str,
        quantity: int,
        size_mm: str | None = None,
        urgent: bool = False,
    ) -> dict:
        product_key = self._resolve_product_key(product_type, size_mm)
        if not product_key:
            return {
                "success": False,
                "error": "Невідомий тип виробу. Зверніться до менеджера для індивідуального прорахунку.",
            }

        product = self._data["products"][product_key]
        unit_price = self.get_unit_price(product_key, quantity)

        if unit_price is None:
            return {
                "success": False,
                "error": f"Мінімальний тираж для {product['name']} — {product['tiers'][0]['min_qty']} шт.",
            }

        total = round(unit_price * quantity, 2)
        prod_days = product["production_days"]["urgent" if urgent else "standard"]
        payload = {
            "success": True,
            "product_key": product_key,
            "product_name": product["name"],
            "quantity": quantity,
            "unit_price": unit_price,
            "total_amount": total,
            "currency": "UAH",
            "production_days": prod_days,
            "urgent": urgent,
            "branding": product.get("branding", ""),
        }
        payload["message"] = format_quote_message(payload, with_confirm=False)
        return payload
