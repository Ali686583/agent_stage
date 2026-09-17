// Espace collaboratif IA (page2.html) : sidebar (projets/discussions),
// composer (modele/documents/actions), rendu des reponses riches, profil.
// Toute l'identite affichee vient du backend (/api/auth/me, /api/workspace/*) ;
// rien n'est invente cote client.
(function () {
  "use strict";

  const API = "/api/workspace";

  const state = {
    user: null,
    conversationId: null,
    conversationTitle: "",
    model: "chatgpt",
    attachments: [], // {fileId, name, status: 'uploading'|'uploaded'|'failed'}
    sourceResultIds: [],
    selectedAction: null,
    actionsCatalog: [],
    projects: [],
    projectConversations: {}, // projectId -> {items, cursor, open}
    discussionsCursor: null,
    discussionsItems: [],
    sending: false,
    sidebarCollapsed: false,
  };

  // ---------------------------------------------------------------------
  // Aides generiques
  // ---------------------------------------------------------------------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key in attrs) {
        if (key === "class") node.className = attrs[key];
        else if (key === "text") node.textContent = attrs[key];
        else if (key.startsWith("on") && typeof attrs[key] === "function") {
          node.addEventListener(key.slice(2), attrs[key]);
        } else node.setAttribute(key, attrs[key]);
      }
    }
    (children || []).forEach((child) => {
      if (child == null) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function t(key) {
    return window.AgentStageI18N ? window.AgentStageI18N.t(key) : key;
  }

  function formatDate(iso) {
    try {
      const date = new Date(iso);
      return date.toLocaleString(document.documentElement.lang || "fr", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (error) {
      return "";
    }
  }

  async function api(path, options) {
    const response = await fetch(API + path, {
      credentials: "include",
      headers: options && options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
      ...options,
    });
    let data = {};
    try {
      data = await response.json();
    } catch (error) {
      data = {};
    }
    return { ok: response.ok, status: response.status, data };
  }

  // ---------------------------------------------------------------------
  // DOM references
  // ---------------------------------------------------------------------

  const dom = {};

  function cacheDom() {
    dom.sidebar = document.getElementById("ws-sidebar");
    dom.sidebarNotch = document.getElementById("ws-sidebar-notch");
    dom.mobileToggle = document.getElementById("ws-mobile-toggle");
    dom.backdrop = document.getElementById("ws-backdrop");
    dom.projectsSection = document.getElementById("ws-projects-section");
    dom.projectsHeader = document.getElementById("ws-projects-header");
    dom.projectsList = document.getElementById("ws-projects-list");
    dom.newProjectBtn = document.getElementById("ws-new-project-btn");
    dom.projectForm = document.getElementById("ws-project-form");
    dom.projectNameInput = document.getElementById("ws-project-name-input");
    dom.projectCancelBtn = document.getElementById("ws-project-cancel-btn");
    dom.projectCreateBtn = document.getElementById("ws-project-create-btn");
    dom.discussionsList = document.getElementById("ws-discussions-list");
    dom.newDiscussionBtn = document.getElementById("ws-new-discussion-btn");
    dom.conversationArea = document.getElementById("ws-conversation-area");
    dom.composer = document.getElementById("ws-composer");
    dom.composerTextarea = document.getElementById("ws-textarea");
    dom.fileChips = document.getElementById("ws-file-chips");
    dom.fileInput = document.getElementById("ws-file-input");
    dom.attachBtn = document.getElementById("ws-attach-btn");
    dom.sendBtn = document.getElementById("ws-send-btn");
    dom.modelButtons = Array.from(document.querySelectorAll(".model-selector button"));
    dom.actionsBtn = document.getElementById("ws-actions-btn");
    dom.actionsMenu = document.getElementById("ws-actions-menu");
    dom.profileTrigger = document.getElementById("ws-profile-trigger");
    dom.profileMenu = document.getElementById("ws-profile-menu");
    dom.profileName = document.getElementById("ws-profile-name");
    dom.profileMenuName = document.getElementById("ws-profile-menu-name");
    dom.profileMenuEmail = document.getElementById("ws-profile-menu-email");
    dom.profileAvatar = document.getElementById("ws-profile-avatar");
    dom.logoutBtn = document.getElementById("ws-logout-btn");
  }

  // ---------------------------------------------------------------------
  // Auth / profil
  // ---------------------------------------------------------------------

  async function loadUser() {
    const response = await fetch("/api/auth/me", { credentials: "include" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      window.location.href = "login.html";
      return false;
    }
    state.user = data.user;
    const initial = (state.user.username || "?").trim().charAt(0).toUpperCase();
    dom.profileAvatar.textContent = initial;
    dom.profileName.textContent = state.user.username;
    dom.profileMenuName.textContent = state.user.username;
    dom.profileMenuEmail.textContent = state.user.email;
    return true;
  }

  function wireProfileMenu() {
    dom.profileTrigger.addEventListener("click", (event) => {
      event.stopPropagation();
      dom.profileMenu.classList.toggle("open");
    });
    document.addEventListener("click", () => dom.profileMenu.classList.remove("open"));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") dom.profileMenu.classList.remove("open");
    });
    dom.logoutBtn.addEventListener("click", async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      window.location.href = "login.html";
    });
  }

  // ---------------------------------------------------------------------
  // Sidebar : ouverture / fermeture
  // ---------------------------------------------------------------------

  function initSidebarToggle() {
    try {
      state.sidebarCollapsed = localStorage.getItem("agentStageSidebarCollapsed") === "true";
    } catch (error) {
      state.sidebarCollapsed = false;
    }
    applySidebarState();

    dom.sidebarNotch.addEventListener("click", () => {
      state.sidebarCollapsed = !state.sidebarCollapsed;
      try {
        localStorage.setItem("agentStageSidebarCollapsed", String(state.sidebarCollapsed));
      } catch (error) {
        /* navigation privee : pas grave */
      }
      applySidebarState();
    });

    dom.mobileToggle.addEventListener("click", () => {
      dom.sidebar.classList.toggle("mobile-open");
      dom.backdrop.classList.toggle("visible");
    });
    dom.backdrop.addEventListener("click", () => {
      dom.sidebar.classList.remove("mobile-open");
      dom.backdrop.classList.remove("visible");
    });
  }

  function applySidebarState() {
    dom.sidebar.classList.toggle("collapsed", state.sidebarCollapsed);
    dom.sidebarNotch.setAttribute(
      "aria-label",
      t(state.sidebarCollapsed ? "workspace.sidebar_open" : "workspace.sidebar_close")
    );
  }

  // ---------------------------------------------------------------------
  // Projets
  // ---------------------------------------------------------------------

  function toggleSection(section) {
    section.classList.toggle("open");
  }

  async function loadProjects() {
    const { ok, data } = await api("/projects");
    if (ok && data.ok) {
      state.projects = data.projects;
      renderProjects();
    }
  }

  function renderProjects() {
    dom.projectsList.innerHTML = "";
    if (!state.projects.length) {
      dom.projectsList.appendChild(el("div", { class: "sidebar-empty", "data-i18n": "workspace.no_projects", text: t("workspace.no_projects") }));
      return;
    }
    state.projects.forEach((project) => {
      const group = el("div", { class: "sidebar-project-group" });
      const header = el("div", { class: "sidebar-project-name" }, [
        el("span", { class: "section-chevron" }, [chevronIcon()]),
        el("span", { text: project.name }),
      ]);
      const list = el("div", { class: "sidebar-list hidden" });
      let loaded = false;
      header.addEventListener("click", async () => {
        const willOpen = list.classList.contains("hidden");
        list.classList.toggle("hidden");
        header.parentElement.classList.toggle("open", willOpen);
        if (willOpen && !loaded) {
          loaded = true;
          await loadProjectConversations(project.id, list);
        }
      });
      group.appendChild(header);
      group.appendChild(list);
      dom.projectsList.appendChild(group);
    });
  }

  async function loadProjectConversations(projectId, listEl) {
    const { ok, data } = await api(`/projects/${projectId}/conversations?limit=30`);
    listEl.innerHTML = "";
    if (!ok || !data.ok) return;
    if (!data.conversations.length) {
      listEl.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.empty_history") }));
      return;
    }
    data.conversations.forEach((conversation) => {
      listEl.appendChild(renderConversationItem(conversation));
    });
  }

  function chevronIcon() {
    const wrapper = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    wrapper.setAttribute("width", "12");
    wrapper.setAttribute("height", "12");
    wrapper.setAttribute("viewBox", "0 0 24 24");
    wrapper.setAttribute("fill", "none");
    wrapper.setAttribute("stroke", "currentColor");
    wrapper.setAttribute("stroke-width", "2.5");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M9 18l6-6-6-6");
    wrapper.appendChild(path);
    return wrapper;
  }

  async function submitNewProject() {
    const name = dom.projectNameInput.value.trim();
    if (!name) return;
    const { ok, data } = await api("/projects", { method: "POST", body: JSON.stringify({ name }) });
    if (ok && data.ok) {
      dom.projectNameInput.value = "";
      dom.projectForm.classList.add("hidden");
      await loadProjects();
    }
  }

  // ---------------------------------------------------------------------
  // Discussions (historique partage)
  // ---------------------------------------------------------------------

  async function loadDiscussions(reset) {
    if (reset) {
      state.discussionsItems = [];
      state.discussionsCursor = null;
      dom.discussionsList.innerHTML = "";
    }
    const params = new URLSearchParams({ limit: "20" });
    if (state.discussionsCursor) params.set("before", state.discussionsCursor);
    const { ok, data } = await api(`/conversations?${params.toString()}`);
    if (!ok || !data.ok) return;

    const loadMoreBtn = dom.discussionsList.querySelector(".sidebar-load-more");
    if (loadMoreBtn) loadMoreBtn.remove();

    if (!data.conversations.length && !state.discussionsItems.length) {
      dom.discussionsList.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.empty_history") }));
      return;
    }

    data.conversations.forEach((conversation) => {
      state.discussionsItems.push(conversation);
      dom.discussionsList.appendChild(renderConversationItem(conversation));
    });

    if (data.conversations.length === 20) {
      state.discussionsCursor = data.conversations[data.conversations.length - 1].updatedAt;
      const btn = el("button", {
        class: "sidebar-load-more",
        text: t("workspace.load_more"),
        onclick: () => loadDiscussions(false),
      });
      dom.discussionsList.appendChild(btn);
    }
  }

  function renderConversationItem(conversation) {
    const item = el("div", { class: "sidebar-item", "data-conversation-id": conversation.id });
    if (conversation.id === state.conversationId) item.classList.add("active");
    const main = el("div", { class: "sidebar-item-main" }, [
      el("div", { class: "sidebar-item-title", text: conversation.title || t("workspace.new_discussion") }),
      el("div", { class: "sidebar-item-meta", text: `${conversation.createdByName} • ${formatDate(conversation.updatedAt)}` }),
    ]);
    const menuBtn = el("button", {
      class: "sidebar-item-menu-btn",
      type: "button",
      "aria-label": t("workspace.conversation_menu"),
      text: "⋯",
      onclick: (event) => {
        event.stopPropagation();
        openConversationMenu(conversation, menuBtn);
      },
    });
    item.appendChild(main);
    item.appendChild(menuBtn);
    item.addEventListener("click", () => openConversation(conversation.id));
    return item;
  }

  let activeContextMenu = null;

  function openConversationMenu(conversation, anchorBtn) {
    closeContextMenu();
    const menu = el("div", { class: "item-context-menu open" });
    menu.appendChild(
      el("button", {
        type: "button",
        text: t("workspace.add_to_project"),
        disabled: "disabled",
        style: "font-weight:600;cursor:default;",
      })
    );
    if (!state.projects.length) {
      menu.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_projects") }));
    } else {
      state.projects.forEach((project) => {
        menu.appendChild(
          el("button", {
            type: "button",
            text: project.name,
            onclick: async (event) => {
              event.stopPropagation();
              await api(`/conversations/${conversation.id}/projects`, {
                method: "POST",
                body: JSON.stringify({ projectId: project.id }),
              });
              closeContextMenu();
              await loadProjects();
            },
          })
        );
      });
    }
    anchorBtn.parentElement.appendChild(menu);
    activeContextMenu = menu;
    setTimeout(() => {
      document.addEventListener("click", closeContextMenu, { once: true });
    }, 0);
  }

  function closeContextMenu() {
    if (activeContextMenu && activeContextMenu.parentElement) {
      activeContextMenu.parentElement.removeChild(activeContextMenu);
    }
    activeContextMenu = null;
  }

  // ---------------------------------------------------------------------
  // Conversation courante
  // ---------------------------------------------------------------------

  function startNewDiscussion() {
    state.conversationId = null;
    state.conversationTitle = "";
    resetComposer();
    renderInitialQuestion();
    document.querySelectorAll(".sidebar-item.active").forEach((n) => n.classList.remove("active"));
  }

  async function openConversation(conversationId) {
    const { ok, data } = await api(`/conversations/${conversationId}?limit=100`);
    if (!ok || !data.ok) return;
    state.conversationId = conversationId;
    state.conversationTitle = data.conversation.title;
    dom.conversationArea.innerHTML = "";
    data.messages.forEach((message) => renderMessage(message));
    scrollToBottom();
    document.querySelectorAll(".sidebar-item").forEach((n) => {
      n.classList.toggle("active", n.getAttribute("data-conversation-id") === conversationId);
    });
    dom.sidebar.classList.remove("mobile-open");
    dom.backdrop.classList.remove("visible");
  }

  function renderInitialQuestion() {
    dom.conversationArea.innerHTML = "";
    const heading = el("div", { class: "initial-question" });
    const span = el("span", {});
    const cursor = el("span", { class: "typewriter-cursor" });
    heading.appendChild(span);
    heading.appendChild(cursor);
    dom.conversationArea.appendChild(heading);
    typewrite(span, t("workspace.initial_question"), cursor);
  }

  function typewrite(target, text, cursor) {
    const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      target.textContent = text;
      if (cursor) cursor.remove();
      return;
    }
    let i = 0;
    const interval = setInterval(() => {
      i += 1;
      target.textContent = text.slice(0, i);
      if (i >= text.length) {
        clearInterval(interval);
        if (cursor) setTimeout(() => cursor.remove(), 400);
      }
    }, 28);
  }

  function scrollToBottom() {
    dom.conversationArea.scrollTop = dom.conversationArea.scrollHeight;
  }

  // ---------------------------------------------------------------------
  // Rendu d'un message + reponses riches
  // ---------------------------------------------------------------------

  function renderMessage(message) {
    const row = el("div", { class: `message-row ${message.role}` });
    const bubble = el("div", { class: "message-bubble" });
    if (message.role === "assistant") {
      bubble.appendChild(el("div", { class: "message-author", text: message.authorName }));
    }
    if (message.blocks && message.blocks.length) {
      renderBlocks(bubble, message.blocks);
    } else if (message.content) {
      bubble.appendChild(el("div", { class: "block-markdown", html: null, text: message.content }));
    }
    if (message.attachments && message.attachments.length) {
      const attWrap = el("div", { class: "message-attachments" });
      message.attachments.forEach((file) => attWrap.appendChild(fileChipReadOnly(file)));
      bubble.appendChild(attWrap);
    }
    row.appendChild(bubble);
    dom.conversationArea.appendChild(row);
    return row;
  }

  function fileChipReadOnly(file) {
    return el(
      "a",
      { class: "file-chip", href: `${API}/files/${file.id}`, target: "_blank", rel: "noopener" },
      [el("span", { class: "file-chip-name", text: `📎 ${file.name}` })]
    );
  }

  const RICH_RENDERERS = {
    markdown: renderMarkdownBlock,
    text: renderMarkdownBlock,
    callout: renderCalloutBlock,
    code: renderCodeBlock,
    table: renderTableBlock,
    chart: renderChartBlock,
    image: renderImageBlock,
    file: renderFileBlock,
    link: renderLinkBlock,
  };

  function renderBlocks(container, blocks) {
    blocks.forEach((block) => {
      const renderer = RICH_RENDERERS[block && block.type];
      const wrap = el("div", { class: "rich-block" });
      try {
        if (renderer) {
          wrap.appendChild(renderer(block));
        } else {
          wrap.appendChild(el("div", { class: "block-unknown", text: `[bloc non supporte: ${block && block.type}]` }));
        }
      } catch (error) {
        wrap.appendChild(el("div", { class: "block-unknown", text: "[erreur d'affichage de ce bloc]" }));
      }
      container.appendChild(wrap);
    });
  }

  function markdownLiteToHtml(content) {
    let html = escapeHtml(content || "");
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/\n/g, "<br>");
    return html;
  }

  function renderMarkdownBlock(block) {
    const div = el("div", { class: "block-markdown" });
    div.innerHTML = markdownLiteToHtml(block.content || "");
    return div;
  }

  function renderCalloutBlock(block) {
    const div = el("div", { class: "block-callout" });
    div.innerHTML = markdownLiteToHtml(block.content || "");
    return div;
  }

  function renderCodeBlock(block) {
    const pre = el("pre", { class: "block-code" });
    const code = el("code", { text: block.content || "" });
    if (block.language) code.setAttribute("data-language", block.language);
    pre.appendChild(code);
    return pre;
  }

  function renderTableBlock(block) {
    const wrap = el("div", { class: "block-table-wrap" });
    const table = el("table", {});
    if (Array.isArray(block.headers)) {
      const thead = el("thead", {});
      const tr = el("tr", {});
      block.headers.forEach((headerText) => tr.appendChild(el("th", { text: headerText })));
      thead.appendChild(tr);
      table.appendChild(thead);
    }
    const tbody = el("tbody", {});
    (block.rows || []).forEach((row) => {
      const tr = el("tr", {});
      row.forEach((cell) => tr.appendChild(el("td", { text: cell })));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function renderImageBlock(block) {
    const src = block.url || (block.fileId ? `${API}/files/${block.fileId}` : "");
    return el("img", { src, alt: block.alt || "", style: "max-width:100%;border-radius:var(--radius-medium);" });
  }

  function renderFileBlock(block) {
    const href = block.url || (block.fileId ? `${API}/files/${block.fileId}` : "#");
    return el("a", { class: "block-file", href, target: "_blank", rel: "noopener" }, [
      el("span", { text: "📎" }),
      el("span", { text: block.name || "Fichier" }),
    ]);
  }

  function renderLinkBlock(block) {
    return el("a", { href: block.url || "#", target: "_blank", rel: "noopener", text: block.label || block.url || "" });
  }

  let chartCounter = 0;

  function renderChartBlock(block) {
    const wrap = el("div", { class: "block-chart-wrap" });
    if (block.title) wrap.appendChild(el("div", { class: "block-chart-title", text: block.title }));
    chartCounter += 1;
    const canvas = el("canvas", { id: `ws-chart-${chartCounter}`, height: "220" });
    wrap.appendChild(canvas);

    if (!window.Chart) {
      wrap.appendChild(el("div", { class: "block-unknown", text: "[Chart.js non charge]" }));
      return wrap;
    }

    const chartTypeMap = { line: "line", area: "line", bar: "bar", histogram: "bar", pie: "pie", donut: "doughnut" };
    const chartType = chartTypeMap[block.chartType] || "line";
    const series = block.series || [];
    const labels = (series[0] && series[0].data ? series[0].data : []).map((point) => point.x);
    const palette = ["#6DB831", "#0F6722", "#d97706", "#2563eb", "#9333ea", "#dc2626"];
    const datasets = series.map((serie, index) => ({
      label: serie.name,
      data: (serie.data || []).map((point) => point.y),
      borderColor: palette[index % palette.length],
      backgroundColor:
        chartType === "line" && block.chartType === "area"
          ? palette[index % palette.length] + "33"
          : palette[index % palette.length],
      fill: block.chartType === "area",
      tension: 0.25,
    }));

    // Chart.js est charge de facon asynchrone (CDN) ; on differe la creation
    // au prochain tick pour etre sur que le canvas est bien dans le DOM.
    setTimeout(() => {
      try {
        new window.Chart(canvas.getContext("2d"), {
          type: chartType,
          data: { labels, datasets },
          options: {
            responsive: true,
            plugins: { legend: { display: series.length > 1 } },
            scales:
              chartType === "pie" || chartType === "doughnut"
                ? {}
                : {
                    x: { title: { display: !!(block.xAxis && block.xAxis.label), text: block.xAxis && block.xAxis.label } },
                    y: { title: { display: !!(block.yAxis && block.yAxis.label), text: block.yAxis && block.yAxis.label } },
                  },
          },
        });
      } catch (error) {
        /* rendu impossible : le fallback textuel [bloc non supporte] n'est
           pas declenche ici, mais l'erreur ne casse pas le reste de la page */
      }
    }, 0);

    return wrap;
  }

  // ---------------------------------------------------------------------
  // Composer : modele, actions, fichiers, drag & drop, envoi
  // ---------------------------------------------------------------------

  function resetComposer() {
    dom.composerTextarea.value = "";
    autoResize();
    state.attachments = [];
    state.sourceResultIds = [];
    state.selectedAction = null;
    renderFileChips();
  }

  function autoResize() {
    dom.composerTextarea.style.height = "auto";
    dom.composerTextarea.style.height = Math.min(dom.composerTextarea.scrollHeight, 220) + "px";
  }

  function wireModelSelector() {
    dom.modelButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        state.model = btn.getAttribute("data-model");
        dom.modelButtons.forEach((b) => b.classList.toggle("active", b === btn));
        dom.sendBtn.classList.toggle("model-claude", state.model === "claude");
      });
    });
  }

  async function loadActions() {
    const { ok, data } = await api("/actions");
    if (ok && data.ok) {
      state.actionsCatalog = data.actions;
      renderActionsMenu();
    }
  }

  function renderActionsMenu() {
    dom.actionsMenu.innerHTML = "";
    state.actionsCatalog.forEach((actionId) => {
      dom.actionsMenu.appendChild(
        el("button", {
          type: "button",
          text: t(`workspace.action_${actionId}`),
          onclick: (event) => {
            event.stopPropagation();
            state.selectedAction = actionId;
            dom.actionsMenu.classList.remove("open");
            dom.actionsBtn.textContent = `${t("workspace.actions_label")}: ${t(`workspace.action_${actionId}`)}`;
          },
        })
      );
    });
  }

  function wireActionsMenu() {
    dom.actionsBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      dom.actionsMenu.classList.toggle("open");
    });
    document.addEventListener("click", () => dom.actionsMenu.classList.remove("open"));
  }

  function renderFileChips() {
    dom.fileChips.innerHTML = "";
    state.attachments.forEach((attachment) => {
      const chip = el("div", { class: `file-chip ${attachment.status === "failed" ? "failed" : ""}` }, [
        el("span", { class: "file-chip-name", text: `📎 ${attachment.name}${attachment.status === "uploading" ? " (" + t("workspace.uploading") + ")" : ""}` }),
        el("button", {
          type: "button",
          "aria-label": t("workspace.remove"),
          text: "×",
          onclick: () => {
            state.attachments = state.attachments.filter((a) => a !== attachment);
            renderFileChips();
          },
        }),
      ]);
      dom.fileChips.appendChild(chip);
    });
  }

  async function handleFiles(fileList) {
    for (const file of Array.from(fileList)) {
      const attachment = { name: file.name, status: "uploading", fileId: null };
      state.attachments.push(attachment);
      renderFileChips();
      const formData = new FormData();
      formData.append("file", file);
      const { ok, data } = await api("/files", { method: "POST", body: formData });
      if (ok && data.ok) {
        attachment.status = "uploaded";
        attachment.fileId = data.file.id;
      } else {
        attachment.status = "failed";
      }
      renderFileChips();
    }
  }

  function wireFileUpload() {
    dom.attachBtn.addEventListener("click", () => dom.fileInput.click());
    dom.fileInput.addEventListener("change", (event) => {
      if (event.target.files.length) handleFiles(event.target.files);
      dom.fileInput.value = "";
    });

    ["dragenter", "dragover"].forEach((eventName) => {
      dom.composer.addEventListener(eventName, (event) => {
        event.preventDefault();
        dom.composer.classList.add("drag-over");
      });
    });
    ["dragleave", "drop"].forEach((eventName) => {
      dom.composer.addEventListener(eventName, (event) => {
        event.preventDefault();
        if (eventName === "dragleave" && event.target !== dom.composer) return;
        dom.composer.classList.remove("drag-over");
      });
    });
    dom.composer.addEventListener("drop", (event) => {
      if (event.dataTransfer && event.dataTransfer.files.length) {
        handleFiles(event.dataTransfer.files);
      }
    });
  }

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return "id-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  async function sendMessage() {
    if (state.sending) return;
    const text = dom.composerTextarea.value.trim();
    const readyAttachments = state.attachments.filter((a) => a.status === "uploaded");
    const stillUploading = state.attachments.some((a) => a.status === "uploading");

    if (!text && !readyAttachments.length) {
      showComposerError(t("workspace.error_empty_message"));
      return;
    }
    if (stillUploading) return; // le bouton est deja desactive dans ce cas

    state.sending = true;
    updateSendButtonState();

    if (!state.conversationId) {
      dom.conversationArea.innerHTML = "";
    }

    const userMessageRow = renderMessage({
      role: "user",
      authorName: state.user.username,
      content: text,
      blocks: null,
      attachments: readyAttachments.map((a) => ({ id: a.fileId, name: a.name })),
    });
    scrollToBottom();

    const loadingRow = el("div", { class: "message-row assistant" }, [
      el("div", { class: "message-bubble" }, [
        el("div", { class: "loader-dots" }, [el("span"), el("span"), el("span")]),
      ]),
    ]);
    dom.conversationArea.appendChild(loadingRow);
    scrollToBottom();

    const requestId = uuid();
    const payload = {
      conversationId: state.conversationId,
      model: state.model,
      message: text,
      fileIds: readyAttachments.map((a) => a.fileId),
      sourceResultIds: state.sourceResultIds,
      requestId,
    };
    if (state.selectedAction) {
      payload.action = { id: state.selectedAction, parameters: {} };
    }

    const composerSnapshot = { text, attachments: state.attachments.slice(), action: state.selectedAction };
    resetComposer();

    const { ok, data } = await api("/messages", { method: "POST", body: JSON.stringify(payload) });

    loadingRow.remove();
    state.sending = false;
    updateSendButtonState();

    if (!ok || !data.ok) {
      const statusMap = { 504: "workspace.error_timeout", 503: "workspace.error_not_configured" };
      const message = statusMap[data.status] ? t(statusMap[data.status]) : data.error ? data.error : t("workspace.error_generic");
      const statusRow = el("div", { class: "message-status error" }, [
        el("span", { text: message }),
        el("button", {
          type: "button",
          text: t("workspace.retry"),
          onclick: () => {
            statusRow.remove();
            state.attachments = composerSnapshot.attachments;
            state.selectedAction = composerSnapshot.action;
            dom.composerTextarea.value = composerSnapshot.text;
            renderFileChips();
            sendMessage();
          },
        }),
      ]);
      userMessageRow.appendChild(statusRow);
      scrollToBottom();
      return;
    }

    if (data.isNewConversation) {
      state.conversationId = data.conversationId;
      state.conversationTitle = data.conversationTitle;
      loadDiscussions(true);
    } else {
      loadDiscussions(true);
    }

    renderMessage(data.assistantMessage);
    scrollToBottom();
  }

  function showComposerError(message) {
    dom.composerTextarea.setAttribute("placeholder", message);
    setTimeout(() => dom.composerTextarea.setAttribute("placeholder", t("workspace.entry_placeholder")), 2200);
  }

  function updateSendButtonState() {
    dom.sendBtn.disabled = state.sending;
  }

  function wireComposer() {
    dom.composerTextarea.addEventListener("input", autoResize);
    dom.composerTextarea.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });
    dom.sendBtn.addEventListener("click", sendMessage);
  }

  // ---------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------

  async function init() {
    cacheDom();
    const authed = await loadUser();
    if (!authed) return;

    wireProfileMenu();
    initSidebarToggle();
    wireModelSelector();
    wireActionsMenu();
    wireFileUpload();
    wireComposer();

    dom.newProjectBtn.addEventListener("click", () => dom.projectForm.classList.toggle("hidden"));
    dom.projectCancelBtn.addEventListener("click", () => {
      dom.projectForm.classList.add("hidden");
      dom.projectNameInput.value = "";
    });
    dom.projectCreateBtn.addEventListener("click", submitNewProject);
    dom.projectNameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") submitNewProject();
    });

    dom.projectsHeader.addEventListener("click", () => toggleSection(dom.projectsSection));
    dom.newDiscussionBtn.addEventListener("click", startNewDiscussion);

    await Promise.all([loadProjects(), loadDiscussions(true), loadActions()]);

    renderInitialQuestion();

    document.addEventListener("agentstage:langchange", () => {
      dom.sendBtn.setAttribute("aria-label", t("workspace.send"));
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
