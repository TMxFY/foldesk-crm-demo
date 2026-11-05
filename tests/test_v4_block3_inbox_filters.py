from fastapi.testclient import TestClient

from app.db import connect, init_db, utc_now
from app.inbox import get_inbox_counts, list_inbox, mark_dialog_read, mark_dialog_unread
from app.main import create_app


def _seed_block3_fixture(conn):
    now = utc_now()
    # Account 1: Telegram
    c1 = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, updated_at, daily_limit, sent_today, provider
        ) VALUES ('+79991112233', 'data/sessions/demo_tg1.session', 'connected', 1001, 'tg_acc', 'TG Acc', ?, 'active', ?, 20, 0, 'telegram')
        """,
        (now, now),
    )
    tg_acc_id = c1.lastrowid

    # Account 2: WhatsApp
    c2 = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, updated_at, daily_limit, sent_today, provider
        ) VALUES ('+79994445566', 'data/sessions/demo_wa1.session', 'connected', 1002, 'wa_acc', 'WA Acc', ?, 'active', ?, 20, 0, 'whatsapp')
        """,
        (now, now),
    )
    wa_acc_id = c2.lastrowid

    # Leads with different statuses
    l1 = conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, phone,
            status, created_at, updated_at, provider
        ) VALUES (?, 5001, 'alice_wait', 'Алиса Ждёт', '+79001110001', 'talking', ?, ?, 'telegram')
        """,
        (tg_acc_id, now, now),
    ).lastrowid

    l2 = conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, phone,
            status, created_at, updated_at, provider
        ) VALUES (?, 5002, 'bob_our_turn', 'Боб Наш Ход', '+79001110002', 'waiting', ?, ?, 'telegram')
        """,
        (tg_acc_id, now, now),
    ).lastrowid

    l3 = conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, phone,
            status, created_at, updated_at, provider
        ) VALUES (?, 5003, 'clara_wa', 'Клара WA', '+79001110003', 'won', ?, ?, 'whatsapp')
        """,
        (wa_acc_id, now, now),
    ).lastrowid

    # Dialog 1: waiting (last_from_peer=1), unread=2, telegram, lead status=talking
    d1 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 5001, 'alice_wait', 'Алиса Ждёт', '+79001110001', 'telegram', 0, 2, '2025-02-01T12:00:00', 'Жду КП', 1, 0, ?, ?, ?)
        """,
        (tg_acc_id, l1, now, now),
    ).lastrowid

    # Dialog 2: our_turn (last_from_peer=0), unread=0, muted=1, telegram, lead status=waiting
    d2 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 5002, 'bob_our_turn', 'Боб Наш Ход', '+79001110002', 'telegram', 0, 0, '2025-02-01T11:00:00', 'Отправил презентацию', 0, 1, ?, ?, ?)
        """,
        (tg_acc_id, l2, now, now),
    ).lastrowid

    # Dialog 3: group (is_group=1), waiting (last_from_peer=1), unread=5, muted=1, telegram
    d3 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'chat', 5099, 'b2b_group', 'Чат Партнёров', NULL, 'telegram', 1, 5, '2025-02-01T10:00:00', 'Обсуждение условий', 1, 1, NULL, ?, ?)
        """,
        (tg_acc_id, now, now),
    ).lastrowid

    # Dialog 4: whatsapp, our_turn (last_from_peer=0), unread=0, muted=0, lead status=won
    d4 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 5003, 'clara_wa', 'Клара WA', '+79001110003', 'whatsapp', 0, 0, '2025-02-01T09:00:00', 'Оплатили счёт', 0, 0, ?, ?, ?)
        """,
        (wa_acc_id, l3, now, now),
    ).lastrowid

    conn.commit()
    return {
        "tg_acc_id": tg_acc_id,
        "wa_acc_id": wa_acc_id,
        "dialogs": {"d1": d1, "d2": d2, "d3": d3, "d4": d4},
        "leads": {"l1": l1, "l2": l2, "l3": l3},
    }


def test_inbox_server_chip_account_provider_status_filters_and_counts(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    fx = _seed_block3_fixture(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        # 1. Full inbox with counts (account_id=0)
        res = client.get("/api/inbox", params={"account_id": 0, "with_counts": 1})
        assert res.status_code == 200
        payload = res.json()
        assert "dialogs" in payload and "counts" in payload
        assert len(payload["dialogs"]) == 4
        assert payload["counts"] == {
            "waiting": 2,   # d1, d3
            "our_turn": 2,  # d2, d4
            "unread": 2,    # d1 (2), d3 (5)
            "groups": 1,    # d3
            "muted": 2,     # d2, d3
        }

        # 2. Chip filters
        waiting = client.get("/api/inbox", params={"account_id": 0, "chip": "waiting"}).json()
        assert {d["id"] for d in waiting} == {fx["dialogs"]["d1"], fx["dialogs"]["d3"]}

        our_turn = client.get("/api/inbox", params={"account_id": 0, "chip": "our_turn"}).json()
        assert {d["id"] for d in our_turn} == {fx["dialogs"]["d2"], fx["dialogs"]["d4"]}

        unread = client.get("/api/inbox", params={"account_id": 0, "chip": "unread"}).json()
        assert {d["id"] for d in unread} == {fx["dialogs"]["d1"], fx["dialogs"]["d3"]}

        groups = client.get("/api/inbox", params={"account_id": 0, "chip": "groups"}).json()
        assert [d["id"] for d in groups] == [fx["dialogs"]["d3"]]

        muted = client.get("/api/inbox", params={"account_id": 0, "chip": "muted"}).json()
        assert {d["id"] for d in muted} == {fx["dialogs"]["d2"], fx["dialogs"]["d3"]}

        # Combined chips: waiting + muted -> only d3
        waiting_muted = client.get("/api/inbox", params={"account_id": 0, "chip": "waiting,muted"}).json()
        assert [d["id"] for d in waiting_muted] == [fx["dialogs"]["d3"]]

        # 3. Filter by account_id
        tg_only = client.get("/api/inbox", params={"account_id": fx["tg_acc_id"], "with_counts": 1}).json()
        assert {d["id"] for d in tg_only["dialogs"]} == {
            fx["dialogs"]["d1"],
            fx["dialogs"]["d2"],
            fx["dialogs"]["d3"],
        }
        assert tg_only["counts"] == {
            "waiting": 2,
            "our_turn": 1,
            "unread": 2,
            "groups": 1,
            "muted": 2,
        }

        wa_only = client.get("/api/inbox", params={"account_id": fx["wa_acc_id"]}).json()
        assert [d["id"] for d in wa_only] == [fx["dialogs"]["d4"]]

        # 4. Filter by provider (telegram / whatsapp)
        prov_tg = client.get("/api/inbox", params={"account_id": 0, "provider": "telegram"}).json()
        assert {d["id"] for d in prov_tg} == {
            fx["dialogs"]["d1"],
            fx["dialogs"]["d2"],
            fx["dialogs"]["d3"],
        }

        prov_wa = client.get("/api/inbox", params={"account_id": 0, "provider": "whatsapp"}).json()
        assert [d["id"] for d in prov_wa] == [fx["dialogs"]["d4"]]

        # 5. Filter by lead status
        st_talking = client.get("/api/inbox", params={"account_id": 0, "status": "talking"}).json()
        assert [d["id"] for d in st_talking] == [fx["dialogs"]["d1"]]

        st_waiting = client.get("/api/inbox", params={"account_id": 0, "status": "waiting"}).json()
        assert [d["id"] for d in st_waiting] == [fx["dialogs"]["d2"]]

        st_won = client.get("/api/inbox", params={"account_id": 0, "status": "won"}).json()
        assert [d["id"] for d in st_won] == [fx["dialogs"]["d4"]]

        # 6. Dedicated /api/inbox/counts endpoint
        counts_resp = client.get("/api/inbox/counts", params={"account_id": 0})
        assert counts_resp.status_code == 200
        assert counts_resp.json()["counts"] == {
            "waiting": 2,
            "our_turn": 2,
            "unread": 2,
            "groups": 1,
            "muted": 2,
        }


def test_mark_dialog_unread_and_read_routes(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    fx = _seed_block3_fixture(conn)
    app = create_app(conn=conn, gateway=None)

    d1 = fx["dialogs"]["d1"]  # starts with unread_count=2, last_from_peer=1
    d2 = fx["dialogs"]["d2"]  # starts with unread_count=0, last_from_peer=0

    with TestClient(app) as client:
        # Mark d2 (unread_count=0, last_from_peer=0) as unread -> unread_count=1, last_from_peer=1
        r_unread = client.post(f"/api/inbox/dialogs/{d2}/unread")
        assert r_unread.status_code == 200
        body_u = r_unread.json()
        assert body_u["id"] == d2
        assert body_u["unread_count"] == 1
        assert body_u["last_from_peer"] == 1
        assert body_u["counts"]["unread"] == 3
        assert body_u["counts"]["waiting"] == 3
        assert body_u["counts"]["our_turn"] == 0

        # Mark d1 (unread_count=2) as unread -> keeps MAX(1, 2) == 2
        r_unread_d1 = client.post(f"/api/inbox/dialogs/{d1}/unread")
        assert r_unread_d1.status_code == 200
        assert r_unread_d1.json()["unread_count"] == 2
        assert r_unread_d1.json()["last_from_peer"] == 1

        # Mark d1 as read -> unread_count becomes 0
        r_read_d1 = client.post(f"/api/inbox/dialogs/{d1}/read")
        assert r_read_d1.status_code == 200
        body_r = r_read_d1.json()
        assert body_r["id"] == d1
        assert body_r["unread_count"] == 0
        assert body_r["counts"]["unread"] == 2  # d2 (1) and d3 (5)

        # Nonexistent dialog returns 404
        assert client.post("/api/inbox/dialogs/999999/unread").status_code == 404
        assert client.post("/api/inbox/dialogs/999999/read").status_code == 404

    # Direct Python helpers verification
    res_list = list_inbox(conn, account_id=0)
    assert hasattr(res_list, "counts")
    assert res_list.counts == get_inbox_counts(conn, account_id=0)
    direct_read = mark_dialog_read(conn, d2)
    assert direct_read["unread_count"] == 0
    direct_unread = mark_dialog_unread(conn, d2)
    assert direct_unread["unread_count"] == 1
    assert direct_unread["last_from_peer"] == 1


def test_status_filter_matches_phone_and_at_username_and_large_inbox_over_60(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    fx = _seed_block3_fixture(conn)
    now = utc_now()

    # 1. Lead with phone only (no telegram_user_id, no username) + dialog with unlinked lead_id=NULL
    conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, phone,
            status, created_at, updated_at, provider
        ) VALUES (?, NULL, NULL, 'Телефонный Лид', '+7 (900) 555-77-88', 'lost', ?, ?, 'whatsapp')
        """,
        (fx["wa_acc_id"], now, now),
    )
    phone_only_dialog = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 79005557788, NULL, 'Телефонный Лид', '+79005557788', 'whatsapp', 0, 1, '2025-02-01T08:00:00', 'Прайс?', 1, 0, NULL, ?, ?)
        """,
        (fx["wa_acc_id"], now, now),
    ).lastrowid

    # 2. Lead with @-prefixed username and dialog with plain username, lead_id=NULL
    conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, phone,
            status, created_at, updated_at, provider
        ) VALUES (NULL, NULL, '@at_prefixed_lead', 'Лид с собачкой', NULL, 'new', ?, ?, 'telegram')
        """,
        (now, now),
    )
    at_user_dialog = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 99001, 'at_prefixed_lead', 'Лид с собачкой', NULL, 'telegram', 0, 0, '2025-02-01T07:30:00', 'Старт', 0, 0, NULL, ?, ?)
        """,
        (fx["tg_acc_id"], now, now),
    ).lastrowid

    # 3. Insert 65 additional dialogs so total dialogs > 60 (71 total)
    tail_dialog_id = None
    for i in range(65):
        did = conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, phone, provider,
                is_group, unread_count, last_message_at, last_message_preview,
                last_from_peer, muted, lead_id, created_at, updated_at
            ) VALUES (?, 'user', ?, ?, ?, NULL, 'telegram', 0, 1, ?, 'Пакет', 1, 0, ?, ?, ?)
            """,
            (
                fx["tg_acc_id"],
                80000 + i,
                f"bulk_user_{i}",
                f"Массовый Клиент {i}",
                f"2025-01-01T00:{i % 60:02d}:00",
                fx["leads"]["l1"],  # status='talking'
                now,
                now,
            ),
        ).lastrowid
        if i == 64:
            tail_dialog_id = did
    conn.commit()

    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        # Phone-only lead status filter works without lead_id
        lost_rows = client.get("/api/inbox", params={"account_id": 0, "status": "lost"}).json()
        assert [d["id"] for d in lost_rows] == [phone_only_dialog]

        # @username lead status filter works without lead_id
        new_rows = client.get("/api/inbox", params={"account_id": 0, "status": "new"}).json()
        assert [d["id"] for d in new_rows] == [at_user_dialog]

        # Unfiltered inbox caps at 60, but counts reflect all 71 dialogs
        unfiltered = client.get("/api/inbox", params={"account_id": 0, "with_counts": 1}).json()
        assert len(unfiltered["dialogs"]) == 60
        assert unfiltered["counts"]["waiting"] == 2 + 1 + 65  # d1, d3 + phone_only + 65 bulk

        # Explicit chip filter returns all 68 waiting dialogs (not truncated to 60 or 30)
        all_waiting = client.get("/api/inbox", params={"account_id": 0, "chip": "waiting"}).json()
        assert len(all_waiting) == 68
        assert tail_dialog_id in {d["id"] for d in all_waiting}

        # Combined status + provider + q finds tail dialog beyond 60
        combo = client.get(
            "/api/inbox",
            params={
                "account_id": 0,
                "status": "talking",
                "provider": "telegram",
                "q": "bulk_user_64",
            },
        ).json()
        assert [d["id"] for d in combo] == [tail_dialog_id]


def test_playwright_inbox_chips_and_mark_unread(tmp_path):
    import shutil
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    pw_module = Path("/Users/timofei/.claude/skills/gstack/node_modules/playwright")
    chrome_bin = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not shutil.which("node") or not pw_module.exists() or not chrome_bin.exists():
        return

    db_path = tmp_path / "pw_block3.db"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server_code = f"""
import os
os.environ["PYTEST_CURRENT_TEST"] = "1"
import uvicorn
from app.db import connect, init_db
from app.demo import seed_demo
from app.main import create_app

conn = connect({str(db_path)!r})
init_db(conn)
seed_demo(conn)
app = create_app(conn=conn, gateway=None)
uvicorn.run(app, host="127.0.0.1", port={port}, log_level="error")
"""
    proc = subprocess.Popen([sys.executable, "-c", server_code])
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)

        js = f"""
const {{ chromium }} = require({str(pw_module)!r});
(async () => {{
  const browser = await chromium.launch({{
    headless: true,
    executablePath: {str(chrome_bin)!r}
  }});
  const page = await browser.newPage({{ viewport: {{ width: 1440, height: 900 }} }});
  page.setDefaultTimeout(5000);
  await page.goto("http://127.0.0.1:{port}/#inbox", {{ waitUntil: "domcontentloaded" }});
  await page.waitForTimeout(800);

  const crmModeBtn = page.locator("#inbox-mode-crm");
  if ((await crmModeBtn.count()) > 0 && (await crmModeBtn.isVisible())) {{
    await crmModeBtn.click();
    await page.waitForTimeout(300);
  }}

  const waitingChip = page.locator("#s04 [data-chip=\\"waiting\\"]");
  const unreadChip = page.locator("#s04 [data-chip=\\"unread\\"]");
  const wText = await waitingChip.textContent();
  const uText = await unreadChip.textContent();
  if (!wText.includes("(") || !uText.includes("(")) {{
    throw new Error("Missing chip counts: " + wText + " / " + uText);
  }}

  const totalDialogs = await page.locator("#dialog-results li").count();
  await unreadChip.click();
  await page.waitForTimeout(300);
  const filteredDialogs = await page.locator("#dialog-results li").count();
  if (filteredDialogs < 1 || filteredDialogs >= totalDialogs) {{
    throw new Error("Expected filtered unread dialogs, got " + filteredDialogs);
  }}

  // Click an unread dialog WHILE unread chip is active -> stays visible as active, clears .count, and #mark-unread restores it
  const firstFiltered = page.locator("#dialog-results li button.dialog").first();
  await firstFiltered.click();
  await page.waitForTimeout(300);
  const activeInUnread = page.locator("#dialog-results li button.dialog[aria-current=\\"true\\"]");
  if ((await activeInUnread.count()) !== 1) {{
    throw new Error("Selected dialog vanished while unread chip was active");
  }}

  await page.locator("#mark-unread").click();
  await page.waitForTimeout(300);
  const activeBadge = page.locator("#dialog-results li button.dialog[aria-current=\\"true\\"] .count");
  if ((await activeBadge.count()) !== 1 || Number(await activeBadge.textContent()) < 1) {{
    throw new Error("Expected unread badge after #mark-unread click");
  }}
  await browser.close();
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr or r.stdout
    finally:
        proc.terminate()
        proc.wait(timeout=5)


