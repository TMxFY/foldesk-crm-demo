import { useEffect, useState } from "react";
import { Badge } from "@astryxdesign/core/Badge";
import { ExternalLink, FileText, Plus, X } from "lucide-react";
import type {
  ClientProfileData,
  DialogItem,
  LeadItem,
  SnippetItem,
  StageItem,
} from "../types";
import {
  LEAD_EVENT_META,
  STAGE_BADGE_CLASS,
  formatMoney,
  formatShortTime,
  normalizeProvider,
  renderSnippetTemplate,
} from "../utils/format";

interface ClientContextPanelProps {
  isOpen: boolean;
  isPopupMode: boolean;
  selectedDialog: DialogItem | null;
  selectedLead: LeadItem | null;
  selectedProfile: ClientProfileData | null;
  stages: StageItem[];
  snippets: SnippetItem[];
  onClose: () => void;
  onOpenFullProfile: () => void;
  onCreateLead: () => void;
  onPatchLeadField: (leadId: number, patch: Record<string, unknown>) => Promise<void>;
  onAddNote: (leadId: number, body: string) => Promise<void>;
  onCreateSnippet: (payload: { shortcut: string; title: string; body: string }) => Promise<unknown>;
  onApplySnippetText: (rendered: string, shortcut: string) => void;
}

export function ClientContextPanel({
  isOpen,
  isPopupMode,
  selectedDialog,
  selectedLead,
  selectedProfile,
  stages,
  snippets,
  onClose,
  onOpenFullProfile,
  onCreateLead,
  onPatchLeadField,
  onAddNote,
  onCreateSnippet,
  onApplySnippetText,
}: ClientContextPanelProps) {
  const [noteInput, setNoteInput] = useState("");
  const [savingNote, setSavingNote] = useState(false);
  const [amountInput, setAmountInput] = useState("");
  const [scShortcut, setScShortcut] = useState("");
  const [scTitle, setScTitle] = useState("");
  const [scBody, setScBody] = useState("");
  const [savingSnippet, setSavingSnippet] = useState(false);

  const effectiveLead = selectedLead || selectedProfile?.lead || null;

  useEffect(() => {
    setAmountInput(
      effectiveLead?.amount != null ? String(Math.round(Number(effectiveLead.amount) || 0)) : "0",
    );
  }, [effectiveLead?.id, effectiveLead?.amount]);

  const sideState: "empty" | "nolead" | "filled" = !selectedDialog
    ? "empty"
    : !effectiveLead
      ? "nolead"
      : "filled";

  const provider = normalizeProvider(effectiveLead?.provider || selectedDialog?.provider);
  const statusKey = effectiveLead?.status || selectedDialog?.lead_status || "new";
  const statusLabel = stages.find((s) => s.key === statusKey)?.label || statusKey;
  const company = effectiveLead?.company || selectedDialog?.lead_company || "";
  const niche = effectiveLead?.niche || selectedDialog?.lead_niche || "";
  const airtableStatus = effectiveLead?.airtable_status || selectedDialog?.lead_airtable_status || "";
  const airtableUrl = effectiveLead?.airtable_url || selectedDialog?.lead_airtable_url || "";
  const events = Array.isArray(effectiveLead?.events) ? effectiveLead.events.slice(-5).reverse() : [];
  const notes = Array.isArray(effectiveLead?.notes) ? effectiveLead.notes : [];
  const latestNote = notes.length ? notes[notes.length - 1].body : effectiveLead?.note || "";
  const tgBio = selectedProfile?.telegram_profile?.bio || "";
  const outreachItems = Array.isArray(selectedProfile?.outreach) ? selectedProfile.outreach : [];

  return (
    <aside
      id="inbox-client-panel"
      className={`inbox-client-panel${isOpen ? " is-open" : " is-collapsed"}${isPopupMode ? " is-popup-open" : ""}`}
      aria-label="Рядом с чатом"
      role="region"
    >
      <section id="s06" className="surface-box inbox-v2-side-card" aria-labelledby="s06-title">
        <div className="pane-head row inbox-v2-side-head">
          <h2 id="s06-title">Карточка сделки и заметки</h2>
          <button
            type="button"
            id="close-deal-popup-btn"
            className="btn ghost sm"
            onClick={onClose}
            aria-label="Закрыть панель клиента"
          >
            <X size={13} aria-hidden="true" /> Закрыть
          </button>
        </div>

        <div id="side-view" data-state={sideState}>
          <p data-when="empty" className="hint empty-state" hidden={sideState !== "empty"}>
            Выберите разговор
          </p>

          <div data-when="nolead" className="stack" hidden={sideState !== "nolead"}>
            <p className="hint">К этому чату лид ещё не заведён</p>
            <div className="row">
              <button
                type="button"
                id="to-leads"
                className="btn primary sm"
                onClick={onCreateLead}
              >
                В лиды
              </button>
              <button
                type="button"
                id="nolead-client-profile-btn"
                className="btn secondary sm"
                onClick={onOpenFullProfile}
              >
                👤 Полный профиль
              </button>
            </div>
          </div>

          <div data-when="filled" id="side-card" hidden={sideState !== "filled"}>
            {effectiveLead ? (
              <div className="panel inbox-v2-panel-body">
                <div className="inbox-v2-side-identity">
                  <h3 className="name">{effectiveLead.name || selectedDialog?.name || "Без имени"}</h3>
                  <div className="row inbox-v2-side-badges">
                    <span className={`channel-badge ${provider === "whatsapp" ? "channel-wa" : "channel-tg"}`}>
                      {provider === "whatsapp" ? "WA" : "TG"}
                    </span>
                    <span className={`badge ${STAGE_BADGE_CLASS[statusKey] || "b-new"}`}>
                      {statusLabel}
                    </span>
                    <span
                      className="badge b-won side-amount-badge"
                      data-side-amount={String(Number(effectiveLead.amount) || 0)}
                    >
                      {formatMoney(effectiveLead.amount, effectiveLead.currency || "USD")}
                    </span>
                  </div>
                </div>

                <div className="inbox-v2-side-fields">
                  <label className="inbox-v2-field">
                    <span>Стадия</span>
                    <select
                      aria-label="Стадия сделки"
                      value={statusKey}
                      onChange={(ev) => onPatchLeadField(effectiveLead.id, { status: ev.target.value })}
                    >
                      {stages.map((st) => (
                        <option key={st.key} value={st.key}>
                          {st.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="inbox-v2-field side-amount-label">
                    <span>Бюджет ($)</span>
                    <input
                      type="number"
                      min={0}
                      step={50}
                      className="lead-amount-input"
                      value={amountInput}
                      onChange={(ev) => setAmountInput(ev.target.value)}
                      onBlur={() => {
                        const parsed = Math.max(0, Math.round(Number(amountInput) || 0));
                        if (parsed !== Number(effectiveLead.amount || 0)) {
                          onPatchLeadField(effectiveLead.id, { amount: parsed });
                        }
                      }}
                    />
                  </label>
                </div>

                {company || niche || airtableStatus ? (
                  <div className="inbox-v2-meta-grid">
                    {company ? (
                      <div className="inbox-v2-meta-chip">
                        <small>Компания</small>
                        <strong>{company}</strong>
                      </div>
                    ) : null}
                    {niche ? (
                      <div className="inbox-v2-meta-chip">
                        <small>Ниша</small>
                        <strong>{niche}</strong>
                      </div>
                    ) : null}
                    {airtableStatus ? (
                      <div className="inbox-v2-meta-chip">
                        <small>Airtable</small>
                        <span className="inbox-v2-airtable-row">
                          <Badge variant="purple" label={airtableStatus} />
                          {airtableUrl ? (
                            <a
                              href={airtableUrl}
                              target="_blank"
                              rel="noreferrer noopener"
                              aria-label="Открыть в Airtable"
                            >
                              <ExternalLink size={12} aria-hidden="true" />
                            </a>
                          ) : null}
                        </span>
                      </div>
                    ) : null}
                  </div>
                ) : null}

                {tgBio ? (
                  <div className="inbox-v2-side-block">
                    <span className="inbox-v2-block-label">Telegram Bio</span>
                    <p className="hint inbox-v2-bio">{tgBio}</p>
                  </div>
                ) : null}

                {outreachItems.length > 0 ? (
                  <div className="inbox-v2-side-block">
                    <span className="inbox-v2-block-label">Аутрич контекст</span>
                    <p className="hint">
                      {`${outreachItems[0].campaign_name || "Кампания"} · Шаг ${outreachItems[0].current_step || 0}`}
                    </p>
                  </div>
                ) : null}

                <div className="inbox-v2-side-block">
                  <span className="inbox-v2-block-label">
                    <FileText size={12} aria-hidden="true" /> Заметки
                  </span>
                  {latestNote ? (
                    <p className="note inbox-v2-latest-note">{latestNote}</p>
                  ) : (
                    <p className="hint">Заметок пока нет</p>
                  )}
                  <form
                    className="inbox-v2-quick-note-form"
                    onSubmit={async (ev) => {
                      ev.preventDefault();
                      const clean = noteInput.trim();
                      if (!clean || savingNote) return;
                      setSavingNote(true);
                      try {
                        await onAddNote(effectiveLead.id, clean);
                        setNoteInput("");
                      } finally {
                        setSavingNote(false);
                      }
                    }}
                  >
                    <input
                      type="text"
                      placeholder="Заметка по клиенту..."
                      aria-label="Быстрая заметка"
                      value={noteInput}
                      onChange={(ev) => setNoteInput(ev.target.value)}
                    />
                    <button type="submit" className="btn secondary sm" disabled={savingNote || !noteInput.trim()}>
                      Добавить
                    </button>
                  </form>
                </div>

                <section className="lead-events-section stack" aria-label="История событий">
                  <div className="row event-section-head">
                    <h4>История событий</h4>
                    <span className="badge b-new event-count-badge">{events.length}</span>
                  </div>
                  {events.length ? (
                    <ol className="event-timeline is-compact">
                      {events.map((ev) => {
                        const meta = LEAD_EVENT_META[ev.event_type] || {
                          label: ev.event_type,
                          badge: "b-new",
                        };
                        return (
                          <li
                            key={ev.id}
                            className="event-timeline-item"
                            data-event-type={ev.event_type || ""}
                          >
                            <div className="event-timeline-head">
                              <span className={`badge ${meta.badge} event-type-badge`}>
                                {meta.label}
                              </span>
                              {ev.created_at ? (
                                <time className="meta event-time" dateTime={ev.created_at}>
                                  {formatShortTime(ev.created_at)}
                                </time>
                              ) : null}
                            </div>
                            <p className="event-timeline-title">{ev.title || ""}</p>
                          </li>
                        );
                      })}
                    </ol>
                  ) : (
                    <p className="hint">Событий пока нет</p>
                  )}
                </section>

                <div className="inbox-v2-side-footer">
                  <button
                    type="button"
                    className="btn primary sm inbox-v2-full-profile-btn"
                    onClick={onOpenFullProfile}
                  >
                    Полный профиль
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </div>

        <div id="side-snippets-panel" className="snippets-panel" aria-label="Быстрые шаблоны рядом с чатом">
          <div className="snippets-panel-head">
            <strong className="snippets-panel-title">Быстрые шаблоны (Snippets)</strong>
            <span className="hint">
              Клик подставит <code>{"{name}"}</code>, <code>{"{company}"}</code>
            </span>
          </div>
          <div id="side-snippets-list" className="snippets-chips-row">
            {snippets.map((s) => (
              <div
                key={s.id}
                className="snippet-chip"
                data-snippet-id={String(s.id)}
                data-snippet-shortcut={s.shortcut}
              >
                <button
                  type="button"
                  className="snippet-chip-btn"
                  data-snippet-id={String(s.id)}
                  data-snippet-shortcut={s.shortcut}
                  title={s.body}
                  onClick={() => {
                    const rendered = renderSnippetTemplate(
                      s.body,
                      selectedDialog,
                      effectiveLead,
                    );
                    onApplySnippetText(rendered, s.shortcut);
                  }}
                >
                  <code>{s.shortcut}</code>
                  <span>{s.title}</span>
                </button>
              </div>
            ))}
          </div>
          <form
            id="snippet-create-form"
            className="snippet-create-form"
            aria-label="Добавить быстрый шаблон"
            onSubmit={async (ev) => {
              ev.preventDefault();
              if (!scShortcut.trim() || !scTitle.trim() || !scBody.trim() || savingSnippet) return;
              setSavingSnippet(true);
              try {
                await onCreateSnippet({
                  shortcut: scShortcut.trim(),
                  title: scTitle.trim(),
                  body: scBody.trim(),
                });
                setScShortcut("");
                setScTitle("");
                setScBody("");
              } finally {
                setSavingSnippet(false);
              }
            }}
          >
            <div className="snippet-form-row">
              <input
                id="snippet-shortcut"
                name="shortcut"
                type="text"
                autoComplete="off"
                placeholder="/команда (напр. /demo)"
                aria-label="Команда шаблона"
                value={scShortcut}
                onChange={(ev) => setScShortcut(ev.target.value)}
                required
              />
              <input
                id="snippet-title"
                name="title"
                type="text"
                autoComplete="off"
                placeholder="Название (Демо)"
                aria-label="Название шаблона"
                value={scTitle}
                onChange={(ev) => setScTitle(ev.target.value)}
                required
              />
            </div>
            <textarea
              id="snippet-body"
              name="body"
              rows={2}
              placeholder="Текст шаблона с {name} и {company}..."
              aria-label="Текст шаблона"
              value={scBody}
              onChange={(ev) => setScBody(ev.target.value)}
              required
            />
            <div className="row">
              <button
                type="submit"
                id="snippet-submit-btn"
                className="btn secondary sm"
                disabled={savingSnippet}
              >
                <Plus size={12} aria-hidden="true" /> Добавить шаблон
              </button>
            </div>
          </form>
        </div>
      </section>
    </aside>
  );
}
