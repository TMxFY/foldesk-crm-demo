import type {
  AccountItem,
  ClientProfileData,
  DialogItem,
  InboxResponsePayload,
  LeadItem,
  MessageItem,
  SnippetItem,
  StageItem,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function apiFetch<T = unknown>(
  path: string,
  options?: { method?: string; body?: unknown; signal?: AbortSignal },
): Promise<T> {
  const method = options?.method || "GET";
  const headers: Record<string, string> = {};
  let body: string | undefined;
  if (options?.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const resp = await fetch(path, {
    method,
    headers,
    body,
    credentials: "same-origin",
    signal: options?.signal,
  });
  const text = await resp.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!resp.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed
        ? String((parsed as { detail: unknown }).detail)
        : parsed && typeof parsed === "object" && "error" in parsed
          ? String((parsed as { error: unknown }).error)
          : typeof parsed === "string" && parsed
            ? parsed
            : `HTTP ${resp.status}`;
    throw new ApiError(resp.status, detail);
  }
  return parsed as T;
}

export function normalizeInboxResponse(raw: InboxResponsePayload | DialogItem[]): InboxResponsePayload {
  if (Array.isArray(raw)) {
    return { dialogs: raw };
  }
  return {
    dialogs: Array.isArray(raw?.dialogs) ? raw.dialogs : [],
    counts: raw?.counts ?? null,
    all_counts: raw?.all_counts ?? null,
    raw_counts: raw?.raw_counts ?? null,
    workspace_total_dialogs: raw?.workspace_total_dialogs ?? raw?.total_dialogs ?? null,
    total_dialogs: raw?.total_dialogs ?? raw?.workspace_total_dialogs ?? null,
    grouped_total_leads: raw?.grouped_total_leads ?? null,
    q_applied: raw?.q_applied,
    search_applied: raw?.search_applied,
  };
}

export async function fetchInbox(params: {
  accountId: string;
  groupByLead: boolean;
  chips: string[];
  q: string;
}): Promise<InboxResponsePayload> {
  const qs = new URLSearchParams({
    account_id: params.accountId || "0",
    crm_only: "1",
    with_counts: "1",
  });
  if (params.groupByLead) qs.set("group_by_lead", "1");
  if (params.chips.length) qs.set("chip", params.chips.join(","));
  if (params.q.trim()) qs.set("q", params.q.trim());

  const res = await apiFetch<InboxResponsePayload | DialogItem[]>(`/api/inbox?${qs.toString()}`);
  return normalizeInboxResponse(res);
}

export async function searchTelegramInbox(accountId: string, q: string): Promise<DialogItem[]> {
  const qs = new URLSearchParams({
    account_id: accountId || "0",
    q,
    crm_only: "1",
  });
  try {
    const res = await apiFetch<DialogItem[] | InboxResponsePayload>(
      `/api/inbox/telegram-search?${qs.toString()}`,
    );
    return Array.isArray(res) ? res : res.dialogs || [];
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      const res = await apiFetch<DialogItem[] | InboxResponsePayload>(
        `/api/accounts/${encodeURIComponent(accountId || "0")}/dialogs?q=${encodeURIComponent(q)}&crm_only=1`,
      );
      return Array.isArray(res) ? res : res.dialogs || [];
    }
    throw err;
  }
}

export async function syncInboxNow(accountId: number | null): Promise<void> {
  await apiFetch("/api/inbox/sync", {
    method: "POST",
    body: { account_id: accountId, crm_only: true },
  });
}

export async function fetchDialogMessages(dialogId: number): Promise<MessageItem[]> {
  const res = await apiFetch<MessageItem[]>(`/api/dialogs/${dialogId}/messages`);
  return Array.isArray(res) ? res : [];
}

export async function sendDialogMessage(
  dialogId: number,
  text: string,
  replyToMsgId?: number | null,
): Promise<MessageItem> {
  return apiFetch<MessageItem>(`/api/dialogs/${dialogId}/messages`, {
    method: "POST",
    body: {
      text,
      ...(replyToMsgId != null ? { reply_to_msg_id: Number(replyToMsgId) } : {}),
    },
  });
}

export async function editDialogMessage(
  dialogId: number,
  msgId: number,
  text: string,
): Promise<MessageItem> {
  return apiFetch<MessageItem>(`/api/dialogs/${dialogId}/messages/${msgId}`, {
    method: "PATCH",
    body: { text },
  });
}

export async function sendDialogMedia(
  dialogId: number,
  payload: {
    media_type: string;
    filename: string;
    mime_type: string;
    data_base64: string;
    caption?: string;
    duration?: number;
    reply_to_msg_id?: number | null;
  },
): Promise<MessageItem> {
  return apiFetch<MessageItem>(`/api/dialogs/${dialogId}/media`, {
    method: "POST",
    body: payload,
  });
}

export async function markDialogReadApi(dialogId: number): Promise<Record<string, unknown>> {
  return apiFetch<Record<string, unknown>>(`/api/inbox/dialogs/${dialogId}/read?crm_only=1`, {
    method: "POST",
  });
}

export async function markDialogUnreadApi(dialogId: number): Promise<Record<string, unknown>> {
  return apiFetch<Record<string, unknown>>(`/api/inbox/dialogs/${dialogId}/unread?crm_only=1`, {
    method: "POST",
  });
}

export async function switchDialogAccountApi(payload: {
  dialog_id?: number | null;
  lead_id?: number | null;
  peer_id?: number | null;
  username?: string | null;
  target_account_id: number;
  name?: string | null;
  phone?: string | null;
}): Promise<{ ok: boolean; dialog: DialogItem; messages?: MessageItem[]; created?: boolean }> {
  try {
    return await apiFetch<{ ok: boolean; dialog: DialogItem; messages?: MessageItem[]; created?: boolean }>(
      "/api/dialogs/switch-account",
      {
        method: "POST",
        body: {
          dialog_id: payload.dialog_id ?? null,
          lead_id: payload.lead_id ?? null,
          peer_id: payload.peer_id ?? null,
          username: payload.username ?? null,
          target_account_id: payload.target_account_id,
        },
      },
    );
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
      const dlg = await apiFetch<DialogItem>("/api/dialogs/open-by-lead", {
        method: "POST",
        body: {
          lead_id: payload.lead_id ?? null,
          account_id: payload.target_account_id,
          telegram_user_id: payload.peer_id ?? null,
          username: payload.username ?? null,
          phone: payload.phone ?? null,
          name: payload.name || payload.username || "Контакт",
        },
      });
      const msgs = dlg?.id ? await fetchDialogMessages(dlg.id).catch(() => []) : [];
      return { ok: true, dialog: dlg, messages: msgs, created: false };
    }
    throw err;
  }
}

export async function openDialogByLeadApi(payload: {
  lead_id?: number | null;
  account_id: number;
  telegram_user_id?: number | null;
  username?: string | null;
  phone?: string | null;
  name?: string | null;
}): Promise<DialogItem> {
  return apiFetch<DialogItem>("/api/dialogs/open-by-lead", {
    method: "POST",
    body: payload,
  });
}

export async function fetchClientProfile(
  dialogId: number | null,
  leadId: number | null,
): Promise<ClientProfileData> {
  if (dialogId != null) {
    return apiFetch<ClientProfileData>(`/api/dialogs/${dialogId}/profile`);
  }
  return apiFetch<ClientProfileData>(`/api/leads/${leadId}/profile`);
}

export async function fetchLeadCard(leadId: number): Promise<LeadItem> {
  return apiFetch<LeadItem>(`/api/leads/${leadId}/card`);
}

export async function patchLeadApi(leadId: number, patch: Record<string, unknown>): Promise<LeadItem> {
  return apiFetch<LeadItem>(`/api/leads/${leadId}`, {
    method: "PATCH",
    body: patch,
  });
}

export async function createLeadApi(payload: Record<string, unknown>): Promise<LeadItem> {
  return apiFetch<LeadItem>("/api/leads", {
    method: "POST",
    body: payload,
  });
}

export async function addLeadNoteApi(leadId: number, body: string): Promise<void> {
  await apiFetch(`/api/leads/${leadId}/notes`, {
    method: "POST",
    body: { body },
  });
}

export async function fetchSnippetsApi(): Promise<SnippetItem[]> {
  const res = await apiFetch<SnippetItem[]>("/api/snippets");
  return Array.isArray(res) ? res : [];
}

export async function createSnippetApi(payload: {
  shortcut: string;
  title: string;
  body: string;
}): Promise<SnippetItem> {
  return apiFetch<SnippetItem>("/api/snippets", {
    method: "POST",
    body: payload,
  });
}

export async function deleteSnippetApi(snippetId: number): Promise<void> {
  await apiFetch(`/api/snippets/${snippetId}`, { method: "DELETE" });
}

export async function fetchAccountsApi(): Promise<AccountItem[]> {
  const res = await apiFetch<AccountItem[]>("/api/accounts");
  return Array.isArray(res) ? res : [];
}

export async function fetchLeadsApi(): Promise<LeadItem[]> {
  const res = await apiFetch<LeadItem[]>("/api/leads");
  return Array.isArray(res) ? res : [];
}

export async function fetchStagesApi(): Promise<StageItem[]> {
  const res = await apiFetch<StageItem[]>("/api/pipeline/stages");
  return Array.isArray(res) ? res : [];
}
