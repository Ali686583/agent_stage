// Espace collaboratif IA (page2.html) : sidebar (projets/discussions),
// composer (modele/documents/actions), rendu des reponses riches, profil.
// Toute l'identite affichee vient du backend (/api/auth/me, /api/workspace/*) ;
// rien n'est invente cote client.
(function () {
  "use strict";

  const API = "/api/workspace";

  // Garde-fou cote client pour une requete en attente d'une reponse
  // asynchrone (Phase 4) : un peu plus que le timeout serveur (90s appel
  // n8n + 30s marge du job) pour ne jamais se declencher avant une reponse
  // legitime mais lente.
  const PENDING_REQUEST_TIMEOUT_MS = 130000;

  const state = {
    user: null,
    conversationId: null,
    conversationTitle: "",
    model: "chatgpt",
    attachments: [], // {fileId, name, status: 'uploading'|'uploaded'|'failed'}
    sourceResultIds: [],
    selectedWidgetIndex: null,
    // Banque de boutons/actions (Phase 5) : chargee depuis le backend
    // (Postgres-jg_R via /api/workspace/entry-actions), jamais codee en dur.
    entryActions: [],
    // Banque de connexions plateformes/API (bouton a gauche du trombone) :
    // chargee depuis /api/workspace/connections. La selection (quelles
    // connexions sont cochees) est un etat de la session en cours, pas
    // reinitialise a chaque envoi (contrairement aux pieces jointes) :
    // cocher "API Finance" une fois reste coche pour les messages suivants.
    connections: [],
    selectedConnectionIds: new Set(),
    projects: [],
    discussionsCursor: null,
    sending: false,
    sidebarCollapsed: false,
    // Temps reel (Phase 3)
    eventSource: null,
    seenMessageIds: new Set(),
    lastSeenSeq: 0,
    typingUsers: new Map(), // userId -> { displayName, avatarUrl, timeoutId }
    lastTypingPingAt: 0,
    pendingRequests: new Map(), // requestId -> { loadingRow, showSendError } (Phase 4, reponses asynchrones)
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
      return new Date(iso).toLocaleString(document.documentElement.lang || "fr", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (error) {
      return "";
    }
  }

  function avatarNode(className, avatarUrl, fallbackText) {
    if (avatarUrl) {
      return el("img", { class: className, src: avatarUrl, alt: "" });
    }
    const span = el("span", { class: className });
    span.textContent = (fallbackText || "?").trim().charAt(0).toUpperCase();
    return span;
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

  async function authApi(path, options) {
    const response = await fetch(`/api/auth${path}`, {
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
    dom.sidebarColumn = document.getElementById("ws-sidebar-column");
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
    dom.discussionsSection = document.getElementById("ws-discussions-section");
    dom.discussionsHeader = document.getElementById("ws-discussions-header");
    dom.discussionsList = document.getElementById("ws-discussions-list");
    dom.newDiscussionBtn = document.getElementById("ws-new-discussion-btn");
    dom.sharedDiscussionsSection = document.getElementById("ws-shared-discussions-section");
    dom.sharedDiscussionsHeader = document.getElementById("ws-shared-discussions-header");
    dom.sharedDiscussionsList = document.getElementById("ws-shared-discussions-list");
    dom.newSharedDiscussionBtn = document.getElementById("ws-new-shared-discussion-btn");
    dom.sharedDiscussionForm = document.getElementById("ws-shared-discussion-form");
    dom.sharedDiscussionNameInput = document.getElementById("ws-shared-discussion-name-input");
    dom.sharedDiscussionCancelBtn = document.getElementById("ws-shared-discussion-cancel-btn");
    dom.sharedDiscussionCreateBtn = document.getElementById("ws-shared-discussion-create-btn");
    dom.centralColumn = document.getElementById("ws-central-column");
    dom.conversationArea = document.getElementById("ws-conversation-area");
    dom.composer = document.getElementById("ws-composer");
    dom.composerTextarea = document.getElementById("ws-textarea");
    dom.fileChips = document.getElementById("ws-file-chips");
    dom.fileInput = document.getElementById("ws-file-input");
    dom.attachBtn = document.getElementById("ws-attach-btn");
    dom.sendBtn = document.getElementById("ws-send-btn");
    dom.modelButtons = Array.from(document.querySelectorAll(".model-selector button"));
    dom.actionWidgets = document.getElementById("ws-action-widgets");
    dom.addActionBtn = document.getElementById("ws-add-action-btn");
    dom.connectionsBtn = document.getElementById("ws-connections-btn");
    dom.connectionsBadge = document.getElementById("ws-connections-badge");
    dom.profileTrigger = document.getElementById("ws-profile-trigger");
    dom.profileMenu = document.getElementById("ws-profile-menu");
    dom.profileName = document.getElementById("ws-profile-name");
    dom.profileAvatar = document.getElementById("ws-profile-avatar");
    dom.profileMenuAvatar = document.getElementById("ws-profile-menu-avatar");
    dom.profileMenuName = document.getElementById("ws-profile-menu-name");
    dom.profileMenuEmail = document.getElementById("ws-profile-menu-email");
    dom.logoutBtn = document.getElementById("ws-logout-btn");
    dom.moreOptionsBtn = document.getElementById("ws-more-options-btn");
    dom.moreOptionsPanel = document.getElementById("ws-more-options-panel");
    dom.deleteAccountPanel = document.getElementById("ws-delete-account-panel");
    dom.optAvatar = document.getElementById("ws-opt-avatar");
    dom.optNickname = document.getElementById("ws-opt-nickname");
    dom.optDeleteAccount = document.getElementById("ws-opt-delete-account");
    dom.avatarInput = document.getElementById("ws-avatar-input");
    dom.nicknameForm = document.getElementById("ws-nickname-form");
    dom.nicknameInput = document.getElementById("ws-nickname-input");
    dom.nicknameCancel = document.getElementById("ws-nickname-cancel");
    dom.nicknameSave = document.getElementById("ws-nickname-save");
    dom.nicknameRemove = document.getElementById("ws-nickname-remove");
    dom.modalOverlay = document.getElementById("ws-modal-overlay");
    dom.modalMessage = document.getElementById("ws-modal-message");
    dom.modalCancel = document.getElementById("ws-modal-cancel");
    dom.modalConfirm = document.getElementById("ws-modal-confirm");
  }

  // ---------------------------------------------------------------------
  // Modale de confirmation generique
  // ---------------------------------------------------------------------

  let modalConfirmHandler = null;
  let modalLastFocused = null;

  function showConfirmModal(message, onConfirm) {
    dom.modalMessage.textContent = message;
    modalConfirmHandler = onConfirm;
    modalLastFocused = document.activeElement;
    dom.modalOverlay.classList.remove("hidden");
    dom.modalConfirm.focus();
  }

  function hideConfirmModal() {
    dom.modalOverlay.classList.add("hidden");
    modalConfirmHandler = null;
    if (modalLastFocused && modalLastFocused.focus) modalLastFocused.focus();
  }

  function wireModal() {
    dom.modalCancel.addEventListener("click", hideConfirmModal);
    dom.modalOverlay.addEventListener("click", (event) => {
      if (event.target === dom.modalOverlay) hideConfirmModal();
    });
    dom.modalConfirm.addEventListener("click", () => {
      const handler = modalConfirmHandler;
      hideConfirmModal();
      if (handler) handler();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !dom.modalOverlay.classList.contains("hidden")) {
        hideConfirmModal();
      }
    });
  }

  // ---------------------------------------------------------------------
  // Auth / profil
  // ---------------------------------------------------------------------

  function applyUserToUI() {
    const user = state.user;
    dom.profileAvatar.replaceWith((dom.profileAvatar = avatarNode("avatar", user.avatarUrl, user.displayName)));
    dom.profileAvatar.id = "ws-profile-avatar";
    dom.profileName.textContent = user.displayName;
    dom.profileMenuAvatar.replaceWith(
      (dom.profileMenuAvatar = avatarNode("avatar avatar-lg", user.avatarUrl, user.displayName))
    );
    dom.profileMenuAvatar.id = "ws-profile-menu-avatar";
    dom.profileMenuName.textContent = user.displayName;
    dom.profileMenuEmail.textContent = user.email;
    dom.profileMenuEmail.title = user.email;
  }

  async function loadUser() {
    const response = await fetch("/api/auth/me", { credentials: "include" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      window.location.href = "login.html";
      return false;
    }
    state.user = data.user;
    applyUserToUI();
    return true;
  }

  function wireProfileMenu() {
    dom.profileTrigger.addEventListener("click", (event) => {
      event.stopPropagation();
      dom.profileMenu.classList.toggle("open");
    });
    document.addEventListener("click", () => {
      dom.profileMenu.classList.remove("open");
      dom.moreOptionsPanel.classList.add("hidden");
      dom.deleteAccountPanel.classList.add("hidden");
      dom.nicknameForm.classList.add("hidden");
    });
    dom.profileMenu.addEventListener("click", (event) => event.stopPropagation());

    dom.logoutBtn.addEventListener("click", async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      window.location.href = "login.html";
    });

    dom.moreOptionsBtn.addEventListener("click", () => {
      // "Plus d'options" affiche/masque tout le groupe d'un coup : la
      // section avatar/pseudonyme ET "Supprimer le compte" tout en bas,
      // separes uniquement pour eviter un conflit CSS entre eux (voir
      // commentaire dans workspace.css).
      const willOpen = dom.moreOptionsPanel.classList.contains("hidden");
      dom.moreOptionsPanel.classList.toggle("hidden", !willOpen);
      dom.deleteAccountPanel.classList.toggle("hidden", !willOpen);
      dom.nicknameForm.classList.add("hidden");
    });

    dom.optAvatar.addEventListener("click", () => dom.avatarInput.click());
    dom.avatarInput.addEventListener("change", async (event) => {
      const file = event.target.files[0];
      event.target.value = "";
      if (!file) return;
      const formData = new FormData();
      formData.append("file", file);
      const { ok, data } = await authApi("/avatar", { method: "POST", body: formData });
      if (ok && data.ok) {
        state.user = data.user;
        applyUserToUI();
      }
    });

    dom.optNickname.addEventListener("click", () => {
      dom.nicknameInput.value = state.user.displayName === state.user.email ? "" : state.user.displayName;
      dom.nicknameForm.classList.toggle("hidden");
    });
    dom.nicknameCancel.addEventListener("click", () => dom.nicknameForm.classList.add("hidden"));
    dom.nicknameSave.addEventListener("click", async () => {
      // Un champ laisse vide (ou uniquement des espaces) est envoye tel
      // quel : le backend le traite comme une reinitialisation du
      // pseudonyme (retour a l'email), ce n'est pas ignore ici.
      const value = dom.nicknameInput.value.trim();
      const { ok, data } = await authApi("/display-name", {
        method: "POST",
        body: JSON.stringify({ displayName: value }),
      });
      if (ok && data.ok) {
        state.user = data.user;
        applyUserToUI();
        dom.nicknameForm.classList.add("hidden");
        loadDiscussions(true);
      }
    });
    dom.nicknameRemove.addEventListener("click", async () => {
      const { ok, data } = await authApi("/display-name", { method: "DELETE" });
      if (ok && data.ok) {
        state.user = data.user;
        applyUserToUI();
        dom.nicknameForm.classList.add("hidden");
        loadDiscussions(true);
      }
    });

    dom.optDeleteAccount.addEventListener("click", () => {
      showConfirmModal(t("workspace.confirm_delete_account"), async () => {
        const { ok, data } = await authApi("/account", { method: "DELETE" });
        if (ok && data.ok) {
          window.location.href = "login.html";
        } else {
          showConfirmModal(t("workspace.error_generic"), null);
        }
      });
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
      dom.sidebarColumn.classList.toggle("mobile-open");
      dom.backdrop.classList.toggle("visible");
    });
    dom.backdrop.addEventListener("click", () => {
      dom.sidebarColumn.classList.remove("mobile-open");
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
      dom.projectsList.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_projects") }));
      return;
    }
    state.projects.forEach((project) => {
      const group = el("div", { class: "sidebar-project-group" });
      const header = el("div", { class: "sidebar-project-name" }, [
        el("span", { class: "section-chevron" }, [chevronIcon()]),
        el("span", { text: project.name, style: "flex:1;" }),
        el("button", {
          class: "sidebar-item-menu-btn forced-visible",
          type: "button",
          "aria-label": t("workspace.conversation_menu"),
          text: "⋯",
          onclick: (event) => {
            event.stopPropagation();
            openProjectMenu(project, header);
          },
        }),
      ]);
      const list = el("div", { class: "sidebar-list hidden" });
      let loaded = false;
      header.addEventListener("click", async () => {
        const willOpen = list.classList.contains("hidden");
        list.classList.toggle("hidden");
        group.classList.toggle("open", willOpen);
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
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "12");
    svg.setAttribute("height", "12");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2.5");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M9 18l6-6-6-6");
    svg.appendChild(path);
    return svg;
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

  function openProjectMenu(project, anchorEl) {
    closeContextMenu();
    const menu = el("div", { class: "item-context-menu open" });
    if (project.createdByUserId === state.user.id) {
      menu.appendChild(
        el("button", {
          type: "button",
          text: t("workspace.rename_project"),
          onclick: (event) => {
            event.stopPropagation();
            showRenameProjectForm(project, menu);
          },
        })
      );
      menu.appendChild(
        el("button", {
          type: "button",
          class: "danger-text",
          text: t("workspace.delete_project"),
          onclick: (event) => {
            event.stopPropagation();
            closeContextMenu();
            showConfirmModal(t("workspace.confirm_delete_project"), async () => {
              const { ok } = await api(`/projects/${project.id}`, { method: "DELETE" });
              if (ok) await loadProjects();
            });
          },
        })
      );
    } else {
      menu.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_permission") }));
    }
    anchorEl.appendChild(menu);
    activeContextMenu = menu;
    setTimeout(() => document.addEventListener("click", closeContextMenu, { once: true }), 0);
  }

  function showRenameProjectForm(project, menu) {
    // Le menu se ferme sur tout clic exterieur (voir openProjectMenu) : on
    // bloque la propagation ici pour qu'interagir avec le formulaire
    // (cliquer dans le champ, etc.) ne le fasse pas disparaitre.
    menu.onclick = (event) => event.stopPropagation();
    menu.innerHTML = "";
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.rename_project") }));
    const input = el("input", { type: "text", value: project.name });
    input.value = project.name;
    const form = el("div", { class: "project-create-form" }, [
      input,
      el("div", { class: "project-create-actions" }, [
        el("button", { type: "button", text: t("workspace.cancel"), onclick: (e) => { e.stopPropagation(); closeContextMenu(); } }),
        el("button", {
          type: "button",
          class: "primary",
          text: t("workspace.save"),
          onclick: async (event) => {
            event.stopPropagation();
            const name = input.value.trim();
            if (!name) return;
            const { ok } = await api(`/projects/${project.id}`, {
              method: "PATCH",
              body: JSON.stringify({ name }),
            });
            closeContextMenu();
            if (ok) await loadProjects();
          },
        }),
      ]),
    ]);
    menu.appendChild(form);
    input.focus();
    input.select();
  }

  // ---------------------------------------------------------------------
  // Discussions (historique partage)
  // ---------------------------------------------------------------------

  async function loadDiscussions(reset) {
    if (reset) {
      state.discussionsCursor = null;
      dom.discussionsList.innerHTML = "";
    }
    const params = new URLSearchParams({ limit: "20" });
    if (state.discussionsCursor) params.set("before", state.discussionsCursor);
    const { ok, data } = await api(`/conversations?${params.toString()}`);
    if (!ok || !data.ok) return;

    const loadMoreBtn = dom.discussionsList.querySelector(".sidebar-load-more");
    if (loadMoreBtn) loadMoreBtn.remove();

    if (!data.conversations.length && !dom.discussionsList.children.length) {
      dom.discussionsList.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.empty_history") }));
      return;
    }

    data.conversations.forEach((conversation) => {
      dom.discussionsList.appendChild(renderConversationItem(conversation));
    });

    if (data.conversations.length === 20) {
      state.discussionsCursor = data.conversations[data.conversations.length - 1].updatedAt;
      dom.discussionsList.appendChild(
        el("button", { class: "sidebar-load-more", text: t("workspace.load_more"), onclick: () => loadDiscussions(false) })
      );
    }
  }

  function renderConversationItem(conversation) {
    const item = el("div", { class: "sidebar-item", "data-conversation-id": conversation.id });
    if (conversation.id === state.conversationId) item.classList.add("active");
    const main = el("div", { class: "sidebar-item-main" }, [
      avatarNode("sidebar-item-avatar", conversation.createdByAvatarUrl, conversation.createdByName),
      el("div", { class: "sidebar-item-text" }, [
        el("div", { class: "sidebar-item-title", text: conversation.title || t("workspace.new_discussion") }),
        el("div", { class: "sidebar-item-meta", text: `${conversation.createdByName} • ${formatDate(conversation.updatedAt)}` }),
      ]),
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

  // Menu flottant independant (position: fixed), utilise pour la suppression
  // definitive dans "Choisir un bouton" : ce menu doit rester visible meme
  // si son bouton declencheur est dans .action-bank-results, qui a
  // overflow-y:auto et couperait un .item-context-menu classique (position:
  // absolute) ancre dans son propre flux. Coexiste avec activeContextMenu
  // (le menu "Choisir un bouton" lui-meme reste ouvert par-dessous).
  let activeFixedMenu = null;
  function closeFixedMenu() {
    if (activeFixedMenu && activeFixedMenu.parentElement) {
      activeFixedMenu.parentElement.removeChild(activeFixedMenu);
    }
    activeFixedMenu = null;
  }

  function openActionBankDeleteMenu(action, anchorBtn, onDeleted) {
    closeFixedMenu();
    const rect = anchorBtn.getBoundingClientRect();
    const menu = el("div", { class: "action-bank-fixed-menu" });
    menu.style.top = `${rect.bottom + 4}px`;
    menu.style.left = `${Math.max(8, rect.right - 200)}px`;
    menu.appendChild(
      el("button", {
        type: "button",
        class: "danger-text",
        text: t("workspace.delete_button_definitively"),
        onclick: async (event) => {
          event.stopPropagation();
          closeFixedMenu();
          // Suppression DEFINITIVE de la banque (pas juste "Retirer" de mon
          // interface) : le serveur refuse si cette action est encore
          // utilisee ailleurs (voir delete_action_bank_route / delete_action).
          const { ok, data } = await api(`/action-bank/${action.id}`, { method: "DELETE" });
          if (!ok || !data.ok) {
            showComposerError(data && data.error ? data.error : t("workspace.error_generic"));
            return;
          }
          if (onDeleted) onDeleted();
        },
      })
    );
    document.body.appendChild(menu);
    activeFixedMenu = menu;
    setTimeout(() => document.addEventListener("click", closeFixedMenu, { once: true }), 0);
  }

  function openConversationMenu(conversation, anchorBtn) {
    closeContextMenu();
    const menu = el("div", { class: "item-context-menu open" });
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.add_to_project") }));
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
    if (conversation.createdByUserId === state.user.id) {
      menu.appendChild(el("div", { class: "menu-divider" }));
      menu.appendChild(
        el("button", {
          type: "button",
          text: t("workspace.rename_conversation"),
          onclick: (event) => {
            event.stopPropagation();
            showRenameConversationForm(conversation, menu);
          },
        })
      );
      menu.appendChild(
        el("button", {
          type: "button",
          class: "danger-text",
          text: t("workspace.delete_conversation"),
          onclick: (event) => {
            event.stopPropagation();
            closeContextMenu();
            showConfirmModal(t("workspace.confirm_delete_conversation"), async () => {
              const { ok } = await api(`/conversations/${conversation.id}`, { method: "DELETE" });
              if (ok) {
                if (state.conversationId === conversation.id) startNewDiscussion();
                loadDiscussions(true);
              }
            });
          },
        })
      );
    }
    anchorBtn.parentElement.appendChild(menu);
    activeContextMenu = menu;
    setTimeout(() => document.addEventListener("click", closeContextMenu, { once: true }), 0);
  }

  function showRenameConversationForm(conversation, menu) {
    // Meme raison que showRenameProjectForm : bloquer la propagation pour
    // qu'interagir avec le formulaire ne ferme pas le menu.
    menu.onclick = (event) => event.stopPropagation();
    menu.innerHTML = "";
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.rename_conversation") }));
    const input = el("input", { type: "text", value: conversation.title || "" });
    input.value = conversation.title || "";
    const form = el("div", { class: "project-create-form" }, [
      input,
      el("div", { class: "project-create-actions" }, [
        el("button", { type: "button", text: t("workspace.cancel"), onclick: (e) => { e.stopPropagation(); closeContextMenu(); } }),
        el("button", {
          type: "button",
          class: "primary",
          text: t("workspace.save"),
          onclick: async (event) => {
            event.stopPropagation();
            const title = input.value.trim();
            if (!title) return;
            const { ok } = await api(`/conversations/${conversation.id}`, {
              method: "PATCH",
              body: JSON.stringify({ title }),
            });
            closeContextMenu();
            if (ok) {
              loadDiscussions(true);
              if (state.conversationId === conversation.id) state.conversationTitle = title;
            }
          },
        }),
      ]),
    ]);
    menu.appendChild(form);
    input.focus();
    input.select();
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
    disconnectRealtime();
    state.conversationId = null;
    state.conversationTitle = "";
    resetComposer();
    renderInitialQuestion();
    document.querySelectorAll(".sidebar-item.active").forEach((n) => n.classList.remove("active"));
  }

  // ---------------------------------------------------------------------
  // Discussions communes (session commune) : visibles et ouvrables par
  // tous les utilisateurs authentifies, sans invitation (voir is_participant
  // cote serveur, qui rejoint automatiquement quiconque ouvre l'une d'elles).
  // ---------------------------------------------------------------------

  async function loadSharedConversations() {
    const { ok, data } = await api("/conversations/shared");
    if (!ok || !data.ok) return;
    dom.sharedDiscussionsList.innerHTML = "";
    if (!data.conversations.length) {
      dom.sharedDiscussionsList.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_shared_discussions") }));
      return;
    }
    data.conversations.forEach((conversation) => {
      dom.sharedDiscussionsList.appendChild(renderSharedConversationItem(conversation));
    });
  }

  function renderSharedConversationItem(conversation) {
    const item = el("div", { class: "sidebar-item", "data-conversation-id": conversation.id });
    if (conversation.id === state.conversationId) item.classList.add("active");
    item.appendChild(
      el("div", { class: "sidebar-item-main" }, [
        avatarNode("sidebar-item-avatar", conversation.createdByAvatarUrl, conversation.createdByName),
        el("div", { class: "sidebar-item-text" }, [
          el("div", { class: "sidebar-item-title", text: conversation.title }),
          el("div", { class: "sidebar-item-meta", text: `${conversation.createdByName} • ${formatDate(conversation.updatedAt)}` }),
        ]),
      ])
    );
    item.addEventListener("click", async () => {
      await openConversation(conversation.id);
      document.querySelectorAll(".sidebar-item.active").forEach((n) => n.classList.remove("active"));
      item.classList.add("active");
    });
    return item;
  }

  async function submitNewSharedDiscussion() {
    const title = dom.sharedDiscussionNameInput.value.trim();
    if (!title) return;
    const { ok, data } = await api("/conversations/shared", { method: "POST", body: JSON.stringify({ title }) });
    if (!ok || !data.ok) return;
    dom.sharedDiscussionNameInput.value = "";
    dom.sharedDiscussionForm.classList.add("hidden");
    await loadSharedConversations();
    await openConversation(data.conversation.id);
    document.querySelectorAll(".sidebar-item.active").forEach((n) => n.classList.remove("active"));
    const newItem = dom.sharedDiscussionsList.querySelector(`[data-conversation-id="${data.conversation.id}"]`);
    if (newItem) newItem.classList.add("active");
  }

  async function openConversation(conversationId) {
    const { ok, data } = await api(`/conversations/${conversationId}?limit=100`);
    if (!ok || !data.ok) return;
    state.conversationId = conversationId;
    state.conversationTitle = data.conversation.title;
    dom.centralColumn.classList.remove("is-empty");
    dom.conversationArea.innerHTML = "";
    state.seenMessageIds = new Set();
    state.lastSeenSeq = 0;
    data.messages.forEach((message) => {
      state.seenMessageIds.add(message.id);
      if (message.seq) state.lastSeenSeq = Math.max(state.lastSeenSeq, message.seq);
      renderMessage(message);
    });
    scrollToBottom();
    document.querySelectorAll(".sidebar-item").forEach((n) => {
      n.classList.toggle("active", n.getAttribute("data-conversation-id") === conversationId);
    });
    dom.sidebarColumn.classList.remove("mobile-open");
    dom.backdrop.classList.remove("visible");
    connectRealtime(conversationId);
  }

  // ---------------------------------------------------------------------
  // Temps reel : SSE (nouveaux messages) + indicateurs "en train d'ecrire"
  // ---------------------------------------------------------------------

  function disconnectRealtime() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    state.typingUsers.forEach((entry) => clearTimeout(entry.timeoutId));
    state.typingUsers.clear();
    dom.conversationArea.querySelectorAll(".typing-row").forEach((n) => n.remove());
  }

  function connectRealtime(conversationId) {
    disconnectRealtime();
    if (!window.EventSource) return; // navigateur trop ancien : pas de temps reel, le reste marche quand meme
    const url = `${API}/events?conversationId=${encodeURIComponent(conversationId)}&after=${state.lastSeenSeq}`;
    const source = new EventSource(url, { withCredentials: true });

    source.addEventListener("message.created", (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      // Phase 4 : resout la requete en attente correspondante (retire son
      // loader) avant tout, qu'elle vienne de ce meme onglet ou d'un autre
      // (message.requestId n'est present que sur les reponses assistant
      // issues du worker en arriere-plan, voir librairies/jobs.py).
      if (message.requestId) {
        const pending = resolvePendingRequest(message.requestId);
        if (pending) pending.loadingRow.remove();
      }
      if (message.userId && state.user && message.userId === state.user.id) {
        // Notre propre message : deja affiche de maniere optimiste dans
        // sendMessage() des l'envoi. Le seul risque ici est une course ou
        // cet echo SSE arrive avant que la reponse HTTP du POST n'ait marque
        // le message comme vu (voir sendMessage) ; on se contente donc de
        // retenir son id/seq sans le re-rendre, pour ne jamais le dupliquer.
        state.seenMessageIds.add(message.id);
        if (message.seq) state.lastSeenSeq = Math.max(state.lastSeenSeq, message.seq);
        return;
      }
      if (state.seenMessageIds.has(message.id)) return; // deja vu (reconnexion, autre onglet, etc.)
      state.seenMessageIds.add(message.id);
      if (message.seq) state.lastSeenSeq = Math.max(state.lastSeenSeq, message.seq);
      // Retire l'indicateur typing de l'auteur (il vient d'envoyer) sans
      // redessiner tout de suite : renderMessage() doit inserer le message
      // AVANT les bulles typing restantes, qui doivent toujours rester en
      // bas de la conversation.
      if (message.userId) state.typingUsers.delete(message.userId);
      renderMessage(message);
      renderTypingRows();
      scrollToBottom();
    });

    source.addEventListener("typing", (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      if (!data.userId || data.userId === state.user.id) return; // jamais son propre indicateur
      upsertTypingIndicator(data.userId, data.displayName, data.avatarUrl);
    });

    source.addEventListener("workflow.failed", (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      const pending = data.requestId && resolvePendingRequest(data.requestId);
      if (!pending) return; // echec d'une requete d'un autre onglet/utilisateur : rien a faire ici
      const statusMap = { n8n_timeout: "workspace.error_timeout", workflow_not_configured: "workspace.error_not_configured" };
      pending.showSendError(t(statusMap[data.error] || "workspace.error_generic"));
    });

    state.eventSource = source;
  }

  function upsertTypingIndicator(userId, displayName, avatarUrl) {
    let entry = state.typingUsers.get(userId);
    if (entry) {
      clearTimeout(entry.timeoutId);
    } else {
      entry = { displayName, avatarUrl };
      state.typingUsers.set(userId, entry);
    }
    // Timeout automatique : si aucun nouvel evenement typing n'arrive avant
    // 4s (l'utilisateur s'est arrete ou a envoye), la bulle disparait.
    entry.timeoutId = setTimeout(() => removeTypingIndicator(userId), 4000);
    renderTypingRows();
  }

  function removeTypingIndicator(userId) {
    const entry = state.typingUsers.get(userId);
    if (!entry) return;
    clearTimeout(entry.timeoutId);
    state.typingUsers.delete(userId);
    renderTypingRows();
  }

  function renderTypingRows() {
    dom.conversationArea.querySelectorAll(".typing-row").forEach((n) => n.remove());
    state.typingUsers.forEach((entry) => {
      const row = el("div", { class: "message-row assistant typing-row" }, [
        el("div", { class: "message-bubble" }, [
          el("div", { class: "message-author" }, [
            avatarNode("message-author-avatar", entry.avatarUrl, entry.displayName),
            entry.displayName,
          ]),
          el("div", { class: "loader-dots" }, [el("span"), el("span"), el("span")]),
        ]),
      ]);
      dom.conversationArea.appendChild(row);
    });
    scrollToBottom();
  }

  function pingTyping() {
    if (!state.conversationId) return; // pas encore de conversation : rien a signaler
    const now = Date.now();
    if (now - state.lastTypingPingAt < 2500) return; // throttle cote client
    state.lastTypingPingAt = now;
    api(`/conversations/${state.conversationId}/typing`, { method: "POST" });
  }

  function renderInitialQuestion() {
    dom.centralColumn.classList.add("is-empty");
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
      bubble.appendChild(el("div", { class: "message-author" }, [message.authorName]));
    } else {
      bubble.appendChild(
        el("div", { class: "message-author" }, [
          avatarNode("message-author-avatar", message.authorAvatarUrl, message.authorName),
          message.authorName,
        ])
      );
    }
    if (message.blocks && message.blocks.length) {
      renderBlocks(bubble, message.blocks);
    } else if (message.content) {
      const textDiv = el("div", { class: "block-markdown" });
      textDiv.textContent = message.content;
      bubble.appendChild(textDiv);
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
    const nameSpan = el("span", { class: "file-chip-name", text: `📎 ${file.name}` });
    const nameLink = el(
      "a",
      { href: `${API}/files/${file.id}`, target: "_blank", rel: "noopener", style: "color:inherit;text-decoration:none;flex:1;min-width:0;" },
      [nameSpan]
    );
    const chip = el("div", { class: "file-chip", style: "position:relative;" }, [nameLink]);
    if (file.uploadedByUserId === state.user.id) {
      chip.appendChild(
        el("button", {
          type: "button",
          "aria-label": t("workspace.conversation_menu"),
          text: "⋯",
          onclick: (event) => {
            event.preventDefault();
            event.stopPropagation();
            openFileMenu(file, chip, (newName) => {
              nameSpan.textContent = `📎 ${newName}`;
            });
          },
        })
      );
    }
    return chip;
  }

  function openFileMenu(file, anchorEl, onRenamed) {
    closeContextMenu();
    const menu = el("div", { class: "item-context-menu open" });
    menu.appendChild(
      el("button", {
        type: "button",
        text: t("workspace.rename_file"),
        onclick: (event) => {
          event.stopPropagation();
          showRenameFileForm(file, menu, onRenamed);
        },
      })
    );
    anchorEl.appendChild(menu);
    activeContextMenu = menu;
    setTimeout(() => document.addEventListener("click", closeContextMenu, { once: true }), 0);
  }

  function showRenameFileForm(file, menu, onRenamed) {
    menu.onclick = (event) => event.stopPropagation();
    menu.innerHTML = "";
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.rename_file") }));
    const input = el("input", { type: "text" });
    input.value = file.name;
    const form = el("div", { class: "project-create-form" }, [
      input,
      el("div", { class: "project-create-actions" }, [
        el("button", { type: "button", text: t("workspace.cancel"), onclick: () => closeContextMenu() }),
        el("button", {
          type: "button",
          class: "primary",
          text: t("workspace.save"),
          onclick: async () => {
            const name = input.value.trim();
            if (!name) return;
            const { ok, data } = await api(`/files/${file.id}`, {
              method: "PATCH",
              body: JSON.stringify({ name }),
            });
            closeContextMenu();
            if (ok && data.ok) {
              file.name = data.file.name;
              if (onRenamed) onRenamed(data.file.name);
            }
          },
        }),
      ]),
    ]);
    menu.appendChild(form);
    input.focus();
    input.select();
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
    // Repli minimal si marked/DOMPurify n'ont pas pu charger (CDN bloque,
    // reseau) : jamais un ecran vide, juste un rendu moins riche.
    let html = escapeHtml(content || "");
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/\n/g, "<br>");
    return html;
  }

  // Rendu "rapport professionnel" des reponses des boutons/n8n : ces
  // workflows renvoient un unique bloc markdown (titres, tableaux, listes,
  // gras...) en texte brut - jusqu'ici affiche via markdownLiteToHtml, qui ne
  // comprenait ni les titres ni les tableaux ni les listes. On utilise
  // desormais un vrai parseur markdown (marked, charge en CDN dans
  // page2.html) puis on assainit le HTML resultant (DOMPurify) avant de
  // l'injecter : le contenu vient de reponses IA potentiellement influencees
  // par du texte externe (document joint, page web recuperee par un
  // workflow), jamais une source de confiance a traiter comme du HTML brut.
  // Ne change jamais CE que l'IA repond, uniquement comment c'est affiche.
  function renderMarkdownToHtml(content) {
    const text = content || "";
    if (!window.marked || !window.DOMPurify) {
      return markdownLiteToHtml(text);
    }
    try {
      const rawHtml = window.marked.parse(text, { breaks: true, gfm: true });
      return window.DOMPurify.sanitize(rawHtml, { ADD_ATTR: ["target", "rel"] });
    } catch (error) {
      return markdownLiteToHtml(text);
    }
  }

  function renderMarkdownBlock(block) {
    const div = el("div", { class: "block-markdown" });
    div.innerHTML = renderMarkdownToHtml(block.content || "");
    // Les tableaux markdown (frequents dans les reponses des boutons SPS/
    // Patents) doivent pouvoir defiler horizontalement sans jamais faire
    // deborder la bulle de message ni la page (meme regle que les blocs
    // "table" structures, voir .block-table-wrap).
    div.querySelectorAll("table").forEach((table) => {
      const wrap = el("div", { class: "md-table-wrap" });
      table.replaceWith(wrap);
      wrap.appendChild(table);
    });
    // Un lien qui ressemble a une source/reference (texte court, domaine
    // externe) est mis en valeur comme une "puce" plutot qu'un lien bleu
    // classique, pour se rapprocher visuellement d'un vrai rapport.
    div.querySelectorAll("a[href]").forEach((a) => {
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
    });
    return div;
  }

  function renderCalloutBlock(block) {
    const div = el("div", { class: "block-callout" });
    div.innerHTML = renderMarkdownToHtml(block.content || "");
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
        /* le canvas reste vide plutot que de casser le reste de la reponse */
      }
    }, 0);

    return wrap;
  }

  // ---------------------------------------------------------------------
  // Composer : modele, widgets d'actions, fichiers, drag & drop, envoi
  // ---------------------------------------------------------------------

  function resetComposer() {
    dom.composerTextarea.value = "";
    autoResize();
    state.attachments = [];
    state.sourceResultIds = [];
    state.selectedWidgetIndex = null;
    renderFileChips();
    renderActionWidgets();
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
        dom.sendBtn.classList.toggle("model-chatgpt", state.model === "chatgpt");
      });
    });
  }

  // ---------------------------------------------------------------------
  // Banque de boutons/actions (Phase 5)
  // ---------------------------------------------------------------------
  // Les boutons sous l'entry viennent de /api/workspace/entry-actions
  // (persistes dans Postgres-jg_R, voir librairies/workflow_bank.py), plus
  // jamais d'un tableau code en dur. "context" = l'utilisateur courant : cf.
  // la note d'architecture dans workspace_routes.py (une bibliotheque
  // personnelle, valable sur toutes ses conversations).

  async function loadEntryActions() {
    const { ok, data } = await api("/entry-actions");
    if (!ok || !data.ok) return;
    state.entryActions = data.entryActions;
    if (state.selectedWidgetIndex != null && state.selectedWidgetIndex >= state.entryActions.length) {
      state.selectedWidgetIndex = null;
    }
    renderActionWidgets();
  }

  function renderActionWidgets() {
    dom.actionWidgets.innerHTML = "";
    state.entryActions.forEach((entryAction, index) => {
      const btn = el("button", {
        type: "button",
        class: `action-widget-btn${state.selectedWidgetIndex === index ? " active" : ""}`,
        text: entryAction.displayName,
        onclick: () => {
          state.selectedWidgetIndex = state.selectedWidgetIndex === index ? null : index;
          renderActionWidgets();
        },
      });
      const menuBtn = el("button", {
        type: "button",
        class: "action-widget-menu-btn",
        "aria-label": t("workspace.action_menu"),
        text: "⋯",
        onclick: (event) => {
          event.stopPropagation();
          openActionWidgetMenu(entryAction, menuBtn);
        },
      });
      dom.actionWidgets.appendChild(el("div", { class: "action-widget-wrap" }, [btn, menuBtn]));
    });
  }

  function openActionWidgetMenu(entryAction, anchorBtn) {
    closeContextMenu();
    const menu = el("div", { class: "item-context-menu open align-start open-up" });
    if (entryAction.editorUrl) {
      // Ouvre directement l'editeur du workflow n8n de cette action, sans
      // avoir a le rechercher manuellement (spec Phase 5 : accès direct au
      // workflow correspondant). Nouvel onglet : jamais de navigation qui
      // ferait perdre la conversation en cours.
      menu.appendChild(
        el("button", {
          type: "button",
          text: t("workspace.open_in_n8n"),
          onclick: (event) => {
            event.stopPropagation();
            closeContextMenu();
            window.open(entryAction.editorUrl, "_blank", "noopener");
          },
        })
      );
      menu.appendChild(el("div", { class: "menu-divider" }));
    }
    menu.appendChild(
      el("button", {
        type: "button",
        text: t("workspace.rename"),
        onclick: (event) => {
          event.stopPropagation();
          showRenameEntryActionForm(entryAction, menu);
        },
      })
    );
    menu.appendChild(
      el("button", {
        type: "button",
        class: "danger-text",
        text: t("workspace.remove"),
        onclick: async (event) => {
          event.stopPropagation();
          closeContextMenu();
          // "Enlever" ne retire QUE l'association a mon interface : l'action
          // centrale, son workflow n8n et les autres utilisateurs qui
          // l'utilisent restent intacts dans la banque (voir remove_entry_action).
          await api(`/entry-actions/${entryAction.id}`, { method: "DELETE" });
          loadEntryActions();
        },
      })
    );
    anchorBtn.parentElement.appendChild(menu);
    activeContextMenu = menu;
    setTimeout(() => document.addEventListener("click", closeContextMenu, { once: true }), 0);
  }

  function showRenameEntryActionForm(entryAction, menu) {
    menu.onclick = (event) => event.stopPropagation();
    menu.innerHTML = "";
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.rename") }));
    const input = el("input", { type: "text" });
    input.value = entryAction.displayName;
    const form = el("div", { class: "project-create-form" }, [
      input,
      el("div", { class: "project-create-actions" }, [
        el("button", { type: "button", text: t("workspace.cancel"), onclick: (e) => { e.stopPropagation(); closeContextMenu(); } }),
        el("button", {
          type: "button",
          class: "primary",
          text: t("workspace.save"),
          onclick: async (event) => {
            event.stopPropagation();
            const alias = input.value.trim();
            if (!alias) return;
            // Alias LOCAL uniquement : le nom de l'action centrale partagee
            // n'est jamais modifie, donc les autres utilisateurs de la meme
            // action ne voient jamais ce renommage (voir spec Phase 5 §15).
            await api(`/entry-actions/${entryAction.id}`, { method: "PATCH", body: JSON.stringify({ alias }) });
            closeContextMenu();
            loadEntryActions();
          },
        }),
      ]),
    ]);
    menu.appendChild(form);
    input.focus();
    input.select();
  }

  function openAddActionMenu() {
    closeContextMenu();
    const menu = el("div", { class: "item-context-menu open align-start open-up" });
    menu.appendChild(
      el("button", {
        type: "button",
        text: t("workspace.add_action_button"),
        onclick: (event) => {
          event.stopPropagation();
          showCreateActionForm(menu);
        },
      })
    );
    menu.appendChild(
      el("button", {
        type: "button",
        text: t("workspace.choose_action_button"),
        onclick: (event) => {
          event.stopPropagation();
          showChooseActionForm(menu);
        },
      })
    );
    dom.addActionBtn.parentElement.appendChild(menu);
    activeContextMenu = menu;
    setTimeout(() => document.addEventListener("click", closeContextMenu, { once: true }), 0);
  }

  function showCreateActionForm(menu) {
    menu.onclick = (event) => event.stopPropagation();
    menu.innerHTML = "";
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.action_button_name") }));
    const input = el("input", { type: "text", placeholder: t("workspace.action_button_name_placeholder") });
    const form = el("div", { class: "project-create-form" }, [
      input,
      el("div", { class: "project-create-actions" }, [
        el("button", { type: "button", text: t("workspace.cancel"), onclick: (e) => { e.stopPropagation(); closeContextMenu(); } }),
        el("button", {
          type: "button",
          class: "primary",
          text: t("workspace.save"),
          onclick: async (event) => {
            event.stopPropagation();
            const name = input.value.trim();
            if (!name) return;
            const saveBtn = event.currentTarget;
            saveBtn.disabled = true;
            saveBtn.textContent = t("workspace.creating");
            // Cree reellement l'action + son workflow n8n cote serveur
            // (voir POST /action-bank -> librairies/n8n_client.py) : jamais
            // simule cote frontend.
            const created = await api("/action-bank", { method: "POST", body: JSON.stringify({ name }) });
            if (!created.ok || !created.data.ok) {
              saveBtn.disabled = false;
              saveBtn.textContent = t("workspace.save");
              showComposerError(created.data && created.data.error ? created.data.error : t("workspace.error_generic"));
              return;
            }
            await api("/entry-actions", { method: "POST", body: JSON.stringify({ actionId: created.data.action.id }) });
            closeContextMenu();
            loadEntryActions();
          },
        }),
      ]),
    ]);
    menu.appendChild(form);
    input.focus();
  }

  function showChooseActionForm(menu) {
    menu.onclick = (event) => event.stopPropagation();
    menu.innerHTML = "";
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.choose_action_button") }));
    const input = el("input", { type: "text", placeholder: t("workspace.search_actions_placeholder") });
    const resultsBox = el("div", { class: "action-bank-results" });
    menu.appendChild(el("div", { class: "project-create-form" }, [input]));
    menu.appendChild(resultsBox);

    async function runSearch() {
      const { ok, data } = await api(`/action-bank?search=${encodeURIComponent(input.value.trim())}`);
      resultsBox.innerHTML = "";
      if (!ok || !data.ok || !data.actions.length) {
        resultsBox.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_actions_found") }));
        return;
      }
      data.actions.forEach((action) => {
        const chooseBtn = el("button", {
          type: "button",
          class: "action-bank-result-btn",
          text: action.name,
          onclick: async (event) => {
            event.stopPropagation();
            // Reutilise l'action et son workflow existants : aucune
            // recreation, seule l'association a mon interface est ajoutee
            // (voir add_entry_action, deduplique cote serveur).
            await api("/entry-actions", { method: "POST", body: JSON.stringify({ actionId: action.id }) });
            closeContextMenu();
            loadEntryActions();
          },
        });
        const menuBtn = el("button", {
          type: "button",
          class: "action-bank-result-menu-btn",
          "aria-label": t("workspace.action_menu"),
          text: "⋯",
          onclick: (event) => {
            event.stopPropagation();
            openActionBankDeleteMenu(action, menuBtn, runSearch);
          },
        });
        resultsBox.appendChild(el("div", { class: "action-bank-result-row" }, [chooseBtn, menuBtn]));
      });
    }

    input.addEventListener("input", runSearch);
    input.addEventListener("click", (e) => e.stopPropagation());
    runSearch();
    input.focus();
  }

  function wireAddActionButton() {
    dom.addActionBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      openAddActionMenu();
    });
  }

  // ---------------------------------------------------------------------
  // Plateformes/API (banque de connexions) : bouton place immediatement a
  // gauche du trombone. Meme principe de banque centrale et partagee que
  // la banque de boutons ci-dessus (voir librairies/connections_bank.py) :
  // les connexions sont chargees depuis le backend, jamais codees en dur.
  // La clef API n'est jamais recue ici : /connections ne renvoie que des
  // metadonnees (id, nom, type, mots-cles, connected:true).
  // ---------------------------------------------------------------------

  function updateConnectionsBadge() {
    const count = state.selectedConnectionIds.size;
    dom.connectionsBadge.textContent = String(count);
    dom.connectionsBadge.classList.toggle("hidden", count === 0);
  }

  function openConnectionsMenu() {
    closeContextMenu();
    const menu = el("div", { class: "item-context-menu open open-up" });
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.connections_menu_title") }));
    const listBox = el("div", { class: "connections-list" });
    menu.appendChild(listBox);
    menu.appendChild(el("div", { class: "menu-divider" }));
    menu.appendChild(
      el("button", {
        type: "button",
        text: t("workspace.add_connection"),
        onclick: (event) => {
          event.stopPropagation();
          showAddConnectionForm(menu);
        },
      })
    );
    dom.connectionsBtn.parentElement.appendChild(menu);
    activeContextMenu = menu;
    setTimeout(() => document.addEventListener("click", closeContextMenu, { once: true }), 0);
    renderConnectionsList(listBox);
  }

  async function renderConnectionsList(listBox) {
    const { ok, data } = await api("/connections");
    listBox.innerHTML = "";
    if (!ok || !data.ok) return;
    state.connections = data.connections;
    // Une connexion supprimee ailleurs (autre onglet/utilisateur) ne doit
    // jamais rester cochee ici : re-synchronise la selection sur ce qui
    // existe reellement dans la banque.
    const validIds = new Set(state.connections.map((c) => c.id));
    Array.from(state.selectedConnectionIds).forEach((id) => {
      if (!validIds.has(id)) state.selectedConnectionIds.delete(id);
    });
    updateConnectionsBadge();

    if (!state.connections.length) {
      listBox.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_connections") }));
      return;
    }
    state.connections.forEach((connection) => {
      const checkboxId = `ws-conn-${connection.id}`;
      const checkbox = el("input", { type: "checkbox", id: checkboxId });
      checkbox.checked = state.selectedConnectionIds.has(connection.id);
      checkbox.addEventListener("click", (event) => event.stopPropagation());
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selectedConnectionIds.add(connection.id);
        else state.selectedConnectionIds.delete(connection.id);
        updateConnectionsBadge();
      });
      const label = el("label", { class: "connection-row-label", for: checkboxId }, [
        el("span", { class: "connection-row-name", text: connection.name }),
        el("span", { class: "connection-row-type", text: connection.platformType }),
      ]);
      label.addEventListener("click", (event) => event.stopPropagation());
      const menuBtn = el("button", {
        type: "button",
        class: "connection-row-menu-btn",
        "aria-label": t("workspace.action_menu"),
        text: "⋯",
        onclick: (event) => {
          event.stopPropagation();
          openConnectionRowMenu(connection, menuBtn, () => renderConnectionsList(listBox));
        },
      });
      listBox.appendChild(el("div", { class: "connection-row" }, [checkbox, label, menuBtn]));
    });
  }

  function openConnectionRowMenu(connection, anchorBtn, onChanged) {
    closeFixedMenu();
    const rect = anchorBtn.getBoundingClientRect();
    const menu = el("div", { class: "action-bank-fixed-menu" });
    menu.style.top = `${rect.bottom + 4}px`;
    menu.style.left = `${Math.max(8, rect.right - 200)}px`;
    menu.appendChild(
      el("button", {
        type: "button",
        text: t("workspace.rename"),
        onclick: (event) => {
          event.stopPropagation();
          showRenameConnectionForm(connection, menu, onChanged);
        },
      })
    );
    menu.appendChild(
      el("button", {
        type: "button",
        class: "danger-text",
        text: t("workspace.delete_connection"),
        onclick: (event) => {
          event.stopPropagation();
          closeFixedMenu();
          showConfirmModal(t("workspace.confirm_delete_connection").replace("{name}", connection.name), async () => {
            const { ok, data } = await api(`/connections/${connection.id}`, { method: "DELETE" });
            if (!ok || !data.ok) {
              showComposerError(data && data.error ? data.error : t("workspace.error_generic"));
              return;
            }
            state.selectedConnectionIds.delete(connection.id);
            if (onChanged) onChanged();
          });
        },
      })
    );
    document.body.appendChild(menu);
    activeFixedMenu = menu;
    setTimeout(() => document.addEventListener("click", closeFixedMenu, { once: true }), 0);
  }

  function showRenameConnectionForm(connection, menu, onChanged) {
    menu.innerHTML = "";
    menu.onclick = (event) => event.stopPropagation();
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.rename") }));
    const input = el("input", { type: "text" });
    input.value = connection.name;
    const form = el("div", { class: "project-create-form" }, [
      input,
      el("div", { class: "project-create-actions" }, [
        el("button", { type: "button", text: t("workspace.cancel"), onclick: () => closeFixedMenu() }),
        el("button", {
          type: "button",
          class: "primary",
          text: t("workspace.save"),
          onclick: async () => {
            const name = input.value.trim();
            if (!name) return;
            await api(`/connections/${connection.id}`, { method: "PATCH", body: JSON.stringify({ name }) });
            closeFixedMenu();
            if (onChanged) onChanged();
          },
        }),
      ]),
    ]);
    menu.appendChild(form);
    input.focus();
    input.select();
  }

  function showAddConnectionForm(menu) {
    menu.onclick = (event) => event.stopPropagation();
    menu.innerHTML = "";
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.add_connection") }));

    const nameInput = el("input", { type: "text", placeholder: t("workspace.connection_name_placeholder") });
    const typeInput = el("input", { type: "text", placeholder: t("workspace.connection_type_placeholder") });
    const keyInput = el("input", { type: "password", placeholder: t("workspace.connection_api_key_placeholder") });
    const keywordsInput = el("input", { type: "text", placeholder: t("workspace.connection_keywords_placeholder") });
    const baseUrlInput = el("input", { type: "text", placeholder: t("workspace.connection_base_url_placeholder") });

    const advancedWrap = el("div", { class: "project-create-form hidden" }, [baseUrlInput]);
    const advancedToggle = el("button", {
      type: "button",
      class: "connections-advanced-toggle",
      text: t("workspace.advanced_settings"),
      onclick: (event) => {
        event.stopPropagation();
        advancedWrap.classList.toggle("hidden");
      },
    });

    const form = el("div", { class: "project-create-form" }, [
      nameInput,
      typeInput,
      keyInput,
      keywordsInput,
      advancedToggle,
      advancedWrap,
      el("div", { class: "project-create-actions" }, [
        el("button", { type: "button", text: t("workspace.cancel"), onclick: (e) => { e.stopPropagation(); closeContextMenu(); } }),
        el("button", {
          type: "button",
          class: "primary",
          text: t("workspace.create"),
          onclick: async (event) => {
            event.stopPropagation();
            const name = nameInput.value.trim();
            const platformType = typeInput.value.trim();
            const apiKey = keyInput.value.trim();
            if (!name || !platformType || !apiKey) {
              showComposerError(t("workspace.error_generic"));
              return;
            }
            const keywords = keywordsInput.value
              .split(",")
              .map((k) => k.trim())
              .filter(Boolean);
            const saveBtn = event.currentTarget;
            saveBtn.disabled = true;
            saveBtn.textContent = t("workspace.creating");
            const created = await api("/connections", {
              method: "POST",
              body: JSON.stringify({
                name,
                platformType,
                apiKey,
                keywords,
                baseUrl: baseUrlInput.value.trim(),
              }),
            });
            if (!created.ok || !created.data.ok) {
              saveBtn.disabled = false;
              saveBtn.textContent = t("workspace.create");
              showComposerError(created.data && created.data.error ? created.data.error : t("workspace.error_generic"));
              return;
            }
            state.selectedConnectionIds.add(created.data.connection.id);
            closeContextMenu();
            openConnectionsMenu();
          },
        }),
      ]),
    ]);
    menu.appendChild(form);
    nameInput.focus();
  }

  function wireConnectionsButton() {
    dom.connectionsBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      openConnectionsMenu();
    });
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
    if (stillUploading) return;

    state.sending = true;
    updateSendButtonState();
    dom.centralColumn.classList.remove("is-empty");

    if (!state.conversationId) {
      dom.conversationArea.innerHTML = "";
    }

    const userMessageRow = renderMessage({
      role: "user",
      authorName: state.user.displayName,
      authorAvatarUrl: state.user.avatarUrl,
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
    const selectedEntryAction = state.selectedWidgetIndex != null ? state.entryActions[state.selectedWidgetIndex] : null;
    const payload = {
      conversationId: state.conversationId,
      model: state.model,
      message: text,
      fileIds: readyAttachments.map((a) => a.fileId),
      sourceResultIds: state.sourceResultIds,
      requestId,
    };
    // Un bouton de la banque (Phase 5) declenche SON PROPRE workflow n8n
    // cote backend (voir jobs.py) : jamais confondu avec le provider
    // ChatGPT/Claude selectionne par ailleurs.
    if (selectedEntryAction) {
      payload.action = { id: selectedEntryAction.actionId, type: selectedEntryAction.actionId, parameters: {} };
    }
    // Plateformes/API : seuls les identifiants sont transmis (jamais une
    // configuration ou une clef) ; le backend resout, filtre par pertinence
    // et appelle lui-meme les connexions retenues (voir jobs.py).
    if (state.selectedConnectionIds.size) {
      payload.connectionIds = Array.from(state.selectedConnectionIds);
    }

    const composerSnapshot = {
      text,
      attachments: state.attachments.slice(),
      widgetIndex: state.selectedWidgetIndex,
    };
    resetComposer();

    function showSendError(message) {
      loadingRow.remove();
      const statusRow = el("div", { class: "message-status error" }, [
        el("span", { text: message }),
        el("button", {
          type: "button",
          text: t("workspace.retry"),
          onclick: () => {
            statusRow.remove();
            state.attachments = composerSnapshot.attachments;
            state.selectedWidgetIndex = composerSnapshot.widgetIndex;
            dom.composerTextarea.value = composerSnapshot.text;
            renderFileChips();
            renderActionWidgets();
            sendMessage();
          },
        }),
      ]);
      userMessageRow.appendChild(statusRow);
      scrollToBottom();
    }

    // L'etat "en cours" (bouton desactive, entry bloquee) dure jusqu'a la
    // VRAIE reponse du provider (evenement SSE), pas juste jusqu'a la mise
    // en file : un seul clic = un seul workflow, et on empeche toute
    // nouvelle saisie avant que celui-ci n'ait reellement repondu.
    const { ok, data } = await api("/messages", { method: "POST", body: JSON.stringify(payload) });

    if (!ok || !data.ok) {
      state.sending = false;
      updateSendButtonState();
      const statusMap = { 504: "workspace.error_timeout", 503: "workspace.error_not_configured" };
      showSendError(statusMap[data.status] ? t(statusMap[data.status]) : data.error ? data.error : t("workspace.error_generic"));
      return;
    }

    if (data.replay) {
      // Requete deja completee lors d'une tentative precedente (meme
      // requestId rejoue) : sa reponse assistant a deja ete diffusee a
      // l'epoque, rien de nouveau a attendre ici.
      state.sending = false;
      updateSendButtonState();
      loadingRow.remove();
      return;
    }

    if (data.isNewConversation) {
      state.conversationId = data.conversationId;
      state.conversationTitle = data.conversationTitle;
      connectRealtime(state.conversationId); // pas encore connecte : cette conversation vient de naitre
    }
    // Deja rendu localement de maniere optimiste ci-dessus : le marquer vu
    // evite de le re-afficher en double quand sa publication SSE revient.
    if (data.userMessage) {
      state.seenMessageIds.add(data.userMessage.id);
      if (data.userMessage.seq) state.lastSeenSeq = Math.max(state.lastSeenSeq, data.userMessage.seq);
    }
    loadDiscussions(true);

    // Phase 4 : la reponse assistant est traitee en arriere-plan (appel n8n
    // potentiellement long) et arrivera plus tard via SSE, jamais dans cette
    // reponse HTTP. Le loader reste visible, rattache a cette requete : voir
    // connectRealtime() pour sa resolution (message.created / workflow.failed).
    // Garde-fou : si aucune resolution n'arrive (SSE coupe, worker perdu),
    // ne jamais laisser l'entry bloquee indefiniment.
    const timeoutId = setTimeout(() => {
      if (!state.pendingRequests.has(requestId)) return; // deja resolu entre-temps
      state.pendingRequests.delete(requestId);
      state.sending = false;
      updateSendButtonState();
      showSendError(t("workspace.error_timeout"));
    }, PENDING_REQUEST_TIMEOUT_MS);
    state.pendingRequests.set(requestId, { loadingRow, showSendError, timeoutId });
  }

  function resolvePendingRequest(requestId) {
    const pending = state.pendingRequests.get(requestId);
    if (!pending) return null;
    clearTimeout(pending.timeoutId);
    state.pendingRequests.delete(requestId);
    state.sending = false;
    updateSendButtonState();
    return pending;
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
    dom.composerTextarea.addEventListener("input", pingTyping);
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
    wireModal();
    initSidebarToggle();
    wireModelSelector();
    wireFileUpload();
    wireComposer();
    wireAddActionButton();
    wireConnectionsButton();
    updateConnectionsBadge();

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
    dom.discussionsHeader.addEventListener("click", () => toggleSection(dom.discussionsSection));
    dom.sharedDiscussionsHeader.addEventListener("click", () => toggleSection(dom.sharedDiscussionsSection));
    dom.newDiscussionBtn.addEventListener("click", startNewDiscussion);

    dom.newSharedDiscussionBtn.addEventListener("click", () => dom.sharedDiscussionForm.classList.toggle("hidden"));
    dom.sharedDiscussionCancelBtn.addEventListener("click", () => {
      dom.sharedDiscussionForm.classList.add("hidden");
      dom.sharedDiscussionNameInput.value = "";
    });
    dom.sharedDiscussionCreateBtn.addEventListener("click", submitNewSharedDiscussion);
    dom.sharedDiscussionNameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") submitNewSharedDiscussion();
    });

    renderActionWidgets();

    // La phrase initiale est ecrite par le typewriter, pas par le systeme
    // data-i18n declaratif : sans ce listener, changer de langue en cours
    // de route (FR<->AR) ne la mettait pas a jour tant qu'on ne relancait
    // pas une nouvelle discussion.
    document.addEventListener("agentstage:langchange", () => {
      if (!dom.centralColumn.classList.contains("is-empty")) return;
      const span = dom.conversationArea.querySelector(".initial-question span");
      if (span) span.textContent = t("workspace.initial_question");
      const cursor = dom.conversationArea.querySelector(".typewriter-cursor");
      if (cursor) cursor.remove();
    });

    await Promise.all([loadProjects(), loadDiscussions(true), loadEntryActions(), loadSharedConversations()]);

    renderInitialQuestion();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
