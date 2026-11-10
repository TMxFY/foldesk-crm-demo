import warnings
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import (
    TelethonGateway,
    _parse_proxy_for_telethon,
    build_nodemaven_proxy_url,
    infer_proxy_country,
    make_nodemaven_sid,
    mask_proxy_url,
)


def test_telethon_proxy_no_pysocks_warning():
    from telethon import TelegramClient

    proxy_url = "socks5://nmuser-country-us-sid-foldeskacc1-ttl-86400-filter-medium:secret123@gate.nodemaven.com:1080"
    proxy_tuple = _parse_proxy_for_telethon(proxy_url)
    assert proxy_tuple is not None
    assert proxy_tuple[1] == "gate.nodemaven.com"
    assert proxy_tuple[2] == 1080
    assert proxy_tuple[3] is True
    assert proxy_tuple[4] == "nmuser-country-us-sid-foldeskacc1-ttl-86400-filter-medium"
    assert proxy_tuple[5] == "secret123"

    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        client = TelegramClient(
            None,
            2040,
            "b18441a1ff607e10a989891a5462e627",
            proxy=proxy_tuple,
        )
        assert client is not None


def test_parse_raw_nodemaven_colon_format():
    raw = "gate.nodemaven.com:1080:myuser-country-de-sid-foldeskacc2:mypass"
    proxy_tuple = _parse_proxy_for_telethon(raw)
    assert proxy_tuple is not None
    assert proxy_tuple[1] == "gate.nodemaven.com"
    assert proxy_tuple[2] == 1080
    assert proxy_tuple[4] == "myuser-country-de-sid-foldeskacc2"
    assert proxy_tuple[5] == "mypass"


def test_ten_accounts_get_unique_sticky_nodemaven_sessions(tmp_path):
    conn = connect(tmp_path / "nm.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO kv_settings (key, value, updated_at) VALUES ('nodemaven_user', 'tim_nm_user-country-us-sid-old123', '2026-11-05T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO kv_settings (key, value, updated_at) VALUES ('nodemaven_pass', 'nm_super_secret', '2026-11-05T00:00:00Z')"
    )
    conn.commit()

    phones = [
        "+14155550101",
        "+79995550102",
        "+77015550103",
        "+447700900104",
        "+4915123456105",
        "+380501234106",
        "+971501234107",
        "+33612345108",
        "+31612345109",
        "+14155550110",
    ]
    expected_countries = ["us", "kz", "kz", "gb", "de", "ua", "ae", "fr", "nl", "us"]

    urls = []
    sids = set()
    for idx, (phone, exp_iso) in enumerate(zip(phones, expected_countries), start=1):
        url = build_nodemaven_proxy_url(account_key=idx, phone=phone, preset="auto", conn=conn)
        assert url is not None
        assert f"-country-{exp_iso}-" in url
        sid = make_nodemaven_sid(idx)
        assert f"-sid-{sid}-" in url
        assert "-sid-old123" not in url
        assert url.endswith(":nm_super_secret@gate.nodemaven.com:1080")
        urls.append(url)
        sids.add(sid)

    assert len(sids) == 10
    assert len(set(urls)) == 10


def test_gateway_resolves_nodemaven_and_api_endpoints(tmp_path):
    conn = connect(tmp_path / "api_nm.db")
    init_db(conn)
    for i in range(1, 4):
        conn.execute(
            """
            INSERT INTO accounts (id, phone, telegram_username, telegram_name, session_path, status, proxy_url, created_at)
            VALUES (?, ?, ?, ?, ?, 'connected', 'auto', '2026-11-05T00:00:00Z')
            """,
            (i, f"+1415000000{i}", f"acc{i}", f"Account {i}", f"data/sessions/{i}.session"),
        )
    conn.commit()

    gw = TelethonGateway()
    app = create_app(conn=conn, gateway=gw)
    with TestClient(app) as client:
        st0 = client.get("/api/proxy/nodemaven/config").json()
        assert st0["ok"] is True
        assert st0["configured"] is False
        assert len(st0["accounts"]) == 3

        save_res = client.post(
            "/api/proxy/nodemaven/config",
            json={
                "user": "timofei_nm",
                "password": "secret_password_99",
                "host": "gate.nodemaven.com",
                "port": 1080,
                "ttl": 86400,
                "filter": "medium",
                "default_country": "us",
            },
        )
        assert save_res.status_code == 200
        saved = save_res.json()
        assert saved["configured"] is True
        assert saved["user_masked"] == "tim***nm"
        assert len(saved["accounts"]) == 3
        for idx, item in enumerate(saved["accounts"], start=1):
            assert item["sid"] == f"foldeskacc{idx}"
            assert item["country"] == "us"
            assert "secret_password_99" not in item["proxy_masked"]
            assert "***@gate.nodemaven.com:1080" in item["proxy_masked"]

        # Verify TelethonGateway._resolve_proxy returns the full unmasked URL for each account
        p1 = gw._resolve_proxy("data/sessions/1.session", conn=conn)
        p2 = gw._resolve_proxy("data/sessions/2.session", conn=conn)
        assert p1 != p2
        assert "sid-foldeskacc1" in p1
        assert "sid-foldeskacc2" in p2
        assert mask_proxy_url(p1).endswith("***@gate.nodemaven.com:1080")


def test_telethon_open_falls_back_when_primary_nodemaven_peer_times_out(monkeypatch, tmp_path):
    import asyncio
    import telethon

    gw = TelethonGateway()
    sess_path = str(tmp_path / "25.session")
    sticky_ru_url = "socks5://nmuser-country-ru-sid-foldeskacc25-ttl-24h-filter-medium:secret@gate.nodemaven.com:1080"
    gw.set_proxy(sess_path, sticky_ru_url)

    monkeypatch.setenv("NODEMAVEN_USER", "nmuser")
    monkeypatch.setenv("NODEMAVEN_PASS", "secret")
    monkeypatch.setenv("TG_API_ID", "2040")
    monkeypatch.setenv("TG_API_HASH", "b18441a1ff607e10a989891a5462e627")

    attempts = []

    class DummyTelegramClient:
        def __init__(self, session_name, api_id, api_hash, **kwargs):
            self.proxy = kwargs.get("proxy")
            self.connected = False

        async def connect(self):
            proxy_user = self.proxy[4] if self.proxy else ""
            attempts.append(proxy_user)
            if len(attempts) == 1:
                raise ConnectionError("Connection to Telegram failed 2 time(s)")
            self.connected = True

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            self.connected = False

    monkeypatch.setattr(telethon, "TelegramClient", DummyTelegramClient)

    client, _ = asyncio.run(gw._open(sess_path))
    assert client.is_connected() is True
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert "country-ru-sid-foldeskacc25-" in attempts[0]
    assert gw._proxies[sess_path] == sticky_ru_url


