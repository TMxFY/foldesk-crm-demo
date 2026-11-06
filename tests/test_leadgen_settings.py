import json
from pathlib import Path
import sys
import pytest
from fastapi.testclient import TestClient

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import connect, init_db
from app.main import create_app
from app.outreach import get_campaign
from leadgen.crm_mcp_push import build_lead_payload_from_dict
from leadgen.job_monitor import apply_vacancies_config
from leadgen.job_reply import build_vacancy_mcp_lead, load_bank


@pytest.fixture()
def test_env(tmp_path, monkeypatch):
    cfg_file = tmp_path / "leadgen_config.json"
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    conn = connect(tmp_path / "test_leadgen_settings.db")
    init_db(conn)
    app = create_app(conn=conn)
    client = TestClient(app)
    return {
        "conn": conn,
        "client": client,
        "cfg_file": cfg_file,
        "state_dir": state_dir,
    }


def test_get_and_put_leadgen_config_persists_to_db_and_json(test_env):
    client = test_env["client"]
    cfg_file = test_env["cfg_file"]

    res_get = client.get("/api/leadgen/config")
    assert res_get.status_code == 200
    data = res_get.json()
    assert data["ok"] is True
    cfg = data["config"]
    assert cfg["enabled"] is True
    assert cfg["total_daily_quota"] == 50
    assert cfg["b2b"]["touch_mode"] == "micro_ping"
    assert cfg["vacancies"]["touch_mode"] == "direct"
    assert "gutz156" in cfg["cases_bank_md"]

    payload = {
        "total_daily_quota": 75,
        "b2b": {
            "touch_mode": "direct",
            "target_campaign_tg": "B2B Outbound TG",
            "campaign_daily_limit": 40,
            "sources": {
                "tg_groups": True,
                "meta_ads": False,
                "linkedin_jobs": False,
            },
            "tg_groups_new": ["@solofounderschat", "https://t.me/saas_builders/"],
            "limits": {
                "yc_limit": 80,
                "msg_limit": 1200,
            },
        },
        "vacancies": {
            "touch_mode": "micro_ping",
            "campaign_name": "TG Вакансии AI",
            "daily_limit": 25,
            "remote_only": True,
            "layers": {
                "AI-контент": True,
                "автоматизация": False,
            },
            "extra_chats": ["@ai_jobs_board", "t.me/itconnectbelgrade"],
            "pause_between_chats": 2.0,
        },
    }
    res_put = client.put("/api/leadgen/config", json=payload)
    assert res_put.status_code == 200
    saved = res_put.json()["config"]
    assert saved["total_daily_quota"] == 75
    assert saved["b2b"]["touch_mode"] == "direct"
    assert saved["b2b"]["target_campaign_tg"] == "B2B Outbound TG"
    assert saved["b2b"]["campaign_daily_limit"] == 40
    assert saved["b2b"]["sources"]["meta_ads"] is False
    assert saved["b2b"]["sources"]["linkedin_jobs"] is False
    assert saved["b2b"]["sources"]["tg_groups"] is True
    assert saved["b2b"]["tg_groups_new"] == ["solofounderschat", "saas_builders"]
    assert saved["b2b"]["limits"]["yc_limit"] == 80
    assert saved["b2b"]["limits"]["msg_limit"] == 1200
    assert saved["vacancies"]["touch_mode"] == "micro_ping"
    assert saved["vacancies"]["campaign_name"] == "TG Вакансии AI"
    assert saved["vacancies"]["daily_limit"] == 25
    assert saved["vacancies"]["layers"]["автоматизация"] is False
    assert saved["vacancies"]["extra_chats"] == ["ai_jobs_board", "itconnectbelgrade"]
    assert saved["vacancies"]["pause_between_chats"] == 2.0

    assert cfg_file.exists()
    disk_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert disk_cfg["total_daily_quota"] == 75
    assert disk_cfg["b2b"]["tg_groups_new"] == ["solofounderschat", "saas_builders"]


def test_sync_leadgen_campaigns_and_push_now(test_env):
    client = test_env["client"]
    state_dir = test_env["state_dir"]

    res_sync = client.post("/api/leadgen/sync-campaigns")
    assert res_sync.status_code == 200
    sync_data = res_sync.json()
    assert sync_data["ok"] is True
    assert sync_data["synced_count"] == 3
    names = {c["name"]: c for c in sync_data["campaigns"]}
    assert "Leadgen Auto (TG)" in names
    assert "Leadgen Auto (WA)" in names
    assert "TG Вакансии" in names
    tg_camp = names["Leadgen Auto (TG)"]
    assert tg_camp["leadgen_enabled"] is True
    assert tg_camp["daily_limit"] == 30
    assert tg_camp["leadgen_daily_quota"] == 30
    assert "tg_groups" in tg_camp["leadgen_sources"]
    assert len(tg_camp["steps"]) == 3
    assert tg_camp["steps"][0]["template_text"] == "{first_touch}"

    # Write local ready vacancy monitor & replies to state_dir and test push-now without LLM
    mon_path = state_dir / "job_monitor_2026-11-06.json"
    rep_path = state_dir / "job_replies_2026-11-06.json"
    mon_path.write_text(
        json.dumps(
            [
                {
                    "hash": "vac_hash_1",
                    "role": "AI Video Producer",
                    "why": "Нужен продюсер AI-рилсов",
                    "pay": "$2000",
                    "contact": "@producer_hr",
                    "layer": "AI-контент",
                    "hot": True,
                    "src": "AI Jobs",
                    "url": "https://t.me/ai_jobs/101",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rep_path.write_text(
        json.dumps(
            [
                {
                    "hash": "vac_hash_1",
                    "text": "Добрый день! По AI Video Producer.\n\nКакой объём роликов в месяц?",
                    "followup_1": "Фоллоу-ап 1",
                    "followup_2": "Фоллоу-ап 2",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    res_push = client.post("/api/leadgen/push-now")
    assert res_push.status_code == 200
    push_data = res_push.json()
    assert push_data["ok"] is True
    assert push_data["ingested"] == 1

    status_res = client.get("/api/leadgen/config").json()["status"]
    assert status_res["crm_leadgen_counts"]["vacancies_leads"] == 1
    assert status_res["crm_leadgen_counts"]["total_leadgen_leads"] == 1
    assert status_res["latest_vacancy_report"]["vacancies_count"] == 1


def test_update_campaign_leadgen_fields_and_steps(test_env):
    client = test_env["client"]
    conn = test_env["conn"]

    create_res = client.post(
        "/api/campaigns",
        json={
            "name": "Custom Outreach",
            "daily_limit": 15,
            "leadgen_enabled": False,
            "leadgen_daily_quota": 10,
            "leadgen_sources": ["yc_ph"],
        },
    )
    assert create_res.status_code == 200
    cid = create_res.json()["id"]

    upd_res = client.put(
        f"/api/campaigns/{cid}",
        json={
            "name": "Custom Outreach Updated",
            "daily_limit": 45,
            "leadgen_enabled": True,
            "leadgen_daily_quota": 35,
            "leadgen_sources": ["tg_groups", "channel_graph", "meta_ads"],
            "steps": [
                {"step_order": 1, "delay_hours": 0, "template_text": "{first_touch}"},
                {"step_order": 2, "delay_hours": 36, "template_text": "{followup_1}"},
            ],
        },
    )
    assert upd_res.status_code == 200
    camp = get_campaign(conn, cid)
    assert camp["name"] == "Custom Outreach Updated"
    assert camp["daily_limit"] == 45
    assert camp["leadgen_enabled"] is True
    assert camp["leadgen_daily_quota"] == 35
    assert camp["leadgen_sources"] == ["tg_groups", "channel_graph", "meta_ads"]
    assert len(camp["steps"]) == 2
    assert camp["steps"][1]["delay_hours"] == 36


def test_leadgen_scripts_read_saved_ui_config(test_env):
    client = test_env["client"]

    client.put(
        "/api/leadgen/config",
        json={
            "b2b": {"touch_mode": "direct"},
            "vacancies": {
                "touch_mode": "micro_ping",
                "extra_chats": ["@custom_vac_chat"],
                "pause_between_chats": 0.75,
            },
            "cases_bank_md": "# Кастомный банк кейсов 999M views",
        },
    )

    # 1. crm_mcp_push reads b2b.touch_mode = "direct" when touch_mode is not passed
    lead_dict = {
        "username": "founder_jane",
        "company": "AcmeAI",
        "category": "SaaS",
        "their_angle": "AI copilot for sales",
    }
    built_b2b = build_lead_payload_from_dict(lead_dict)
    assert "183.5M" in built_b2b["step1_text"]

    # 2. job_reply reads vacancies.touch_mode = "micro_ping" and custom cases_bank_md
    vacancy = {
        "role": "AI Reels Creator",
        "why": "Генерация вертикальных роликов",
        "contact": "@hr_reels",
        "layer": "AI-контент",
    }
    draft_obj = {
        "text": "Добрый день! Полный отклик с кейсами.",
        "followup_1": "Фоллоу-ап 1",
        "followup_2": "Фоллоу-ап 2",
    }
    built_vac = build_vacancy_mcp_lead(vacancy, draft_obj)
    assert built_vac is not None
    assert "AI Reels Creator" in built_vac["step1_text"]
    assert built_vac["step2_text"] == "Добрый день! Полный отклик с кейсами."
    assert "999M views" in load_bank()

    # 3. job_monitor apply_vacancies_config reads extra_chats & pause_between_chats
    applied = apply_vacancies_config()
    assert "custom_vac_chat" in applied["extra_chats"]
    assert applied["pause_between_chats"] == 0.75


def test_crm_auth_default_users_login_register_me_and_logout(test_env):
    client = test_env["client"]

    # 1. Check 3 seeded users in /api/auth/users and /api/auth/me
    users_res = client.get("/api/auth/users")
    assert users_res.status_code == 200
    users_list = users_res.json()["users"]
    usernames = {u["username"]: u for u in users_list}
    assert {"tim", "vlad", "operator"}.issubset(usernames.keys())
    assert usernames["tim"]["role"] == "admin"
    assert usernames["vlad"]["role"] == "sales"
    assert usernames["operator"]["role"] == "operator"
    assert "password_hash" not in usernames["tim"]

    # 2. Login under each of the 3 preset accounts
    presets = [
        ("tim", "TimFoldesk2026!", "admin"),
        ("vlad", "VladSales2026!", "sales"),
        ("operator", "Operator2026!", "operator"),
    ]
    for uname, pwd, expected_role in presets:
        res_login = client.post("/api/auth/login", json={"username": uname, "password": pwd})
        assert res_login.status_code == 200
        body = res_login.json()
        assert body["ok"] is True
        assert body["authenticated"] is True
        assert body["user"]["username"] == uname
        assert body["user"]["role"] == expected_role
        assert body["token"]

    # 3. Wrong password returns 401
    bad_res = client.post("/api/auth/login", json={"username": "tim", "password": "wrong-password"})
    assert bad_res.status_code == 401

    # 4. Register new user, verify /api/auth/me, then logout
    reg_res = client.post(
        "/api/auth/register",
        json={
            "username": "anna_leadgen",
            "password": "AnnaPass2026!",
            "display_name": "Анна (Leadgen)",
            "role": "sales",
        },
    )
    assert reg_res.status_code == 200
    reg_data = reg_res.json()
    assert reg_data["authenticated"] is True
    assert reg_data["user"]["username"] == "anna_leadgen"
    assert reg_data["user"]["display_name"] == "Анна (Leadgen)"
    token = reg_data["token"]

    # Duplicate registration returns 409
    dup_res = client.post(
        "/api/auth/register",
        json={"username": "ANNA_LEADGEN", "password": "AnotherPassword123"},
    )
    assert dup_res.status_code == 409

    me_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 200
    me_data = me_res.json()
    assert me_data["authenticated"] is True
    assert me_data["user"]["username"] == "anna_leadgen"

    logout_res = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout_res.status_code == 200
    assert logout_res.json()["authenticated"] is False

    me_after = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_after.status_code == 200
    assert me_after.json()["authenticated"] is False
    assert me_after.json()["user"] is None

