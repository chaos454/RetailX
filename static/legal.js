const themeButton = document.querySelector("#legal-theme");

function applyTheme(theme) {
  const selected = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = selected;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", selected === "dark" ? "#101114" : "#F4EFE6");
  if (themeButton) themeButton.textContent = selected === "dark" ? "☼" : "☾";
}

fetch("/api/settings", { cache: "no-store" }).then(response => response.ok ? response.json() : null).then(settings => {
  if (settings) applyTheme(settings.theme);
}).catch(() => {});

themeButton?.addEventListener("click", async () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(theme);
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ theme }),
    });
    if (!response.ok) throw new Error("Settings could not be saved.");
  } catch {
    themeButton.setAttribute("aria-label", "Theme changed for this page only; settings could not be saved.");
  }
});
