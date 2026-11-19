from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from app.db import ROOT, connect, init_db, utc_now


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _resolve_playwright_node_path() -> str:
    candidates = [ROOT / "e2e" / "node_modules"]
    try:
        common = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
            text=True,
        ).strip()
        if common:
            main_root = (ROOT / common).resolve().parent
            candidates.append(main_root / "e2e" / "node_modules")
    except Exception:
        pass
    parts = [str(p) for p in candidates if p.is_dir()]
    if os.environ.get("NODE_PATH"):
        parts.append(os.environ["NODE_PATH"])
    return os.pathsep.join(parts)


def _seed_adequacy_db(db_path: Path) -> None:
    conn = connect(db_path)
    init_db(conn)
    now = utc_now()

    # 6 active Telegram accounts (20/day each = 120/day) + 1 frozen account (20/day, must be excluded!)
    accounts_spec = [
        (1, "+79001110001", "acc1_active", "Аккаунт 1", "active", "socks5://nm-country-kz-sid-foldeskacc1-ttl-24h:pw@gate.nodemaven.com:1080"),
        (2, "+79001110002", "acc2_active", "Аккаунт 2", "active", "socks5://nm-country-kz-sid-foldeskacc2-ttl-24h:pw@gate.nodemaven.com:1080"),
        (3, "+79001110003", "acc3_active", "Аккаунт 3", "active", "socks5://static-isp-1.example.com:1080"),
        (4, "+79001110004", "acc4_active", "Аккаунт 4", "active", "socks5://static-isp-2.example.com:1080"),
        (5, "+79001110005", "acc5_active", "Аккаунт 5", "active", "socks5://static-isp-3.example.com:1080"),
        (6, "+79001110006", "acc6_active", "Аккаунт 6", "active", "socks5://static-isp-4.example.com:1080"),
        (7, "+381671110007", "frozen_acc", "Замороженный Акк", "frozen", "socks5://nm-country-rs-sid-foldeskacc7-ttl-24h:pw@gate.nodemaven.com:1080"),
    ]
    for acc_id, phone, uname, name, health, proxy in accounts_spec:
        conn.execute(
            """
            INSERT INTO accounts (
                id, phone, session_path, status, telegram_user_id,
                telegram_username, telegram_name, created_at,
                health, updated_at, daily_limit, sent_today, provider, proxy_url
            ) VALUES (?, ?, ?, 'connected', ?, ?, ?, ?, ?, ?, 20, 0, 'telegram', ?)
            """,
            (acc_id, phone, f"data/sessions/{acc_id}.session", 500000 + acc_id, uname, name, now, health, now, proxy),
        )

    # 4 CRM leads (3 active/talking/new/waiting + 1 closed lost)
    leads_spec = [
        (101, 1, 700101, "active_lead", "Алексей SaaS", "talking", "Acme AI"),
        (102, 1, 700102, "closed_lost_lead", "Закрытый Отказ", "lost", "OldCo"),
        (103, 1, 700103, "waiting_reply_lead", "Мария Ждём", "waiting", "WaitApp"),
        (104, 2, 700104, "second_acc_lead", "Борис ВтораяСессия", "new", "SecondSaaS"),
    ]
    for lid, acc_id, tg_uid, uname, name, status, company in leads_spec:
        conn.execute(
            """
            INSERT INTO leads (
                id, account_id, telegram_user_id, username, name, company,
                status, created_at, updated_at, provider
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'telegram')
            """,
            (lid, acc_id, tg_uid, uname, name, company, status, now, now),
        )

    # 4 CRM dialogs (3 on acc #1, 1 on acc #2):
    # - #1001 active_lead (last_from_peer=1, unread=2, status=talking) -> WAITING ACTIVE
    # - #1002 closed_lost_lead (last_from_peer=1, unread=0, status=lost) -> CLOSED (must NOT appear in waiting!)
    # - #1003 waiting_reply_lead (last_from_peer=0, unread=0, status=waiting) -> OUR_TURN
    # - #1004 second_acc_lead (last_from_peer=1, unread=1, status=new, acc#2) -> WAITING ACTIVE
    dialogs_spec = [
        (1001, 1, 700101, "active_lead", "Алексей SaaS", 2, "2026-11-19T09:30:00", "Пришлите кейсы UGC", 1, 101),
        (1002, 1, 700102, "closed_lost_lead", "Закрытый Отказ", 0, "2026-11-19T09:20:00", "Не актуально, спс", 1, 102),
        (1003, 1, 700103, "waiting_reply_lead", "Мария Ждём", 0, "2026-11-19T09:10:00", "Написали фоллоуап", 0, 103),
        (1004, 2, 700104, "second_acc_lead", "Борис ВтораяСессия", 1, "2026-11-19T09:05:00", "Давайте созвонимся", 1, 104),
    ]
    for did, acc_id, peer_id, uname, title, unread, last_at, preview, lfp, lid in dialogs_spec:
        conn.execute(
            """
            INSERT INTO dialogs (
                id, account_id, peer_type, peer_id, username, title, provider,
                is_group, unread_count, last_message_at, last_message_preview,
                last_from_peer, muted, lead_id, created_at, updated_at
            ) VALUES (?, ?, 'user', ?, ?, ?, 'telegram', 0, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (did, acc_id, peer_id, uname, title, unread, last_at, preview, lfp, lid, now, now),
        )
        conn.execute(
            """
            INSERT INTO messages (
                dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (did, 9000 + did, "in" if lfp == 1 else "out", preview, last_at, now, now),
        )

    # 2 Outreach campaigns:
    # - Camp #1: B2B running, leadgen_enabled=1, 0 pending -> STARVED!
    # - Camp #2: OFM running, leadgen_enabled=1, 20 pending -> HEALTHY!
    conn.execute(
        """
        INSERT INTO campaigns (
            id, name, account_id, account_ids_json, status, daily_limit,
            leadgen_enabled, leadgen_daily_quota, created_at, updated_at
        ) VALUES (1, 'SoftUGC — B2B Starved', 1, '[1,2]', 'running', 140, 1, 140, ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO campaigns (
            id, name, account_id, account_ids_json, status, daily_limit,
            leadgen_enabled, leadgen_daily_quota, created_at, updated_at
        ) VALUES (2, 'SoftUGC — OFM Active', 1, '[1]', 'running', 25, 1, 25, ?, ?)
        """,
        (now, now),
    )
    for i in range(1, 21):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, username, name, status, current_step, created_at, updated_at
            ) VALUES (2, ?, ?, 'pending', 0, ?, ?)
            """,
            (f"ofm_pending_{i}", f"OFM Buyer {i}", now, now),
        )

    conn.commit()
    conn.close()


def test_playwright_astryx_ui_adequacy_inbox_filters_pool_and_leadgen_health(tmp_path: Path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "adequacy.db"
    admin_password = "AstryxAdequacyPass!2026"
    _seed_adequacy_db(db_path)

    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("BOT_TOKEN", None)
    env["PYTHONPATH"] = str(ROOT)
    env["CRM_REQUIRE_AUTH"] = "1"
    env["CRM_ADMIN_PASSWORD"] = admin_password
    env["CRM_VIEW_MODE"] = "sheet"
    env["CLOUD_BACKUP_ENABLED"] = "0"
    env["DISABLE_TG_WATCHES"] = "1"
    env["DEMO_AUTOSEED"] = "0"
    env["BOOTSTRAP_PROD_SNAPSHOT"] = "0"

    server_code = f"""
import uvicorn
from pathlib import Path
from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import FakeGateway

conn = connect(Path({str(db_path)!r}))
init_db(conn)
gw = FakeGateway(code="11111", password="secret", needs_password=False)
app = create_app(conn=conn, gateway=gw)
uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
"""

    server = subprocess.Popen(
        [sys.executable, "-c", server_code],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = False
        for _ in range(60):
            try:
                with urlopen(f"{base_url}/api/health", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.15)
        assert ready, "Test server did not start in time"

        mock_health_summary = json.dumps(
            {
                "campaigns": [
                    {
                        "campaign_id": 1,
                        "name": "SoftUGC — B2B Starved",
                        "status": "running",
                        "leadgen_enabled": True,
                        "pending": 0,
                        "ingested_24h": 1,
                        "sources_exhausted": True,
                        "low_water_alert": True,
                    },
                    {
                        "campaign_id": 2,
                        "name": "SoftUGC — OFM Active",
                        "status": "running",
                        "leadgen_enabled": True,
                        "pending": 20,
                        "ingested_24h": 15,
                        "sources_exhausted": False,
                        "low_water_alert": False,
                    },
                ],
                "last_run": {
                    "rc": 0,
                    "stage_error_count": 0,
                    "finished_at": "2026-11-19T07:37:28Z",
                },
                "direct_x_count": 2,
                "direct_x_leads": [
                    {
                        "company": "Granola AI",
                        "name": "Chris Pedregal",
                        "x_handle": "@granola_ai",
                        "website": "https://granola.ai",
                        "niche": "AI SaaS",
                        "source": "ph_detailed",
                        "hook": "Fast-growing AI notepad with X-only founder contact",
                    },
                    {
                        "company": "Wispr Flow",
                        "name": "Tanay Kothari",
                        "x_handle": "@wisprflow",
                        "website": "https://wisprflow.ai",
                        "niche": "AI Productivity",
                        "source": "yc_ph",
                        "hook": "Voice dictation app scaling creator UGC",
                    },
                ],
            }
        )

        pw_script = f"""
const fs = require('fs');
const {{ chromium }} = require('playwright');

(async () => {{
  const chromePath = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const launchOpts = {{ headless: true }};
  if (fs.existsSync(chromePath)) launchOpts.executablePath = chromePath;
  const browser = await chromium.launch(launchOpts);
  const context = await browser.newContext({{ viewport: {{ width: 1440, height: 900 }} }});
  const page = await context.newPage();

  const consoleErrors = [];
  const pageErrors = [];
  const bootRequests = [];

  page.on('pageerror', (err) => pageErrors.push(err.message));
  page.on('console', (msg) => {{
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  }});
  page.on('request', (req) => {{
    const url = req.url();
    if (url.startsWith('{base_url}/api/')) bootRequests.push(new URL(url).pathname);
  }});

  // Serve Track 3 /api/leadgen/health-summary contract so we test rich Leadgen Health Strip + Direct X drawer
  await page.route('**/api/leadgen/health-summary', async (route) => {{
    await route.fulfill({{
      status: 200,
      contentType: 'application/json',
      body: {mock_health_summary!r},
    }});
  }});

  // 1. Real CRM cookie authentication (no synthetic BOT_TOKEN bypass)
  const unauth = await page.request.get('{base_url}/api/accounts');
  if (unauth.status() !== 401) {{
    throw new Error(`Expected 401 before login, got ${{unauth.status()}}`);
  }}
  const loginRes = await page.request.post('{base_url}/api/auth/login', {{
    data: {{ username: 'tim', password: '{admin_password}' }},
  }});
  if (!loginRes.ok()) {{
    throw new Error(`Login failed: ${{loginRes.status()}}`);
  }}

  await page.goto('{base_url}/', {{ waitUntil: 'domcontentloaded' }});
  await page.locator('#board .lead-card').first().waitFor({{ state: 'visible', timeout: 15000 }});
  await page.waitForTimeout(350);

  // 2. Astryx Phase 0 Design System assertions (Cyrillic Inter, NEVER Figtree, 1px white inset dark bezel)
  const designCheck = await page.evaluate(() => {{
    const bodyStyle = getComputedStyle(document.body);
    const boxStyle = getComputedStyle(document.querySelector('.surface-box'));
    return {{
      theme: document.documentElement.getAttribute('data-theme'),
      fontFamily: bodyStyle.fontFamily,
      boxShadow: boxStyle.boxShadow,
    }};
  }});
  if (designCheck.theme !== 'dark') {{
    throw new Error(`Expected dark theme, got ${{designCheck.theme}}`);
  }}
  if (!designCheck.fontFamily.includes('Inter') || /figtree/i.test(designCheck.fontFamily)) {{
    throw new Error(`Expected Cyrillic Inter font stack without Figtree, got: ${{designCheck.fontFamily}}`);
  }}
  if (!designCheck.boxShadow.includes('inset')) {{
    throw new Error(`Expected Astryx dark-mode 1px white inset bezel on .surface-box, got: ${{designCheck.boxShadow}}`);
  }}

  // 3. Strictly 4 sidebar nav tabs & dead DOM / wasted boot fetches removed
  const navTabs = await page.locator('.nav a[data-sheet-link]').count();
  if (navTabs !== 4) {{
    throw new Error(`Expected exactly 4 sidebar nav tabs, got ${{navTabs}}`);
  }}
  for (const deadSel of ['#tg-web-a-panel', '.ops-grid', '#s11', '#leadgen', '#tool-phones', '#tool-parser', '#ai-bot']) {{
    if ((await page.locator(deadSel).count()) !== 0) {{
      throw new Error(`Dead DOM selector still present: ${{deadSel}}`);
    }}
  }}
  for (const wastedPath of ['/api/ai-bot/config', '/api/calendar', '/api/property-defs', '/api/leadgen/config', '/api/folders']) {{
    if (bootRequests.includes(wastedPath)) {{
      throw new Error(`Wasted boot() HTTP fetch still executed: ${{wastedPath}}`);
    }}
  }}

  // Verify initial topbar counts
  const initialDialogsStat = (await page.locator('#stat-dialogs').textContent()).trim();
  const initialLeadsStat = (await page.locator('#stat-leads').textContent()).trim();
  const initialKanbanCards = await page.locator('#board .lead-card').count();
  if (initialDialogsStat !== '4') {{
    throw new Error(`Expected initial #stat-dialogs to be 4, got ${{initialDialogsStat}}`);
  }}
  if (initialLeadsStat !== '4' || initialKanbanCards !== 4) {{
    throw new Error(`Expected 4 initial leads/cards, got stat=${{initialLeadsStat}} cards=${{initialKanbanCards}}`);
  }}

  // 4. Inbox filtering: Группы removed, #chip-note hidden on chip click with empty query, closed deal excluded, #stat-dialogs invariant
  await page.locator('a[data-sheet-link="inbox"]').first().click();
  await page.locator('#dialog-results .dialog').first().waitFor({{ state: 'visible' }});

  if ((await page.locator('#s04 [data-chip="groups"]').count()) !== 0) {{
    throw new Error('Dead Группы chip must be removed from Inbox');
  }}
  const mutedChipVisible = await page.locator('#s04 [data-chip="muted"]').isVisible();
  if (mutedChipVisible) {{
    throw new Error('Zero-count Без звука chip should be hidden');
  }}

  // Click Ждут ответа with empty query
  await page.locator('#s04 [data-chip="waiting"]').click();
  await page.waitForTimeout(300);

  if (await page.locator('#chip-note').isVisible()) {{
    throw new Error('#chip-note must stay hidden when clicking chip with empty query in CRM mode');
  }}
  const waitingDialogsText = await page.locator('#dialog-results').textContent();
  if (waitingDialogsText.includes('Закрытый Отказ')) {{
    throw new Error('Closed lost deal must be excluded from Ждут ответа filter');
  }}
  if (!waitingDialogsText.includes('Алексей SaaS') || !waitingDialogsText.includes('Борис ВтораяСессия')) {{
    throw new Error(`Expected active waiting dialogs in list, got: ${{waitingDialogsText}}`);
  }}
  const dialogsStatAfterChip = (await page.locator('#stat-dialogs').textContent()).trim();
  if (dialogsStatAfterChip !== '4') {{
    throw new Error(`#stat-dialogs must remain 4 after clicking waiting chip, got ${{dialogsStatAfterChip}}`);
  }}

  // Uncheck waiting chip and switch to single session #2 (only 1 dialog)
  await page.locator('#s04 [data-chip="waiting"]').click();
  await page.waitForTimeout(250);
  await page.locator('#inbox-session-switcher [data-session-filter="2"]').click();
  await page.waitForTimeout(300);
  const singleSessionRows = await page.locator('#dialog-results .dialog').count();
  if (singleSessionRows !== 1) {{
    throw new Error(`Expected 1 dialog for session #2, got ${{singleSessionRows}}`);
  }}
  const dialogsStatAfterSession = (await page.locator('#stat-dialogs').textContent()).trim();
  if (dialogsStatAfterSession !== '4') {{
    throw new Error(`#stat-dialogs must remain 4 after switching to 1 session, got ${{dialogsStatAfterSession}}`);
  }}

  // Switch back to all sessions & open dialog to verify 1-click "Открыть в Telegram ↗"
  await page.locator('#inbox-session-switcher [data-session-filter="all"]').click();
  await page.waitForTimeout(250);
  await page.locator('#dialog-results .dialog').filter({{ hasText: 'Алексей SaaS' }}).first().click();
  const openTgBtn = page.locator('#thread-open-tg-btn');
  await openTgBtn.waitFor({{ state: 'visible' }});
  const openTgText = (await openTgBtn.textContent()).trim();
  const openTgHref = await openTgBtn.getAttribute('href');
  const openTgFallback = await openTgBtn.getAttribute('data-fallback-url');
  if (openTgText !== 'Открыть в Telegram ↗' || openTgHref !== 'tg://resolve?domain=active_lead' || openTgFallback !== 'https://t.me/active_lead') {{
    throw new Error(`Unexpected Открыть в Telegram button: text=${{openTgText}} href=${{openTgHref}} fallback=${{openTgFallback}}`);
  }}
  await page.waitForTimeout(200);
  const waitingChipCountAfterSelect = (await page.locator('#s04 [data-chip-count="waiting"]').textContent()).trim();
  if (waitingChipCountAfterSelect !== '2') {{
    throw new Error(`Expected waiting chip count to remain 2 (excluding closed lost deal), got ${{waitingChipCountAfterSelect}}`);
  }}

  // 5. "В CRM" search MUST NOT mutate Kanban / state.leads!
  await page.locator('#q').fill('Алексей');
  await page.locator('#search-form button[type="submit"]').click();
  await page.waitForTimeout(350);
  const searchedRows = await page.locator('#dialog-results .dialog').count();
  if (searchedRows !== 1) {{
    throw new Error(`Expected 1 CRM search result for Алексей, got ${{searchedRows}}`);
  }}
  if (await page.locator('#chip-note').isVisible()) {{
    throw new Error('#chip-note must remain hidden during CRM search');
  }}
  await page.locator('a[data-sheet-link="leads"]').first().click();
  await page.locator('#board .lead-card').first().waitFor({{ state: 'visible' }});
  const kanbanAfterSearch = await page.locator('#board .lead-card').count();
  const leadsStatAfterSearch = (await page.locator('#stat-leads').textContent()).trim();
  const dialogsStatAfterTab = (await page.locator('#stat-dialogs').textContent()).trim();
  if (kanbanAfterSearch !== 4 || leadsStatAfterSearch !== '4') {{
    throw new Error(`В CRM inbox search mutated Kanban/leads! cards=${{kanbanAfterSearch}} stat=${{leadsStatAfterSearch}}`);
  }}
  if (dialogsStatAfterTab !== '4') {{
    throw new Error(`#stat-dialogs must remain 4 after switching tabs, got ${{dialogsStatAfterTab}}`);
  }}

  // 6. Outreach -> Leadgen Health Strip (starvation + Direct X drawer) & Accounts pool math (frozen excluded + NodeMaven badge)
  await page.locator('a[data-sheet-link="outreach"]').first().click();
  await page.locator('#leadgen-health-strip').waitFor({{ state: 'visible' }});
  const starvedPill = page.locator('#leadgen-health-campaigns .leadgen-health-pill[data-starved="true"]');
  await starvedPill.waitFor({{ state: 'visible' }});
  const starvedText = await starvedPill.textContent();
  if (!starvedText.includes('SoftUGC — B2B Starved') || !starvedText.includes('0 pending · источники исчерпаны')) {{
    throw new Error(`Leadgen Health Strip starvation text mismatch: ${{starvedText}}`);
  }}

  // Open Direct X Leads drawer
  await page.locator('#leadgen-direct-x-btn').click();
  await page.locator('#direct-x-leads-modal').waitFor({{ state: 'visible' }});
  const dxBodyText = await page.locator('#direct-x-leads-body').textContent();
  if (!dxBodyText.includes('Granola AI') || !dxBodyText.includes('Wispr Flow')) {{
    throw new Error(`Direct X drawer missing expected leads: ${{dxBodyText}}`);
  }}
  await page.locator('#direct-x-leads-close-btn').click();
  await page.locator('#direct-x-leads-modal').waitFor({{ state: 'hidden' }});

  // Switch to Рассылки -> Аккаунты and check frozen account exclusion from pool (6 active, 120/day NOT 140)
  await page.locator('#outreach-tab-accounts').click();
  await page.locator('#accounts-summary-bar').waitFor({{ state: 'visible' }});
  const summaryText = (await page.locator('#accounts-summary-bar').textContent()).trim();
  if (!summaryText.includes('Активных: 6') || !summaryText.includes('120 сообщ./сут')) {{
    throw new Error(`Frozen account leaked into daily pool! summary=${{summaryText}}`);
  }}
  const nmBadges = await page.locator('#account-list .proxy-mode-badge').count();
  if (nmBadges < 2) {{
    throw new Error(`Expected subtle NodeMaven rotating badges on rotating accounts, got ${{nmBadges}}`);
  }}
  const nmBadgeText = (await page.locator('#account-list .proxy-mode-badge').first().textContent()).trim();
  if (!nmBadgeText.includes('NodeMaven rotating')) {{
    throw new Error(`Unexpected NodeMaven badge text: ${{nmBadgeText}}`);
  }}

  // 7. Statistics tab & final #stat-dialogs invariant + zero error toasts
  await page.locator('a[data-sheet-link="analytics"]').first().click();
  await page.locator('#stats-campaigns-grid').waitFor({{ state: 'visible' }});
  const finalDialogsStat = (await page.locator('#stat-dialogs').textContent()).trim();
  if (finalDialogsStat !== '4') {{
    throw new Error(`#stat-dialogs drifted on Статистика tab: ${{finalDialogsStat}}`);
  }}
  const errorToastVisible = await page.locator('#toast[data-kind="error"]').isVisible();
  if (errorToastVisible) {{
    const toastTxt = await page.locator('#toast').textContent();
    throw new Error(`Unexpected visible error toast: ${{toastTxt}}`);
  }}

  if (pageErrors.length || consoleErrors.length) {{
    throw new Error(`Browser errors detected: pageErrors=${{JSON.stringify(pageErrors)}} consoleErrors=${{JSON.stringify(consoleErrors)}}`);
  }}

  await browser.close();
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = _resolve_playwright_node_path()
        proc = subprocess.run(
            ["node", "-e", pw_script],
            cwd=str(ROOT / "e2e"),
            env=node_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, (
            f"Playwright UI Adequacy E2E failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
