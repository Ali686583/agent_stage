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
