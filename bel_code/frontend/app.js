/* BEL CODE — AI-First Workspace & On-Demand IDE Controller */
(function () {
  "use strict";

  const STORAGE_KEY = "bel_code_antigravity_ui_v1";

  const STATE = {
    mode: "ai", // "ai" | "ide"
    sidebarCollapsed: false,
    workspacePath: "",
    projectId: "",
    gitBranch: "main",
    recentProjects: [],
    collapsedProjectGroups: new Set(),
    sessions: [],
    activeSessionId: null,
    activeExecutionController: null,
    activeEventSource: null,
    attachments: [], // [{ id, type, label, content }]
    openTabs: [], // [{ path, content, savedContent, dirty, language }]
    activeEditorPath: null,
    editorMode: "code", // "code" | "diff"
    expandedFolders: new Set(["bel_code", "bel_code/frontend", "tests"]),
    changedFileMap: new Map(),
    problems: [],
    pendingApprovals: [],
    terminalHistory: [],
    terminalHistoryIdx: -1,
    outputStream: [],
    recognition: null,
    isRecording: false,
    registryProviders: [],
    wizardTab: "free_cloud",
    wizardSelectedId: "gemini",
    onboarding: null,
  };

  /* -------------------- Utilities -------------------- */
  function $(id) {
    return document.getElementById(id);
  }

  function qs(sel, root = document) {
    return root.querySelector(sel);
  }

  function qsa(sel, root = document) {
    return Array.from(root.querySelectorAll(sel));
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function relativeAge(ts) {
    if (!ts) return "";
    try {
      const diffMs = Date.now() - new Date(ts).getTime();
      if (Number.isNaN(diffMs) || diffMs < 0) return "now";
      const mins = Math.floor(diffMs / 60000);
      if (mins < 1) return "1m";
      if (mins < 60) return `${mins}m`;
      const hrs = Math.floor(mins / 60);
      if (hrs < 24) return `${hrs}h`;
      return `${Math.floor(hrs / 24)}d`;
    } catch {
      return "";
    }
  }

  function detectLanguage(path) {
    const ext = (path.split(".").pop() || "").toLowerCase();
    const map = {
      py: "python",
      js: "javascript",
      ts: "typescript",
      html: "html",
      css: "css",
      json: "json",
      md: "markdown",
    };
    return map[ext] || "plaintext";
  }

  function showToast(msg) {
    const box = $("toast-container");
    if (!box) return;
    const item = document.createElement("div");
    item.className = "toast-item";
    item.textContent = msg;
    box.appendChild(item);
    setTimeout(() => item.remove(), 3000);
  }

  function appendOutputLine(source, msg) {
    const line = `[${new Date().toLocaleTimeString([], { hour12: false })}] [${source}] ${msg}`;
    STATE.outputStream.push(line);
    if (STATE.outputStream.length > 200) STATE.outputStream.shift();
    if ($("bottom-output-stream")) {
      $("bottom-output-stream").textContent = STATE.outputStream.join("\n");
    }
  }

  async function apiFetch(url, options = {}) {
    const resp = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const ct = resp.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await resp.json() : { detail: await resp.text() };
    if (!resp.ok) throw new Error(data?.detail || data?.message || `HTTP ${resp.status}`);
    return data;
  }

  /* -------------------- Mode Switching: AI Workspace <-> Open IDE -------------------- */
  function setWorkspaceMode(mode) {
    STATE.mode = mode === "ide" ? "ide" : "ai";
    document.body.setAttribute("data-mode", STATE.mode);

    const isIde = STATE.mode === "ide";
    $("ai-workspace-mode")?.classList.toggle("hidden", isIde);
    $("ide-mode-container")?.classList.toggle("hidden", !isIde);

    const btn = $("btn-open-ide");
    if (btn) {
      btn.textContent = isIde ? "← AI Workspace" : "Open IDE";
      btn.classList.toggle("active-ide", isIde);
    }

    if (isIde) {
      refreshIdeFileTree();
      refreshProblems();
      refreshLogs();
    }
  }

  function toggleSidebar() {
    if (STATE.mode === "ide") {
      $("ide-left-sidebar")?.classList.toggle("hidden");
      return;
    }
    if (window.innerWidth <= 960) {
      const opening = !$("left-panel")?.classList.contains("drawer-open");
      $("left-panel")?.classList.toggle("drawer-open", opening);
      $("drawer-backdrop")?.classList.toggle("hidden", !opening);
      return;
    }
    STATE.sidebarCollapsed = !STATE.sidebarCollapsed;
    $("left-panel")?.classList.toggle("collapsed", STATE.sidebarCollapsed);
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ sidebarCollapsed: STATE.sidebarCollapsed }));
    } catch {
      // Ignore
    }
  }

  /* -------------------- Projects & Grouped Conversations in Left Sidebar -------------------- */
  async function refreshProjectAndSessions() {
    try {
      const [projData, sessData] = await Promise.all([
        apiFetch("/api/projects/current"),
        apiFetch("/api/opencode/sessions"),
      ]);

      STATE.workspacePath = projData.workspace_path || "";
      STATE.projectId = projData.project_id || "";
      STATE.gitBranch = projData.git_branch || "main";
      STATE.recentProjects = projData.recent_projects || [];
      STATE.sessions = sessData.sessions || [];

      const activeName = (STATE.workspacePath || "BEL CODE").replace(/\\/g, "/").split("/").pop() || "BEL CODE";
      if ($("workspace-path-input") && STATE.workspacePath) $("workspace-path-input").value = STATE.workspacePath;

      // Update Composer Project & Environment selectors
      const projSel = $("composer-project-select");
      if (projSel) {
        projSel.innerHTML = "";
        const projectsList = STATE.recentProjects.length
          ? STATE.recentProjects
          : [{ name: activeName, path: STATE.workspacePath, project_id: STATE.projectId }];
        projectsList.forEach((p) => {
          const opt = document.createElement("option");
          opt.value = p.path;
          opt.textContent = p.name;
          if (p.path === STATE.workspacePath) opt.selected = true;
          projSel.appendChild(opt);
        });
      }
      const envSel = $("composer-env-select");
      if (envSel) {
        envSel.innerHTML = `
          <option value="local">Local Checkout (${escapeHtml(STATE.gitBranch)})</option>
          <option value="isolated">Isolated Workspace</option>`;
      }

      renderSidebarProjectsTree(activeName);
    } catch (err) {
      appendOutputLine("projects", err.message);
    }
  }

  function renderSidebarProjectsTree(activeProjectName) {
    const container = $("sidebar-projects-tree");
    if (!container) return;

    const projects = STATE.recentProjects.length
      ? STATE.recentProjects
      : [{ name: activeProjectName || "BEL CODE", path: STATE.workspacePath }];

    const norm = (p) => String(p || "").replace(/\\/g, "/").toLowerCase();

    container.innerHTML = projects
      .map((proj) => {
        const key = norm(proj.path);
        const collapsed = STATE.collapsedProjectGroups.has(key);
        const matchingSessions = STATE.sessions.filter((s) => {
          const sDir = norm(s.directory || s.workspace_path || STATE.workspacePath);
          return !sDir || sDir === key || key === norm(STATE.workspacePath);
        });

        const convHtml = collapsed
          ? ""
          : matchingSessions.length
          ? matchingSessions
              .slice(0, 12)
              .map(
                (s) => `
                <div class="conv-item-row ${s.id === STATE.activeSessionId ? "active" : ""}" data-sess-id="${escapeHtml(
                  s.id
                )}">
                  <span class="conv-item-title">${escapeHtml(s.title || s.id)}</span>
                  <span class="conv-item-age">${escapeHtml(relativeAge(s.updated_at || s.created_at))}</span>
                </div>`
              )
              .join("")
          : `<div class="muted-placeholder">No conversations yet</div>`;

        return `
          <div class="project-group">
            <div class="project-folder-row" data-toggle-proj="${escapeHtml(proj.path)}">
              <div class="project-folder-left">
                <svg viewBox="0 0 16 16" width="13.5" height="13.5" fill="none" stroke="currentColor" stroke-width="1.35">
                  <path d="M2 4H6.2L7.5 5.4H14V12.5H2V4Z" />
                </svg>
                <span class="project-folder-name">${escapeHtml(proj.name)}</span>
              </div>
              <span class="muted" style="font-size:10px;">${collapsed ? "▸" : ""}</span>
            </div>
            <div class="project-convs-list">${convHtml}</div>
          </div>`;
      })
      .join("");

    qsa("[data-toggle-proj]", container).forEach((el) =>
      el.addEventListener("click", () => {
        const path = el.getAttribute("data-toggle-proj");
        const key = String(path || "").replace(/\\/g, "/").toLowerCase();
        if (STATE.collapsedProjectGroups.has(key)) STATE.collapsedProjectGroups.delete(key);
        else STATE.collapsedProjectGroups.add(key);
        renderSidebarProjectsTree(activeProjectName);
      })
    );

    qsa("[data-sess-id]", container).forEach((el) =>
      el.addEventListener("click", () => loadConversation(el.getAttribute("data-sess-id")))
    );
  }

  async function switchWorkspacePath(path) {
    if (!path || !path.trim()) return;
    try {
      await apiFetch("/api/projects/select", {
        method: "POST",
        body: JSON.stringify({ workspace_path: path.trim() }),
      });
      $("project-open-inline")?.classList.add("hidden");
      showToast(`Project switched: ${path.trim()}`);
      await Promise.all([refreshProjectAndSessions(), refreshIdeFileTree(), refreshProblems()]);
    } catch (err) {
      showToast(err.message);
    }
  }

  /* -------------------- Central Conversation & Honest Agent Execution -------------------- */
  function showConversationStage(show) {
    $("welcome-canvas")?.classList.toggle("hidden", show);
    $("conversation-canvas")?.classList.toggle("hidden", !show);
  }

  function startNewConversation() {
    STATE.activeSessionId = null;
    if ($("ai-conversation-feed")) $("ai-conversation-feed").innerHTML = "";
    if ($("ide-ai-mirror-feed")) $("ide-ai-mirror-feed").innerHTML = "";
    $("btn-conv-revert")?.classList.add("hidden");
    showConversationStage(false);
    renderSidebarProjectsTree();
    $("task-composer-input")?.focus();
  }

  function appendConversationTurn(role, text, details = null) {
    showConversationStage(true);
    const feed = $("ai-conversation-feed");
    const mirror = $("ide-ai-mirror-feed");
    if (!feed) return;

    const block = document.createElement("div");
    block.className = "turn-block";

    if (role === "user") {
      block.innerHTML = `<div class="turn-user-msg">${escapeHtml(text)}</div>`;
    } else {
      const tools = Array.isArray(details?.tools) ? details.tools : [];
      const files = Array.isArray(details?.files) ? details.files : [];
      const ver = details?.verification || null;
      const verified = Boolean(ver?.verified === true && ver?.exit_code === 0);
      const verifyLabel = verified
        ? "Verified (Exit 0)"
        : ver && ver.exit_code !== null && ver.exit_code !== undefined
        ? `Exit ${ver.exit_code}`
        : "Not Verified";

      const hasDetails = Boolean(details && (tools.length || files.length || details.diff || details.agent));
      const execHtml = hasDetails
        ? `<details class="turn-execution-details">
            <summary>
              <span>${escapeHtml(details.agent || "build")} · ${tools.length} tools · ${files.length} files</span>
              <span class="verify-tag ${verified ? "ok" : ""}">${escapeHtml(verifyLabel)}</span>
            </summary>
            <div class="turn-details-body">
              ${
                tools.length
                  ? `<div class="mono">${tools
                      .map((t) => `<div>• ${escapeHtml(t.tool || t.name)}: ${escapeHtml(t.status || "ok")}</div>`)
                      .join("")}</div>`
                  : `<div class="muted">No external tool calls recorded.</div>`
              }
              ${
                files.length
                  ? `<div>${files
                      .map((f) => `<button type="button" class="turn-file-chip" data-open-ide-file="${escapeHtml(f)}">${escapeHtml(f)}</button>`)
                      .join("")}</div>`
                  : ""
              }
              ${details.diff ? `<pre class="turn-inline-diff">${escapeHtml(details.diff.slice(0, 2500))}</pre>` : ""}
            </div>
          </details>`
        : "";

      block.innerHTML = `<div class="turn-agent-msg">${escapeHtml(text)}</div>${execHtml}`;
      qsa("[data-open-ide-file]", block).forEach((btn) =>
        btn.addEventListener("click", async () => {
          setWorkspaceMode("ide");
          await openIdeFile(btn.getAttribute("data-open-ide-file"));
          setIdeEditorMode("diff");
        })
      );
    }

    feed.appendChild(block);
    feed.scrollTop = feed.scrollHeight;

    if (mirror) {
      const m = document.createElement("div");
      m.className = role === "user" ? "turn-user-msg" : "turn-agent-msg";
      m.style.fontSize = "12px";
      m.textContent = text;
      mirror.appendChild(m);
      mirror.scrollTop = mirror.scrollHeight;
    }
  }

  async function loadConversation(sessionId) {
    if (!sessionId) return;
    try {
      const data = await apiFetch(`/api/opencode/sessions/${encodeURIComponent(sessionId)}`);
      const sess = data.session || data;
      STATE.activeSessionId = sessionId;
      if ($("active-conv-title")) $("active-conv-title").textContent = sess.title || sessionId;
      if ($("active-conv-agent-tag")) $("active-conv-agent-tag").textContent = sess.agent || "build";

      if ($("ai-conversation-feed")) $("ai-conversation-feed").innerHTML = "";
      if ($("ide-ai-mirror-feed")) $("ide-ai-mirror-feed").innerHTML = "";

      const msgs = data.messages || sess.messages || [];
      if (!msgs.length) {
        showConversationStage(true);
      } else {
        msgs.forEach((m) => appendConversationTurn(m.role || "assistant", m.content || m.text || ""));
      }
      renderSidebarProjectsTree();
    } catch (err) {
      showToast(err.message);
    }
  }

  async function submitPrompt(rawText) {
    const raw = (rawText || "").trim();
    if (!raw) return;

    const ctx = STATE.attachments.length
      ? "\n\n[Attached Context]\n" + STATE.attachments.map((a) => `--- ${a.label} ---\n${a.content}`).join("\n\n")
      : "";
    const fullPrompt = `${raw}${ctx}`;

    const agent = $("composer-agent-select")?.value || "build";
    const model = $("composer-model-select")?.value || undefined;
    const permissionMode = $("composer-permission-mode")?.value || "ask";
    const reasoning = $("composer-reasoning-select")?.value || "standard";

    if ($("active-conv-title")) $("active-conv-title").textContent = raw.slice(0, 64);
    if ($("active-conv-agent-tag")) $("active-conv-agent-tag").textContent = agent;
    if ($("task-composer-input")) $("task-composer-input").value = "";

    appendConversationTurn("user", raw);

    $("btn-execute-task")?.classList.add("hidden");
    $("btn-stop-execution")?.classList.remove("hidden");
    if ($("agent-ws-name")) $("agent-ws-name").textContent = agent;
    if ($("agent-ws-status")) $("agent-ws-status").textContent = "Running";
    if ($("agent-ws-progress")) $("agent-ws-progress").textContent = "35%";

    apiFetch("/api/security/mode", { method: "POST", body: JSON.stringify({ mode: permissionMode }) }).catch(() => {});

    if (STATE.activeExecutionController) STATE.activeExecutionController.abort();
    STATE.activeExecutionController = new AbortController();

    try {
      if (!STATE.activeSessionId) {
        const sess = await apiFetch("/api/opencode/sessions", {
          method: "POST",
          body: JSON.stringify({ title: raw.slice(0, 64), agent }),
        });
        STATE.activeSessionId = sess.id || sess.session?.id;
      }

      if (STATE.activeEventSource) STATE.activeEventSource.close();
      if (typeof EventSource !== "undefined" && STATE.activeSessionId) {
        const es = new EventSource(`/api/opencode/sessions/${encodeURIComponent(STATE.activeSessionId)}/events`);
        STATE.activeEventSource = es;
        es.onmessage = (ev) => {
          try {
            const p = JSON.parse(ev.data);
            if (p.type === "permission" || p.type === "permission.updated") refreshApprovals();
          } catch {
            // Keepalive
          }
        };
        es.onerror = () => es.close();
      }

      const turn = await apiFetch(
        `/api/opencode/sessions/${encodeURIComponent(STATE.activeSessionId)}/message`,
        {
          method: "POST",
          signal: STATE.activeExecutionController.signal,
          body: JSON.stringify({ prompt: fullPrompt, agent, model, permission_mode: permissionMode, reasoning }),
        }
      );

      let diffText = "";
      const files = turn.files_changed || turn.modified_files || [];
      if (files.length && STATE.activeSessionId) {
        const dRes = await apiFetch(`/api/opencode/sessions/${encodeURIComponent(STATE.activeSessionId)}/diff`).catch(() => ({ diff: "" }));
        diffText = dRes.diff || "";
      }

      const reply = turn.response || turn.assistant_text || turn.message?.content || turn.output || turn.error || "Completed.";
      appendConversationTurn("assistant", reply, {
        agent,
        tools: turn.tool_activity || turn.tool_calls || [],
        files,
        diff: diffText,
        verification: turn.verification || null,
      });

      $("btn-conv-revert")?.classList.toggle("hidden", files.length === 0);
      if ($("agent-ws-status")) $("agent-ws-status").textContent = turn.error ? "Attention Needed" : "Completed";
      if ($("agent-ws-progress")) $("agent-ws-progress").textContent = "100%";

      await Promise.all([refreshProjectAndSessions(), refreshApprovals(), refreshIdeChangedFiles()]);
    } catch (err) {
      if (err.name !== "AbortError") {
        appendConversationTurn("assistant", `Execution failed: ${err.message}`);
        if ($("agent-ws-status")) $("agent-ws-status").textContent = "Error";
      }
    } finally {
      $("btn-stop-execution")?.classList.add("hidden");
      $("btn-execute-task")?.classList.remove("hidden");
      if (STATE.activeEventSource) {
        STATE.activeEventSource.close();
        STATE.activeEventSource = null;
      }
    }
  }

  /* -------------------- Attachments & Providers & Approvals -------------------- */
  function renderAttachmentChips() {
    const box = $("attached-context-chips");
    if (!box) return;
    box.innerHTML = STATE.attachments
      .map(
        (a) =>
          `<span class="attached-chip">${escapeHtml(a.label)} <button type="button" data-rm-att="${escapeHtml(
            a.id
          )}">×</button></span>`
      )
      .join("");
    qsa("[data-rm-att]", box).forEach((b) =>
      b.addEventListener("click", () => {
        STATE.attachments = STATE.attachments.filter((x) => x.id !== b.getAttribute("data-rm-att"));
        renderAttachmentChips();
      })
    );
  }

  function addAttachment(type, label, content) {
    const id = `${type}:${label}`;
    if (!STATE.attachments.some((a) => a.id === id)) {
      STATE.attachments.push({ id, type, label, content });
      renderAttachmentChips();
      showToast(`Attached ${label}`);
    }
    $("attach-menu")?.classList.add("hidden");
  }

  function syncProviderFormFields() {
    const pid = $("provider-id-select")?.value;
    const p = (STATE.providers || []).find((x) => x.id === pid);
    if (!p) return;
    if ($("provider-base-url")) $("provider-base-url").value = p.base_url || "";
    if ($("provider-default-model")) $("provider-default-model").value = p.default_model || "";
  }

  async function refreshProviders() {
    try {
      const data = await apiFetch("/api/providers");
      const providers = data.providers || [];
      const primaryPid = data.primary_provider || "freellm";
      STATE.providers = providers;

      const pidSelect = $("provider-id-select");
      if (pidSelect && providers.length) {
        const prev = pidSelect.value;
        pidSelect.innerHTML = "";
        providers.forEach((p) => {
          const opt = document.createElement("option");
          opt.value = p.id;
          opt.textContent = p.name || p.id;
          pidSelect.appendChild(opt);
        });
        if (prev && providers.some((p) => p.id === prev)) pidSelect.value = prev;
        else if (providers.some((p) => p.id === primaryPid)) pidSelect.value = primaryPid;
      }

      syncProviderFormFields();
      const sel = $("composer-model-select");
      if (sel) {
        const previousVal = sel.value;
        sel.innerHTML = "";
        const ordered = [...providers].sort((a, b) => {
          if (a.id === primaryPid) return -1;
          if (b.id === primaryPid) return 1;
          const aReady = a.has_api_key || !a.requires_api_key ? 0 : 1;
          const bReady = b.has_api_key || !b.requires_api_key ? 0 : 1;
          return aReady - bReady;
        });
        const primaryObj = providers.find((p) => p.id === primaryPid);
        const primaryTarget = primaryObj ? `${primaryObj.id}/${primaryObj.default_model || (primaryObj.models && primaryObj.models[0]) || ""}` : "";
        ordered.forEach((p) => {
          const list = Array.from(new Set([...(p.models || []), p.default_model].filter(Boolean)));
          if (!list.length) return;
          const shortName = String(p.name || p.id).replace(/\s*\(Primary Default\)/i, "");
          const isReady = p.has_api_key || p.configured || !p.requires_api_key;
          const grp = document.createElement("optgroup");
          grp.label = `${shortName}${isReady ? ` (${list.length} models)` : " (setup key)"}`;
          list.forEach((m) => {
            const opt = document.createElement("option");
            opt.value = `${p.id}/${m}`;
            opt.textContent = `${shortName} · ${m}`;
            grp.appendChild(opt);
          });
          sel.appendChild(grp);
        });
        const allValues = Array.from(sel.options).map((o) => o.value);
        if (previousVal && allValues.includes(previousVal)) sel.value = previousVal;
        else if (primaryTarget && allValues.includes(primaryTarget)) sel.value = primaryTarget;
      }

      const grid = $("providers-catalog-grid");
      if (grid) {
        grid.innerHTML = providers
          .map((p) => {
            const roles = [
              p.is_primary ? "Primary" : "",
              p.is_secondary ? "Secondary" : "",
              p.is_fallback ? "Fallback" : "",
            ]
              .filter(Boolean)
              .join(" · ");
            const keyState = !p.requires_api_key
              ? "No key needed"
              : p.has_api_key || p.configured
              ? `Key configured (${(p.accounts || ["default"]).join(", ")})`
              : "Needs key";
            return `
            <div class="clean-row">
              <span><strong>${escapeHtml(p.name || p.id)}</strong> <span class="muted mono">${escapeHtml(
              p.default_model || ""
            )}</span> ${roles ? `<span class="subtle-pill">${escapeHtml(roles)}</span>` : ""}</span>
              <span class="subtle-pill">${escapeHtml(keyState)}</span>
            </div>`;
          })
          .join("");
      }
    } catch {
      // Ignore
    }
  }

  /* -------------------- Connect Your AI & Beginner Onboarding -------------------- */
  async function refreshOnboardingStatus() {
    try {
      const st = await apiFetch("/api/onboarding/status");
      STATE.onboarding = st;
      const showGuide = Boolean(st?.needs_onboarding);
      $("welcome-onboarding-panel")?.classList.toggle("hidden", !showGuide);
      $("welcome-compact-ai-bar")?.classList.toggle("hidden", showGuide);

      const configured = st?.configured_providers || [];
      const primary = st?.primary_provider || "freellm";
      if ($("welcome-ai-status-pill")) {
        $("welcome-ai-status-pill").textContent = configured.length
          ? `Configured: ${configured.join(", ")}`
          : "No cloud API key saved yet — pick any option above";
      }
      if ($("welcome-compact-ai-label")) {
        $("welcome-compact-ai-label").textContent = configured.length
          ? `Primary AI: ${primary} (${configured.length} key${configured.length === 1 ? "" : "s"} saved)`
          : `Primary AI: ${primary}`;
      }
    } catch {
      // Ignore
    }
  }

  async function refreshRegistry() {
    try {
      const data = await apiFetch("/api/providers/registry");
      STATE.registryProviders = data.providers || [];
    } catch {
      STATE.registryProviders = [];
    }
  }

  function _filterWizardProviders(tab) {
    const all = STATE.registryProviders || [];
    if (tab === "local") {
      return all.filter((p) => p.category === "local" || p.provider_id === "ollama" || p.provider_id === "freellm");
    }
    if (tab === "custom") {
      return all.filter((p) => p.category === "byo_api" || p.category === "custom" || p.category === "paid" || p.provider_id === "custom");
    }
    return all.filter((p) => p.category === "free_cloud" || p.category === "trial_credits");
  }

  function renderWizardProviderList() {
    const box = $("wizard-provider-list");
    if (!box) return;
    const list = _filterWizardProviders(STATE.wizardTab);
    if (!list.length) {
      box.innerHTML = `<div class="muted-placeholder">No providers in this category.</div>`;
      return;
    }
    if (!list.some((p) => p.provider_id === STATE.wizardSelectedId)) {
      STATE.wizardSelectedId = list[0].provider_id;
    }
    box.innerHTML = list
      .map((p) => {
        const active = p.provider_id === STATE.wizardSelectedId ? "active" : "";
        const isFree = Boolean(p.advertised_free);
        const clsType = p.free_tier_classification || "custom";
        const badgeCls = isFree ? "badge-free" : "badge-warn";
        const badgeTxt =
          clsType === "free_trial_credits"
            ? "Trial Credits"
            : clsType === "local_open_source"
            ? "Local · Free"
            : clsType === "permanently_free"
            ? "Free Tier"
            : clsType === "free_tier_with_limits"
            ? "Free · Limited"
            : clsType === "paid"
            ? "Paid"
            : "Custom";
        const cardTxt = p.credit_card_required === false ? "No card" : "Card / billing";
        const statusTxt = p.has_api_key ? "Key saved" : p.requires_api_key === false ? "No key needed" : "Setup";
        return `
          <button type="button" class="wizard-provider-item ${active}" data-wiz-pid="${escapeHtml(p.provider_id)}">
            <div class="wizard-provider-item-top">
              <span>${escapeHtml(p.name)}</span>
              <span class="${badgeCls}">${escapeHtml(badgeTxt)}</span>
            </div>
            <div class="wizard-provider-item-meta">
              <span>${escapeHtml(cardTxt)}</span>
              <span>${escapeHtml(statusTxt)}</span>
            </div>
          </button>`;
      })
      .join("");

    qsa("[data-wiz-pid]", box).forEach((btn) =>
      btn.addEventListener("click", () => {
        STATE.wizardSelectedId = btn.getAttribute("data-wiz-pid");
        renderWizardProviderList();
        renderWizardDetailPane();
      })
    );
  }

  function populateWizardModelSelect(modelsList, preferredDefault) {
    const modelSel = $("wizard-model-select");
    if (!modelSel) return;
    const prev = modelSel.value;
    modelSel.innerHTML = "";
    const normalized = (modelsList || []).map((item) => {
      if (typeof item === "string") return { id: item, is_free: false };
      return { id: item.id || item.name || "", is_free: Boolean(item.is_free) };
    }).filter((x) => x.id);
    const seen = new Set();
    normalized.forEach((entry) => {
      if (seen.has(entry.id)) return;
      seen.add(entry.id);
      const opt = document.createElement("option");
      opt.value = entry.id;
      opt.textContent = entry.is_free ? `${entry.id} (free)` : entry.id;
      modelSel.appendChild(opt);
    });
    if ($("wizard-models-count")) {
      $("wizard-models-count").textContent = seen.size ? `(${seen.size} available)` : "";
    }
    const target = (prev && seen.has(prev)) ? prev : (preferredDefault && seen.has(preferredDefault)) ? preferredDefault : "";
    if (target) modelSel.value = target;
  }

  async function fetchModelsForWizardProvider(pid) {
    if (!pid) return;
    try {
      const res = await apiFetch(`/api/providers/${encodeURIComponent(pid)}/models`);
      const discovered = res.models || [];
      const p = (STATE.registryProviders || []).find((x) => x.provider_id === pid);
      const defModel = p ? (p.live_default_model || p.default_model) : "";
      if (discovered.length) {
        populateWizardModelSelect(discovered, defModel);
      }
      return discovered;
    } catch {
      return [];
    }
  }

  function renderWizardDetailPane() {
    const p = (STATE.registryProviders || []).find((x) => x.provider_id === STATE.wizardSelectedId);
    const card = $("wizard-provider-card");
    const customBox = $("wizard-custom-form");
    const cfgBox = $("wizard-config-box");
    const isCustomTab = STATE.wizardTab === "custom" && (!p || p.provider_id === "custom");

    customBox?.classList.toggle("hidden", STATE.wizardTab !== "custom");
    cfgBox?.classList.toggle("hidden", isCustomTab);

    if (!card || !p) return;
    const stepsHtml = (p.setup_steps || []).map((s) => `<li>${escapeHtml(s)}</li>`).join("");
    const regLink = p.registration_url
      ? `<a class="btn btn-xs" href="${escapeHtml(p.registration_url)}" target="_blank" rel="noopener noreferrer">Get API Key / Console ↗</a>`
      : "";
    const docsLink = p.docs_url
      ? `<a class="btn btn-xs" href="${escapeHtml(p.docs_url)}" target="_blank" rel="noopener noreferrer">Docs ↗</a>`
      : "";
    const cardText = p.credit_card_required === false ? "Not required for free/local tier" : "Required / Pay-as-you-go billing";
    const restrictionsText = Array.isArray(p.known_restrictions) && p.known_restrictions.length
      ? p.known_restrictions.join(" · ")
      : p.usage_restrictions || "";

    card.innerHTML = `
      <div class="wizard-section-box">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
          <div>
            <strong style="font-size:14px;">${escapeHtml(p.name)}</strong>
            <div class="muted" style="font-size:11.5px;margin-top:2px;">${escapeHtml(p.free_tier_summary || "")}</div>
          </div>
          <div style="display:flex;gap:6px;">${regLink}${docsLink}</div>
        </div>
        <div class="wizard-facts-grid">
          <div class="wizard-fact-cell"><span class="muted">Credit Card</span><span>${escapeHtml(cardText)}</span></div>
          <div class="wizard-fact-cell"><span class="muted">Rate / Quota</span><span>${escapeHtml(p.rate_limits_summary || "See docs")}</span></div>
          <div class="wizard-fact-cell"><span class="muted">Context Window</span><span>${escapeHtml(p.context_window_summary || "Model dependent")}</span></div>
          <div class="wizard-fact-cell"><span class="muted">Privacy &amp; Data</span><span>${escapeHtml(p.data_privacy_summary || "Check provider terms")}</span></div>
        </div>
        ${restrictionsText ? `<div class="muted" style="font-size:11.5px;"><strong>Limitations:</strong> ${escapeHtml(restrictionsText)}</div>` : ""}
        <div>
          <div class="wizard-field-label" style="margin-top:4px;">Steps 1–3 · Verified Setup Steps (verified ${escapeHtml(p.last_verified || "2026-11-14")})</div>
          <ol class="wizard-steps-list">${stepsHtml}</ol>
        </div>
      </div>`;

    const liveModels = (p.models && p.models.length ? p.models : p.free_models) || [];
    const fallbackList = liveModels.length ? liveModels : [p.live_default_model || p.default_model].filter(Boolean);
    const defModel = p.live_default_model || p.default_model || fallbackList[0] || "";
    populateWizardModelSelect(fallbackList, defModel);

    if ($("wizard-base-url-input")) $("wizard-base-url-input").value = p.live_base_url || p.api_base_url || "";
    if ($("wizard-api-key-input")) $("wizard-api-key-input").value = "";
    const needsAcctId = Boolean(p.requires_account_id) || String(p.api_base_url || "").includes("{account_id}");
    $("wizard-account-id-wrap")?.classList.toggle("hidden", !needsAcctId);
    $("wizard-api-key-wrap")?.classList.toggle("hidden", p.requires_api_key === false);
    if ($("wizard-status-output")) {
      $("wizard-status-output").textContent = p.has_api_key ? "Key already stored safely in vault." : "";
    }
    if (p.has_api_key || p.requires_api_key === false) {
      fetchModelsForWizardProvider(p.provider_id);
    }
  }

  async function openConnectAiWizard(tab = "free_cloud", providerId = null) {
    STATE.wizardTab = tab;
    await refreshRegistry();
    if (providerId) STATE.wizardSelectedId = providerId;
    else {
      const filtered = _filterWizardProviders(tab);
      if (filtered.length) STATE.wizardSelectedId = filtered[0].provider_id;
    }
    qsa("[data-wizard-tab]").forEach((b) =>
      b.classList.toggle("active", b.getAttribute("data-wizard-tab") === STATE.wizardTab)
    );
    openModal("modal-connect-ai");
    renderWizardProviderList();
    renderWizardDetailPane();
  }

  async function refreshRuntimeStatus() {
    try {
      const [st, ag] = await Promise.all([
        apiFetch("/api/opencode/status"),
        apiFetch("/api/opencode/agents").catch(() => ({ agents: [] })),
      ]);
      const ok = Boolean(st?.healthy || st?.opencode_available);
      if ($("runtime-dot")) $("runtime-dot").className = `status-dot ${ok ? "online" : "degraded"}`;
      if ($("runtime-label")) $("runtime-label").textContent = ok ? "Ready" : "Fallback";

      const agents = ag.agents || [];
      const agentSel = $("composer-agent-select");
      if (agentSel && agents.length) {
        agentSel.innerHTML = "";
        agents.forEach((a) => {
          const o = document.createElement("option");
          o.value = a.id || a.name;
          o.textContent = `Agent: ${a.name || a.id}`;
          agentSel.appendChild(o);
        });
      }
      if ($("agents-registry-output")) {
        $("agents-registry-output").innerHTML = agents
          .map((a) => `<div class="clean-row"><strong>${escapeHtml(a.name || a.id)}</strong><span class="muted">${escapeHtml(a.description || "")}</span></div>`)
          .join("");
      }
    } catch {
      if ($("runtime-dot")) $("runtime-dot").className = "status-dot error";
    }
  }

  async function refreshApprovals() {
    try {
      const data = await apiFetch("/api/security/approvals");
      const pending = (data.approvals || []).filter((a) => a.status === "pending");
      STATE.pendingApprovals = pending;
      if ($("header-approvals-badge")) $("header-approvals-badge").textContent = String(pending.length);
      $("btn-header-approvals")?.classList.toggle("hidden", pending.length === 0);

      const banner = $("active-approval-banner");
      if (banner) {
        banner.classList.toggle("hidden", pending.length === 0);
        if (pending.length && $("active-approval-description")) {
          $("active-approval-description").textContent = `${pending[0].action || pending[0].tool}: ${
            pending[0].description || pending[0].target || ""
          }`;
        }
      }
    } catch {
      // Ignore
    }
  }

  /* -------------------- IDE Mode (Explorer, Editor, Diff, Terminal, Problems, Logs) -------------------- */
  function highlightSyntax(code, lang) {
    let esc = escapeHtml(code);
    if (lang === "python") {
      esc = esc.replace(/(#.*)$/gm, '<span class="tok-cmt">$1</span>');
      esc = esc.replace(/(&quot;(?:[^&]|&(?!quot;))*&quot;|&#39;(?:[^&]|&(?!#39;))*&#39;)/g, '<span class="tok-str">$1</span>');
      esc = esc.replace(/\b(def|class|async|await|return|if|elif|else|for|while|in|import|from|try|except|with|None|True|False)\b/g, '<span class="tok-kw">$1</span>');
    } else if (lang === "javascript" || lang === "typescript") {
      esc = esc.replace(/(\/\/.*)$/gm, '<span class="tok-cmt">$1</span>');
      esc = esc.replace(/(&quot;(?:[^&]|&(?!quot;))*&quot;|&#39;(?:[^&]|&(?!#39;))*&#39;)/g, '<span class="tok-str">$1</span>');
      esc = esc.replace(/\b(const|let|var|function|async|await|return|if|else|for|while|import|export|from|try|catch|true|false|null)\b/g, '<span class="tok-kw">$1</span>');
    }
    return esc + "\n";
  }

  function renderIdeTabs() {
    const strip = $("ide-tabs-strip");
    if (!strip) return;
    const hasTabs = STATE.openTabs.length > 0;
    $("ide-editor-empty")?.classList.toggle("hidden", hasTabs);
    $("ide-editor-surface")?.classList.toggle("hidden", !hasTabs || STATE.editorMode !== "code");
    $("ide-diff-surface")?.classList.toggle("hidden", !hasTabs || STATE.editorMode !== "diff");

    strip.innerHTML = STATE.openTabs
      .map((t) => {
        const active = t.path === STATE.activeEditorPath ? "active" : "";
        return `
          <div class="editor-open-tab ${active}" data-tab-path="${escapeHtml(t.path)}">
            <span>${escapeHtml(t.path.split("/").pop())}${t.dirty ? " •" : ""}</span>
            <button type="button" class="btn-ghost" data-close-tab="${escapeHtml(t.path)}" style="padding:0 2px;">×</button>
          </div>`;
      })
      .join("");
    qsa("[data-tab-path]", strip).forEach((el) =>
      el.addEventListener("click", (e) => {
        if (!e.target.hasAttribute("data-close-tab")) activateIdeTab(el.getAttribute("data-tab-path"));
      })
    );
    qsa("[data-close-tab]", strip).forEach((el) =>
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const p = el.getAttribute("data-close-tab");
        STATE.openTabs = STATE.openTabs.filter((x) => x.path !== p);
        STATE.activeEditorPath = STATE.openTabs[0]?.path || null;
        if (STATE.activeEditorPath) activateIdeTab(STATE.activeEditorPath);
        else renderIdeTabs();
      })
    );
  }

  function syncIdeHighlight() {
    const tab = STATE.openTabs.find((t) => t.path === STATE.activeEditorPath);
    const ta = $("editor-textarea");
    const gutter = $("editor-line-gutter");
    const layer = $("editor-highlight-layer");
    if (!tab || !ta || !gutter || !layer) return;
    const n = Math.max(1, ta.value.split("\n").length);
    gutter.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
    layer.innerHTML = highlightSyntax(ta.value, tab.language);
  }

  async function setIdeEditorMode(mode) {
    STATE.editorMode = mode;
    $("btn-editor-mode-code")?.classList.toggle("active", mode === "code");
    $("btn-editor-mode-diff")?.classList.toggle("active", mode === "diff");
    renderIdeTabs();
    if (mode === "diff" && STATE.activeEditorPath) {
      const res = await apiFetch(`/api/git/diff?path=${encodeURIComponent(STATE.activeEditorPath)}`).catch(() => ({ diff: "" }));
      const lines = (res.diff || "").split("\n");
      if ($("editor-diff-table")) {
        $("editor-diff-table").innerHTML = res.diff?.trim()
          ? lines
              .map((l) => {
                const c = l.startsWith("+") ? "diff-add" : l.startsWith("-") ? "diff-del" : l.startsWith("@@") ? "diff-hunk" : "";
                return `<div class="${c}">${escapeHtml(l)}</div>`;
              })
              .join("")
          : `<div class="muted">No uncommitted changes in ${escapeHtml(STATE.activeEditorPath)}.</div>`;
      }
    }
  }

  function activateIdeTab(path) {
    const tab = STATE.openTabs.find((t) => t.path === path);
    if (!tab) return;
    STATE.activeEditorPath = tab.path;
    if ($("editor-textarea")) $("editor-textarea").value = tab.content;
    renderIdeTabs();
    syncIdeHighlight();
  }

  async function openIdeFile(path) {
    if (!path) return;
    const found = STATE.openTabs.find((t) => t.path === path);
    if (found) {
      activateIdeTab(path);
      return;
    }
    try {
      const data = await apiFetch(`/api/files/read?path=${encodeURIComponent(path)}`);
      STATE.openTabs.push({
        path: data.path || path,
        content: data.content || "",
        savedContent: data.content || "",
        dirty: false,
        language: data.language || detectLanguage(path),
      });
      activateIdeTab(data.path || path);
    } catch (err) {
      showToast(err.message);
    }
  }

  async function refreshIdeFileTree() {
    const box = $("ide-file-tree");
    if (!box) return;
    try {
      const data = await apiFetch("/api/files/tree?max_depth=4&limit=400");
      box.innerHTML = "";
      function build(nodes, depth) {
        nodes.forEach((n) => {
          const row = document.createElement("div");
          if (n.type === "dir") {
            const open = STATE.expandedFolders.has(n.path) || depth === 0;
            row.className = "file-tree-dir";
            row.style.paddingLeft = `${8 + depth * 12}px`;
            row.textContent = `${open ? "▾" : "▸"} ${n.name}`;
            row.addEventListener("click", () => {
              if (STATE.expandedFolders.has(n.path)) STATE.expandedFolders.delete(n.path);
              else STATE.expandedFolders.add(n.path);
              refreshIdeFileTree();
            });
            box.appendChild(row);
            if (open && Array.isArray(n.children)) build(n.children, depth + 1);
          } else {
            row.className = `file-tree-item ${STATE.activeEditorPath === n.path ? "active" : ""}`;
            row.style.paddingLeft = `${18 + depth * 12}px`;
            row.textContent = n.name;
            row.addEventListener("click", () => openIdeFile(n.path));
            box.appendChild(row);
          }
        });
      }
      if (Array.isArray(data.tree)) build(data.tree, 0);
    } catch {
      // Ignore
    }
  }

  async function refreshIdeChangedFiles() {
    try {
      const data = await apiFetch("/api/files/changed");
      const files = data.files || [];
      if ($("ide-git-changed")) {
        $("ide-git-changed").innerHTML = files.length
          ? files
              .map((f) => `<div class="file-tree-item" data-scm-open="${escapeHtml(f.path)}">${escapeHtml(f.path)} (${escapeHtml(f.status || "M")})</div>`)
              .join("")
          : `<div class="muted-placeholder">Working tree clean.</div>`;
        qsa("[data-scm-open]", $("ide-git-changed")).forEach((el) =>
          el.addEventListener("click", async () => {
            await openIdeFile(el.getAttribute("data-scm-open"));
            setIdeEditorMode("diff");
          })
        );
      }
    } catch {
      // Ignore
    }
  }

  async function refreshProblems() {
    try {
      const data = await apiFetch("/api/problems");
      const probs = data.problems || [];
      STATE.problems = probs;
      if ($("problems-count-badge")) $("problems-count-badge").textContent = String(probs.length);
      if ($("bottom-problems-list")) {
        $("bottom-problems-list").innerHTML = probs.length
          ? probs.map((p) => `<div class="clean-row" style="cursor:pointer" data-pr="${escapeHtml(p.file)}"><span>${escapeHtml(p.message)}</span><span class="muted">${escapeHtml(p.file)}:${p.line || 1}</span></div>`).join("")
          : `<div class="muted-placeholder">No problems.</div>`;
        qsa("[data-pr]", $("bottom-problems-list")).forEach((r) =>
          r.addEventListener("click", () => openIdeFile(r.getAttribute("data-pr")))
        );
      }
    } catch {
      // Ignore
    }
  }

  async function refreshLogs() {
    try {
      const data = await apiFetch("/api/logs?limit=60");
      const rows = (data.audit_logs || []).slice(-40).reverse();
      if ($("bottom-logs-list")) {
        $("bottom-logs-list").innerHTML = rows.length
          ? rows.map((l) => `<div>${escapeHtml(l.actor || "system")}: ${escapeHtml(l.action || "")}</div>`).join("")
          : `<div class="muted-placeholder">No logs yet.</div>`;
      }
    } catch {
      // Ignore
    }
  }

  /* -------------------- Modals & Event Wiring -------------------- */
  function openModal(modalId) {
    $("modal-backdrop")?.classList.remove("hidden");
    qsa(".clean-modal").forEach((m) => m.classList.toggle("hidden", m.id !== modalId));
    if (modalId === "modal-skills") refreshSkillsModal();
    if (modalId === "modal-settings") {
      refreshProviders();
      refreshMemoryModal();
    }
    if (modalId === "modal-connect-ai") {
      refreshRegistry().then(() => {
        renderWizardProviderList();
        renderWizardDetailPane();
      });
    }
  }

  async function refreshSkillsModal() {
    const box = $("skills-list-output");
    if (!box) return;
    try {
      const data = await apiFetch("/api/advanced/skills");
      const skills = data.skills || [];
      box.innerHTML = skills.length
        ? skills
            .map(
              (s) => `
              <div class="clean-row">
                <div><strong>${escapeHtml(s.name)}</strong> <span class="muted">${escapeHtml(s.description || "")}</span></div>
                <button type="button" class="btn btn-xs" data-exec-sk="${escapeHtml(s.name)}">Run</button>
              </div>`
            )
            .join("")
        : `<div class="muted-placeholder">No forged skills yet.</div>`;
      qsa("[data-exec-sk]", box).forEach((b) =>
        b.addEventListener("click", async () => {
          const name = b.getAttribute("data-exec-sk");
          await apiFetch("/api/advanced/skills", { method: "POST", body: JSON.stringify({ action: "execute", name }) });
          showToast(`Executed skill "${name}"`);
        })
      );
    } catch {
      // Ignore
    }
  }

  async function refreshMemoryModal() {
    try {
      const data = await apiFetch("/api/memory");
      if ($("memory-list-output")) {
        $("memory-list-output").innerHTML = (data.memories || [])
          .slice(0, 8)
          .map((m) => `<div class="clean-row"><strong>${escapeHtml(m.title)}</strong><span class="muted">${escapeHtml(m.content)}</span></div>`)
          .join("");
      }
    } catch {
      // Ignore
    }
  }

  function bindEvents() {
    $("btn-open-ide")?.addEventListener("click", () => setWorkspaceMode(STATE.mode === "ide" ? "ai" : "ide"));
    $("btn-toggle-sidebar")?.addEventListener("click", toggleSidebar);
    $("drawer-backdrop")?.addEventListener("click", () => {
      $("left-panel")?.classList.remove("drawer-open");
      $("drawer-backdrop")?.classList.add("hidden");
    });

    $("btn-new-conversation")?.addEventListener("click", startNewConversation);
    $("btn-add-project")?.addEventListener("click", () => {
      $("project-open-inline")?.classList.toggle("hidden");
      $("workspace-path-input")?.focus();
    });
    $("btn-refresh-projects")?.addEventListener("click", refreshProjectAndSessions);
    $("btn-select-workspace")?.addEventListener("click", () => switchWorkspacePath($("workspace-path-input")?.value));
    $("composer-project-select")?.addEventListener("change", (e) => switchWorkspacePath(e.target.value));

    // Topbar Search & Popover
    $("btn-topbar-search")?.addEventListener("click", () => {
      $("quick-command-popover")?.classList.toggle("hidden");
      $("topbar-quick-search")?.focus();
    });
    $("btn-close-popover")?.addEventListener("click", () => $("quick-command-popover")?.classList.add("hidden"));
    $("btn-quick-search-go")?.addEventListener("click", async () => {
      const q = $("topbar-quick-search")?.value.trim();
      if (!q) return;
      const res = await apiFetch(`/api/files/search?q=${encodeURIComponent(q)}&limit=25`);
      if ($("popover-results")) {
        $("popover-results").innerHTML = (res.matches || []).length
          ? res.matches
              .map((m) => `<div class="clean-row" style="cursor:pointer" data-pop-file="${escapeHtml(m.path)}">${escapeHtml(m.path)}:${m.line} — ${escapeHtml(m.preview || "")}</div>`)
              .join("")
          : `<div class="muted-placeholder">No matches found.</div>`;
        qsa("[data-pop-file]", $("popover-results")).forEach((el) =>
          el.addEventListener("click", () => {
            $("quick-command-popover")?.classList.add("hidden");
            setWorkspaceMode("ide");
            openIdeFile(el.getAttribute("data-pop-file"));
          })
        );
      }
    });
    $("topbar-quick-search")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("btn-quick-search-go")?.click();
    });
    $("btn-more-menu")?.addEventListener("click", () => openModal("modal-settings"));

    // Modals
    qsa("[data-modal-view]").forEach((b) => b.addEventListener("click", () => openModal(b.getAttribute("data-modal-view"))));
    qsa(".modal-close-btn").forEach((b) =>
      b.addEventListener("click", () => $("modal-backdrop")?.classList.add("hidden"))
    );
    $("modal-backdrop")?.addEventListener("click", (e) => {
      if (e.target.id === "modal-backdrop") $("modal-backdrop")?.classList.add("hidden");
    });

    // AI Composer & Voice
    $("btn-execute-task")?.addEventListener("click", () => submitPrompt($("task-composer-input")?.value));
    $("task-composer-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submitPrompt(e.target.value);
      }
    });
    $("btn-stop-execution")?.addEventListener("click", async () => {
      STATE.activeExecutionController?.abort();
      if (STATE.activeSessionId) {
        await apiFetch(`/api/opencode/sessions/${encodeURIComponent(STATE.activeSessionId)}/abort`, { method: "POST" }).catch(() => {});
      }
    });
    $("btn-open-attach")?.addEventListener("click", () => $("attach-menu")?.classList.toggle("hidden"));
    $("btn-attach-file")?.addEventListener("click", () => {
      const tab = STATE.openTabs.find((t) => t.path === STATE.activeEditorPath);
      if (!tab) return showToast("Open a file in IDE first to attach it.");
      addAttachment("file", tab.path, tab.content.slice(0, 6000));
    });
    $("btn-attach-diff")?.addEventListener("click", async () => {
      const d = await apiFetch("/api/git/diff");
      if (!d.diff) return showToast("No uncommitted git diff.");
      addAttachment("diff", "git-diff", d.diff.slice(0, 6000));
    });
    $("btn-attach-problems")?.addEventListener("click", async () => {
      await refreshProblems();
      if (!STATE.problems.length) return showToast("No workspace errors.");
      addAttachment("problems", `Errors (${STATE.problems.length})`, JSON.stringify(STATE.problems.slice(0, 15)));
    });
    $("btn-voice-input")?.addEventListener("click", () => {
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR) return showToast("Voice recognition is not supported in this browser.");
      if (STATE.isRecording && STATE.recognition) {
        STATE.recognition.stop();
        return;
      }
      const rec = new SR();
      STATE.recognition = rec;
      STATE.isRecording = true;
      $("btn-voice-input")?.classList.add("recording");
      rec.onresult = (ev) => {
        const text = ev.results?.[0]?.[0]?.transcript || "";
        if ($("task-composer-input")) $("task-composer-input").value += (text ? " " + text : "");
      };
      rec.onend = () => {
        STATE.isRecording = false;
        $("btn-voice-input")?.classList.remove("recording");
      };
      rec.start();
    });

    // Conversation actions & approvals
    $("btn-conv-diff")?.addEventListener("click", async () => {
      setWorkspaceMode("ide");
      await refreshIdeChangedFiles();
    });
    $("btn-conv-revert")?.addEventListener("click", async () => {
      if (!STATE.activeSessionId) return;
      await apiFetch(`/api/opencode/sessions/${encodeURIComponent(STATE.activeSessionId)}/revert`, { method: "POST" });
      showToast("Reverted last turn.");
      $("btn-conv-revert")?.classList.add("hidden");
    });
    $("btn-approve-inline")?.addEventListener("click", async () => {
      const p = STATE.pendingApprovals[0];
      if (!p) return;
      await apiFetch(`/api/security/approvals/${encodeURIComponent(p.id)}/resolve`, {
        method: "POST",
        body: JSON.stringify({ approved: true }),
      });
      refreshApprovals();
    });
    $("btn-deny-inline")?.addEventListener("click", async () => {
      const p = STATE.pendingApprovals[0];
      if (!p) return;
      await apiFetch(`/api/security/approvals/${encodeURIComponent(p.id)}/resolve`, {
        method: "POST",
        body: JSON.stringify({ approved: false }),
      });
      refreshApprovals();
    });

    // IDE internal wiring
    qsa(".ide-rail-btn").forEach((btn) =>
      btn.addEventListener("click", () => {
        const tab = btn.getAttribute("data-ide-tab");
        qsa(".ide-rail-btn").forEach((b) => b.classList.toggle("active", b === btn));
        qsa(".ide-pane").forEach((p) => {
          const match = p.id === `ide-pane-${tab}`;
          p.classList.toggle("active", match);
          p.classList.toggle("hidden", !match);
        });
        if (tab === "git") refreshIdeChangedFiles();
      })
    );
    $("btn-ide-refresh-tree")?.addEventListener("click", refreshIdeFileTree);
    $("btn-ide-new-file")?.addEventListener("click", () => $("ide-new-file-bar")?.classList.toggle("hidden"));
    $("btn-ide-create-confirm")?.addEventListener("click", async () => {
      const path = $("ide-new-file-input")?.value.trim();
      if (!path) return;
      await apiFetch("/api/files/write", { method: "POST", body: JSON.stringify({ path, content: "" }) });
      $("ide-new-file-bar")?.classList.add("hidden");
      $("ide-new-file-input").value = "";
      await refreshIdeFileTree();
      openIdeFile(path);
    });
    $("btn-ide-search-go")?.addEventListener("click", async () => {
      const q = $("ide-search-input")?.value.trim();
      if (!q) return;
      const res = await apiFetch(`/api/files/search?q=${encodeURIComponent(q)}&limit=30`);
      if ($("ide-search-results")) {
        $("ide-search-results").innerHTML = (res.matches || [])
          .map((m) => `<div class="file-tree-item" data-sr-file="${escapeHtml(m.path)}">${escapeHtml(m.path)}:${m.line}</div>`)
          .join("");
        qsa("[data-sr-file]", $("ide-search-results")).forEach((el) =>
          el.addEventListener("click", () => openIdeFile(el.getAttribute("data-sr-file")))
        );
      }
    });
    $("btn-git-create-checkpoint")?.addEventListener("click", async () => {
      const label = $("git-checkpoint-label")?.value.trim() || "bel-code-checkpoint";
      await apiFetch("/api/git/checkpoint", { method: "POST", body: JSON.stringify({ label }) });
      showToast("Checkpoint created.");
      refreshIdeChangedFiles();
    });
    $("btn-git-rollback")?.addEventListener("click", async () => {
      await apiFetch("/api/git/rollback", { method: "POST", body: JSON.stringify({}) });
      showToast("Rolled back checkpoint.");
      refreshIdeChangedFiles();
    });

    const ta = $("editor-textarea");
    if (ta) {
      ta.addEventListener("input", () => {
        const t = STATE.openTabs.find((x) => x.path === STATE.activeEditorPath);
        if (t) {
          t.content = ta.value;
          t.dirty = t.content !== t.savedContent;
          renderIdeTabs();
        }
        syncIdeHighlight();
      });
      ta.addEventListener("scroll", () => {
        if ($("editor-highlight-layer")) {
          $("editor-highlight-layer").scrollTop = ta.scrollTop;
          $("editor-highlight-layer").scrollLeft = ta.scrollLeft;
        }
        if ($("editor-line-gutter")) $("editor-line-gutter").scrollTop = ta.scrollTop;
      });
    }
    $("btn-editor-mode-code")?.addEventListener("click", () => setIdeEditorMode("code"));
    $("btn-editor-mode-diff")?.addEventListener("click", () => setIdeEditorMode("diff"));
    $("btn-save-file")?.addEventListener("click", async () => {
      const tab = STATE.openTabs.find((t) => t.path === STATE.activeEditorPath);
      if (!tab) return;
      await apiFetch("/api/files/write", { method: "POST", body: JSON.stringify({ path: tab.path, content: tab.content }) });
      tab.savedContent = tab.content;
      tab.dirty = false;
      renderIdeTabs();
      showToast(`Saved ${tab.path}`);
    });
    $("btn-ide-toggle-bottom")?.addEventListener("click", () => $("ide-bottom-panel")?.classList.toggle("hidden"));
    qsa(".bottom-tab").forEach((b) =>
      b.addEventListener("click", () => {
        const name = b.getAttribute("data-bottom-tab");
        qsa(".bottom-tab").forEach((x) => x.classList.toggle("active", x === b));
        qsa(".bottom-tab-pane").forEach((p) => p.classList.toggle("active", p.id === `bottom-tab-${name}`));
        if (name === "problems") refreshProblems();
        if (name === "logs") refreshLogs();
      })
    );
    $("btn-run-terminal")?.addEventListener("click", async () => {
      const inp = $("terminal-cmd-input");
      const out = $("terminal-output-box");
      const cmd = inp?.value.trim();
      if (!cmd || !out) return;
      inp.value = "";
      out.textContent += `\n$ ${cmd}\n`;
      const res = await apiFetch("/api/terminal/execute", { method: "POST", body: JSON.stringify({ command: cmd }) });
      out.textContent += `${[res.stdout, res.stderr].filter(Boolean).join("\n")}\n[exit ${res.exit_code ?? 0}]\n`;
    });
    $("terminal-cmd-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("btn-run-terminal")?.click();
    });
    $("btn-run-qa-tests")?.addEventListener("click", async () => {
      const res = await apiFetch("/api/qa/tests", { method: "POST", body: JSON.stringify({}) });
      if ($("debug-console-output")) {
        $("debug-console-output").textContent = `[exit ${res.exit_code ?? "?"}]\n${[res.stdout, res.stderr].filter(Boolean).join("\n")}`;
      }
    });
    $("btn-ide-ai-send")?.addEventListener("click", () => {
      const val = $("ide-ai-prompt")?.value;
      if (!val) return;
      $("ide-ai-prompt").value = "";
      submitPrompt(val);
    });

    // Settings, Skills & Benchmarks inside modals
    $("btn-forge-skill-submit")?.addEventListener("click", async () => {
      const name = $("forge-skill-name")?.value.trim();
      const description = $("forge-skill-desc")?.value.trim();
      if (!name) return;
      await apiFetch("/api/advanced/skills", {
        method: "POST",
        body: JSON.stringify({ action: "register", name, description, steps: ["Inspect", "Execute", "Verify"] }),
      });
      $("forge-skill-name").value = "";
      $("forge-skill-desc").value = "";
      refreshSkillsModal();
    });
    qsa(".adv-tool-btn").forEach((b) =>
      b.addEventListener("click", async () => {
        const tool = b.getAttribute("data-tool");
        const res = await apiFetch(`/api/advanced/${encodeURIComponent(tool)}`, {
          method: "POST",
          body: JSON.stringify({ workspace_path: STATE.workspacePath }),
        });
        if ($("advanced-tool-output")) {
          $("advanced-tool-output").innerHTML = `<div class="clean-row"><strong>${escapeHtml(tool)}</strong><span>${escapeHtml(
            res.summary || res.message || "Completed"
          )}</span></div>`;
        }
      })
    );
    $("btn-run-all-benchmarks")?.addEventListener("click", async () => {
      const res = await apiFetch("/api/benchmarks/run", { method: "POST", body: JSON.stringify({}) });
      if ($("benchmarks-output")) {
        $("benchmarks-output").innerHTML = (res.results || [])
          .map((r) => `<div class="clean-row"><span>${escapeHtml(r.id || r.name)}</span><span>${r.passed ? "PASS" : "FAIL"}</span></div>`)
          .join("");
      }
    });
    $("provider-id-select")?.addEventListener("change", syncProviderFormFields);
    $("btn-settings-open-wizard")?.addEventListener("click", () => openConnectAiWizard("free_cloud"));
    $("btn-onboard-free-cloud")?.addEventListener("click", () => openConnectAiWizard("free_cloud", "gemini"));
    $("btn-onboard-custom-api")?.addEventListener("click", () => openConnectAiWizard("custom", "custom"));
    $("btn-onboard-local-ai")?.addEventListener("click", () => openConnectAiWizard("local", "ollama"));
    $("btn-reopen-connect-ai")?.addEventListener("click", () => openConnectAiWizard("free_cloud"));
    $("btn-skip-onboarding")?.addEventListener("click", async () => {
      await apiFetch("/api/onboarding/complete", { method: "POST", body: JSON.stringify({ completed: true }) }).catch(() => {});
      refreshOnboardingStatus();
    });

    qsa("[data-wizard-tab]").forEach((btn) =>
      btn.addEventListener("click", () => {
        STATE.wizardTab = btn.getAttribute("data-wizard-tab") || "free_cloud";
        qsa("[data-wizard-tab]").forEach((b) => b.classList.toggle("active", b === btn));
        const filtered = _filterWizardProviders(STATE.wizardTab);
        if (filtered.length) STATE.wizardSelectedId = filtered[0].provider_id;
        renderWizardProviderList();
        renderWizardDetailPane();
      })
    );

    $("btn-wizard-toggle-key")?.addEventListener("click", () => {
      const inp = $("wizard-api-key-input");
      if (!inp) return;
      const show = inp.type === "password";
      inp.type = show ? "text" : "password";
      $("btn-wizard-toggle-key").textContent = show ? "Hide" : "Show";
    });

    $("btn-wizard-fetch-models")?.addEventListener("click", async () => {
      const pid = STATE.wizardSelectedId;
      const out = $("wizard-status-output");
      if (!pid) return;
      if (out) out.textContent = "Fetching all available models for key...";
      try {
        const rawKey = $("wizard-api-key-input")?.value.trim();
        const rawUrl = $("wizard-base-url-input")?.value.trim();
        const acctId = $("wizard-account-id-input")?.value.trim();
        if (rawKey || rawUrl || acctId) {
          await apiFetch("/api/providers/configure", {
            method: "POST",
            body: JSON.stringify({
              provider_id: pid,
              api_key: rawKey || undefined,
              base_url: rawUrl || undefined,
              account_id: acctId || undefined,
              label: $("wizard-key-label-input")?.value.trim() || "default",
            }),
          });
          if ($("wizard-api-key-input") && rawKey) $("wizard-api-key-input").value = "";
        }
        const discovered = await fetchModelsForWizardProvider(pid);
        await Promise.all([refreshRegistry(), refreshProviders()]);
        renderWizardProviderList();
        if (out) out.textContent = `Loaded ${discovered.length} model(s) for ${pid}.`;
      } catch (err) {
        if (out) out.textContent = err.message || "Could not fetch models.";
      }
    });

    $("btn-wizard-test-save")?.addEventListener("click", async () => {
      const pid = STATE.wizardSelectedId;
      const out = $("wizard-status-output");
      if (!pid) return;
      if (out) out.textContent = "Saving & testing connection...";
      try {
        const role = $("wizard-role-select")?.value || "primary";
        const payload = {
          provider_id: pid,
          api_key: $("wizard-api-key-input")?.value.trim() || undefined,
          base_url: $("wizard-base-url-input")?.value.trim() || undefined,
          default_model: $("wizard-model-select")?.value || undefined,
          account_id: $("wizard-account-id-input")?.value.trim() || undefined,
          label: $("wizard-key-label-input")?.value.trim() || "default",
          role,
          set_as_primary: role === "primary",
          set_as_secondary: role === "secondary",
          set_as_fallback: role === "fallback",
          enabled: true,
        };
        await apiFetch("/api/providers/configure", { method: "POST", body: JSON.stringify(payload) });
        if ($("wizard-api-key-input")) $("wizard-api-key-input").value = "";
        const testRes = await apiFetch(`/api/providers/${encodeURIComponent(pid)}/test`, {
          method: "POST",
          body: JSON.stringify({ live_network: true }),
        });
        if (testRes.ok && Array.isArray(testRes.models) && testRes.models.length) {
          populateWizardModelSelect(testRes.models, payload.default_model);
        } else if (testRes.ok) {
          await fetchModelsForWizardProvider(pid);
        }
        await Promise.all([refreshRegistry(), refreshProviders(), refreshOnboardingStatus()]);
        renderWizardProviderList();
        if (out) out.textContent = testRes.message || (testRes.ok ? "Connected and ready!" : "Connection failed.");
      } catch (err) {
        if (out) out.textContent = err.message || "Error testing connection.";
      }
    });

    $("btn-wizard-finish")?.addEventListener("click", async () => {
      await apiFetch("/api/onboarding/complete", { method: "POST", body: JSON.stringify({ completed: true }) }).catch(() => {});
      await Promise.all([refreshProviders(), refreshOnboardingStatus()]);
      $("modal-backdrop")?.classList.add("hidden");
      $("task-composer-input")?.focus();
      showToast("AI setup ready. Ask BEL CODE to build something!");
    });

    $("btn-register-custom-provider")?.addEventListener("click", async () => {
      const out = $("wizard-status-output");
      try {
        const res = await apiFetch("/api/providers/custom", {
          method: "POST",
          body: JSON.stringify({
            name: $("custom-provider-name")?.value.trim() || "Custom Endpoint",
            endpoint_mode: $("custom-provider-mode")?.value || "openai",
            base_url: $("custom-provider-url")?.value.trim(),
            default_model: $("custom-provider-model")?.value.trim(),
            api_key: $("custom-provider-key")?.value.trim() || undefined,
            label: $("custom-provider-label")?.value.trim() || "default",
            set_as_primary: Boolean($("custom-provider-primary")?.checked),
          }),
        });
        if ($("custom-provider-key")) $("custom-provider-key").value = "";
        await Promise.all([refreshRegistry(), refreshProviders(), refreshOnboardingStatus()]);
        renderWizardProviderList();
        if (out) out.textContent = `Registered custom provider '${res.provider_id}'.`;
      } catch (err) {
        if (out) out.textContent = err.message || "Failed to register custom provider.";
      }
    });

    $("btn-save-provider")?.addEventListener("click", async () => {
      const provider_id = $("provider-id-select")?.value;
      const api_key = $("provider-api-key")?.value.trim();
      const base_url = $("provider-base-url")?.value.trim();
      const default_model = $("provider-default-model")?.value.trim();
      const label = $("provider-account-label")?.value.trim() || "default";
      const role = $("provider-role-select")?.value || "";
      await apiFetch("/api/providers/configure", {
        method: "POST",
        body: JSON.stringify({
          provider_id,
          api_key: api_key || undefined,
          base_url: base_url || undefined,
          default_model: default_model || undefined,
          label,
          role: role || undefined,
          set_as_primary: role === "primary",
          set_as_secondary: role === "secondary",
          set_as_fallback: role === "fallback",
        }),
      });
      if ($("provider-api-key")) $("provider-api-key").value = "";
      if ($("provider-action-output")) $("provider-action-output").textContent = `Saved ${provider_id} (${label}).`;
      await Promise.all([refreshProviders(), refreshOnboardingStatus()]);
    });
    $("provider-models-discovered-select")?.addEventListener("change", (e) => {
      if ($("provider-default-model") && e.target.value) {
        $("provider-default-model").value = e.target.value;
      }
    });
    $("btn-test-provider")?.addEventListener("click", async () => {
      const pid = $("provider-id-select")?.value;
      const res = await apiFetch(`/api/providers/${encodeURIComponent(pid)}/test`, {
        method: "POST",
        body: JSON.stringify({ live_network: true }),
      });
      if ($("provider-action-output")) {
        $("provider-action-output").textContent = res.message || (res.ok ? "Connected" : "Failed");
      }
      if (res.ok) $("btn-discover-models")?.click();
    });
    $("btn-discover-models")?.addEventListener("click", async () => {
      const pid = $("provider-id-select")?.value;
      const res = await apiFetch(`/api/providers/${encodeURIComponent(pid)}/models`);
      const models = res.models || [];
      if ($("provider-action-output")) $("provider-action-output").textContent = `${models.length} models available`;
      const dSel = $("provider-models-discovered-select");
      if (dSel) {
        dSel.innerHTML = "";
        models.forEach((m) => {
          const mid = typeof m === "string" ? m : m.id || m.name || "";
          if (!mid) return;
          const opt = document.createElement("option");
          opt.value = mid;
          opt.textContent = m && m.is_free ? `${mid} (free)` : mid;
          dSel.appendChild(opt);
        });
        $("provider-discovered-models-row")?.classList.toggle("hidden", !models.length);
      }
      await refreshProviders();
    });
    $("btn-save-routing-budget")?.addEventListener("click", async () => {
      await apiFetch("/api/providers/routing", {
        method: "POST",
        body: JSON.stringify({
          per_task_usd: Number($("budget-per-task")?.value || 2),
          per_project_usd: Number($("budget-per-project")?.value || 10),
          global_usd: Number($("budget-global")?.value || 25),
        }),
      });
      showToast("Budget saved.");
    });
    $("btn-memory-save")?.addEventListener("click", async () => {
      const title = $("memory-title")?.value.trim();
      const content = $("memory-content")?.value.trim();
      if (!title || !content) return;
      await apiFetch("/api/memory", { method: "POST", body: JSON.stringify({ title, content, category: "architecture" }) });
      $("memory-title").value = "";
      $("memory-content").value = "";
      refreshMemoryModal();
    });

    // Global keyboard shortcuts
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        toggleSidebar();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "p") {
        e.preventDefault();
        $("quick-command-popover")?.classList.remove("hidden");
        $("topbar-quick-search")?.focus();
      }
    });
  }

  async function boot() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      if (saved.sidebarCollapsed) {
        STATE.sidebarCollapsed = true;
        $("left-panel")?.classList.add("collapsed");
      }
    } catch {
      // Ignore
    }
    bindEvents();
    setWorkspaceMode("ai");

    await Promise.all([
      refreshRuntimeStatus(),
      refreshProjectAndSessions(),
      refreshProviders(),
      refreshRegistry(),
      refreshOnboardingStatus(),
      refreshApprovals(),
    ]);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
