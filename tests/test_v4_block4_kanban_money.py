import os
import socket
import subprocess
import sys
import time

from fastapi.testclient import TestClient

from app.db import ROOT, connect, init_db
from app.main import create_app


def _make_client(tmp_path):
    conn = connect(tmp_path / "crm_block4.db")
    init_db(conn)
    app = create_app(conn=conn)
    return TestClient(app), conn


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_lead_amount_create_and_patch(tmp_path):
    client, _ = _make_client(tmp_path)

    # 1. Create lead with explicit integer amount
    r1 = client.post(
        "/api/leads",
        json={
            "name": "Иван Инвест",
            "username": "@ivan_invest",
            "status": "new",
            "amount": 2500,
            "currency": "USD",
        },
    )
    assert r1.status_code == 200, r1.text
    lead1 = r1.json()
    assert lead1["amount"] == 2500
    assert lead1["currency"] == "USD"

    # 2. Create lead with formatted string amount "$12,000" and default currency
    r2 = client.post(
        "/api/leads",
        json={
            "name": "Ольга Тех",
            "username": "@olga_tech",
            "status": "talking",
            "amount": "$12,000",
        },
    )
    assert r2.status_code == 200, r2.text
    lead2 = r2.json()
    assert lead2["amount"] == 12000
    assert lead2["currency"] == "USD"

    # 3. Patch amount and status (Kanban drag-and-drop simulation)
    r_patch = client.patch(
        f"/api/leads/{lead1['id']}",
        json={"status": "talking", "amount": 4800, "currency": "EUR"},
    )
    assert r_patch.status_code == 200, r_patch.text
    patched = r_patch.json()
    assert patched["status"] == "talking"
    assert patched["amount"] == 4800
    assert patched["currency"] == "EUR"

    # 4. Verify in GET /api/leads and GET /api/leads/{id}/card
    r_list = client.get("/api/leads", params={"status": "talking"})
    assert r_list.status_code == 200
    talking_leads = r_list.json()
    assert len(talking_leads) == 2
    total_talking = sum(item["amount"] for item in talking_leads)
    assert total_talking == 4800 + 12000

    r_card = client.get(f"/api/leads/{lead1['id']}/card")
    assert r_card.status_code == 200
    card = r_card.json()
    assert card["amount"] == 4800
    assert card["currency"] == "EUR"


def test_lead_amount_edge_cases(tmp_path):
    client, _ = _make_client(tmp_path)

    # Default amount is 0 when omitted or empty string
    r_default = client.post(
        "/api/leads",
        json={"name": "Без Бюджета", "username": "@zero_budget", "amount": ""},
    )
    assert r_default.status_code == 200
    lead = r_default.json()
    assert lead["amount"] == 0
    assert lead["currency"] == "USD"

    # Negative amount is rejected with 400
    r_neg = client.post(
        "/api/leads",
        json={"name": "Минус", "username": "@neg_budget", "amount": -100},
    )
    assert r_neg.status_code == 400

    r_patch_neg = client.patch(
        f"/api/leads/{lead['id']}",
        json={"amount": -50},
    )
    assert r_patch_neg.status_code == 400

    # Non-numeric string is rejected with 400
    r_bad = client.patch(
        f"/api/leads/{lead['id']}",
        json={"amount": "not-a-number"},
    )
    assert r_bad.status_code == 400


def test_pipeline_stages_list_rename_and_create(tmp_path):
    client, conn = _make_client(tmp_path)

    # 1. Default 5 stages
    r_stages = client.get("/api/pipeline/stages")
    assert r_stages.status_code == 200
    stages = r_stages.json()
    assert [s["key"] for s in stages] == ["new", "talking", "waiting", "won", "lost"]
    assert stages[1]["label"] == "Разговор"

    # 2. Rename stage via PATCH /api/pipeline/stages/{key}
    r_rename = client.patch(
        "/api/pipeline/stages/talking",
        json={"label": "Переговоры", "color": "#2563eb"},
    )
    assert r_rename.status_code == 200, r_rename.text
    renamed = r_rename.json()
    assert renamed["key"] == "talking"
    assert renamed["label"] == "Переговоры"
    assert renamed["color"] == "#2563eb"

    # Re-running init_db must preserve custom stage label
    init_db(conn)
    stages_after_init = client.get("/api/pipeline/stages").json()
    talking_stage = next(s for s in stages_after_init if s["key"] == "talking")
    assert talking_stage["label"] == "Переговоры"

    # 3. Create custom stage via POST /api/pipeline/stages
    r_create = client.post(
        "/api/pipeline/stages",
        json={"key": "contract_review", "label": "Согласование договора", "position": 3},
    )
    assert r_create.status_code == 200, r_create.text
    custom_stage = r_create.json()
    assert custom_stage["key"] == "contract_review"
    assert custom_stage["label"] == "Согласование договора"

    # Auto-generated key when key is omitted for Cyrillic label
    r_auto = client.post(
        "/api/pipeline/stages",
        json={"label": "Демо показ"},
    )
    assert r_auto.status_code == 200, r_auto.text
    auto_stage = r_auto.json()
    assert auto_stage["key"]
    assert auto_stage["label"] == "Демо показ"

    # 4. Move lead into custom stage and filter by custom stage
    lead = client.post(
        "/api/leads",
        json={"name": "Клиент Договор", "username": "@contract_lead", "amount": 7500},
    ).json()
    r_move = client.patch(
        f"/api/leads/{lead['id']}",
        json={"status": "contract_review"},
    )
    assert r_move.status_code == 200, r_move.text
    assert r_move.json()["status"] == "contract_review"

    r_filter = client.get("/api/leads", params={"status": "contract_review"})
    assert r_filter.status_code == 200
    assert len(r_filter.json()) == 1
    assert r_filter.json()[0]["id"] == lead["id"]

    # 5. Saved view supports custom stage
    r_view = client.post(
        "/api/views",
        json={"name": "На договоре", "status": "contract_review"},
    )
    assert r_view.status_code == 200, r_view.text
    assert r_view.json()["status"] == "contract_review"

    # 6. Error cases on pipeline stages
    assert client.patch("/api/pipeline/stages/nonexistent", json={"label": "X"}).status_code == 404
    assert client.patch("/api/pipeline/stages/new", json={"label": "   "}).status_code == 400
    assert client.post("/api/pipeline/stages", json={"label": "   "}).status_code == 400
    assert client.post("/api/pipeline/stages", json={"key": "new", "label": "Дубль"}).status_code == 400


def test_demo_seed_populates_lead_amounts(tmp_path):
    client, _ = _make_client(tmp_path)
    r_seed = client.post("/api/demo/seed")
    assert r_seed.status_code == 200, r_seed.text

    leads = client.get("/api/leads").json()
    assert len(leads) >= 8
    total_amount = sum(item.get("amount", 0) for item in leads)
    assert total_amount > 10000
    assert all(isinstance(item.get("amount"), int) and item["amount"] > 0 for item in leads)


def test_kanban_drag_drop_sums_and_stages_playwright_e2e(tmp_path):
    port = _free_port()
    db_path = tmp_path / "e2e_block4.db"
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["E2E_DB_PATH"] = str(db_path)

    srv = subprocess.Popen(
        [sys.executable, str(ROOT / "e2e" / "serve.py")],
        cwd=str(ROOT / "e2e"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        import urllib.request

        ready = False
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/accounts", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.15)
        assert ready, "E2E server did not start in time"

        js_script = f"""
const {{ chromium }} = require("playwright");

(async () => {{
  const baseURL = "http://127.0.0.1:{port}";
  const browser = await chromium.launch({{
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  }});
  const context = await browser.newContext({{ viewport: {{ width: 1440, height: 900 }} }});
  const page = await context.newPage();

  await page.request.post(`${{baseURL}}/api/test/reset`);
  await page.goto(`${{baseURL}}/`);

  // 1. Seed demo data and open Kanban board (#s07)
  await page.locator("#seed-demo").click();
  await page.locator('a[data-sheet-link="s07"]').first().click();
  await page.locator("#board .col[data-stage='new'] .lead-card").first().waitFor();

  const newCol = page.locator("#board .col[data-stage='new']");
  const talkingCol = page.locator("#board .col[data-stage='talking']");

  const initNewSum = Number(await newCol.locator(".col-sum").getAttribute("data-col-sum"));
  const initTalkingSum = Number(await talkingCol.locator(".col-sum").getAttribute("data-col-sum"));
  const initNewCount = Number(await newCol.locator(".col-count").getAttribute("data-col-count"));
  const initTalkingCount = Number(await talkingCol.locator(".col-count").getAttribute("data-col-count"));

  if (initNewSum <= 0 || initTalkingSum <= 0) {{
    throw new Error(`Expected positive initial column sums, got ${{initNewSum}}, ${{initTalkingSum}}`);
  }}

  const firstCard = newCol.locator(".lead-card").first();
  const cardLeadId = await firstCard.getAttribute("data-lead-id");
  const cardAmount = Number(await firstCard.getAttribute("data-amount"));
  const cardAmountText = (await firstCard.locator(".card-amount").textContent()).trim();
  if (!cardAmountText.startsWith("$") || cardAmount <= 0) {{
    throw new Error(`Unexpected card amount badge: ${{cardAmountText}} (${{cardAmount}})`);
  }}

  // 2. HTML5 Drag-and-Drop card from 'new' to 'talking'
  await firstCard.dragTo(talkingCol);
  await page.waitForTimeout(300);

  const afterDragNewCount = Number(await newCol.locator(".col-count").getAttribute("data-col-count"));
  const afterDragNewSum = Number(await newCol.locator(".col-sum").getAttribute("data-col-sum"));
  const afterDragTalkingSum = Number(await talkingCol.locator(".col-sum").getAttribute("data-col-sum"));

  if (afterDragNewCount !== initNewCount - 1) {{
    throw new Error(`Expected new count ${{initNewCount - 1}}, got ${{afterDragNewCount}}`);
  }}
  if (afterDragNewSum !== initNewSum - cardAmount) {{
    throw new Error(`Expected new sum ${{initNewSum - cardAmount}}, got ${{afterDragNewSum}}`);
  }}

  // Verify backend persisted status = 'talking'
  const leadAfterDrag = await (await page.request.get(`${{baseURL}}/api/leads/${{cardLeadId}}`)).json();
  if (leadAfterDrag.status !== "talking") {{
    throw new Error(`Expected persisted status 'talking', got ${{leadAfterDrag.status}}`);
  }}

  // 3. Click card to open #s09, edit amount input, verify immediate sum update
  const movedCard = talkingCol.locator(`.lead-card[data-lead-id='${{cardLeadId}}']`);
  await movedCard.click();
  const amountInput = page.locator("#lead-card input.lead-amount-input");
  await amountInput.waitFor();
  const delta = 5800;
  const targetAmount = cardAmount + delta;
  await amountInput.fill(String(targetAmount));
  await amountInput.dispatchEvent("change");
  await page.waitForTimeout(300);

  const updatedTalkingSum = Number(await talkingCol.locator(".col-sum").getAttribute("data-col-sum"));
  if (updatedTalkingSum !== afterDragTalkingSum + delta) {{
    throw new Error(`Expected talking sum ${{afterDragTalkingSum + delta}}, got ${{updatedTalkingSum}}`);
  }}

  // 4. Create custom stage via #add-stage-btn and verify #board column + #s09 status select update
  page.once("dialog", (dlg) => dlg.accept("Договор"));
  await page.locator("#add-stage-btn").click();
  await page.waitForTimeout(300);

  const colCount = await page.locator("#board .col").count();
  if (colCount !== 6) {{
    throw new Error(`Expected 6 columns after adding stage, got ${{colCount}}`);
  }}
  const customDotColor = await page.locator("#board .col").nth(5).locator(".stage-dot").getAttribute("data-stage-color");
  if (!customDotColor) {{
    throw new Error("Expected custom stage dot to have data-stage-color");
  }}

  const cardStatusOptions = await page.locator("#lead-card select[aria-label='Статус'] option").allTextContents();
  if (!cardStatusOptions.includes("Договор")) {{
    throw new Error(`Expected #lead-card status select to include 'Договор', got ${{JSON.stringify(cardStatusOptions)}}`);
  }}

  // 5. Rename stage via double-click on column heading
  page.once("dialog", (dlg) => dlg.accept("Подписание"));
  await page.locator("#board .col").nth(5).locator("header h3").dblclick();
  await page.waitForTimeout(300);

  const renamedHeading = (await page.locator("#board .col").nth(5).locator("header h3").textContent()).trim();
  if (renamedHeading !== "Подписание") {{
    throw new Error(`Expected renamed heading 'Подписание', got ${{renamedHeading}}`);
  }}
  const cardStatusOptionsAfterRename = await page.locator("#lead-card select[aria-label='Статус'] option").allTextContents();
  if (!cardStatusOptionsAfterRename.includes("Подписание")) {{
    throw new Error(`Expected #lead-card status select to include 'Подписание', got ${{JSON.stringify(cardStatusOptionsAfterRename)}}`);
  }}

  await browser.close();
}})();
"""
        proc = subprocess.run(
            ["node", "-e", js_script],
            cwd=str(ROOT / "e2e"),
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert proc.returncode == 0, f"Playwright E2E failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

