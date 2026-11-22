export type Provider = "telegram" | "whatsapp";

export type InboxMode = "unified" | "all";
export type SearchMode = "crm" | "telegram";
export type ChipKey = "waiting" | "our_turn" | "unread" | "muted";
export type InboxViewState = "loading" | "empty" | "none" | "filled" | "error";
export type ThreadViewState = "empty" | "loading" | "filled";

export interface AccountItem {
  id: number;
  phone?: string | null;
  status?: string | null;
  health?: string | null;
  health_status?: string | null;
  provider?: string | null;
  telegram_username?: string | null;
  username?: string | null;
  telegram_name?: string | null;
  daily_limit?: number | null;
  sent_today?: number | null;
  remaining_today?: number | null;
  proxy_mode?: string | null;
  connection_label?: string | null;
  is_demo?: boolean;
}

export interface StageItem {
  key: string;
  label: string;
  color?: string | null;
  position?: number;
}

export interface LeadProperty {
  id?: number;
  name?: string;
  key?: string;
  type?: string;
  value_text?: string | null;
  value_int?: number | null;
}

export interface LeadEventItem {
  id: number;
  lead_id?: number;
  event_type: string;
  title?: string;
  created_at?: string;
  payload?: Record<string, unknown>;
}

export interface LeadNoteItem {
  id: number;
  body: string;
  created_at?: string;
}

export interface LeadItem {
  id: number;
  account_id?: number | null;
  dialog_id?: number | null;
  telegram_user_id?: number | null;
  username?: string | null;
  name?: string | null;
  phone?: string | null;
  provider?: string | null;
  company?: string | null;
  niche?: string | null;
  status: string;
  amount?: number | null;
  currency?: string | null;
  monthly_value?: number | null;
  note?: string | null;
  next_step?: string | null;
  next_follow_up?: string | null;
  last_contacted?: string | null;
  folder_name?: string | null;
  airtable_status?: string | null;
  airtable_url?: string | null;
  lead_source?: string | null;
  offer_type?: string | null;
  offer_details?: string | null;
  description?: string | null;
  email?: string | null;
  url?: string | null;
  properties?: LeadProperty[];
  notes?: LeadNoteItem[];
  events?: LeadEventItem[];
}

export interface PeerDialogSummary {
  dialog_id: number;
  account_id: number;
  account_username?: string | null;
  account_name?: string | null;
  account_health?: string | null;
  last_message_at?: string | null;
  last_from_peer?: number;
  unread_count?: number;
  is_waiting_our_answer?: boolean;
}

export interface DialogItem {
  id: number;
  dialog_id?: number;
  primary_dialog_id?: number;
  account_id: number;
  account_phone?: string | null;
  account_name?: string | null;
  account_username?: string | null;
  account_health?: string | null;
  telegram_user_id?: number | null;
  peer_id?: number | null;
  peer_type?: string | null;
  username?: string | null;
  name?: string | null;
  title?: string | null;
  phone?: string | null;
  company?: string | null;
  provider?: string | null;
  unread_count?: number | null;
  muted?: number | boolean | null;
  is_group?: number | boolean | null;
  is_bot?: number | boolean | null;
  last_message_at?: string | null;
  last_message_preview?: string | null;
  last_from_peer?: number | null;
  waiting_our_answer?: boolean | null;
  is_waiting_our_answer?: boolean | null;
  lead_id?: number | null;
  lead_status?: string | null;
  lead_amount?: number | null;
  lead_company?: string | null;
  lead_niche?: string | null;
  lead_airtable_status?: string | null;
  lead_airtable_url?: string | null;
  avatar_url?: string | null;
  peer_dialogs?: PeerDialogSummary[];
}

export interface InboxCounts {
  waiting: number;
  our_turn: number;
  unread: number;
  muted: number;
  waiting_active?: number;
}

export interface InboxResponsePayload {
  dialogs: DialogItem[];
  counts?: InboxCounts | null;
  all_counts?: InboxCounts | null;
  raw_counts?: InboxCounts | null;
  workspace_total_dialogs?: number | null;
  total_dialogs?: number | null;
  grouped_total_leads?: number | null;
  q_applied?: boolean;
  search_applied?: boolean;
}

export interface MessageItem {
  id: number;
  dialog_id: number;
  telegram_message_id?: number | null;
  direction: "in" | "out" | string;
  text?: string | null;
  formatted_html?: string | null;
  sent_at?: string | null;
  edited_at?: string | null;
  read_at?: string | null;
  reply_to_telegram_message_id?: number | null;
  media_url?: string | null;
  media_type?: string | null;
  media_mime?: string | null;
  media_filename?: string | null;
  media_duration?: number | null;
  media_meta?: {
    duration?: number;
    waveform?: number[];
    filename?: string;
    file_name?: string;
    mime_type?: string;
  } | null;
  is_snapshot_preview?: boolean;
  clientTempId?: string;
  localStatus?: "sending" | "failed";
  localError?: string;
}

export interface SnippetItem {
  id: number;
  shortcut: string;
  title: string;
  body: string;
  created_at?: string;
}

export interface ClientProfileData {
  primary_dialog_id?: number | null;
  lead?: LeadItem | null;
  telegram_profile?: {
    full_name?: string | null;
    username?: string | null;
    phone?: string | null;
    peer_id?: number | null;
    provider?: string | null;
    bio?: string | null;
    status_text?: string | null;
    avatar_url?: string | null;
    tg_url?: string | null;
    source?: string | null;
    fetched_at?: string | null;
    is_premium?: boolean;
    is_verified?: boolean;
    is_scam?: boolean;
    is_fake?: boolean;
    personal_channel?: string | null;
    common_chats_count?: number | null;
  } | null;
  dialogs?: Array<Record<string, unknown>>;
  outreach?: Array<{
    campaign_id?: number;
    campaign_name?: string;
    status?: string;
    sent_account_username?: string;
    sent_account_name?: string;
    current_step?: number;
    last_sent_at?: string;
    hook?: string;
    their_angle?: string;
    our_angle?: string;
    extra?: Record<string, string>;
  }>;
  recent_messages?: Array<Record<string, unknown>>;
  reminders?: Array<{ id: number; due_at?: string; text: string; done_at?: string | null }>;
  stats?: {
    total_messages?: number;
    in_messages?: number;
    out_messages?: number;
    total_dialogs?: number;
    campaigns_count?: number;
    last_contact_at?: string | null;
  };
}

export interface CrmBridge {
  openClientProfile?: (leadId: number | null, dialogId: number | null) => void;
  showToast?: (msg: string, kind?: "ok" | "error") => void;
  setWorkspaceDialogsTotal?: (n: number) => void;
  setLiveStatus?: (ok: boolean) => void;
  onLeadUpdated?: (updatedLead: LeadItem) => void;
  switchSheet?: (sheetName: string) => void;
  openAccountsModal?: (opts?: {
    openAuth?: boolean;
    tab?: "phone" | "session" | "whatsapp";
    accountId?: number;
    reauth?: boolean;
  }) => void;
  getBootData?: () => {
    accounts?: AccountItem[];
    leads?: LeadItem[];
    stages?: StageItem[];
  };
}

export interface InboxV2Bridge {
  selectDialogById: (dialogId: number) => Promise<void>;
  openLeadDialog: (leadOrId: number | LeadItem) => Promise<void>;
  refresh: (opts?: { accountId?: string | number }) => Promise<void>;
  setAccounts: (accounts: AccountItem[]) => void;
  setLeads: (leads: LeadItem[]) => void;
  setStages: (stages: StageItem[]) => void;
  getState: () => {
    activeDialogId: number | null;
    mode: InboxMode;
    searchMode: SearchMode;
    sessionFilter: string;
    counts: InboxCounts;
    dialogCount: number;
    liveOk: boolean;
  };
}

declare global {
  interface Window {
    __INBOX_V2_ACTIVE__?: boolean;
    __INBOX_V2_BRIDGE__?: InboxV2Bridge;
    __CRM_BRIDGE__?: CrmBridge;
    __lastCopiedSnippet?: string;
  }
}
