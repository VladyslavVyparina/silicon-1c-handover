"""Генерація PDF-рахунку з печаткою та підписом для відправки в Telegram."""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.config import PROJECT_ROOT, get_settings

BRANDING_DIR = PROJECT_ROOT / "data" / "branding"
INVOICES_DIR = PROJECT_ROOT / "data" / "invoices"

_FONT_REGISTERED = False
FONT_NAME = "InvoiceUA"


def _register_font() -> str:
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return FONT_NAME
    candidates = [
        Path("/Library/Fonts/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
        PROJECT_ROOT / "data" / "branding" / "DejaVuSans.ttf",
    ]
    for path in candidates:
        if path.exists():
            pdfmetrics.registerFont(TTFont(FONT_NAME, str(path)))
            _FONT_REGISTERED = True
            return FONT_NAME
    _FONT_REGISTERED = True
    return "Helvetica"


def _seller() -> dict[str, str]:
    s = get_settings()
    return {
        "name": getattr(s, "invoice_seller_name", None)
        or "ФОП Афонін Дмитро Вікторович",
        "address": getattr(s, "invoice_seller_address", None)
        or "вулиця Київська, буд. 22, с. Бриків, Шумський район, Тернопільська обл., 47140",
        "phone": getattr(s, "invoice_seller_phone", None) or "0958814263",
        "ipn": getattr(s, "invoice_seller_ipn", None) or "3052600213",
        "bank": getattr(s, "invoice_seller_bank", None) or 'КБ "ПРИВАТБАНК" М.ДНІПРО',
        "iban": getattr(s, "invoice_seller_iban", None)
        or "UA393052990000026006003304573",
    }


def _money(value: Any) -> str:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    whole = f"{n:,.2f}".replace(",", " ").replace(".", ",")
    return whole


def _draw_stamp_and_sign(c: canvas.Canvas, x: float, y: float) -> None:
    stamp = BRANDING_DIR / "stamp.png"
    sign = BRANDING_DIR / "signature.png"
    if stamp.exists():
        c.drawImage(str(stamp), x, y, width=38 * mm, height=38 * mm, mask="auto")
    else:
        c.setStrokeColor(colors.Color(0.75, 0.1, 0.1, alpha=0.85))
        c.setLineWidth(2)
        c.circle(x + 19 * mm, y + 19 * mm, 17 * mm)
        c.setFont(_register_font(), 8)
        c.setFillColor(colors.Color(0.75, 0.1, 0.1, alpha=0.85))
        c.drawCentredString(x + 19 * mm, y + 20 * mm, "М.П.")
        c.drawCentredString(x + 19 * mm, y + 15 * mm, "Silicon")
    if sign.exists():
        c.drawImage(str(sign), x + 42 * mm, y + 8 * mm, width=45 * mm, height=22 * mm, mask="auto")
    else:
        c.setStrokeColor(colors.Color(0.1, 0.1, 0.45, alpha=0.9))
        c.setLineWidth(1.4)
        path = c.beginPath()
        path.moveTo(x + 44 * mm, y + 14 * mm)
        path.curveTo(x + 52 * mm, y + 28 * mm, x + 60 * mm, y + 8 * mm, x + 78 * mm, y + 18 * mm)
        c.drawPath(path)


def build_invoice_pdf(
    *,
    invoice_number: str,
    buyer: dict[str, Any],
    items: list[dict[str, Any]],
    total_amount: float,
    invoice_date: date | None = None,
) -> bytes:
    """Повертає PDF-байти рахунку на оплату."""
    font = _register_font()
    seller = _seller()
    inv_date = invoice_date or date.today()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 18 * mm

    c.setFont(font, 14)
    c.drawString(18 * mm, y, f"Рахунок на оплату № {invoice_number} від {inv_date.strftime('%d.%m.%Y')} р.")
    y -= 12 * mm

    c.setFont(font, 9)
    lines_left = [
        f"Постачальник: {seller['name']}",
        f"Адреса: {seller['address']}",
        f"Тел.: {seller['phone']}",
        f"код за ДРФО/ІПН: {seller['ipn']}",
        f"Банк: {seller['bank']}",
        f"р/р (IBAN): {seller['iban']}",
    ]
    for line in lines_left:
        c.drawString(18 * mm, y, line)
        y -= 5 * mm

    y -= 4 * mm
    buyer_name = buyer.get("company_name") or buyer.get("name") or "—"
    c.drawString(18 * mm, y, f"Покупець: {buyer_name}")
    y -= 5 * mm
    if buyer.get("edrpou"):
        c.drawString(18 * mm, y, f"ЄДРПОУ: {buyer['edrpou']}")
        y -= 5 * mm
    if buyer.get("ipn"):
        c.drawString(18 * mm, y, f"ІПН: {buyer['ipn']}")
        y -= 5 * mm
    if buyer.get("company_address") or buyer.get("address"):
        c.drawString(18 * mm, y, f"Адреса: {buyer.get('company_address') or buyer.get('address')}")
        y -= 5 * mm

    y -= 6 * mm
    # table header
    c.setFillColor(colors.Color(0.93, 0.93, 0.93))
    c.rect(18 * mm, y - 4 * mm, width - 36 * mm, 8 * mm, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont(font, 9)
    c.drawString(20 * mm, y - 1.5 * mm, "№")
    c.drawString(28 * mm, y - 1.5 * mm, "Товар / послуга")
    c.drawString(118 * mm, y - 1.5 * mm, "К-сть")
    c.drawString(140 * mm, y - 1.5 * mm, "Ціна")
    c.drawString(165 * mm, y - 1.5 * mm, "Сума")
    y -= 10 * mm

    c.setFont(font, 9)
    for idx, item in enumerate(items, start=1):
        name = str(item.get("product") or item.get("name") or "Товар")[:55]
        qty = item.get("qty") or item.get("quantity") or 1
        amount = float(item.get("amount") or item.get("total_amount") or 0)
        try:
            unit = float(item.get("unit_price") or (amount / float(qty or 1)))
        except (TypeError, ValueError, ZeroDivisionError):
            unit = amount
        c.drawString(20 * mm, y, str(idx))
        c.drawString(28 * mm, y, name)
        c.drawRightString(132 * mm, y, str(qty))
        c.drawRightString(158 * mm, y, _money(unit))
        c.drawRightString(192 * mm, y, _money(amount))
        y -= 6 * mm
        if y < 60 * mm:
            c.showPage()
            c.setFont(font, 9)
            y = height - 20 * mm

    y -= 4 * mm
    c.setFont(font, 11)
    c.drawRightString(192 * mm, y, f"Разом: {_money(total_amount)} грн")
    y -= 8 * mm
    c.setFont(font, 9)
    c.drawString(18 * mm, y, "Усього до сплати за цим рахунком.")
    y -= 16 * mm
    c.drawString(18 * mm, y, f"Виписав(ла): {seller['name']}")
    _draw_stamp_and_sign(c, 110 * mm, y - 28 * mm)

    c.showPage()
    c.save()
    return buf.getvalue()


def save_invoice_pdf(
    *,
    invoice_number: str,
    buyer: dict[str, Any],
    items: list[dict[str, Any]],
    total_amount: float,
    order_id: int | None = None,
) -> Path:
    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in str(invoice_number) if ch.isalnum() or ch in "-_")
    name = f"invoice_{order_id or 'x'}_{safe}.pdf"
    path = INVOICES_DIR / name
    path.write_bytes(
        build_invoice_pdf(
            invoice_number=invoice_number,
            buyer=buyer,
            items=items,
            total_amount=total_amount,
        )
    )
    return path
