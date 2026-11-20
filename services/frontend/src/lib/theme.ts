export function initTheme(): void {
  let saved: string | null = null;
  try {
    saved = localStorage.getItem("theme");
  } catch {
    /* storage blocked: follow the system */
  }
  const dark = saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.classList.toggle("dark", dark);
}

export const isDark = (): boolean => document.documentElement.classList.contains("dark");

/** Flips the theme and returns whether it is now dark. */
export function toggleTheme(): boolean {
  const dark = !document.documentElement.classList.contains("dark");
  document.documentElement.classList.toggle("dark", dark);
  try {
    localStorage.setItem("theme", dark ? "dark" : "light");
  } catch {
    /* ignore */
  }
  return dark;
}
