#!/usr/bin/env python3
"""Черновики откликов на отобранные вакансии + прямая заливка в Foldesk CRM через MCP.

Поддерживает два режима цепочки (--touch-mode):
- direct (Вариант А, по умолчанию):
  * step1_text (0ч)   — полный персонализированный отклик в голосе Тима (кейсы + ссылки + вопрос)
  * step2_text (24ч)  — короткий фоллоу-ап по задаче с предложением тестового фрагмента 10–15 сек
  * step3_text (72ч)  — финальный короткий пинг по статусу отбора
- micro_ping (Вариант Б, опционально):
  * step1_text (0ч)   — короткий вопрос в 1 строку без ссылок
  * step2_text (24ч)  — полный персонализированный отклик с кейсами и ссылками
  * step3_text (72ч)  — короткий фоллоу-ап с предложением тестового фрагмента

Все отобранные вакансии загружаются в CRM через MCP-инструмент `ingest_discovered_leads`
в кампанию `TG Вакансии` (создаются карточки сделок в `leads` и получатели в `campaign_recipients`).
Пересылка в «Избранное» / @vladwilhelm (job_forward.py) полностью убрана.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import glob
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

try:
    from leadgen.crm_mcp_push import _extract_contact_fields, push_leads_via_mcp
    from leadgen.job_monitor import OUTDIR as STATE_DIR, ask_llm, load_env
except ImportError:
    from crm_mcp_push import _extract_contact_fields, push_leads_via_mcp
    from job_monitor import OUTDIR as STATE_DIR, ask_llm, load_env

VAULT = "/Users/timofei/obsidian/Second Brain"
LOCAL_BANK = WORKTREE_ROOT / "leadgen" / "job_cases_bank.md"
VAULT_BANK = Path(VAULT) / "!ПРОЕКТЫ" / "Кейсы — банк для откликов.md"
DEFAULT_CAMPAIGN_NAME = os.environ.get("JOBS_CAMPAIGN_NAME", "TG Вакансии")
PROD_LOCAL_DB = Path("/Users/timofei/dev/Python/new era/crmtg/data/crm.db")
WORKERS = 3

EXAMPLES = """ПРИМЕР 1 — вакансия: реалистичный ИИ-персонаж под ключ (@romapronik)

Добрый день! По задаче с реалистичным ИИ-персонажем под ключ.

Это моя основная специализация. Работаю на Nano Banana Pro и Higgsfield
(персонаж), Kling / Seedance / Omni / HeyGen (видео), ElevenLabs (голос и
липсинк). Консистентность держу: лицо, одежда, стиль сохраняются от серии к
серии.

Что делал:
— Faceless-канал на AI-персонажах: 183,5 млн просмотров за 90 дней, 241K
  подписчиков, 94,8% холодный охват
— Продакшн на потоке: 90 AI-роликов в месяц для одного клиента
— Реализм: люди, которых не отличить от живых — продукт в кадре, сторителлинг,
  RU и EN

Кейсы (строго реалистичные персонажи с видео и озвучкой):
• Шоурил: https://tmxfy.github.io/
• PDF кейсов: https://drive.google.com/file/d/1Aet4uXbGJwtDeJoh1fep0xeSlMTAH65w/view
• Полная папка с видео: https://drive.google.com/drive/folders/1EhiDsB4E4AJ6sgpn5_TcXBk0ObHyw212

По условиям скажу точно после ТЗ: какой персонаж, сколько роликов в месяц,
какой хронометраж?


ПРИМЕР 2 — вакансия: рилсмейкер в трейдинг-проект (@elvinagaripova)

Добрый день! По рилсмейкеру в трейдинг-проект.

Работаю в нише трейдинга и строю системы клиппинга для блогеров.

Цифры:
— Faceless-канал: 183,5 млн просмотров за 90 дней, 241K подписчиков, 94,8%
  холодный охват
— Монетизация через воронку: выручка $30 000/мес
— Темп: 20–30 сценариев в месяц рабочий объём, держал 90 единиц контента на
  проект

Финансовые ниши: строил воронку для компании по банкротству и разбору кредитов
(ИИ-сериал «выбирается из долгов»), лоу-тикет $27.

Кейсы:
• Сайт: https://tmxfy.github.io/
• PDF кейсов: https://drive.google.com/file/d/1Aet4uXbGJwtDeJoh1fep0xeSlMTAH65w/view
• Видео: https://drive.google.com/drive/folders/1EhiDsB4E4AJ6sgpn5_TcXBk0ObHyw212

Фикс плюс бонус за результат — только так и работаю. Какой сейчас CPA на
подписчика или лид?"""

PROMPT = """Ты пишешь отклик и два коротких фоллоу-апа на вакансию ЗА ТИМА для рассылки в CRM.

Кто такой Тим: практик AI UGC. Делает контент нейросетями, строит органические
воронки и контент-заводы. Он архитектор и владелец, руки делегирует своим людям.
Пришёл из разработки, 5+ лет программирования.

═══ БАНК КЕЙСОВ (единственный источник цифр — не выдумывай своих) ═══
{bank}

═══ КАК ПИШЕТ ТИМ (повторяй ритм и лексику, не стиль ИИ) ═══
{examples}

═══ ВАКАНСИЯ ═══
Роль по разбору: {role}
Почему подошла: {why}
Оплата: {pay}
Контакт: {contact}
Способ взять: {mode}

Текст объявления:
{text}

═══ ПРАВИЛА ДЛЯ ПЕРВОГО СООБЩЕНИЯ (text) ═══
СКЕЛЕТ:
1. «Добрый день! По <конкретной вакансии>.» — назвать её словами заказчика
2. Кто я и на чём работаю применительно к ЭТОЙ задаче: одно-два предложения, конкретика и стек
3. «Что делал:» — от двух до четырёх пунктов с цифрами, ПОДОБРАННЫМИ ПОД ЭТУ вакансию
4. Блок ссылок:
   — всегда: Шоурил https://tmxfy.github.io/ и PDF кейсов https://drive.google.com/file/d/1Aet4uXbGJwtDeJoh1fep0xeSlMTAH65w/view
   — видео и креаторские вакансии, плюсом: папка с видео https://drive.google.com/drive/folders/1EhiDsB4E4AJ6sgpn5_TcXBk0ObHyw212
   — технические и воронковые, плюсом: Notion https://destiny-occupation-0eb.notion.site/2418af798589806abbccd94a57083c2f?v=2418af798589809389ed000c2f3f02ac
5. Ровно один вопрос в конце по делу (объём, ниша, формат, метрика, ТЗ)

ЖЕЛЕЗНЫЕ ЗАПРЕТЫ:
— Везде, где в задаче есть креаторы или UGC — сразу заходить с «предлагаю внедрить AI UGC» (ролики с реалистичными ИИ-креаторами), вторым слоем своя команда креаторов.
— Экономику «минута AI UGC ≈ 0,5$ генераций» вставлять ТОЛЬКО если заказчик выбирает между живыми съёмками и AI в найме.
— Не называть себестоимость (600 ₽ за ролик), «15 минут на ролик», прайс-вилки.
— Без штампов ИИ («не просто X, а Y», «комплексный», «уникальный», «ключевой»), без эмодзи, без лести.

═══ ПРАВИЛА ДЛЯ ФОЛЛОУ-АПОВ (followup_1 и followup_2) ═══
— followup_1 (через 24 часа, если не ответили): 1–2 коротких предложения в том же спокойном деловом тоне. Без «просто напоминаю о себе». Привяжи к сути задачи ({role}): например, предложи прислать референс/ТЗ, чтобы собрать тестовый фрагмент 10–15 сек (для видео/дизайна) или набросать схему реализации (для ботов/воронок).
— followup_2 (через 72 часа, финальный пинг): 1–2 коротких предложения. Спокойно уточни, открыта ли ещё задача по {role} или уже взяли человека; если актуально — предложи быстрый созвон на 10 минут.

Верни СТРОГО один JSON без пояснений:
{{"text": "готовый текст первого отклика целиком, с переносами строк",
  "followup_1": "короткий фоллоу-ап через 24ч (1–2 предложения)",
  "followup_2": "короткий финальный фоллоу-ап через 72ч (1–2 предложения)",
  "cases": "какие цифры взял и почему именно они, одной строкой",
  "risk": "чем отклик может не зайти, одной строкой; нечем — пусто"}}"""


def load_bank() -> str:
    for p in (LOCAL_BANK, VAULT_BANK):
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except OSError:
                continue
    return "(банка нет)"


def _json_block(res: str | None) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", res or "", re.S)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def build_default_followups(item: dict[str, Any]) -> tuple[str, str]:
    role = (item.get("role") or "вакансии").strip()
    layer = (item.get("layer") or "").strip().lower()
    if "автоматиз" in layer or "бот" in role.lower():
        f1 = (
            f"Добрый день! Возвращаюсь по задаче ({role}). "
            "Если скинете короткое описание текущей воронки или ТЗ, сразу скажу по срокам и архитектуре."
        )
    else:
        f1 = (
            f"Добрый день! Возвращаюсь по задаче ({role}). "
            "Если пришлёте 1–2 референса или ТЗ, соберу тестовый фрагмент 10–15 секунд под ваш формат, чтобы оценить качество в деле."
        )
    f2 = (
        f"Подскажите, задача по «{role}» ещё открыта или уже нашли исполнителя? "
        "Если актуально — можем созвониться на 10 минут и обсудить запуск."
    )
    return f1, f2


def build_micro_ping(item: dict[str, Any]) -> str:
    role = (item.get("role") or "задаче").strip()
    return f"Добрый день! Подскажите, ещё ищете исполнителя по задаче «{role}»? Могу скинуть релевантные кейсы и примеры."


def draft(item: dict[str, Any], bank: str) -> dict[str, Any]:
    prompt = PROMPT.format(
        bank=bank,
        examples=EXAMPLES,
        role=item.get("role") or "не разобрано",
        why=item.get("why") or "",
        pay=item.get("pay") or "не названа",
        contact=item.get("contact") or "нет",
        mode=item.get("mode") or "не разобрано",
        text=(item.get("text") or "")[:3500],
    )
    try:
        res = ask_llm(prompt, timeout=360)
    except Exception as e:
        return {"error": f"модель не ответила: {type(e).__name__}: {e}"}
    d = _json_block(res)
    if not isinstance(d, dict) or not (d.get("text") or "").strip():
        return {"error": "ответ без JSON"}

    text = str(d["text"]).strip()
    f1_def, f2_def = build_default_followups(item)
    d["text"] = text
    d["followup_1"] = str(d.get("followup_1") or "").strip() or f1_def
    d["followup_2"] = str(d.get("followup_2") or "").strip() or f2_def
    d["no_question"] = "?" not in text[-260:]
    return d


def build_vacancy_mcp_lead(
    item: dict[str, Any],
    draft_obj: dict[str, Any],
    *,
    touch_mode: str = "direct",
) -> dict[str, Any] | None:
    """Преобразует вакансию и черновик отклика в формат MCP `ingest_discovered_leads`."""
    raw_contact = str(item.get("contact") or "").strip()
    contact_info = _extract_contact_fields(raw_contact, tg_col=raw_contact)
    username = contact_info["username"]
    phone = contact_info["phone"]
    email = contact_info["email"]
    if not any((username, phone, email)):
        return None

    role = str(item.get("role") or item.get("head") or "Вакансия").strip().lstrip("🔥 ").strip()
    why = str(item.get("why") or "").strip()
    pay = str(item.get("pay") or "").strip()
    layer = str(item.get("layer") or "AI-контент").strip()
    src = str(item.get("src") or "TG").strip()
    url = str(item.get("url") or "").strip()

    full_reply = str(draft_obj.get("text") or "").strip()
    if not full_reply:
        return None
    f1_def, f2_def = build_default_followups(item)
    followup_1 = str(draft_obj.get("followup_1") or "").strip() or f1_def
    followup_2 = str(draft_obj.get("followup_2") or "").strip() or f2_def

    mode = (touch_mode or "direct").strip().lower()
    if mode == "micro_ping":
        step1_text = build_micro_ping(item)
        step2_text = full_reply
        step3_text = followup_1
    else:
        step1_text = full_reply
        step2_text = followup_1
        step3_text = followup_2

    first_line = full_reply.splitlines()[0].strip() if full_reply else role
    their_angle_parts = [p for p in (why, f"Оплата: {pay}" if pay else "") if p]
    their_angle = " | ".join(their_angle_parts) or role
    our_angle = str(draft_obj.get("cases") or f"Отклик на вакансию ({item.get('mode') or 'AI UGC'})").strip()

    note_lines = []
    if why:
        note_lines.append(f"Суть: {why}")
    if pay:
        note_lines.append(f"Оплата: {pay}")
    if url:
        note_lines.append(f"Пост: {url}")

    display_name = f"{role} (@{username})" if username else role
    return {
        "username": username or None,
        "name": display_name,
        "company": role,
        "phone": phone or None,
        "email": email or None,
        "website": url or None,
        "url": url or None,
        "category": layer or None,
        "niche": layer or None,
        "their_angle": their_angle,
        "our_angle": our_angle,
        "hook": first_line,
        "step1_text": step1_text,
        "step2_text": step2_text,
        "step3_text": step3_text,
        "lead_source": f"TG Вакансии ({src})",
        "note": "\n".join(note_lines) if note_lines else None,
        "provider": contact_info["provider"] or "telegram",
    }


def _find_latest_monitor_report() -> str | None:
    candidates = []
    for folder in (STATE_DIR, os.path.join(VAULT, ".raw", "data")):
        if os.path.isdir(folder):
            candidates.extend(glob.glob(os.path.join(folder, "job_monitor_[0-9]*.json")))
    if not candidates:
        return None
    candidates.sort(key=lambda p: (os.path.basename(p), os.path.getmtime(p)))
    return candidates[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="Генерация откликов на вакансии и заливка в Foldesk CRM через MCP")
    ap.add_argument("report", nargs="?", help="job_monitor_<дата>.json; по умолчанию самый свежий")
    ap.add_argument("--from-replies", default="", help="Использовать уже готовый job_replies_<дата>.json без повторного вызова LLM")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="", help="Куда сохранить JSON с черновиками")
    ap.add_argument(
        "--touch-mode",
        choices=("direct", "micro_ping"),
        default=os.environ.get("JOBS_TOUCH_MODE", "direct"),
        help="Вариант А: direct (полный отклик в step 1, по умолчанию) или Вариант Б: micro_ping",
    )
    ap.add_argument("--campaign-name", default=DEFAULT_CAMPAIGN_NAME, help="Название кампании в CRM")
    ap.add_argument("--campaign-id", type=int, default=None)
    ap.add_argument("--no-campaign", action="store_true", help="Создать только сделки в CRM без добавления в кампанию рассылки")
    ap.add_argument("--no-crm", action="store_true", help="Не отправлять в CRM (только сгенерировать файл)")
    ap.add_argument("--mcp-url", default=os.environ.get("CRMTG_MCP_URL", ""), help="HTTP MCP URL")
    ap.add_argument("--db-path", default=os.environ.get("CRM_DB_PATH", ""), help="Путь к SQLite БД CRM")
    a = ap.parse_args()

    load_env()
    path = a.report or _find_latest_monitor_report()
    if not path or not os.path.exists(path):
        sys.exit("данных монитора нет — сначала запустите job_monitor.py")

    items = json.load(open(path, encoding="utf-8"))
    if a.limit:
        items = items[: a.limit]
    print(f"отчёт: {os.path.basename(path)} · вакансий: {len(items)}\n")
    if not items:
        print("откликать нечего")
        return 0

    existing_replies_by_key: dict[Any, dict[str, Any]] = {}
    if a.from_replies and os.path.exists(a.from_replies):
        for r in json.load(open(a.from_replies, encoding="utf-8")):
            key = r.get("hash") or (r.get("chat_id"), r.get("msg_id")) or r.get("contact")
            if key:
                existing_replies_by_key[key] = r

    bank = load_bank()
    rows: list[dict[str, Any]] = []

    def _process_one(it: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        key = it.get("hash") or (it.get("chat_id"), it.get("msg_id")) or it.get("contact")
        if key and key in existing_replies_by_key:
            cached = existing_replies_by_key[key]
            f1_def, f2_def = build_default_followups(it)
            text_val = str(cached.get("text") or "").strip()
            return it, {
                "text": text_val,
                "followup_1": str(cached.get("followup_1") or "").strip() or f1_def,
                "followup_2": str(cached.get("followup_2") or "").strip() or f2_def,
                "cases": str(cached.get("cases") or "").strip(),
                "risk": str(cached.get("risk") or "").strip(),
                "no_question": "?" not in text_val[-260:],
            }
        return it, draft(it, bank)

    with futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for it, d in ex.map(_process_one, items):
            if d.get("error"):
                print(f"  ✗ {(it.get('role') or '')[:48]} — {d['error']}")
                continue
            flag = " ⚠ без вопроса" if d.get("no_question") else ""
            print(f"  ✓ {(it.get('role') or '')[:48]}{flag}")
            rows.append({"item": it, "draft": d})

    if not rows:
        sys.exit("ни одного черновика не вышло")

    os.makedirs(STATE_DIR, exist_ok=True)
    out = a.out or os.path.join(STATE_DIR, f"job_replies_{dt.date.today()}.json")
    serialized_replies = []
    mcp_leads = []

    for r in rows:
        it = r["item"]
        d = r["draft"]
        mcp_lead = build_vacancy_mcp_lead(it, d, touch_mode=a.touch_mode)
        if mcp_lead is not None:
            mcp_leads.append(mcp_lead)
        serialized_replies.append(
            {
                "msg_id": it.get("msg_id"),
                "chat_id": it.get("chat_id"),
                "hash": it.get("hash"),
                "contact": it.get("contact", ""),
                "role": it.get("role", ""),
                "text": d["text"],
                "followup_1": d.get("followup_1", ""),
                "followup_2": d.get("followup_2", ""),
                "step1_text": mcp_lead["step1_text"] if mcp_lead else d["text"],
                "step2_text": mcp_lead["step2_text"] if mcp_lead else d.get("followup_1", ""),
                "step3_text": mcp_lead["step3_text"] if mcp_lead else d.get("followup_2", ""),
                "no_question": d.get("no_question", False),
            }
        )

    json.dump(serialized_replies, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nчерновиков: {len(rows)}\nданные: {out}")

    if not a.no_crm and mcp_leads:
        resolved_db: str | Path | None = a.db_path or None
        if resolved_db is None and "PYTEST_CURRENT_TEST" not in os.environ and PROD_LOCAL_DB.exists():
            resolved_db = PROD_LOCAL_DB

        res = push_leads_via_mcp(
            mcp_leads,
            enroll_in_campaign=not a.no_campaign,
            campaign_id=a.campaign_id,
            campaign_name=a.campaign_name,
            mcp_url=a.mcp_url or None,
            db_path=resolved_db,
        )
        print(
            f"CRM MCP ({a.campaign_name}, mode={a.touch_mode}): "
            f"processed={res.get('processed', 0)}, "
            f"created_leads={res.get('created_leads', 0)}, "
            f"updated_leads={res.get('updated_leads', 0)}, "
            f"enrolled_in_campaign={res.get('enrolled_in_campaign', 0)}, "
            f"campaign_id={res.get('campaign_id')}"
        )

        # Если писали напрямую в локальную SQLite БД (без HTTP MCP), синхронизируем R2 снэпшот
        if not a.mcp_url and "PYTEST_CURRENT_TEST" not in os.environ:
            try:
                from app.cloud_persistence import backup_to_cloud
                from app.db import connect

                with connect(resolved_db) as conn:
                    backup_to_cloud(conn)
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
