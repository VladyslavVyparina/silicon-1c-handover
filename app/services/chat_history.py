"""Збереження історії чатів і заявок менеджеру."""
from __future__ import annotations

import json

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone

from app.database.models import AdminUser, ChatMessage, Client, ConversationState, ManagerTicket, Order
from app.config import get_settings
from app.services.telegram_bot import TelegramBot


def telegram_display_name(client: Client | None) -> str:
    """Ім'я лише з Telegram (не CRM/замовлення)."""
    if not client:
        return ""
    parts = [client.tg_first_name or "", client.tg_last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    if client.tg_username:
        return f"@{client.tg_username}"
    return ""


def order_display_name(client: Client | None) -> str:
    """Ім'я з замовлення / CRM."""
    if not client:
        return ""
    parts = [client.first_name or "", client.last_name or ""]
    return " ".join(p for p in parts if p).strip()


async def save_message(
    db: AsyncSession,
    chat_id: str,
    sender: str,
    content: str,
    content_type: str = "text",
) -> ChatMessage:
    msg = ChatMessage(
        chat_id=str(chat_id),
        sender=sender,
        content=content,
        content_type=content_type,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


async def get_or_create_state(db: AsyncSession, chat_id: str) -> ConversationState:
    result = await db.execute(
        select(ConversationState).where(ConversationState.chat_id == str(chat_id))
    )
    state = result.scalar_one_or_none()
    if state:
        return state
    state = ConversationState(chat_id=str(chat_id), step="idle", data_json="{}", human_mode=False)
    db.add(state)
    await db.flush()
    return state


async def is_human_mode(db: AsyncSession, chat_id: str) -> bool:
    state = await get_or_create_state(db, chat_id)
    return bool(state.human_mode)


async def set_human_mode(db: AsyncSession, chat_id: str, enabled: bool) -> None:
    state = await get_or_create_state(db, chat_id)
    state.human_mode = enabled
    if enabled:
        state.step = "human_mode"
    elif state.step == "human_mode":
        state.step = "idle"
    await db.commit()


async def build_ticket_question(db: AsyncSession, chat_id: str, trigger: str) -> str:
    """Контекст для заявки: тригер + останні повідомлення + відомі поля замовлення."""
    trigger_clean = (trigger or "").strip() or "Клієнт просить менеджера"
    lines: list[str] = [f"Останнє повідомлення клієнта: {trigger_clean}", ""]

    # відомі дані зі сценарію
    state = await get_or_create_state(db, chat_id)
    try:
        data = json.loads(state.data_json or "{}")
    except json.JSONDecodeError:
        data = {}
    known = []
    for label, key in (
        ("Товар", "product_type"),
        ("Кількість", "quantity"),
        ("Сума", "total_amount"),
        ("Прізвище", "last_name"),
        ("Ім'я", "first_name"),
        ("Телефон", "phone"),
        ("Місто", "city"),
    ):
        if data.get(key):
            known.append(f"{label}: {data[key]}")
    if known:
        lines.append("Відомі дані зі сценарію:")
        lines.extend(known)
        lines.append("")

    msgs = await list_messages(db, chat_id, after_id=0, limit=200)
    recent = msgs[-16:]
    if recent:
        lines.append("Контекст переписки:")
        for m in recent:
            if m.sender == "user":
                who = "Клієнт"
            elif m.sender == "admin":
                who = "Менеджер"
            else:
                who = "Бот"
            content = (m.content or "").strip().replace("\n", " ")
            if not content:
                continue
            lines.append(f"• {who}: {content[:350]}")

    text = "\n".join(lines).strip()
    return text[:6000] if text else trigger_clean


async def create_manager_ticket(
    db: AsyncSession,
    chat_id: str,
    question: str,
) -> ManagerTicket:
    """Створює (або оновлює відкриту) заявку менеджеру і вмикає human_mode."""
    client_result = await db.execute(select(Client).where(Client.chat_id == str(chat_id)))
    client = client_result.scalar_one_or_none()

    # не плодити дублікати — оновлюємо існуючу відкриту заявку
    existing = (
        await db.execute(
            select(ManagerTicket)
            .where(
                ManagerTicket.chat_id == str(chat_id),
                ManagerTicket.status.in_(["open", "in_progress"]),
            )
            .order_by(desc(ManagerTicket.id))
            .limit(1)
        )
    ).scalar_one_or_none()

    q = await build_ticket_question(db, chat_id, question)
    if existing:
        existing.question = q
        existing.status = "open"
        existing.is_read = False
        if client and not existing.client_id:
            existing.client_id = client.id
        ticket = existing
    else:
        ticket = ManagerTicket(
            chat_id=str(chat_id),
            client_id=client.id if client else None,
            question=q,
            status="open",
            is_read=False,
        )
        db.add(ticket)

    await db.flush()

    state = await get_or_create_state(db, chat_id)
    state.human_mode = True
    state.step = "human_mode"
    await db.commit()
    await db.refresh(ticket)

    # Telegram notify managers (best-effort)
    try:
        settings = get_settings()
        notify_id = settings.admin_notify_chat_id
        if notify_id:
            name = telegram_display_name(client) or "—"
            phone = client.phone if client else ""
            base = settings.admin_public_url.rstrip("/")
            short = (question or "").strip()[:200] or "запит"
            text = (
                "🔔 Новий запит менеджеру (Silicon / Олег)\n\n"
                f"Telegram: {name}\n"
                f"Chat ID: {chat_id}\n"
                f"Телефон: {phone or '—'}\n"
                f"Тригер: {short}\n\n"
                f"Відкрити чат: {base}/admin/live-chats?chat_id={chat_id}"
            )
            await TelegramBot().send_message(notify_id, text)
    except Exception:
        logger = __import__("logging").getLogger(__name__)
        logger.exception("manager notify failed")

    return ticket


async def list_messages(
    db: AsyncSession, chat_id: str, after_id: int = 0, limit: int = 200
) -> list[ChatMessage]:
    q = (
        select(ChatMessage)
        .where(ChatMessage.chat_id == str(chat_id), ChatMessage.id > after_id)
        .order_by(ChatMessage.id)
        .limit(limit)
    )
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_stats(db: AsyncSession) -> dict:
    clients = (await db.execute(select(func.count(Client.id)))).scalar() or 0
    # Unique chat participants from messages
    chats = (
        await db.execute(select(func.count(func.distinct(ChatMessage.chat_id))))
    ).scalar() or 0
    messages = (await db.execute(select(func.count(ChatMessage.id)))).scalar() or 0
    open_tickets = (
        await db.execute(
            select(func.count(ManagerTicket.id)).where(ManagerTicket.status == "open")
        )
    ).scalar() or 0
    unread_tickets = (
        await db.execute(
            select(func.count(ManagerTicket.id)).where(
                ManagerTicket.status == "open", ManagerTicket.is_read == False  # noqa: E712
            )
        )
    ).scalar() or 0
    human_chats = (
        await db.execute(
            select(func.count(ConversationState.id)).where(
                ConversationState.human_mode == True  # noqa: E712
            )
        )
    ).scalar() or 0
    orders = (await db.execute(select(func.count(Order.id)))).scalar() or 0
    closed_tickets = (
        await db.execute(
            select(func.count(ManagerTicket.id)).where(ManagerTicket.status == "closed")
        )
    ).scalar() or 0
    latest_unread = (
        await db.execute(
            select(ManagerTicket)
            .where(
                ManagerTicket.status.in_(["open", "in_progress"]),
                ManagerTicket.is_read == False,  # noqa: E712
            )
            .order_by(desc(ManagerTicket.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    latest_payload = None
    if latest_unread:
        latest_payload = {
            "id": latest_unread.id,
            "chat_id": latest_unread.chat_id,
            "question": (latest_unread.question or "")[:240],
        }

    # статистика по менеджерах: скільки закрили
    managers = list(
        (
            await db.execute(
                select(AdminUser).where(AdminUser.is_active == True)  # noqa: E712
            )
        ).scalars().all()
    )
    manager_stats = []
    for m in managers:
        closed_by = (
            await db.execute(
                select(func.count(ManagerTicket.id)).where(
                    ManagerTicket.closed_by_id == m.id,
                    ManagerTicket.status == "closed",
                )
            )
        ).scalar() or 0
        claimed = (
            await db.execute(
                select(func.count(ManagerTicket.id)).where(
                    ManagerTicket.assigned_to_id == m.id,
                    ManagerTicket.status.in_(["open", "in_progress"]),
                )
            )
        ).scalar() or 0
        manager_stats.append({
            "id": m.id,
            "name": m.full_name or m.email,
            "role": m.role,
            "closed": closed_by,
            "active": claimed,
        })
    manager_stats.sort(key=lambda x: (-x["closed"], x["name"]))

    return {
        "clients": clients,
        "bot_users": max(clients, chats),
        "messages": messages,
        "open_tickets": open_tickets,
        "unread_tickets": unread_tickets,
        "closed_tickets": closed_tickets,
        "human_mode_chats": human_chats,
        "orders": orders,
        "latest_unread_ticket": latest_payload,
        "latest_ticket_id": latest_unread.id if latest_unread else 0,
        "manager_stats": manager_stats,
    }


async def list_chat_previews(db: AsyncSession) -> list[dict]:
    # Latest message per chat
    subq = (
        select(
            ChatMessage.chat_id,
            func.max(ChatMessage.id).label("max_id"),
        )
        .group_by(ChatMessage.chat_id)
        .subquery()
    )
    result = await db.execute(
        select(ChatMessage)
        .join(subq, ChatMessage.id == subq.c.max_id)
        .order_by(desc(ChatMessage.id))
        .limit(200)
    )
    messages = list(result.scalars().all())

    clients_result = await db.execute(select(Client))
    clients = {c.chat_id: c for c in clients_result.scalars().all()}

    tickets_result = await db.execute(
        select(ManagerTicket)
        .where(ManagerTicket.status.in_(["open", "in_progress"]))
        .order_by(desc(ManagerTicket.id))
    )
    tickets_by_chat: dict[str, ManagerTicket] = {}
    for t in tickets_result.scalars().all():
        if t.chat_id not in tickets_by_chat:
            tickets_by_chat[t.chat_id] = t

    states_result = await db.execute(select(ConversationState))
    states = {s.chat_id: s for s in states_result.scalars().all()}

    admins = {
        a.id: a
        for a in (
            await db.execute(select(AdminUser))
        ).scalars().all()
    }

    previews = []
    for m in messages:
        c = clients.get(m.chat_id)
        t = tickets_by_chat.get(m.chat_id)
        st = states.get(m.chat_id)
        display = telegram_display_name(c) or f"Chat {m.chat_id}"
        assignee = None
        if t and t.assigned_to_id and t.assigned_to_id in admins:
            a = admins[t.assigned_to_id]
            assignee = {"id": a.id, "name": a.full_name or a.email}
        previews.append({
            "chat_id": m.chat_id,
            "full_name": display,
            "tg_name": telegram_display_name(c),
            "order_name": order_display_name(c),
            "tg_username": c.tg_username if c else None,
            "tg_photo_url": c.tg_photo_url if c else None,
            "phone": c.phone if c else None,
            "email": c.email if c else None,
            "city": c.city if c else None,
            "last_snippet": (m.content or "")[:140],
            "last_sender": m.sender,
            "last_at": m.created_at.isoformat() if m.created_at else None,
            "human_mode": bool(st.human_mode) if st else False,
            "open_ticket": bool(t),
            "ticket_id": t.id if t else None,
            "ticket_question": t.question if t else None,
            "ticket_unread": (not t.is_read) if t else False,
            "ticket_status": t.status if t else None,
            "assigned_to": assignee,
            "client_id": c.id if c else None,
        })
    return previews


async def list_open_tickets(db: AsyncSession) -> list[ManagerTicket]:
    result = await db.execute(
        select(ManagerTicket)
        .where(ManagerTicket.status.in_(["open", "in_progress"]))
        .order_by(desc(ManagerTicket.id))
    )
    return list(result.scalars().all())


async def claim_ticket(
    db: AsyncSession, ticket_id: int, admin: AdminUser
) -> tuple[ManagerTicket | None, str | None]:
    """Взяти заявку. Помилка, якщо вже взяв інший менеджер."""
    ticket = (
        await db.execute(select(ManagerTicket).where(ManagerTicket.id == ticket_id))
    ).scalar_one_or_none()
    if not ticket:
        return None, "Заявку не знайдено"
    if ticket.status == "closed":
        return ticket, "Заявка вже закрита"
    if ticket.assigned_to_id and ticket.assigned_to_id != admin.id:
        other = await db.get(AdminUser, ticket.assigned_to_id)
        name = (other.full_name or other.email) if other else "інший менеджер"
        return ticket, f"Заявку вже взяв {name}"
    ticket.assigned_to_id = admin.id
    ticket.claimed_at = datetime.now(timezone.utc)
    ticket.status = "in_progress"
    ticket.is_read = True
    await set_human_mode(db, ticket.chat_id, True)
    await db.commit()
    await db.refresh(ticket)
    return ticket, None


async def close_ticket(
    db: AsyncSession, ticket_id: int, admin: AdminUser
) -> tuple[ManagerTicket | None, str | None]:
    ticket = (
        await db.execute(select(ManagerTicket).where(ManagerTicket.id == ticket_id))
    ).scalar_one_or_none()
    if not ticket:
        return None, "Заявку не знайдено"
    if ticket.assigned_to_id and ticket.assigned_to_id != admin.id and admin.role != "owner":
        return ticket, "Закрити може лише менеджер, який взяв заявку, або власник"
    ticket.status = "closed"
    ticket.is_read = True
    ticket.closed_by_id = admin.id
    ticket.closed_at = datetime.now(timezone.utc)
    # якщо немає інших відкритих по чату — вимкнути human mode
    other = (
        await db.execute(
            select(ManagerTicket).where(
                ManagerTicket.chat_id == ticket.chat_id,
                ManagerTicket.status.in_(["open", "in_progress"]),
                ManagerTicket.id != ticket.id,
            )
        )
    ).scalars().first()
    if not other:
        await set_human_mode(db, ticket.chat_id, False)
    await db.commit()
    await db.refresh(ticket)
    return ticket, None


async def ensure_can_access_chat(
    db: AsyncSession, chat_id: str, admin: AdminUser
) -> str | None:
    """None якщо можна; інакше текст помилки (чужа взята заявка)."""
    if admin.role == "owner":
        return None
    tickets = list(
        (
            await db.execute(
                select(ManagerTicket).where(
                    ManagerTicket.chat_id == str(chat_id),
                    ManagerTicket.status.in_(["open", "in_progress"]),
                )
            )
        ).scalars().all()
    )
    for t in tickets:
        if t.assigned_to_id and t.assigned_to_id != admin.id:
            other = await db.get(AdminUser, t.assigned_to_id)
            name = (other.full_name or other.email) if other else "інший менеджер"
            return f"Чат зайнятий менеджером {name}"
    return None


async def ticket_payload(db: AsyncSession, ticket: ManagerTicket) -> dict:
    client = None
    if ticket.client_id:
        client = await db.get(Client, ticket.client_id)
    if not client:
        client = (
            await db.execute(select(Client).where(Client.chat_id == ticket.chat_id))
        ).scalar_one_or_none()
    assignee = None
    if ticket.assigned_to_id:
        a = await db.get(AdminUser, ticket.assigned_to_id)
        if a:
            assignee = {"id": a.id, "name": a.full_name or a.email}
    closer = None
    if ticket.closed_by_id:
        a = await db.get(AdminUser, ticket.closed_by_id)
        if a:
            closer = {"id": a.id, "name": a.full_name or a.email}
    return {
        "id": ticket.id,
        "chat_id": ticket.chat_id,
        "question": ticket.question,
        "status": ticket.status,
        "is_read": ticket.is_read,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "claimed_at": ticket.claimed_at.isoformat() if ticket.claimed_at else None,
        "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
        "tg_name": telegram_display_name(client),
        "order_name": order_display_name(client),
        "tg_username": client.tg_username if client else None,
        "phone": client.phone if client else None,
        "assigned_to": assignee,
        "closed_by": closer,
    }


async def group_tickets_by_person(db: AsyncSession, tickets: list[ManagerTicket]) -> list[dict]:
    """Групує заявки однієї людини (chat_id / телефон) в кучки."""
    clients = {
        c.chat_id: c
        for c in (await db.execute(select(Client))).scalars().all()
    }
    groups: dict[str, dict] = {}
    order: list[str] = []
    for t in tickets:
        c = clients.get(t.chat_id)
        key = t.chat_id
        if c and c.phone:
            key = f"phone:{c.phone}"
        if key not in groups:
            groups[key] = {
                "key": key,
                "chat_id": t.chat_id,
                "tg_name": telegram_display_name(c),
                "phone": c.phone if c else None,
                "tickets": [],
            }
            order.append(key)
        groups[key]["tickets"].append(await ticket_payload(db, t))
    return [groups[k] for k in order]
