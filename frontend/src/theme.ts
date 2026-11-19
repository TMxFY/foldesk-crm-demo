import { neutralTheme } from "@astryxdesign/theme-neutral/built";

const SANS_STACK = '"Inter", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
const MONO_STACK = '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace';

export const foldeskTheme = {
  ...neutralTheme,
  tokens: {
    ...neutralTheme.tokens,
    "--font-family-body": SANS_STACK,
    "--font-family-heading": SANS_STACK,
    "--font-family-code": MONO_STACK,
    "--color-background-body": "#111112",
    "--color-background-surface": "#1b1b1e",
    "--color-background-card": "#1b1b1e",
    "--color-background-popover": "#222226",
    "--color-background-muted": "#26262a",
    "--color-border": "#2e2e33",
    "--color-border-emphasized": "#45454d",
    "--color-text-primary": "#f5f5f7",
    "--color-text-secondary": "#a6a6ae",
    "--color-info": "#6d9cfe",
    "--color-accent": "#6d9cfe",
    "--color-on-accent": "#0b0b0d",
    "--shadow-low":
      "0 2px 6px rgba(0, 0, 0, 0.32), inset 0 0 0 1px rgba(255, 255, 255, 0.06)",
    "--shadow-med":
      "0 8px 24px rgba(0, 0, 0, 0.58), 0 2px 6px rgba(0, 0, 0, 0.42), inset 0 0 0 1px rgba(255, 255, 255, 0.08)",
  },
};
