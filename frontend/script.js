document.addEventListener("DOMContentLoaded", () => {

    // ==============================
    // DOM ELEMENTS
    // ==============================
    const chatBox = document.getElementById("chat-box");
    const inputField = document.getElementById("user-input");
    const chatForm = document.getElementById("chat-form");

    const dailyForm = document.getElementById("daily-form");
    const status = document.getElementById("status");
    const weeklyTargetSelect = document.getElementById("weekly-target");

    const weeklyObjectivesForm = document.getElementById("weekly-objective-form");
    const weeklyObjectivesList = document.getElementById("weekly-objectives-list");
    const weeklyKpiSummary = document.getElementById("weekly-kpi-summary");

    // ==============================
    // STORAGE KEYS
    // ==============================
    const WEEKLY_TARGETS_KEY = "weeklyTargets";
    const DAILY_LOGS_KEY = "dailyLogs";

    // ==============================
    // SESSION MANAGEMENT (CHAT)
    // ==============================
    let sessionId = localStorage.getItem("sessionId");
    if (!sessionId) {
        sessionId = Date.now().toString(36) + Math.random().toString(36).substring(2);
        localStorage.setItem("sessionId", sessionId);
    }

    // ==============================
    // UTILITY FUNCTIONS
    // ==============================
    function safeParse(key) {
        try { return JSON.parse(localStorage.getItem(key)) || []; }
        catch { return []; }
    }

    function isNumber(value) { return !isNaN(value) && value !== ""; }

    function addMessageToDOM(message, sender) {
        const div = document.createElement("div");
        div.className = `message ${sender}`;
        div.innerHTML = message.replace(/\n/g, "<br>");
        chatBox.appendChild(div);
        chatBox.scrollTop = chatBox.scrollHeight;
    }

    function addMessage(message, sender) {
        chatHistory.push({ sender, message });
        localStorage.setItem("chatHistory", JSON.stringify(chatHistory));
        addMessageToDOM(message, sender);
    }

    function showTypingIndicator() {
        const div = document.createElement("div");
        div.className = "message bot typing";
        div.innerHTML = "AI is thinking...";
        chatBox.appendChild(div);
        chatBox.scrollTop = chatBox.scrollHeight;
        return div;
    }

    function scrollChatSmooth() {
        chatBox.scrollTo({ top: chatBox.scrollHeight, behavior: "smooth" });
    }

    // ==============================
    // CHAT HISTORY
    // ==============================
    let chatHistory = safeParse("chatHistory");
    chatHistory.forEach(item => addMessageToDOM(item.message, item.sender));

    async function routeMessage(payload) {
        const msg = payload.message.toLowerCase();
        if (msg.includes("kpi") || msg.includes("on track")) {
            addMessage(getKPIReport(), "bot");
            return;
        }
        await sendToBackend(payload);
    }

    async function sendToBackend(payload) {
        const typingIndicator = showTypingIndicator();
        try {
            const response = await fetch("http://127.0.0.1:8000/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            typingIndicator.remove();
            addMessage(data.reply, "bot");
        } catch {
            typingIndicator.remove();
            addMessage("Backend offline.", "bot");
        }
    }

    async function sendMessage() {
        const message = inputField.value.trim();
        if (!message) return;

        inputField.disabled = true;
        addMessage(message, "user");
        inputField.value = "";

        const payload = { message, session_id: sessionId };
        await routeMessage(payload);

        inputField.disabled = false;
        inputField.focus();
        scrollChatSmooth();
    }

    chatForm.addEventListener("submit", e => { e.preventDefault(); sendMessage(); });

    // ==============================
    // WEEKLY TARGETS
    // ==============================
    function loadWeeklyObjectives() { return safeParse(WEEKLY_TARGETS_KEY); }
    function saveWeeklyObjectives(objectives) { localStorage.setItem(WEEKLY_TARGETS_KEY, JSON.stringify(objectives)); }

    function seedWeeklyTargetsIfEmpty() {
        if (loadWeeklyObjectives().length > 0) return;
        saveWeeklyObjectives([
            { id: crypto.randomUUID(), title: "Finish AI microservice prototype", targetCount: 5 },
            { id: crypto.randomUUID(), title: "Complete weekly trading review", targetCount: 3 },
            { id: crypto.randomUUID(), title: "Prepare A-level application draft", targetCount: 4 },
            { id: crypto.randomUUID(), title: "Do 3x calisthenics workouts", targetCount: 3 },
            { id: crypto.randomUUID(), title: "Read 10 pages", targetCount: 5 }
        ]);
    }

    seedWeeklyTargetsIfEmpty();

    function populateWeeklyTargets() {
        weeklyTargetSelect.innerHTML = "";
        ["general", "admin", "nothing"].forEach(label => {
            const opt = document.createElement("option");
            opt.value = label;
            opt.textContent = label.toUpperCase();
            weeklyTargetSelect.appendChild(opt);
        });
        loadWeeklyObjectives().forEach(t => {
            const option = document.createElement("option");
            option.value = t.id;
            option.textContent = t.title;
            weeklyTargetSelect.appendChild(option);
        });
    }

    populateWeeklyTargets();

    // ==============================
    // DAILY LOG
    // ==============================
    function getDailyLogs() { return safeParse(DAILY_LOGS_KEY); }

    dailyForm.addEventListener("submit", e => {
        e.preventDefault();
        const entry = document.getElementById("entry").value.trim();
        const contribution = document.getElementById("contribution").value.trim();
        const selected = weeklyTargetSelect.value;

        if (!entry || !contribution || !isNumber(contribution)) {
            status.textContent = "Please fill all fields correctly (contribution must be a number).";
            return;
        }

        const logs = getDailyLogs();
        logs.push({ entry, contribution: Number(contribution), weeklyTargetId: selected, timestamp: Date.now() });
        localStorage.setItem(DAILY_LOGS_KEY, JSON.stringify(logs));
        status.textContent = "Execution logged successfully!";
        dailyForm.reset();
        updateKPISummary();
    });

    // ==============================
    // KPI BUILD & REPORT
    // ==============================
    function buildWeeklyKPI() {
        const weeklyTargets = loadWeeklyObjectives();
        const dailyLogs = getDailyLogs();
        const now = Date.now();
        const weekLogs = dailyLogs.filter(
            log => now - log.timestamp <= 7 * 24 * 60 * 60 * 1000 &&
                weeklyTargets.some(t => t.id === log.weeklyTargetId)
        );

        const progress = {};
        weeklyTargets.forEach(target => { progress[target.id] = { title: target.title, target: target.targetCount, executed: 0 }; });
        weekLogs.forEach(log => { if (progress[log.weeklyTargetId]) progress[log.weeklyTargetId].executed += log.contribution; });
        return progress;
    }

    function getKPIReport() {
        const progress = buildWeeklyKPI();
        const keys = Object.keys(progress);
        if (!keys.length) return "No weekly targets defined.";

        let report = "WEEKLY KPI REPORT\n----------------\n";
        keys.forEach(id => { const p = progress[id]; report += `${p.title}: ${p.executed}/${p.target}\n`; });
        return report;
    }

    function updateKPISummary() {
        const logs = getDailyLogs();
        const now = Date.now();
        const weekLogs = logs.filter(log => now - log.timestamp <= 7 * 24 * 60 * 60 * 1000);

        document.getElementById("kpi-total").textContent = logs.length;
        document.getElementById("kpi-week").textContent = weekLogs.length;
        document.getElementById("kpi-avg").textContent =
            logs.length === 0 ? 0 : (logs.reduce((s, l) => s + Number(l.contribution), 0) / logs.length).toFixed(2);

        renderWeeklyObjectivesDashboard();
        renderWeeklyKpiSummaryBar();
    }

    // ==============================
    // WEEKLY OBJECTIVES DASHBOARD
    // ==============================
    function renderWeeklyObjectivesDashboard() {
        const objectives = loadWeeklyObjectives();
        const progress = buildWeeklyKPI();
        weeklyObjectivesList.innerHTML = "";

        if (!objectives.length) {
            weeklyObjectivesList.innerHTML = `<li style="padding:10px;color:#5b9bd5;text-align:center;">No objectives yet. Add one above.</li>`;
            weeklyKpiSummary.innerHTML = `<div style="text-align:center;color:#5b9bd5;">No KPI data yet.</div>`;
            return;
        }

        
        objectives.forEach(obj => {
            const executed = progress[obj.id]?.executed || 0;
            const percent = Math.min((executed / obj.targetCount) * 100, 100).toFixed(0);
            const color = percent >= 80 ? "#00b050" : percent >= 50 ? "#ffc000" : "#c00000";

            const li = document.createElement("li");
            li.className = "objective-item";
            li.setAttribute("draggable", "true");
            li.dataset.id = obj.id;
            li.style.padding = "8px";
            li.style.border = "1px solid #5b9bd5";
            li.style.marginBottom = "6px";
            li.style.backgroundColor = "#e6f0fa";
            li.style.cursor = "grab";

            li.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span style="font-weight:bold;color:#1f4e79;">${obj.title}</span>
                    <span>${executed}/${obj.targetCount}</span>
                </div>
                <div style="height:8px; background:#d9e6f2; border-radius:4px; margin-top:4px;">
                    <div style="width:${percent}%; height:100%; background:${color}; border-radius:4px;"></div>
                </div>
                <div style="margin-top:4px;">
                    <button class="edit-btn" style="background:#5b9bd5;color:white;border:none;padding:2px 6px;border-radius:3px;margin-right:4px;">Edit</button>
                    <button class="delete-btn" style="background:#c00000;color:white;border:none;padding:2px 6px;border-radius:3px;">Delete</button>
                </div>
            `;

            li.querySelector(".edit-btn").addEventListener("click", () => {
                const newTitle = prompt("Edit title:", obj.title);
                const newTarget = parseInt(prompt("Edit target count:", obj.targetCount));
                if (newTitle && newTarget > 0) {
                    obj.title = newTitle;
                    obj.targetCount = newTarget;
                    const allObj = loadWeeklyObjectives();
                    const idx = allObj.findIndex(o => o.id === obj.id);
                    allObj[idx] = obj;
                    saveWeeklyObjectives(allObj);
                    renderWeeklyObjectivesDashboard();
                    populateWeeklyTargets();
                }
            });

            li.querySelector(".delete-btn").addEventListener("click", () => {
                const updated = loadWeeklyObjectives().filter(o => o.id !== obj.id);
                saveWeeklyObjectives(updated);
                renderWeeklyObjectivesDashboard();
                populateWeeklyTargets();
            });

            weeklyObjectivesList.appendChild(li);
        });

        enableDragAndDrop();
        renderWeeklyKpiSummaryBar();
    }

    function enableDragAndDrop() {
        const items = weeklyObjectivesList.querySelectorAll(".objective-item");
        let dragSrcEl = null;

        items.forEach(item => {
            item.addEventListener("dragstart", e => {
                dragSrcEl = item;
                e.dataTransfer.effectAllowed = "move";
                e.dataTransfer.setData("text/html", item.innerHTML);
            });

            item.addEventListener("dragover", e => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; });
            item.addEventListener("drop", e => {
                e.stopPropagation();
                if (dragSrcEl !== item) {
                    const allObj = loadWeeklyObjectives();
                    const dragIndex = Array.from(weeklyObjectivesList.children).indexOf(dragSrcEl);
                    const dropIndex = Array.from(weeklyObjectivesList.children).indexOf(item);
                    const [moved] = allObj.splice(dragIndex, 1);
                    allObj.splice(dropIndex, 0, moved);
                    saveWeeklyObjectives(allObj);
                    renderWeeklyObjectivesDashboard();
                }
            });
        });
    }

    weeklyObjectivesForm.addEventListener("submit", e => {
        e.preventDefault();
        const title = document.getElementById("objective-title").value.trim();
        const targetCount = parseInt(document.getElementById("objective-target").value);
        if (!title || targetCount <= 0) { alert("Fill both fields correctly."); return; }

        const objectives = loadWeeklyObjectives();
        if (objectives.some(o => o.title.toLowerCase() === title.toLowerCase())) {
            alert("Objective already exists!"); return;
        }

        objectives.push({ id: crypto.randomUUID(), title, targetCount });
        saveWeeklyObjectives(objectives);
        weeklyObjectivesForm.reset();
        renderWeeklyObjectivesDashboard();
        populateWeeklyTargets();
    });

    // ==============================
    // KPI SUMMARY BAR
    // ============================

    // ==============================
    // INITIALIZE
    // ==============================
    updateKPISummary();
    

function renderWeeklyKpiSummaryBar() {
    const objectives = loadWeeklyObjectives();
    const progress = buildWeeklyKPI();
    weeklyKpiSummary.innerHTML = "";

    objectives.forEach(obj => {
        const executed = progress[obj.id]?.executed || 0;
        const percent = Math.min(executed / obj.targetCount, 1);

        const size = 120;
        const stroke = 10;
        const radius = (size - stroke) / 2;
        const ARC_SPAN = Math.PI * 1.6; // ~80% of circle
const startAngle = Math.PI * 0.7;
const endAngle = startAngle + ARC_SPAN;


        const cx = size / 2;
        const cy = size / 2;

        const polar = (a) => ({
            x: cx + radius * Math.cos(a),
            y: cy + radius * Math.sin(a)
        });

        const start = polar(startAngle);
        const end = polar(endAngle);

        const arcPath = `
          M ${start.x} ${start.y}
          A ${radius} ${radius} 0 1 1 ${end.x} ${end.y}
        `;

        const container = document.createElement("div");
        container.style.textAlign = "center";
        container.style.margin = "8px";

        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("width", size);
        svg.setAttribute("height", size * 0.65);
        svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
        svg.style.overflow = "visible";

        const bg = document.createElementNS("http://www.w3.org/2000/svg", "path");
        bg.setAttribute("d", arcPath);
        bg.setAttribute("stroke", "#023859");
        bg.setAttribute("stroke-width", stroke);
        bg.setAttribute("fill", "none");
        bg.setAttribute("stroke-linecap", "round");

        const fg = document.createElementNS("http://www.w3.org/2000/svg", "path");
        fg.setAttribute("d", arcPath);
        fg.setAttribute("stroke", "#54ACBF");
        fg.setAttribute("stroke-width", stroke);
        fg.setAttribute("fill", "none");
        fg.setAttribute("stroke-linecap", "round");

        svg.appendChild(bg);
        svg.appendChild(fg);
        container.appendChild(svg);

        const length = fg.getTotalLength();
        fg.style.strokeDasharray = length;
        fg.style.strokeDashoffset = length;
        fg.style.transition = "stroke-dashoffset 1s ease-out";

        requestAnimationFrame(() => {
            fg.style.strokeDashoffset = length * (1 - percent);
        });

        const label = document.createElement("div");
        label.textContent = `${obj.title} (${executed}/${obj.targetCount})`;
        label.style.marginTop = "-6px";
        label.style.color = "#A7EBF2";
        label.style.fontSize = "12px";
        label.style.fontWeight = "600";

        container.appendChild(label);
        weeklyKpiSummary.appendChild(container);
    });
}



});
