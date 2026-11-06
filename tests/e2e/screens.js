#!/usr/bin/env node
/*
 * Founder OS — layout checks and a contact sheet of every page (Playwright, plain Node runner).
 *
 * Starts the backend on a throwaway SQLite database (no password), seeds a little data, then
 * for every page at 1280×720, 1536×864, 1920×1080 and 390×844:
 *   - takes a screenshot (tests/e2e/out/<viewport>/<page>.png)
 *   - checks the computed body font size, header height and rail width are the same on
 *     every page at that viewport
 *   - checks nothing scrolls horizontally (document and the #content scroller)
 *   - checks no interactive element is covered by the talk bar: each one is scrolled to the
 *     middle of the scroller and must sit fully above the bar and be the element hit-tested
 *     at its centre
 * and writes every screenshot into one contact sheet: tests/e2e/out/contact-sheet.jpg.
 *
 * Run from the repo root:   node tests/e2e/screens.js
 * Needs: Python env with the backend requirements (PYTHON env var, default `python`) and the
 * `playwright` npm package with Chromium installed. Env: PORT (default 8766), SHEET=path, PW_CHANNEL=msedge to use an installed browser.
 */
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const OUT = path.join(__dirname, "out");
const PORT = Number(process.env.PORT || 8766);
const BASE = `http://127.0.0.1:${PORT}`;
const PYTHON = process.env.PYTHON || "python";

const VIEWPORTS = [
  { name: "1280x720", width: 1280, height: 720 },
  { name: "1536x864", width: 1536, height: 864 },
  { name: "1920x1080", width: 1920, height: 1080 },
  { name: "390x844", width: 390, height: 844, mobile: true },
];
const PAGES = ["home", "timetable", "health", "syllabus", "agents", "warroom", "annual", "guide", "settings"];
// Extra states worth seeing on the sheet (not part of the per-page consistency checks).
const EXTRAS = ["stage", "menu"];

let failures = 0;
const problems = [];
function check(cond, msg) {
  if (cond) return true;
  failures++;
  problems.push(msg);
  console.log("  ✗ " + msg);
  return false;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(method, p, body) {
  const r = await fetch(BASE + p, {
    method,
    headers: body && !(body instanceof FormData) ? { "Content-Type": "application/json" } : {},
    body: body ? (body instanceof FormData ? body : JSON.stringify(body)) : undefined,
  });
  let json = null;
  try { json = await r.json(); } catch (_) {}
  return { status: r.status, json };
}

async function waitForServer(ms = 60000) {
  const start = Date.now();
  while (Date.now() - start < ms) {
    try { const r = await fetch(BASE + "/ping"); if (r.ok) return; } catch (_) {}
    await sleep(500);
  }
  throw new Error("backend did not start");
}

async function seed() {
  const due = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  await api("POST", "/todos", { text: "Finalise Bel Baby delivery", priority: 1, category: "business", due, source: "manual" });
  await api("POST", "/todos", { text: "Economics P1 prep", priority: 2, category: "academics", due, source: "manual" });
  await api("POST", "/annual-targets", { name: "AI services revenue", target_value: 2000, current_value: 520 });
  await api("POST", "/annual-targets", { name: "Apply to Wharton", display_value: "Essays drafted" });
  await api("POST", "/documents", { title: "Operating doctrine", content: "Character before status. Execute the plan.", source_type: "paste" });
  const csv = fs.readFileSync(path.join(ROOT, "backend", "tests", "fixtures", "syllabus_sample.csv"));
  const fd = new FormData();
  fd.set("file", new Blob([csv], { type: "text/csv" }), "tracker.csv");
  fd.set("subject_code", "9709");
  fd.set("subject_name", "Maths 9709");
  fd.set("level", "AS");
  fd.set("papers", "P1,S1");
  await api("POST", "/syllabus/import", fd);
}

// Runs in the page: shell measurements, overflow and dock coverage for the active page.
function measure() {
  const r = (el) => el.getBoundingClientRect();
  const content = document.getElementById("content");
  const header = document.getElementById("header");
  const nav = document.getElementById("nav");
  const navVisible = r(nav).right > 1;
  const res = {
    bodyFont: getComputedStyle(document.body).fontSize,
    headerH: Math.round(r(header).height * 100) / 100,
    railW: navVisible ? Math.round(r(nav).width * 100) / 100 : 0,
    docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    contentOverflow: content.scrollWidth - content.clientWidth,
    covered: [],
  };
  const bar = document.querySelector("#cmd-dock .cmd-dock-bar");
  const dockFixed = getComputedStyle(document.getElementById("cmd-dock")).position === "fixed";
  if (!bar || !dockFixed) return res;          // docked in the home grid: it is content, not an overlay
  const dockTop = r(bar).top;
  const page = document.querySelector(".page.active");
  const sel = "button, a[href], input, select, textarea, [role='button'], [role='checkbox'], [tabindex='0']";
  const els = [...page.querySelectorAll(sel)].filter((el) => {
    if (el.closest("#cmd-dock")) return false;
    const b = r(el);
    return b.width > 0 && b.height > 0 && getComputedStyle(el).visibility !== "hidden";
  });
  res.tested = els.length;
  for (const el of els) {
    el.scrollIntoView({ block: "center", inline: "nearest" });
    const b = r(el);
    const cx = Math.min(Math.max(b.left + b.width / 2, 0), innerWidth - 1);
    const cy = b.top + b.height / 2;
    const hit = cy >= 0 && cy < innerHeight ? document.elementFromPoint(cx, cy) : null;
    const underDock = b.bottom > dockTop + 0.5 || (hit && hit.closest("#cmd-dock"));
    if (underDock) {
      const label = (el.getAttribute("aria-label") || el.textContent || el.placeholder || el.tagName).trim().replace(/\s+/g, " ").slice(0, 40);
      res.covered.push(`${el.tagName.toLowerCase()} "${label}" bottom ${Math.round(b.bottom)} > dock top ${Math.round(dockTop)}`);
    }
  }
  content.scrollTop = 0;
  return res;
}

async function show(page, name) {
  await page.evaluate(async (name) => {
    if (typeof closeStage === "function" && STAGE.open) closeStage();
    closeSidebar();
    const el = document.querySelector(`.nav-item[onclick*="nav('${name}'"]`);
    nav(name, el);
  }, name);
  await page.waitForTimeout(name === "home" ? 1200 : 900);
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fos-screens-"));
  const env = { ...process.env, DATABASE_URL: `sqlite:///${path.join(tmp, "screens.db")}`, VAULT_REPO_URL: "https://127.0.0.1/none.git" };
  delete env.FOUNDER_OS_PASSWORD;
  delete env.OPENAI_API_KEY;
  const server = spawn(PYTHON, ["-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", String(PORT)], { cwd: path.join(ROOT, "backend"), env });
  let serverLog = "";
  server.stdout.on("data", (d) => (serverLog += d));
  server.stderr.on("data", (d) => (serverLog += d));

  const { chromium } = require("playwright");
  let browser;
  const shots = [];          // {vp, page, file}
  const table = {};          // vp -> page -> measurements
  try {
    await waitForServer();
    await seed();
    browser = await chromium.launch(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {});
    for (const vp of VIEWPORTS) {
      console.log(`${vp.name}`);
      const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, isMobile: !!vp.mobile, hasTouch: !!vp.mobile, deviceScaleFactor: 1 });
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await page.goto(BASE + "/app/");
      await page.waitForFunction(() => document.querySelectorAll("#cc-items .cc-item").length > 0, null, { timeout: 30000 });
      await page.evaluate(() => document.fonts && document.fonts.ready);
      fs.mkdirSync(path.join(OUT, vp.name), { recursive: true });
      table[vp.name] = {};
      for (const name of PAGES) {
        await show(page, name);
        const file = path.join(OUT, vp.name, `${name}.png`);
        await page.screenshot({ path: file });
        shots.push({ vp: vp.name, page: name, file });
        const m = await page.evaluate(measure);
        table[vp.name][name] = m;
        check(m.docOverflow <= 0 && m.contentOverflow <= 0, `${vp.name} ${name}: horizontal overflow (document ${m.docOverflow}px, content ${m.contentOverflow}px)`);
        if (vp.name === VIEWPORTS[0].name || vp.mobile) console.log(`  ${name}: ${m.tested ?? "home grid (bar is not an overlay)"} controls checked against the talk bar`);
        check(m.covered.length === 0, `${vp.name} ${name}: covered by the talk bar: ${m.covered.join("; ")}`);
      }
      // consistency across pages at this viewport
      for (const key of ["bodyFont", "headerH", "railW"]) {
        const vals = new Set(PAGES.map((n) => String(table[vp.name][n][key])));
        check(vals.size === 1, `${vp.name}: ${key} differs between pages: ${[...vals].join(", ")}`);
      }
      const t0 = table[vp.name].home;
      console.log(`  body ${t0.bodyFont} · header ${t0.headerH}px · rail ${t0.railW}px`);
      // extra states: a stage view, and the open menu
      await show(page, "home");
      await page.evaluate(() => handleUiDirective({ view: "syllabus", params: {} }));
      await page.waitForTimeout(1200);
      let file = path.join(OUT, vp.name, "stage.png");
      await page.screenshot({ path: file });
      shots.push({ vp: vp.name, page: "stage", file });
      await page.evaluate(() => closeStage());
      await show(page, "syllabus");
      await page.evaluate(() => toggleSidebar());
      await page.waitForTimeout(500);
      file = path.join(OUT, vp.name, "menu.png");
      await page.screenshot({ path: file });
      shots.push({ vp: vp.name, page: "menu", file });
      const menuOk = await page.evaluate(() => document.body.classList.contains("nav-open"));
      await page.evaluate(() => { const b = document.querySelector(".nav-item[onclick*=\"nav('health'\"]"); b.click(); });
      await page.waitForTimeout(400);
      const closed = await page.evaluate(() => !document.body.classList.contains("nav-open") && document.getElementById("p-health").classList.contains("active"));
      check(menuOk && closed, `${vp.name}: the menu opens and closes after navigating`);
      check(errors.length === 0, `${vp.name}: page errors: ${errors.join(" | ")}`);
      await ctx.close();
    }

    // ── contact sheet: one row per page, one column per viewport ──
    const rows = [...PAGES, ...EXTRAS];
    const cell = (s) => `<figure><img src="data:image/png;base64,${fs.readFileSync(s.file).toString("base64")}"><figcaption>${s.page} · ${s.vp}</figcaption></figure>`;
    const html = `<!doctype html><meta charset="utf-8"><style>
      body{margin:0;padding:24px;background:#05070a;color:#cfe;font:14px system-ui}
      h1{font-size:20px;margin:0 0 16px}
      .grid{display:grid;grid-template-columns:repeat(3,360px) 140px;gap:16px;align-items:start}
      figure{margin:0}img{width:100%;display:block;border:1px solid #234;border-radius:6px}
      figcaption{margin-top:4px;color:#8ab}
    </style><h1>Founder OS — every page at 1280×720, 1536×864, 1920×1080 and 390×844</h1><div class="grid">${
      rows.map((p) => VIEWPORTS.map((vp) => { const s = shots.find((x) => x.vp === vp.name && x.page === p); return s ? cell(s) : "<div></div>"; }).join("")).join("")
    }</div>`;
    const sheetCtx = await browser.newContext({ viewport: { width: 1700, height: 1000 } });
    const sheet = await sheetCtx.newPage();
    await sheet.setContent(html, { waitUntil: "load" });
    const sheetPath = process.env.SHEET || path.join(OUT, "contact-sheet.jpg");
    await sheet.screenshot({ path: sheetPath, fullPage: true, type: "jpeg", quality: 82 });
    fs.writeFileSync(path.join(OUT, "measurements.json"), JSON.stringify(table, null, 2));
    console.log(`contact sheet: ${path.relative(ROOT, sheetPath)}`);
  } catch (e) {
    failures++;
    console.log("✗ run aborted: " + e.message);
    console.log(serverLog.slice(-2000));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.log(failures ? `\n${failures} check(s) failed` : "\nall layout checks passed");
  process.exit(failures ? 1 : 0);
}

main();
