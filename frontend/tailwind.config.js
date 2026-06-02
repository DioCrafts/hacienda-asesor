/** @type {import('tailwindcss').Config} */
// Design tokens mirror the Streamlit redesign so any screenshot or copy
// that exists today maps 1:1 to the new React UI. New tokens (e.g.
// risk-critical) are added here when needed.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: "#f6f4ef",
        surface: "#ffffff",
        ink: "#1b2330",
        "ink-soft": "#46505f",
        muted: "#79828f",
        line: "#e7e2d8",
        primary: {
          DEFAULT: "#14635e",
          dark: "#0f4f4a",
          soft: "#e4efed",
        },
        navy: "#1f3a5f",
        grounding: {
          "cited-bg":   "#e8f3ec",
          "cited-fg":   "#1f7a52",
          "cited-bd":   "#c2e2cf",
          "uncited-bg": "#fbf1da",
          "uncited-fg": "#a96f08",
          "uncited-bd": "#eed8a6",
          "abstain-bg": "#eef0f3",
          "abstain-fg": "#586679",
          "abstain-bd": "#d6dbe2",
        },
        risk: {
          "high-bg":   "#fae8e4",
          "high-fg":   "#c1442e",
          "medium-bg": "#fbf0db",
          "medium-fg": "#b5760a",
          "low-bg":    "#e8f3ec",
          "low-fg":    "#1f7a52",
        },
      },
      fontFamily: {
        // Body: system sans for crispness without webfont latency.
        sans: ["ui-sans-serif", "-apple-system", "BlinkMacSystemFont",
               "Segoe UI", "Inter", "sans-serif"],
        // Brand-only serif. Used by the logotype + section headings.
        serif: ["Georgia", "Cambria", "Times New Roman", "serif"],
        mono: ["ui-monospace", "SF Mono", "Menlo", "monospace"],
      },
      borderRadius: { pill: "999px" },
      boxShadow: {
        card: "0 1px 2px rgba(27,35,48,0.04), 0 4px 12px rgba(27,35,48,0.04)",
      },
    },
  },
  plugins: [],
};
