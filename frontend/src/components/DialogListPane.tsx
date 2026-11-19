import React from "react";
import { Avatar } from "@astryxdesign/core/Avatar";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { RefreshCw, Search, X } from "lucide-react";
import type {
  AccountItem,
  ChipKey,
  DialogItem,
  InboxCounts,
  InboxMode,
  InboxViewState,
  LeadItem,
  SearchMode,
  StageItem,
} from "../types";
import {
  STAGE_BADGE_CLASS,
  accountDisplayHandle,
  formatBubbleTime,
  formatShortTime,
  isAccountDead,
  isDialogWaitingOurAnswer,
  matchLeadForDialog,
  normalizeProvider,
} from "../utils/format";

interface DialogListPaneProps {
  accounts: AccountItem[];
  connectedAccounts: AccountItem[];
  leads: LeadItem[];
  stages: StageItem[];
  dialogs: DialogItem[];
  selectedDialogId: number | null;
  inboxMode: InboxMode;
  searchMode: SearchMode;
  sessionFilter: string;
  activeChips: ChipKey[];
  query: string;
  telegramSubmittedQuery: string;
  inboxViewState: InboxViewState;
  inboxError: string;
  counts: InboxCounts;
  syncing: boolean;
  searchInputRef: React.RefObject<HTMLInputElement | null>;
  hasDraftForDialog: (d: DialogItem) => boolean;
  onSelectDialog: (d: DialogItem) => void;
  onQueryChange: (val: string) => void;
  onSearchSubmit: () => void;
  onSearchModeChange: (mode: SearchMode) => void;
  onSessionChange: (session: string) => void;
  onInboxModeChange: (mode: InboxMode) => void;
  onToggleChip: (chip: ChipKey) => void;
  onSyncNow: () => void;
}

const CHIP_CONFIG: Array<{ key: ChipKey; label: string }> = [
  { key: "waiting", label: "Ждут ответа" },
  { key: "our_turn", label: "Ждём лида" },
  { key: "unread", label: "Непрочитанные" },
  { key: "muted", label: "Без звука" },
];

export function DialogListPane({
  accounts,
  connectedAccounts,
  leads,
  stages,
  dialogs,
  selectedDialogId,
  inboxMode,
  searchMode,
  sessionFilter,
  activeChips,
  query,
  telegramSubmittedQuery,
  inboxViewState,
  inboxError,
  counts,
  syncing,
  searchInputRef,
  hasDraftForDialog,
  onSelectDialog,
  onQueryChange,
  onSearchSubmit,
  onSearchModeChange,
  onSessionChange,
  onInboxModeChange,
  onToggleChip,
  onSyncNow,
}: DialogListPaneProps) {
  const activeAccount = connectedAccounts.find((a) => String(a.id) === sessionFilter) || null;
  const sessionBadgeText =
    sessionFilter === "all"
      ? connectedAccounts.length > 1
        ? `Все сессии (${connectedAccounts.length})`
        : "Все сессии"
      : activeAccount
        ? `Сессия: ${normalizeProvider(activeAccount.provider) === "whatsapp" ? "WA" : "TG"} · ${accountDisplayHandle(activeAccount)}`
        : "Активная сессия";

  const showChipNote =
    searchMode === "telegram" &&
    Boolean(query.trim()) &&
    Boolean(telegramSubmittedQuery.trim()) &&
    activeChips.length > 0;

  return (
    <section
      id="s04"
      className="surface-box pane-col inbox-v2-left"
      aria-labelledby="s04-title"
    >
      <div className="pane-head inbox-v2-left-head">
        <div className="inbox-v2-title-row">
          <h2 id="s04-title">Входящие</h2>
          <span
            id="inbox-crm-only-badge"
            className="badge b-ok"
            title="Отображаются только чаты сделок CRM и ответившие из рассылок"
          >
            Только CRM
          </span>
        </div>
        <div className="inbox-v2-head-actions">
          <span id="inbox-active-session-badge" className="badge b-new">
            {sessionBadgeText}
          </span>
          <Button
            id="tg-web-sync-btn"
            label="Обновить"
            variant="ghost"
            size="sm"
            isLoading={syncing}
            isDisabled={syncing}
            icon={<RefreshCw size={13} aria-hidden="true" />}
            onClick={onSyncNow}
            tooltip="Синхронизировать чаты из Telegram"
          />
        </div>
      </div>

      <div className="inbox-v2-mode-switch" role="group" aria-label="Режим списка диалогов">
        <button
          type="button"
          className={`inbox-mode-tab${inboxMode === "unified" ? " is-active" : ""}`}
          data-inbox-mode="unified"
          aria-pressed={inboxMode === "unified" ? "true" : "false"}
          onClick={() => onInboxModeChange("unified")}
        >
          По лидам
        </button>
        <button
          type="button"
          className={`inbox-mode-tab${inboxMode === "all" ? " is-active" : ""}`}
          data-inbox-mode="all"
          aria-pressed={inboxMode === "all" ? "true" : "false"}
          onClick={() => onInboxModeChange("all")}
        >
          Все чаты
        </button>
      </div>

      <div
        id="inbox-session-switcher"
        className="inbox-session-switcher"
        role="group"
        aria-label="Сессии Telegram"
      >
        <button
          type="button"
          className="chip session-pill inbox-session-pill"
          data-session-filter="all"
          data-session-account="all"
          aria-pressed={sessionFilter === "all" ? "true" : "false"}
          onClick={() => onSessionChange("all")}
        >
          Все сессии
        </button>
        {connectedAccounts.map((acc) => {
          const key = String(acc.id);
          const prefix = normalizeProvider(acc.provider) === "whatsapp" ? "WA" : "TG";
          const label = accountDisplayHandle(acc);
          const dead = isAccountDead(acc);
          return (
            <button
              key={acc.id}
              type="button"
              className={`chip session-pill inbox-session-pill${dead ? " is-unhealthy" : ""}`}
              data-session-filter={key}
              data-session-account={key}
              aria-pressed={sessionFilter === key ? "true" : "false"}
              title={dead ? `Статус сессии: ${acc.health || "офлайн"}` : `Сессия ${label}`}
              onClick={() => onSessionChange(key)}
            >
              {`${prefix} · ${label}`}
            </button>
          );
        })}
      </div>

      <section id="search" className="search-panel" aria-labelledby="search-title">
        <div className="search-top">
          <h3 id="search-title">Поиск</h3>
          <div className="switch" id="search-mode" role="group" aria-label="Где искать">
            <button
              type="button"
              data-mode="crm"
              className={`btn ${searchMode === "crm" ? "secondary" : "ghost"} sm`}
              aria-pressed={searchMode === "crm" ? "true" : "false"}
              onClick={() => onSearchModeChange("crm")}
            >
              В CRM
            </button>
            <button
              type="button"
              data-mode="telegram"
              className={`btn ${searchMode === "telegram" ? "secondary" : "ghost"} sm`}
              aria-pressed={searchMode === "telegram" ? "true" : "false"}
              onClick={() => onSearchModeChange("telegram")}
            >
              В Telegram
            </button>
          </div>
        </div>

        <form
          id="search-form"
          role="search"
          data-typing={query ? "true" : "false"}
          onSubmit={(ev) => {
            ev.preventDefault();
            onSearchSubmit();
          }}
        >
          <label className="tg-only" hidden={searchMode !== "telegram"}>
            Аккаунт
            <select
              id="tg-account"
              value={sessionFilter === "all" ? String(connectedAccounts[0]?.id || "") : sessionFilter}
              onChange={(ev) => onSessionChange(ev.target.value || "all")}
            >
              {connectedAccounts.map((acc) => (
                <option key={acc.id} value={String(acc.id)}>
                  {accountDisplayHandle(acc)}
                </option>
              ))}
            </select>
          </label>
          <div className="search-row">
            <label htmlFor="q">Запрос</label>
            <div className="search-input-group">
              <div className="inbox-v2-search-field">
                <Search size={14} className="inbox-v2-search-icon" aria-hidden="true" />
                <input
                  ref={searchInputRef}
                  id="q"
                  name="q"
                  type="search"
                  placeholder="Имя или @"
                  autoComplete="off"
                  value={query}
                  onChange={(ev) => onQueryChange(ev.target.value)}
                />
                {query ? (
                  <button
                    type="button"
                    className="inbox-v2-clear-q"
                    aria-label="Очистить поиск"
                    onClick={() => {
                      onQueryChange("");
                      if (searchMode === "telegram") onSearchModeChange("crm");
                    }}
                  >
                    <X size={12} aria-hidden="true" />
                  </button>
                ) : null}
              </div>
              <button type="submit" className="btn primary sm">
                Найти
              </button>
            </div>
          </div>
        </form>

        <div className="chips" role="group" aria-label="Фильтр">
          {CHIP_CONFIG.map(({ key, label }) => {
            const val = Number(counts[key] ?? 0) || 0;
            const isPressed = activeChips.includes(key);
            const isHidden = key === "muted" && val === 0 && !isPressed;
            return (
              <button
                key={key}
                type="button"
                className="chip"
                data-chip={key}
                data-count={String(val)}
                aria-pressed={isPressed ? "true" : "false"}
                hidden={isHidden}
                onClick={() => onToggleChip(key)}
              >
                {label} (<span className="chip-count" data-chip-count={key}>{val}</span>)
              </button>
            );
          })}
        </div>
        <p id="chip-note" className="hint" hidden={!showChipNote}>
          В ответе поиска Telegram нет этого признака, поэтому список может опустеть.
        </p>
      </section>

      <div
        id="inbox-view"
        data-state={inboxViewState}
        aria-busy={inboxViewState === "loading" ? "true" : "false"}
      >
        <div
          data-when="loading"
          className="skeleton"
          aria-hidden="true"
          hidden={inboxViewState !== "loading"}
        >
          <span className="sk" />
          <span className="sk" />
          <span className="sk" />
        </div>
        <p data-when="empty" className="hint empty-state" hidden={inboxViewState !== "empty"}>
          Разговоров на столе нет
        </p>
        <p data-when="none" className="hint empty-state" hidden={inboxViewState !== "none"}>
          Ничего не найдено
        </p>
        <p
          data-when="error"
          id="inbox-error"
          className="msg"
          role="alert"
          hidden={inboxViewState !== "error"}
        >
          {inboxError}
        </p>

        <ul
          data-when="filled"
          id="dialog-results"
          className="dialog-list"
          hidden={inboxViewState !== "filled"}
        >
          {dialogs.map((dialog, index) => {
            const name = dialog.name || dialog.title || "Без имени";
            const ownerAcc =
              accounts.find((a) => Number(a.id) === Number(dialog.account_id)) || null;
            const provider = normalizeProvider(dialog.provider || ownerAcc?.provider);
            const userHandle = dialog.username
              ? `@${dialog.username.replace(/^@+/, "")}`
              : provider === "whatsapp" && dialog.phone
                ? dialog.phone
                : "без ника";
            const lead = matchLeadForDialog(dialog, leads);
            const isDraft = hasDraftForDialog(dialog);
            const previewText = dialog.last_message_preview
              ? `${userHandle} · ${dialog.last_message_preview}`
              : userHandle;
            const isSelected = Number(selectedDialogId) === Number(dialog.id);
            const unread = Number(dialog.unread_count || 0);
            const isWaiting = isDialogWaitingOurAnswer(dialog, lead);
            const isOurTurn = Number(dialog.last_from_peer || 0) === 0;
            const stageKey = lead?.status || dialog.lead_status || "";
            const stageLabel =
              stages.find((s) => s.key === stageKey)?.label || (stageKey ? stageKey : "CRM");
            const peerCount = dialog.peer_dialogs?.length || 1;
            const sessionTag =
              dialog.account_username
                ? `@${dialog.account_username.replace(/^@+/, "")}`
                : ownerAcc
                  ? accountDisplayHandle(ownerAcc)
                  : "";
            const timeLabel =
              formatBubbleTime(dialog.last_message_at) || formatShortTime(dialog.last_message_at);

            return (
              <li
                key={dialog.id}
                className={index >= 12 ? "dialog-row-deferred" : undefined}
              >
                <button
                  type="button"
                  className={`dialog${index >= 12 ? " dialog-row-deferred" : ""}`}
                  aria-current={isSelected ? "true" : "false"}
                  aria-label={unread ? `${name}, не прочитано, ${unread}` : `${name}, ${userHandle}`}
                  onClick={() => onSelectDialog(dialog)}
                >
                  <span className="avatar dialog-avatar" aria-hidden="true">
                    <Avatar
                      src={dialog.avatar_url || `/api/dialogs/${dialog.id}/avatar`}
                      name={name}
                      size="md"
                      tooltip={false}
                      role="presentation"
                      aria-hidden="true"
                    />
                  </span>
                  <span className="dialog-body-col">
                    <span className="dialog-name-row">
                      <span
                        className={`channel-badge ${provider === "whatsapp" ? "channel-wa" : "channel-tg"}`}
                      >
                        {provider === "whatsapp" ? "WA" : "TG"}
                      </span>
                      <span className="name">{name}</span>
                      {timeLabel ? (
                        <time className="dialog-time-stamp" dateTime={dialog.last_message_at || ""}>
                          {timeLabel}
                        </time>
                      ) : null}
                    </span>
                    <span className="preview">{previewText}</span>
                    <span className="dialog-meta-row">
                      <span
                        className={`badge ${STAGE_BADGE_CLASS[stageKey] || "b-talk"} dialog-crm-badge`}
                      >
                        {stageLabel}
                      </span>
                      {peerCount > 1 ? (
                        <Badge variant="blue" label={`${peerCount} сессии`} />
                      ) : null}
                      {sessionTag ? <span className="dialog-session-pill">{sessionTag}</span> : null}
                      {isWaiting ? (
                        <span className="badge b-ok dialog-turn-badge">Ждёт ответа</span>
                      ) : isOurTurn && dialog.last_message_at ? (
                        <span className="badge b-off dialog-turn-badge">
                          {formatShortTime(dialog.last_message_at)
                            ? `Ждём лида · ${formatShortTime(dialog.last_message_at)}`
                            : "Ждём лида"}
                        </span>
                      ) : null}
                      {isDraft ? (
                        <Badge variant="warning" label="Черновик" />
                      ) : null}
                    </span>
                  </span>
                  {unread > 0 ? <span className="count">{unread}</span> : null}
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}
