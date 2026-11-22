import type { AccountItem, DialogItem, LeadItem, MessageItem, Provider, StageItem } from "../types";

export const CLOSED_DEAL_STATUSES = new Set(["lost", "won", "on_hold", "archived"]);

export const DEFAULT_STAGES: StageItem[] = [
  { key: "new", label: "Лид" },
  { key: "talking", label: "Разговор" },
  { key: "waiting", label: "Ждём" },
  { key: "won", label: "Выигран" },
  { key: "lost", label: "Проигран" },
];

export const STAGE_BADGE_CLASS: Record<string, string> = {
  new: "b-new",
  talking: "b-talk",
  call_scheduled: "b-talk",
  waiting: "b-wait",
  on_hold: "b-wait",
  won: "b-won",
  lost: "b-lost",
};

export const LEAD_EVENT_META: Record<string, { label: string; badge: string }> = {
  created: { label: "Создан", badge: "b-new" },
  status_changed: { label: "Стадия", badge: "b-talk" },
  amount_changed: { label: "Бюджет", badge: "b-won" },
  outreach_sent: { label: "Рассылка", badge: "b-wait" },
  reply_received: { label: "Ответ", badge: "b-ok" },
  note_added: { label: "Заметка", badge: "b-new" },
  reminder_set: { label: "Напоминание", badge: "b-wait" },
  merged: { label: "Слияние", badge: "b-talk" },
};

export const ACCOUNT_HEALTH_META: Record<string, { label: string; tone: "ok" | "warn" | "bad" | "off" }> = {
  active: { label: "Активен", tone: "ok" },
  ok: { label: "Активен", tone: "ok" },
  offline: { label: "Офлайн", tone: "off" },
  unauthorized: { label: "Не авторизован", tone: "bad" },
  banned: { label: "Заблокирован", tone: "bad" },
  frozen: { label: "Заморожен", tone: "bad" },
};

export function normalizeProvider(raw: string | null | undefined): Provider {
  return String(raw || "").trim().toLowerCase() === "whatsapp" ? "whatsapp" : "telegram";
}

export function isLeadStatusClosed(status: string | null | undefined): boolean {
  return CLOSED_DEAL_STATUSES.has(String(status || "").trim().toLowerCase());
}

export function isAccountDead(acc: AccountItem | null | undefined): boolean {
  if (!acc) return true;
  const status = String(acc.status || "").trim().toLowerCase();
  const health = String(acc.health || "active").trim().toLowerCase();
  if (status && status !== "connected") return true;
  return health === "frozen" || health === "unauthorized" || health === "banned" || health === "offline";
}

export function accountRemainingQuota(acc: AccountItem): { sent: number; limit: number; remaining: number } {
  const limit = Number(acc.daily_limit ?? 20) || 20;
  const sent = Number(acc.sent_today ?? 0) || 0;
  const remaining =
    acc.remaining_today != null
      ? Math.max(0, Number(acc.remaining_today) || 0)
      : isAccountDead(acc)
        ? 0
        : Math.max(0, limit - sent);
  return { sent, limit, remaining };
}

export function accountDisplayHandle(acc: AccountItem | null | undefined): string {
  if (!acc) return "аккаунт";
  const uname = acc.telegram_username || acc.username;
  if (uname) return `@${uname.replace(/^@+/, "")}`;
  return acc.telegram_name || acc.phone || `#${acc.id}`;
}

export function normalizeDialogStamp(val: string | null | undefined): string {
  return String(val || "").trim().replace(" ", "T");
}

export function isSameDialog(a: Partial<DialogItem> | null | undefined, b: Partial<DialogItem> | null | undefined): boolean {
  if (!a || !b) return false;
  if (a.id != null && b.id != null) return Number(a.id) === Number(b.id);
  if (a.telegram_user_id != null && b.telegram_user_id != null) {
    if (Number(a.telegram_user_id) !== Number(b.telegram_user_id)) return false;
    if (a.account_id && b.account_id) return Number(a.account_id) === Number(b.account_id);
    return true;
  }
  return false;
}

export function sortDialogsByRecency(rows: DialogItem[]): DialogItem[] {
  const list = [...rows];
  return list.sort((a, b) => {
    const aStamp = normalizeDialogStamp(a?.last_message_at);
    const bStamp = normalizeDialogStamp(b?.last_message_at);
    if (!aStamp && !bStamp) {
      return (Number(b?.id) || 0) - (Number(a?.id) || 0);
    }
    if (!aStamp) return 1;
    if (!bStamp) return -1;
    if (aStamp !== bStamp) {
      return bStamp > aStamp ? 1 : -1;
    }
    return (Number(b?.id) || 0) - (Number(a?.id) || 0);
  });
}

export function isDialogWaitingOurAnswer(dialog: DialogItem | null | undefined, lead?: LeadItem | null): boolean {
  if (!dialog) return false;
  const rawStatus = lead?.status || dialog.lead_status || "";
  if (isLeadStatusClosed(rawStatus)) return false;
  if (dialog.waiting_our_answer != null) return Boolean(dialog.waiting_our_answer);
  if (dialog.is_waiting_our_answer != null) return Boolean(dialog.is_waiting_our_answer);
  return Number(dialog.last_from_peer || 0) === 1;
}

export function matchLeadForDialog(dialog: DialogItem | null | undefined, leads: LeadItem[]): LeadItem | null {
  if (!dialog) return null;
  const cleanUser = String(dialog.username || "").replace(/^@+/, "").toLowerCase();
  const dlgTgId = dialog.telegram_user_id ?? dialog.peer_id;
  return (
    leads.find((lead) => {
      if (dialog.lead_id && Number(lead.id) === Number(dialog.lead_id)) return true;
      if (dlgTgId != null && lead.telegram_user_id != null && Number(lead.telegram_user_id) === Number(dlgTgId)) {
        if (lead.account_id && dialog.account_id && Number(lead.account_id) !== Number(dialog.account_id)) {
          return false;
        }
        return true;
      }
      if (cleanUser && lead.username && String(lead.username).replace(/^@+/, "").toLowerCase() === cleanUser) {
        return true;
      }
      return false;
    }) || null
  );
}

export function formatMoney(amount: number | null | undefined, currency = "USD"): string {
  const n = Number(amount) || 0;
  const curr = String(currency || "USD").toUpperCase();
  const sym = curr === "EUR" ? "€" : curr === "RUB" ? "₽" : "$";
  return `${sym}${n.toLocaleString("en-US")}`;
}

export function formatBubbleTime(raw: string | null | undefined): string {
  if (!raw) return "";
  const normalized = normalizeDialogStamp(raw);
  const ms = Date.parse(normalized);
  if (Number.isNaN(ms)) return "";
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function formatShortTime(raw: string | null | undefined): string {
  if (!raw) return "";
  const ms = Date.parse(normalizeDialogStamp(raw));
  if (Number.isNaN(ms)) return "";
  const d = new Date(ms);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) {
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

export function getDayKey(raw: string | null | undefined): string {
  if (!raw) return "";
  const ms = Date.parse(normalizeDialogStamp(raw));
  if (Number.isNaN(ms)) return "";
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function formatDayDividerLabel(raw: string | null | undefined): string {
  if (!raw) return "";
  const ms = Date.parse(normalizeDialogStamp(raw));
  if (Number.isNaN(ms)) return "";
  const d = new Date(ms);
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfTarget = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diffDays = Math.round((startOfToday - startOfTarget) / 86400000);
  if (diffDays === 0) return "Сегодня";
  if (diffDays === 1) return "Вчера";
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString("ru-RU", {
    day: "numeric",
    month: "long",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

export function formatVoiceDuration(seconds: number | null | undefined): string {
  const total = Math.max(1, Math.round(Number(seconds) || 0));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

export function getInitials(name: string | null | undefined): string {
  const clean = String(name || "").trim();
  if (!clean) return "?";
  const parts = clean.split(/[\s·@._-]+/).filter(Boolean);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }
  return clean.slice(0, 2).toUpperCase();
}

export function buildTelegramLinks(
  dialog: DialogItem,
  lead?: LeadItem | null,
): { href: string; fallbackUrl: string; label: string; title: string } {
  const provider = normalizeProvider(dialog.provider);
  const cleanUname = String(dialog.username || lead?.username || "").trim().replace(/^@+/, "");
  const cleanPhoneDigits = String(dialog.phone || lead?.phone || "").replace(/\D+/g, "");
  const numericPeerId = dialog.peer_id ?? dialog.telegram_user_id ?? lead?.telegram_user_id ?? "";

  if (provider === "whatsapp" && cleanPhoneDigits) {
    const waUrl = `https://wa.me/${cleanPhoneDigits}`;
    return {
      href: waUrl,
      fallbackUrl: waUrl,
      label: "Открыть в WhatsApp ↗",
      title: "Открыть диалог в WhatsApp",
    };
  }
  if (cleanUname) {
    return {
      href: `tg://resolve?domain=${encodeURIComponent(cleanUname)}`,
      fallbackUrl: `https://t.me/${encodeURIComponent(cleanUname)}`,
      label: "Открыть в Telegram ↗",
      title: `Открыть @${cleanUname} в Telegram`,
    };
  }
  if (cleanPhoneDigits) {
    return {
      href: `tg://resolve?phone=${encodeURIComponent(cleanPhoneDigits)}`,
      fallbackUrl: `https://t.me/+${encodeURIComponent(cleanPhoneDigits)}`,
      label: "Открыть в Telegram ↗",
      title: "Открыть диалог в Telegram",
    };
  }
  if (numericPeerId) {
    return {
      href: `tg://openmessage?user_id=${encodeURIComponent(String(numericPeerId))}`,
      fallbackUrl: `https://web.telegram.org/a/#${encodeURIComponent(String(numericPeerId))}`,
      label: "Открыть в Telegram ↗",
      title: "Открыть диалог в Telegram",
    };
  }
  return {
    href: "https://t.me/",
    fallbackUrl: "https://t.me/",
    label: "Открыть в Telegram ↗",
    title: "Открыть диалог в Telegram",
  };
}

export function renderSnippetTemplate(
  body: string,
  dialog: DialogItem | null,
  lead: LeadItem | null,
  fallbackLead?: LeadItem | null,
): string {
  const effectiveLead = lead || (!dialog ? fallbackLead : null) || null;
  const rawName = String(
    effectiveLead?.name ||
      dialog?.name ||
      dialog?.title ||
      effectiveLead?.username ||
      dialog?.username ||
      "",
  ).trim();
  let rawCompany = String(effectiveLead?.company || dialog?.lead_company || dialog?.company || "").trim();
  let cleanName = rawName;
  if (rawName.includes("·")) {
    const parts = rawName.split("·").map((p) => p.trim());
    cleanName = parts[0] || "";
    if (!rawCompany && parts[1]) {
      rawCompany = parts.slice(1).join(" · ").trim();
    }
  }
  if (!rawCompany && effectiveLead?.properties) {
    for (const prop of effectiveLead.properties) {
      const pName = String(prop?.name || "").trim().toLowerCase();
      const pKey = String(prop?.key || "").trim().toLowerCase();
      if (
        ["компания", "company", "проект", "организация"].includes(pName) ||
        ["компания", "company", "org", "project"].includes(pKey)
      ) {
        const val = String(prop?.value_text || "").trim();
        if (val) {
          rawCompany = val;
          break;
        }
      }
    }
  }
  const finalName = cleanName || "коллега";
  const finalCompany = rawCompany || "вашей компании";
  return String(body || "")
    .replace(/\{name\}/gi, () => finalName)
    .replace(/\{company\}/gi, () => finalCompany);
}

export function messageIdentityKey(msg: MessageItem): string {
  if (msg.clientTempId) return `tmp:${msg.clientTempId}`;
  if (msg.telegram_message_id != null && Number(msg.telegram_message_id) > 0) {
    return `tg:${msg.telegram_message_id}`;
  }
  if (msg.id != null) return `id:${msg.id}`;
  return `ts:${msg.sent_at || ""}:${msg.direction}:${(msg.text || "").slice(0, 24)}`;
}
