import re
import sqlite3

from app.db import as_dict, utc_now

DEFAULT_SNIPPETS: tuple[dict[str, str], ...] = (
    {
        "shortcut": "/price",
        "title": "Прайс",
        "body": (
            "Привет, {name}! Направляю условия и тарифы для {company}: "
            "базовый пакет стартует от $1,500/мес, под ключ с выделенной командой — от $2,800/мес. "
            "Подскажите, какой формат ближе?"
        ),
    },
    {
        "shortcut": "/cases",
        "title": "Кейсы",
        "body": (
            "Привет, {name}! Собрал релевантные кейсы под {company} "
            "с цифрами конверсии и окупаемости. Могу прислать короткий разбор прямо сюда в чат?"
        ),
    },
    {
        "shortcut": "/call",
        "title": "Созвон",
        "body": (
            "Привет, {name}! Предлагаю созвониться на 10–15 минут, чтобы обсудить задачи {company} "
            "и показать живое демо. Когда вам удобнее — сегодня после 15:00 или завтра утром?"
        ),
    },
    {
        "shortcut": "/requisites",
        "title": "Реквизиты",
        "body": (
            "Привет, {name}! Направляю реквизиты и шаблон договора для {company}. "
            "Подскажите, оформляем через юрлицо (счёт/ЭДО) или удобнее оплата в USDT?"
        ),
    },
)


def _normalize_shortcut(raw: str | None) -> str:
    cleaned = str(raw or "").strip()
    if not cleaned:
        raise ValueError("Укажите команду шаблона (например, /price)")
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    slug = cleaned[1:].strip()
    if not slug:
        raise ValueError("Укажите команду шаблона после /")
    if any(ch.isspace() for ch in slug):
        raise ValueError("Команда шаблона не должна содержать пробелы")
    return "/" + slug.lower()


def seed_default_snippets(conn: sqlite3.Connection) -> None:
    now = utc_now()
    for item in DEFAULT_SNIPPETS:
        conn.execute(
            """
            INSERT OR IGNORE INTO snippets (shortcut, title, body, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (item["shortcut"], item["title"], item["body"], now),
        )
    conn.commit()


def list_snippets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, shortcut, title, body, created_at
        FROM snippets
        ORDER BY id ASC
        """
    ).fetchall()
    return [as_dict(row) for row in rows]


def create_snippet(
    conn: sqlite3.Connection,
    shortcut: str,
    title: str,
    body: str,
) -> dict:
    norm_shortcut = _normalize_shortcut(shortcut)
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("Укажите название шаблона")
    clean_body = str(body or "").strip()
    if not clean_body:
        raise ValueError("Укажите текст шаблона")

    existing = conn.execute(
        "SELECT id FROM snippets WHERE lower(shortcut) = lower(?)",
        (norm_shortcut,),
    ).fetchone()
    if existing is not None:
        raise ValueError("Шаблон с такой командой уже есть")

    now = utc_now()
    try:
        cur = conn.execute(
            """
            INSERT INTO snippets (shortcut, title, body, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (norm_shortcut, clean_title, clean_body, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Шаблон с такой командой уже есть") from exc

    row = conn.execute(
        "SELECT id, shortcut, title, body, created_at FROM snippets WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return as_dict(row)


def delete_snippet(conn: sqlite3.Connection, snippet_id: int) -> dict:
    try:
        sid = int(snippet_id)
    except (TypeError, ValueError):
        raise ValueError("Некорректный id шаблона") from None

    row = conn.execute(
        "SELECT id, shortcut, title FROM snippets WHERE id = ?",
        (sid,),
    ).fetchone()
    if row is None:
        raise LookupError("snippet")

    conn.execute("DELETE FROM snippets WHERE id = ?", (sid,))
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at) VALUES ('snippets_user_modified', '1', ?)
        ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at
        """,
        (utc_now(),),
    )
    conn.commit()
    return {"deleted": True, "id": sid}


def render_snippet(
    body: str,
    lead: dict | None = None,
    *,
    name: str | None = None,
    company: str | None = None,
) -> str:
    text = str(body or "")
    lead_dict = lead if isinstance(lead, dict) else {}

    raw_name = str(
        name
        if name is not None
        else (
            lead_dict.get("name")
            or lead_dict.get("title")
            or lead_dict.get("username")
            or ""
        )
    ).strip()
    raw_company = str(
        company if company is not None else (lead_dict.get("company") or "")
    ).strip()

    if "·" in raw_name:
        parts = [p.strip() for p in raw_name.split("·", 1)]
        clean_name = parts[0]
        if not raw_company and len(parts) > 1 and parts[1]:
            raw_company = parts[1]
    else:
        clean_name = raw_name

    if not raw_company and isinstance(lead_dict.get("properties"), list):
        for prop in lead_dict["properties"]:
            if not isinstance(prop, dict):
                continue
            pname = str(prop.get("name") or "").strip().lower()
            pkey = str(prop.get("key") or "").strip().lower()
            if pname in ("компания", "company", "проект", "организация") or pkey in (
                "компания",
                "company",
                "org",
                "project",
            ):
                val = str(prop.get("value_text") or "").strip()
                if val:
                    raw_company = val
                    break

    final_name = clean_name or "коллега"
    final_company = raw_company or "вашей компании"
    rendered = re.sub(r"\{name\}", lambda _: final_name, text, flags=re.IGNORECASE)
    rendered = re.sub(r"\{company\}", lambda _: final_company, rendered, flags=re.IGNORECASE)
    return rendered
