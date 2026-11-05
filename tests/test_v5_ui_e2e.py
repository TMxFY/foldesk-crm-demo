from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from fastapi.testclient import TestClient

from app.db import ROOT, connect, init_db
from app.main import create_app


def _client(tmp_path: Path) -> TestClient:
    conn = connect(tmp_path / "crm_v5_ui.sqlite3")
    init_db(conn)
    app = create_app(conn=conn)
    return TestClient(app)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_v5_index_html_has_calendar_charts_mcp_and_airtable_controls(tmp_path: Path):
    client = _client(tmp_path)
    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    for expected_id in (
        'id="open-mcp-modal-btn"',
        'id="mcp-modal-box"',
        'id="mcp-config-stdio"',
        'id="mcp-config-http"',
        'id="mcp-tools-list"',
        'id="airtable-sync-btn"',
        'id="board-airtable-sync-btn"',
        'id="calendar"',
        'id="calendar-event-form"',
        'id="calendar-grid-container"',
        'id="analytics-period-switch"',
        'id="analytics-charts-grid"',
        'id="analytics-chart-daily-flow"',
        'id="analytics-chart-revenue"',
        'id="analytics-chart-funnel-dropoff"',
        'id="analytics-chart-channels-sources"',
        'id="analytics-chart-heatmap"',
    ):
        assert expected_id in html, f"Missing {expected_id} in index.html"


def test_playwright_v5_calendar_charts_mcp_airtable_and_softugc_ui(tmp_path: Path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "e2e_v5.db"
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["E2E_DB_PATH"] = str(db_path)
    env["PYTEST_CURRENT_TEST"] = "test_v5_ui_e2e"
    env["AIRTABLE_API_KEY"] = ""
    env["AIRTABLE_BASE_ID"] = ""
    env["AIRTABLE_TOKEN"] = ""

    server = subprocess.Popen(
        [sys.executable, str(ROOT / "e2e" / "serve.py")],
        cwd=str(ROOT / "e2e"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = False
        for _ in range(50):
            try:
                with urlopen(f"{base_url}/api/accounts", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.15)
        assert ready, "Test server did not start in time"

        pw_script = f"""
const {{ chromium }} = require('playwright');

(async () => {{
  const browser = await chromium.launch({{
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  }});
  const context = await browser.newContext({{ viewport: {{ width: 1440, height: 900 }} }});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push('pageerror: ' + err.message));
  page.on('console', (msg) => {{
    if (msg.type() === 'error' && !/status of 4\\d\\d/.test(msg.text())) {{
      errors.push('console.error: ' + msg.text());
    }}
  }});

  await page.request.post('{base_url}/api/test/reset');
  await page.request.post('{base_url}/api/demo/seed');
  await page.goto('{base_url}/');
  await page.locator("#board .lead-card").first().waitFor();

  // Verify no stray <img> elements break F03-N2
  const imgCount = await page.getByRole('img').count();
  if (imgCount !== 0) {{
    throw new Error(`Expected 0 visible role=img elements, got ${{imgCount}}`);
  }}

  // 1. Open MCP Modal and verify 13 tools + stdio & http configs
  await page.locator('#open-mcp-modal-btn').click();
  await page.locator('#mcp-modal-box').waitFor({{ state: 'visible' }});
  const toolCount = await page.locator('#mcp-tools-list [data-mcp-tool]').count();
  if (toolCount < 13) {{
    throw new Error(`Expected at least 13 MCP tools, got ${{toolCount}}`);
  }}
  const stdioText = await page.locator('#mcp-config-stdio').textContent();
  const httpText = await page.locator('#mcp-config-http').textContent();
  if (!stdioText.includes('mcpServers') || !httpText.includes('/mcp')) {{
    throw new Error(`Unexpected MCP configs: stdio=${{stdioText}} http=${{httpText}}`);
  }}
  await page.locator('#close-mcp-modal-btn').click();

  // 2. Test Airtable sync button & 7 SoftUGC stages
  await page.locator('#airtable-sync-btn').click();
  await page.locator('#board .col[data-stage="call_scheduled"]').waitFor();
  const stageCols = await page.locator('#board .col').count();
  if (stageCols < 7) {{
    throw new Error(`Expected 7 SoftUGC stages on board after sync, got ${{stageCols}}`);
  }}

  // 3. Open lead card (#lead-card) and edit SoftUGC Deal fields
  const firstCard = page.locator('#board .lead-card').first();
  const leadId = await firstCard.getAttribute('data-lead-id');
  await firstCard.click();
  await page.locator('#lead-card .softugc-deal-box').waitFor({{ state: 'visible' }});

  const companyInput = page.locator('#lead-card input[name="company"]');
  await companyInput.fill('SoftUGC Cloud Corp');
  await companyInput.dispatchEvent('change');

  const nicheInput = page.locator('#lead-card input[name="niche"]');
  await nicheInput.fill('AI Video SaaS');
  await nicheInput.dispatchEvent('change');

  const mrrInput = page.locator('#lead-card input[name="monthly_value"]');
  await mrrInput.fill('4200');
  await mrrInput.dispatchEvent('change');

  const fupInput = page.locator('#lead-card input[name="next_follow_up"]');
  await fupInput.fill('2026-11-18');
  await fupInput.dispatchEvent('change');
  await page.waitForTimeout(250);

  const checkCardRes = await page.request.get(`{base_url}/api/leads/${{leadId}}/card`);
  const checkCard = await checkCardRes.json();
  if (checkCard.company !== 'SoftUGC Cloud Corp' || checkCard.niche !== 'AI Video SaaS' || Number(checkCard.monthly_value) !== 4200) {{
    throw new Error(`SoftUGC fields did not persist: ${{JSON.stringify(checkCard)}}`);
  }}

  // 4. Test Calendar: create event, verify chip in grid, drag-and-drop reschedule, switch views
  await page.locator('a[data-sheet-link="calendar"]').click();
  await page.locator('#calendar').waitFor({{ state: 'visible' }});
  await page.locator('#cal-ev-title').fill('Демо созвон по AI UGC');
  await page.locator('#cal-ev-type').selectOption('call');
  await page.locator('#cal-ev-start').fill('2026-11-15T15:00');
  await page.locator('#cal-ev-end').fill('2026-11-15T15:30');
  await page.locator('#cal-ev-link').fill('https://meet.google.com/ugc-demo');
  await page.locator('#cal-ev-submit-btn').click();

  const createdChip = page.locator('#calendar-grid-container .cal-item-chip').filter({{ hasText: 'Демо созвон по AI UGC' }}).first();
  await createdChip.waitFor({{ state: 'visible' }});
  const chipUid = await createdChip.getAttribute('data-cal-uid');

  // Drag chip to 2026-11-20 cell
  await page.locator('#calendar-grid-container').evaluate((el) => el.scrollIntoView({{ block: 'start' }}));
  const targetCell = page.locator('#calendar-grid-container .cal-day-cell[data-cal-date="2026-11-20"]').first();
  await createdChip.dragTo(targetCell);
  await page.waitForTimeout(300);

  const movedChip = page.locator('#calendar-grid-container .cal-day-cell[data-cal-date="2026-11-20"] .cal-item-chip').filter({{ hasText: 'Демо созвон по AI UGC' }});
  if ((await movedChip.count()) < 1) {{
    throw new Error(`Expected calendar chip ${{chipUid}} to move to 2026-11-20`);
  }}

  // Switch Calendar views: week, day, agenda, month
  for (const viewMode of ['week', 'day', 'agenda', 'month']) {{
    await page.locator(`#cal-view-switch button[data-cal-view="${{viewMode}}"]`).click();
    const attrView = await page.locator('#calendar-grid-container').getAttribute('data-cal-view');
    if (attrView !== viewMode) {{
      throw new Error(`Expected calendar view ${{viewMode}}, got ${{attrView}}`);
    }}
  }}

  // 5. Test 5 SVG Charts in #analytics and period switcher
  await page.locator('a[data-sheet-link="analytics"]').first().click();
  await page.locator('#analytics').waitFor({{ state: 'visible' }});
  for (const chartSel of [
    '#analytics-chart-daily-flow svg',
    '#analytics-chart-revenue svg',
    '#analytics-chart-funnel-dropoff svg',
    '#analytics-chart-channels-sources svg',
    '#analytics-chart-heatmap svg',
  ]) {{
    const cnt = await page.locator(chartSel).count();
    if (cnt < 1) {{
      throw new Error(`Missing SVG chart for ${{chartSel}}`);
    }}
  }}

  for (const period of ['7d', '90d', 'all', '30d']) {{
    const btn = page.locator(`#analytics-period-switch button[data-period="${{period}}"]`);
    await btn.click();
    await page.waitForTimeout(150);
    const pressed = await btn.getAttribute('aria-pressed');
    if (pressed !== 'true') {{
      throw new Error(`Expected period button ${{period}} aria-pressed=true, got ${{pressed}}`);
    }}
  }}

  if (errors.length) {{
    throw new Error('Browser errors: ' + errors.join('; '));
  }}

  await browser.close();
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
        proc = subprocess.run(
            ["node", "-e", pw_script],
            cwd=str(ROOT / "e2e"),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, f"Playwright V5 E2E failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
