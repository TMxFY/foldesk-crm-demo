from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from app.db import ROOT, connect, init_db, utc_now, utc_today


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
    except subprocess.SubprocessError:
        candidates.append(ROOT / "e2e" / "node_modules")
    if not (ROOT / "e2e" / "node_modules").exists():
        venv_link = ROOT / ".venv"
        if venv_link.is_symlink():
            candidates.append(venv_link.resolve().parent / "e2e" / "node_modules")
    parts = [str(p) for p in candidates if p.is_dir()]
    if os.environ.get("NODE_PATH"):
        parts.append(os.environ["NODE_PATH"])
    return os.pathsep.join(parts)


def _seed_unified_inbox_db(db_path: Path) -> None:
    conn = connect(db_path)
    init_db(conn)
    now = utc_now()
    today = utc_today()

    # 2 active Telegram accounts (combined quota: 7 / 50 = 14%)
    accounts_spec = [
        (1, "+79001110001", "vladwilhelm", "Vladislav Wilhelm", "active", 20, 2, "data/sessions/w2_acc1.session"),
        (2, "+79001110002", "tim_sales", "Тимофей Сейлс", "active", 30, 5, "data/sessions/w2_acc2.session"),
    ]
    for acc_id, phone, uname, name, health, dlimit, sent_today, spath in accounts_spec:
        conn.execute(
            """
            INSERT INTO accounts (
                id, phone, session_path, status, telegram_user_id,
                telegram_username, telegram_name, created_at,
                health, updated_at, daily_limit, sent_today, sent_date, provider
            ) VALUES (?, ?, ?, 'connected', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'telegram')
            """,
            (acc_id, phone, spath, 510000 + acc_id, uname, name, now, health, now, dlimit, sent_today, today),
        )

    # 3 CRM leads:
    # - #101 Алексей SaaS: talking, $2500, Acme AI, Airtable In Progress (2 dialogs across Acc #1 and Acc #2!)
    # - #102 Мария Ждём: waiting, $1200, WaitApp (dialog on Acc #1, waiting our answer)
    # - #103 Борис Наш Ход: talking, $900, TurnCo (dialog on Acc #2, our turn / NOT waiting)
    leads_spec = [
        (101, 1, 710101, "active_lead", "Алексей SaaS", "talking", 2500, "Acme AI", "AI SaaS", "In Progress", "recAcme101"),
        (102, 1, 710102, "maria_wait", "Мария Ждём", "waiting", 1200, "WaitApp", "E-com", None, None),
        (103, 2, 710103, "boris_turn", "Борис Наш Ход", "talking", 900, "TurnCo", "Fintech", None, None),
    ]
    for lid, acc_id, tg_uid, uname, name, status, amount, company, niche, at_status, at_rec in leads_spec:
        conn.execute(
            """
            INSERT INTO leads (
                id, account_id, telegram_user_id, username, name, status, amount, currency,
                company, niche, airtable_status, airtable_record_id, created_at, updated_at, provider
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?, 'telegram')
            """,
            (lid, acc_id, tg_uid, uname, name, status, amount, company, niche, at_status, at_rec, now, now),
        )

    # 4 CRM dialogs:
    # - #1001 & #1002 both belong to lead #101 (Алексей SaaS) -> unified mode must group into 1 row!
    # - #1003 belongs to lead #102 (Мария Ждём) -> waiting
    # - #1004 belongs to lead #103 (Борис Наш Ход) -> our_turn
    dialogs_spec = [
        (1001, 1, 710101, "active_lead", "Алексей SaaS", 1, "2026-11-19T10:30:00", "Жду КП на почту", 1, 101),
        (1002, 2, 710101, "active_lead", "Алексей SaaS", 2, "2026-11-19T09:15:00", "Привет от второй сессии", 1, 101),
        (1003, 1, 710102, "maria_wait", "Мария Ждём", 0, "2026-11-19T10:10:00", "Когда старт?", 1, 102),
        (1004, 2, 710103, "boris_turn", "Борис Наш Ход", 0, "2026-11-19T10:00:00", "Отправили детали", 0, 103),
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

    # Outreach campaign with 2 sent (1 replied) -> Reply Rate = 50% (never 0%)
    conn.execute(
        """
        INSERT INTO campaigns (
            id, name, account_id, account_ids_json, status, daily_limit,
            leadgen_enabled, leadgen_daily_quota, created_at, updated_at
        ) VALUES (1, 'SoftUGC — B2B Outbound', 1, '[1,2]', 'running', 50, 1, 50, ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, username, name, status, current_step, sent_account_id,
            last_sent_at, created_at, updated_at
        ) VALUES (1, 'replied_user', 'Replied Lead', 'replied', 1, 1, ?, ?, ?)
        """,
        (now, now, now),
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, username, name, status, current_step, sent_account_id,
            last_sent_at, created_at, updated_at
        ) VALUES (1, 'sent_user', 'Sent Lead', 'completed', 1, 2, ?, ?, ?)
        """,
        (now, now, now),
    )

    conn.commit()
    conn.close()


def test_inbox_v2_react_unified_e2e_playwright(tmp_path: Path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "inbox_v2_e2e.db"
    admin_password = "InboxV2Playwright!2026"
    _seed_unified_inbox_db(db_path)

    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("BOT_TOKEN", None)
    env["PYTHONPATH"] = str(ROOT)
    env["CRM_REQUIRE_AUTH"] = "1"
    env["CRM_ADMIN_PASSWORD"] = admin_password
    env["CRM_VIEW_MODE"] = "sheet"
    env["CLOUD_BACKUP_ENABLED"] = "0"
    env["DISABLE_TG_WATCHES"] = "0"
    env["DEMO_AUTOSEED"] = "0"
    env["BOOTSTRAP_PROD_SNAPSHOT"] = "0"
    env["AIRTABLE_API_KEY"] = ""
    env["AIRTABLE_BASE_ID"] = ""
    env["AIRTABLE_TOKEN"] = ""

    server_code = f"""
import uvicorn
from pathlib import Path
from fastapi import Body
from app.db import connect, init_db, utc_now
from app.main import create_app
from app.telegram_gateway import FakeGateway

conn = connect(Path({str(db_path)!r}))
init_db(conn)
gw = FakeGateway(
    code="11111",
    password="secret",
    needs_password=False,
    history={{
        710101: [
            {{
                "telegram_message_id": 10001,
                "direction": "in",
                "text": "Жду КП на почту",
                "sent_at": "2026-11-19T10:30:00",
            }},
        ],
        710102: [
            {{
                "telegram_message_id": 10003,
                "direction": "in",
                "text": "Когда старт?",
                "sent_at": "2026-11-19T10:10:00",
            }},
        ],
        710103: [
            {{
                "telegram_message_id": 10004,
                "direction": "out",
                "text": "Отправили детали",
                "sent_at": "2026-11-19T10:00:00",
            }},
        ],
    }},
)
app = create_app(conn=conn, gateway=gw)

@app.post("/__test/push-incoming")
async def _test_push_incoming(payload: dict = Body(...)):
    session_path = str(payload["session_path"])
    peer_id = int(payload["peer_id"])
    msg = {{
        "telegram_user_id": peer_id,
        "telegram_message_id": int(payload["telegram_message_id"]),
        "username": payload.get("username") or "active_lead",
        "name": payload.get("name") or "Алексей SaaS",
        "text": str(payload["text"]),
        "sent_at": str(payload.get("sent_at") or utc_now()),
        "direction": "in",
    }}
    gw.history.setdefault(peer_id, []).append({{
        "telegram_message_id": msg["telegram_message_id"],
        "direction": "in",
        "text": msg["text"],
        "sent_at": msg["sent_at"],
    }})
    gw.push_private(session_path, msg)
    return {{"ok": True}}

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
            except OSError:
                time.sleep(0.15)
        assert ready, "Inbox v2 E2E server did not start in time"

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
  const server5xx = [];

  page.on('pageerror', (err) => pageErrors.push(err.message));
  page.on('console', (msg) => {{
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  }});
  page.on('response', (res) => {{
    if (res.status() >= 500) server5xx.push(`${{res.status()}} ${{res.url()}}`);
  }});

  // 1. Login via real CRM session cookie & verify /sw-inbox.js headers
  const loginRes = await page.request.post('{base_url}/api/auth/login', {{
    data: {{ username: 'tim', password: '{admin_password}' }},
  }});
  if (!loginRes.ok()) throw new Error(`Login failed: ${{loginRes.status()}}`);

  const swRes = await page.request.get('{base_url}/sw-inbox.js');
  if (swRes.status() !== 200) throw new Error(`/sw-inbox.js status ${{swRes.status()}}`);
  const swHeaders = swRes.headers();
  if (swHeaders['service-worker-allowed'] !== '/') {{
    throw new Error(`Missing Service-Worker-Allowed: / header, got ${{JSON.stringify(swHeaders)}}`);
  }}
  if (!(swHeaders['content-type'] || '').includes('javascript')) {{
    throw new Error(`Unexpected /sw-inbox.js content-type: ${{swHeaders['content-type']}}`);
  }}

  // 2. Boot CRM & verify Leads KPIs hydrated (never 0% / 0 / 0)
  await page.goto('{base_url}/', {{ waitUntil: 'domcontentloaded' }});
  await page.locator('#board .lead-card').first().waitFor({{ state: 'visible', timeout: 15000 }});
  await page.waitForFunction(() => {{
    const pool = document.querySelector('#analytics-kpi-pool')?.textContent?.trim() || '';
    const reply = document.querySelector('#analytics-kpi-reply-rate')?.textContent?.trim() || '';
    return pool !== '' && pool !== '0 / 0' && reply !== '' && reply !== '0%';
  }}, {{ timeout: 10000 }});
  const leadsKpiReply = (await page.locator('#analytics-kpi-reply-rate').textContent()).trim();
  const leadsKpiPool = (await page.locator('#analytics-kpi-pool').textContent()).trim();
  if (leadsKpiReply !== '50%' || leadsKpiPool !== '7 / 50') {{
    throw new Error(`Unexpected Leads KPIs: reply=${{leadsKpiReply}}, pool=${{leadsKpiPool}}`);
  }}

  // Verify Service Worker registered cleanly
  await page.waitForFunction(async () => {{
    if (!('serviceWorker' in navigator)) return false;
    const regs = await navigator.serviceWorker.getRegistrations();
    return regs.some((r) => {{
      const script = (r.active || r.waiting || r.installing)?.scriptURL || '';
      return script.endsWith('/sw-inbox.js');
    }});
  }}, {{ timeout: 10000 }});

  // 3. Switch to Inbox & confirm React Inbox v2 mounted cleanly
  await page.locator('a[data-sheet-link="inbox"]').first().click();
  await page.waitForFunction(() => window.__INBOX_V2_ACTIVE__ === true, {{ timeout: 10000 }});
  await page.locator('#inbox-v2-host[data-inbox-v2="mounted"]').waitFor({{ state: 'visible' }});
  await page.locator('#dialog-results .dialog').first().waitFor({{ state: 'visible', timeout: 10000 }});

  // 4. "Один лид — одна строка": 4 raw dialogs collapse into 3 grouped rows; #stat-dialogs stays 4
  const rowCount = await page.locator('#dialog-results .dialog').count();
  if (rowCount !== 3) {{
    throw new Error(`Expected 3 grouped rows (lead 101 collapsed), got ${{rowCount}}`);
  }}
  const alexRow = page.locator('#dialog-results .dialog').filter({{ hasText: 'Алексей SaaS' }});
  if ((await alexRow.count()) !== 1) {{
    throw new Error(`Expected exactly 1 row for Алексей SaaS, got ${{await alexRow.count()}}`);
  }}
  const alexText = await alexRow.first().textContent();
  if (!alexText.includes('2 сессии')) {{
    throw new Error(`Expected '2 сессии' badge on grouped row, got: ${{alexText}}`);
  }}
  const unreadCountText = (await alexRow.first().locator('.count').textContent()).trim();
  if (unreadCountText !== '3') {{
    throw new Error(`Expected aggregated unread=3 (1+2), got ${{unreadCountText}}`);
  }}
  const statDialogs = (await page.locator('#stat-dialogs').textContent()).trim();
  if (statDialogs !== '4') {{
    throw new Error(`Expected #stat-dialogs=4, got ${{statDialogs}}`);
  }}

  // 5. Open Алексей SaaS, inspect Account Picker popover="auto" & switch to Account #2 via POST /api/dialogs/switch-account
  await alexRow.first().click();
  await page.locator('#thread-head').waitFor({{ state: 'visible' }});
  const pickerBtn = page.locator('#inbox-account-picker-btn');
  await pickerBtn.click();
  const pickerPop = page.locator('#inbox-account-picker-popover');
  const popAttr = await pickerPop.getAttribute('popover');
  if (popAttr !== 'auto') {{
    throw new Error(`Expected popover="auto" on Account Picker, got ${{popAttr}}`);
  }}
  const popVisible = await pickerPop.evaluate((el) => el.matches(':popover-open'));
  if (!popVisible) {{
    throw new Error('Account Picker popover did not open via popover="auto"');
  }}
  const popText = await pickerPop.textContent();
  if (!popText.includes('@vladwilhelm') || !popText.includes('@tim_sales')) {{
    throw new Error(`Account Picker missing both sessions: ${{popText}}`);
  }}

  const switchPromise = page.waitForResponse((res) =>
    res.url().includes('/api/dialogs/switch-account') && res.request().method() === 'POST'
  );
  await pickerPop.locator('.inbox-account-option').filter({{ hasText: '@tim_sales' }}).click();
  const switchRes = await switchPromise;
  if (switchRes.status() !== 200) {{
    throw new Error(`switch-account failed with ${{switchRes.status()}}`);
  }}
  await page.locator('#messages').getByText('Привет от второй сессии').waitFor({{ state: 'visible', timeout: 8000 }});
  const pickerBtnAfter = (await pickerBtn.textContent()).trim();
  if (!pickerBtnAfter.includes('@tim_sales')) {{
    throw new Error(`Expected Account Picker button to update to @tim_sales, got ${{pickerBtnAfter}}`);
  }}

  // 6. Draft persistence across dialog switching + Черновик badge
  const draftText = 'Черновик предложения по UGC';
  await page.locator('#reply-text').fill(draftText);
  const mariaRow = page.locator('#dialog-results .dialog').filter({{ hasText: 'Мария Ждём' }}).first();
  await mariaRow.click();
  await page.locator('#messages').getByText('Когда старт?').waitFor({{ state: 'visible' }});

  const alexRowAfterSwitch = page.locator('#dialog-results .dialog').filter({{ hasText: 'Алексей SaaS' }}).first();
  const alexBadgeText = await alexRowAfterSwitch.textContent();
  if (!alexBadgeText.includes('Черновик')) {{
    throw new Error(`Expected 'Черновик' badge on row with saved draft, got: ${{alexBadgeText}}`);
  }}
  await alexRowAfterSwitch.click();
  await page.waitForFunction((expected) => {{
    const el = document.querySelector('#reply-text');
    return el && el.value === expected;
  }}, draftText);

  // 7. Optimistic send + persistence + SSE live patch without full flicker
  const sendText = 'Отправляем кейсы сейчас!';
  await page.locator('#reply-text').fill(sendText);
  const sendRespPromise = page.waitForResponse((res) =>
    /^\\/api\\/dialogs\\/\\d+\\/messages$/.test(new URL(res.url()).pathname) && res.request().method() === 'POST'
  );
  await page.locator('#reply-submit-btn').click();
  // Immediately verify message appears in #messages
  await page.locator('#messages li.out').filter({{ hasText: sendText }}).waitFor({{ state: 'visible', timeout: 5000 }});
  const sendResp = await sendRespPromise;
  if (sendResp.status() !== 200) {{
    throw new Error(`Send message failed with ${{sendResp.status()}}`);
  }}

  // Tag existing DOM nodes in #messages before SSE live patch
  await page.evaluate(() => {{
    window.__firstMsgNodeBeforeSse = document.querySelector('#messages > li');
    window.__msgCountBeforeSse = document.querySelectorAll('#messages > li').length;
  }});

  // Push real incoming message via FakeGateway -> watch_private -> SSE dialog event
  const pushRes = await page.request.post('{base_url}/__test/push-incoming', {{
    data: {{
      session_path: 'data/sessions/w2_acc2.session',
      peer_id: 710101,
      telegram_message_id: 55001,
      username: 'active_lead',
      name: 'Алексей SaaS',
      text: 'Новое сообщение через SSE!',
    }},
  }});
  if (!pushRes.ok()) throw new Error(`push-incoming failed: ${{pushRes.status()}}`);

  await page.locator('#messages li.in').filter({{ hasText: 'Новое сообщение через SSE!' }}).waitFor({{ state: 'visible', timeout: 10000 }});
  const noFlicker = await page.evaluate(() => {{
    const firstNow = document.querySelector('#messages > li');
    const countNow = document.querySelectorAll('#messages > li').length;
    return firstNow === window.__firstMsgNodeBeforeSse && countNow === window.__msgCountBeforeSse + 1;
  }});
  if (!noFlicker) {{
    throw new Error('SSE live patch re-mounted existing message DOM nodes instead of incremental patch!');
  }}

  // 8. Right-hand Client Context Panel (#inbox-client-panel) + quick note persistence
  const panel = page.locator('#inbox-client-panel');
  await panel.waitFor({{ state: 'visible' }});
  const panelText = await panel.textContent();
  if (!panelText.includes('Acme AI') || !panelText.includes('AI SaaS') || !panelText.includes('In Progress')) {{
    throw new Error(`Client context panel missing lead metadata: ${{panelText}}`);
  }}
  const noteText = 'Готов к созвону в пятницу';
  const noteRespPromise = page.waitForResponse((res) =>
    res.url().includes('/api/leads/101/notes') && res.request().method() === 'POST'
  );
  await panel.locator('.inbox-v2-quick-note-form input').fill(noteText);
  await panel.locator('.inbox-v2-quick-note-form button[type="submit"]').click();
  const noteResp = await noteRespPromise;
  if (noteResp.status() !== 200) throw new Error(`Save note failed with ${{noteResp.status()}}`);
  await panel.locator('.inbox-v2-latest-note').filter({{ hasText: noteText }}).waitFor({{ state: 'visible', timeout: 8000 }});

  // Reload to verify persisted sent message + SSE message + quick note
  await page.reload({{ waitUntil: 'domcontentloaded' }});
  await page.locator('a[data-sheet-link="inbox"]').first().click();
  await page.locator('#dialog-results .dialog').filter({{ hasText: 'Алексей SaaS' }}).first().click();
  await page.locator('#messages').getByText(sendText).waitFor({{ state: 'visible', timeout: 10000 }});
  await page.locator('#inbox-client-panel .inbox-v2-latest-note').filter({{ hasText: noteText }}).waitFor({{ state: 'visible', timeout: 10000 }});

  // 9. Keyboard triage (J, K, E, /, ?)
  await page.locator('#s04-title').click();
  const selectedText = async () => (await page.locator('#dialog-results .dialog[aria-current="true"] .name').textContent()).trim();
  const firstSel = await selectedText();
  await page.keyboard.press('j');
  await page.waitForTimeout(150);
  const afterJ = await selectedText();
  if (afterJ === firstSel) throw new Error(`Keyboard J did not advance dialog selection (stayed on ${{afterJ}})`);
  await page.keyboard.press('k');
  await page.waitForTimeout(150);
  const afterK = await selectedText();
  if (afterK !== firstSel) throw new Error(`Keyboard K did not return to ${{firstSel}}, got ${{afterK}}`);

  // Jump to Борис Наш Ход (last row, our_turn), then press E -> jumps to waiting row!
  await page.locator('#dialog-results .dialog').filter({{ hasText: 'Борис Наш Ход' }}).first().click();
  await page.locator('#s04-title').click();
  await page.keyboard.press('e');
  await page.waitForTimeout(150);
  const afterE = await selectedText();
  if (afterE === 'Борис Наш Ход') {{
    throw new Error('Keyboard E failed to jump to a waiting dialog');
  }}

  await page.keyboard.press('/');
  const focusedId = await page.evaluate(() => document.activeElement?.id || '');
  if (focusedId !== 'q') throw new Error(`Keyboard / should focus #q, got #${{focusedId}}`);
  await page.locator('#q').evaluate((el) => el.blur());

  await page.keyboard.press('?');
  const shortcutsOpen = await page.locator('#inbox-shortcuts-popover').evaluate((el) => el.matches(':popover-open'));
  if (!shortcutsOpen) throw new Error('Keyboard ? did not open #inbox-shortcuts-popover');
  await page.keyboard.press('?');

  // 10. Tabs Рассылки (Аккаунты full names unclipped at 1440px) & Статистика (zero CSS regression)
  await page.locator('a[data-sheet-link="outreach"]').first().click();
  await page.locator('#outreach-tab-accounts').click();
  await page.locator('#account-list .account-col-identity').first().waitFor({{ state: 'visible', timeout: 8000 }});
  const accCheck = await page.evaluate(() => {{
    const items = [...document.querySelectorAll('#account-list .account-col-identity')];
    const texts = items.map((el) => ({{
      text: el.textContent || '',
      scrollW: el.scrollWidth,
      clientW: el.clientWidth,
    }}));
    const overflow = document.documentElement.scrollWidth > window.innerWidth;
    return {{ texts, overflow }};
  }});
  if (accCheck.overflow) throw new Error('Horizontal overflow detected on Рассылки -> Аккаунты at 1440px');
  const vladRow = accCheck.texts.find((x) => x.text.includes('Vladislav Wilhelm'));
  if (!vladRow || !vladRow.text.includes('@vladwilhelm')) {{
    throw new Error(`Full account identity missing in Аккаунты: ${{JSON.stringify(accCheck.texts)}}`);
  }}
  if (vladRow.scrollW > vladRow.clientW + 2) {{
    throw new Error(`Account identity clipped at 1440px: scrollWidth=${{vladRow.scrollW}} > clientWidth=${{vladRow.clientW}}`);
  }}

  await page.locator('a[data-sheet-link="analytics"]').first().click();
  await page.locator('#stats-campaigns-grid').waitFor({{ state: 'visible' }});
  await page.locator('#stats-accounts-health').waitFor({{ state: 'visible' }});
  const statsOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  if (statsOverflow) throw new Error('Horizontal overflow detected on Статистика tab at 1440px');

  if (pageErrors.length || consoleErrors.length || server5xx.length) {{
    throw new Error(
      `Browser/Server errors detected: pageErrors=${{JSON.stringify(pageErrors)}} consoleErrors=${{JSON.stringify(consoleErrors)}} server5xx=${{JSON.stringify(server5xx)}}`
    );
  }}

  await browser.close();
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = _resolve_playwright_node_path()
        node_bin = shutil.which("node") or "/opt/homebrew/bin/node"
        proc = subprocess.run(
            [node_bin, "-e", pw_script],
            cwd=str(ROOT / "e2e"),
            env=node_env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert proc.returncode == 0, (
            f"Inbox v2 React Playwright E2E failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
