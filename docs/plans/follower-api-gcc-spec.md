# Follower API (`treg × Jev`) + Декабрьский фокус на GCC / Арабоязычные проекты — Спецификация

> Зафиксировано по итогам Deep HTML Grill (Раунд 1 + Раунд 2) и реального кейса **Adam Health (KSA)**.
> Рабочая ветка Волны 1: `feat/wave1-follower-api-gcc`.

## 1. Бизнес-контекст и декабрьский приоритет
- **Почему GCC в декабре:** Западный (US/EU) и RU B2B-рынок уходит на рождественско-новогодние каникулы с середины декабря. В **Саудовской Аравии и странах GCC** Рождество и Новый год не являются выходными.
- **Баланс рынков (`active_market_profile`):**
  - `gcc_december` (декабрьский профиль): **60–70%** фокуса на GCC/MENA (при 80% квоты на богатую шестёрку `SA, AE, QA, KW, BH, OM` и повышенным фильтром размера для `EG, JO`), **30–40%** на глобальный безканикульный поток (`OFM`, крипто, AI/consumer apps).
  - `global`: штатный глобальный профиль.
  - **1-кнопочный откат:** переключение `gcc_december ↔ global` в Настройках лидгена CRM не затирает пользовательские настройки профиля `global`.
- **Каналы доставки:**
  - **Telegram:** существующий пул TG-аккаунтов (140 слотов/день), пополнение через `tg_identity_bridge` и нативные дубайские/MENA бизнес- и крипто-чаты.
  - **WhatsApp:** серверный автопилот через `WhatsAppGateway` (`app/whatsapp_gateway.py`) с 1–2 прогретыми SIM-картами (ручной режим `wa.me` не используем).
  - **Direct X / IG (`social_manual`):** только сверхпроверенные безTG/безWA, а обычный поток без TG/WA не перегружаем дорогим обогащением выше `$0.02–$0.03`.

## 2. Целевой портрет GCC (эталон: Adam Health + D2C/Apps/Crypto)
- **Разрешённые сегменты в GCC:**
  1. **Крупные клиники и мед-сети / health-tech:** эстетическая медицина, стоматология, дерматология, телемедицина, лабораторные пакеты (пример: *Adam Health*, KSA). **Снимаем запрет `clinic/salon`** в `typesafe_leads.py` и `tg_llm_qualify.py` для клиник/сетей с рекламой/брендом; мелкие одиночные парикмахерские/салоны, локальные кафе и SMM-агентства остаются под баном.
  2. **D2C и физические бренды:** парфюм/уд, косметика, уход, БАДы/витамины (`salla.sa` / `zid.sa` / Shopify).
  3. **Consumer / AI / Fintech приложения GCC:** App Store / Google Play / YC / Hub71 / Flat6Labs / TAQADAM.
  4. **Дубайский крипто, проп-трейдинг и high-ticket инфобиз.**
- **Фильтр для `EG` и `JO`:** допускаются только крупные бренды (`followers >= 20 000`, либо App Store/Google Play приложение, либо `>= 25` сотрудников, либо подтверждённая активная реклама).
- **Офферы, язык и тональность (реализация языкового движка — Волна 2):**
  - Авто-роутинг `ar | en` (если профиль/посты/реклама на арабском → естественный бизнес-арабский; иначе EN).
  - Две тональности в GCC: деловой бренд-тон (клиники, D2C, приложения) и прямой growth-тон (крипто, инфобиз).
  - Офферы: для клиник и физических D2C-брендов GCC — **фикс-ретейнер под ключ** (ориентир из Adam Health: `$1.5k` 1-й мес / `$1.2k/мес` + CPA + 3 тест-видео); для AI-приложений и Whales — **Revshare / CPA**.
  - Кампании: отдельные `GCC / MENA — TG` и `GCC / MENA — WA` (7 дней/нед, окно `09:00–20:00 UTC+3/+4`), первая пачка в `draft` под визуальный контроль (Rule 5).

## 3. Архитектура Follower API (`treg × Jev`) — `leadgen/follower_graph_leads.py`
- **Платформы (50/50 бюджет на старте):**
  - **X:** `tikhub.x.twitter-web-fetch-user-followers` (`$0.001`/стр) + `treg.x.user.profile` (`X-Treg-Route-Max-Cost: 0.002`).
  - **Instagram:** `tikhub.x.instagram-v2-fetch-user-followers` (`$0.002`/стр, пропуск приватных) + `treg.instagram.user.profile` (`X-Treg-Route-Max-Cost: 0.003`).
- **Pre-filter ($0):** отсев аккаунтов `<5` фолловеров с дефолтным аватаром и без бейджа, приватных IG, 90-дневного `seen` и supply/job/spam-шума.
- **Jev-скоринг (prompt-injection-safe `<person>` + `& < >` escaping):**
  - Primary: прямой `TYPESAFE_API_KEY` (`AsyncTypeSafeClient`, $0 списания с `treg`).
  - Fallback: `treg` `openrouter.ai-judge.decide` (`typesafe/jev-1.13`, `~$0.00003`/вызов).
  - **2-pass gate (`SKILL.md`):** если `enough < 0.5` и `fit >= 0.4` → читаем 10 постов (`treg.x.user.posts`, кап `$0.001`) + при наличии домена `treg.companies.enrich` (кап `$0.002`) → Jev pass 2.
  - Для всех финальных лидов (`fit >= 1.5`) также дочитываем посты для извлечения цитаты боли (`pain_quote`).
- **Граф 1st + 2nd degree и сэмплинг:**
  - Порциями по `300–500` фолловеров на сида за цикл с сохранением курсора и ротацией (потолок `2 500` на сида).
  - Промоушен в 2nd degree только если `fit >= 1.9`, `500 <= followers <= 50 000`, есть не-социальный домен (или GCC-профиль), не агентство.
- **Изоляция OFM:** отдельная ветка OFM-сидов (`15–20%` бюджета Follower API → `segment="ofm"`, `#21 SoftUGC — OFM (TG)`), 0% пересечения с GCC.
- **Headless режим:** без генерации `card.html` / `network.html` — лиды идут сразу в конвейер CRM (`contact_waterfall` → `tg_identity_bridge` → `crm.db`).

## 4. Бюджет, таймауты и анти-слив
- **Дневной бюджет `crmtg`: `$10/день`:**
  - `$4/день` — Follower API;
  - `$4/день` — обогащение/мост (в т.ч. `contactout.people.contact.phone` `$0.25` разрешён для всех `fit >= 1.5` без TG/бесплатного телефона в рамках этого под-лимита `$4/день`);
  - `$2/день` — Meta Ads / TikTok / App Store GCC.
- **Защита баланса и прозрачность:**
  - Каждый платный `treg`-вызов использует `treg_client.call_with_cost()` (`charged_micro`) и пишется в SQLite `enrichment_ledger`.
  - Авто-стоп-лосс источника: CPL `> $0.50` при оценке `>= 20` профилей.
  - Алерт при балансе `< $5.00`; в Волне 2 — выделение отдельного `treg` agent-ключа `crmtg-leadgen`.
- **Фикс таймаута `tg_llm_qualify.py` (>300с):**
  - Follower API минует `tg_llm_qualify.py` (квалифицируется напрямую в Jev).
  - В `tg_llm_qualify.py`: уменьшен `TIMEOUT` (35с) + мгновенная деградация зависшей модели на `TimeoutError` в `relay_route.py` (`claude-sonnet-5 → claude-haiku-5-5 → grok-4.7 → gemini-3.8-flash`) + мягкий wall-clock дедлайн `220с` с сохранением готовых батчей.

## 5. План волн и KPI
- **Волна 1 (текущая ветка):** `SPEC.md` + `call_with_cost` + фикс `tg_llm_qualify.py` + белый список клиник GCC + 1-кнопочный `active_market_profile` + `follower_graph_leads.py` + GCC-профиль в `meta_ads`/`treg_social`/`high_yield_veins` + правило ContactOut + тесты/`verify.sh`/`proactive_audit.py` + живой Canary (`$1.50`, 20 лидов на осмотр).
- **Волна 2 (после аппрува Canary):** `ar` язык в `language_router`/`outreach_copy`, кампании `GCC / MENA — TG/WA` в `draft`, `tg_catalog_discovery` для MENA-чатов, включение `follower_graph` в 3ч крон, отдельный `treg` agent-ключ, холодный `adversarial-skeptic` и деплой на VPS.
- **Целевые KPI 1-й недели:** `>= 50–80` контактных GCC/Whale лидов/день при CPL `<= $0.10–$0.15`, `reply rate >= 15%`, `0` часов простоя с `pending_step1 = 0`.
