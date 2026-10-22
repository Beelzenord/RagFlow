// The topbar both pages share: theme, language, who is signed in, signing out,
// and - for admins only - the switch between Chat and the Dashboard.
//
// Loaded before the page's own script, which waits on Shell.session for the
// /api/me answer instead of asking again. Needs static/lang.js (in <head>).

(function () {
  const THEME_KEY = "rag.theme.v1";

  // Where to send the browser when the server stops recognising it. Overwritten
  // by /api/me, because with Microsoft sign-in the destination belongs to the
  // platform ("/.auth/login/aad") rather than to this app.
  let loginUrl = "/login";

  // The session can end mid-visit, after which every /api call answers 401.
  // Send the tab to the sign-in page instead of painting "HTTP 401" into the
  // page.
  function bounceIfLoggedOut(status) {
    if (status !== 401) return false;
    location.href = loginUrl;
    return true;
  }

  function getTheme() {
    try {
      return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
    } catch {
      return "dark";
    }
  }

  function applyTheme(theme) {
    const t = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
    const toggle = document.getElementById("theme-toggle");
    if (toggle) {
      const light = t === "light";
      toggle.setAttribute("aria-checked", light ? "true" : "false");
      toggle.setAttribute("aria-label", Lang.t(light ? "common.useDark" : "common.useLight"));
    }
  }

  function initTheme() {
    applyTheme(getTheme());
    const toggle = document.getElementById("theme-toggle");
    if (!toggle) return;
    toggle.addEventListener("click", () => {
      const next = getTheme() === "light" ? "dark" : "light";
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch {
        /* ignore */
      }
      applyTheme(next);
    });
  }

  // Built here rather than written into the HTML, so a reader's page carries
  // no trace of a dashboard: no hidden link, no markup to unhide.
  function addPageSwitch() {
    const topbar = document.querySelector(".topbar");
    if (!topbar || document.getElementById("page-switch")) return;
    const current = document.body.dataset.page === "dashboard" ? "dashboard" : "chat";
    const nav = document.createElement("nav");
    nav.id = "page-switch";
    nav.className = "page-switch";
    nav.setAttribute("data-i18n-aria-label", "nav.label");
    nav.setAttribute("aria-label", Lang.t("nav.label"));
    for (const [page, href, key] of [
      ["chat", "/", "nav.chat"],
      ["dashboard", "/dashboard", "nav.dashboard"],
    ]) {
      const a = document.createElement("a");
      a.href = href;
      // Marked like the static text, so a language switch relabels it too.
      a.dataset.i18n = key;
      a.textContent = Lang.t(key);
      if (page === current) a.setAttribute("aria-current", "page");
      nav.appendChild(a);
    }
    const anchor = document.getElementById("lang-select") || document.getElementById("theme-toggle");
    topbar.insertBefore(nav, anchor || null);
  }

  // Everyone gets this, signed in or not; each language is named in itself.
  function addLanguagePicker() {
    const topbar = document.querySelector(".topbar");
    if (!topbar || document.getElementById("lang-select")) return;
    const select = document.createElement("select");
    select.id = "lang-select";
    select.className = "lang-select";
    select.setAttribute("data-i18n-aria-label", "common.language");
    select.setAttribute("aria-label", Lang.t("common.language"));
    for (const code of Lang.supported) {
      const opt = document.createElement("option");
      opt.value = code;
      opt.lang = code;
      opt.textContent = Lang.names[code];
      select.appendChild(opt);
    }
    select.value = Lang.current;
    select.addEventListener("change", () => Lang.set(select.value));
    topbar.insertBefore(select, document.getElementById("theme-toggle") || null);
  }

  // The topbar cannot work out who is signed in on its own: with Microsoft
  // sign-in the identity arrives in a request header the page never sees, and
  // signing out has to go through the platform rather than this app.
  async function loadSession() {
    let info = {};
    try {
      const resp = await fetch("/api/me");
      if (resp.ok) info = await resp.json();
    } catch (err) {
      console.warn("session lookup failed", err);
    }

    if (info.login_url) loginUrl = info.login_url;

    const badge = document.getElementById("user-badge");
    if (info.user && badge) {
      badge.textContent = info.user;
      badge.hidden = false;
    }

    const canWrite = info.can_write === true;
    document.body.classList.toggle("can-write", canWrite);
    if (canWrite) addPageSwitch();

    // No logout_url means there is no session to end - an open console with
    // neither a password nor Microsoft sign-in in front of it.
    const logoutBtn = document.getElementById("logout-btn");
    if (logoutBtn && info.logout_url) {
      logoutBtn.hidden = false;
      logoutBtn.addEventListener("click", async () => {
        logoutBtn.disabled = true;
        if (info.auth_mode !== "entra") {
          try {
            await fetch("/api/logout", { method: "POST" });
          } catch (err) {
            console.warn("logout failed", err);
          }
        }
        location.href = info.logout_url;
      });
    }
    return info;
  }

  initTheme();
  addLanguagePicker();
  // The theme toggle's label depends on the theme, so it is set here rather
  // than marked for Lang.apply().
  document.addEventListener("langchange", () => applyTheme(getTheme()));
  window.Shell = {
    bounceIfLoggedOut,
    session: loadSession(),
  };
})();
