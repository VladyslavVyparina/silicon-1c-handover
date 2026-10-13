import uuid
from typing import Any

import httpx

from app.config import get_settings

settings = get_settings()

# In-memory mock registry for tests
_mock_counterparties: list[dict[str, Any]] = [
    {"id": "1c-001", "name": "ТОВ Сілікон", "edrpou": "12345678"},
    {"id": "1c-002", "name": "ПП Тест Компані", "edrpou": "87654321"},
]


class OneCService:
    """Перевірка та створення контрагентів у 1С (без дублікатів за ЄДРПОУ/назвою)."""

    def __init__(self):
        self.base_url = settings.onec_api_url.rstrip("/") if settings.onec_api_url else ""
        self.auth = (settings.onec_api_user, settings.onec_api_password)

    async def find_counterparty(self, edrpou: str | None = None, name: str | None = None) -> dict | None:
        if settings.mock_external_apis:
            for cp in _mock_counterparties:
                if edrpou and str(cp.get("edrpou") or "") == str(edrpou):
                    return cp
                if name and name.lower() in (cp.get("name") or "").lower():
                    return cp
            return None

        params: dict[str, str] = {}
        if edrpou:
            params["edrpou"] = edrpou
        if name:
            params["name"] = name

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.base_url}/counterparties/search",
                params=params,
                auth=self.auth,
            )
            resp.raise_for_status()
            items = resp.json()
            return items[0] if items else None

    async def create_counterparty(self, data: dict[str, Any]) -> dict:
        existing = await self.find_counterparty(
            edrpou=data.get("edrpou"),
            name=data.get("name") or data.get("company_name"),
        )
        if existing:
            return {
                "success": True,
                "counterparty_id": existing["id"],
                "created": False,
                "message": "Контрагент вже існує в 1С",
            }

        if settings.mock_external_apis:
            new_id = f"1c-{uuid.uuid4().hex[:6]}"
            cp = {
                "id": new_id,
                "name": data.get("name") or data.get("company_name"),
                "edrpou": data.get("edrpou"),
                "ipn": data.get("ipn"),
                "address": data.get("company_address") or data.get("address"),
            }
            _mock_counterparties.append(cp)
            return {
                "success": True,
                "counterparty_id": new_id,
                "created": True,
                "message": "Контрагент створено в 1С",
            }

        payload = {
            "edrpou": data.get("edrpou"),
            "name": data.get("name") or data.get("company_name"),
            "ipn": data.get("ipn"),
            "address": data.get("company_address") or data.get("address"),
            "phone": data.get("phone"),
            "email": data.get("email"),
            "bank_account": data.get("bank_account"),
            "bank_name": data.get("bank_name"),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/counterparties",
                json=payload,
                auth=self.auth,
            )
            resp.raise_for_status()
            result = resp.json()
            return {
                "success": True,
                "counterparty_id": str(result.get("id")),
                "created": True,
                "message": "Контрагент створено в 1С",
            }

    async def create_invoice(self, counterparty_id: str, order_data: dict[str, Any]) -> dict:
        invoice_number = f"INV-{uuid.uuid4().hex[:8].upper()}"
        if settings.mock_external_apis:
            return {
                "success": True,
                "invoice_number": invoice_number,
                "counterparty_id": counterparty_id,
                "amount": order_data.get("total_amount"),
                "mock": True,
            }

        payload = {
            "counterparty_id": counterparty_id,
            "amount": order_data.get("total_amount"),
            "description": order_data.get("description", "Замовлення Silicon"),
            "items": order_data.get("items", []),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/invoices",
                json=payload,
                auth=self.auth,
            )
            resp.raise_for_status()
            result = resp.json()
            return {
                "success": True,
                "invoice_number": result.get("number", invoice_number),
                "counterparty_id": counterparty_id,
            }


def reset_mock_counterparties() -> None:
    global _mock_counterparties
    _mock_counterparties = [
        {"id": "1c-001", "name": "ТОВ Сілікон", "edrpou": "12345678"},
        {"id": "1c-002", "name": "ПП Тест Компані", "edrpou": "87654321"},
    ]
