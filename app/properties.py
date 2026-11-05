import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.db import as_dict, utc_now

TYPES = (
    "text",
    "textarea",
    "single_select",
    "multi_select",
    "url",
    "email",
    "tel",
    "amount",
)
OBJECTS = ("lead", "account")


def _key(name: str) -> str:
    parts = []
    for char in name.casefold():
        if char.isalnum():
            parts.append(char)
        elif parts and parts[-1] != "_":
            parts.append("_")
    key = "".join(parts).strip("_")
    return key or "field"


def _cents(text: str) -> int:
    cleaned = text.replace(" ", "").replace(",", ".")
    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Сумма должна быть числом") from exc
    return int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def create_property_def(conn, *, name: str, type_: str, object_type: str = "lead") -> dict:
    label = (name or "").strip()
    if not label:
        raise ValueError("Укажите имя поля")
    kind = "" if type_ is None else str(type_).strip()
    if not kind or kind not in TYPES:
        raise ValueError("Укажите тип поля")
    if object_type not in OBJECTS:
        raise ValueError("Неизвестный объект поля")
    key = _key(label)
    existing = conn.execute(
        "SELECT * FROM property_defs WHERE object_type = ? AND key = ?",
        (object_type, key),
    ).fetchone()
    if existing:
        row = as_dict(existing)
        if row["type"] != kind:
            raise ValueError("Поле с таким именем уже есть")
        return row
    now = utc_now()
    position = conn.execute(
        """
        SELECT COALESCE(MAX(position), 0) + 1 AS pos
        FROM property_defs
        WHERE object_type = ?
        """,
        (object_type,),
    ).fetchone()["pos"]
    try:
        cur = conn.execute(
            """
            INSERT INTO property_defs (
                object_type, key, name, type, options_json, required, position,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, '[]', 0, ?, ?, ?)
            """,
            (object_type, key, label, kind, position, now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Поле с таким именем уже есть") from exc
    row = conn.execute("SELECT * FROM property_defs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return as_dict(row)


def list_property_defs(conn, object_type: str | None = None) -> list[dict]:
    if object_type and str(object_type).strip():
        clean_obj = str(object_type).strip()
        if clean_obj not in OBJECTS:
            raise ValueError("Неизвестный объект поля")
        rows = conn.execute(
            "SELECT * FROM property_defs WHERE object_type = ? ORDER BY position, id",
            (clean_obj,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM property_defs ORDER BY object_type, position, id"
        ).fetchall()
    return [as_dict(row) for row in rows]


def delete_property_def(conn, prop_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM property_defs WHERE id = ?",
        (int(prop_id),),
    ).fetchone()
    if row is None:
        raise LookupError("property")
    conn.execute("DELETE FROM property_values WHERE property_id = ?", (int(prop_id),))
    conn.execute("DELETE FROM property_defs WHERE id = ?", (int(prop_id),))
    conn.commit()
    return {"deleted": True, "id": int(prop_id)}


def list_lead_properties(conn, lead_id: int) -> list:
    rows = conn.execute(
        """
        SELECT
            d.id, d.key, d.name, d.type, d.object_type, d.position,
            v.value_text, v.value_int, v.currency
        FROM property_defs d
        LEFT JOIN property_values v
            ON v.property_id = d.id AND v.lead_id = ?
        WHERE d.object_type = 'lead'
        ORDER BY d.position, d.id
        """,
        (lead_id,),
    )
    return [as_dict(row) for row in rows]


def set_lead_property(conn, lead_id: int, property_id: int, value) -> dict:
    lead = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        raise LookupError("lead")
    prop = conn.execute(
        "SELECT * FROM property_defs WHERE id = ?",
        (property_id,),
    ).fetchone()
    if prop is None:
        raise LookupError("property")
    prop = as_dict(prop)
    if prop["object_type"] != "lead":
        raise ValueError("Поле не для лида")
    text = None if value is None else str(value).strip() or None
    value_int = _cents(text) if prop["type"] == "amount" and text is not None else None
    now = utc_now()
    existing = conn.execute(
        """
        SELECT id FROM property_values
        WHERE property_id = ? AND lead_id = ?
        """,
        (property_id, lead_id),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE property_values
            SET value_text = ?, value_int = ?, updated_at = ?
            WHERE id = ?
            """,
            (text, value_int, now, existing["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO property_values (
                property_id, lead_id, account_id, value_text, value_int, currency,
                created_at, updated_at
            ) VALUES (?, ?, NULL, ?, ?, NULL, ?, ?)
            """,
            (property_id, lead_id, text, value_int, now, now),
        )
    if prop["type"] == "amount":
        from app.events import record_lead_event

        record_lead_event(
            conn,
            lead_id,
            "amount_changed",
            f"Поле «{prop['name']}»: {text or '0'}",
            {
                "property_id": property_id,
                "property_name": prop["name"],
                "value_text": text,
                "value_int": value_int,
            },
            created_at=now,
        )
    conn.commit()
    for row in list_lead_properties(conn, lead_id):
        if row["id"] == property_id:
            return row
    raise LookupError("property")
