// Traduction FR / EN / AR : persistance locale + application sur les
// elements marques [data-i18n], [data-i18n-placeholder] et [data-i18n-aria].
// Le nom "OCPAgentic" n'est jamais traduit : ce n'est qu'une image (logo.png)
// avec un attribut alt fixe, jamais du texte gere par ce systeme.
(function () {
  const STORAGE_KEY = "agentStageLang";
  const DEFAULT_LANG = "fr";
  const SUPPORTED = ["fr", "en", "ar"];

  const DICTIONARIES = {
    fr: {
      "theme.toggle": "Mode sombre",
      "login.title_tag": "Connexion",
      "login.heading": "Connexion",
      "login.identifier_label": "Identifiant",
      "login.identifier_placeholder": "Saisissez votre identifiant",
      "login.password_label": "Mot de passe",
      "login.password_placeholder": "Saisissez votre mot de passe",
      "login.show_password": "Afficher le mot de passe",
      "login.hide_password": "Masquer le mot de passe",
      "login.btn_login": "Se connecter",
      "login.forgot_link": "Mot de passe oublié ?",
      "login.or": "ou",
      "login.btn_create": "Créer un compte",
      "login.err_fill": "Remplis l'identifiant et le mot de passe.",
      "login.err_server": "Impossible de contacter le serveur.",
      "login.err_login_generic": "Connexion impossible.",
      "login.err_email_required": "Pour créer un compte, l'identifiant doit être une adresse mail.",
      "login.err_exists": "Cet identifiant ou ce mot de passe existe déjà.",
      "login.err_create_generic": "Création du compte impossible.",

      "forgot.title_tag": "Mot de passe oublié",
      "forgot.heading": "Mot de passe oublié",
      "forgot.subtitle": "Indique ton adresse email, on t'envoie un lien pour choisir un nouveau mot de passe.",
      "forgot.email_label": "Adresse email",
      "forgot.email_placeholder": "Saisissez votre adresse email",
      "forgot.btn_send": "Envoyer le lien",
      "forgot.back_link": "Retour à la connexion",
      "forgot.err_email_required": "Renseigne ton adresse email.",
      "forgot.err_server": "Impossible de contacter le serveur.",
      "forgot.success_generic": "Si un compte existe avec cette adresse, un email de réinitialisation a été envoyé.",

      "reset.title_tag": "Nouveau mot de passe",
      "reset.heading": "Nouveau mot de passe",
      "reset.new_password_label": "Nouveau mot de passe",
      "reset.new_password_placeholder": "Au moins 8 caractères",
      "reset.confirm_label": "Confirmer le mot de passe",
      "reset.confirm_placeholder": "Ressaisissez le mot de passe",
      "reset.btn_submit": "Valider le nouveau mot de passe",
      "reset.back_link": "Retour à la connexion",
      "reset.err_invalid_link": "Ce lien de réinitialisation est invalide.",
      "reset.err_fill": "Renseigne et confirme le nouveau mot de passe.",
      "reset.err_mismatch": "Les deux mots de passe ne correspondent pas.",
      "reset.err_too_short": "Le mot de passe doit contenir au moins 8 caractères.",
      "reset.err_server": "Impossible de contacter le serveur.",
      "reset.err_generic": "Impossible de réinitialiser le mot de passe.",
      "reset.success": "Mot de passe mis à jour. Tu peux maintenant te connecter.",

      "workspace.projects": "Projets",
      "workspace.new_project": "+ Nouveau projet",
      "workspace.project_name_placeholder": "Nom du projet...",
      "workspace.cancel": "Annuler",
      "workspace.create": "Créer",
      "workspace.discussions": "Discussions",
      "workspace.new_discussion": "+ Nouvelle discussion",
      "workspace.initial_question": "Quelle est votre demande aujourd'hui ?",
      "workspace.entry_placeholder": "Saisissez votre demande...",
      "workspace.add_to_project": "Ajouter à un projet",
      "workspace.no_projects": "Aucun projet",
      "workspace.reuse": "Réutiliser",
      "workspace.drop_here": "Déposez votre document ici",
      "workspace.attach_file": "Joindre un fichier",
      "workspace.retry": "Réessayer",
      "workspace.remove": "Retirer",
      "workspace.loading": "Génération en cours...",
      "workspace.error_timeout": "Le workflow n'a pas répondu à temps.",
      "workspace.error_network": "Impossible de contacter le serveur.",
      "workspace.error_generic": "Une erreur est survenue.",
      "workspace.error_not_configured": "Cette fonctionnalité IA n'est pas encore configurée.",
      "workspace.error_empty_message": "Écris un message ou joins un document.",
      "workspace.load_more": "Charger plus",
      "workspace.uploading": "Envoi...",
      "workspace.upload_failed": "Échec de l'envoi",
      "workspace.profile_logout": "Se déconnecter",
      "workspace.action_analyze_document": "Analyser",
      "workspace.action_summarize_document": "Résumer",
      "workspace.action_compare_documents": "Comparer",
      "workspace.action_extract_data": "Extraire les données",
      "workspace.action_generate_chart": "Créer graphiques",
      "workspace.action_generate_report": "Générer rapport",
      "workspace.actions_label": "Actions",
      "workspace.send": "Envoyer",
      "workspace.sidebar_open": "Ouvrir le menu",
      "workspace.sidebar_close": "Fermer le menu",
      "workspace.conversation_menu": "Options de la conversation",
      "workspace.empty_history": "Aucune conversation pour l'instant.",
      "workspace.source_result": "Résultat source",
      "workspace.delete": "Supprimer",
      "workspace.more_options": "Plus d'options",
      "workspace.nickname_placeholder": "Nouveau pseudonyme...",
      "workspace.opt_avatar": "Mettre une photo de profil",
      "workspace.opt_delete_account": "Supprimer le compte",
      "workspace.opt_nickname": "Changer de pseudonyme",
      "workspace.remove_nickname": "Supprimer le pseudonyme",
      "workspace.save": "Enregistrer",
      "workspace.confirm_delete_account": "Êtes-vous sûr de vouloir supprimer votre compte ?",
      "workspace.confirm_delete_conversation": "Êtes-vous sûr de vouloir supprimer cette conversation ?",
      "workspace.confirm_delete_project": "Êtes-vous sûr de vouloir supprimer ce projet ?",
      "workspace.delete_conversation": "Supprimer la conversation",
      "workspace.delete_project": "Supprimer le projet",
      "workspace.no_permission": "Action non autorisée.",
      "workspace.rename_project": "Renommer le projet",
      "workspace.rename_file": "Renommer le fichier",
      "workspace.rename_conversation": "Renommer la conversation",
      "workspace.action_menu": "Options du bouton",
      "workspace.add_action_button": "+ Ajouter un bouton",
      "workspace.choose_action_button": "Choisir un bouton",
      "workspace.action_button_name": "Nom du bouton",
      "workspace.action_button_name_placeholder": "Ex : Analyser PDF",
      "workspace.creating": "Création...",
      "workspace.search_actions_placeholder": "Rechercher un bouton...",
      "workspace.no_actions_found": "Aucun bouton trouvé.",
      "workspace.rename": "Renommer",
      "workspace.open_in_n8n": "Ouvrir dans n8n",
      "workspace.delete_button_definitively": "Supprimer définitivement",
    },
    en: {
      "theme.toggle": "Dark mode",
      "login.title_tag": "Login",
      "login.heading": "Login",
      "login.identifier_label": "Username",
      "login.identifier_placeholder": "Enter your username",
      "login.password_label": "Password",
      "login.password_placeholder": "Enter your password",
      "login.show_password": "Show password",
      "login.hide_password": "Hide password",
      "login.btn_login": "Log in",
      "login.forgot_link": "Forgot password?",
      "login.or": "or",
      "login.btn_create": "Create an account",
      "login.err_fill": "Fill in the username and password.",
      "login.err_server": "Unable to reach the server.",
      "login.err_login_generic": "Login failed.",
      "login.err_email_required": "To create an account, the username must be an email address.",
      "login.err_exists": "This username or password already exists.",
      "login.err_create_generic": "Account creation failed.",

      "forgot.title_tag": "Forgot password",
      "forgot.heading": "Forgot password",
      "forgot.subtitle": "Enter your email address, we'll send you a link to choose a new password.",
      "forgot.email_label": "Email address",
      "forgot.email_placeholder": "Enter your email address",
      "forgot.btn_send": "Send the link",
      "forgot.back_link": "Back to login",
      "forgot.err_email_required": "Enter your email address.",
      "forgot.err_server": "Unable to reach the server.",
      "forgot.success_generic": "If an account exists with this address, a reset email has been sent.",

      "reset.title_tag": "New password",
      "reset.heading": "New password",
      "reset.new_password_label": "New password",
      "reset.new_password_placeholder": "At least 8 characters",
      "reset.confirm_label": "Confirm password",
      "reset.confirm_placeholder": "Re-enter the password",
      "reset.btn_submit": "Confirm new password",
      "reset.back_link": "Back to login",
      "reset.err_invalid_link": "This reset link is invalid.",
      "reset.err_fill": "Enter and confirm the new password.",
      "reset.err_mismatch": "The two passwords do not match.",
      "reset.err_too_short": "The password must contain at least 8 characters.",
      "reset.err_server": "Unable to reach the server.",
      "reset.err_generic": "Unable to reset the password.",
      "reset.success": "Password updated. You can now log in.",

      "workspace.projects": "Projects",
      "workspace.new_project": "+ New project",
      "workspace.project_name_placeholder": "Project name...",
      "workspace.cancel": "Cancel",
      "workspace.create": "Create",
      "workspace.discussions": "Discussions",
      "workspace.new_discussion": "+ New discussion",
      "workspace.initial_question": "What's your request today?",
      "workspace.entry_placeholder": "Enter your request...",
      "workspace.add_to_project": "Add to a project",
      "workspace.no_projects": "No projects",
      "workspace.reuse": "Reuse",
      "workspace.drop_here": "Drop your document here",
      "workspace.attach_file": "Attach a file",
      "workspace.retry": "Retry",
      "workspace.remove": "Remove",
      "workspace.loading": "Generating...",
      "workspace.error_timeout": "The workflow did not respond in time.",
      "workspace.error_network": "Unable to reach the server.",
      "workspace.error_generic": "Something went wrong.",
      "workspace.error_not_configured": "This AI feature isn't configured yet.",
      "workspace.error_empty_message": "Write a message or attach a document.",
      "workspace.load_more": "Load more",
      "workspace.uploading": "Uploading...",
      "workspace.upload_failed": "Upload failed",
      "workspace.profile_logout": "Log out",
      "workspace.action_analyze_document": "Analyze",
      "workspace.action_summarize_document": "Summarize",
      "workspace.action_compare_documents": "Compare",
      "workspace.action_extract_data": "Extract data",
      "workspace.action_generate_chart": "Create charts",
      "workspace.action_generate_report": "Generate report",
      "workspace.actions_label": "Actions",
      "workspace.send": "Send",
      "workspace.sidebar_open": "Open sidebar",
      "workspace.sidebar_close": "Close sidebar",
      "workspace.conversation_menu": "Conversation options",
      "workspace.empty_history": "No conversations yet.",
      "workspace.source_result": "Source result",
      "workspace.delete": "Delete",
      "workspace.more_options": "More options",
      "workspace.nickname_placeholder": "New nickname...",
      "workspace.opt_avatar": "Set a profile picture",
      "workspace.opt_delete_account": "Delete account",
      "workspace.opt_nickname": "Change nickname",
      "workspace.remove_nickname": "Remove nickname",
      "workspace.save": "Save",
      "workspace.confirm_delete_account": "Are you sure you want to delete your account?",
      "workspace.confirm_delete_conversation": "Are you sure you want to delete this conversation?",
      "workspace.confirm_delete_project": "Are you sure you want to delete this project?",
      "workspace.delete_conversation": "Delete conversation",
      "workspace.delete_project": "Delete project",
      "workspace.no_permission": "Not allowed.",
      "workspace.rename_project": "Rename project",
      "workspace.rename_file": "Rename file",
      "workspace.rename_conversation": "Rename conversation",
      "workspace.action_menu": "Button options",
      "workspace.add_action_button": "+ Add a button",
      "workspace.choose_action_button": "Choose a button",
      "workspace.action_button_name": "Button name",
      "workspace.action_button_name_placeholder": "e.g. Analyze PDF",
      "workspace.creating": "Creating...",
      "workspace.search_actions_placeholder": "Search a button...",
      "workspace.no_actions_found": "No button found.",
      "workspace.rename": "Rename",
      "workspace.open_in_n8n": "Open in n8n",
      "workspace.delete_button_definitively": "Delete permanently",
    },
    ar: {
      "theme.toggle": "الوضع الداكن",
      "login.title_tag": "تسجيل الدخول",
      "login.heading": "تسجيل الدخول",
      "login.identifier_label": "المعرف",
      "login.identifier_placeholder": "أدخل معرفك",
      "login.password_label": "كلمة المرور",
      "login.password_placeholder": "أدخل كلمة المرور",
      "login.show_password": "إظهار كلمة المرور",
      "login.hide_password": "إخفاء كلمة المرور",
      "login.btn_login": "تسجيل الدخول",
      "login.forgot_link": "هل نسيت كلمة المرور؟",
      "login.or": "أو",
      "login.btn_create": "إنشاء حساب",
      "login.err_fill": "املأ المعرف وكلمة المرور.",
      "login.err_server": "تعذر الاتصال بالخادم.",
      "login.err_login_generic": "تعذر تسجيل الدخول.",
      "login.err_email_required": "لإنشاء حساب، يجب أن يكون المعرف عنوان بريد إلكتروني.",
      "login.err_exists": "هذا المعرف أو كلمة المرور موجود بالفعل.",
      "login.err_create_generic": "تعذر إنشاء الحساب.",

      "forgot.title_tag": "نسيت كلمة المرور",
      "forgot.heading": "نسيت كلمة المرور",
      "forgot.subtitle": "أدخل عنوان بريدك الإلكتروني، سنرسل لك رابطًا لاختيار كلمة مرور جديدة.",
      "forgot.email_label": "عنوان البريد الإلكتروني",
      "forgot.email_placeholder": "أدخل عنوان بريدك الإلكتروني",
      "forgot.btn_send": "إرسال الرابط",
      "forgot.back_link": "العودة إلى تسجيل الدخول",
      "forgot.err_email_required": "أدخل عنوان بريدك الإلكتروني.",
      "forgot.err_server": "تعذر الاتصال بالخادم.",
      "forgot.success_generic": "إذا كان هناك حساب مرتبط بهذا العنوان، فقد تم إرسال بريد إلكتروني لإعادة التعيين.",

      "reset.title_tag": "كلمة مرور جديدة",
      "reset.heading": "كلمة مرور جديدة",
      "reset.new_password_label": "كلمة المرور الجديدة",
      "reset.new_password_placeholder": "8 أحرف على الأقل",
      "reset.confirm_label": "تأكيد كلمة المرور",
      "reset.confirm_placeholder": "أعد إدخال كلمة المرور",
      "reset.btn_submit": "تأكيد كلمة المرور الجديدة",
      "reset.back_link": "العودة إلى تسجيل الدخول",
      "reset.err_invalid_link": "رابط إعادة التعيين هذا غير صالح.",
      "reset.err_fill": "أدخل كلمة المرور الجديدة وأكدها.",
      "reset.err_mismatch": "كلمتا المرور غير متطابقتين.",
      "reset.err_too_short": "يجب أن تحتوي كلمة المرور على 8 أحرف على الأقل.",
      "reset.err_server": "تعذر الاتصال بالخادم.",
      "reset.err_generic": "تعذر إعادة تعيين كلمة المرور.",
      "reset.success": "تم تحديث كلمة المرور. يمكنك الآن تسجيل الدخول.",

      "workspace.projects": "المشاريع",
      "workspace.new_project": "+ مشروع جديد",
      "workspace.project_name_placeholder": "اسم المشروع...",
      "workspace.cancel": "إلغاء",
      "workspace.create": "إنشاء",
      "workspace.discussions": "المحادثات",
      "workspace.new_discussion": "+ محادثة جديدة",
      "workspace.initial_question": "ما طلبك اليوم؟",
      "workspace.entry_placeholder": "أدخل طلبك...",
      "workspace.add_to_project": "إضافة إلى مشروع",
      "workspace.no_projects": "لا توجد مشاريع",
      "workspace.reuse": "إعادة استخدام",
      "workspace.drop_here": "أسقط مستندك هنا",
      "workspace.attach_file": "إرفاق ملف",
      "workspace.retry": "إعادة المحاولة",
      "workspace.remove": "إزالة",
      "workspace.loading": "جارٍ الإنشاء...",
      "workspace.error_timeout": "لم يستجب سير العمل في الوقت المحدد.",
      "workspace.error_network": "تعذر الاتصال بالخادم.",
      "workspace.error_generic": "حدث خطأ ما.",
      "workspace.error_not_configured": "لم يتم تكوين ميزة الذكاء الاصطناعي هذه بعد.",
      "workspace.error_empty_message": "اكتب رسالة أو أرفق مستندًا.",
      "workspace.load_more": "تحميل المزيد",
      "workspace.uploading": "جارٍ الرفع...",
      "workspace.upload_failed": "فشل الرفع",
      "workspace.profile_logout": "تسجيل الخروج",
      "workspace.action_analyze_document": "تحليل",
      "workspace.action_summarize_document": "تلخيص",
      "workspace.action_compare_documents": "مقارنة",
      "workspace.action_extract_data": "استخراج البيانات",
      "workspace.action_generate_chart": "إنشاء رسوم بيانية",
      "workspace.action_generate_report": "إنشاء تقرير",
      "workspace.actions_label": "الإجراءات",
      "workspace.send": "إرسال",
      "workspace.sidebar_open": "فتح الشريط الجانبي",
      "workspace.sidebar_close": "إغلاق الشريط الجانبي",
      "workspace.conversation_menu": "خيارات المحادثة",
      "workspace.empty_history": "لا توجد محادثات بعد.",
      "workspace.source_result": "النتيجة المصدر",
      "workspace.delete": "حذف",
      "workspace.more_options": "المزيد من الخيارات",
      "workspace.nickname_placeholder": "اسم مستعار جديد...",
      "workspace.opt_avatar": "تعيين صورة الملف الشخصي",
      "workspace.opt_delete_account": "حذف الحساب",
      "workspace.opt_nickname": "تغيير الاسم المستعار",
      "workspace.remove_nickname": "إزالة الاسم المستعار",
      "workspace.save": "حفظ",
      "workspace.confirm_delete_account": "هل أنت متأكد من رغبتك في حذف حسابك؟",
      "workspace.confirm_delete_conversation": "هل أنت متأكد من رغبتك في حذف هذه المحادثة؟",
      "workspace.confirm_delete_project": "هل أنت متأكد من رغبتك في حذف هذا المشروع؟",
      "workspace.delete_conversation": "حذف المحادثة",
      "workspace.delete_project": "حذف المشروع",
      "workspace.no_permission": "غير مسموح.",
      "workspace.rename_project": "إعادة تسمية المشروع",
      "workspace.rename_file": "إعادة تسمية الملف",
      "workspace.rename_conversation": "إعادة تسمية المحادثة",
      "workspace.action_menu": "خيارات الزر",
      "workspace.add_action_button": "+ إضافة زر",
      "workspace.choose_action_button": "اختيار زر",
      "workspace.action_button_name": "اسم الزر",
      "workspace.action_button_name_placeholder": "مثال: تحليل PDF",
      "workspace.creating": "جارٍ الإنشاء...",
      "workspace.search_actions_placeholder": "ابحث عن زر...",
      "workspace.no_actions_found": "لم يتم العثور على أي زر.",
      "workspace.rename": "إعادة تسمية",
      "workspace.open_in_n8n": "فتح في n8n",
      "workspace.delete_button_definitively": "حذف نهائي",
    },
  };

  // Vocabulaire fixe des messages renvoyes par le backend (toujours en
  // francais cote serveur) : traduits cote client par correspondance exacte.
  const SERVER_MESSAGES = {
    "Identifiant et mot de passe requis.": {
      en: "Username and password are required.",
      ar: "الاسم وكلمة المرور مطلوبان.",
    },
    "Identifiant ou mot de passe incorrect.": {
      en: "Incorrect username or password.",
      ar: "المعرف أو كلمة المرور غير صحيحة.",
    },
    "L'identifiant doit contenir au moins 3 caracteres.": {
      en: "The username must contain at least 3 characters.",
      ar: "يجب أن يحتوي المعرف على 3 أحرف على الأقل.",
    },
    "Adresse email invalide.": {
      en: "Invalid email address.",
      ar: "عنوان البريد الإلكتروني غير صالح.",
    },
    "Le mot de passe doit contenir au moins 8 caracteres.": {
      en: "The password must contain at least 8 characters.",
      ar: "يجب أن تحتوي كلمة المرور على 8 أحرف على الأقل.",
    },
    "Cet identifiant ou cette adresse email existe deja.": {
      en: "This username or email address already exists.",
      ar: "هذا المعرف أو عنوان البريد الإلكتروني موجود بالفعل.",
    },
    "Requete invalide.": {
      en: "Invalid request.",
      ar: "طلب غير صالح.",
    },
    "Ce lien de reinitialisation est invalide ou a expire.": {
      en: "This reset link is invalid or has expired.",
      ar: "رابط إعادة التعيين هذا غير صالح أو منتهي الصلاحية.",
    },
    "Erreur serveur.": {
      en: "Server error.",
      ar: "خطأ في الخادم.",
    },
    "Trop de tentatives, reessayez plus tard.": {
      en: "Too many attempts, try again later.",
      ar: "عدد كبير جدًا من المحاولات، حاول مرة أخرى لاحقًا.",
    },
    "Non authentifie.": {
      en: "Not authenticated.",
      ar: "غير مصادق عليه.",
    },
    "Route inconnue.": {
      en: "Unknown route.",
      ar: "مسار غير معروف.",
    },
  };

  function readStoredLang() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (error) {
      return null;
    }
  }

  function storeLang(lang) {
    try {
      localStorage.setItem(STORAGE_KEY, lang);
    } catch (error) {
      /* stockage indisponible : on continue sans persister */
    }
  }

  function currentLang() {
    const stored = readStoredLang();
    return SUPPORTED.includes(stored) ? stored : DEFAULT_LANG;
  }

  function t(key) {
    const dict = DICTIONARIES[currentLang()] || DICTIONARIES[DEFAULT_LANG];
    return dict[key] || DICTIONARIES[DEFAULT_LANG][key] || key;
  }

  function translateServerText(text) {
    const entry = SERVER_MESSAGES[text];
    const lang = currentLang();
    if (!entry || lang === "fr") return text;
    return entry[lang] || text;
  }

  function applyTranslations() {
    const lang = currentLang();
    document.documentElement.setAttribute("lang", lang);
    document.documentElement.setAttribute("dir", lang === "ar" ? "rtl" : "ltr");

    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
    });
    document.querySelectorAll("[data-i18n-aria]").forEach((el) => {
      el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria")));
    });

    if (window.AGENT_STAGE_TITLE_KEY) {
      document.title = t(window.AGENT_STAGE_TITLE_KEY);
    }

    const select = document.getElementById("lang-select");
    if (select) select.value = lang;

    document.dispatchEvent(new CustomEvent("agentstage:langchange", { detail: { lang } }));
  }

  function setLanguage(lang) {
    if (!SUPPORTED.includes(lang)) return;
    storeLang(lang);
    applyTranslations();
  }

  document.addEventListener("DOMContentLoaded", () => {
    applyTranslations();
    const select = document.getElementById("lang-select");
    if (select) {
      select.addEventListener("change", (event) => setLanguage(event.target.value));
    }
  });

  window.AgentStageI18N = { t, translateServerText, setLanguage, currentLang };
})();
