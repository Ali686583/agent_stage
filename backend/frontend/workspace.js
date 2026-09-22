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

  // Mode "Message" (mission §4) : jamais un provider IA, voir data-model
  // dans page2.html et le bypass cote serveur (workspace_routes.py).
  const MESSAGE_MODEL = "message";

  const state = {
    user: null,
    conversationId: null,
    conversationTitle: "",
    model: "chatgpt",
    attachments: [], // {fileId, name, status: 'uploading'|'uploaded'|'failed'}
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
    oauthGloballyConfigured: false,
    selectedConnectionIds: new Set(),
    projects: [],
    sharedProjects: [],
    discussionsCursor: null,
    // Dictee vocale (Web Speech API) : etat de l'instance de reconnaissance
    // en cours, voir wireDictation(). null si non supportee par le navigateur.
    dictation: { recognition: null, active: false, supported: false },
    sending: false,
    sidebarCollapsed: false,
    // Temps reel (Phase 3)
    eventSource: null,
    seenMessageIds: new Set(),
    lastSeenSeq: 0,
    // Messages de la conversation actuellement affichee, dans l'ordre :
    // sert uniquement a "Telecharger la discussion en PDF" (jamais
    // rejoue vers le serveur, purement un miroir local de ce qui est
    // deja rendu a l'ecran).
    currentMessages: [],
    // "Repondre" a un message precis (mission contexte+reply §2/§3) :
    // reply_to_message_id, persiste en base (voir librairies/workspace.py),
    // fonctionne pour N'IMPORTE QUEL message (utilisateur, "Message", IA,
    // bouton) -- contrairement a l'ancien mecanisme sourceResultIds (toujours
    // accepte par le backend pour compatibilite, mais plus pilote depuis
    // cette UI : ce role est repris par replyToMessageId, plus general).
    replyToMessageId: null,
    replyPreviewText: "",
    typingUsers: new Map(), // userId -> { displayName, avatarUrl, timeoutId }
    lastTypingPingAt: 0,
    pendingRequests: new Map(), // requestId -> { loadingRow, showSendError } (Phase 4, reponses asynchrones)
    // Mentions "@" (discussions communes) : liste des {userId, displayName}
    // reellement inseres dans le texte du composer, pas juste "tapes" -- un
    // "@Nom" ensuite efface par l'utilisateur est retire au moment de
    // l'envoi (voir sendMessage), jamais renvoye comme mention fantome.
    composerMentions: [],
    // Participants de la conversation ACTUELLEMENT ouverte, pour le menu de
    // mentions -- rechargee a chaque ouverture de conversation commune (voir
    // openConversation), jamais suppose a jour indefiniment.
    mentionCandidates: [],
    mentionMenu: null, // {triggerStart, activeIndex, filtered}
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
    dom.sharedProjectsSection = document.getElementById("ws-shared-projects-section");
    dom.sharedProjectsHeader = document.getElementById("ws-shared-projects-header");
    dom.sharedProjectsList = document.getElementById("ws-shared-projects-list");
    dom.newSharedProjectBtn = document.getElementById("ws-new-shared-project-btn");
    dom.sharedProjectForm = document.getElementById("ws-shared-project-form");
    dom.sharedProjectNameInput = document.getElementById("ws-shared-project-name-input");
    dom.sharedProjectDescriptionInput = document.getElementById("ws-shared-project-description-input");
    dom.sharedProjectCancelBtn = document.getElementById("ws-shared-project-cancel-btn");
    dom.sharedProjectCreateBtn = document.getElementById("ws-shared-project-create-btn");
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
    dom.replyPreview = document.getElementById("ws-reply-preview");
    dom.fileInput = document.getElementById("ws-file-input");
    dom.attachBtn = document.getElementById("ws-attach-btn");
    dom.sendBtn = document.getElementById("ws-send-btn");
    dom.modelButtons = Array.from(document.querySelectorAll(".model-selector button"));
    dom.actionWidgets = document.getElementById("ws-action-widgets");
    dom.addActionBtn = document.getElementById("ws-add-action-btn");
    dom.dictateBtn = document.getElementById("ws-dictate-btn");
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
    dom.optGoogleDrive = document.getElementById("ws-opt-google-drive");
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

    // Notifications (mission notifications, Phase 6)
    dom.notificationsBtn = document.getElementById("ws-notifications-btn");
    dom.notificationsBadge = document.getElementById("ws-notifications-badge");
    dom.notificationToasts = document.getElementById("ws-notification-toasts");

    // Documentation / RAG (mission RAG, Phase 4/7)
    dom.documentationEntry = document.getElementById("ws-documentation-entry");
    dom.docsModalOverlay = document.getElementById("ws-docs-modal-overlay");
    dom.docsModalClose = document.getElementById("ws-docs-modal-close");
    dom.docsSearchInput = document.getElementById("ws-docs-search-input");
    dom.docsUploadInput = document.getElementById("ws-docs-upload-input");
    dom.docsUploadBtn = document.getElementById("ws-docs-upload-btn");
    dom.docsList = document.getElementById("ws-docs-list");
    dom.docsLoadMore = document.getElementById("ws-docs-load-more");
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
        refreshInitialGreetingIfVisible();
      }
    });
    dom.nicknameRemove.addEventListener("click", async () => {
      const { ok, data } = await authApi("/display-name", { method: "DELETE" });
      if (ok && data.ok) {
        state.user = data.user;
        applyUserToUI();
        dom.nicknameForm.classList.add("hidden");
        loadDiscussions(true);
        refreshInitialGreetingIfVisible();
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
  // Google Drive (bouton "Resume Drive", mission §5) : connexion/deconnexion
  // OAuth personnelle, depuis le menu profil ("Plus d'options").
  // ---------------------------------------------------------------------

  async function refreshGoogleDriveOption() {
    const { ok, data } = await api("/integrations/google-drive/status");
    if (!ok || !data.ok) return;
    if (!data.configured) {
      // Jamais une fausse promesse : si GOOGLE_OAUTH_* n'est pas configure
      // sur ce deploiement, l'option reste invisible plutot que de proposer
      // un bouton qui echouerait systematiquement (mission §5, ne jamais
      // simuler un fonctionnement impossible).
      dom.optGoogleDrive.classList.add("hidden");
      return;
    }
    dom.optGoogleDrive.classList.remove("hidden");
    dom.optGoogleDrive.textContent = data.connected
      ? `${t("workspace.opt_disconnect_drive")}${data.googleEmail ? " (" + data.googleEmail + ")" : ""}`
      : t("workspace.opt_connect_drive");
    dom.optGoogleDrive.dataset.connected = data.connected ? "true" : "false";
  }

  function wireGoogleDriveOption() {
    dom.optGoogleDrive.addEventListener("click", async () => {
      if (dom.optGoogleDrive.dataset.connected === "true") {
        await api("/integrations/google-drive", { method: "DELETE" });
        await refreshGoogleDriveOption();
        return;
      }
      // Flux OAuth reel : necessite une navigation complete (redirection vers
      // l'ecran de consentement Google), jamais un simple fetch.
      window.location.href = `${API}/integrations/google-drive/connect`;
    });
  }

  function handleGoogleDriveRedirectStatus() {
    const params = new URLSearchParams(window.location.search);
    const status = params.get("googleDriveStatus");
    if (!status) return;
    const messageKeyByStatus = {
      connected: "workspace.google_drive_connected",
      denied: "workspace.google_drive_error",
      error: "workspace.google_drive_error",
      state_mismatch: "workspace.google_drive_error",
    };
    showComposerError(t(messageKeyByStatus[status] || "workspace.google_drive_error"));
    // Nettoie l'URL (jamais garder ce parametre au rechargement/partage du lien).
    params.delete("googleDriveStatus");
    const newSearch = params.toString();
    window.history.replaceState({}, "", window.location.pathname + (newSearch ? `?${newSearch}` : ""));
  }

  // ---------------------------------------------------------------------
  // Connexions plateformes/API, entree 2 (OAuth generique) : retour de
  // redirection apres le flux d'autorisation (voir workspace_routes.py,
  // connections_oauth_callback_route) -- meme principe que
  // handleGoogleDriveRedirectStatus ci-dessus, code errreur stable jamais un
  // texte serveur brut.
  // ---------------------------------------------------------------------

  function handleConnectionsOAuthRedirectStatus() {
    const params = new URLSearchParams(window.location.search);
    const status = params.get("connectionsOAuthStatus");
    if (!status) return;
    const messageKeyByStatus = {
      connected: "workspace.connections_oauth_connected",
      denied: "workspace.connections_oauth_error",
      error: "workspace.connections_oauth_error",
      state_mismatch: "workspace.connections_oauth_error",
    };
    showComposerError(t(messageKeyByStatus[status] || "workspace.connections_oauth_error"));
    params.delete("connectionsOAuthStatus");
    const newSearch = params.toString();
    window.history.replaceState({}, "", window.location.pathname + (newSearch ? `?${newSearch}` : ""));
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
          await loadProjectConversations(project.id, list, false);
        }
      });
      group.appendChild(header);
      group.appendChild(list);
      dom.projectsList.appendChild(group);
    });
  }

  async function loadProjectConversations(projectId, listEl, isSharedProject) {
    const { ok, data } = await api(`/projects/${projectId}/conversations?limit=30`);
    listEl.innerHTML = "";
    if (!ok || !data.ok) return;
    if (!data.conversations.length) {
      listEl.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.empty_history") }));
      return;
    }
    data.conversations.forEach((conversation) => {
      listEl.appendChild(
        isSharedProject
          ? renderSharedConversationItem(conversation, projectId)
          : renderConversationItem(conversation, projectId)
      );
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
    // La description n'est proposee que pour les projets communs (mission
    // §2) : le champ n'existe pas pour un projet personnel, comportement
    // inchange pour ne rien casser la (rename_project accepte de toute
    // facon description=undefined sans y toucher, voir workspace.py).
    if (project.isShared) menu.classList.add("wide");
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.rename_project") }));
    const input = el("input", { type: "text", value: project.name });
    input.value = project.name;
    const formChildren = [input];
    let descriptionInput = null;
    if (project.isShared) {
      // Deja enregistree lors d'une modification precedente : reaffichee
      // automatiquement dans le champ (mission §2, dernier point).
      descriptionInput = el("textarea", {
        class: "project-description-input",
        rows: "3",
        placeholder: t("workspace.add_description_placeholder"),
      });
      descriptionInput.value = project.description || "";
      formChildren.push(descriptionInput);
    }
    formChildren.push(
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
            const body = { name };
            if (descriptionInput) body.description = descriptionInput.value.trim();
            const { ok } = await api(`/projects/${project.id}`, { method: "PATCH", body: JSON.stringify(body) });
            closeContextMenu();
            if (ok) await (project.isShared ? loadSharedProjects() : loadProjects());
          },
        }),
      ])
    );
    const form = el("div", { class: "project-create-form" }, formChildren);
    menu.appendChild(form);
    input.focus();
    input.select();
  }

  // ---------------------------------------------------------------------
  // Projets communs : meme logique que les discussions communes (visibles
  // et modifiables par tous les utilisateurs authentifies), generalisee aux
  // projets plutot que dupliquee (voir workspace.list_shared_projects cote
  // serveur, qui reutilise la meme table `projects` avec is_shared=true).
  // ---------------------------------------------------------------------

  async function loadSharedProjects() {
    const { ok, data } = await api("/projects/shared");
    if (ok && data.ok) {
      state.sharedProjects = data.projects;
      renderSharedProjects();
    }
  }

  function renderSharedProjects() {
    dom.sharedProjectsList.innerHTML = "";
    if (!state.sharedProjects.length) {
      dom.sharedProjectsList.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_shared_projects") }));
      return;
    }
    state.sharedProjects.forEach((project) => {
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
            openSharedProjectMenu(project, header);
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
          // Meme route que pour un projet personnel : le serveur distingue
          // deja lui-meme is_shared et renvoie la bonne liste (voir
          // list_project_conversations_route), aucune duplication necessaire ici.
          // isSharedProject=true : rend les items avec la semantique commune
          // (menu "Deplacer dans un autre projet COMMUN", jamais de
          // suppression -- voir renderSharedConversationItem).
          await loadProjectConversations(project.id, list, true);
        }
      });
      group.appendChild(header);
      group.appendChild(list);
      dom.sharedProjectsList.appendChild(group);
    });
  }

  async function submitNewSharedProject() {
    const name = dom.sharedProjectNameInput.value.trim();
    if (!name) return;
    // Description facultative (mission "Projets communs" §2) : son absence
    // ne doit jamais empecher la creation, on envoie simplement une chaine
    // vide que le serveur traite comme "pas de description".
    const description = dom.sharedProjectDescriptionInput.value.trim();
    const { ok, data } = await api("/projects/shared", {
      method: "POST",
      body: JSON.stringify({ name, description }),
    });
    if (ok && data.ok) {
      dom.sharedProjectNameInput.value = "";
      dom.sharedProjectDescriptionInput.value = "";
      dom.sharedProjectForm.classList.add("hidden");
      await loadSharedProjects();
    }
  }

  function openSharedProjectMenu(project, anchorEl) {
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
    } else {
      menu.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_permission") }));
    }
    // Jamais d'option de suppression ici : un projet commun n'appartient a
    // personne en particulier (meme regle que la conversation commune, voir
    // workspace.delete_project qui la refuse explicitement).
    anchorEl.appendChild(menu);
    activeContextMenu = menu;
    setTimeout(() => document.addEventListener("click", closeContextMenu, { once: true }), 0);
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

  function renderConversationItem(conversation, contextProjectId) {
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
        openConversationMenu(conversation, menuBtn, contextProjectId);
      },
    });
    item.appendChild(main);
    item.appendChild(menuBtn);
    item.addEventListener("click", () => openConversation(conversation.id));
    return item;
  }

  async function moveConversationToProject(conversationId, fromProjectId, toProjectId) {
    const { ok, data } = await api(`/conversations/${conversationId}/move-project`, {
      method: "POST",
      body: JSON.stringify({ fromProjectId, toProjectId }),
    });
    if (!ok || !data.ok) {
      showComposerError(data && data.error ? data.error : t("workspace.error_generic"));
      return false;
    }
    // Rafraichissement complet des deux arbres (personnel + commun) : le plus
    // simple pour garantir que la discussion disparait bien de l'ancien
    // projet et apparait dans le nouveau, sans etat intermediaire incoherent
    // (referme au passage les groupes deja ouverts, cout accepte pour la
    // garantie de coherence -- mission §6, "mettre immediatement l'interface
    // a jour").
    await Promise.all([loadProjects(), loadSharedProjects()]);
    return true;
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
    menu.appendChild(el("div", { class: "action-bank-creator-label", text: creatorLabelText(action.createdByName) }));
    menu.appendChild(
      el("button", {
        type: "button",
        class: "danger-text",
        text: t("workspace.delete_button_definitively"),
        onclick: (event) => {
          event.stopPropagation();
          closeFixedMenu();
          // Suppression DEFINITIVE en un clic (mission "Supprimer
          // definitivement" §3, decision produit) : detache de la banque
          // partagee ET supprime le workflow n8n associe, sans possibilite
          // d'annulation -- d'ou une confirmation explicite avant d'appeler
          // l'API (reprend le modal existant, deja utilise ailleurs pour les
          // autres suppressions destructives de l'app).
          showConfirmModal(t("workspace.delete_button_confirm"), async () => {
            const { ok, data } = await api(`/action-bank/${action.id}`, { method: "DELETE" });
            if (!ok || !data.ok) {
              showComposerError(data && data.error ? data.error : t("workspace.error_generic"));
              return;
            }
            if (onDeleted) onDeleted();
          });
        },
      })
    );
    document.body.appendChild(menu);
    activeFixedMenu = menu;
    setTimeout(() => document.addEventListener("click", closeFixedMenu, { once: true }), 0);
  }

  function openConversationMenu(conversation, anchorBtn, contextProjectId) {
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
    // "Deplacer dans un autre projet" (mission §6) : uniquement pertinent
    // depuis la liste des discussions d'un projet PRECIS (contextProjectId),
    // jamais depuis la liste generale "Discussions" ou aucun projet source
    // n'est connu. Le projet courant n'est jamais propose comme destination.
    if (contextProjectId) {
      const otherProjects = state.projects.filter((p) => p.id !== contextProjectId);
      menu.appendChild(el("div", { class: "menu-divider" }));
      menu.appendChild(el("div", { class: "menu-label", text: t("workspace.move_to_project") }));
      if (!otherProjects.length) {
        menu.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_other_projects") }));
      } else {
        otherProjects.forEach((project) => {
          menu.appendChild(
            el("button", {
              type: "button",
              text: project.name,
              onclick: async (event) => {
                event.stopPropagation();
                closeContextMenu();
                await moveConversationToProject(conversation.id, contextProjectId, project.id);
              },
            })
          );
        });
      }
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
    state.currentMessages = [];
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

  function renderSharedConversationItem(conversation, contextProjectId) {
    const item = el("div", { class: "sidebar-item", "data-conversation-id": conversation.id });
    if (conversation.id === state.conversationId) item.classList.add("active");
    const main = el("div", { class: "sidebar-item-main" }, [
      avatarNode("sidebar-item-avatar", conversation.createdByAvatarUrl, conversation.createdByName),
      el("div", { class: "sidebar-item-text" }, [
        el("div", { class: "sidebar-item-title", text: conversation.title }),
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
        openSharedConversationMenu(conversation, menuBtn, contextProjectId);
      },
    });
    item.appendChild(main);
    item.appendChild(menuBtn);
    item.addEventListener("click", async () => {
      await openConversation(conversation.id);
      document.querySelectorAll(".sidebar-item.active").forEach((n) => n.classList.remove("active"));
      item.classList.add("active");
    });
    return item;
  }

  function openSharedConversationMenu(conversation, anchorBtn, contextProjectId) {
    closeContextMenu();
    const menu = el("div", { class: "item-context-menu open" });
    menu.appendChild(el("div", { class: "menu-label", text: t("workspace.add_to_project") }));
    if (!state.sharedProjects.length) {
      menu.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_shared_projects") }));
    } else {
      state.sharedProjects.forEach((project) => {
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
              await loadSharedProjects();
            },
          })
        );
      });
    }
    // "Deplacer dans un autre projet commun" (mission §7) : le backend
    // revalide integralement le droit de deplacement (participation a la
    // discussion + type commun/commun compatible, voir
    // workspace.move_conversation_to_project) -- cette liste cote-client
    // n'est qu'un confort d'affichage, jamais la source de la permission.
    if (contextProjectId) {
      const otherProjects = state.sharedProjects.filter((p) => p.id !== contextProjectId);
      menu.appendChild(el("div", { class: "menu-divider" }));
      menu.appendChild(el("div", { class: "menu-label", text: t("workspace.move_to_shared_project") }));
      if (!otherProjects.length) {
        menu.appendChild(el("div", { class: "sidebar-empty", text: t("workspace.no_other_projects") }));
      } else {
        otherProjects.forEach((project) => {
          menu.appendChild(
            el("button", {
              type: "button",
              text: project.name,
              onclick: async (event) => {
                event.stopPropagation();
                closeContextMenu();
                await moveConversationToProject(conversation.id, contextProjectId, project.id);
              },
            })
          );
        });
      }
    }
    // "Supprimer la discussion" (commune) : reserve au createur (ou un
    // administrateur, verifie de toute facon cote serveur -- voir
    // workspace.delete_conversation) -- meme garde-fou que pour une
    // conversation personnelle, juste applique ici a is_shared=true.
    if (conversation.createdByUserId === state.user.id) {
      menu.appendChild(el("div", { class: "menu-divider" }));
      menu.appendChild(
        el("button", {
          type: "button",
          class: "danger-text",
          text: t("workspace.delete_conversation"),
          onclick: (event) => {
            event.stopPropagation();
            closeContextMenu();
            showConfirmModal(t("workspace.confirm_delete_conversation"), async () => {
              const { ok, data } = await api(`/conversations/${conversation.id}`, { method: "DELETE" });
              if (ok && data.ok) {
                if (state.conversationId === conversation.id) startNewDiscussion();
                await loadSharedConversations();
              } else {
                showComposerError(data && data.error ? data.error : t("workspace.error_generic"));
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
    // Mentions "@" : seulement pertinent pour une discussion commune (voir
    // workspace.list_participants, qui existe deja pour toute conversation
    // mais n'a de vrai sens de "personnes a mentionner" que la ou plusieurs
    // participants coexistent reellement).
    state.composerMentions = [];
    state.mentionCandidates = [];
    closeMentionMenu();
    if (data.conversation.isShared) loadMentionCandidates(conversationId);
    dom.centralColumn.classList.remove("is-empty");
    dom.conversationArea.innerHTML = "";
    state.seenMessageIds = new Set();
    state.lastSeenSeq = 0;
    state.currentMessages = [];
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
      const statusMap = {
        n8n_timeout: "workspace.error_timeout",
        workflow_not_configured: "workspace.error_not_configured",
        // Bouton "Resume Drive" (mission §5) : chaque code d'echec distinct
        // recoit un message clair, jamais une trace technique brute.
        google_drive_not_configured: "workspace.error_not_configured",
        google_drive_not_connected: "workspace.google_drive_not_connected",
        google_drive_no_reference_found: "workspace.google_drive_no_reference_found",
        google_drive_document_not_found: "workspace.google_drive_document_not_found",
        google_drive_permission_denied: "workspace.google_drive_permission_denied",
        google_drive_document_too_large: "workspace.google_drive_document_too_large",
        google_drive_unsupported_file_type: "workspace.google_drive_unsupported_file_type",
        google_drive_empty_document: "workspace.google_drive_empty_document",
        google_drive_drive_api_error: "workspace.google_drive_api_error",
        // Boutons "Veille Web" (sans API / Tavily) : memes principes que
        // Google Drive ci-dessus.
        web_search_not_configured: "workspace.error_not_configured",
        web_search_empty_query: "workspace.web_search_empty_query",
        web_search_no_results: "workspace.web_search_no_results",
        web_search_network_error: "workspace.web_search_network_error",
      };
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
    // Garde le lien "Telecharger la discussion en PDF" en tout dernier
    // (ne le CREE pas ici : rien a exporter tant qu'aucun message n'existe).
    if (document.getElementById("ws-conversation-footer")) ensureConversationFooter();
    scrollToBottom();
  }

  function pingTyping() {
    if (!state.conversationId) return; // pas encore de conversation : rien a signaler
    const now = Date.now();
    if (now - state.lastTypingPingAt < 2500) return; // throttle cote client
    state.lastTypingPingAt = now;
    api(`/conversations/${state.conversationId}/typing`, { method: "POST" });
  }

  // Pseudo reellement defini par l'utilisateur : meme comparaison que dans
  // wireProfileMenu() (displayName === email <=> aucun pseudo choisi, voir
  // database.display_name_for cote serveur, qui applique deja ce repli).
  // Jamais invente cote client : si aucun pseudo, on n'affiche simplement rien.
  function userHasPseudo() {
    return !!(state.user && state.user.displayName && state.user.displayName !== state.user.email);
  }

  function renderInitialQuestion() {
    dom.centralColumn.classList.add("is-empty");
    dom.conversationArea.innerHTML = "";
    if (userHasPseudo()) {
      dom.conversationArea.appendChild(
        el("div", { class: "initial-greeting", id: "ws-initial-greeting" }, [
          t("workspace.greeting_prefix") + " " + state.user.displayName + t("workspace.greeting_suffix"),
        ])
      );
    }
    const heading = el("div", { class: "initial-question" });
    const span = el("span", {});
    const cursor = el("span", { class: "typewriter-cursor" });
    heading.appendChild(span);
    heading.appendChild(cursor);
    dom.conversationArea.appendChild(heading);
    typewrite(span, t("workspace.initial_question"), cursor);
  }

  // Met a jour la ligne "Bonjour ..." sans relancer le typewriter, si
  // l'ecran d'accueil est actuellement affiche (appele apres un changement
  // de pseudonyme, voir wireProfileMenu()).
  function refreshInitialGreetingIfVisible() {
    if (!dom.centralColumn.classList.contains("is-empty")) return;
    const existing = document.getElementById("ws-initial-greeting");
    if (userHasPseudo()) {
      const text = t("workspace.greeting_prefix") + " " + state.user.displayName + t("workspace.greeting_suffix");
      if (existing) {
        existing.textContent = text;
      } else {
        dom.conversationArea.insertBefore(
          el("div", { class: "initial-greeting", id: "ws-initial-greeting" }, [text]),
          dom.conversationArea.firstChild
        );
      }
    } else if (existing) {
      existing.remove();
    }
  }

  // Surligne chaque "@Nom" (mentions structurees, voir message.mentions)
  // dans le DOM deja rendu d'une bulle de message : parcourt uniquement les
  // noeuds TEXTE (jamais le HTML lui-meme, pour ne jamais casser un rendu
  // markdown deja assaini) et remplace les occurrences exactes par un
  // <span class="mention-tag">.
  function highlightMentions(container, mentions) {
    if (!mentions.length) return;
    const names = mentions.map((m) => m.displayName).filter(Boolean);
    if (!names.length) return;
    const pattern = new RegExp(`@(${names.map(escapeRegExp).join("|")})(?![\\w])`, "g");
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
    const targets = [];
    let node;
    while ((node = walker.nextNode())) {
      if (pattern.test(node.textContent)) targets.push(node);
      pattern.lastIndex = 0;
    }
    targets.forEach((textNode) => {
      const frag = document.createDocumentFragment();
      let lastIndex = 0;
      let match;
      pattern.lastIndex = 0;
      while ((match = pattern.exec(textNode.textContent))) {
        if (match.index > lastIndex) {
          frag.appendChild(document.createTextNode(textNode.textContent.slice(lastIndex, match.index)));
        }
        frag.appendChild(el("span", { class: "mention-tag", text: match[0] }));
        lastIndex = match.index + match[0].length;
      }
      frag.appendChild(document.createTextNode(textNode.textContent.slice(lastIndex)));
      textNode.replaceWith(frag);
    });
  }

  function escapeRegExp(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
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
    state.currentMessages.push(message);
    const row = el("div", { class: `message-row ${message.role}` });
    if (message.id) row.setAttribute("data-message-id", message.id);
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
    // Aperçu du message auquel celui-ci repond (mission "Repondre" §3) :
    // persistant (vient du backend, reste apres actualisation/reouverture),
    // cliquable pour remonter jusqu'au message original.
    if (message.replyTo) {
      bubble.appendChild(renderReplyContextBubble(message.replyTo));
    }
    if (message.blocks && message.blocks.length) {
      renderBlocks(bubble, message.blocks);
    } else if (message.content) {
      const textDiv = el("div", { class: "block-markdown" });
      textDiv.textContent = message.content;
      bubble.appendChild(textDiv);
    }
    // Mentions "@" : surlignage APRES le rendu (blocks ou texte brut), sur
    // le DOM deja construit -- ne touche que les noeuds de texte, ne casse
    // jamais le HTML du rendu markdown (voir highlightMentions).
    if (message.mentions && message.mentions.length) {
      highlightMentions(bubble, message.mentions);
    }
    if (message.attachments && message.attachments.length) {
      const attWrap = el("div", { class: "message-attachments" });
      message.attachments.forEach((file) => attWrap.appendChild(fileChipReadOnly(file)));
      bubble.appendChild(attWrap);
    }
    // Actions discretes de bas de reponse (mission §5) : PDF/Copier restent
    // reserves a l'agent ; "Repondre" est desormais disponible sur TOUT
    // message (mission contexte+reply §2 : repondre a un message precis
    // n'est pas limite aux reponses IA, ex. repondre a un "Message" d'un
    // autre participant dans une discussion commune).
    if (message.role === "assistant") {
      bubble.appendChild(renderMessageActions(message));
    } else if (message.id) {
      bubble.appendChild(renderUserMessageActions(message));
    }
    row.appendChild(bubble);
    dom.conversationArea.appendChild(row);
    ensureConversationFooter();
    return row;
  }

  // ---------------------------------------------------------------------
  // Aperçu "en reponse a" persistant au-dessus d'un message (mission §3)
  // ---------------------------------------------------------------------

  function renderReplyContextBubble(replyTo) {
    const bubble = el("div", {
      class: "reply-context-bubble",
      role: "button",
      tabindex: "0",
      "aria-label": t("workspace.replying_to"),
    });
    if (replyTo.unavailable) {
      bubble.classList.add("unavailable");
      bubble.appendChild(el("span", { class: "reply-context-text", text: t("workspace.original_message_unavailable") }));
    } else {
      bubble.appendChild(el("span", { class: "reply-context-author", text: replyTo.authorName || "" }));
      bubble.appendChild(el("span", { class: "reply-context-text", text: replyTo.excerpt || "" }));
      const activate = () => scrollToMessageAndHighlight(replyTo.id);
      bubble.addEventListener("click", activate);
      bubble.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      });
    }
    return bubble;
  }

  function scrollToMessageAndHighlight(messageId) {
    const target = dom.conversationArea.querySelector(`[data-message-id="${CSS.escape(messageId)}"]`);
    if (!target) {
      // Message pas (encore) charge dans cette vue (au-dela de la fenetre
      // d'historique deja recuperee) : jamais une erreur bloquante, un
      // simple signal discret (mission §13, gerer proprement sans casser
      // l'affichage existant).
      showComposerError(t("workspace.original_message_not_loaded"));
      return;
    }
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("message-row-highlight");
    setTimeout(() => target.classList.remove("message-row-highlight"), 1600);
  }

  function renderUserMessageActions(message) {
    const wrap = el("div", { class: "message-actions message-actions-user" });
    // Bouton Copier egalement sur les messages utilisateur (mission §6,
    // "toutes les bulles/messages dont le contenu est copiable") : meme
    // helper que pour les reponses assistant, voir buildCopyButton.
    wrap.appendChild(buildCopyButton(message));
    const replyBtn = el("button", {
      type: "button",
      class: "msg-action-link",
      text: t("workspace.reply"),
      onclick: () => startReplyTo(message),
    });
    wrap.appendChild(replyBtn);
    return wrap;
  }

  // ---------------------------------------------------------------------
  // Actions de reponse : Copier / Telecharger en PDF / Repondre (mission §5)
  // ---------------------------------------------------------------------

  function extractMessagePlainText(message) {
    if (Array.isArray(message.blocks) && message.blocks.length) {
      return message.blocks
        .map((block) => {
          if (!block) return "";
          if (block.type === "table") {
            const header = (block.headers || []).join(" | ");
            const rows = (block.rows || []).map((row) => row.join(" | ")).join("\n");
            return [header, rows].filter(Boolean).join("\n");
          }
          return block.content || block.text || block.label || "";
        })
        .filter(Boolean)
        .join("\n\n");
    }
    return message.content || "";
  }

  function copyIconSvg() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "14");
    svg.setAttribute("height", "14");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.innerHTML =
      '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>';
    return svg;
  }

  function checkIconSvg() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "14");
    svg.setAttribute("height", "14");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2.4");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.innerHTML = '<polyline points="20 6 9 17 4 12"/>';
    return svg;
  }

  async function copyMessageContent(message, btn) {
    const text = extractMessagePlainText(message);
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // Repli pour un contexte sans Clipboard API (navigateur ancien,
        // page non servie en HTTPS) : jamais planter l'action pour autant.
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      btn.innerHTML = "";
      btn.appendChild(checkIconSvg());
      btn.classList.add("msg-action-success");
      setTimeout(() => {
        btn.innerHTML = "";
        btn.appendChild(copyIconSvg());
        btn.classList.remove("msg-action-success");
      }, 1500);
    } catch (error) {
      showComposerError(t("workspace.copy_error"));
    }
  }

  function downloadMessagePdf(message, linkEl) {
    if (!window.AgentStagePdf) {
      showComposerError(t("workspace.pdf_export_error"));
      return;
    }
    try {
      window.AgentStagePdf.downloadMessagePdf(message, {
        title: message.authorName,
        subtitle: formatDate(message.createdAt),
        filename: `${message.authorName || "reponse"}-${message.id || ""}`,
      });
    } catch (error) {
      // Un echec de generation PDF ne doit jamais casser la discussion
      // (mission §9, "cas d'erreur") : simple feedback, rien d'autre.
      showComposerError(t("workspace.pdf_export_error"));
    }
  }

  // Extrait de renderMessageActions (mission §6) : reutilise pour les
  // messages utilisateur ET assistant, jamais deux implementations qui
  // pourraient diverger (copyMessageContent/copyIconSvg/checkIconSvg sont
  // deja entierement generiques, independants du role).
  function buildCopyButton(message) {
    const copyBtn = el(
      "button",
      { type: "button", class: "msg-action-icon-btn", "aria-label": t("workspace.copy_response"), title: t("workspace.copy_response") },
      [copyIconSvg()]
    );
    copyBtn.addEventListener("click", () => copyMessageContent(message, copyBtn));
    return copyBtn;
  }

  function renderMessageActions(message) {
    const wrap = el("div", { class: "message-actions" });
    const copyBtn = buildCopyButton(message);
    const replyBtn = el("button", {
      type: "button",
      class: "msg-action-link",
      text: t("workspace.reply"),
      onclick: () => startReplyTo(message),
    });
    wrap.appendChild(copyBtn);
    // "Telecharger en PDF" par reponse individuelle (mission PDF §8) :
    // uniquement utile/affiche quand la reponse provient d'un bouton/workflow
    // de la banque (message.actionId non nul) -- une reponse ChatGPT/Claude
    // "classique" (sans bouton) n'a pas besoin de ce PDF individuel, "Copier"
    // reste suffisant pour elle.
    if (message.actionId) {
      const pdfBtn = el("button", {
        type: "button",
        class: "msg-action-link",
        text: t("workspace.download_pdf"),
        onclick: () => downloadMessagePdf(message, pdfBtn),
      });
      wrap.appendChild(pdfBtn);
    }
    wrap.appendChild(replyBtn);
    return wrap;
  }

  // ---------------------------------------------------------------------
  // Telecharger l'integralite de la discussion en PDF (mission §6) :
  // toujours le dernier element de la zone de conversation (voir
  // ensureConversationFooter, appele apres chaque nouveau message).
  // ---------------------------------------------------------------------

  function ensureConversationFooter() {
    let footer = document.getElementById("ws-conversation-footer");
    if (!footer) {
      footer = el("div", { class: "conversation-footer", id: "ws-conversation-footer" });
      const link = el("button", { type: "button", class: "msg-action-link", text: t("workspace.download_conversation_pdf") });
      link.addEventListener("click", () => downloadConversationPdf(link));
      footer.appendChild(link);
    }
    dom.conversationArea.appendChild(footer);
    return footer;
  }

  function downloadConversationPdf(linkEl) {
    if (!window.AgentStagePdf) {
      showComposerError(t("workspace.pdf_export_error"));
      return;
    }
    try {
      window.AgentStagePdf.downloadConversationPdf(
        { title: state.conversationTitle || t("workspace.new_discussion") },
        state.currentMessages,
        { filename: state.conversationTitle || "discussion" }
      );
    } catch (error) {
      showComposerError(t("workspace.pdf_export_error"));
    }
  }

  function fileChipReadOnly(file) {
    // Correctif overflow (mission §5) : la classe file-chip-name (ellipsis
    // CSS) DOIT etre portee par l'element flex-item lui-meme (ce <a>), pas
    // par un <span> imbrique dedans -- overflow/text-overflow n'ont aucun
    // effet sur une boite inline simple qui n'est pas elle-meme l'item flex
    // (voir .reply-context-text/.reply-preview-text pour l'analogue qui
    // fonctionne deja). `title` restitue le nom complet au survol.
    const nameLink = el("a", {
      href: `${API}/files/${file.id}`,
      target: "_blank",
      rel: "noopener",
      class: "file-chip-name",
      title: file.name,
      text: `📎 ${file.name}`,
      style: "color:inherit;text-decoration:none;",
    });
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
              nameLink.textContent = `📎 ${newName}`;
              nameLink.title = newName;
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
    state.replyToMessageId = null;
    state.replyPreviewText = "";
    state.selectedWidgetIndex = null;
    state.composerMentions = [];
    closeMentionMenu();
    renderFileChips();
    renderActionWidgets();
    renderReplyPreview();
  }

  // ---------------------------------------------------------------------
  // Mentions "@" (discussions communes) : menu deroulant filtrable au clavier
  // ET a la souris, insertion structuree (userId + nom, jamais juste du
  // texte colore -- voir librairies/database.py::message_mentions).
  // ---------------------------------------------------------------------

  async function loadMentionCandidates(conversationId) {
    const { ok, data } = await api(`/conversations/${conversationId}/participants`);
    if (!ok || !data.ok) return;
    state.mentionCandidates = (data.participants || []).filter((p) => p.userId !== state.user.id);
  }

  function closeMentionMenu() {
    if (state.mentionMenu && state.mentionMenu.node) state.mentionMenu.node.remove();
    state.mentionMenu = null;
  }

  function mentionMenuCandidates(query) {
    const q = query.toLowerCase();
    return state.mentionCandidates.filter((p) => p.displayName.toLowerCase().includes(q)).slice(0, 8);
  }

  function insertMention(candidate) {
    if (!state.mentionMenu) return;
    const { triggerStart } = state.mentionMenu;
    const textarea = dom.composerTextarea;
    const cursor = textarea.selectionStart;
    const before = textarea.value.slice(0, triggerStart);
    const after = textarea.value.slice(cursor);
    const insertion = `@${candidate.displayName} `;
    textarea.value = before + insertion + after;
    const newCursor = before.length + insertion.length;
    textarea.setSelectionRange(newCursor, newCursor);
    textarea.focus();
    autoResize();
    // Deduplique par userId : re-mentionner la meme personne ne cree pas
    // deux entrees (voir aussi la deduplication cote serveur, redondante
    // par prudence, jamais la seule ligne de defense).
    if (!state.composerMentions.some((m) => m.userId === candidate.userId)) {
      state.composerMentions.push({ userId: candidate.userId, displayName: candidate.displayName });
    }
    closeMentionMenu();
  }

  function renderMentionMenu(triggerStart, candidates, activeIndex) {
    closeMentionMenu();
    const menu = el("div", { class: "mention-menu" });
    if (!candidates.length) {
      menu.appendChild(el("div", { class: "mention-menu-empty", text: t("workspace.mention_no_results") }));
    } else {
      candidates.forEach((candidate, index) => {
        const item = el("button", {
          type: "button",
          class: `mention-menu-item${index === activeIndex ? " active" : ""}`,
          onclick: (event) => {
            event.preventDefault();
            insertMention(candidate);
          },
        }, [
          avatarNode("sidebar-item-avatar", candidate.avatarUrl, candidate.displayName),
          el("span", { text: candidate.displayName }),
        ]);
        // mousedown (pas click) : evite que le textarea ne perde le focus et
        // ne ferme le menu (blur) avant que le clic ne soit traite.
        item.addEventListener("mousedown", (event) => {
          event.preventDefault();
          insertMention(candidate);
        });
        menu.appendChild(item);
      });
    }
    const rect = dom.composer.getBoundingClientRect();
    menu.style.position = "fixed";
    menu.style.left = `${rect.left}px`;
    menu.style.bottom = `${window.innerHeight - rect.top + 6}px`;
    document.body.appendChild(menu);
    state.mentionMenu = { triggerStart, candidates, activeIndex, node: menu };
  }

  function handleMentionInput() {
    const textarea = dom.composerTextarea;
    const cursor = textarea.selectionStart;
    const textBeforeCursor = textarea.value.slice(0, cursor);
    // Declenche seulement apres un debut de mot ("@" en tout debut, ou
    // precede d'un espace/retour a la ligne) -- jamais au milieu d'une
    // adresse email ou d'un mot quelconque contenant "@".
    const match = textBeforeCursor.match(/(?:^|[\s\n])@([^\s@]*)$/);
    if (!match || !state.mentionCandidates.length) {
      closeMentionMenu();
      return;
    }
    const triggerStart = cursor - match[0].length + (match[0].startsWith("@") ? 0 : 1);
    const query = match[1];
    const candidates = mentionMenuCandidates(query);
    renderMentionMenu(triggerStart, candidates, 0);
  }

  function handleMentionKeydown(event) {
    if (!state.mentionMenu) return false;
    const { candidates, activeIndex } = state.mentionMenu;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      const next = candidates.length ? (activeIndex + 1) % candidates.length : 0;
      renderMentionMenu(state.mentionMenu.triggerStart, candidates, next);
      return true;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      const next = candidates.length ? (activeIndex - 1 + candidates.length) % candidates.length : 0;
      renderMentionMenu(state.mentionMenu.triggerStart, candidates, next);
      return true;
    }
    if (event.key === "Enter" || event.key === "Tab") {
      if (candidates.length) {
        event.preventDefault();
        insertMention(candidates[activeIndex]);
        return true;
      }
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeMentionMenu();
      return true;
    }
    return false;
  }

  // ---------------------------------------------------------------------
  // "Repondre" a un message precis (mission contexte+reply §2/§3) :
  // state.replyToMessageId est persiste en base sur le message envoye (voir
  // librairies/workspace.py::add_message), et le backend (librairies/jobs.py)
  // en fait le contexte PRIORITAIRE de la prochaine demande IA. Fonctionne
  // pour n'importe quel message (utilisateur, "Message", IA, bouton).
  // N'empeche jamais l'usage normal de l'agent/bouton/pieces jointes/
  // connexions selectionnes par ailleurs.
  // ---------------------------------------------------------------------

  function renderReplyPreview() {
    dom.replyPreview.innerHTML = "";
    if (!state.replyToMessageId) {
      dom.replyPreview.classList.add("hidden");
      return;
    }
    dom.replyPreview.classList.remove("hidden");
    dom.replyPreview.appendChild(el("span", { class: "reply-preview-label", text: t("workspace.replying_to") }));
    dom.replyPreview.appendChild(el("span", { class: "reply-preview-text", text: state.replyPreviewText }));
    dom.replyPreview.appendChild(
      el("button", {
        type: "button",
        class: "reply-preview-cancel",
        "aria-label": t("workspace.cancel_reply"),
        title: t("workspace.cancel_reply"),
        text: "×",
        onclick: cancelReply,
      })
    );
  }

  function startReplyTo(message) {
    if (!message.id) return;
    state.replyToMessageId = message.id;
    state.replyPreviewText = extractMessagePlainText(message).slice(0, 160) || t("workspace.attachment_only");
    renderReplyPreview();
    dom.composerTextarea.focus();
  }

  function cancelReply() {
    state.replyToMessageId = null;
    state.replyPreviewText = "";
    renderReplyPreview();
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
        // Mode "Message" (mission §4) : meme vert que le logo, voir
        // --brand-green dans theme.css et .send-btn.model-message ci-dessous.
        dom.sendBtn.classList.toggle("model-message", state.model === "message");
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

  // Suivi des noeuds DOM deja rendus, cle par entryAction.id : permet un
  // diff (seuls les boutons ajoutes/retires sont animes et touchent le DOM,
  // ceux deja presents restent en place) plutot qu'un reset complet a
  // chaque rafraichissement de state.entryActions, qui provoquerait un saut
  // visuel desagreable de toute la rangee (mission "boutons au-dessus de
  // l'entry" §1 : pas de deplacement/saut brutal).
  const actionWidgetNodes = new Map(); // entryAction.id -> { wrap, btn, menuBtn }

  function renderActionWidgets() {
    const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const currentIds = new Set(state.entryActions.map((ea) => ea.id));

    // Boutons disparus (supprimes/renommes ailleurs) : animation de sortie
    // (pop-out) puis retrait reel du DOM, jamais une disparition brutale.
    actionWidgetNodes.forEach((node, id) => {
      if (currentIds.has(id)) return;
      actionWidgetNodes.delete(id);
      if (reducedMotion) {
        node.wrap.remove();
        return;
      }
      node.wrap.classList.add("widget-removing");
      node.wrap.addEventListener("transitionend", () => node.wrap.remove(), { once: true });
      // Garde-fou : si transitionend ne se declenche jamais pour une raison
      // quelconque, ne jamais laisser un bouton fantome indefiniment.
      setTimeout(() => { if (node.wrap.parentElement) node.wrap.remove(); }, 400);
    });

    state.entryActions.forEach((entryAction, index) => {
      let node = actionWidgetNodes.get(entryAction.id);
      const isActive = state.selectedWidgetIndex === index;
      if (!node) {
        const btn = el("button", { type: "button", class: "action-widget-btn" });
        const menuBtn = el("button", {
          type: "button",
          class: "action-widget-menu-btn",
          "aria-label": t("workspace.action_menu"),
          text: "⋯",
        });
        const wrap = el("div", { class: "action-widget-wrap" }, [btn, menuBtn]);
        node = { wrap, btn, menuBtn };
        actionWidgetNodes.set(entryAction.id, node);
        if (!reducedMotion) {
          // Nouveau bouton : demarre reduit/transparent puis relache la
          // classe au prochain frame pour que la transition CSS (voir
          // .action-widget-wrap dans workspace.css) l'anime vers son etat
          // normal (pop-in), au lieu d'apparaitre instantanement.
          wrap.classList.add("widget-entering");
          requestAnimationFrame(() => requestAnimationFrame(() => wrap.classList.remove("widget-entering")));
        }
      }
      node.btn.textContent = entryAction.displayName;
      node.btn.className = `action-widget-btn${isActive ? " active" : ""}`;
      node.btn.onclick = () => {
        state.selectedWidgetIndex = state.selectedWidgetIndex === index ? null : index;
        renderActionWidgets();
      };
      node.menuBtn.onclick = (event) => {
        event.stopPropagation();
        openActionWidgetMenu(entryAction, node.menuBtn);
      };
      const referenceNode = dom.actionWidgets.children[index] || null;
      if (referenceNode !== node.wrap) {
        dom.actionWidgets.insertBefore(node.wrap, referenceNode);
      }
    });
  }

  function creatorLabelText(createdByName) {
    return `${t("workspace.creator_label")} ${createdByName || t("workspace.creator_unknown")}`;
  }

  function openActionWidgetMenu(entryAction, anchorBtn) {
    closeContextMenu();
    const menu = el("div", { class: "item-context-menu open align-start open-up" });
    // Provient de donnees reellement enregistrees en base (jointure
    // resolue cote serveur, voir workspace_routes._with_entry_action_creator_names)
    // jamais devinee/simulee cote client. Un ancien bouton dont le compte
    // createur a ete supprime depuis affiche "inconnu" plutot qu'un nom faux.
    menu.appendChild(el("div", { class: "action-bank-creator-label", text: creatorLabelText(entryAction.actionCreatedByName) }));
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
    // Jamais un bouton "Se connecter (OAuth)" qui echouerait systematiquement
    // (meme principe que Google Drive, voir refreshGoogleDriveOption) :
    // n'est propose que si le connecteur est configure sur ce deploiement.
    state.oauthGloballyConfigured = !!data.oauthGloballyConfigured;
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
      const labelChildren = [el("span", { class: "connection-row-name", text: connection.name })];
      // Champ "Plateforme / Type" retire du formulaire de creation (voir
      // showAddConnectionForm) : ne reste affiche que pour les connexions
      // deja creees avant ce changement, jamais une ligne vide pour les
      // nouvelles.
      if (connection.platformType) {
        labelChildren.push(el("span", { class: "connection-row-type", text: connection.platformType }));
      }
      const label = el("label", { class: "connection-row-label", for: checkboxId }, labelChildren);
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
    // Entree 2 (OAuth generique) : n'affiche "Se connecter" que si la
    // connexion a effectivement une configuration OAuth (voir
    // connections_bank.py, hasOAuthConfig) -- jamais un bouton qui
    // echouerait systematiquement. Une fois connectee, propose plutot
    // "Deconnecter" (retire seulement le jeton, pas la connexion).
    if (connection.hasOAuthConfig && (connection.oauthConnected || state.oauthGloballyConfigured)) {
      if (connection.oauthConnected) {
        menu.appendChild(
          el("button", {
            type: "button",
            text: t("workspace.oauth_disconnect"),
            onclick: async (event) => {
              event.stopPropagation();
              closeFixedMenu();
              await api(`/connections/${connection.id}/oauth`, { method: "DELETE" });
              if (onChanged) onChanged();
            },
          })
        );
      } else {
        menu.appendChild(
          el("button", {
            type: "button",
            text: t("workspace.oauth_connect"),
            onclick: (event) => {
              event.stopPropagation();
              window.location.href = `${API}/connections/${connection.id}/oauth/connect`;
            },
          })
        );
      }
    }
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

    // Seul le nom est obligatoire. Les TROIS entrees ci-dessous (cle API
    // generalisee / OAuth generique / serveur MCP -- voir
    // librairies/connections_bank.py) sont affichees directement,
    // facultatives et independantes : on peut remplir n'importe laquelle,
    // plusieurs, ou aucune.
    const nameInput = el("input", { type: "text", placeholder: t("workspace.connection_name_placeholder") });
    const keywordsInput = el("input", { type: "text", placeholder: t("workspace.connection_keywords_placeholder") });

    const apiKeyInput = el("input", { type: "password", placeholder: t("workspace.connection_api_key_placeholder") });
    const baseUrlInput = el("input", { type: "text", placeholder: t("workspace.connection_base_url_placeholder") });
    const authLocationSelect = el("select", { class: "connections-select" }, [
      el("option", { value: "header_bearer", text: t("workspace.connection_auth_bearer") }),
      el("option", { value: "header_custom", text: t("workspace.connection_auth_header") }),
      el("option", { value: "query_param", text: t("workspace.connection_auth_query") }),
    ]);
    const authFieldNameInput = el("input", { type: "text", placeholder: t("workspace.connection_auth_field_placeholder") });
    const authFieldNameWrap = el("div", { class: "project-create-form hidden" }, [authFieldNameInput]);
    authLocationSelect.addEventListener("change", (event) => {
      event.stopPropagation();
      authFieldNameWrap.classList.toggle("hidden", authLocationSelect.value === "header_bearer");
    });
    authLocationSelect.addEventListener("click", (event) => event.stopPropagation());

    const oauthClientIdInput = el("input", { type: "text", placeholder: t("workspace.connection_oauth_client_id_placeholder") });
    const oauthClientSecretInput = el("input", { type: "password", placeholder: t("workspace.connection_oauth_client_secret_placeholder") });
    const oauthAuthorizeUrlInput = el("input", { type: "text", placeholder: t("workspace.connection_oauth_authorize_url_placeholder") });
    const oauthTokenUrlInput = el("input", { type: "text", placeholder: t("workspace.connection_oauth_token_url_placeholder") });
    const oauthScopeInput = el("input", { type: "text", placeholder: t("workspace.connection_oauth_scope_placeholder") });

    const mcpServerUrlInput = el("input", { type: "text", placeholder: t("workspace.connection_mcp_url_placeholder") });
    const mcpAuthLocationSelect = el("select", { class: "connections-select" }, [
      el("option", { value: "none", text: t("workspace.connection_mcp_auth_none") }),
      el("option", { value: "header_bearer", text: t("workspace.connection_auth_bearer") }),
      el("option", { value: "header_custom", text: t("workspace.connection_auth_header") }),
    ]);
    const mcpAuthFieldNameInput = el("input", { type: "text", placeholder: t("workspace.connection_auth_field_placeholder") });
    const mcpAuthFieldNameWrap = el("div", { class: "project-create-form hidden" }, [mcpAuthFieldNameInput]);
    const mcpAuthTokenInput = el("input", { type: "password", placeholder: t("workspace.connection_mcp_token_placeholder") });
    mcpAuthLocationSelect.addEventListener("change", (event) => {
      event.stopPropagation();
      mcpAuthFieldNameWrap.classList.toggle("hidden", mcpAuthLocationSelect.value !== "header_custom");
    });
    mcpAuthLocationSelect.addEventListener("click", (event) => event.stopPropagation());

    const form = el("div", { class: "project-create-form" }, [
      nameInput,
      keywordsInput,
      el("div", { class: "connections-entry-label", text: t("workspace.connection_entry_api_key") }),
      apiKeyInput,
      baseUrlInput,
      authLocationSelect,
      authFieldNameWrap,
      el("div", { class: "connections-entry-label", text: t("workspace.connection_entry_oauth") }),
      oauthClientIdInput,
      oauthClientSecretInput,
      oauthAuthorizeUrlInput,
      oauthTokenUrlInput,
      oauthScopeInput,
      el("div", { class: "connections-entry-label", text: t("workspace.connection_entry_mcp") }),
      mcpServerUrlInput,
      mcpAuthLocationSelect,
      mcpAuthFieldNameWrap,
      mcpAuthTokenInput,
      el("div", { class: "project-create-actions" }, [
        el("button", { type: "button", text: t("workspace.cancel"), onclick: (e) => { e.stopPropagation(); closeContextMenu(); } }),
        el("button", {
          type: "button",
          class: "primary",
          text: t("workspace.create"),
          onclick: async (event) => {
            event.stopPropagation();
            const name = nameInput.value.trim();
            if (!name) {
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
                keywords,
                apiKeyEntry: {
                  apiKey: apiKeyInput.value.trim(),
                  baseUrl: baseUrlInput.value.trim(),
                  authLocation: authLocationSelect.value,
                  authFieldName: authFieldNameInput.value.trim(),
                },
                oauthEntry: {
                  clientId: oauthClientIdInput.value.trim(),
                  clientSecret: oauthClientSecretInput.value.trim(),
                  authorizeUrl: oauthAuthorizeUrlInput.value.trim(),
                  tokenUrl: oauthTokenUrlInput.value.trim(),
                  scope: oauthScopeInput.value.trim(),
                },
                mcpEntry: {
                  serverUrl: mcpServerUrlInput.value.trim(),
                  authLocation: mcpAuthLocationSelect.value,
                  authFieldName: mcpAuthFieldNameInput.value.trim(),
                  authToken: mcpAuthTokenInput.value.trim(),
                },
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
        el("span", {
          class: "file-chip-name",
          title: attachment.name,
          text: `📎 ${attachment.name}${attachment.status === "uploading" ? " (" + t("workspace.uploading") + ")" : ""}`,
        }),
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

    const isMessageMode = state.model === MESSAGE_MODEL;

    state.sending = true;
    updateSendButtonState();
    dom.centralColumn.classList.remove("is-empty");

    if (!state.conversationId) {
      dom.conversationArea.innerHTML = "";
    }

    // Objet mutable (pas juste un litteral jete) : une fois l'id reel connu
    // (reponse HTTP ci-dessous), on le complete EN PLACE plutot que de
    // laisser ce rendu optimiste fige sans id -- sinon "Repondre" a SON
    // PROPRE message tout juste envoye resterait indisponible tant que la
    // conversation n'est pas rouverte (voir la mise a jour plus bas).
    const optimisticUserMessage = {
      role: "user",
      authorName: state.user.displayName,
      authorAvatarUrl: state.user.avatarUrl,
      content: text,
      blocks: null,
      attachments: readyAttachments.map((a) => ({ id: a.fileId, name: a.name })),
    };
    const userMessageRow = renderMessage(optimisticUserMessage);
    scrollToBottom();

    // Mode "Message" (mission §4) : aucune IA n'est appelee, donc aucun
    // loader "en cours de generation" a afficher -- rien a attendre.
    let loadingRow = null;
    if (!isMessageMode) {
      loadingRow = el("div", { class: "message-row assistant" }, [
        el("div", { class: "message-bubble" }, [
          el("div", { class: "loader-dots" }, [el("span"), el("span"), el("span")]),
        ]),
      ]);
      dom.conversationArea.appendChild(loadingRow);
      scrollToBottom();
    }

    const requestId = uuid();
    const selectedEntryAction = state.selectedWidgetIndex != null ? state.entryActions[state.selectedWidgetIndex] : null;
    const payload = {
      conversationId: state.conversationId,
      model: state.model,
      message: text,
      fileIds: readyAttachments.map((a) => a.fileId),
      requestId,
    };
    if (state.replyToMessageId) {
      payload.replyToMessageId = state.replyToMessageId;
    }
    // Mentions "@" : ne garde que celles dont le "@Nom" exact est encore
    // present dans le texte envoye -- une mention inseree puis effacee par
    // l'utilisateur ne doit jamais etre renvoyee comme mention fantome.
    const activeMentions = state.composerMentions.filter((m) => text.includes(`@${m.displayName}`));
    if (activeMentions.length) {
      payload.mentionedUserIds = activeMentions.map((m) => m.userId);
    }
    // Un bouton de la banque (Phase 5) declenche SON PROPRE workflow n8n
    // cote backend (voir jobs.py) : jamais confondu avec le provider
    // ChatGPT/Claude selectionne par ailleurs. Sans objet en mode "Message".
    if (selectedEntryAction && !isMessageMode) {
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
      replyToMessageId: state.replyToMessageId,
      replyPreviewText: state.replyPreviewText,
      mentions: activeMentions.slice(),
    };
    resetComposer();

    function showSendError(message) {
      if (loadingRow) loadingRow.remove();
      const statusRow = el("div", { class: "message-status error" }, [
        el("span", { text: message }),
        el("button", {
          type: "button",
          text: t("workspace.retry"),
          onclick: () => {
            statusRow.remove();
            state.attachments = composerSnapshot.attachments;
            state.selectedWidgetIndex = composerSnapshot.widgetIndex;
            state.replyToMessageId = composerSnapshot.replyToMessageId;
            state.replyPreviewText = composerSnapshot.replyPreviewText;
            state.composerMentions = composerSnapshot.mentions;
            dom.composerTextarea.value = composerSnapshot.text;
            renderFileChips();
            renderActionWidgets();
            renderReplyPreview();
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
    // nouvelle saisie avant que celui-ci n'ait reellement repondu. En mode
    // "Message", il n'y a justement rien a attendre : reactive des la
    // reponse HTTP (voir plus bas).
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
      if (loadingRow) loadingRow.remove();
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
      // Complete le rendu optimiste avec l'id reel (mission "Repondre" §2/§3) :
      // sans ca, "Repondre" a SON PROPRE message tout juste envoye (ex. le
      // scenario de test "creer A, creer B, repondre a A" dans la MEME
      // session) resterait indisponible tant que la conversation n'est pas
      // rouverte.
      Object.assign(optimisticUserMessage, data.userMessage);
      userMessageRow.setAttribute("data-message-id", data.userMessage.id);
      const bubbleEl = userMessageRow.querySelector(".message-bubble");
      if (bubbleEl) {
        if (data.userMessage.replyTo && !bubbleEl.querySelector(".reply-context-bubble")) {
          const authorEl = bubbleEl.querySelector(".message-author");
          bubbleEl.insertBefore(renderReplyContextBubble(data.userMessage.replyTo), authorEl ? authorEl.nextSibling : bubbleEl.firstChild);
        }
        if (!bubbleEl.querySelector(".message-actions-user")) {
          bubbleEl.appendChild(renderUserMessageActions(optimisticUserMessage));
        }
        // Le rendu optimiste initial n'a pas encore les mentions (connues
        // seulement une fois la reponse serveur arrivee) : surligne
        // maintenant, jamais besoin de rouvrir la conversation pour le voir.
        if (data.userMessage.mentions && data.userMessage.mentions.length) {
          highlightMentions(bubbleEl, data.userMessage.mentions);
        }
      }
    }
    loadDiscussions(true);

    if (isMessageMode || !data.queued) {
      // Mode "Message" : le message est deja enregistre/diffuse, rien
      // d'autre a attendre (voir workspace_routes.py::send_message_route).
      state.sending = false;
      updateSendButtonState();
      return;
    }

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
    dom.composerTextarea.addEventListener("input", handleMentionInput);
    dom.composerTextarea.addEventListener("keydown", (event) => {
      // Le menu de mentions intercepte d'abord (fleches/Entree/Echap) :
      // jamais envoyer le message si l'utilisateur navigue dans le menu.
      if (handleMentionKeydown(event)) return;
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });
    dom.composerTextarea.addEventListener("blur", () => {
      // Delai court : laisse le temps a un clic sur un item du menu de
      // s'executer (mousedown) avant que le blur ne le ferme.
      setTimeout(() => closeMentionMenu(), 150);
    });
    dom.sendBtn.addEventListener("click", sendMessage);
  }

  // ---------------------------------------------------------------------
  // Dictee vocale (Web Speech API du navigateur, aucune dependance externe).
  // LIMITE HONNETE : cette API n'est pas standardisee partout (Chrome/Edge
  // via webkitSpeechRecognition ; Firefox desktop ne l'implemente pas a ce
  // jour) -- si absente, le bouton reste visible mais desactive avec une
  // infobulle claire, jamais une fausse promesse de fonctionnalite.
  // ---------------------------------------------------------------------

  const DICTATION_LANG_MAP = { fr: "fr-FR", en: "en-US", ar: "ar-SA" };

  function setDictateVisualState(stateName) {
    dom.dictateBtn.classList.remove("dictate-listening", "dictate-processing", "dictate-error");
    if (stateName) dom.dictateBtn.classList.add(`dictate-${stateName}`);
  }

  function insertDictatedText(text) {
    const clean = (text || "").trim();
    if (!clean) return;
    const textarea = dom.composerTextarea;
    const start = textarea.selectionStart != null ? textarea.selectionStart : textarea.value.length;
    const end = textarea.selectionEnd != null ? textarea.selectionEnd : textarea.value.length;
    const before = textarea.value.slice(0, start);
    const after = textarea.value.slice(end);
    // N'efface JAMAIS un texte deja present : ajoute la transcription au
    // point d'insertion (curseur), avec un espace de separation seulement
    // si le texte existant n'en a pas deja un a cet endroit.
    const needsSpaceBefore = before.length > 0 && !/\s$/.test(before);
    const insertion = (needsSpaceBefore ? " " : "") + clean;
    textarea.value = before + insertion + after;
    const cursor = before.length + insertion.length;
    textarea.setSelectionRange(cursor, cursor);
    autoResize();
  }

  function wireDictation() {
    const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognitionCtor) {
      dom.dictateBtn.disabled = true;
      dom.dictateBtn.title = t("workspace.dictate_not_supported");
      return;
    }

    const recognition = new SpeechRecognitionCtor();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = DICTATION_LANG_MAP[document.documentElement.lang] || "fr-FR";
    document.addEventListener("agentstage:langchange", () => {
      recognition.lang = DICTATION_LANG_MAP[document.documentElement.lang] || "fr-FR";
    });

    recognition.onresult = (event) => {
      setDictateVisualState("listening");
      // Seuls les resultats FINAUX sont inseres dans l'entry : un resultat
      // provisoire ("interim") peut encore changer, l'y inserer risquerait
      // d'ecraser une modification manuelle de l'utilisateur en cours de
      // frappe (exigence explicite : il doit pouvoir corriger avant d'envoyer).
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (result.isFinal) insertDictatedText(result[0].transcript);
      }
    };

    recognition.onspeechend = () => {
      // Etat transitoire "traitement" : la reconnaissance continue peut
      // encore livrer un dernier resultat final juste apres la fin de la
      // parole detectee, avant de repasser en ecoute ou de s'arreter.
      if (state.dictation.active) setDictateVisualState("processing");
    };

    recognition.onerror = (event) => {
      state.dictation.active = false;
      setDictateVisualState("error");
      const messageKey =
        event.error === "not-allowed" || event.error === "service-not-allowed"
          ? "workspace.dictate_mic_denied"
          : "workspace.dictate_error";
      showComposerError(t(messageKey));
      setTimeout(() => setDictateVisualState(null), 1800);
    };

    recognition.onend = () => {
      if (state.dictation.active) {
        // Arret spontane (silence prolonge, limite du navigateur) sans que
        // l'utilisateur ait clique pour arreter : relance pour que le mode
        // "continu" le soit reellement.
        try {
          recognition.start();
          return;
        } catch (error) {
          /* deja demarree, ou navigateur qui refuse : retombe en inactif */
        }
      }
      state.dictation.active = false;
      setDictateVisualState(null);
    };

    dom.dictateBtn.addEventListener("click", () => {
      if (state.dictation.active) {
        state.dictation.active = false;
        setDictateVisualState(null);
        recognition.stop();
        return;
      }
      try {
        recognition.start();
        state.dictation.active = true;
        setDictateVisualState("listening");
      } catch (error) {
        setDictateVisualState("error");
        showComposerError(t("workspace.dictate_error"));
        setTimeout(() => setDictateVisualState(null), 1800);
      }
    });

    state.dictation.recognition = recognition;
    state.dictation.supported = true;
  }

  // ---------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------

  // ---------------------------------------------------------------------
  // Notifications (mission notifications, Phase 6) : mentions + reponses,
  // persistees cote serveur + poussees en temps reel sur un canal PAR
  // UTILISATEUR (independant de la conversation actuellement ouverte, voir
  // connectNotificationsRealtime ci-dessous -- meme principe que
  // connectRealtime mais un seul flux ouvert pour toute la session, pas un
  // par conversation).
  // ---------------------------------------------------------------------

  function updateNotificationsBadge(count) {
    dom.notificationsBadge.textContent = String(count);
    dom.notificationsBadge.classList.toggle("hidden", !count);
  }

  async function refreshNotificationsBadge() {
    const { ok, data } = await api("/notifications?limit=1");
    if (ok && data.ok) updateNotificationsBadge(data.unreadCount || 0);
  }

  function notificationRowLabel(notification) {
    const key = notification.type === "mention" ? "workspace.notification_mention" : "workspace.notification_reply";
    return t(key).replace("{name}", notification.actorName || "");
  }

  async function renderNotificationsList(listBox) {
    listBox.innerHTML = "";
    const { ok, data } = await api("/notifications?limit=20");
    if (!ok || !data.ok) return;
    updateNotificationsBadge(data.unreadCount || 0);
    if (!data.notifications.length) {
      listBox.appendChild(el("div", { class: "notifications-empty", text: t("workspace.notifications_empty") }));
      return;
    }
    data.notifications.forEach((notification) => {
      const row = el(
        "button",
        { type: "button", class: `notification-row ${notification.readAt ? "" : "unread"}` },
        [
          el("span", { class: "notification-row-title", text: notificationRowLabel(notification) }),
          el("span", { class: "notification-row-preview", text: notification.previewText || "" }),
          el("span", { class: "notification-row-time", text: formatDate(notification.createdAt) }),
        ]
      );
      row.addEventListener("click", async () => {
        if (!notification.readAt) {
          await api(`/notifications/${notification.id}/read`, { method: "POST" });
        }
        closeContextMenu();
        if (notification.conversationId) openConversation(notification.conversationId);
      });
      listBox.appendChild(row);
    });
  }

  function openNotificationsMenu() {
    closeContextMenu();
    // Correctif trouve en test E2E : PAS "open-up" ici -- ce menu est
    // ancre a la cloche du bandeau superieur (topbar), tout en haut de
    // l'ecran ; "open-up" (pense pour les menus proches du bas, ex.
    // connexions dans le composer) le faisait s'ouvrir hors ecran vers le
    // haut (top negatif). Ouverture par defaut (top:100%, vers le bas).
    const menu = el("div", { class: "item-context-menu open notifications-menu" });
    const header = el("div", { class: "notifications-menu-header" }, [
      el("h3", { text: t("workspace.notifications_title") }),
      el("button", {
        type: "button",
        class: "link-btn",
        text: t("workspace.notifications_mark_all_read"),
        onclick: async (event) => {
          event.stopPropagation();
          await api("/notifications/read-all", { method: "POST" });
          renderNotificationsList(listBox);
        },
      }),
    ]);
    const listBox = el("div", { class: "notifications-list" });
    menu.appendChild(header);
    menu.appendChild(listBox);
    dom.notificationsBtn.parentElement.appendChild(menu);
    activeContextMenu = menu;
    setTimeout(() => document.addEventListener("click", closeContextMenu, { once: true }), 0);
    renderNotificationsList(listBox);
  }

  function wireNotifications() {
    dom.notificationsBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      openNotificationsMenu();
    });
    refreshNotificationsBadge();
  }

  function showNotificationToast(notification) {
    const toast = el("div", { class: "notification-toast" }, [
      el("span", { class: "notification-toast-title", text: notificationRowLabel(notification) }),
      el("span", { class: "notification-toast-preview", text: notification.previewText || "" }),
    ]);
    toast.addEventListener("click", () => {
      if (notification.conversationId) openConversation(notification.conversationId);
      toast.remove();
    });
    dom.notificationToasts.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("visible"));
    setTimeout(() => {
      toast.classList.remove("visible");
      setTimeout(() => toast.remove(), 250);
    }, 4500);
  }

  let notificationsEventSource = null;

  function connectNotificationsRealtime() {
    if (notificationsEventSource) notificationsEventSource.close();
    notificationsEventSource = new EventSource(`${API}/notifications/events`, { withCredentials: true });
    notificationsEventSource.addEventListener("notification.created", (event) => {
      let notification;
      try {
        notification = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      refreshNotificationsBadge();
      showNotificationToast({
        type: notification.type,
        actorName: notification.actorName,
        previewText: notification.previewText,
        conversationId: notification.conversationId,
      });
    });
  }

  // ---------------------------------------------------------------------
  // Documentation / RAG (mission RAG, Phase 4/7) : bibliotheque des
  // documents auxquels l'utilisateur courant a acces -- meme filtre
  // d'autorisation que la recuperation RAG elle-meme (cote serveur, voir
  // librairies/rag.py). Metadonnees uniquement, jamais le contenu complet
  // (mission §18, "ne jamais charger inutilement tous les fichiers").
  // ---------------------------------------------------------------------

  const docsState = { search: "", before: null, loading: false };

  function docStatusLabel(status) {
    const key = {
      UPLOADED: "workspace.documentation_status_uploaded",
      PROCESSING: "workspace.documentation_status_processing",
      READY: "workspace.documentation_status_ready",
      FAILED: "workspace.documentation_status_failed",
    }[status];
    return key ? t(key) : status;
  }

  function docIconSvg() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "18");
    svg.setAttribute("height", "18");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("class", "doc-row-icon");
    svg.innerHTML = '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>';
    return svg;
  }

  function renderDocumentRow(doc) {
    const row = el("div", { class: "doc-row" }, [
      docIconSvg(),
      el("div", { class: "doc-row-main" }, [
        el("div", { class: "doc-row-name", text: doc.name, title: doc.name }),
        el("div", { class: "doc-row-meta", text: formatDate(doc.createdAt) }),
      ]),
      el("span", { class: `doc-row-status status-${doc.status}`, text: docStatusLabel(doc.status) }),
    ]);
    const actions = el("div", { class: "doc-row-actions" });
    actions.appendChild(
      el("button", {
        type: "button",
        class: "link-btn",
        text: t("workspace.documentation_open"),
        onclick: () => window.open(`${API}/files/${doc.id}`, "_blank"),
      })
    );
    // canManage : decision produit explicite (demandee directement) --
    // quiconque peut VOIR un document dans sa Documentation peut aussi le
    // gerer (renommer/reindexer/supprimer), sans notion de proprietaire ni
    // de role admin special. Calcule cote serveur (toujours True pour une
    // ligne listee ici, cf. rag.list_visible_documents/user_can_manage_document)
    // -- jamais une deuxieme regle derivee ici.
    if (doc.canManage) {
      actions.appendChild(
        el("button", {
          type: "button",
          class: "link-btn",
          text: t("workspace.documentation_reindex"),
          onclick: async (event) => {
            event.target.disabled = true;
            await api(`/documents/${doc.id}/reindex`, { method: "POST" });
            loadDocuments(true);
          },
        })
      );
      actions.appendChild(
        el("button", {
          type: "button",
          class: "link-btn danger-text",
          text: t("workspace.delete"),
          onclick: async () => {
            if (!window.confirm(t("workspace.documentation_delete_confirm").replace("{name}", doc.name))) return;
            const { ok, data } = await api(`/documents/${doc.id}`, { method: "DELETE" });
            if (ok && data.ok) loadDocuments(true);
          },
        })
      );
    }
    row.appendChild(actions);
    return row;
  }

  async function loadDocuments(reset) {
    if (docsState.loading) return;
    docsState.loading = true;
    if (reset) {
      docsState.before = null;
      dom.docsList.innerHTML = "";
    }
    const params = new URLSearchParams({ limit: "30" });
    if (docsState.search) params.set("search", docsState.search);
    if (docsState.before) params.set("before", docsState.before);
    const { ok, data } = await api(`/documents?${params.toString()}`);
    docsState.loading = false;
    if (!ok || !data.ok) return;
    // NB : le fichier ELEMENT (doc.id) reste attache a son message d'origine
    // dans la discussion -- ceci n'est qu'un listing metadonnees, jamais un
    // chargement du contenu complet (mission §18).
    data.documents.forEach((doc) => dom.docsList.appendChild(renderDocumentRow(doc)));
    dom.docsLoadMore.classList.toggle("hidden", data.documents.length < 30);
    if (data.documents.length) {
      docsState.before = data.documents[data.documents.length - 1].createdAt;
    }
  }

  let docsSearchDebounce = null;

  function wireDocumentation() {
    dom.documentationEntry.addEventListener("click", () => {
      dom.docsModalOverlay.classList.remove("hidden");
      loadDocuments(true);
    });
    dom.docsModalClose.addEventListener("click", () => dom.docsModalOverlay.classList.add("hidden"));
    dom.docsModalOverlay.addEventListener("click", (event) => {
      if (event.target === dom.docsModalOverlay) dom.docsModalOverlay.classList.add("hidden");
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !dom.docsModalOverlay.classList.contains("hidden")) {
        dom.docsModalOverlay.classList.add("hidden");
      }
    });
    dom.docsSearchInput.addEventListener("input", () => {
      clearTimeout(docsSearchDebounce);
      docsSearchDebounce = setTimeout(() => {
        docsState.search = dom.docsSearchInput.value.trim();
        loadDocuments(true);
      }, 300);
    });
    dom.docsLoadMore.addEventListener("click", () => loadDocuments(false));
    dom.docsUploadBtn.addEventListener("click", () => dom.docsUploadInput.click());
    dom.docsUploadInput.addEventListener("change", async (event) => {
      const file = event.target.files[0];
      dom.docsUploadInput.value = "";
      if (!file) return;
      const formData = new FormData();
      formData.append("file", file);
      const { ok, data } = await api("/documents", { method: "POST", body: formData });
      if (ok && data.ok) loadDocuments(true);
      else showComposerError(data && data.error ? data.error : t("workspace.error_generic"));
    });
  }

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
    wireDictation();
    wireGoogleDriveOption();
    wireNotifications();
    wireDocumentation();
    connectNotificationsRealtime();
    updateConnectionsBadge();
    handleGoogleDriveRedirectStatus();
    refreshGoogleDriveOption();
    handleConnectionsOAuthRedirectStatus();

    dom.newProjectBtn.addEventListener("click", () => dom.projectForm.classList.toggle("hidden"));
    dom.projectCancelBtn.addEventListener("click", () => {
      dom.projectForm.classList.add("hidden");
      dom.projectNameInput.value = "";
    });
    dom.projectCreateBtn.addEventListener("click", submitNewProject);
    dom.projectNameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") submitNewProject();
    });

    dom.newSharedProjectBtn.addEventListener("click", () => dom.sharedProjectForm.classList.toggle("hidden"));
    dom.sharedProjectCancelBtn.addEventListener("click", () => {
      dom.sharedProjectForm.classList.add("hidden");
      dom.sharedProjectNameInput.value = "";
      dom.sharedProjectDescriptionInput.value = "";
    });
    dom.sharedProjectCreateBtn.addEventListener("click", submitNewSharedProject);
    dom.sharedProjectNameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") submitNewSharedProject();
    });

    dom.projectsHeader.addEventListener("click", () => toggleSection(dom.projectsSection));
    dom.sharedProjectsHeader.addEventListener("click", () => toggleSection(dom.sharedProjectsSection));
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

    await Promise.all([
      loadProjects(),
      loadSharedProjects(),
      loadDiscussions(true),
      loadEntryActions(),
      loadSharedConversations(),
    ]);

    renderInitialQuestion();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
