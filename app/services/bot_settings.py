"""Налаштування бота (модель GPT, промпт, база знань) — лише для owner у адмінці."""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PROJECT_ROOT, get_settings
from app.database.models import AppSetting

KNOWLEDGE_DIR = PROJECT_ROOT / "data" / "knowledge"
PROMPT_OVERRIDE_PATH = PROJECT_ROOT / "data" / "consultant_prompt_override.txt"
SETTINGS_CACHE_PATH = PROJECT_ROOT / "data" / "bot_runtime.json"

OPENAI_CHAT_MODELS = [
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.2",
    "gpt-5.1",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5-pro",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o",
    "gpt-4o-mini",
    "o3",
    "o3-mini",
    "o4-mini",
]

KEY_MODEL = "openai_model"
KEY_PROMPT = "consultant_prompt"


def ensure_dirs() -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)


async def get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    row = (
        await db.execute(select(AppSetting).where(AppSetting.key == key))
    ).scalar_one_or_none()
    if row and row.value is not None:
        return row.value
    return default


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    row = (
        await db.execute(select(AppSetting).where(AppSetting.key == key))
    ).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    await db.commit()
    _write_runtime_cache(key, value)


def _write_runtime_cache(key: str, value: str) -> None:
    ensure_dirs()
    data: dict = {}
    if SETTINGS_CACHE_PATH.exists():
        try:
            data = json.loads(SETTINGS_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[key] = value
    SETTINGS_CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_runtime_cache(key: str) -> str | None:
    if not SETTINGS_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(SETTINGS_CACHE_PATH.read_text(encoding="utf-8"))
        val = data.get(key)
        return str(val) if val is not None else None
    except Exception:
        return None


def get_openai_model_sync() -> str:
    cached = _read_runtime_cache(KEY_MODEL)
    if cached:
        return cached
    return get_settings().openai_model or "gpt-5"


def get_prompt_sync() -> str:
    """Промпт: override з адмінки → файл інструкцій."""
    if PROMPT_OVERRIDE_PATH.exists():
        text = PROMPT_OVERRIDE_PATH.read_text(encoding="utf-8").strip()
        if text:
            return text
    path = Path(get_settings().consultant_instructions_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


async def save_prompt(db: AsyncSession, text: str) -> None:
    ensure_dirs()
    PROMPT_OVERRIDE_PATH.write_text(text or "", encoding="utf-8")
    await set_setting(db, KEY_PROMPT, text or "")


async def save_model(db: AsyncSession, model: str) -> None:
    model = (model or "").strip()
    if model not in OPENAI_CHAT_MODELS:
        raise ValueError(f"Невідома модель: {model}")
    await set_setting(db, KEY_MODEL, model)


def list_knowledge_files() -> list[dict]:
    ensure_dirs()
    files = []
    for p in sorted(KNOWLEDGE_DIR.iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and not p.name.startswith("."):
            try:
                rel = str(p.relative_to(PROJECT_ROOT))
            except ValueError:
                rel = str(p)
            files.append({
                "name": p.name,
                "size": p.stat().st_size,
                "path": rel,
            })
    return files


def save_knowledge_file(filename: str, content: bytes) -> str:
    ensure_dirs()
    safe = "".join(c for c in filename if c.isalnum() or c in ("-", "_", ".", " ")).strip()
    if not safe:
        safe = "knowledge.txt"
    dest = KNOWLEDGE_DIR / safe
    dest.write_bytes(content)
    return safe


def delete_knowledge_file(filename: str) -> bool:
    ensure_dirs()
    safe = Path(filename).name
    dest = KNOWLEDGE_DIR / safe
    if dest.exists() and dest.is_file():
        dest.unlink()
        return True
    return False


def load_knowledge_text(limit_chars: int = 20000) -> str:
    ensure_dirs()
    chunks: list[str] = []
    total = 0
    for p in sorted(KNOWLEDGE_DIR.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() not in {".txt", ".md", ".csv", ".json"}:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not text:
            continue
        block = f"### Файл: {p.name}\n{text}\n"
        if total + len(block) > limit_chars:
            remain = limit_chars - total
            if remain > 100:
                chunks.append(block[:remain])
            break
        chunks.append(block)
        total += len(block)
    return "\n".join(chunks).strip()
