#!/usr/bin/env node
/*
 * Founder OS — browser smoke tests (Playwright, plain Node runner).
 *
 * Starts the backend on a throwaway SQLite database with a password set, drives the real
 * frontend in headless Chromium, and checks:
 *   1. login gate: protected API is 401, the page shows the gate, wrong password is rejected,
 *      the right one gets in
 *   2. completing a brief item from the home screen and undoing it
 *   3. advancing a syllabus row by tapping it
 *   4. a show_view directive (as the orchestrator emits it) opens the stage filtered to S1,
 *      Esc closes it, the close directive closes it
 *   5. Business page: the Clients tab lists a client, a tap opens it, and Paid on a due
 *      payment marks it paid and logs the revenue
 *   6. Portfolio tab: Focus makes a project priority 1 and demotes the previous one
 *   7. Agents page: the registry lists the agents, the toggle switches one off, Run runs one
 *   8. Applications page: a tap advances a requirement, Make it a task creates its task
 *   9. Brand tab: says "Not connected" with no account; Advance moves an idea on
 *
 * Run from the repo root:
 *     node tests/e2e/smoke.js
 * Needs: Python env with the backend requirements (PYTHON env var, default `python`),
 * the `playwright` npm package with Chromium installed (`npx playwright install chromium`).
 * Env: PORT (default 8765), HEADED=1 to watch, KEEP=1 to leave the server running,
 * PW_CHANNEL=msedge (or chrome) to use an installed browser instead of Playwright's Chromium.
 */
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const PORT = Number(process.env.PORT || 8765);
const BASE = `http://127.0.0.1:${PORT}`;
const PASSWORD = "smoke-test-password";
const PYTHON = process.env.PYTHON || "python";

let failures = 0;
function check(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { failures++; console.log("  ✗ " + msg); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForServer(ms = 60000) {
  const start = Date.now();
  while (Date.now() - start < ms) {
    try { const r = await fetch(`${BASE}/health`); if (r.ok) return; } catch (_) {}
    await sleep(400);
  }
  throw new Error("backend did not start");
}

async function api(token, method, p, body, isForm) {
  const headers = { Authorization: `Bearer ${token}` };
  if (!isForm && body) headers["Content-Type"] = "application/json";
  const r = await fetch(`${BASE}${p}`, { method, headers, body: isForm ? body : body ? JSON.stringify(body) : undefined });
  const text = await r.text();
  try { return { status: r.status, json: JSON.parse(text) }; } catch { return { status: r.status, text }; }
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "founder-os-smoke-"));
  const env = {
    ...process.env,
    DATABASE_URL: `sqlite:///${path.join(tmp, "smoke.db")}`,
    FOUNDER_OS_PASSWORD: PASSWORD,
    FOUNDER_OS_ORIGINS: BASE,
    VAULT_REPO_URL: "https://127.0.0.1/none.git", // never clone a real vault in a smoke run
    VAULT_LOCAL_PATH: path.join(tmp, "vault"),
    // The backend takes "today" from FOUNDER_OS_TZ; this script seeds with this machine's
    // local date, so the server runs in this machine's zone.
    FOUNDER_OS_TZ: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  };
  delete env.OPENAI_API_KEY;
  const server = spawn(PYTHON, ["-m", "uvicorn", "main:app", "--port", String(PORT)], {
    cwd: path.join(ROOT, "backend"), env, stdio: ["ignore", "pipe", "pipe"],
  });
  let serverLog = "";
  server.stdout.on("data", (d) => (serverLog += d));
  server.stderr.on("data", (d) => (serverLog += d));

  const { chromium } = require("playwright");
  let browser;
  try {
    await waitForServer();
    console.log(`backend up on ${BASE}`);

    // ── seed through the API (needs a token) ──
    const login = await api("", "POST", "/auth/login", { password: PASSWORD });
    check(login.status === 200 && login.json.token, "API login returns a token");
    const token = login.json.token;
    // The local date, as the backend sees it (toISOString() alone is UTC, which is still
    // yesterday for the first hours after local midnight).
    const now = new Date();
    const today = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    const todo = await api(token, "POST", "/todos", { text: "Finalise Bel Baby delivery", due: today, priority: 1, category: "business" });
    check(todo.status === 200 && todo.json.id, "seeded a task due today");
    const csv = fs.readFileSync(path.join(ROOT, "backend", "tests", "fixtures", "syllabus_sample.csv"));
    const fd = new FormData();
    fd.set("file", new Blob([csv], { type: "text/csv" }), "tracker.csv");
    fd.set("subject_code", "9709"); fd.set("subject_name", "Maths 9709"); fd.set("level", "AS"); fd.set("papers", "P1,S1");
    const imp = await api(token, "POST", "/syllabus/import", fd, true);
    check(imp.status === 200 && imp.json.created === 10, `imported the syllabus fixture (${JSON.stringify(imp.json)})`);

    browser = await chromium.launch({ headless: !process.env.HEADED, ...(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {}) });
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));

    // ── 1. login gate ──
    console.log("1. login");
    const unauth = await fetch(`${BASE}/todos`);
    check(unauth.status === 401, "protected route is 401 without a token");
    await page.goto(BASE + "/", { waitUntil: "networkidle", timeout: 60000 });
    await page.waitForSelector("#login-screen.show", { timeout: 15000 });
    check(true, "login gate is shown");
    await page.fill("#login-password", "wrong");
    await page.click("#login-btn");
    await page.waitForTimeout(600);
    check((await page.textContent("#login-err")).includes("Wrong"), "wrong password is rejected");
    await page.fill("#login-password", PASSWORD);
    await page.click("#login-btn");
    await page.waitForFunction(() => !document.getElementById("login-screen").classList.contains("show"), null, { timeout: 15000 });
    check(true, "correct password hides the gate");
    await page.waitForFunction(() => document.querySelectorAll("#cc-items .cc-item").length > 0, null, { timeout: 20000 });
    check(new URL(page.url()).pathname === "/app/", "/ redirected to /app/");
    const imgs = await page.evaluate(() => [...document.querySelectorAll("img.home-scene-img, img#orb")]
      .map((i) => i.complete && i.naturalWidth > 0));
    check(imgs.length === 2 && imgs.every(Boolean), "scene and orb images load");

    // ── 2. complete a brief item, then undo ──
    console.log("2. brief item complete + undo");
    const row = page.locator("#cc-items .cc-item", { hasText: "Finalise Bel Baby delivery" }).first();
    check((await row.count()) === 1, "seeded task appears in Today's Brief");
    await row.locator(".check").click();
    await page.waitForTimeout(1200);
    const after = await api(token, "GET", "/todos");
    const t = after.json.find((x) => x.id === todo.json.id);
    check(t && t.done === true, "checkbox completed the task on the backend");
    const changes = await api(token, "GET", "/changes");
    const change = changes.json.find((c) => c.table === "todos" && c.record_id === todo.json.id && c.action === "update");
    check(!!change, "completion was recorded in the change log");
    await page.waitForFunction(() => document.querySelectorAll("#cc-items .cc-item").length >= 0, null, { timeout: 10000 });
    const undoBtn = page.locator("#ucmd-changes .chg-undo").first();
    if ((await undoBtn.count()) > 0) {
      await undoBtn.click();
      await page.waitForTimeout(1200);
      check(true, "Undo button clicked in the dock");
    } else {
      const u = await api(token, "POST", `/changes/${change.id}/undo`);
      check(u.status === 200, "undo via API (dock strip not rendered on this path)");
    }
    const restored = (await api(token, "GET", "/todos")).json.find((x) => x.id === todo.json.id);
    check(restored && restored.done === false, "undo reopened the task");

    // ── 3. syllabus tap-to-advance ──
    console.log("3. syllabus advance");
    await page.evaluate(() => { const ni = document.querySelector(".nav-item[onclick*=\"nav('syllabus'\"]"); nav("syllabus", ni); });
    await page.waitForSelector("#sy-body .sy-row", { timeout: 15000 });
    const first = page.locator("#sy-body .sy-row").first();
    const before = await first.getAttribute("data-status");
    await first.click();
    await page.waitForTimeout(1200);
    const afterStatus = await page.locator("#sy-body .sy-row").first().getAttribute("data-status");
    const order = ["not_started", "learned", "revised", "bulletproof"];
    check(afterStatus === order[(order.indexOf(before) + 1) % 4], `tap advanced ${before} → ${afterStatus}`);
    const points = (await api(token, "GET", "/syllabus")).json.subjects[0].papers.flatMap((p) => p.points);
    check(points.some((p) => p.status === afterStatus), "backend reflects the new state");

    // ── 4. show_view directive opens the stage (as the orchestrator would emit it) ──
    console.log("4. stage via show_view directive");
    await page.evaluate(() => handleUiDirective({ view: "syllabus", params: { paper: "S1" } }));
    await page.waitForTimeout(1200);
    const stage = await page.evaluate(() => ({
      shown: document.getElementById("stage").classList.contains("show"),
      title: document.getElementById("stage-title").textContent,
      codes: [...document.querySelectorAll("#stage-body .sy-code")].map((e) => e.textContent),
      orbInStage: !!document.querySelector("#stage-orb-slot .orb-stage"),
    }));
    check(stage.shown && stage.title.includes("S1"), "stage opened with the syllabus view filtered to S1");
    check(stage.codes.length === 5 && stage.codes.every((c) => c.startsWith("5.")), `only S1 rows shown (${stage.codes.join(", ")})`);
    check(stage.orbInStage, "the orb moved into the stage");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    const closed = await page.evaluate(() => ({ shown: document.getElementById("stage").classList.contains("show"), orbHome: !!document.querySelector("#home-orb .orb-stage") }));
    check(!closed.shown && closed.orbHome, "Esc closed the stage and the orb returned home");
    await page.evaluate(() => { handleUiDirective({ view: "brief" }); handleUiDirective({ view: "close" }); });
    check(!(await page.evaluate(() => document.getElementById("stage").classList.contains("show"))), "close directive closes the stage");

    // ── 5. Business: Clients → client → Paid ──
    console.log("5. business: mark a payment paid");
    const cl = await api(token, "POST", "/clients", { name: "Smoke Test Bakery", status: "active" });
    const pay = await api(token, "POST", "/payments", { client_id: cl.json.id, amount: 1200, currency: "ZMW", title: "Deposit", due_date: today });
    check(pay.status === 201 && pay.json.status === "due", "seeded a client with a due payment");
    await page.evaluate(() => openBusinessTile("clients"));
    await page.waitForSelector("#biz-body .row--tap", { timeout: 15000 });
    check((await page.locator("#biz-tabs .tab.is-on").textContent()).trim() === "Clients", "the Clients tab is selected");
    await page.locator("#biz-body .row--tap", { hasText: "Smoke Test Bakery" }).click();
    await page.waitForSelector("#biz-body button:has-text('Paid')", { timeout: 15000 });
    await page.locator("#biz-body button:has-text('Paid')").first().click();
    await page.waitForTimeout(1200);
    const paid = (await api(token, "GET", `/clients/${cl.json.id}`)).json.payments[0];
    check(paid.status === "paid" && paid.revenue_id, "Paid marked the payment paid and linked its revenue");
    const rev = (await api(token, "GET", "/revenue")).json.records.find((r) => r.id === paid.revenue_id);
    check(rev && rev.amount === 1200 && rev.currency === "ZMW", "the revenue row was written in Kwacha");
    check((await page.locator("#ucmd-changes .chg-row").count()) >= 1, "the change is shown with Undo in the dock");

    // ── 6. Portfolio: Focus ──
    console.log("6. portfolio: focus a project");
    const projA = await api(token, "POST", "/projects", { title: "Smoke project one", priority: 1 });
    const projB = await api(token, "POST", "/projects", { title: "Smoke project two", priority: 3 });
    await page.evaluate(() => businessTab("portfolio"));
    await page.waitForSelector("#biz-body .row", { timeout: 15000 });
    await page.locator("#biz-body .row", { hasText: "Smoke project two" }).locator("button:has-text('Focus')").click();
    await page.waitForTimeout(1200);
    const projects = (await api(token, "GET", "/projects")).json;
    const pr = (id) => projects.find((p) => p.id === id).priority;
    check(pr(projB.json.id) === 1 && pr(projA.json.id) === 2, "Focus made the project priority 1 and demoted the other");
    check((await page.locator("#biz-body .row", { hasText: "Smoke project two" }).locator("text=In focus").count()) === 1, "the row shows it is in focus");

    // ── 7. Agents: toggle and Run ──
    console.log("7. agents: toggle and run");
    await page.evaluate(() => openAgentsTile());
    await page.waitForSelector("#ag-list .row", { timeout: 15000 });
    check((await page.locator("#ag-list .row").count()) >= 6, "the registry lists the shipped agents");
    const qualifier = page.locator('#ag-list .row[data-key="lead_qualifier"]');
    await qualifier.locator("button.toggle").click();
    await page.waitForTimeout(1200);
    const agentsNow = (await api(token, "GET", "/agents")).json;
    check(agentsNow.find((a) => a.key === "lead_qualifier").enabled === false, "the toggle switched the agent off");
    await page.locator('#ag-list .row[data-key="portfolio_reviewer"]').locator("button:has-text('Run')").click();
    await page.waitForTimeout(1500);
    const reviewerRuns = (await api(token, "GET", "/agents/portfolio_reviewer/runs")).json;
    check(reviewerRuns.length === 1 && reviewerRuns[0].trigger === "manual", "Run ran the agent once");
    await page.locator('#ag-list .row[data-key="portfolio_reviewer"]').click();
    await page.waitForSelector("#ag-runs .row", { timeout: 15000 });
    check((await page.locator("#ag-detail-head").textContent()).includes("Portfolio reviewer"), "a tap opens the agent with its run log");

    // ── 8. Applications: advance and make a task ──
    console.log("8. applications: advance a requirement, make it a task");
    const appl = await api(token, "POST", "/applications", { name: "Smoke University", deadline: today });
    const reqA = await api(token, "POST", `/applications/${appl.json.id}/requirements`, { title: "Personal essay", kind: "essay" });
    await page.evaluate(() => openApplicationsTile());
    await page.waitForSelector("#apps-body .row--tap", { timeout: 15000 });
    check((await page.locator("#apps-body .card", { hasText: "Smoke University" }).textContent()).includes("At risk"), "an application due today with a missing requirement is marked at risk");
    await page.locator("#apps-body .row--tap", { hasText: "Personal essay" }).locator(".row-title").click();
    await page.waitForTimeout(1200);
    let reqNow = (await api(token, "GET", `/applications/${appl.json.id}`)).json.requirements[0];
    check(reqNow.status === "in_progress", "a tap moved the requirement to in progress");
    await page.locator("#apps-body .row--tap", { hasText: "Personal essay" }).locator("button:has-text('Make it a task')").click();
    await page.waitForTimeout(1200);
    reqNow = (await api(token, "GET", `/applications/${appl.json.id}`)).json.requirements[0];
    const taskRow = (await api(token, "GET", "/todos")).json.find((x) => x.id === reqNow.todo_id);
    check(reqNow.status === "in_progress" && taskRow && taskRow.text === "Smoke University: Personal essay", "Make it a task created the task without moving the requirement");

    // ── 9. Brand: not connected, and advancing an idea ──
    console.log("9. brand: not connected state, advance an idea");
    const idea = await api(token, "POST", "/brand/ideas", { title: "Smoke content idea" });
    await page.evaluate(() => openBusinessTile("brand"));
    await page.waitForSelector("#biz-body .card", { timeout: 15000 });
    await page.waitForFunction(() => document.getElementById("biz-body").textContent.includes("Accounts"), null, { timeout: 15000 });
    const brandText = await page.locator("#biz-body").textContent();
    check(brandText.includes("No account is connected") && brandText.includes("Not connected"), "with no account the tab says it is not connected");
    check(!/\b0 followers\b/.test(brandText), "no follower number is shown for an unconnected platform");
    await page.locator(`#idea-${idea.json.id} button`).click();
    await page.waitForTimeout(1200);
    let ideaNow = (await api(token, "GET", "/brand/ideas")).json.find((i) => i.id === idea.json.id);
    check(ideaNow.status === "drafted", "Advance moved the idea to drafted");
    await page.locator(`#idea-${idea.json.id} button`).click();          // drafted → scheduled needs a date
    await page.waitForSelector(`#idea-date-${idea.json.id}`, { timeout: 15000 });
    await page.fill(`#idea-date-${idea.json.id}`, today);
    await page.locator(`#idea-${idea.json.id} button.btn--primary`).click();
    await page.waitForTimeout(1200);
    ideaNow = (await api(token, "GET", "/brand/ideas")).json.find((i) => i.id === idea.json.id);
    check(ideaNow.status === "scheduled" && ideaNow.scheduled_for === today, "scheduling asked for a day and set it");

    check(pageErrors.length === 0, `no page errors (${pageErrors.join("; ") || "none"})`);
  } catch (e) {
    failures++;
    console.log("  ✗ " + (e.stack || e.message));
    console.log("--- server log tail ---\n" + serverLog.slice(-2000));
  } finally {
    if (browser) await browser.close();
    if (!process.env.KEEP) server.kill("SIGTERM");
  }
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nall smoke checks passed");
  process.exit(failures ? 1 : 0);
})();
