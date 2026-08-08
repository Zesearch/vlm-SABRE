(() => {
  const root = document.documentElement;

  const updateControls = (theme) => {
    const toggle = document.getElementById("theme-toggle");
    const themeColor = document.getElementById("theme-color");
    const nextTheme = theme === "light" ? "dark" : "light";

    if (toggle) {
      toggle.setAttribute("aria-pressed", String(theme === "light"));
      toggle.setAttribute("aria-label", `Switch to ${nextTheme} mode`);
      toggle.title = `Switch to ${nextTheme} mode`;
    }
    if (themeColor) {
      themeColor.content = theme === "light" ? "#f4f6f8" : "#080a0d";
    }
  };

  const applyTheme = (theme) => {
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    updateControls(theme);
  };

  applyTheme("dark");

  const initializeToggle = () => {
    const toggle = document.getElementById("theme-toggle");
    updateControls(root.dataset.theme);
    toggle?.addEventListener("click", () => {
      const nextTheme = root.dataset.theme === "light" ? "dark" : "light";
      applyTheme(nextTheme);
    });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeToggle, { once: true });
  } else {
    initializeToggle();
  }
})();
