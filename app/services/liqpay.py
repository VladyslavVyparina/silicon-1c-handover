import base64
import hashlib
import json
import uuid
from typing import Any
from urllib.parse import urlencode

from app.config import get_settings

settings = get_settings()


class LiqPayService:
    """Створення платежів через LiqPay та перевірка webhook-підпису."""

    CHECKOUT_URL = "https://www.liqpay.ua/api/3/checkout"

    def __init__(self):
        self.public_key = settings.liqpay_public_key or "sandbox_i0000000"
        self.private_key = settings.liqpay_private_key or "sandbox_private_key"
        self.sandbox = settings.liqpay_sandbox

    def _sign(self, data_b64: str) -> str:
        sign_string = self.private_key + data_b64 + self.private_key
        return base64.b64encode(hashlib.sha1(sign_string.encode()).digest()).decode()

    def create_payment(self, order_id: str, amount: float, description: str, result_url: str | None = None) -> dict:
        if amount is None or float(amount) <= 0:
            return {"success": False, "error": "Некоректна сума платежу"}

        liqpay_order_id = f"silicon-{order_id}-{uuid.uuid4().hex[:6]}"
        params: dict[str, Any] = {
            "public_key": self.public_key,
            "version": 3,
            "action": "pay",
            "amount": round(float(amount), 2),
            "currency": "UAH",
            "description": description[:200],
            "order_id": liqpay_order_id,
            "sandbox": 1 if self.sandbox or settings.mock_external_apis else 0,
        }
        if result_url:
            params["result_url"] = result_url

        data_json = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
        data_b64 = base64.b64encode(data_json.encode("utf-8")).decode()
        signature = self._sign(data_b64)

        # LiqPay вимагає data + signature — голе /api/3/checkout не працює
        query = urlencode({"data": data_b64, "signature": signature})
        payment_url = f"{self.CHECKOUT_URL}?{query}"

        return {
            "success": True,
            "payment_url": payment_url,
            "liqpay_order_id": liqpay_order_id,
            "data": data_b64,
            "signature": signature,
            "mock": bool(settings.mock_external_apis),
        }

    def verify_webhook(self, data: str, signature: str) -> bool:
        expected = self._sign(data)
        return expected == signature

    def decode_webhook(self, data: str) -> dict:
        decoded = base64.b64decode(data).decode()
        return json.loads(decoded)
