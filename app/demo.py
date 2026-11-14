from datetime import datetime, timedelta, timezone
import json

from app.db import utc_now, utc_today
from app.properties import create_property_def
from app.views import save_view

DEMO_PHONE = "+79995550101"
DEMO_SESSION = "data/sessions/demo.session"
DEMO_TG_ID = 700100

DEMO2_PHONE = "+79995550202"
DEMO2_SESSION = "data/sessions/demo2.session"
DEMO2_TG_ID = 700101

DEMO3_PHONE = "+79990003344"
DEMO3_SESSION = "data/sessions/demo3.session"
DEMO3_TG_ID = 700102

DEMO_WA_PHONE = "+79990007788"
DEMO_WA_SESSION = "data/sessions/demo_wa.session"
DEMO_WA_TG_ID = 700103
DEMO_WA_JID = "79990007788@s.whatsapp.net"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _cleanup_existing_demo(conn) -> None:
    lead_tg_ids = tuple(range(800101, 800125)) + (
        79993014455,
        79993025566,
        79993036677,
    )
    acc_rows = conn.execute(
        "SELECT id FROM accounts WHERE phone IN (?, ?, ?, ?) OR session_path IN (?, ?, ?, ?)",
        (
            DEMO_PHONE,
            DEMO2_PHONE,
            DEMO3_PHONE,
            DEMO_WA_PHONE,
            DEMO_SESSION,
            DEMO2_SESSION,
            DEMO3_SESSION,
            DEMO_WA_SESSION,
        ),
    ).fetchall()
    acc_ids = [row["id"] for row in acc_rows]

    conn.execute(
        "DELETE FROM campaigns WHERE name IN ('Web3 Founders · Осень', 'Партнёрские агентства · Follow-up')"
        + (" OR account_id IN ({})".format(",".join("?" for _ in acc_ids)) if acc_ids else ""),
        tuple(acc_ids),
    )

    lead_rows = conn.execute(
        f"SELECT id FROM leads WHERE telegram_user_id IN ({','.join('?' for _ in lead_tg_ids)})"
        + (" OR account_id IN ({})".format(",".join("?" for _ in acc_ids)) if acc_ids else ""),
        (*lead_tg_ids, *acc_ids),
    ).fetchall()
    lead_ids = [row["id"] for row in lead_rows]

    if lead_ids:
        placeholders = ",".join("?" for _ in lead_ids)
        conn.execute(f"DELETE FROM campaign_recipients WHERE lead_id IN ({placeholders})", lead_ids)
        conn.execute(f"DELETE FROM reminders WHERE lead_id IN ({placeholders})", lead_ids)
        conn.execute(f"DELETE FROM calendar_events WHERE lead_id IN ({placeholders})", lead_ids)
        conn.execute(f"DELETE FROM notes WHERE lead_id IN ({placeholders})", lead_ids)
        conn.execute(f"DELETE FROM tasks WHERE lead_id IN ({placeholders})", lead_ids)
        conn.execute(f"DELETE FROM property_values WHERE lead_id IN ({placeholders})", lead_ids)
        conn.execute(f"DELETE FROM lead_events WHERE lead_id IN ({placeholders})", lead_ids)
        conn.execute(f"UPDATE dialogs SET lead_id = NULL WHERE lead_id IN ({placeholders})", lead_ids)
        conn.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", lead_ids)
    conn.execute("DELETE FROM calendar_events WHERE source LIKE 'demo%'")

    if acc_ids:
        acc_ph = ",".join("?" for _ in acc_ids)
        conn.execute(
            f"DELETE FROM messages WHERE dialog_id IN (SELECT id FROM dialogs WHERE account_id IN ({acc_ph}))",
            acc_ids,
        )
        conn.execute(f"DELETE FROM dialogs WHERE account_id IN ({acc_ph})", acc_ids)
        conn.execute(f"DELETE FROM folders WHERE account_id IN ({acc_ph})", acc_ids)
        conn.execute(f"DELETE FROM property_values WHERE account_id IN ({acc_ph})", acc_ids)
        conn.execute(f"DELETE FROM accounts WHERE id IN ({acc_ph})", acc_ids)


def seed_demo(conn) -> dict:
    now_dt = datetime.now(timezone.utc)
    now = _iso(now_dt)
    today = utc_today()

    _cleanup_existing_demo(conn)

    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, last_error, updated_at, daily_limit, sent_today, sent_date
        ) VALUES (?, ?, 'connected', ?, 'tim_sales', 'Тимофей · Sales', ?, 'active', NULL, ?, 20, 7, ?)
        """,
        (DEMO_PHONE, DEMO_SESSION, DEMO_TG_ID, now, now, today),
    )
    account_id = cur.lastrowid

    cur2 = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, last_error, updated_at, daily_limit, sent_today, sent_date
        ) VALUES (?, ?, 'connected', ?, 'anna_outreach', 'Анна · Outreach', ?, 'active', NULL, ?, 10, 4, ?)
        """,
        (DEMO2_PHONE, DEMO2_SESSION, DEMO2_TG_ID, now, now, today),
    )
    account2_id = cur2.lastrowid

    cur3 = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, last_error, updated_at, daily_limit, sent_today, sent_date, provider
        ) VALUES (?, ?, 'connected', ?, 'maxim_growth_tg', 'Максим · Growth', ?, 'active', NULL, ?, 15, 3, ?, 'telegram')
        """,
        (DEMO3_PHONE, DEMO3_SESSION, DEMO3_TG_ID, now, now, today),
    )
    account3_id = cur3.lastrowid

    cur_wa = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, last_error, updated_at, daily_limit, sent_today, sent_date,
            provider, wa_jid
        ) VALUES (?, ?, 'connected', ?, NULL, 'Timofei · WhatsApp Business', ?, 'active', NULL, ?, 25, 5, ?, 'whatsapp', ?)
        """,
        (DEMO_WA_PHONE, DEMO_WA_SESSION, DEMO_WA_TG_ID, now, now, today, DEMO_WA_JID),
    )
    wa_account_id = cur_wa.lastrowid

    folders_spec = [
        (10, "VIP Клиенты", 1, None),
        (11, "Web3 & SaaS", 1, None),
        (12, "Партнёры и Агентства", 0, None),
    ]
    folder_map = {}
    for tg_folder_id, title, sync_enabled, blocked_reason in folders_spec:
        fcur = conn.execute(
            """
            INSERT INTO folders (
                account_id, telegram_folder_id, title, sync_enabled,
                blocked_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, tg_folder_id, title, sync_enabled, blocked_reason, now, now),
        )
        folder_map[title] = fcur.lastrowid

    leads_spec = [
        {
            "telegram_user_id": 800101,
            "username": "alex_ton",
            "name": "Алексей Соколов · TON Labs",
            "phone": "+79991012233",
            "status": "new",
            "note": "Интересует пакет на 15 аккаунтов с прогревом",
            "next_step": "Отправить презентацию и медиакит",
            "description": "Инфраструктурная команда в экосистеме TON, ищут CRM для отдела партнёрств.",
            "email": "alex@tonlabs.io",
            "url": "https://tonlabs.io",
            "folder": "Web3 & SaaS",
            "amount_text": "$2,400",
            "amount_cents": 240000,
            "company": "TON Labs",
            "notes": [
                "Пришёл с конференции TOKEN2049, просил расчёт на 15 сейлз-аккаунтов.",
                "Важно наличие синхронизации папок Telegram и командного доступа.",
            ],
            "tasks": [
                ("Собрать КП под Web3-команду (15 мест)", False),
                ("Отправить примеры дашборда в Telegram", False),
            ],
        },
        {
            "telegram_user_id": 800102,
            "username": "elena_s42",
            "name": "Елена Романова · Studio 42",
            "phone": "+79991023344",
            "status": "new",
            "note": "Пришла по рекомендации от Сергея",
            "next_step": "Квалифицировать по бюджету",
            "description": "Дизайн-студия, 6 сейлзов в Telegram, хотят общую воронку.",
            "email": "elena@studio42.design",
            "url": "https://studio42.design",
            "folder": "VIP Клиенты",
            "amount_text": "$1,200",
            "amount_cents": 120000,
            "company": "Studio 42",
            "notes": [
                "Пришла по рекомендации от Сергея из NovaPay, хотят быстро уйти из Google-таблиц.",
            ],
            "tasks": [
                ("Уточнить количество активных диалогов в день", False),
            ],
        },
        {
            "telegram_user_id": 800103,
            "username": "denis_fb",
            "name": "Денис Морозов · FinBridge",
            "phone": "+79991034455",
            "status": "talking",
            "note": "Обсудили условия пилота на 1 месяц",
            "next_step": "Созвон в Zoom в 16:00",
            "description": "Платежный шлюз для B2B, высокий чек, быстрый цикл сделки.",
            "email": "d.morozov@finbridge.io",
            "url": "https://finbridge.io",
            "folder": "VIP Клиенты",
            "amount_text": "$3,600",
            "amount_cents": 360000,
            "company": "FinBridge",
            "notes": [
                "Обсудили условия пилота на 1 месяц для 10 аккаунтов аккаунт-менеджеров.",
                "На созвоне показать утренний дайджест в Telegram-боте и фильтрацию по папкам.",
            ],
            "tasks": [
                ("Подготовить демо-стенд к созвону в Zoom", True),
                ("Согласовать SLA и выделенный инстанс", False),
            ],
        },
        {
            "telegram_user_id": 800104,
            "username": "artem_gdao",
            "name": "Артём Волков · GrowthDAO",
            "phone": "+79991045566",
            "status": "talking",
            "note": "Нужен экспорт вебхуков и кастомные поля",
            "next_step": "Согласовать ТЗ на интеграцию",
            "description": "Маркетинговое агентство для Web3-проектов, ведут 40+ чатов с клиентами.",
            "email": "artem@growthdao.xyz",
            "url": "https://growthdao.xyz",
            "folder": "Web3 & SaaS",
            "amount_text": "$2,900",
            "amount_cents": 290000,
            "company": "GrowthDAO",
            "notes": [
                "Команда ведёт 40+ личных чатов с фаундерами, теряют фоллоу-апы без напоминаний.",
            ],
            "tasks": [
                ("Проверить схему кастомных полей под их воронку", True),
                ("Отправить спецификацию API и вебхуков", False),
            ],
        },
        {
            "telegram_user_id": 800105,
            "username": "maria_adscale",
            "name": "Мария Лебедева · AdScale",
            "phone": "+79991056677",
            "status": "waiting",
            "note": "Счёт на $1,800 выставлен, бухгалтерия подтверждает",
            "next_step": "Ждём оплату инвойса #408",
            "description": "Performance-агентство, подключают 3 рабочих аккаунта менеджеров.",
            "email": "maria@adscale.agency",
            "url": "https://adscale.agency",
            "folder": "Партнёры и Агентства",
            "amount_text": "$1,800",
            "amount_cents": 180000,
            "company": "AdScale",
            "notes": [
                "Счёт #408 на $1,800 (квартальный тариф) отправлен в финансовый отдел.",
            ],
            "tasks": [
                ("Выставить инвойс #408 в USDT / безнал", True),
                ("Подготовить доступы для 3 руководителей групп", False),
            ],
        },
        {
            "telegram_user_id": 800106,
            "username": "ilya_c9",
            "name": "Илья Ковалёв · CloudNine",
            "phone": "+79991067788",
            "status": "waiting",
            "note": "Юристы смотрят NDA",
            "next_step": "Пингануть в четверг по договору",
            "description": "Облачный провайдер, переход из таблиц в единый Telegram-инбокс.",
            "email": "ilya@cloudnine.host",
            "url": "https://cloudnine.host",
            "folder": "Web3 & SaaS",
            "amount_text": "$2,100",
            "amount_cents": 210000,
            "company": "CloudNine",
            "notes": [
                "Юристы смотрят NDA и пункт про локальное хранение сессий.",
            ],
            "tasks": [
                ("Отправить подписанный скан NDA", True),
                ("Пингануть Илью в четверг по статусу договора", False),
            ],
        },
        {
            "telegram_user_id": 800107,
            "username": "viktor_novapay",
            "name": "Виктор Громов · NovaPay",
            "phone": "+79991078899",
            "status": "won",
            "note": "Оплачен годовой тариф Pro ($4,500)",
            "next_step": "Онбординг команды в пятницу",
            "description": "Финтех-платформа, годовой контракт на 25 рабочих мест.",
            "email": "v.gromov@novapay.global",
            "url": "https://novapay.global",
            "folder": "VIP Клиенты",
            "amount_text": "$4,500",
            "amount_cents": 450000,
            "company": "NovaPay",
            "notes": [
                "Оплачен годовой тариф Pro ($4,500), деньги поступили.",
                "Назначен общий онбординг сейлз-команды на пятницу в 14:00.",
            ],
            "tasks": [
                ("Проверить оплату годового контракта", True),
                ("Создать рабочие папки для 5 тимлидов", True),
                ("Провести финальный онбординг в пятницу", False),
            ],
        },
        {
            "telegram_user_id": 800108,
            "username": "max_retail",
            "name": "Максим Орлов · RetailBox",
            "phone": "+79991089900",
            "status": "lost",
            "note": "Заморозили бюджет до следующего квартала",
            "next_step": "Вернуться в Q1",
            "description": "Сеть автоматизации ритейла, отложили внедрение до нового бюджета.",
            "email": "m.orlov@retailbox.ru",
            "url": "https://retailbox.ru",
            "folder": "Партнёры и Агентства",
            "amount_text": "$1,500",
            "amount_cents": 150000,
            "company": "RetailBox",
            "notes": [
                "Заморозили ИТ-бюджет до следующего квартала, просили написать в начале Q1.",
            ],
            "tasks": [
                ("Поставить напоминание на возврат в Q1", True),
            ],
        },
        {
            "provider": "whatsapp",
            "account_id": wa_account_id,
            "telegram_user_id": 79993014455,
            "username": "kirill_boxberry_wa",
            "name": "Кирилл Сафонов · BoxBerry Logistics",
            "phone": "+79993014455",
            "status": "new",
            "note": "Пишет в WhatsApp по интеграции отдела логистики (12 номеров)",
            "next_step": "Отправить коммерческое предложение в WhatsApp",
            "description": "Федеральный логистический оператор, переводят поддержку и B2B-сейлзов в единый WhatsApp + Telegram инбокс.",
            "email": "k.safonov@boxberry-b2b.ru",
            "url": "https://boxberry.ru",
            "folder": None,
            "amount_text": "$3,200",
            "amount_cents": 320000,
            "company": "BoxBerry Logistics",
            "notes": [
                "Обратился в WhatsApp Business после вебинара, хотят подключить 12 номеров менеджеров.",
                "Критично видеть переписки WhatsApp и Telegram в одном окне.",
            ],
            "tasks": [
                ("Подготовить расчёт тарифа на 12 WhatsApp + Telegram линий", False),
                ("Показать QR-подключение WhatsApp на демо", False),
            ],
        },
        {
            "provider": "whatsapp",
            "account_id": wa_account_id,
            "telegram_user_id": 79993025566,
            "username": "alina_medline_wa",
            "name": "Алина Белова · MedLine Clinic",
            "phone": "+79993025566",
            "status": "talking",
            "note": "Согласовываем шаблон WhatsApp-рассылки по базе клиентов",
            "next_step": "Запустить тестовую батч-рассылку на 50 номеров",
            "description": "Сеть частных клиник премиум-сегмента, основная коммуникация с VIP-клиентами идёт в WhatsApp.",
            "email": "a.belova@medline.clinic",
            "url": "https://medline.clinic",
            "folder": None,
            "amount_text": "$2,700",
            "amount_cents": 270000,
            "company": "MedLine Clinic",
            "notes": [
                "Провели демо WhatsApp-инбокса, команде понравилась карточка сделки справа от чата.",
                "Готовим CSV-базу для первой сервисной рассылки.",
            ],
            "tasks": [
                ("Проверить CSV-шаблон с номерами WhatsApp", True),
                ("Запустить тестовый батч с дневным лимитом 25 сообщений", False),
            ],
        },
        {
            "provider": "whatsapp",
            "account_id": wa_account_id,
            "telegram_user_id": 79993036677,
            "username": "ruslan_auto_wa",
            "name": "Руслан Каримов · AutoImport Pro",
            "phone": "+79993036677",
            "status": "waiting",
            "note": "Ждём оплату счёта за полугодовой доступ (WhatsApp + Telegram)",
            "next_step": "Проверить зачисление средств и выдать доступы РОПу",
            "description": "Импорт автомобилей под ключ, отдел продаж общается с покупателями параллельно в WhatsApp и Telegram.",
            "email": "ruslan@autoimport.pro",
            "url": "https://autoimport.pro",
            "folder": None,
            "amount_text": "$1,950",
            "amount_cents": 195000,
            "company": "AutoImport Pro",
            "notes": [
                "Договорились на пакет 5 Telegram + 3 WhatsApp аккаунта, счёт выставлен на ИП.",
            ],
            "tasks": [
                ("Отправить договор и счёт в WhatsApp", True),
                ("Настроить рабочие места для 8 сейлзов после оплаты", False),
            ],
        },
    ]

    prop_amount = create_property_def(conn, name="Сумма сделки", type_="amount", object_type="lead")
    prop_company = create_property_def(conn, name="Компания", type_="text", object_type="lead")

    from app.events import record_lead_event

    lead_ids_by_username = {}
    for idx, item in enumerate(leads_spec):
        lead_provider = item.get("provider") or "telegram"
        lead_acc_id = item.get("account_id") or account_id
        if item["username"] == "maria_adscale":
            stamp = _iso(now_dt - timedelta(days=4, hours=3))
        elif item["username"] == "ilya_c9":
            stamp = _iso(now_dt - timedelta(days=5, hours=2))
        elif item["username"] == "ruslan_auto_wa":
            stamp = _iso(now_dt - timedelta(days=4, hours=1))
        elif lead_provider == "whatsapp":
            stamp = _iso(now_dt - timedelta(minutes=10 + idx * 3))
        else:
            stamp = _iso(now_dt - timedelta(minutes=(8 - idx) * 7))
        folder_id = folder_map[item["folder"]] if item.get("folder") else None
        lead_amount = int(item.get("amount_cents", 0) or 0) // 100
        lead_company = item.get("company")
        lead_niche = "Web3 & Crypto" if item.get("folder") == "Web3 & SaaS" else ("FinTech & B2B" if item.get("folder") == "VIP Клиенты" else "Performance & Agencies")
        lead_monthly = max(500, int(round(lead_amount * 0.45))) if lead_amount > 0 else 0
        lead_src = "Cold outreach" if idx % 2 == 0 else "Referral"
        follow_day = (now_dt + timedelta(days=(idx % 5) + 1)).strftime("%Y-%m-%d")
        lcur = conn.execute(
            """
            INSERT INTO leads (
                account_id, telegram_user_id, username, name, phone,
                status, note, next_step, created_at, updated_at,
                description, email, url, folder_id, provider, amount, currency,
                company, niche, monthly_value, lead_source, next_follow_up, offer_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_acc_id,
                item["telegram_user_id"],
                item["username"],
                item["name"],
                item["phone"],
                item["status"],
                item["note"],
                item["next_step"],
                stamp,
                stamp,
                item["description"],
                item["email"],
                item["url"],
                folder_id,
                lead_provider,
                lead_amount,
                lead_company,
                lead_niche,
                lead_monthly,
                lead_src,
                follow_day,
                json.dumps(["Performance"] if idx % 2 == 0 else ["Custom project"]),
            ),
        )
        lead_id = lcur.lastrowid
        lead_ids_by_username[item["username"]] = lead_id

        record_lead_event(
            conn,
            lead_id,
            "created",
            f"Создан лид {item['name']}",
            {
                "status": "new",
                "provider": lead_provider,
                "amount": lead_amount,
                "currency": "USD",
            },
            created_at=stamp,
        )
        if lead_amount > 0:
            record_lead_event(
                conn,
                lead_id,
                "amount_changed",
                f"Установлен бюджет: {item['amount_text']} USD",
                {"old_amount": 0, "new_amount": lead_amount, "currency": "USD"},
                created_at=stamp,
            )
        if item["status"] != "new":
            record_lead_event(
                conn,
                lead_id,
                "status_changed",
                f"Стадия изменена: new → {item['status']}",
                {"old_status": "new", "new_status": item["status"]},
                created_at=stamp,
            )

        conn.execute(
            """
            INSERT INTO property_values (
                property_id, lead_id, account_id, value_text, value_int, currency,
                created_at, updated_at
            ) VALUES (?, ?, NULL, ?, ?, 'USD', ?, ?)
            """,
            (prop_amount["id"], lead_id, item["amount_text"], item["amount_cents"], stamp, stamp),
        )
        conn.execute(
            """
            INSERT INTO property_values (
                property_id, lead_id, account_id, value_text, value_int, currency,
                created_at, updated_at
            ) VALUES (?, ?, NULL, ?, NULL, NULL, ?, ?)
            """,
            (prop_company["id"], lead_id, item["company"], stamp, stamp),
        )

        for n_idx, note_body in enumerate(item["notes"]):
            n_stamp = _iso(now_dt - timedelta(minutes=(len(leads_spec) - idx) * 6 - n_idx))
            conn.execute(
                """
                INSERT INTO notes (lead_id, body, source, created_at, updated_at)
                VALUES (?, ?, 'manual', ?, ?)
                """,
                (lead_id, note_body, n_stamp, n_stamp),
            )
            record_lead_event(
                conn,
                lead_id,
                "note_added",
                f"Добавлена заметка: {note_body[:80]}",
                {"body": note_body[:200], "source": "manual"},
                created_at=n_stamp,
            )

        for t_idx, (task_title, is_done) in enumerate(item["tasks"]):
            t_stamp = _iso(now_dt - timedelta(minutes=(len(leads_spec) - idx) * 5 - t_idx))
            done_at = t_stamp if is_done else None
            conn.execute(
                """
                INSERT INTO tasks (lead_id, title, done_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (lead_id, task_title, done_at, t_stamp, t_stamp),
            )

    dialogs_spec = [
        {
            "username": "denis_fb",
            "peer_id": 800103,
            "title": "Денис Морозов · FinBridge",
            "unread_count": 3,
            "last_from_peer": 1,
            "messages": [
                (1001, "in", "Тимофей, привет! Посмотрели с командой ваш пайплайн для Telegram.", -180),
                (1002, "out", "Денис, привет! Отлично, какие задачи для сейлзов сейчас самые острые?", -165),
                (1003, "in", "У нас 10 аккаунт-менеджеров ведут B2B-клиентов в личке. Теряем контекст и напоминания.", -140),
                (1004, "out", "В Foldesk как раз синхронизируются папки Telegram, есть карточки сделок у чата и утренний дайджест в боте.", -120),
                (1005, "in", "Звучит ровно как то, что нужно. Давай сегодня в 16:00 в Zoom обсудим старт пилота на месяц?", -15),
            ],
        },
        {
            "username": "alex_ton",
            "peer_id": 800101,
            "title": "Алексей Соколов · TON Labs",
            "unread_count": 1,
            "last_from_peer": 1,
            "messages": [
                (2001, "in", "Привет! Взял твой контакт после сессии по B2B-продажам в Telegram.", -240),
                (2002, "out", "Алексей, рад знакомству! Под какую команду присматриваете CRM?", -210),
                (2003, "in", "Отдел партнёрств и грантов, около 15 аккаунтов. Пришли презентацию и медиакит, пожалуйста.", -25),
            ],
        },
        {
            "username": "maria_adscale",
            "peer_id": 800105,
            "title": "Мария Лебедева · AdScale",
            "unread_count": 0,
            "last_from_peer": 0,
            "messages": [
                (3001, "in", "Тимофей, добрый день! Реквизиты передала в бухгалтерию, ждём инвойс.", -400),
                (3002, "out", "Мария, направил инвойс #408 на $1,800 (квартальный пакет для агентства).", -360),
                (3003, "in", "Получила, спасибо! Поставили в платёжный календарь на сегодня.", -90),
                (3004, "out", "Супер, как только пройдёт оплата — сразу откроем доступы вашим тимлидам.", -60),
            ],
        },
        {
            "username": "artem_gdao",
            "peer_id": 800104,
            "title": "Артём Волков · GrowthDAO",
            "unread_count": 0,
            "last_from_peer": 1,
            "messages": [
                (4001, "in", "Хей! Нам критично хранить сумму сделки и проект прямо рядом с перепиской.", -520),
                (4002, "out", "Артём, привет! Кастомные поля (`Сумма сделки`, `Компания` и любые свои) доступны в один клик прямо у чата.", -480),
                (4003, "in", "Отлично, тогда ждём от вас короткое ТЗ по вебхукам и заходим на тест.", -110),
            ],
        },
        {
            "username": "viktor_novapay",
            "peer_id": 800107,
            "title": "Виктор Громов · NovaPay",
            "unread_count": 0,
            "last_from_peer": 0,
            "messages": [
                (5001, "in", "Тимофей, платёж по годовому тарифу Pro ($4,500) ушёл, ловите платёжку.", -720),
                (5002, "out", "Виктор, оплату видим, спасибо за доверие! В пятницу в 14:00 проведём онбординг всей команды.", -680),
            ],
        },
        {
            "username": "elena_s42",
            "peer_id": 800102,
            "title": "Елена Романова · Studio 42",
            "unread_count": 0,
            "last_from_peer": 1,
            "messages": [
                (6001, "in", "Здравствуйте! Сергей из NovaPay очень рекомендовал вашу систему для работы с клиентами в Telegram.", -840),
                (6002, "out", "Елена, добрый день! С удовольствием покажу, как устроена воронка и синхронизация папок.", -800),
                (6003, "in", "Давайте завтра созвонимся на 15 минут, расскажу про наши процессы.", -300),
            ],
        },
        {
            "account_id": wa_account_id,
            "provider": "whatsapp",
            "phone": "+79993014455",
            "username": "kirill_boxberry_wa",
            "peer_id": 79993014455,
            "title": "Кирилл Сафонов · BoxBerry Logistics",
            "unread_count": 2,
            "last_from_peer": 1,
            "messages": [
                (7001, "in", "Добрый день! Пишу вам на рабочий WhatsApp. Мы ищем CRM, где можно вести и WhatsApp, и Telegram сделки в одном окне.", -190),
                (7002, "out", "Кирилл, здравствуйте! Да, в Foldesk WhatsApp подключается по QR-коду за 15 секунд и попадает в единый инбокс.", -170),
                (7003, "in", "Отлично! У нас 12 менеджеров по корпоративным клиентам. Пришлите, пожалуйста, КП и условия пилота сюда в WhatsApp.", -20),
            ],
        },
        {
            "account_id": wa_account_id,
            "provider": "whatsapp",
            "phone": "+79993025566",
            "username": "alina_medline_wa",
            "peer_id": 79993025566,
            "title": "Алина Белова · MedLine Clinic",
            "unread_count": 1,
            "last_from_peer": 1,
            "messages": [
                (8001, "in", "Тимофей, здравствуйте! Протестировали приём заявок через WhatsApp — всё летает.", -310),
                (8002, "out", "Алина, супер! Мы также добавили CSV-импорт номеров с поддержкой колонок WhatsApp и контролем суточных лимитов.", -280),
                (8003, "in", "Супер, базу на 50 контактов подготовили. Давайте сегодня запустим первую волну!", -35),
            ],
        },
        {
            "account_id": wa_account_id,
            "provider": "whatsapp",
            "phone": "+79993036677",
            "username": "ruslan_auto_wa",
            "peer_id": 79993036677,
            "title": "Руслан Каримов · AutoImport Pro",
            "unread_count": 0,
            "last_from_peer": 0,
            "messages": [
                (8501, "in", "Привет! Скиньте реквизиты сюда в WhatsApp, сегодня проведём оплату за полгода.", -500),
                (8502, "out", "Руслан, добрый день! Направил счёт на $1,950. Как пройдёт платёж — сразу подключим ваши WhatsApp и Telegram номера.", -450),
            ],
        },
    ]

    dialog_count = 0
    message_count = 0
    for d_spec in dialogs_spec:
        lead_id = lead_ids_by_username[d_spec["username"]]
        dlg_acc_id = d_spec.get("account_id") or account_id
        dlg_provider = d_spec.get("provider") or "telegram"
        dlg_phone = d_spec.get("phone")
        msgs = d_spec["messages"]
        last_sent_at = _iso(now_dt + timedelta(minutes=msgs[-1][3]))
        last_preview = msgs[-1][2][:120]
        dcur = conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, phone, provider,
                is_group, unread_count, last_message_at, last_message_preview,
                last_from_peer, muted, lead_id, created_at, updated_at
            ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                dlg_acc_id,
                d_spec["peer_id"],
                d_spec["username"],
                d_spec["title"],
                dlg_phone,
                dlg_provider,
                d_spec["unread_count"],
                last_sent_at,
                last_preview,
                d_spec["last_from_peer"],
                lead_id,
                now,
                last_sent_at,
            ),
        )
        dialog_id = dcur.lastrowid
        conn.execute("UPDATE leads SET dialog_id = ? WHERE id = ?", (dialog_id, lead_id))
        dialog_count += 1
        for tg_mid, direction, text, offset_min in msgs:
            m_sent = _iso(now_dt + timedelta(minutes=offset_min))
            conn.execute(
                """
                INSERT INTO messages (
                    dialog_id, telegram_message_id, direction, text, sent_at,
                    reply_to_telegram_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (dialog_id, tg_mid, direction, text, m_sent, m_sent, m_sent),
            )
            message_count += 1

        if d_spec["username"] == "denis_fb":
            from app.db import INBOX_MEDIA_DIR
            from app.inbox import ensure_playable_voice_wav

            voice_mid = 1006
            voice_sent = _iso(now_dt + timedelta(minutes=-10))
            voice_text = "Голосовое по запуску пилота и созвону в Zoom"
            voice_url = f"/api/dialogs/{dialog_id}/messages/{voice_mid}/media"
            conn.execute(
                """
                INSERT INTO messages (
                    dialog_id, telegram_message_id, direction, text,
                    media_url, media_type, media_mime, media_filename, media_duration,
                    sent_at, reply_to_telegram_message_id, created_at, updated_at
                ) VALUES (?, ?, 'in', ?, ?, 'voice', 'audio/wav', 'voice_1006.wav', 6, ?, NULL, ?, ?)
                """,
                (dialog_id, voice_mid, voice_text, voice_url, voice_sent, voice_sent, voice_sent),
            )
            message_count += 1
            try:
                wav_bytes = ensure_playable_voice_wav(
                    b"",
                    f"dlg{dialog_id}_msg{voice_mid}",
                    text_hint=voice_text,
                )
                INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
                (INBOX_MEDIA_DIR / f"dlg{dialog_id}_msg{voice_mid}.wav").write_bytes(wav_bytes)
                (INBOX_MEDIA_DIR / f"dlg{dialog_id}_msg{voice_mid}.ogg").write_bytes(wav_bytes)
            except Exception:
                pass

    end_of_day = now_dt.replace(hour=23, minute=59, second=50, microsecond=0)
    today_due_1 = min(now_dt + timedelta(hours=2), end_of_day - timedelta(seconds=20))
    today_due_2 = min(now_dt + timedelta(hours=4), end_of_day)

    reminders_spec = [
        (
            lead_ids_by_username["denis_fb"],
            _iso(now_dt - timedelta(hours=3)),
            "Отправить финальное КП после звонка в Zoom",
        ),
        (
            lead_ids_by_username["maria_adscale"],
            _iso(today_due_1),
            "Проверить поступление оплаты по инвойсу #408",
        ),
        (
            lead_ids_by_username["alex_ton"],
            _iso(today_due_2),
            "Прислать медиакит и примеры интеграции в Telegram",
        ),
        (
            lead_ids_by_username["viktor_novapay"],
            _iso(now_dt + timedelta(days=2)),
            "Провести онбординг команды NovaPay (25 мест)",
        ),
    ]
    for r_lead_id, due_at, r_text in reminders_spec:
        rcur = conn.execute(
            """
            INSERT INTO reminders (lead_id, due_at, text, done_at, created_at)
            VALUES (?, ?, ?, NULL, ?)
            """,
            (r_lead_id, due_at, r_text, now),
        )
        record_lead_event(
            conn,
            r_lead_id,
            "reminder_set",
            f"Напоминание на {due_at[:16].replace('T', ' ')}: {r_text[:80]}",
            {"reminder_id": int(rcur.lastrowid), "due_at": due_at, "text": r_text},
            created_at=now,
        )

    c1_account_ids = [account_id, account2_id, account3_id]
    c1_cur = conn.execute(
        """
        INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, created_at, updated_at)
        VALUES ('Web3 Founders · Осень', ?, ?, 20, 'running', ?, ?)
        """,
        (account_id, json.dumps(c1_account_ids), now, now),
    )
    camp1_id = c1_cur.lastrowid
    camp1_steps = [
        (
            1,
            0,
            "Привет, {name}! Увидел ваш продукт {company}. Мы помогаем сейлз-командам вести сделки и папки прямо в Telegram без потери чатов. Покажу за 5 минут?",
        ),
        (
            2,
            24,
            "{name}, пингую на всякий случай — актуально сейчас навести порядок в Telegram-чатах менеджеров?",
        ),
        (
            3,
            72,
            "Если сейчас не в фокусе — дайте знать, вернёмся в следующем месяце. Хорошей недели!",
        ),
    ]
    for s_order, s_delay, s_text in camp1_steps:
        conn.execute(
            """
            INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (camp1_id, s_order, s_delay, s_text, now),
        )

    camp1_recipients = [
        (
            lead_ids_by_username["denis_fb"],
            account_id,
            800103,
            "denis_fb",
            "Денис Морозов",
            "+79991034455",
            "FinBridge",
            "Платежный шлюз B2B",
            1,
            "replied",
            _iso(now_dt - timedelta(hours=3)),
        ),
        (
            lead_ids_by_username["alex_ton"],
            account2_id,
            800101,
            "alex_ton",
            "Алексей Соколов",
            "+79991012233",
            "TON Labs",
            "Пакет на 15 аккаунтов",
            1,
            "replied",
            _iso(now_dt - timedelta(hours=4)),
        ),
        (
            lead_ids_by_username["maria_adscale"],
            account3_id,
            800105,
            "maria_adscale",
            "Мария Лебедева",
            "+79991056677",
            "AdScale",
            "Квартальный тариф",
            1,
            "in_progress",
            _iso(now_dt - timedelta(hours=1)),
        ),
        (
            lead_ids_by_username["viktor_novapay"],
            account_id,
            800107,
            "viktor_novapay",
            "Виктор Громов",
            "+79991078899",
            "NovaPay",
            "Годовой контракт Pro",
            1,
            "in_progress",
            _iso(now_dt - timedelta(hours=2)),
        ),
        (
            lead_ids_by_username["ilya_c9"],
            account2_id,
            800106,
            "ilya_c9",
            "Илья Ковалёв",
            "+79991067788",
            "CloudNine",
            "Переход из таблиц в CRM",
            0,
            "pending",
            None,
        ),
        (
            lead_ids_by_username["max_retail"],
            account3_id,
            800108,
            "max_retail",
            "Максим Орлов",
            "+79991089900",
            "RetailBox",
            "Возврат к диалогу по пилоту",
            0,
            "pending",
            None,
        ),
    ]
    for r_lead_id, r_acc_id, r_tg_id, r_user, r_name, r_phone, r_comp, r_note, r_step, r_status, r_sent in camp1_recipients:
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, assigned_account_id, telegram_user_id, username, name,
                phone, company, note, current_step, status,
                last_sent_at, error_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (camp1_id, r_lead_id, r_acc_id, r_tg_id, r_user, r_name, r_phone, r_comp, r_note, r_step, r_status, r_sent, now, now),
        )
        if r_lead_id and r_step >= 1 and r_sent:
            record_lead_event(
                conn,
                r_lead_id,
                "outreach_sent",
                f"Отправлен шаг {r_step}/3 кампании «Web3 Founders · Осень»",
                {
                    "campaign_id": camp1_id,
                    "campaign_name": "Web3 Founders · Осень",
                    "step": r_step,
                    "account_id": r_acc_id,
                },
                created_at=r_sent,
            )
        if r_lead_id and r_status == "replied":
            record_lead_event(
                conn,
                r_lead_id,
                "reply_received",
                "Получен ответ от клиента — рассылка остановлена, стадия «Разговор»",
                {
                    "campaign_id": camp1_id,
                    "account_id": r_acc_id,
                    "username": r_user,
                },
                created_at=_iso(now_dt - timedelta(minutes=25)),
            )

    c2_account_ids = [account2_id, account3_id]
    c2_cur = conn.execute(
        """
        INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, created_at, updated_at)
        VALUES ('Партнёрские агентства · Follow-up', ?, ?, 10, 'draft', ?, ?)
        """,
        (account2_id, json.dumps(c2_account_ids), now, now),
    )
    camp2_id = c2_cur.lastrowid
    camp2_steps = [
        (
            1,
            0,
            "Здравствуйте, {name}! Видел кейсы {company}. Предлагаю партнёрскую программу по внедрению Telegram CRM для ваших клиентов.",
        ),
        (
            2,
            48,
            "{name}, направить короткий PDF с партнёрскими комиссиями и демо-доступом?",
        ),
    ]
    for s_order, s_delay, s_text in camp2_steps:
        conn.execute(
            """
            INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (camp2_id, s_order, s_delay, s_text, now),
        )

    camp2_recipients = [
        (account2_id, 800111, "sergey_kinetica", "Сергей Белов", "+79992011122", "Kinetica Digital", "Топ-агентство перформанса"),
        (account3_id, 800112, "olga_leadcraft", "Ольга Миронова", "+79992022233", "LeadCraft", "Интегратор B2B-отделов продаж"),
        (account2_id, 800113, "pavel_scaleup", "Павел Захаров", "+79992033344", "ScaleUp Partners", "Консалтинг по продажам"),
        (account3_id, 800114, "daria_ventures", "Дарья Смирнова", "+79992044455", "Venture Studio", "Портфель из 12 SaaS-команд"),
    ]
    for r_acc_id, r_tg_id, r_user, r_name, r_phone, r_comp, r_note in camp2_recipients:
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, assigned_account_id, telegram_user_id, username, name,
                phone, company, note, current_step, status,
                last_sent_at, error_text, created_at, updated_at
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', NULL, NULL, ?, ?)
            """,
            (camp2_id, r_acc_id, r_tg_id, r_user, r_name, r_phone, r_comp, r_note, now, now),
        )

    conn.commit()

    save_view(conn, "VIP в работе", status="talking", folder_id=folder_map["VIP Клиенты"])
    save_view(conn, "Ожидают оплаты", status="waiting", folder_id=None)

    from app.snippets import seed_default_snippets

    seed_default_snippets(conn)

    return {
        "ok": True,
        "account_id": account_id,
        "folders": len(folders_spec),
        "leads": len(leads_spec),
        "dialogs": dialog_count,
        "messages": message_count,
        "reminders": len(reminders_spec),
        "views": 2,
        "campaigns": 2,
    }
