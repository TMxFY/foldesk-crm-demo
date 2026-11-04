# CRM для лидов из Telegram

Локальная CRM на одного оператора: подключаете свой аккаунт Telegram, собираете лиды из диалогов или вручную, ищете их, меняете статус и ставите напоминания. Входа в саму CRM нет.

Напоминание живёт только в CRM и в Telegram не отправляется.

## Запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload
```

В `.env` впишите `TG_API_ID` и `TG_API_HASH` с https://my.telegram.org/apps. Без них подключение аккаунта не стартует. Сессии лежат в `data/sessions/` и в git не попадают.

Откройте http://127.0.0.1:8000

## Проверка

```bash
./scripts/verify.sh
```

## Чего в этой версии нет

Массовых сообщений, прогрева аккаунтов, парсинга участников групп и отправки напоминаний в Telegram.
