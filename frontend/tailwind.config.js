/** @type {import('tailwindcss').Config} */
// Every colour is a CSS variable (RGB triplet) defined in src/index.css, with
// a light and a dark value, so components never branch on the theme: the same
// `bg-surface text-ink` reads correctly in both. `<alpha-value>` keeps
// Tailwind's opacity modifiers (`bg-line/60`) working.
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        paper: token("paper"),
        surface: token("surface"),
        ink: token("ink"),
        "ink-soft": token("ink-soft"),
        muted: token("muted"),
        line: token("line"),
        primary: {
          DEFAULT: token("primary"),
          hover: token("primary-hover"),
          // Text/icon colour on `primary-soft` plates.
          dark: token("primary-dark"),
          soft: token("primary-soft"),
        },
        navy: token("navy"),
        grounding: {
          "cited-bg": token("cited-bg"),
          "cited-fg": token("cited-fg"),
          "cited-bd": token("cited-bd"),
          "uncited-bg": token("uncited-bg"),
          "uncited-fg": token("uncited-fg"),
          "uncited-bd": token("uncited-bd"),
          "abstain-bg": token("abstain-bg"),
          "abstain-fg": token("abstain-fg"),
          "abstain-bd": token("abstain-bd"),
        },
        risk: {
          "high-bg": token("risk-high-bg"),
          "high-fg": token("risk-high-fg"),
          "medium-bg": token("uncited-bg"),
          "medium-fg": token("uncited-fg"),
          "low-bg": token("cited-bg"),
          "low-fg": token("cited-fg"),
        },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "Inter", "sans-serif"],
        serif: ["Georgia", "Cambria", "Times New Roman", "serif"],
        mono: ["ui-monospace", "SF Mono", "Menlo", "monospace"],
      },
      borderRadius: { pill: "999px" },
      boxShadow: {
        card: "0 1px 2px rgb(var(--shadow) / 0.05), 0 4px 12px rgb(var(--shadow) / 0.05)",
        pop: "0 8px 30px rgb(var(--shadow) / 0.18)",
      },
      keyframes: {
        "fade-up": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        flash: {
          "0%, 60%": { boxShadow: "0 0 0 3px rgb(var(--primary) / 0.45)" },
          "100%": { boxShadow: "0 0 0 3px rgb(var(--primary) / 0)" },
        },
      },
      animation: {
        "fade-up": "fade-up 220ms ease-out both",
        flash: "flash 1.8s ease-out both",
      },
    },
  },
  plugins: [],
};
