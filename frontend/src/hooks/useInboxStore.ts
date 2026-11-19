import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  addLeadNoteApi,
  createLeadApi,
  createSnippetApi,
  deleteSnippetApi,
  editDialogMessage,
  fetchAccountsApi,
  fetchClientProfile,
  fetchDialogMessages,
  fetchInbox,
  fetchLeadCard,
  fetchLeadsApi,
  fetchSnippetsApi,
  fetchStagesApi,
  markDialogReadApi,
  markDialogUnreadApi,
  openDialogByLeadApi,
  patchLeadApi,
  searchTelegramInbox,
  sendDialogMedia,
  sendDialogMessage,
  switchDialogAccountApi,
  syncInboxNow,
} from "../api";
import type {
  AccountItem,
  ChipKey,
  ClientProfileData,
  DialogItem,
  InboxCounts,
  InboxMode,
  InboxResponsePayload,
  InboxViewState,
  LeadItem,
  MessageItem,
  SearchMode,
  SnippetItem,
  StageItem,
  ThreadViewState,
} from "../types";
import {
  DEFAULT_STAGES,
  isAccountDead,
  isDialogWaitingOurAnswer,
  matchLeadForDialog,
  messageIdentityKey,
  normalizeDialogStamp,
  sortDialogsByRecency,
} from "../utils/format";
import { draftKeyForDialog, listDraftKeysSnapshot, writeDraft } from "../utils/drafts";

const EMPTY_COUNTS: InboxCounts = { waiting: 0, our_turn: 0, unread: 0, muted: 0 };

function notifyToast(message: string, kind: "ok" | "error" = "ok"): void {
  if (window.__CRM_BRIDGE__?.showToast) {
    window.__CRM_BRIDGE__.showToast(message, kind);
    return;
  }
  const el = document.querySelector("#toast") as HTMLElement | null;
  if (!el) return;
  el.textContent = message;
  el.dataset.kind = kind;
  el.hidden = false;
  window.setTimeout(() => {
    el.hidden = true;
  }, 3200);
}

function syncWorkspaceTotal(total: number): void {
  if (window.__CRM_BRIDGE__?.setWorkspaceDialogsTotal) {
    window.__CRM_BRIDGE__.setWorkspaceDialogsTotal(total);
    return;
  }
  const node = document.querySelector("#stat-dialogs");
  if (node) node.textContent = String(total);
}

function syncLivePill(ok: boolean): void {
  if (window.__CRM_BRIDGE__?.setLiveStatus) {
    window.__CRM_BRIDGE__.setLiveStatus(ok);
    return;
  }
  const node = document.querySelector("#inbox-live") as HTMLElement | null;
  if (!node) return;
  node.dataset.live = ok ? "on" : "off";
  node.textContent = ok ? "Список обновляется сам" : "Список не обновляется";
}

function computeLocalCounts(
  dialogs: DialogItem[],
  sessionFilter: string,
  leads: LeadItem[],
): InboxCounts {
  const counts: InboxCounts = { waiting: 0, our_turn: 0, unread: 0, muted: 0 };
  for (const d of dialogs) {
    if (sessionFilter !== "all") {
      const targetId = Number(sessionFilter);
      const matchesOwn = Number(d.account_id) === targetId;
      const matchesPeer = Boolean(d.peer_dialogs?.some((p) => Number(p.account_id) === targetId));
      if (!matchesOwn && !matchesPeer) continue;
    }
    const lead = matchLeadForDialog(d, leads);
    if (isDialogWaitingOurAnswer(d, lead)) counts.waiting += 1;
    if (Number(d.last_from_peer || 0) === 0) counts.our_turn += 1;
    if (Number(d.unread_count || 0) > 0) counts.unread += 1;
    if (Boolean(d.muted)) counts.muted += 1;
  }
  return counts;
}

function resolveCountsFromResponse(
  res: InboxResponsePayload,
  sessionFilter: string,
  connectedCount: number,
  fallbackPool: DialogItem[],
  leads: LeadItem[],
): InboxCounts {
  const useAll = sessionFilter === "all" && connectedCount > 1;
  const incoming = useAll && res.all_counts ? res.all_counts : res.counts;
  if (incoming) {
    const waitingVal =
      incoming.waiting_active != null
        ? Number(incoming.waiting_active)
        : incoming.waiting != null
          ? Number(incoming.waiting)
          : computeLocalCounts(fallbackPool, sessionFilter, leads).waiting;
    return {
      waiting: waitingVal,
      our_turn: Number(incoming.our_turn || 0),
      unread: Number(incoming.unread || 0),
      muted: Number(incoming.muted || 0),
    };
  }
  return computeLocalCounts(fallbackPool, sessionFilter, leads);
}

function mergeMessageLists(existing: MessageItem[], incoming: MessageItem[]): MessageItem[] {
  const map = new Map<string, MessageItem>();
  const localOptimistic: MessageItem[] = [];
  for (const msg of existing) {
    if (msg.clientTempId && msg.localStatus) {
      localOptimistic.push(msg);
    } else {
      map.set(messageIdentityKey(msg), msg);
    }
  }
  for (const msg of incoming) {
    map.set(messageIdentityKey(msg), msg);
  }
  const merged = [...map.values()].sort((a, b) => {
    const aStamp = normalizeDialogStamp(a.sent_at);
    const bStamp = normalizeDialogStamp(b.sent_at);
    if (aStamp !== bStamp) return aStamp < bStamp ? -1 : 1;
    return (Number(a.id) || 0) - (Number(b.id) || 0);
  });
  for (const opt of localOptimistic) {
    const alreadySynced = merged.some(
      (m) =>
        m.direction === "out" &&
        String(m.text || "").trim() === String(opt.text || "").trim() &&
        Math.abs(Date.parse(normalizeDialogStamp(m.sent_at)) - Date.parse(normalizeDialogStamp(opt.sent_at))) < 15000,
    );
    if (!alreadySynced) merged.push(opt);
  }
  return merged;
}

export function useInboxStore() {
  const boot = window.__CRM_BRIDGE__?.getBootData?.();

  const [accounts, setAccounts] = useState<AccountItem[]>(() =>
    Array.isArray(boot?.accounts) ? boot.accounts : [],
  );
  const [leads, setLeads] = useState<LeadItem[]>(() =>
    Array.isArray(boot?.leads) ? boot.leads : [],
  );
  const [stages, setStages] = useState<StageItem[]>(() =>
    Array.isArray(boot?.stages) && boot.stages.length ? boot.stages : DEFAULT_STAGES,
  );
  const [snippets, setSnippets] = useState<SnippetItem[]>([]);

  const [inboxMode, setInboxMode] = useState<InboxMode>("unified");
  const [searchMode, setSearchMode] = useState<SearchMode>("crm");
  const [sessionFilter, setSessionFilter] = useState<string>("all");
  const [activeChips, setActiveChips] = useState<ChipKey[]>([]);
  const [query, setQuery] = useState<string>("");
  const [telegramSubmittedQuery, setTelegramSubmittedQuery] = useState<string>("");

  const [dialogsById, setDialogsById] = useState<Record<number, DialogItem>>({});
  const [dialogOrder, setDialogOrder] = useState<number[]>([]);
  const [allDialogsPool, setAllDialogsPool] = useState<DialogItem[]>([]);
  const [inboxViewState, setInboxViewState] = useState<InboxViewState>("empty");
  const [inboxError, setInboxError] = useState<string>("");
  const [counts, setCounts] = useState<InboxCounts>(EMPTY_COUNTS);
  const [workspaceTotalDialogs, setWorkspaceTotalDialogs] = useState<number | null>(null);
  const [serverQueryApplied, setServerQueryApplied] = useState<boolean>(false);
  const [liveOk, setLiveOk] = useState<boolean>(false);
  const [syncing, setSyncing] = useState<boolean>(false);

  const [selectedDialogId, setSelectedDialogId] = useState<number | null>(null);
  const [pinnedDialogId, setPinnedDialogId] = useState<number | null>(null);
  const [threadViewState, setThreadViewState] = useState<ThreadViewState>("empty");
  const [threadError, setThreadError] = useState<string>("");
  const [messagesByDialog, setMessagesByDialog] = useState<Record<number, MessageItem[]>>({});

  const [replyToMessage, setReplyToMessage] = useState<MessageItem | null>(null);
  const [editingMessage, setEditingMessage] = useState<MessageItem | null>(null);
  const [draftRevision, setDraftRevision] = useState<number>(0);

  const [profilesByKey, setProfilesByKey] = useState<Record<string, ClientProfileData>>({});
  const [cardsByLeadId, setCardsByLeadId] = useState<Record<number, LeadItem>>({});
  const [switchingAccount, setSwitchingAccount] = useState<boolean>(false);

  const stateRef = useRef({
    accounts,
    leads,
    stages,
    inboxMode,
    searchMode,
    sessionFilter,
    activeChips,
    query,
    telegramSubmittedQuery,
    dialogsById,
    dialogOrder,
    selectedDialogId,
    pinnedDialogId,
    counts,
    liveOk,
  });
  stateRef.current = {
    accounts,
    leads,
    stages,
    inboxMode,
    searchMode,
    sessionFilter,
    activeChips,
    query,
    telegramSubmittedQuery,
    dialogsById,
    dialogOrder,
    selectedDialogId,
    pinnedDialogId,
    counts,
    liveOk,
  };

  const inboxFetchTokenRef = useRef(0);
  const dialogLoadTokenRef = useRef(0);
  const pendingReadIdsRef = useRef<Set<number>>(new Set());
  const sseRef = useRef<EventSource | null>(null);
  const draftKeys = useMemo(() => listDraftKeysSnapshot(), [draftRevision, selectedDialogId]);

  const connectedAccounts = useMemo(
    () => accounts.filter((a) => a.status === "connected"),
    [accounts],
  );

  // Initial metadata hydration if __CRM_BRIDGE__ didn't have boot data yet
  useEffect(() => {
    let active = true;
    const bootNow = window.__CRM_BRIDGE__?.getBootData?.();
    if (!bootNow?.accounts?.length) {
      fetchAccountsApi()
        .then((rows) => {
          if (active && rows.length) setAccounts(rows);
        })
        .catch(() => {});
    }
    if (!bootNow?.leads?.length) {
      fetchLeadsApi()
        .then((rows) => {
          if (active && rows.length) setLeads(rows);
        })
        .catch(() => {});
    }
    if (!bootNow?.stages?.length) {
      fetchStagesApi()
        .then((rows) => {
          if (active && rows.length) setStages(rows);
        })
        .catch(() => {});
    }
    fetchSnippetsApi()
      .then((rows) => {
        if (active) setSnippets(rows);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  const upsertDialogsBatch = useCallback(
    (rows: DialogItem[], replaceList = false) => {
      const sorted = sortDialogsByRecency(rows);
      setDialogsById((prev) => {
        const next = replaceList ? { ...prev } : { ...prev };
        for (const r of sorted) {
          if (!r?.id) continue;
          const id = Number(r.id);
          const existing = prev[id];
          const isPinnedRead =
            stateRef.current.pinnedDialogId != null &&
            id === Number(stateRef.current.pinnedDialogId) &&
            stateRef.current.selectedDialogId === id &&
            Number(existing?.unread_count || 0) === 0;
          const forcedRead = pendingReadIdsRef.current.has(id) || isPinnedRead;
          next[id] = {
            ...(existing || {}),
            ...r,
            ...(forcedRead ? { unread_count: 0 } : {}),
          };
        }
        return next;
      });
      if (replaceList) {
        const nextIds = sorted.map((d) => Number(d.id));
        const curPinned = stateRef.current.pinnedDialogId;
        if (
          curPinned != null &&
          stateRef.current.selectedDialogId === curPinned &&
          !nextIds.includes(Number(curPinned))
        ) {
          nextIds.unshift(Number(curPinned));
        }
        setDialogOrder(nextIds);
      } else {
        setDialogOrder((prevOrder) => {
          const idSet = new Set(prevOrder);
          for (const d of sorted) idSet.add(Number(d.id));
          return [...idSet];
        });
      }
    },
    [],
  );

  const loadInboxList = useCallback(
    async (opts?: {
      session?: string;
      mode?: InboxMode;
      chips?: ChipKey[];
      q?: string;
      silent?: boolean;
    }) => {
      const cur = stateRef.current;
      const effSession = opts?.session ?? cur.sessionFilter;
      const effMode = opts?.mode ?? cur.inboxMode;
      const effChips = opts?.chips ?? cur.activeChips;
      const effQuery = opts?.q ?? cur.query;
      const connected = cur.accounts.filter((a) => a.status === "connected");
      if (!connected.length) {
        setDialogOrder([]);
        setDialogsById({});
        setAllDialogsPool([]);
        setCounts(EMPTY_COUNTS);
        setInboxViewState("empty");
        syncWorkspaceTotal(0);
        return;
      }

      const token = ++inboxFetchTokenRef.current;
      if (!opts?.silent && !cur.dialogOrder.length) {
        setInboxViewState("loading");
      }
      setInboxError("");

      const targetAccountId =
        effSession === "all" && connected.length > 1
          ? "0"
          : effSession !== "all"
            ? String(effSession)
            : String(connected[0]?.id || "0");

      try {
        const res = await fetchInbox({
          accountId: targetAccountId,
          groupByLead: effMode === "unified",
          chips: effChips,
          q: effQuery,
        });
        if (token !== inboxFetchTokenRef.current) return;

        const sorted = sortDialogsByRecency(res.dialogs || []);
        upsertDialogsBatch(sorted, true);
        setServerQueryApplied(Boolean(effQuery.trim() && (res.q_applied || res.search_applied)));

        const isUnfilteredAll = effSession === "all" && effChips.length === 0 && !effQuery.trim();
        if (isUnfilteredAll) {
          setAllDialogsPool(sorted);
        }

        const totalResolved = res.workspace_total_dialogs ?? res.total_dialogs ?? null;
        if (totalResolved != null) {
          const n = Number(totalResolved);
          setWorkspaceTotalDialogs(n);
          syncWorkspaceTotal(n);
        } else if (isUnfilteredAll) {
          setWorkspaceTotalDialogs(sorted.length);
          syncWorkspaceTotal(sorted.length);
        }

        const poolForCounts = isUnfilteredAll ? sorted : allDialogsPool.length ? allDialogsPool : sorted;
        setCounts(resolveCountsFromResponse(res, effSession, connected.length, poolForCounts, cur.leads));
        setInboxViewState(sorted.length ? "filled" : effQuery.trim() || effChips.length ? "none" : "empty");
      } catch (err) {
        if (token !== inboxFetchTokenRef.current) return;
        const msg = err instanceof Error ? err.message : "Ошибка загрузки диалогов";
        setInboxError(msg);
        setInboxViewState("error");
      }
    },
    [allDialogsPool, upsertDialogsBatch],
  );

  // Trigger initial load once connected accounts appear or change
  const connectedIdsKey = useMemo(
    () => connectedAccounts.map((a) => Number(a.id)).sort((a, b) => a - b).join(","),
    [connectedAccounts],
  );
  const prevConnectedKeyRef = useRef<string>("__init__");
  useEffect(() => {
    if (connectedIdsKey !== prevConnectedKeyRef.current) {
      prevConnectedKeyRef.current = connectedIdsKey;
      loadInboxList({ silent: true });
    }
  }, [connectedIdsKey, loadInboxList]);

  // Load client profile & lead card when selectedDialogId changes
  const selectedDialog = selectedDialogId != null ? dialogsById[selectedDialogId] || null : null;
  const selectedLead = useMemo(
    () => matchLeadForDialog(selectedDialog, leads),
    [selectedDialog, leads],
  );

  const loadContextForDialog = useCallback(
    async (dlg: DialogItem | null) => {
      if (!dlg?.id) return;
      const lead = matchLeadForDialog(dlg, stateRef.current.leads);
      const leadId = dlg.lead_id ?? lead?.id ?? null;
      const pKey = `${dlg.id}:${leadId ?? "none"}`;
      try {
        const prof = await fetchClientProfile(dlg.id, leadId);
        setProfilesByKey((prev) => ({ ...prev, [pKey]: prof }));
        if (prof.lead?.id) {
          setCardsByLeadId((prev) => ({ ...prev, [prof.lead!.id]: prof.lead! }));
        }
      } catch {
        /* non-fatal profile load */
      }
      if (leadId != null) {
        try {
          const card = await fetchLeadCard(Number(leadId));
          setCardsByLeadId((prev) => ({ ...prev, [Number(leadId)]: card }));
        } catch {
          /* non-fatal */
        }
      }
    },
    [],
  );

  const selectDialog = useCallback(
    async (dialog: DialogItem | null, opts?: { markRead?: boolean; forceReload?: boolean }) => {
      const token = ++dialogLoadTokenRef.current;
      if (!dialog) {
        setSelectedDialogId(null);
        setPinnedDialogId(null);
        setThreadViewState("empty");
        setThreadError("");
        setReplyToMessage(null);
        setEditingMessage(null);
        return;
      }

      const dlgId = Number(dialog.id);
      setDialogsById((prev) => ({
        ...prev,
        [dlgId]: { ...(prev[dlgId] || {}), ...dialog },
      }));
      setSelectedDialogId(dlgId);
      setReplyToMessage(null);
      setEditingMessage(null);
      setThreadError("");

      const markRead = opts?.markRead !== false;
      if (markRead) {
        setPinnedDialogId(dlgId);
      } else if (stateRef.current.pinnedDialogId !== dlgId) {
        setPinnedDialogId(null);
      }

      const cachedMsgs = messagesByDialog[dlgId];
      if (!cachedMsgs || opts?.forceReload) {
        setThreadViewState("loading");
      } else {
        setThreadViewState("filled");
      }

      const preFetchStamp = normalizeDialogStamp(dialog.last_message_at);
      let loadedMsgs: MessageItem[] = [];
      let loadErr = "";
      try {
        loadedMsgs = await fetchDialogMessages(dlgId);
      } catch (err) {
        loadErr = err instanceof Error ? err.message : "Не удалось загрузить сообщения";
      }
      if (token !== dialogLoadTokenRef.current) return;

      setMessagesByDialog((prev) => ({
        ...prev,
        [dlgId]: mergeMessageLists(prev[dlgId] || [], loadedMsgs),
      }));
      setThreadViewState("filled");
      if (loadErr) {
        setThreadError(loadErr);
      } else if (markRead) {
        const liveRow = stateRef.current.dialogsById[dlgId] || dialog;
        const liveStamp = normalizeDialogStamp(liveRow.last_message_at);
        const newestStamp = loadedMsgs.length
          ? normalizeDialogStamp(loadedMsgs[loadedMsgs.length - 1].sent_at)
          : "";
        const newerSse = Boolean(liveStamp && liveStamp > preFetchStamp && (!newestStamp || liveStamp > newestStamp));
        const effectiveUnread = Math.max(Number(dialog.unread_count || 0), Number(liveRow.unread_count || 0));
        if (!newerSse && effectiveUnread > 0) {
          pendingReadIdsRef.current.add(dlgId);
          setDialogsById((prev) => ({
            ...prev,
            [dlgId]: { ...(prev[dlgId] || dialog), unread_count: 0 },
          }));
          markDialogReadApi(dlgId)
            .then((res) => {
              if (res && typeof res === "object" && "counts" in res) {
                const payload = res as unknown as InboxResponsePayload;
                setCounts(
                  resolveCountsFromResponse(
                    payload,
                    stateRef.current.sessionFilter,
                    stateRef.current.accounts.filter((a) => a.status === "connected").length,
                    allDialogsPool,
                    stateRef.current.leads,
                  ),
                );
              }
            })
            .catch(() => {})
            .finally(() => {
              pendingReadIdsRef.current.delete(dlgId);
            });
        }
      }

      loadContextForDialog(dialog).catch(() => {});
    },
    [allDialogsPool, loadContextForDialog, messagesByDialog],
  );

  // Persistent SSE connection (/api/inbox/events?account_id=0&crm_only=1)
  useEffect(() => {
    if (!connectedAccounts.length) {
      if (sseRef.current) {
        sseRef.current.close();
        sseRef.current = null;
      }
      setLiveOk(false);
      syncLivePill(false);
      return;
    }

    const src = new EventSource("/api/inbox/events?account_id=0&crm_only=1");
    sseRef.current = src;

    src.addEventListener("error", () => {
      if (sseRef.current !== src) return;
      setLiveOk(false);
      syncLivePill(false);
    });

    src.addEventListener("live", (ev) => {
      if (sseRef.current !== src) return;
      let ok = false;
      try {
        ok = JSON.parse((ev as MessageEvent).data).ok === true;
      } catch {
        ok = false;
      }
      setLiveOk(ok);
      syncLivePill(ok);
    });

    src.addEventListener("meta", (ev) => {
      if (sseRef.current !== src) return;
      try {
        const meta = JSON.parse((ev as MessageEvent).data);
        if (meta?.workspace_total_dialogs != null) {
          const total = Number(meta.workspace_total_dialogs);
          setWorkspaceTotalDialogs(total);
          syncWorkspaceTotal(total);
        }
      } catch {
        /* ignore malformed meta */
      }
    });

    src.addEventListener("snapshot", (ev) => {
      if (sseRef.current !== src) return;
      const cur = stateRef.current;
      if (cur.searchMode === "telegram" && cur.telegramSubmittedQuery.trim()) return;
      if (cur.query.trim()) return;
      try {
        const parsed = JSON.parse((ev as MessageEvent).data);
        const rows: DialogItem[] | null = Array.isArray(parsed)
          ? parsed
          : Array.isArray(parsed?.dialogs)
            ? parsed.dialogs
            : null;
        if (!Array.isArray(rows)) return;
        const sorted = sortDialogsByRecency(rows);
        if (parsed && !Array.isArray(parsed) && parsed.workspace_total_dialogs != null) {
          const total = Number(parsed.workspace_total_dialogs);
          setWorkspaceTotalDialogs(total);
          syncWorkspaceTotal(total);
        } else if (cur.sessionFilter === "all") {
          setWorkspaceTotalDialogs(sorted.length);
          syncWorkspaceTotal(sorted.length);
        }
        setAllDialogsPool(sorted);
        if (cur.inboxMode !== "unified") {
          upsertDialogsBatch(sorted, true);
          if (!cur.activeChips.length && cur.sessionFilter === "all") {
            setCounts(computeLocalCounts(sorted, "all", cur.leads));
            setInboxViewState(sorted.length ? "filled" : "empty");
          }
        } else {
          // In unified mode, patch individual dialog updates cleanly without breaking peer_dialogs grouping
          upsertDialogsBatch(sorted, false);
        }
      } catch {
        /* ignore malformed snapshot */
      }
    });

    src.addEventListener("dialog", (ev) => {
      if (sseRef.current !== src) return;
      try {
        const incoming = JSON.parse((ev as MessageEvent).data) as DialogItem;
        if (!incoming?.id) return;
        const dlgId = Number(incoming.id);
        const cur = stateRef.current;
        const isCurrentSelected =
          cur.selectedDialogId === dlgId ||
          Boolean(
            cur.selectedDialogId != null &&
              cur.dialogsById[cur.selectedDialogId]?.peer_dialogs?.some((p) => Number(p.dialog_id) === dlgId),
          );

        setDialogsById((prev) => {
          const prevRow = prev[dlgId];
          const merged = { ...(prevRow || {}), ...incoming };
          if (pendingReadIdsRef.current.has(dlgId)) merged.unread_count = 0;
          return { ...prev, [dlgId]: merged };
        });

        setDialogOrder((prevOrder) => {
          if (prevOrder.includes(dlgId)) return prevOrder;
          return [dlgId, ...prevOrder];
        });

        // Incrementally patch active thread messages without full redraw or scroll jump
        if (isCurrentSelected) {
          const activeId = cur.selectedDialogId!;
          fetchDialogMessages(activeId)
            .then((msgs) => {
              setMessagesByDialog((prev) => ({
                ...prev,
                [activeId]: mergeMessageLists(prev[activeId] || [], msgs),
              }));
            })
            .catch(() => {});
        }
      } catch {
        /* wait for next frame */
      }
    });

    return () => {
      src.close();
      if (sseRef.current === src) sseRef.current = null;
    };
  }, [connectedAccounts.length, upsertDialogsBatch]);

  // Visible dialogs computed cleanly with pinnedDialogId support + client filtering
  const visibleDialogs = useMemo(() => {
    const rawList = dialogOrder.map((id) => dialogsById[id]).filter(Boolean);
    const isTgSearch = searchMode === "telegram" && Boolean(telegramSubmittedQuery.trim());
    const cleanQ = isTgSearch ? "" : query.trim().toLowerCase();
    const cleanQNoAt = cleanQ.replace(/^@+/, "");

    const filtered = rawList.filter((dialog) => {
      if (sessionFilter !== "all") {
        const targetId = Number(sessionFilter);
        const matchesMain = Number(dialog.account_id) === targetId;
        const matchesPeer = Boolean(dialog.peer_dialogs?.some((p) => Number(p.account_id) === targetId));
        if (!matchesMain && !matchesPeer) return false;
      }

      if (cleanQ && !serverQueryApplied) {
        const lead = matchLeadForDialog(dialog, leads);
        const haystack = [
          dialog.name,
          dialog.title,
          dialog.username,
          dialog.phone,
          dialog.company,
          dialog.lead_company,
          dialog.last_message_preview,
          lead?.name,
          lead?.company,
          lead?.username,
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(cleanQ) && (!cleanQNoAt || !haystack.includes(cleanQNoAt))) {
          return false;
        }
      }

      if (pinnedDialogId != null && Number(dialog.id) === Number(pinnedDialogId) && selectedDialogId === Number(dialog.id)) {
        return true;
      }

      const lead = matchLeadForDialog(dialog, leads);
      if (activeChips.includes("waiting") && !isDialogWaitingOurAnswer(dialog, lead)) return false;
      if (activeChips.includes("our_turn") && Number(dialog.last_from_peer || 0) !== 0) return false;
      if (activeChips.includes("unread") && Number(dialog.unread_count || 0) <= 0) return false;
      if (activeChips.includes("muted") && !dialog.muted) return false;
      return true;
    });

    return sortDialogsByRecency(filtered);
  }, [
    activeChips,
    dialogOrder,
    dialogsById,
    leads,
    pinnedDialogId,
    query,
    searchMode,
    selectedDialogId,
    serverQueryApplied,
    sessionFilter,
    telegramSubmittedQuery,
  ]);

  const effectiveInboxViewState: InboxViewState = useMemo(() => {
    if (inboxViewState === "loading" || inboxViewState === "error") return inboxViewState;
    if (visibleDialogs.length > 0) return "filled";
    if (dialogOrder.length === 0 && !query.trim() && activeChips.length === 0 && !telegramSubmittedQuery.trim()) {
      return "empty";
    }
    return "none";
  }, [activeChips.length, dialogOrder.length, inboxViewState, query, telegramSubmittedQuery, visibleDialogs.length]);

  // Search debounce in CRM mode
  const crmSearchTimerRef = useRef<number>(0);
  const handleQueryChange = useCallback(
    (nextVal: string) => {
      setQuery(nextVal);
      setServerQueryApplied(false);
      if (searchMode === "crm") {
        window.clearTimeout(crmSearchTimerRef.current);
        crmSearchTimerRef.current = window.setTimeout(() => {
          loadInboxList({ q: nextVal, silent: true });
        }, 220);
      } else if (!nextVal.trim() && telegramSubmittedQuery) {
        setTelegramSubmittedQuery("");
        loadInboxList({ q: "" });
      }
    },
    [loadInboxList, searchMode, telegramSubmittedQuery],
  );

  const handleSearchSubmit = useCallback(
    async (explicitQuery?: string) => {
      window.clearTimeout(crmSearchTimerRef.current);
      const qVal = (explicitQuery ?? query).trim();
      if (searchMode === "telegram") {
        const connected = stateRef.current.accounts.filter((a) => a.status === "connected");
        if (!connected.length) {
          setDialogsById({});
          setDialogOrder([]);
          setInboxError("Нет подключённого аккаунта");
          setInboxViewState("error");
          return;
        }
        setTelegramSubmittedQuery(qVal);
        setInboxViewState("loading");
        setInboxError("");
        const targetAcc =
          sessionFilter === "all" ? "0" : String(sessionFilter || connected[0]?.id || "0");
        try {
          const rows = await searchTelegramInbox(targetAcc, qVal);
          upsertDialogsBatch(rows, true);
          setInboxViewState(rows.length ? "filled" : "none");
        } catch (err) {
          const msg = err instanceof Error ? err.message : "Ошибка поиска в Telegram";
          setInboxError(msg);
          setInboxViewState("error");
          notifyToast(msg, "error");
        }
      } else {
        setTelegramSubmittedQuery("");
        await loadInboxList({ q: qVal });
      }
    },
    [loadInboxList, query, searchMode, sessionFilter, upsertDialogsBatch],
  );

  const handleSearchModeChange = useCallback(
    (nextMode: SearchMode) => {
      setSearchMode(nextMode);
      if (nextMode === "telegram") {
        setServerQueryApplied(false);
      } else if (telegramSubmittedQuery) {
        setTelegramSubmittedQuery("");
        loadInboxList({ q: query });
      }
    },
    [loadInboxList, query, telegramSubmittedQuery],
  );

  const handleSessionChange = useCallback(
    async (nextSession: string) => {
      setSessionFilter(nextSession);
      setPinnedDialogId(null);
      await loadInboxList({ session: nextSession });
    },
    [loadInboxList],
  );

  const handleInboxModeChange = useCallback(
    async (nextMode: InboxMode) => {
      setInboxMode(nextMode);
      await loadInboxList({ mode: nextMode });
    },
    [loadInboxList],
  );

  const handleToggleChip = useCallback(
    async (chip: ChipKey) => {
      setPinnedDialogId(null);
      const prev = stateRef.current.activeChips;
      const isOn = prev.includes(chip);
      let next: ChipKey[];
      if (isOn) {
        next = prev.filter((c) => c !== chip);
      } else {
        next = [...prev, chip];
        if (chip === "waiting") next = next.filter((c) => c !== "our_turn");
        if (chip === "our_turn") next = next.filter((c) => c !== "waiting");
      }
      setActiveChips(next);
      if (!stateRef.current.telegramSubmittedQuery) {
        await loadInboxList({ chips: next, silent: true });
      }
    },
    [loadInboxList],
  );

  const handleSyncNow = useCallback(async () => {
    setSyncing(true);
    try {
      const accId = sessionFilter !== "all" ? Number(sessionFilter) : null;
      await syncInboxNow(accId);
      await loadInboxList();
      if (selectedDialogId != null) {
        const msgs = await fetchDialogMessages(selectedDialogId);
        setMessagesByDialog((prev) => ({
          ...prev,
          [selectedDialogId]: mergeMessageLists(prev[selectedDialogId] || [], msgs),
        }));
      }
      notifyToast("Список диалогов CRM синхронизирован", "ok");
    } catch (err) {
      notifyToast(err instanceof Error ? err.message : "Не удалось синхронизировать чаты", "error");
    } finally {
      setSyncing(false);
    }
  }, [loadInboxList, selectedDialogId, sessionFilter]);

  const handleSendText = useCallback(
    async (
      text: string,
      opts?: { tempIdToRetry?: string },
    ): Promise<"sent" | "failed-bubble" | "rejected"> => {
      const dlg = selectedDialog;
      setThreadError("");
      if (!dlg) {
        setThreadError("Выберите разговор");
        return "rejected";
      }
      const ownerAcc = accounts.find((a) => Number(a.id) === Number(dlg.account_id));
      if (!ownerAcc || ownerAcc.status !== "connected") {
        setThreadError("Аккаунт не подключён");
        notifyToast("Аккаунт не подключён", "error");
        return "rejected";
      }
      const clean = text.trim();
      if (!clean) {
        setThreadError("Напишите сообщение");
        notifyToast("Напишите сообщение", "error");
        return "rejected";
      }
      if (!dlg.id) {
        setThreadError("Чат ещё не в кэше");
        notifyToast("Чат ещё не в кэше", "error");
        return "rejected";
      }

      if (editingMessage) {
        const targetId = editingMessage.telegram_message_id || editingMessage.id;
        try {
          const updated = await editDialogMessage(dlg.id, Number(targetId), clean);
          setMessagesByDialog((prev) => ({
            ...prev,
            [dlg.id]: (prev[dlg.id] || []).map((m) =>
              Number(m.telegram_message_id || m.id) === Number(targetId)
                ? { ...m, ...updated, text: clean, edited_at: updated.edited_at || new Date().toISOString() }
                : m,
            ),
          }));
          setDialogsById((prev) => ({
            ...prev,
            [dlg.id]: { ...prev[dlg.id], last_message_preview: clean },
          }));
          setEditingMessage(null);
          notifyToast("Сообщение изменено", "ok");
          return "sent";
        } catch (err) {
          const msg = err instanceof Error ? err.message : "Не удалось изменить сообщение";
          setThreadError(msg);
          notifyToast(msg, "error");
          return "rejected";
        }
      }

      const tempId = opts?.tempIdToRetry || `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      const replyId = replyToMessage ? replyToMessage.telegram_message_id || replyToMessage.id : null;
      const optimistic: MessageItem = {
        id: -Date.now(),
        dialog_id: dlg.id,
        direction: "out",
        text: clean,
        sent_at: new Date().toISOString(),
        reply_to_telegram_message_id: replyId ? Number(replyId) : null,
        clientTempId: tempId,
        localStatus: "sending",
      };

      setMessagesByDialog((prev) => {
        const list = prev[dlg.id] || [];
        const withoutOldTemp = list.filter((m) => m.clientTempId !== tempId);
        return { ...prev, [dlg.id]: [...withoutOldTemp, optimistic] };
      });
      setReplyToMessage(null);

      try {
        const sent = await sendDialogMessage(dlg.id, clean, replyId ? Number(replyId) : null);
        const hadUnread = Number(dlg.unread_count || 0) > 0;
        setMessagesByDialog((prev) => {
          const filtered = (prev[dlg.id] || []).filter((m) => m.clientTempId !== tempId);
          return { ...prev, [dlg.id]: mergeMessageLists(filtered, [sent]) };
        });
        setPinnedDialogId(dlg.id);
        setDialogsById((prev) => ({
          ...prev,
          [dlg.id]: {
            ...(prev[dlg.id] || dlg),
            last_message_preview: clean,
            last_from_peer: 0,
            last_message_at: sent.sent_at || new Date().toISOString(),
            unread_count: 0,
            waiting_our_answer: false,
            is_waiting_our_answer: false,
          },
        }));
        writeDraft(dlg, "");
        setDraftRevision((r) => r + 1);
        if (hadUnread) {
          markDialogReadApi(dlg.id).catch(() => {});
        }
        return "sent";
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Ошибка отправки";
        const isClientRejection = err instanceof ApiError && err.status >= 400 && err.status < 500;
        setMessagesByDialog((prev) => {
          const list = prev[dlg.id] || [];
          if (isClientRejection) {
            return { ...prev, [dlg.id]: list.filter((m) => m.clientTempId !== tempId) };
          }
          return {
            ...prev,
            [dlg.id]: list.map((m) =>
              m.clientTempId === tempId ? { ...m, localStatus: "failed", localError: msg } : m,
            ),
          };
        });
        setThreadError(msg);
        notifyToast(msg, "error");
        return isClientRejection ? "rejected" : "failed-bubble";
      }
    },
    [accounts, editingMessage, replyToMessage, selectedDialog],
  );

  const handleSendMedia = useCallback(
    async (payload: {
      media_type: string;
      filename: string;
      mime_type: string;
      data_base64: string;
      caption?: string;
      duration?: number;
    }): Promise<boolean> => {
      const dlg = selectedDialog;
      if (!dlg?.id) {
        notifyToast("Сначала выберите чат", "error");
        return false;
      }
      const replyId = replyToMessage ? replyToMessage.telegram_message_id || replyToMessage.id : null;
      try {
        const sent = await sendDialogMedia(dlg.id, {
          ...payload,
          reply_to_msg_id: replyId ? Number(replyId) : null,
        });
        const hadUnread = Number(dlg.unread_count || 0) > 0;
        setMessagesByDialog((prev) => ({
          ...prev,
          [dlg.id]: mergeMessageLists(prev[dlg.id] || [], [sent]),
        }));
        setPinnedDialogId(dlg.id);
        setDialogsById((prev) => ({
          ...prev,
          [dlg.id]: {
            ...(prev[dlg.id] || dlg),
            last_message_preview:
              sent.text || (payload.media_type === "voice" ? "🎤 Голосовое сообщение" : `📎 ${payload.filename}`),
            last_from_peer: 0,
            last_message_at: sent.sent_at || new Date().toISOString(),
            unread_count: 0,
            waiting_our_answer: false,
            is_waiting_our_answer: false,
          },
        }));
        setReplyToMessage(null);
        writeDraft(dlg, "");
        setDraftRevision((r) => r + 1);
        if (hadUnread) {
          markDialogReadApi(dlg.id).catch(() => {});
        }
        return true;
      } catch (err) {
        notifyToast(err instanceof Error ? err.message : "Ошибка отправки медиа", "error");
        return false;
      }
    },
    [replyToMessage, selectedDialog],
  );

  const handleMarkUnread = useCallback(async () => {
    const dlg = selectedDialog;
    if (!dlg?.id) {
      setThreadError("Выберите разговор");
      notifyToast("Выберите разговор", "error");
      return;
    }
    try {
      pendingReadIdsRef.current.delete(dlg.id);
      const res = await markDialogUnreadApi(dlg.id);
      setPinnedDialogId(dlg.id);
      const nextUnread = Number((res as { unread_count?: number })?.unread_count ?? Math.max(1, Number(dlg.unread_count) || 0));
      setDialogsById((prev) => ({
        ...prev,
        [dlg.id]: {
          ...(prev[dlg.id] || dlg),
          unread_count: nextUnread,
          last_from_peer: 1,
          is_waiting_our_answer: true,
        },
      }));
      if (res && "counts" in res) {
        setCounts(
          resolveCountsFromResponse(
            res as unknown as InboxResponsePayload,
            sessionFilter,
            connectedAccounts.length,
            allDialogsPool,
            leads,
          ),
        );
      }
      notifyToast("Диалог отмечен непрочитанным", "ok");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Не удалось отметить непрочитанным";
      setThreadError(msg);
      notifyToast(msg, "error");
    }
  }, [allDialogsPool, connectedAccounts.length, leads, selectedDialog, sessionFilter]);

  const handleMoveStage = useCallback(
    async (targetStatus: string) => {
      const dlg = selectedDialog;
      if (!dlg || !targetStatus) return;
      const existingLead = matchLeadForDialog(dlg, leads);
      try {
        let updated: LeadItem;
        if (existingLead?.id) {
          updated = await patchLeadApi(existingLead.id, { status: targetStatus });
        } else {
          const accId = dlg.account_id || connectedAccounts[0]?.id || null;
          updated = await createLeadApi({
            account_id: accId,
            telegram_user_id: dlg.telegram_user_id ?? dlg.peer_id ?? null,
            username: dlg.username || null,
            name: dlg.name || dlg.title || dlg.username || "Контакт",
            phone: dlg.phone || null,
            status: targetStatus,
            dialog_id: dlg.id,
          });
        }
        setLeads((prev) => {
          const idx = prev.findIndex((l) => Number(l.id) === Number(updated.id));
          if (idx >= 0) {
            const copy = [...prev];
            copy[idx] = { ...copy[idx], ...updated };
            return copy;
          }
          return [updated, ...prev];
        });
        setDialogsById((prev) => ({
          ...prev,
          [dlg.id]: { ...prev[dlg.id], lead_id: updated.id, lead_status: updated.status },
        }));
        window.__CRM_BRIDGE__?.onLeadUpdated?.(updated);
        loadContextForDialog({ ...dlg, lead_id: updated.id, lead_status: updated.status }).catch(() => {});
        const stLabel = stages.find((s) => s.key === targetStatus)?.label || targetStatus;
        notifyToast(`Этап: «${stLabel}»`, "ok");
      } catch (err) {
        notifyToast(err instanceof Error ? err.message : "Не удалось сменить этап", "error");
      }
    },
    [connectedAccounts, leads, loadContextForDialog, selectedDialog, stages],
  );

  const handleCreateLeadFromDialog = useCallback(async () => {
    const dlg = selectedDialog;
    if (!dlg) return;
    try {
      const created = await createLeadApi({
        account_id: dlg.account_id,
        telegram_user_id: dlg.telegram_user_id ?? dlg.peer_id ?? null,
        username: dlg.username || null,
        name: dlg.name || dlg.title || dlg.username || "Контакт",
        phone: dlg.phone || null,
        dialog_id: dlg.id,
      });
      setLeads((prev) => [created, ...prev]);
      setDialogsById((prev) => ({
        ...prev,
        [dlg.id]: { ...prev[dlg.id], lead_id: created.id, lead_status: created.status },
      }));
      window.__CRM_BRIDGE__?.onLeadUpdated?.(created);
      loadContextForDialog({ ...dlg, lead_id: created.id }).catch(() => {});
      notifyToast("Записан в лиды", "ok");
    } catch (err) {
      notifyToast(err instanceof Error ? err.message : "Не удалось создать лида", "error");
    }
  }, [loadContextForDialog, selectedDialog]);

  const handleSwitchAccount = useCallback(
    async (targetAccountId: number) => {
      const dlg = selectedDialog;
      if (!dlg) return;
      const targetAcc = accounts.find((a) => Number(a.id) === Number(targetAccountId));
      if (targetAcc && isAccountDead(targetAcc)) {
        notifyToast("Этот аккаунт недоступен или заморожен", "error");
        return;
      }
      setSwitchingAccount(true);
      try {
        const res = await switchDialogAccountApi({
          dialog_id: dlg.id,
          lead_id: dlg.lead_id ?? selectedLead?.id ?? null,
          peer_id: dlg.peer_id ?? dlg.telegram_user_id ?? null,
          username: dlg.username ?? selectedLead?.username ?? null,
          target_account_id: targetAccountId,
          name: dlg.name ?? selectedLead?.name ?? null,
          phone: dlg.phone ?? selectedLead?.phone ?? null,
        });
        if (res?.dialog?.id) {
          const nextDlg: DialogItem = {
            ...res.dialog,
            peer_dialogs: dlg.peer_dialogs,
          };
          setDialogsById((prev) => ({
            ...prev,
            [nextDlg.id]: nextDlg,
          }));
          setDialogOrder((prevOrder) => {
            if (prevOrder.includes(nextDlg.id)) return prevOrder;
            return prevOrder.map((id) => (id === dlg.id ? nextDlg.id : id));
          });
          if (Array.isArray(res.messages)) {
            setMessagesByDialog((prev) => ({
              ...prev,
              [nextDlg.id]: mergeMessageLists(prev[nextDlg.id] || [], res.messages!),
            }));
          }
          await selectDialog(nextDlg, { markRead: false, forceReload: !res.messages?.length });
          notifyToast(`Сессия переключена`, "ok");
        }
      } catch (err) {
        notifyToast(err instanceof Error ? err.message : "Не удалось переключить аккаунт", "error");
      } finally {
        setSwitchingAccount(false);
      }
    },
    [accounts, selectDialog, selectedDialog, selectedLead],
  );

  const handleAddNote = useCallback(
    async (leadId: number, body: string) => {
      await addLeadNoteApi(leadId, body);
      const card = await fetchLeadCard(leadId).catch(() => null);
      if (card) {
        setCardsByLeadId((prev) => ({ ...prev, [leadId]: card }));
      }
      if (selectedDialog) {
        await loadContextForDialog(selectedDialog);
      }
      notifyToast("Заметка записана", "ok");
    },
    [loadContextForDialog, selectedDialog],
  );

  const handlePatchLeadField = useCallback(async (leadId: number, patch: Record<string, unknown>) => {
    const updated = await patchLeadApi(leadId, patch);
    setLeads((prev) => prev.map((l) => (Number(l.id) === Number(leadId) ? { ...l, ...updated } : l)));
    setCardsByLeadId((prev) => ({ ...prev, [leadId]: { ...(prev[leadId] || {}), ...updated } }));
    window.__CRM_BRIDGE__?.onLeadUpdated?.(updated);
    notifyToast("Сохранено", "ok");
  }, []);

  const handleCreateSnippet = useCallback(
    async (payload: { shortcut: string; title: string; body: string }) => {
      const created = await createSnippetApi(payload);
      const fresh = await fetchSnippetsApi().catch(() => [...snippets, created]);
      setSnippets(fresh);
      notifyToast(`Шаблон ${created.shortcut} добавлен`, "ok");
      return created;
    },
    [snippets],
  );

  const handleDeleteSnippet = useCallback(
    async (snippetId: number) => {
      await deleteSnippetApi(snippetId);
      const fresh = await fetchSnippetsApi().catch(() => snippets.filter((s) => s.id !== snippetId));
      setSnippets(fresh);
      notifyToast("Шаблон удалён", "ok");
    },
    [snippets],
  );

  // Register global window.__INBOX_V2_BRIDGE__
  useEffect(() => {
    window.__INBOX_V2_BRIDGE__ = {
      selectDialogById: async (dialogId: number) => {
        const existing = stateRef.current.dialogsById[dialogId];
        if (existing) {
          await selectDialog(existing, { markRead: true });
          return;
        }
        await loadInboxList({ silent: false });
        const found = stateRef.current.dialogsById[dialogId];
        if (found) await selectDialog(found, { markRead: true });
      },
      openLeadDialog: async (leadOrId: number | LeadItem) => {
        const lead =
          typeof leadOrId === "number"
            ? stateRef.current.leads.find((l) => Number(l.id) === Number(leadOrId))
            : leadOrId;
        if (!lead) return;
        const cleanUser = String(lead.username || "").replace(/^@+/, "").toLowerCase();
        const allVals = Object.values(stateRef.current.dialogsById);
        let found = allVals.find(
          (d) =>
            (lead.id && Number(d.lead_id) === Number(lead.id)) ||
            (lead.telegram_user_id && Number(d.telegram_user_id ?? d.peer_id) === Number(lead.telegram_user_id)) ||
            (cleanUser && String(d.username || "").replace(/^@+/, "").toLowerCase() === cleanUser),
        );
        if (!found) {
          const connected = stateRef.current.accounts.filter((a) => a.status === "connected");
          const accId = lead.account_id || connected[0]?.id;
          if (accId) {
            try {
              found = await openDialogByLeadApi({
                lead_id: lead.id,
                account_id: Number(accId),
                telegram_user_id: lead.telegram_user_id ?? null,
                username: cleanUser || null,
                name: lead.name || cleanUser || "Контакт",
                phone: lead.phone || null,
              });
            } catch {
              found = undefined;
            }
          }
        }
        if (!found) {
          notifyToast("Этот чат ещё не открыт в списке", "error");
          return;
        }
        upsertDialogsBatch([found], false);
        window.__CRM_BRIDGE__?.switchSheet?.("inbox");
        await selectDialog(found, { markRead: true });
      },
      refresh: async () => {
        await Promise.all([
          loadInboxList({ silent: true }),
          fetchSnippetsApi().then(setSnippets).catch(() => {}),
        ]);
      },
      setAccounts: (nextAccounts: AccountItem[]) => {
        if (Array.isArray(nextAccounts)) setAccounts(nextAccounts);
      },
      setLeads: (nextLeads: LeadItem[]) => {
        if (Array.isArray(nextLeads)) setLeads(nextLeads);
      },
      setStages: (nextStages: StageItem[]) => {
        if (Array.isArray(nextStages) && nextStages.length) setStages(nextStages);
      },
      getState: () => {
        const cur = stateRef.current;
        return {
          activeDialogId: cur.selectedDialogId,
          mode: cur.inboxMode,
          searchMode: cur.searchMode,
          sessionFilter: cur.sessionFilter,
          counts: cur.counts,
          dialogCount: cur.dialogOrder.length,
          liveOk: cur.liveOk,
        };
      },
    };
  }, [loadInboxList, selectDialog, upsertDialogsBatch]);

  const selectedMessages = selectedDialogId != null ? messagesByDialog[selectedDialogId] || [] : [];
  const selectedLeadCard = selectedLead?.id != null ? cardsByLeadId[selectedLead.id] || selectedLead : null;
  const selectedProfileKey = selectedDialog ? `${selectedDialog.id}:${selectedDialog.lead_id ?? selectedLead?.id ?? "none"}` : "";
  const selectedProfile = selectedProfileKey ? profilesByKey[selectedProfileKey] || null : null;

  const hasDraftForDialog = useCallback(
    (d: DialogItem): boolean => {
      const k = draftKeyForDialog(d);
      return Boolean(k && draftKeys.has(k));
    },
    [draftKeys],
  );

  return {
    accounts,
    connectedAccounts,
    leads,
    stages,
    snippets,
    inboxMode,
    searchMode,
    sessionFilter,
    activeChips,
    query,
    telegramSubmittedQuery,
    visibleDialogs,
    inboxViewState: effectiveInboxViewState,
    inboxError,
    counts,
    workspaceTotalDialogs,
    liveOk,
    syncing,
    selectedDialog,
    selectedLead: selectedLeadCard,
    selectedProfile,
    selectedMessages,
    threadViewState,
    threadError,
    replyToMessage,
    editingMessage,
    switchingAccount,
    setReplyToMessage,
    setEditingMessage,
    setDraftRevision,
    hasDraftForDialog,
    selectDialog,
    handleQueryChange,
    handleSearchSubmit,
    handleSearchModeChange,
    handleSessionChange,
    handleInboxModeChange,
    handleToggleChip,
    handleSyncNow,
    handleSendText,
    handleSendMedia,
    handleMarkUnread,
    handleMoveStage,
    handleCreateLeadFromDialog,
    handleSwitchAccount,
    handleAddNote,
    handlePatchLeadField,
    handleCreateSnippet,
    handleDeleteSnippet,
  };
}
