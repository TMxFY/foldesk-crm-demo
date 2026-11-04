/**
 * Роли из tokens.css. В классах: bg-surface text-muted border-input.
 * Hex сюда не дублировать. Тёмная тема — [data-theme="dark"] в tokens.css.
 */
module.exports = {
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        surface: "var(--surface)",
        sunken: "var(--sunken)",
        border: "var(--border)",
        "border-input": "var(--border-input)",
        text: "var(--text)",
        muted: "var(--text-muted)",
        accent: "var(--accent)",
        "accent-hover": "var(--accent-hover)",
        "on-accent": "var(--on-accent)",
        "fill-muted": "var(--fill-muted)",
        "fill-muted-hover": "var(--fill-muted-hover)",
        danger: "var(--danger)",
        success: "var(--success)",
        warning: "var(--warning)",
        "on-danger": "var(--on-danger)",
        "on-success": "var(--on-success)",
        "on-warning": "var(--on-warning)",
        "danger-soft": "var(--danger-soft)",
        "success-soft": "var(--success-soft)",
        "warning-soft": "var(--warning-soft)",
      },
      fontFamily: {
        sans: ["Manrope", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      fontSize: {
        xs: ["12px", { lineHeight: "16px", fontWeight: "500" }],
        sm: ["14px", { lineHeight: "20px", fontWeight: "500" }],
        base: ["16px", { lineHeight: "24px", fontWeight: "400" }],
        lg: ["20px", { lineHeight: "28px", fontWeight: "600" }],
        xl: ["28px", { lineHeight: "36px", fontWeight: "600" }],
        display: ["40px", { lineHeight: "48px", fontWeight: "600" }],
      },
      spacing: {
        1: "4px",
        2: "8px",
        3: "12px",
        4: "16px",
        5: "24px",
        6: "32px",
        10: "40px",
        12: "48px",
        16: "64px",
      },
      borderRadius: {
        sm: "8px",
        md: "10px",
        lg: "12px",
        pill: "999px",
      },
      boxShadow: {
        card: "0 1px 2px rgba(9,9,11,.06)",
        pop: "0 12px 32px rgba(9,9,11,.16)",
      },
      transitionDuration: {
        fast: "120ms",
        base: "200ms",
      },
      transitionTimingFunction: {
        out: "cubic-bezier(.2,.8,.2,1)",
      },
      width: {
        sidebar: "240px",
        dialogs: "320px",
        "lead-panel": "360px",
      },
      height: {
        header: "56px",
      },
      maxWidth: {
        content: "1280px",
      },
    },
  },
};
