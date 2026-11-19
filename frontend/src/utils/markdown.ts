const ALLOWED_TAGS = new Set([
  "STRONG",
  "B",
  "EM",
  "I",
  "U",
  "S",
  "DEL",
  "CODE",
  "PRE",
  "BLOCKQUOTE",
  "SPAN",
  "A",
  "BR",
]);

export function clientMarkdownToSafeHtml(rawText: string): string {
  const escaped = String(rawText || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return escaped
    .replace(/^&gt;\s?(.*)$/gm, "<blockquote>$1</blockquote>")
    .replace(/```([\s\S]*?)```/g, "<pre><code>$1</code></pre>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<u>$1</u>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    .replace(/\|\|([^|\n]+)\|\|/g, '<span class="tg-spoiler" data-spoiler="1">$1</span>')
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
    .replace(/\n/g, "<br>");
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function sanitizeElement(node: ChildNode): string {
  if (node.nodeType === Node.TEXT_NODE) {
    return escapeHtml(node.textContent || "");
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return "";
  const el = node as Element;
  const tag = el.tagName.toUpperCase();
  const inner = [...el.childNodes].map(sanitizeElement).join("");
  if (!ALLOWED_TAGS.has(tag)) {
    return inner;
  }
  const lower = tag.toLowerCase();
  if (tag === "BR") return "<br>";
  if (tag === "A") {
    const href = String(el.getAttribute("href") || "").trim();
    if (/^(https?:|tg:|mailto:)/i.test(href)) {
      return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${inner}</a>`;
    }
    return `<span>${inner}</span>`;
  }
  if (
    tag === "SPAN" &&
    (el.classList.contains("tg-spoiler") || el.getAttribute("data-spoiler") === "1")
  ) {
    return `<span class="tg-spoiler" data-spoiler="1" title="Нажмите, чтобы показать скрытый текст">${inner}</span>`;
  }
  return `<${lower}>${inner}</${lower}>`;
}

export function sanitizeMessageHtml(rawHtml: string | null | undefined, fallbackText: string | null | undefined): string {
  const source = String(rawHtml || "").trim() || clientMarkdownToSafeHtml(String(fallbackText || ""));
  if (!source) return "";
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(`<div>${source}</div>`, "text/html");
    const root = doc.body.firstElementChild;
    if (!root) return escapeHtml(String(fallbackText || ""));
    return [...root.childNodes].map(sanitizeElement).join("");
  } catch {
    return escapeHtml(String(fallbackText || ""));
  }
}
