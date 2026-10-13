import json
import logging
import re
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import CatalogItem, Template
from app.services.pricing_calculator import PricingCalculator, format_quote_message

settings = get_settings()
logger = logging.getLogger(__name__)

ESCALATE_MARKER = "[ESCALATE]"


class AIConsultant:
    """Олег — AI-консультант Silicon з пошуком по каталогу та шаблонами."""

    AGENT_NAME = "Олег"
    FALLBACK_RESPONSE = (
        "Підкажіть, будь ласка, який саме товар і кількість вас цікавлять — "
        "порахую орієнтовно або підкажу наступний крок."
    )
    MANAGER_NAME = "Дмитро Афонін"
    HISTORY_LIMIT = 24

    ASSORTMENT_TRIGGERS = (
        "які товар",
        "який товар",
        "що у вас є",
        "що є у вас",
        "які є",
        "що продаєте",
        "асортимент",
        "каталог",
        "прайс",
        "цін",
        "вартіст",
        "що пропонуєте",
        "проконсультуй",
        "не знаю які",
        "не знаю що",
        "які браслет",
        "покажи товар",
        "розкажи про товар",
        "що робите",
        "чим займаєтесь",
        "які у вас",
        "що за товар",
    )

    def __init__(self):
        from app.services import bot_settings as bs

        self.instructions = bs.get_prompt_sync()
        self._calculator = PricingCalculator()

    def _reload_instructions(self) -> str:
        from app.services import bot_settings as bs

        self.instructions = bs.get_prompt_sync()
        return self.instructions

    def _wants_assortment(self, question: str) -> bool:
        lower = (question or "").lower()
        if re.search(r"\d+\s*(?:шт|штук)", lower):
            # конкретний прорахунок — не огляд
            return False
        return any(t in lower for t in self.ASSORTMENT_TRIGGERS)

    def build_assortment_overview(self) -> str:
        """Короткий огляд асортименту + орієнтири цін з прайсу (без звали всього PDF)."""
        return (
            "Основний асортимент Silicon:\n"
            "• силіконові браслети з друком лого (5 / 12 / 20 мм)\n"
            "• світловідбиваючі браслети (300×30, 400×30)\n"
            "• RFID, slap, Tyvek / контрольні браслети\n"
            "• еко-чашки, шапочки для плавання, шнурки, брелоки, голограми\n\n"
            "Орієнтовні ціни (грн/шт, з прайсу `pricelist_silicon.txt`):\n"
            "• силікон 12 мм з одноколірним друком: від 50 шт — 17,50; "
            "100 шт — 11,50; 500 шт — 9,20; 1000+ — 8,50\n"
            "• світловідбиваючі 300×30: від ~25–50 грн/шт залежно від тиражу й друку\n\n"
            "У прайсі є доплати «Додатково» (зміна кольору/розміру в тиражі +200 грн, "
            "нумерація +15 грн/номер тощо).\n\n"
            "Що саме цікавить і який тираж — порахую точніше?"
        )

    async def find_template(self, db: AsyncSession, question: str) -> str | None:
        result = await db.execute(select(Template).where(Template.is_active == True))  # noqa: E712
        templates = result.scalars().all()
        lower_q = question.lower()
        for tpl in templates:
            keywords = [k.strip().lower() for k in tpl.keywords.split(",")]
            if any(kw in lower_q for kw in keywords if kw):
                return tpl.response_text
        return self._builtin_template(lower_q)

    def _builtin_template(self, lower_q: str) -> str | None:
        pricing_path = Path(settings.pricing_data_path)
        if not pricing_path.is_absolute():
            pricing_path = Path(__file__).resolve().parent.parent.parent / pricing_path
        templates = json.loads(pricing_path.read_text(encoding="utf-8")).get("templates", {})

        if any(
            w in lower_q
            for w in (
                "хто ти",
                "як тебе звати",
                "представ",
                "хто ви",
                "що за компан",
                "про компан",
                "яка компан",
                "що ви за",
                "хто такі",
                "silicon хто",
            )
        ):
            return templates.get("company") or templates.get("intro")
        if any(w in lower_q for w in ["доставк", "нова пошта", "відправк"]):
            return templates.get("delivery")
        if any(w in lower_q for w in ["оплат", "liqpay", "лікпей", "рахунок", "єдрпоу", "реквізит"]):
            return templates.get("payment_methods")
        if any(w in lower_q for w in ["макет", "логотип файл", "ai,", "eps", "svg", "pdf"]):
            return templates.get("layout")
        if any(w in lower_q for w in ["строк", "термін", "коли буде", "скільки днів"]):
            return templates.get("production_terms")
        return None

    def try_price_quote(self, question: str) -> dict | None:
        """Детермінований прорахунок з прайсу — без OpenAI і без менеджера."""
        lower = (question or "").lower()
        qty_m = re.search(r"(\d[\d\s]*)\s*(?:шт|штук|шт\.)", lower)
        if not qty_m:
            # «скільки коштує 1000 силіконових»
            if any(w in lower for w in ("ціна", "коштує", "прорахун", "розрахун", "прайс", "скільки")):
                qty_m = re.search(r"\b(\d{2,5})\b", lower)
        if not qty_m:
            return None
        qty = int(re.sub(r"\s+", "", qty_m.group(1)))
        if qty < 1:
            return None

        size_m = re.search(r"(\d+)\s*мм", lower)
        size_mm = size_m.group(1) if size_m else None

        has_reflective = any(
            w in lower
            for w in ("світловідб", "світло відб", "відбивн", "рефлект", "reflective", "reflect")
        )
        has_silicone = "силікон" in lower
        # кілька товарів в одному повідомленні — обробляє conversation_flow
        if has_reflective and has_silicone:
            return None
        if has_reflective:
            product_type = "світловідбиваючий"
        elif has_silicone or (
            "браслет" in lower
            and not any(w in lower for w in ("rfid", "slap", "tyvek", "шнур", "чашк", "килим", "шапоч", "брелок"))
        ):
            product_type = "силіконовий"
        else:
            return None

        result = self._calculator.calculate(
            product_type=product_type,
            quantity=qty,
            size_mm=size_mm,
        )
        if not result.get("success"):
            return None
        return result

    async def search_catalog(self, db: AsyncSession, question: str, limit: int = 5) -> list[CatalogItem]:
        words = [w for w in question.lower().split() if len(w) > 2]
        if not words:
            return []
        conditions = []
        for word in words[:5]:
            pattern = f"%{word}%"
            conditions.append(or_(CatalogItem.name.ilike(pattern), CatalogItem.description.ilike(pattern)))
        result = await db.execute(select(CatalogItem).where(or_(*conditions)).limit(limit))
        return list(result.scalars().all())

    def _build_context(self, catalog_items: list[CatalogItem]) -> str:
        if not catalog_items:
            return "Релевантних позицій каталогу не знайдено."
        lines = []
        for item in catalog_items:
            lines.append(f"- {item.name} ({item.category}): {item.description or ''} {item.price_info or ''}")
        return "\n".join(lines)

    async def _load_history(self, db: AsyncSession, chat_id: str | None) -> list[dict]:
        if not chat_id:
            return []
        from app.services import chat_history

        msgs = await chat_history.list_messages(db, chat_id, after_id=0, limit=200)
        recent = msgs[-self.HISTORY_LIMIT :]
        history: list[dict] = []
        for m in recent:
            if m.sender == "user":
                role = "user"
            elif m.sender in ("bot", "admin"):
                role = "assistant"
            else:
                continue
            content = (m.content or "").strip()
            if not content:
                continue
            history.append({"role": role, "content": content[:1500]})
        if history and history[-1]["role"] == "user":
            history = history[:-1]
        return history

    def _detect_escalation(self, text: str) -> tuple[str, bool]:
        """Заявка менеджеру — лише з явним маркером [ESCALATE]."""
        raw = (text or "").strip()
        if not raw:
            return self.FALLBACK_RESPONSE, False

        escalate = False
        if ESCALATE_MARKER in raw:
            escalate = True
            raw = raw.replace(ESCALATE_MARKER, "").strip()

        if not raw:
            raw = self.FALLBACK_RESPONSE
            # порожня відповідь після маркера — все ж ескалація
        return raw, escalate

    async def consult(self, db: AsyncSession, question: str, chat_id: str | None = None) -> dict:
        # 1) спочатку прайс — без менеджера
        quote = self.try_price_quote(question)
        if quote:
            msg = format_quote_message(quote, with_confirm=True)
            return {
                "response": msg,
                "source": "pricing",
                "escalate_to_manager": False,
                "pricing": quote,
            }

        # 2) «які товари / прайс / проконсультуй» — короткий огляд з прайсу (не fallback)
        if self._wants_assortment(question):
            return {
                "response": self.build_assortment_overview(),
                "source": "assortment",
                "escalate_to_manager": False,
            }

        # хто ми / про компанію — фіксована відповідь без зайвих питань
        company = self._builtin_template((question or "").lower())
        if company and "silicon" in company.lower() and "спеціалізуємося" in company.lower():
            return {
                "response": company,
                "source": "template",
                "escalate_to_manager": False,
            }

        catalog_items = await self.search_catalog(db, question)
        context = self._build_context(catalog_items)

        if settings.openai_api_key and not settings.mock_external_apis:
            from app.services import bot_settings as bs

            knowledge = bs.load_knowledge_text()
            if knowledge:
                context = f"{context}\n\nБаза знань:\n{knowledge[:8000]}"
            history = await self._load_history(db, chat_id)
            response, escalate = await self._ask_openai(question, context, history)
            if response == self.FALLBACK_RESPONSE and self._wants_assortment(question):
                response = self.build_assortment_overview()
            return {
                "response": response,
                "source": "openai",
                "escalate_to_manager": escalate,
            }

        template = await self.find_template(db, question)
        if template:
            clean, escalate = self._detect_escalation(template)
            return {
                "response": clean,
                "source": "template",
                "escalate_to_manager": escalate,
            }

        # інші FAQ-шаблони (доставка/оплата/строки)
        builtin = self._builtin_template((question or "").lower())
        if builtin:
            return {
                "response": builtin,
                "source": "template",
                "escalate_to_manager": False,
            }

        if not catalog_items:
            return {
                "response": self.build_assortment_overview(),
                "source": "assortment",
                "escalate_to_manager": False,
            }

        names = ", ".join(i.name for i in catalog_items[:3])
        return {
            "response": (
                f"По запиту є: {names}. "
                f"Яка кількість вас цікавить — порахую орієнтовно?"
            ),
            "source": "catalog",
            "escalate_to_manager": False,
        }

    async def _ask_openai(
        self,
        question: str,
        catalog_context: str,
        history: list[dict] | None = None,
    ) -> tuple[str, bool]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        system_prompt = f"""Ти — {self.AGENT_NAME}, живий консультант Silicon (silicon.com.ua) у Telegram.
Менеджер: {self.MANAGER_NAME}.

Це кастомний бот на сервері (не конструктор). CRM, Нова Пошта/ТТН, LiqPay, рахунок PDF і 1С
робить СИСТЕМА — ти не збираєш ПІБ/телефон/email/доставку в консультації і не вигадуєш посилання/ТТН.

Ціни бери з бази знань: файл `pricelist_silicon.txt` (пріоритет) та `Силіконові браслети.pdf`.
Не вигадуй прайс. Враховуй «Додатково» з прайсу.

Перед відповіддю: (1) останній товар клієнта без підміни; (2) без повторів уже відомого;
(3) без «заводських» AI-фраз («Чудово, дякую!», «Якщо потрібна допомога…»);
(4) одне уточнення на повідомлення; коротко, по-людськи.

Якщо клієнт питає асортимент/прайс без конкретного товару — дай короткий огляд категорій
і орієнтири цін з `pricelist_silicon.txt`, потім спитай що рахувати. Не відповідай лише
«який товар і кількість цікавить».

Якщо є ціна в прайсі й відома кількість — порахуй сам (грн/шт + сума + строк) і запропонуй оформити.
Формат прорахунку як в інструкціях. Суми без зайвого .0 (1150 грн).

ЄДРПОУ/рахунок: не відправляй шукати в інтернеті; для нового контрагента потрібні
назва + ЄДРПОУ + ІПН (або «немає») + адреса — збір веде сценарій.

[ESCALATE] лише якщо клієнт просить людину/менеджера або питання повністю поза асортиментом.
Без маркера заявка в адмінку НЕ створиться. Не ескалюй «про всяк випадок».

Інструкції:
{self._reload_instructions()[:14000]}

Контекст каталогу / бази знань:
{catalog_context}
"""
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history[-self.HISTORY_LIMIT :])
        messages.append({"role": "user", "content": question})

        try:
            from app.services.bot_settings import get_openai_model_sync

            model = get_openai_model_sync() or settings.openai_model or "gpt-5"
            kwargs: dict = {
                "model": model,
                "messages": messages,
                "max_tokens": 450,
            }
            # GPT-5.x інколи не приймає кастомну temperature — тоді повтор без неї
            if not str(model).startswith("gpt-5"):
                kwargs["temperature"] = 0.4
            try:
                completion = await client.chat.completions.create(**kwargs)
            except Exception as first_exc:
                if "temperature" in str(first_exc).lower() or "max_tokens" in str(first_exc).lower():
                    kwargs.pop("temperature", None)
                    kwargs.pop("max_tokens", None)
                    kwargs["max_completion_tokens"] = 450
                    completion = await client.chat.completions.create(**kwargs)
                else:
                    raise
            text = (completion.choices[0].message.content or "").strip()
            if not text:
                return self.FALLBACK_RESPONSE, False
            return self._detect_escalation(text)
        except Exception as exc:
            logger.warning("OpenAI error: %s", exc)
            if catalog_context and "не знайдено" not in catalog_context:
                return (
                    f"Коротко по каталогу:\n{catalog_context}\n\n"
                    "Яка кількість потрібна?",
                    False,
                )
            return self.FALLBACK_RESPONSE, False
