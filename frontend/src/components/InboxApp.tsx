import { useEffect, useRef, useState } from "react";
import { Theme } from "@astryxdesign/core/theme";
import { foldeskTheme } from "../theme";
import { useInboxStore } from "../hooks/useInboxStore";
import { DialogListPane } from "./DialogListPane";
import { ThreadPane } from "./ThreadPane";
import { ClientContextPanel } from "./ClientContextPanel";
import { isDialogWaitingOurAnswer, matchLeadForDialog } from "../utils/format";

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName.toUpperCase();
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

export function InboxApp({ hostEl }: { hostEl: HTMLElement }) {
  const store = useInboxStore();
  const [clientPanelOpen, setClientPanelOpen] = useState<boolean>(true);
  const [dealPopupOpen, setDealPopupOpen] = useState<boolean>(false);
  const [mobilePane, setMobilePane] = useState<"list" | "thread">("list");
  const [externalSnippetReq, setExternalSnippetReq] = useState<{
    text: string;
    shortcut: string;
    seq: number;
  } | null>(null);
  const snippetSeqRef = useRef(0);
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    hostEl.dataset.mobilePane = mobilePane;
  }, [hostEl, mobilePane]);

  useEffect(() => {
    if (store.selectedDialog) {
      setMobilePane("thread");
    } else {
      setMobilePane("list");
    }
  }, [store.selectedDialog?.id]);

  // Global keyboard triage outside inputs/textareas
  useEffect(() => {
    const onKeyDown = (ev: KeyboardEvent) => {
      if (ev.key === "Escape" && dealPopupOpen) {
        setDealPopupOpen(false);
        return;
      }

      // Only handle Inbox shortcuts when Inbox sheet is visible
      const wsMain = document.getElementById("workspace-main");
      const viewMode = wsMain?.dataset.viewMode || "sheet";
      const activeSheet = wsMain?.dataset.activeSheet || "inbox";
      if (viewMode === "sheet" && activeSheet !== "inbox") return;

      if (isEditableTarget(ev.target)) return;

      const list = store.visibleDialogs;
      const curIdx = list.findIndex((d) => Number(d.id) === Number(store.selectedDialog?.id));

      if (ev.key === "j" || ev.key === "J" || (ev.altKey && ev.key === "ArrowDown")) {
        if (!list.length) return;
        ev.preventDefault();
        const nextIdx = curIdx < 0 ? 0 : Math.min(list.length - 1, curIdx + 1);
        store.selectDialog(list[nextIdx], { markRead: true });
        return;
      }

      if (ev.key === "k" || ev.key === "K" || (ev.altKey && ev.key === "ArrowUp")) {
        if (!list.length) return;
        ev.preventDefault();
        const prevIdx = curIdx <= 0 ? 0 : curIdx - 1;
        store.selectDialog(list[prevIdx], { markRead: true });
        return;
      }

      if (ev.key === "e" || ev.key === "E") {
        if (!list.length) return;
        ev.preventDefault();
        const start = curIdx >= 0 ? curIdx + 1 : 0;
        for (let offset = 0; offset < list.length; offset += 1) {
          const idx = (start + offset) % list.length;
          const cand = list[idx];
          const lead = matchLeadForDialog(cand, store.leads);
          if (isDialogWaitingOurAnswer(cand, lead)) {
            store.selectDialog(cand, { markRead: true });
            break;
          }
        }
        return;
      }

      if (ev.key === "/") {
        ev.preventDefault();
        searchInputRef.current?.focus();
        searchInputRef.current?.select();
        return;
      }

      if (ev.key === "?") {
        const pop = document.getElementById("inbox-shortcuts-popover") as HTMLElement & {
          togglePopover?: () => void;
        };
        if (pop?.togglePopover) {
          ev.preventDefault();
          pop.togglePopover();
        }
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [dealPopupOpen, store]);

  const handleOpenFullProfile = () => {
    const leadId = store.selectedLead?.id ?? store.selectedDialog?.lead_id ?? null;
    const dialogId = store.selectedDialog?.id ?? null;
    if (leadId == null && dialogId == null) return;
    window.__CRM_BRIDGE__?.openClientProfile?.(leadId, dialogId);
  };

  return (
    <Theme theme={foldeskTheme} mode="dark">
      <div
        className={`inbox-v2-shell${clientPanelOpen ? " has-client-panel" : ""}`}
        data-mobile-pane={mobilePane}
      >
        <DialogListPane
          accounts={store.accounts}
          connectedAccounts={store.connectedAccounts}
          leads={store.leads}
          stages={store.stages}
          dialogs={store.visibleDialogs}
          selectedDialogId={store.selectedDialog?.id ?? null}
          inboxMode={store.inboxMode}
          searchMode={store.searchMode}
          sessionFilter={store.sessionFilter}
          activeChips={store.activeChips}
          query={store.query}
          telegramSubmittedQuery={store.telegramSubmittedQuery}
          inboxViewState={store.inboxViewState}
          inboxError={store.inboxError}
          counts={store.counts}
          syncing={store.syncing}
          searchInputRef={searchInputRef}
          hasDraftForDialog={store.hasDraftForDialog}
          onSelectDialog={(d) => {
            store.selectDialog(d, { markRead: true });
            setMobilePane("thread");
          }}
          onQueryChange={store.handleQueryChange}
          onSearchSubmit={() => store.handleSearchSubmit()}
          onSearchModeChange={store.handleSearchModeChange}
          onSessionChange={store.handleSessionChange}
          onInboxModeChange={store.handleInboxModeChange}
          onToggleChip={store.handleToggleChip}
          onSyncNow={store.handleSyncNow}
        />

        <ThreadPane
          accounts={store.accounts}
          leads={store.leads}
          stages={store.stages}
          snippets={store.snippets}
          selectedDialog={store.selectedDialog}
          selectedLead={store.selectedLead}
          messages={store.selectedMessages}
          threadViewState={store.threadViewState}
          threadError={store.threadError}
          replyToMessage={store.replyToMessage}
          editingMessage={store.editingMessage}
          switchingAccount={store.switchingAccount}
          isClientPanelOpen={clientPanelOpen}
          onSetReplyTo={store.setReplyToMessage}
          onSetEditing={store.setEditingMessage}
          onDraftSaved={() => store.setDraftRevision((r) => r + 1)}
          onSendText={store.handleSendText}
          onSendMedia={store.handleSendMedia}
          onMarkUnread={store.handleMarkUnread}
          onMoveStage={store.handleMoveStage}
          onSwitchAccount={store.handleSwitchAccount}
          onOpenFullProfile={handleOpenFullProfile}
          onOpenDealPopup={() => {
            setClientPanelOpen(true);
            setDealPopupOpen(true);
          }}
          onToggleClientPanel={() => {
            setDealPopupOpen(false);
            setClientPanelOpen((v) => !v);
          }}
          onMobileBack={() => setMobilePane("list")}
          onDeleteSnippet={store.handleDeleteSnippet}
          externalSnippetRequest={externalSnippetReq}
        />

        <ClientContextPanel
          isOpen={clientPanelOpen}
          isPopupMode={dealPopupOpen}
          selectedDialog={store.selectedDialog}
          selectedLead={store.selectedLead}
          selectedProfile={store.selectedProfile}
          stages={store.stages}
          snippets={store.snippets}
          onClose={() => {
            if (dealPopupOpen) {
              setDealPopupOpen(false);
            } else {
              setClientPanelOpen(false);
            }
          }}
          onOpenFullProfile={handleOpenFullProfile}
          onCreateLead={store.handleCreateLeadFromDialog}
          onPatchLeadField={store.handlePatchLeadField}
          onAddNote={store.handleAddNote}
          onCreateSnippet={store.handleCreateSnippet}
          onApplySnippetText={(text, shortcut) => {
            snippetSeqRef.current += 1;
            setExternalSnippetReq({ text, shortcut, seq: snippetSeqRef.current });
          }}
        />
      </div>
    </Theme>
  );
}
