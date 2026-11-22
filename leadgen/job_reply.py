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
from contextlib import closing
import concurrent.futures as futures
import datetime as dt
import glob
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

try:
    from leadgen.crm_mcp_push import _extract_contact_fields, push_leads_via_mcp
    from leadgen.job_monitor import OUTDIR as STATE_DIR, ask_llm, load_env, resolve_enabled_layers
except ImportError:
    from crm_mcp_push import _extract_contact_fields, push_leads_via_mcp
    from job_monitor import OUTDIR as STATE_DIR, ask_llm, load_env, resolve_enabled_layers

VAULT = "/Users/timofei/obsidian/Second Brain"
LOCAL_BANK = WORKTREE_ROOT / "leadgen" / "job_cases_bank.md"
VAULT_BANK = Path(VAULT) / "!ПРОЕКТЫ" / "Кейсы — банк для откликов.md"
DEFAULT_SHOWCASE_URL = "https://tmxfy.github.io/"


def _load_vacancies_ui_config() -> dict[str, Any]:
    try:
        from app.leadgen_settings import get_leadgen_config

        cfg = get_leadgen_config()
        vac = dict(cfg.get("vacancies") or {})
        vac["cases_bank_md"] = cfg.get("cases_bank_md") or ""
        return vac
    except Exception:
        pass
    cfg_path = Path(os.environ.get("LEADGEN_CONFIG_PATH") or (WORKTREE_ROOT / "data" / "leadgen_config.json"))
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                vac = dict(data.get("vacancies") or {})
                vac["cases_bank_md"] = data.get("cases_bank_md") or ""
                return vac
        except Exception:
            pass
    return {}


DEFAULT_CAMPAIGN_NAME = os.environ.get("JOBS_CAMPAIGN_NAME") or str(
    _load_vacancies_ui_config().get("campaign_name") or "TG Вакансии"
)
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

PROMPT = """Ты пишешь отклик и два коротких фоллоу-апа на вакансию ЗА ТИМА (бренд SoftUGC) для рассылки в CRM.

Кто такой Тим: практик AI UGC и основатель SoftUGC. Строит систему лидгена под ключ:
AI-ролики и короткие вертикалки (Reels / Shorts / TikTok) + воронка в ManyChat / Telegram +
аналитика конверсий. Он архитектор и владелец, руками работает его команда креаторов.
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

═══ ПРАВИЛА ДЛЯ ПЕРВОГО СООБЩЕНИЯ (text / Step 1) ═══
СКЕЛЕТ:
1. «Добрый день! По <конкретной вакансии>.» — назвать её словами заказчика
2. Позиционирование «система лидгена»: одно-два предложения, что строим не просто разовые
   ролики, а связку Reels/Shorts + воронка в ManyChat/TG + аналитика (AI UGC + своя команда)
3. «Что делал:» — от двух до четырёх релевантных кейсов с цифрами ПОД ЭТУ вакансию
4. Блок ссылок:
   — всегда сайт-витрина: {showcase_url} и PDF кейсов https://drive.google.com/file/d/1Aet4uXbGJwtDeJoh1fep0xeSlMTAH65w/view
   — видео и креаторские вакансии, плюсом: папка с видео https://drive.google.com/drive/folders/1EhiDsB4E4AJ6sgpn5_TcXBk0ObHyw212
   — технические и воронковые, плюсом: Notion https://destiny-occupation-0eb.notion.site/2418af798589806abbccd94a57083c2f?v=2418af798589809389ed000c2f3f02ac
5. Предложение тестового запуска/пилота под их задачу и ровно один вопрос по делу в конце (объём, ниша, формат, метрика, ТЗ)

ЖЕЛЕЗНЫЕ ЗАПРЕТЫ:
— Везде, где в задаче есть креаторы или UGC — заходить с «система лидгена на AI UGC» (ролики + воронка ManyChat + аналитика), вторым слоем своя команда.
— Экономику «минута AI UGC ≈ 0,5$ генераций» вставлять ТОЛЬКО если заказчик выбирает между живыми съёмками и AI в найме.
— Не называть в холодном Step 1 себестоимость (600 ₽ за ролик), «15 минут на ролик» и цену тестовой серии $80–$100 (прайс теста и 10-сек фрагмент — только в Step 2!).
— Без штампов ИИ («не просто X, а Y», «комплексный», «уникальный», «ключевой»), без эмодзи, без лести.

═══ ПРАВИЛА ДЛЯ ФОЛЛОУ-АПОВ (followup_1 и followup_2) ═══
— followup_1 (Step 2, через 24 часа, если не ответили): 1–2 коротких предложения в спокойном деловом тоне. Без «просто напоминаю о себе». Предложи начать с тестового фрагмента 10–15 секунд по их референсу/ТЗ или запустить тестовую серию ($80–$100), чтобы проверить качество и отклик в воронке до основного объёма.
— followup_2 (Step 3, через 72 часа, финальный пинг): 1–2 коротких предложения. Спокойно уточни, открыта ли ещё задача по {role} или уже взяли исполнителя; если актуально — предложи быстрый созвон на 10 минут.

Верни СТРОГО один JSON без пояснений:
{{"text": "готовый текст первого отклика целиком, с переносами строк",
  "followup_1": "короткий фоллоу-ап через 24ч с предложением 10-сек фрагмента или тестовой серии $80-$100",
  "followup_2": "короткий финальный фоллоу-ап через 72ч (1–2 предложения)",
  "cases": "какие цифры взял и почему именно они, одной строкой",
  "risk": "чем отклик может не зайти, одной строкой; нечем — пусто"}}"""


def _sanitize_brand_copy(text: str) -> str:
    return re.sub(r"\bCreator\s*OS\b|creatoros", "SoftUGC", str(text or ""), flags=re.IGNORECASE)


def parse_custom_step_templates(raw: Any) -> dict[str, str]:
    """Parse `custom_step_templates_json` (JSON string or mapping) into normalized override keys."""
    if raw is None or raw == "":
        return {}
    data: Any = raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return {}
    if not isinstance(data, Mapping):
        return {}

    out: dict[str, str] = {}
    for canonical, aliases in (
        ("step1", ("step1_text", "step1", "text")),
        ("step2", ("step2_text", "step2", "followup_1")),
        ("step3", ("step3_text", "step3", "followup_2")),
        ("editable_prompt_override", ("editable_prompt_override", "prompt_override", "system_prompt")),
        ("showcase_url", ("showcase_url",)),
    ):
        for key in aliases:
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                out[canonical] = val.strip()
                break
    return out


def load_campaign_copy_overrides(
    conn_or_db_path: Any = None,
    *,
    campaign_id: int | None = None,
    campaign_name: str | None = None,
) -> dict[str, str]:
    """Load `campaigns.custom_step_templates_json` from SQLite cleanly without failing on older schemas."""
    if conn_or_db_path is None:
        return {}

    opened_conn: sqlite3.Connection | None = None
    conn: sqlite3.Connection
    if isinstance(conn_or_db_path, (str, Path)):
        db_file = Path(conn_or_db_path)
        if not db_file.exists():
            return {}
        try:
            opened_conn = sqlite3.connect(str(db_file))
            opened_conn.row_factory = sqlite3.Row
            conn = opened_conn
        except sqlite3.Error:
            return {}
    elif isinstance(conn_or_db_path, sqlite3.Connection):
        conn = conn_or_db_path
    else:
        return {}

    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
        if "custom_step_templates_json" not in cols:
            return {}
        row = None
        if campaign_id is not None:
            row = conn.execute(
                "SELECT custom_step_templates_json FROM campaigns WHERE id = ?",
                (int(campaign_id),),
            ).fetchone()
        elif campaign_name:
            row = conn.execute(
                "SELECT custom_step_templates_json FROM campaigns WHERE lower(trim(name)) = lower(trim(?)) ORDER BY id ASC LIMIT 1",
                (str(campaign_name),),
            ).fetchone()
        if row is None:
            return {}
        raw_val = row["custom_step_templates_json"] if isinstance(row, sqlite3.Row) else row[0]
        return parse_custom_step_templates(raw_val)
    except sqlite3.Error:
        return {}
    finally:
        if opened_conn is not None:
            opened_conn.close()


def resolve_vacancy_copy_settings(
    *,
    vac_cfg: Mapping[str, Any] | None = None,
    campaign_overrides: Any = None,
    db_path: str | Path | None = None,
    conn: Any = None,
    campaign_id: int | None = None,
    campaign_name: str | None = None,
) -> dict[str, str]:
    """Merge editable vacancy templates, prompt override, and showcase URL from config & campaign."""
    cfg_vac: Mapping[str, Any] = vac_cfg if vac_cfg is not None else _load_vacancies_ui_config()
    if isinstance(cfg_vac, Mapping) and isinstance(cfg_vac.get("vacancies"), Mapping):
        cfg_vac = cfg_vac["vacancies"]

    settings: dict[str, str] = {
        "showcase_url": (os.environ.get("VACANCY_SHOWCASE_URL") or "").strip() or DEFAULT_SHOWCASE_URL,
        "editable_prompt_override": "",
        "step1_template": "",
        "step2_template": "",
        "step3_template": "",
    }

    def _apply_parsed(parsed: dict[str, str]) -> None:
        if parsed.get("showcase_url"):
            settings["showcase_url"] = parsed["showcase_url"]
        if parsed.get("editable_prompt_override"):
            settings["editable_prompt_override"] = parsed["editable_prompt_override"]
        if parsed.get("step1"):
            settings["step1_template"] = parsed["step1"]
        if parsed.get("step2"):
            settings["step2_template"] = parsed["step2"]
        if parsed.get("step3"):
            settings["step3_template"] = parsed["step3"]

    # 1. Config-level overrides (`vacancies`)
    if isinstance(cfg_vac, Mapping):
        _apply_parsed(
            parse_custom_step_templates(
                cfg_vac.get("custom_step_templates_json") or cfg_vac.get("custom_step_templates")
            )
        )
        cfg_showcase = str(cfg_vac.get("showcase_url") or "").strip()
        if cfg_showcase:
            settings["showcase_url"] = cfg_showcase
        cfg_prompt = str(cfg_vac.get("editable_prompt_override") or "").strip()
        if cfg_prompt:
            settings["editable_prompt_override"] = cfg_prompt

    # 2. Campaign DB row overrides (`campaigns.custom_step_templates_json`)
    if conn is not None or db_path is not None:
        _apply_parsed(
            load_campaign_copy_overrides(
                conn if conn is not None else db_path,
                campaign_id=campaign_id,
                campaign_name=campaign_name,
            )
        )

    # 3. Direct caller overrides
    if campaign_overrides is not None:
        _apply_parsed(parse_custom_step_templates(campaign_overrides))

    return settings


def render_vacancy_template(
    template_text: str,
    item: Mapping[str, Any],
    *,
    showcase_url: str = DEFAULT_SHOWCASE_URL,
) -> str:
    """Safely render user-editable vacancy templates with item fields and showcase URL."""
    role = str(item.get("role") or item.get("head") or "вакансии").strip().lstrip("🔥 ").strip()
    values = {
        "role": role,
        "company": str(item.get("company") or role).strip(),
        "why": str(item.get("why") or "").strip(),
        "pay": str(item.get("pay") or "").strip(),
        "contact": str(item.get("contact") or "").strip(),
        "src": str(item.get("src") or "").strip(),
        "url": str(item.get("url") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "showcase_url": (showcase_url or DEFAULT_SHOWCASE_URL).strip(),
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return values[key] if key in values else match.group(0)

    rendered = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _replace, str(template_text or ""))
    return _sanitize_brand_copy(rendered).strip()


def load_bank() -> str:
    vac_cfg = _load_vacancies_ui_config()
    if vac_cfg.get("cases_bank_md"):
        return str(vac_cfg["cases_bank_md"])
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


def build_default_step1(
    item: Mapping[str, Any],
    *,
    showcase_url: str = DEFAULT_SHOWCASE_URL,
    copy_settings: Mapping[str, Any] | None = None,
) -> str:
    """Deterministic RU Step 1 pitch: cases + showcase link + leadgen system positioning + test offer."""
    settings = dict(copy_settings) if copy_settings is not None else resolve_vacancy_copy_settings()
    effective_showcase = (showcase_url or settings.get("showcase_url") or DEFAULT_SHOWCASE_URL).strip()
    if settings.get("step1_template"):
        return render_vacancy_template(settings["step1_template"], item, showcase_url=effective_showcase)

    role = str(item.get("role") or item.get("head") or "вакансии").strip().lstrip("🔥 ").strip()
    text = (
        f"Добрый день! По задаче «{role}».\n\n"
        "В SoftUGC строим систему лидгена под ключ: AI UGC и короткие вертикальные ролики "
        "(Reels / Shorts / TikTok) + воронка в ManyChat / Telegram + аналитика конверсий "
        "(AI-продакшн + своя команда креаторов).\n\n"
        "Что делал:\n"
        "— Faceless-канал на AI-роликах: 183,5 млн просмотров за 90 дней, 241K подписчиков, 94,8% холодный охват\n"
        "— Конвейер под ключ для клиентов: до 90 роликов/мес с ростом охватов ×2–×3\n\n"
        f"Сайт-витрина и кейсы: {effective_showcase}\n\n"
        "Можем начать с тестового запуска под ваш продукт, чтобы проверить метрики в деле. "
        "Какой объём роликов в месяц планируете и какая сейчас цель по воронке?"
    )
    return _sanitize_brand_copy(text)


def build_default_followups(
    item: Mapping[str, Any],
    *,
    copy_settings: Mapping[str, Any] | None = None,
    vac_cfg: Mapping[str, Any] | None = None,
    campaign_overrides: Any = None,
) -> tuple[str, str]:
    settings = (
        dict(copy_settings)
        if copy_settings is not None
        else resolve_vacancy_copy_settings(vac_cfg=vac_cfg, campaign_overrides=campaign_overrides)
    )
    showcase = settings.get("showcase_url") or DEFAULT_SHOWCASE_URL
    role = (item.get("role") or "вакансии").strip()
    layer = (item.get("layer") or "").strip().lower()

    if settings.get("step2_template"):
        f1 = render_vacancy_template(settings["step2_template"], item, showcase_url=showcase)
    elif "автоматиз" in layer or "бот" in role.lower():
        f1 = (
            f"Добрый день! Возвращаюсь по задаче ({role}). "
            "Если скинете короткое описание текущей воронки или ТЗ, сразу скажу по срокам и архитектуре."
        )
    else:
        f1 = (
            f"Добрый день! Возвращаюсь по задаче ({role}). "
            "Можем начать с тестового фрагмента 10–15 секунд по вашему референсу/ТЗ "
            "или запустить тестовую серию ($80–$100), чтобы проверить качество и отклик в воронке до основного объёма."
        )

    if settings.get("step3_template"):
        f2 = render_vacancy_template(settings["step3_template"], item, showcase_url=showcase)
    else:
        f2 = (
            f"Подскажите, задача по «{role}» ещё открыта или уже нашли исполнителя? "
            "Если актуально — можем созвониться на 10 минут и обсудить запуск."
        )
    return _sanitize_brand_copy(f1), _sanitize_brand_copy(f2)


def build_micro_ping(item: Mapping[str, Any]) -> str:
    role = (item.get("role") or "задаче").strip()
    return _sanitize_brand_copy(
        f"Добрый день! Подскажите, ещё ищете исполнителя по задаче «{role}»? Могу скинуть релевантные кейсы и примеры."
    )


def build_reply_prompt(
    item: Mapping[str, Any],
    bank: str,
    *,
    copy_settings: Mapping[str, Any] | None = None,
    vac_cfg: Mapping[str, Any] | None = None,
    campaign_overrides: Any = None,
) -> str:
    """Build the vacancy reply prompt incorporating `showcase_url` and `editable_prompt_override`."""
    settings = (
        dict(copy_settings)
        if copy_settings is not None
        else resolve_vacancy_copy_settings(vac_cfg=vac_cfg, campaign_overrides=campaign_overrides)
    )
    showcase_url = settings.get("showcase_url") or DEFAULT_SHOWCASE_URL
    override = (settings.get("editable_prompt_override") or "").strip()

    ctx = {
        "bank": bank,
        "examples": EXAMPLES,
        "role": item.get("role") or "не разобрано",
        "why": item.get("why") or "",
        "pay": item.get("pay") or "не названа",
        "contact": item.get("contact") or "нет",
        "mode": item.get("mode") or "не разобрано",
        "text": (item.get("text") or "")[:3500],
        "showcase_url": showcase_url,
    }

    if override and "{text}" in override:
        rendered = re.sub(
            r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0),
            override,
        )
        if "JSON" not in rendered:
            rendered += (
                '\n\nВерни СТРОГО один JSON: {"text": "...", "followup_1": "...", '
                '"followup_2": "...", "cases": "...", "risk": "..."}'
            )
        return _sanitize_brand_copy(rendered)

    base_prompt = PROMPT.format(**ctx)
    if override:
        base_prompt = base_prompt.replace(
            "═══ ВАКАНСИЯ ═══",
            f"═══ ПОЛЬЗОВАТЕЛЬСКИЕ ИНСТРУКЦИИ (ПРИОРИТЕТ) ═══\n{override}\n\n═══ ВАКАНСИЯ ═══",
        )
    return _sanitize_brand_copy(base_prompt)


def draft(
    item: dict[str, Any],
    bank: str,
    *,
    vac_cfg: Mapping[str, Any] | None = None,
    campaign_overrides: Any = None,
    copy_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    settings = (
        dict(copy_settings)
        if copy_settings is not None
        else resolve_vacancy_copy_settings(vac_cfg=vac_cfg, campaign_overrides=campaign_overrides)
    )
    f1_def, f2_def = build_default_followups(item, copy_settings=settings)
    if settings.get("step1_template"):
        text = render_vacancy_template(
            settings["step1_template"],
            item,
            showcase_url=settings.get("showcase_url") or DEFAULT_SHOWCASE_URL,
        )
        return {
            "text": text,
            "followup_1": f1_def,
            "followup_2": f2_def,
            "cases": "custom_step_templates",
            "risk": "",
            "no_question": "?" not in text[-260:],
        }

    prompt = build_reply_prompt(item, bank, copy_settings=settings)
    try:
        res = ask_llm(prompt, timeout=360)
    except Exception as e:
        return {"error": f"модель не ответила: {type(e).__name__}: {e}"}
    d = _json_block(res)
    if not isinstance(d, dict) or not (d.get("text") or "").strip():
        return {"error": "ответ без JSON"}

    text = _sanitize_brand_copy(str(d["text"]).strip())
    d["text"] = text
    d["followup_1"] = _sanitize_brand_copy(
        f1_def if settings.get("step2_template") else (str(d.get("followup_1") or "").strip() or f1_def)
    )
    d["followup_2"] = _sanitize_brand_copy(
        f2_def if settings.get("step3_template") else (str(d.get("followup_2") or "").strip() or f2_def)
    )
    d["no_question"] = "?" not in text[-260:]
    return d


def build_vacancy_mcp_lead(
    item: dict[str, Any],
    draft_obj: dict[str, Any],
    *,
    touch_mode: str | None = None,
    copy_settings: Mapping[str, Any] | None = None,
    vac_cfg: Mapping[str, Any] | None = None,
    campaign_overrides: Any = None,
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

    settings = (
        dict(copy_settings)
        if copy_settings is not None
        else resolve_vacancy_copy_settings(vac_cfg=vac_cfg, campaign_overrides=campaign_overrides)
    )
    effective_showcase = settings.get("showcase_url") or DEFAULT_SHOWCASE_URL

    if settings.get("step1_template"):
        full_reply = render_vacancy_template(
            settings["step1_template"],
            item,
            showcase_url=effective_showcase,
        )
    else:
        full_reply = _sanitize_brand_copy(str(draft_obj.get("text") or "").strip())
        if full_reply and effective_showcase != DEFAULT_SHOWCASE_URL and DEFAULT_SHOWCASE_URL in full_reply:
            full_reply = full_reply.replace(DEFAULT_SHOWCASE_URL, effective_showcase)
    if not full_reply:
        return None

    f1_def, f2_def = build_default_followups(item, copy_settings=settings)
    followup_1 = _sanitize_brand_copy(
        f1_def if settings.get("step2_template") else (str(draft_obj.get("followup_1") or "").strip() or f1_def)
    )
    followup_2 = _sanitize_brand_copy(
        f2_def if settings.get("step3_template") else (str(draft_obj.get("followup_2") or "").strip() or f2_def)
    )

    ui_cfg = vac_cfg if vac_cfg is not None else _load_vacancies_ui_config()
    resolved_mode = touch_mode or os.environ.get("JOBS_TOUCH_MODE") or str(
        (ui_cfg or {}).get("touch_mode") or "direct"
    )
    mode = str(resolved_mode or "direct").strip().lower()
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
    our_angle = _sanitize_brand_copy(
        str(draft_obj.get("cases") or f"Отклик на вакансию ({item.get('mode') or 'AI UGC'})").strip()
    )

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
        "segment": str(item.get("segment") or "vacancy"),
        "language": str(item.get("language") or "ru"),
        "source": str(item.get("source") or "tg_jobs"),
        "their_angle": their_angle,
        "our_angle": our_angle,
        "hook": first_line,
        "step1_text": step1_text,
        "step2_text": step2_text,
        "step3_text": step3_text,
        "lead_source": f"TG Вакансии ({src})",
        "note": "\n".join(note_lines) if note_lines else None,
        "provider": contact_info["provider"] or "telegram",
        "posted_at": str(item.get("posted_at") or item.get("date") or "").strip() or None,
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
    vac_cfg = _load_vacancies_ui_config()
    default_touch = os.environ.get("JOBS_TOUCH_MODE") or str(vac_cfg.get("touch_mode") or "direct")
    default_camp = os.environ.get("JOBS_CAMPAIGN_NAME") or str(vac_cfg.get("campaign_name") or DEFAULT_CAMPAIGN_NAME)
    raw_cfg_limit = vac_cfg.get("daily_limit")
    cfg_limit = int(raw_cfg_limit) if raw_cfg_limit is not None else None

    ap = argparse.ArgumentParser(description="Генерация откликов на вакансии и заливка в Foldesk CRM через MCP")
    ap.add_argument("report", nargs="?", help="job_monitor_<дата>.json; по умолчанию самый свежий")
    ap.add_argument("--from-replies", default="", help="Использовать уже готовый job_replies_<дата>.json без повторного вызова LLM")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="Запустить даже если вакансии отключены в настройках")
    ap.add_argument("--out", default="", help="Куда сохранить JSON с черновиками")
    ap.add_argument(
        "--touch-mode",
        choices=("direct", "micro_ping"),
        default=default_touch,
        help="Вариант А: direct (полный отклик в step 1, по умолчанию) или Вариант Б: micro_ping",
    )
    ap.add_argument("--campaign-name", default=default_camp, help="Название кампании в CRM")
    ap.add_argument("--campaign-id", type=int, default=None)
    ap.add_argument("--no-campaign", action="store_true", help="Создать только сделки в CRM без добавления в кампанию рассылки")
    ap.add_argument("--no-crm", action="store_true", help="Не отправлять в CRM (только сгенерировать файл)")
    ap.add_argument("--mcp-url", default=os.environ.get("CRMTG_MCP_URL", ""), help="HTTP MCP URL")
    ap.add_argument("--db-path", default=os.environ.get("CRM_DB_PATH", ""), help="Путь к SQLite БД CRM")
    a = ap.parse_args()

    load_env()
    if not a.force and "enabled" in vac_cfg and not bool(vac_cfg.get("enabled")):
        print("отклики на вакансии отключены в настройках (используйте --force для ручного прогона)")
        return 0

    eff_limit = a.limit if a.limit is not None else cfg_limit
    if eff_limit is not None and eff_limit <= 0 and not a.force:
        print("дневной лимит вакансий = 0 — отклики остановлены")
        return 0

    path = a.report or _find_latest_monitor_report()
    if not path or not os.path.exists(path):
        sys.exit("данных монитора нет — сначала запустите job_monitor.py")

    resolved_db: str | Path | None = a.db_path or None
    if resolved_db is None and os.environ.get("CRMTG_EPHEMERAL_RUNTIME") != "1" and PROD_LOCAL_DB.exists():
        resolved_db = PROD_LOCAL_DB

    copy_settings = resolve_vacancy_copy_settings(
        vac_cfg=vac_cfg,
        db_path=resolved_db,
        campaign_id=a.campaign_id,
        campaign_name=a.campaign_name,
    )

    items = json.load(open(path, encoding="utf-8"))
    enabled_layers = resolve_enabled_layers()
    if enabled_layers and not (a.force and a.from_replies):
        items = [
            it
            for it in items
            if not (it.get("layer") or "").strip() or (it.get("layer") or "").strip() in enabled_layers
        ]
    if vac_cfg.get("remote_only", True):
        items = [it for it in items if it.get("remote") is not False]
    if vac_cfg.get("hot_priority", True):
        items = sorted(items, key=lambda it: not bool((it or {}).get("hot")))
    if eff_limit is not None and eff_limit > 0:
        items = items[:eff_limit]
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
            f1_def, f2_def = build_default_followups(it, copy_settings=copy_settings)
            text_val = _sanitize_brand_copy(str(cached.get("text") or "").strip())
            if copy_settings.get("step1_template"):
                text_val = render_vacancy_template(
                    copy_settings["step1_template"],
                    it,
                    showcase_url=copy_settings.get("showcase_url") or DEFAULT_SHOWCASE_URL,
                )
            elif (
                text_val
                and copy_settings.get("showcase_url", DEFAULT_SHOWCASE_URL) != DEFAULT_SHOWCASE_URL
                and DEFAULT_SHOWCASE_URL in text_val
            ):
                text_val = text_val.replace(DEFAULT_SHOWCASE_URL, copy_settings["showcase_url"])
            return it, {
                "text": text_val,
                "followup_1": _sanitize_brand_copy(
                    f1_def if copy_settings.get("step2_template") else (str(cached.get("followup_1") or "").strip() or f1_def)
                ),
                "followup_2": _sanitize_brand_copy(
                    f2_def if copy_settings.get("step3_template") else (str(cached.get("followup_2") or "").strip() or f2_def)
                ),
                "cases": str(cached.get("cases") or "").strip(),
                "risk": str(cached.get("risk") or "").strip(),
                "no_question": "?" not in text_val[-260:],
            }
        return it, draft(it, bank, copy_settings=copy_settings)

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
        mcp_lead = build_vacancy_mcp_lead(it, d, touch_mode=a.touch_mode, copy_settings=copy_settings)
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

        # Если писали напрямую в локальную SQLite БД (без HTTP MCP), синхронизируем R2 снэпшот только при активном lease
        if not a.mcp_url and resolved_db is not None:
            from app.cloud_persistence import (
                backup_to_cloud,
                check_tg_runtime_lease_owned,
            )
            from app.db import connect

            try:
                with closing(connect(resolved_db)) as conn:
                    if check_tg_runtime_lease_owned(conn):
                        backup_to_cloud(conn, Path(resolved_db))
            except Exception as exc:
                print(f"  ⚠ R2 backup skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
