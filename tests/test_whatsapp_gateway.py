import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
from fastapi.testclient import TestClient

from app.db import apply_zero_fake_wa_antiban_v10, connect, init_db, resolve_session
from app.hygiene import audit_crm_hygiene, heal_crm_hygiene
from app.main import create_app
from app.outreach import create_campaign, run_campaign_batch
from app.telegram_gateway import GatewayError
from app.whatsapp_gateway import ChatPresence, DEFAULT_WA_DAILY_LIMIT, WA_MIN_COLD_INTERVAL_SECONDS, WhatsAppGateway
from real_session_helpers import (
    make_protocol_wa_client_factory,
    make_real_whatsmeow_db,
)


def test_whatsapp_live_neonize_qr_and_fail_fast_confirm(tmp_path: Path, monkeypatch):
    """Real neonize NewAClient emits authentic WhatsApp Multi-Device QR SVG without pairing fallback."""
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)

    db_path = tmp_path / "crm_live_qr.db"
    conn = connect(db_path)
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        started = client.post(
            "/api/accounts/start",
            json={"phone": "", "provider": "whatsapp", "auth_mode": "qr"},
        )
        assert started.status_code == 200
        data = started.json()
        acc_id = int(data["id"])
        assert data["provider"] == "whatsapp"
        assert data["status"] == "pending_code"
        assert data["pairing_code"] is None
        assert data["qr_code"].startswith("data:image/svg+xml;base64,")
        assert len(data["qr_code"]) > 1200  # Dense Multi-Device cryptographic QR
        svg_decoded = base64.b64decode(data["qr_code"].split(",", 1)[1]).decode("utf-8")
        assert "<svg" in svg_decoded and "<path" in svg_decoded and "</svg>" in svg_decoded

        # Code confirmation BEFORE scanning on phone MUST fail fast (no fake stub!)
        early = client.post(f"/api/accounts/{acc_id}/code", json={"code": "qr"})
        assert early.status_code in (400, 502)
        assert "привязан" in early.json()["detail"].lower()

        # Starting code-mode login without valid E.164 phone fails fast with 400
        bad_code_start = client.post(
            "/api/accounts/start",
            json={"phone": "", "provider": "whatsapp", "auth_mode": "code"},
        )
        assert bad_code_start.status_code == 400

        # Invalid proxy ("auto") fails fast with 400
        bad_proxy = client.post(
            "/api/accounts/start",
            json={"phone": "+31612345678", "provider": "whatsapp", "auth_mode": "qr", "proxy_url": "auto"},
        )
        assert bad_proxy.status_code == 400

        asyncio.run(app.state.wa_gateway.close_all())


def test_whatsapp_proxy_import_antiban_v10_and_hygiene(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "crm_wa_antiban.db"
    conn = connect(db_path)
    init_db(conn)

    # 1. Protocol proxy wiring + Pairing + PairStatus DB confirm + is_on_whatsapp + typing presence
    factory, records = make_protocol_wa_client_factory(
        registered_phones={"+31648462612"},
        pairing_code="WA48-7719",
        pair_on_connect={"jid": "447700900111:2@s.whatsapp.net", "push_name": "SoftUGC UK"},
    )
    wa_gw = WhatsAppGateway(client_factory=factory, db_path=db_path)
    app = create_app(conn=conn, gateway=None, wa_gateway=wa_gw)

    with TestClient(app) as client:
        st = client.post(
            "/api/accounts/start",
            json={
                "phone": "+447700900111",
                "provider": "whatsapp",
                "auth_mode": "code",
                "proxy_url": "socks5://u:p@198.51.100.2:1080",
            },
        )
        assert st.status_code == 200
        s_data = st.json()
        assert s_data["pairing_code"] == "WA48-7719"
        assert records["connect_proxies"] == ["socks5://u:p@198.51.100.2:1080"]

        conf = client.post(f"/api/accounts/{s_data['id']}/code", json={"code": "WA48-7719"})
        assert conf.status_code == 200
        assert conf.json()["status"] == "connected"

        # import-session MUST reject fake/empty WA DB bytes and accept real whatsmeow_device SQLite DB
        fake_imp = client.post(
            "/api/accounts/import-session",
            json={
                "provider": "whatsapp",
                "sessions": [
                    {
                        "filename": "wa_fake.session",
                        "phone": "+447700900222",
                        "session_base64": base64.b64encode(b"not a real whatsmeow db").decode("ascii"),
                    }
                ],
            },
        )
        assert fake_imp.status_code == 400

        real_wa_file = make_real_whatsmeow_db(
            tmp_path / "import_real_wa.db",
            jid="447700900333:1@s.whatsapp.net",
            push_name="Imported Real WA",
        )
        good_imp = client.post(
            "/api/accounts/import-session",
            json={
                "provider": "whatsapp",
                "proxy_url": "http://u:p@203.0.113.5:8080",
                "sessions": [
                    {
                        "filename": "wa_447700900333.session",
                        "session_base64": base64.b64encode(real_wa_file.read_bytes()).decode("ascii"),
                    }
                ],
            },
        )
        assert good_imp.status_code == 200
        imp_acc = good_imp.json()["imported"][0]
        assert imp_acc["provider"] == "whatsapp"
        assert imp_acc["phone"] == "+447700900333"
        assert imp_acc["wa_jid"] == "447700900333@s.whatsapp.net"
        assert imp_acc["daily_limit"] == DEFAULT_WA_DAILY_LIMIT

        # PATCH settings rejects "auto" for WhatsApp with 400 and applies valid SOCKS5 proxy
        bad_patch = client.patch(
            f"/api/accounts/{s_data['id']}/settings",
            json={"proxy_url": "auto"},
        )
        assert bad_patch.status_code == 400

        good_patch = client.patch(
            f"/api/accounts/{s_data['id']}/settings",
            json={"proxy_url": "socks5://u:p@198.51.100.99:1080"},
        )
        assert good_patch.status_code == 200
        assert records["proxy_updates"] == ["socks5://u:p@198.51.100.99:1080"]

    # 2. Outreach: unregistered phone skipped as completed without breaking WA account;
    #    registered phone passes is_on_whatsapp + ChatPresence; 480s cold interval blocks burst!
    camp = create_campaign(
        conn,
        name="WA Cold Safe Test",
        account_id=imp_acc["id"],
        daily_limit=DEFAULT_WA_DAILY_LIMIT,
        steps=[{"template_text": "Hey, saw {company} — running organic reels yet?"}],
    )
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    for phone, comp in (("+999000111222", "Ghost Shop"), ("+31648462612", "Witlof Skincare"), ("+31648462699", "Second Shop")):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, phone, name, company, website, status, current_step, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'https://witlofskincare.nl', 'pending', 0, ?, ?)
            """,
            (camp["id"], phone, comp, comp, now_iso, now_iso),
        )
    conn.commit()

    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "0")
    out1 = asyncio.run(
        run_campaign_batch(
            conn,
            camp["id"],
            gateway=None,
            wa_gateway=wa_gw,
            respect_delays=False,
        )
    )
    # Ghost Shop fails IsOnWhatsApp (completed skip, account stays active); Witlof Skincare sends 1;
    # Second Shop is held by WA_MIN_COLD_INTERVAL_SECONDS (480s) pacing!
    assert WA_MIN_COLD_INTERVAL_SECONDS == 480
    assert out1["sent"] == 1
    assert len(records["sent_messages"]) == 1
    assert records["sent_messages"][0][0] == "31648462612@s.whatsapp.net"
    assert [c[1] for c in records["presence_calls"]] == [
        ChatPresence.CHAT_PRESENCE_COMPOSING,
        ChatPresence.CHAT_PRESENCE_PAUSED,
    ]
    rows_after = {
        r["phone"]: (r["status"], r["error_text"])
        for r in conn.execute("SELECT phone, status, error_text FROM campaign_recipients WHERE campaign_id = ?", (camp["id"],)).fetchall()
    }
    assert rows_after["+999000111222"][0] == "completed"
    assert "не зарегистрирован" in (rows_after["+999000111222"][1] or "").lower()
    assert rows_after["+31648462612"][0] == "completed"
    assert rows_after["+31648462699"][0] == "pending"

    # 3. Migration v10: caps Leadgen Auto (WA) daily_limit=6 and cleans u003e email + Contact Us name
    conn.execute("DELETE FROM kv_settings WHERE key = 'zero_fake_wa_antiban_v10'")
    create_campaign(conn, name="Leadgen Auto (WA)", daily_limit=20, status="draft")
    conn.execute("INSERT INTO leads (name, company, phone, provider, email, lead_source, status, created_at, updated_at) VALUES ('Contact information', 'Renelle Cosmetics', '+971556440893', 'whatsapp', 'u003einfo@renellecosmetics.com', 'd2c_whatsapp', 'waiting', ?, ?)", (now_iso, now_iso))
    conn.commit()

    v10 = apply_zero_fake_wa_antiban_v10(conn)
    assert v10["already_applied"] is False
    assert v10["wa_campaigns_updated"] == 1
    assert v10["emails_fixed"] == 1
    assert v10["names_fixed"] == 1
    cleaned = conn.execute("SELECT name, email FROM leads WHERE company = 'Renelle Cosmetics'").fetchone()
    assert cleaned["name"] == "Renelle Cosmetics"
    assert cleaned["email"] == "info@renellecosmetics.com"

    # 4. Disconnected WhatsApp session in outreach marks account unauthorized WITHOUT failing recipients
    dead_sess_path = tmp_path / "missing_wa.session"
    conn.execute(
        "UPDATE accounts SET session_path = ?, health = 'active', sent_today = 0 WHERE id = ?",
        (str(dead_sess_path), imp_acc["id"]),
    )
    conn.commit()
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")
    out_dead = asyncio.run(
        run_campaign_batch(
            conn,
            camp["id"],
            gateway=None,
            wa_gateway=wa_gw,
            respect_delays=False,
        )
    )
    assert out_dead["sent"] == 0
    acc_after_dead = conn.execute("SELECT health FROM accounts WHERE id = ?", (imp_acc["id"],)).fetchone()
    assert acc_after_dead["health"] == "unauthorized"
    rec_after_dead = conn.execute(
        "SELECT status FROM campaign_recipients WHERE campaign_id = ? AND phone = '+31648462699'",
        (camp["id"],),
    ).fetchone()
    assert rec_after_dead["status"] == "pending"

    # 5. audit_crm_hygiene detects forged stub session file on disk and heal_crm_hygiene quarantines it
    bad_sess_file = tmp_path / "bad_stub.session"
    bad_sess_file.write_bytes(b"short stub")
    conn.execute(
        "UPDATE accounts SET session_path = ?, status = 'connected' WHERE id = ?",
        (str(bad_sess_file), imp_acc["id"]),
    )
    conn.commit()
    hyg = audit_crm_hygiene(conn)
    assert hyg["ok"] is False
    assert hyg["fake_session_account_count"] >= 1
    assert imp_acc["id"] in hyg["fake_session_account_ids"]

    healed = heal_crm_hygiene(conn)
    assert healed["healed_fake_accounts"] >= 1
    hyg_after = audit_crm_hygiene(conn)
    assert hyg_after["fake_session_account_count"] == 0
    assert not resolve_session("data/sessions/wa_1.session").exists()
    assert not resolve_session("data/sessions/wa_447700900333.session").exists()
    conn.close()


def test_gate_h_verify_sh_rejects_fake_session_and_stub_tokens(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent
    verify_sh = repo_root / "scripts" / "verify.sh"

    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "PYTHON_BIN": sys.executable}
    # Sandbox minimal tree with fake token -> Gate H must exit 1
    sb = tmp_path / "sandbox"
    for d in ("scripts", "app", "leadgen", "tests", "deploy/systemd", ".claude/skills/replica-design", ".claude/skills/replica-launch", "replica/design", "replica/launch"):
        (sb / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2(verify_sh, sb / "scripts" / "verify.sh")
    shutil.copy2(repo_root / ".claude/skills/replica-design/contrast.py", sb / ".claude/skills/replica-design/contrast.py")
    shutil.copy2(repo_root / ".claude/skills/replica-launch/listing.py", sb / ".claude/skills/replica-launch/listing.py")
    shutil.copy2(repo_root / "replica/design/tokens.json", sb / "replica/design/tokens.json")
    shutil.copy2(repo_root / "replica/launch/listing.json", sb / "replica/launch/listing.json")
    (sb / "leadgen" / "__init__.py").write_text("", encoding="utf-8")
    (sb / "leadgen" / "vps_autonomous_replenish.py").write_text("import sys\nif '--help' in sys.argv: sys.exit(0)\n", encoding="utf-8")
    (sb / "leadgen" / "tg_ad_spy.py").write_text("DEFAULT_AD_SPY_CHANNELS = ('a',)\n", encoding="utf-8")
    (sb / "leadgen" / "tg_channel_graph.py").write_text("DEFAULT_GRAPH_SEED_CHANNELS = ('a',)\n", encoding="utf-8")
    (sb / "app" / "bad_fake.py").write_text("SAMPLE_SESSION_JSON = '{}'\n", encoding="utf-8")

    res = subprocess.run(
        ["bash", str(sb / "scripts" / "verify.sh"), "--fast-checks-only"],
        cwd=str(sb),
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    assert "Gate H violation" in res.stderr
    assert "SAMPLE_SESSION_JSON" in res.stderr


def test_stop_outreach_on_reply_quarantines_micro_vacancy_and_suppresses_false_alert(
    tmp_path: Path, monkeypatch
):
    from app.digest import audit_reply_notifications
    from app.inbox import stop_outreach_on_reply

    db_path = tmp_path / "crm_reply_sniper.db"
    conn = connect(db_path)
    init_db(conn)
    monkeypatch.setenv("COLLEAGUE_BOT_TOKEN", "123456:TESTTOKEN")
    monkeypatch.setenv("COLLEAGUE_CHAT_ID", "978069541")
    monkeypatch.delenv("DISABLE_BOT_ALERTS", raising=False)

    now_iso = "2026-11-18T08:55:00"
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, telegram_user_id, telegram_username, telegram_name, health, created_at)
        VALUES (27, '+79088819595', 'data/sessions/27.session', 'connected', 270001, 'vladwilhelm', 'Vlad', 'active', ?)
        """,
        (now_iso,),
    )
    camp = create_campaign(conn, name="Vacancy Legacy", account_id=27, daily_limit=20, status="draft")
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name, company,
            status, current_step, assigned_account_id, sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, NULL, 8772940400, 'asel_pest_hr', 'Иллюстратор детской книги на нейросетях (@asel_pest_hr)', 'Иллюстратор детской книги на нейросетях', 'completed', 1, 27, 27, '2026-11-17T09:31:25', ?, ?)
        """,
        (camp["id"], now_iso, now_iso),
    )
    dlg_cur = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_id, username, title, peer_type, unread_count, last_from_peer, last_message_at, created_at, updated_at
        ) VALUES (27, 8772940400, 'asel_pest_hr', 'Иллюстратор детской книги на нейросетях', 'user', 1, 1, '2026-11-18T08:55:00', ?, ?)
        """,
        (now_iso, now_iso),
    )
    dlg_id = int(dlg_cur.lastrowid)
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 175355, 'in', 'Никакой вакансии не было. Мы вынужденно клепаем фейки. @trackly_official_bot', '2026-11-18T08:55:00', ?, ?)
        """,
        (dlg_id, now_iso, now_iso),
    )
    conn.commit()

    spy_calls: list[tuple[str, bytes | None]] = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true, "result": {"message_id": 999}}'

    def spy_opener(req, timeout=10):
        spy_calls.append((req.full_url, req.data))
        return _FakeResp()

    stop_outreach_on_reply(
        conn,
        account_id=27,
        peer_id=8772940400,
        username="asel_pest_hr",
        preview="Никакой вакансии не было. Мы вынужденно клепаем фейки. @trackly_official_bot",
        notify=True,
        incoming_unread=True,
        telegram_message_id=175355,
        incoming_sent_at="2026-11-18T08:55:00",
        opener=spy_opener,
    )

    assert spy_calls == []
    notif_count = conn.execute("SELECT COUNT(*) FROM bot_reply_notifications").fetchone()[0]
    assert int(notif_count) == 0
    lead_row = conn.execute("SELECT status, airtable_status, next_step FROM leads WHERE telegram_user_id = 8772940400").fetchone()
    assert lead_row is not None
    assert lead_row["status"] == "lost"
    assert lead_row["airtable_status"] == "Cancelled"
    assert audit_crm_hygiene(conn)["ok"] is True
    assert audit_reply_notifications(conn)["ok"] is True
    conn.close()
