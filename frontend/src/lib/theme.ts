// Light/dark theme: follows the OS until the user picks one, then remembers it.

import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";
const KEY = "haciendagpt_theme";

function initialTheme(): Theme {
  try {
    const stored = localStorage.getItem(KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // ignore: fall through to the OS preference
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next = current === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(KEY, next);
      } catch {
        // not persisted; still applies for this session
      }
      return next;
    });
  }, []);

  return [theme, toggle];
}
