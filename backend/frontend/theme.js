// Mode sombre : persistance locale + application sur <html data-theme="...">
(function () {
  const STORAGE_KEY = "agentStageTheme";

  function readStoredTheme() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (error) {
      return null;
    }
  }

  function storeTheme(theme) {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (error) {
      /* stockage indisponible (navigation privee, etc.) : on continue sans persister */
    }
  }

  function currentTheme() {
    return readStoredTheme() === "dark" ? "dark" : "light";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    }
    // Le mot "Agentic" est en noir dans le logo : une variante avec ce mot
    // en blanc (logo-dark.png) est utilisee en mode sombre pour rester lisible.
    document.querySelectorAll(".brand-logo").forEach((img) => {
      img.src = theme === "dark" ? "logo-dark.png" : "logo.png";
    });
  }

  function toggleTheme() {
    const next = currentTheme() === "dark" ? "light" : "dark";
    storeTheme(next);
    applyTheme(next);
  }

  applyTheme(currentTheme());

  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.addEventListener("click", toggleTheme);
    }
  });

  window.AgentStageTheme = { current: currentTheme, toggle: toggleTheme };
})();
