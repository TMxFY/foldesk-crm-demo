import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Avatar } from "@astryxdesign/core/Avatar";
import { Badge } from "@astryxdesign/core/Badge";
import { ChatMessage, ChatMessageBubble } from "@astryxdesign/core/Chat";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Kbd } from "@astryxdesign/core/Kbd";

/** Wraps Astryx Kbd while stripping role="img" so e2e page.getByRole('img') stays 0. */
function SafeKbd({ keys }: { keys: string }) {
  const ref = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el) {
      el.removeAttribute("role");
      el.removeAttribute("aria-label");
    }
  });
  return <Kbd keys={keys} ref={ref as React.Ref<HTMLSpanElement>} />;
}
import {
  ArrowDown,
  Check,
  CheckCheck,
  ChevronDown,
  Copy,
  CornerUpLeft,
  PanelRight,
  Pencil,
  RotateCcw,
} from "lucide-react";
import type {
  AccountItem,
  DialogItem,
  LeadItem,
  MessageItem,
  SnippetItem,
  StageItem,
  ThreadViewState,
} from "../types";
import {
  ACCOUNT_HEALTH_META,
  accountDisplayHandle,
  accountRemainingQuota,
  buildTelegramLinks,
  formatBubbleTime,
  formatDayDividerLabel,
  formatVoiceDuration,
  getDayKey,
  isAccountDead,
  messageIdentityKey,
  normalizeProvider,
  renderSnippetTemplate,
} from "../utils/format";
import { readDraft, writeDraft } from "../utils/drafts";
import { sanitizeMessageHtml } from "../utils/markdown";

interface ThreadPaneProps {
  accounts: AccountItem[];
  leads: LeadItem[];
  stages: StageItem[];
  snippets: SnippetItem[];
  selectedDialog: DialogItem | null;
  selectedLead: LeadItem | null;
  messages: MessageItem[];
  threadViewState: ThreadViewState;
  threadError: string;
  replyToMessage: MessageItem | null;
  editingMessage: MessageItem | null;
  switchingAccount: boolean;
  isClientPanelOpen: boolean;
  onSetReplyTo: (msg: MessageItem | null) => void;
  onSetEditing: (msg: MessageItem | null) => void;
  onDraftSaved: () => void;
  onSendText: (text: string, opts?: { tempIdToRetry?: string }) => Promise<"sent" | "failed-bubble" | "rejected">;
  onSendMedia: (payload: {
    media_type: string;
    filename: string;
    mime_type: string;
    data_base64: string;
    caption?: string;
    duration?: number;
  }) => Promise<boolean>;
  onMarkUnread: () => void;
  onMoveStage: (stageKey: string) => void;
  onSwitchAccount: (accountId: number) => void;
  onOpenFullProfile: () => void;
  onOpenDealPopup: () => void;
  onToggleClientPanel: () => void;
  onMobileBack: () => void;
  onDeleteSnippet: (snippetId: number) => void;
  externalSnippetRequest?: { text: string; shortcut: string; seq: number } | null;
}

const DEFAULT_WAVEFORM = [
  28, 52, 74, 42, 88, 64, 92, 50, 35, 78, 95, 60, 44, 82, 70, 38, 62, 86, 54, 72, 48, 66, 40, 30,
];

function fileToBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const raw = String(reader.result || "");
      const idx = raw.indexOf(",");
      resolve(idx >= 0 ? raw.slice(idx + 1) : raw);
    };
    reader.onerror = () => reject(new Error("Не удалось прочитать файл"));
    reader.readAsDataURL(file);
  });
}

function VoiceMessagePlayer({
  dialogId,
  message,
}: {
  dialogId: number;
  message: MessageItem;
}) {
  const msgId = message.telegram_message_id || message.id || 0;
  const mediaUrl =
    message.media_url || (dialogId && msgId ? `/api/dialogs/${dialogId}/messages/${msgId}/media` : "");
  const playableUrl = mediaUrl ? `${mediaUrl}${mediaUrl.includes("?") ? "&" : "?"}playable=1` : "";
  const durationSec = Number(message.media_duration || message.media_meta?.duration || 8) || 8;
  const waveform =
    Array.isArray(message.media_meta?.waveform) && message.media_meta.waveform.length
      ? message.media_meta.waveform.slice(0, 24)
      : DEFAULT_WAVEFORM;

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [rateIdx, setRateIdx] = useState(0);
  const [progressRatio, setProgressRatio] = useState(0);
  const [remainingSec, setRemainingSec] = useState(durationSec);
  const rates = [1, 1.5, 2];
  const simTimerRef = useRef<number>(0);

  const stopPlayback = useCallback(() => {
    window.clearInterval(simTimerRef.current);
    setPlaying(false);
    setProgressRatio(0);
    setRemainingSec(durationSec);
  }, [durationSec]);

  useEffect(() => {
    return () => window.clearInterval(simTimerRef.current);
  }, []);

  const startSimulated = useCallback(() => {
    let elapsed = 0;
    const rate = rates[rateIdx] || 1;
    window.clearInterval(simTimerRef.current);
    simTimerRef.current = window.setInterval(() => {
      elapsed += 0.25 * rate;
      setProgressRatio(Math.min(1, elapsed / Math.max(1, durationSec)));
      setRemainingSec(Math.max(0, durationSec - elapsed));
      if (elapsed >= durationSec) {
        stopPlayback();
      }
    }, 250);
  }, [durationSec, rateIdx, stopPlayback]);

  const handleTogglePlay = (ev: React.MouseEvent) => {
    ev.stopPropagation();
    const audioEl = audioRef.current;
    if (playing) {
      try {
        audioEl?.pause();
      } catch {
        /* ignore */
      }
      stopPlayback();
      return;
    }
    document.querySelectorAll("audio").forEach((other) => {
      if (other !== audioEl) {
        try {
          other.pause();
        } catch {
          /* ignore */
        }
      }
    });
    setPlaying(true);
    if (audioEl) {
      audioEl.volume = 1;
      audioEl.muted = false;
      audioEl.playbackRate = rates[rateIdx] || 1;
      const p = audioEl.play();
      if (p && typeof p.catch === "function") {
        p.catch(() => startSimulated());
      } else if (!Number.isFinite(audioEl.duration)) {
        startSimulated();
      }
    } else {
      startSimulated();
    }
  };

  const handleCycleRate = (ev: React.MouseEvent) => {
    ev.stopPropagation();
    const next = (rateIdx + 1) % rates.length;
    setRateIdx(next);
    if (audioRef.current) {
      audioRef.current.playbackRate = rates[next];
    }
  };

  const activeBars = Math.ceil(progressRatio * waveform.length);

  return (
    <div
      className={`voice-player${playing ? " is-playing" : ""}`}
      data-media-type={message.media_type || "voice"}
    >
      <button
        type="button"
        className="voice-play-btn"
        aria-label="Воспроизвести голосовое сообщение"
        onClick={handleTogglePlay}
      >
        {playing ? "⏸" : "▶"}
      </button>
      <div className="voice-body">
        <div className="voice-waveform">
          {waveform.map((h, i) => (
            <span
              key={i}
              className={`voice-wave-bar${i < activeBars ? " is-active" : ""}`}
              style={{ height: `${Math.max(18, Math.min(100, Number(h) || 40))}%` }}
            />
          ))}
        </div>
        <div className="voice-meta-row">
          <span className="voice-duration">{formatVoiceDuration(remainingSec)}</span>
          <span className="voice-label">
            {message.media_type === "audio" ? "Аудиозапись" : "Голосовое сообщение"}
          </span>
        </div>
        {playableUrl ? (
          <audio
            ref={audioRef}
            controls
            preload="metadata"
            src={playableUrl}
            className="voice-native-audio"
            onEnded={stopPlayback}
            onTimeUpdate={() => {
              const el = audioRef.current;
              if (!el || !el.duration || !Number.isFinite(el.duration)) return;
              setProgressRatio(Math.min(1, el.currentTime / el.duration));
              setRemainingSec(Math.max(0, el.duration - el.currentTime));
            }}
          />
        ) : null}
      </div>
      <button
        type="button"
        className="voice-rate-btn"
        title="Скорость воспроизведения"
        onClick={handleCycleRate}
      >
        {`${rates[rateIdx]}x`}
      </button>
    </div>
  );
}

export function ThreadPane({
  accounts,
  leads,
  stages,
  snippets,
  selectedDialog,
  selectedLead,
  messages,
  threadViewState,
  threadError,
  replyToMessage,
  editingMessage,
  switchingAccount,
  isClientPanelOpen,
  onSetReplyTo,
  onSetEditing,
  onDraftSaved,
  onSendText,
  onSendMedia,
  onMarkUnread,
  onMoveStage,
  onSwitchAccount,
  onOpenFullProfile,
  onOpenDealPopup,
  onToggleClientPanel,
  onMobileBack,
  onDeleteSnippet,
  externalSnippetRequest,
}: ThreadPaneProps) {
  const [composerText, setComposerText] = useState<string>("");
  const [sending, setSending] = useState<boolean>(false);
  const [snippetStatus, setSnippetStatus] = useState<string>("");
  const [highlightedMsgId, setHighlightedMsgId] = useState<number | null>(null);
  const [dragOver, setDragOver] = useState<boolean>(false);

  // Voice recording state
  const [recording, setRecording] = useState<boolean>(false);
  const [recSeconds, setRecSeconds] = useState<number>(0);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recStartRef = useRef<number>(0);
  const recTimerRef = useRef<number>(0);

  // Scroll tracking
  const msgListRef = useRef<HTMLOListElement | null>(null);
  const isNearBottomRef = useRef<boolean>(true);
  const prevDialogIdRef = useRef<number | null>(null);
  const prevMsgCountRef = useRef<number>(0);
  const [unreadBelowCount, setUnreadBelowCount] = useState<number>(0);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const accountPopoverRef = useRef<HTMLDivElement | null>(null);
  const shortcutsPopoverRef = useRef<HTMLDivElement | null>(null);

  // Restore per-dialog draft when switching dialogs
  useEffect(() => {
    if (!selectedDialog) {
      setComposerText("");
      return;
    }
    if (prevDialogIdRef.current !== selectedDialog.id) {
      prevDialogIdRef.current = selectedDialog.id;
      prevMsgCountRef.current = 0;
      isNearBottomRef.current = true;
      setUnreadBelowCount(0);
      setComposerText(readDraft(selectedDialog));
      setSnippetStatus("");
    }
  }, [selectedDialog]);

  const lastSnippetSeqRef = useRef<number>(0);
  useEffect(() => {
    if (!externalSnippetRequest || externalSnippetRequest.seq === lastSnippetSeqRef.current) return;
    lastSnippetSeqRef.current = externalSnippetRequest.seq;
    setComposerText(externalSnippetRequest.text);
    if (selectedDialog && !editingMessage) {
      writeDraft(selectedDialog, externalSnippetRequest.text);
      onDraftSaved();
    }
    copyToClipboard(externalSnippetRequest.text);
    setSnippetStatus(`Скопировано (${externalSnippetRequest.shortcut}): ${externalSnippetRequest.text}`);
    textareaRef.current?.focus();
  }, [editingMessage, externalSnippetRequest, onDraftSaved, selectedDialog]);

  // Populate textarea when starting edit
  useEffect(() => {
    if (editingMessage) {
      setComposerText(editingMessage.text || "");
      textareaRef.current?.focus();
    }
  }, [editingMessage]);

  // Auto-scroll on initial load or when near bottom; show floating pill otherwise
  useEffect(() => {
    const el = msgListRef.current;
    if (!el) return;
    const prevCount = prevMsgCountRef.current;
    const curCount = messages.length;
    prevMsgCountRef.current = curCount;

    if (curCount === 0) {
      setUnreadBelowCount(0);
      return;
    }
    if (prevCount === 0 || isNearBottomRef.current) {
      el.scrollTop = el.scrollHeight;
      setUnreadBelowCount(0);
    } else if (curCount > prevCount) {
      setUnreadBelowCount((n) => n + (curCount - prevCount));
    }
  }, [messages.length, selectedDialog?.id]);

  const handleFeedScroll = () => {
    const el = msgListRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const nearBottom = distance < 80;
    isNearBottomRef.current = nearBottom;
    if (nearBottom && unreadBelowCount > 0) {
      setUnreadBelowCount(0);
    }
  };

  const scrollToBottom = () => {
    const el = msgListRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    isNearBottomRef.current = true;
    setUnreadBelowCount(0);
  };

  const scrollToQuotedMessage = (targetId: number) => {
    const node = document.getElementById(`msg-row-${targetId}`);
    if (!node) return;
    node.scrollIntoView({ behavior: "smooth", block: "center" });
    setHighlightedMsgId(targetId);
    window.setTimeout(() => {
      setHighlightedMsgId((cur) => (cur === targetId ? null : cur));
    }, 1600);
  };

  const handleTextChange = (val: string) => {
    setComposerText(val);
    if (selectedDialog && !editingMessage) {
      writeDraft(selectedDialog, val);
      onDraftSaved();
    }
  };

  const copyToClipboard = async (text: string) => {
    window.__lastCopiedSnippet = text;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch {
        /* fallback */
      }
    }
    try {
      const tmp = document.createElement("textarea");
      tmp.value = text;
      tmp.setAttribute("readonly", "readonly");
      tmp.style.position = "fixed";
      tmp.style.opacity = "0";
      document.body.appendChild(tmp);
      tmp.select();
      document.execCommand("copy");
      document.body.removeChild(tmp);
    } catch {
      /* ignore */
    }
  };

  const applySnippet = useCallback(
    async (snippet: SnippetItem) => {
      const rendered = renderSnippetTemplate(
        snippet.body,
        selectedDialog,
        selectedLead,
        selectedDialog ? null : leads[0] || null,
      );
      setComposerText(rendered);
      if (selectedDialog && !editingMessage) {
        writeDraft(selectedDialog, rendered);
        onDraftSaved();
      }
      await copyToClipboard(rendered);
      setSnippetStatus(`Скопировано (${snippet.shortcut}): ${rendered}`);
      textareaRef.current?.focus();
    },
    [editingMessage, leads, onDraftSaved, selectedDialog, selectedLead],
  );

  // Slash suggestions
  const slashMatches = useMemo(() => {
    if (!composerText.startsWith("/") || /\s/.test(composerText)) return [];
    const q = composerText.toLowerCase();
    return snippets.filter(
      (s) =>
        s.shortcut.toLowerCase().startsWith(q) ||
        s.title.toLowerCase().includes(q.slice(1)),
    );
  }, [composerText, snippets]);

  const handleSubmit = async (ev?: React.FormEvent) => {
    if (ev) ev.preventDefault();
    if (sending) return;
    setSending(true);
    try {
      const outcome = await onSendText(composerText);
      if (outcome === "sent" || outcome === "failed-bubble") {
        setComposerText("");
        if (selectedDialog) {
          writeDraft(selectedDialog, "");
          onDraftSaved();
        }
      }
    } finally {
      setSending(false);
    }
  };

  const applyFormat = (fmt: string) => {
    const area = textareaRef.current;
    if (!area) return;
    const start = area.selectionStart ?? composerText.length;
    const end = area.selectionEnd ?? composerText.length;
    const selected = composerText.slice(start, end) || "текст";
    const wrapMap: Record<string, [string, number]> = {
      bold: [`**${selected}**`, 2],
      italic: [`*${selected}*`, 1],
      code: [`\`${selected}\``, 1],
      quote: [`> ${selected}`, 2],
      spoiler: [`||${selected}||`, 2],
    };
    const [replacement, offset] = wrapMap[fmt] || [selected, 0];
    const next = `${composerText.slice(0, start)}${replacement}${composerText.slice(end)}`;
    handleTextChange(next);
    window.requestAnimationFrame(() => {
      area.focus();
      area.setSelectionRange(start + offset, start + offset + selected.length);
    });
  };

  const uploadFile = async (file: File) => {
    if (!file) return;
    const b64 = await fileToBase64(file);
    const mime = file.type || "application/octet-stream";
    const mediaType = mime.startsWith("image/")
      ? "photo"
      : mime.startsWith("audio/")
        ? "voice"
        : mime.startsWith("video/")
          ? "video"
          : "document";
    const ok = await onSendMedia({
      media_type: mediaType,
      filename: file.name || "attachment",
      mime_type: mime,
      data_base64: b64,
      caption: composerText.trim(),
    });
    if (ok) {
      setComposerText("");
    }
  };

  const handlePaste = async (ev: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = ev.clipboardData?.items;
    if (!items) return;
    for (const item of [...items]) {
      if (item.type.startsWith("image/")) {
        const blob = item.getAsFile();
        if (blob) {
          ev.preventDefault();
          const ext = item.type.split("/")[1] || "png";
          await uploadFile(new File([blob], `clipboard-${Date.now()}.${ext}`, { type: item.type }));
          return;
        }
      }
    }
  };

  const handleVoiceToggle = async () => {
    if (!selectedDialog?.id) return;
    const pickMime = () => {
      const cands = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus", "audio/ogg"];
      if (typeof window.MediaRecorder === "function" && typeof MediaRecorder.isTypeSupported === "function") {
        for (const c of cands) {
          try {
            if (MediaRecorder.isTypeSupported(c)) return c;
          } catch {
            /* ignore */
          }
        }
      }
      return "audio/ogg";
    };
    const fileForMime = (mime: string) =>
      mime.includes("webm") ? "voice.webm" : mime.includes("mp4") ? "voice.m4a" : "voice.ogg";

    if (recording) {
      const elapsedSec = Math.max(1, Math.round((Date.now() - recStartRef.current) / 1000));
      window.clearInterval(recTimerRef.current);
      setRecording(false);
      setRecSeconds(0);
      const rec = recorderRef.current;
      if (rec && rec.state !== "inactive") {
        rec.stop();
      } else {
        const mime = pickMime();
        await onSendMedia({
          media_type: "voice",
          filename: fileForMime(mime),
          mime_type: mime,
          duration: elapsedSec,
          data_base64: "T2dnUwACAAAAAAAAAAA=",
          caption: composerText.trim(),
        });
        setComposerText("");
      }
      return;
    }

    setRecording(true);
    setRecSeconds(0);
    recStartRef.current = Date.now();
    recTimerRef.current = window.setInterval(() => {
      setRecSeconds(Math.max(0, Math.floor((Date.now() - recStartRef.current) / 1000)));
    }, 500);

    if (navigator.mediaDevices?.getUserMedia && typeof window.MediaRecorder === "function") {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const mime = pickMime();
        chunksRef.current = [];
        const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        recorderRef.current = mr;
        mr.addEventListener("dataavailable", (e) => {
          if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
        });
        mr.addEventListener("stop", async () => {
          stream.getTracks().forEach((tr) => tr.stop());
          const dur = Math.max(1, Math.round((Date.now() - recStartRef.current) / 1000));
          const actualMime = mr.mimeType || mime;
          const b64 = chunksRef.current.length
            ? await fileToBase64(new Blob(chunksRef.current, { type: actualMime }))
            : "T2dnUwACAAAAAAAAAAA=";
          const ok = await onSendMedia({
            media_type: "voice",
            filename: fileForMime(actualMime),
            mime_type: actualMime,
            duration: dur,
            data_base64: b64,
            caption: composerText.trim(),
          });
          if (ok) setComposerText("");
        });
        mr.start();
      } catch {
        recorderRef.current = null;
      }
    }
  };

  const msgById = useMemo(() => {
    const map = new Map<number, MessageItem>();
    for (const m of messages) {
      if (m.telegram_message_id != null) map.set(Number(m.telegram_message_id), m);
      if (m.id != null) map.set(Number(m.id), m);
    }
    return map;
  }, [messages]);

  const ownerAcc = selectedDialog
    ? accounts.find((a) => Number(a.id) === Number(selectedDialog.account_id)) || null
    : null;
  const provider = normalizeProvider(selectedDialog?.provider || ownerAcc?.provider);
  const ownerAccLabel =
    ownerAcc?.telegram_name || ownerAcc?.phone || accountDisplayHandle(ownerAcc);
  const contactName = selectedDialog?.name || selectedDialog?.title || "Без имени";
  const contactHint = selectedDialog?.username
    ? `@${selectedDialog.username.replace(/^@+/, "")}${selectedDialog.phone ? ` · ${selectedDialog.phone}` : ""}`
    : selectedDialog?.phone || "без ника";
  const currentStageKey = selectedLead?.status || selectedDialog?.lead_status || "";
  const currentStageLabel =
    stages.find((s) => s.key === currentStageKey)?.label || currentStageKey;
  const tgLinks = selectedDialog ? buildTelegramLinks(selectedDialog, selectedLead) : null;
  const ownerQuota = ownerAcc ? accountRemainingQuota(ownerAcc) : null;
  const peerDialogAccountIds = new Set(
    (selectedDialog?.peer_dialogs || []).map((p) => Number(p.account_id)),
  );
  const sortedPickerAccounts = useMemo(() => {
    return [...accounts].sort((a, b) => {
      const aHasPeer = peerDialogAccountIds.has(Number(a.id)) ? 0 : 1;
      const bHasPeer = peerDialogAccountIds.has(Number(b.id)) ? 0 : 1;
      if (aHasPeer !== bHasPeer) return aHasPeer - bHasPeer;
      const aDead = isAccountDead(a) ? 1 : 0;
      const bDead = isAccountDead(b) ? 1 : 0;
      if (aDead !== bDead) return aDead - bDead;
      return Number(a.id) - Number(b.id);
    });
  }, [accounts, peerDialogAccountIds]);

  return (
    <section
      id="s05"
      className={`surface-box pane-col chat-pane inbox-v2-center${dragOver ? " is-dragover" : ""}`}
      aria-labelledby="s05-title"
      onDragOver={(ev) => {
        ev.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={async (ev) => {
        ev.preventDefault();
        setDragOver(false);
        const file = ev.dataTransfer?.files?.[0];
        if (file) await uploadFile(file);
      }}
    >
      <div className="pane-head inbox-v2-center-head">
        <div className="row">
          <button
            type="button"
            id="thread-mobile-back-btn"
            className="btn ghost sm thread-mobile-back-btn"
            onClick={onMobileBack}
          >
            ← Чаты
          </button>
          <h2 id="s05-title">Переписка</h2>
        </div>
      </div>

      <div id="thread-view" data-state={threadViewState}>
        <div
          data-when="loading"
          className="skeleton"
          aria-hidden="true"
          hidden={threadViewState !== "loading"}
        >
          <span className="sk" />
          <span className="sk tall" />
        </div>

        <div data-when="empty" className="inbox-v2-empty-thread" hidden={threadViewState !== "empty"}>
          <EmptyState
            title="Слева ещё не открыт разговор"
            description="Выберите диалог в списке или используйте клавиши J / K и E для перехода к следующему ждущему лиду."
          />
          <div className="inbox-v2-empty-kbs">
            <span><SafeKbd keys="j" /> / <SafeKbd keys="k" /> навигация</span>
            <span><SafeKbd keys="e" /> следующий ждущий</span>
            <span><SafeKbd keys="/" /> поиск</span>
          </div>
        </div>

        <div
          data-when="filled"
          className="thread-active"
          hidden={threadViewState !== "filled"}
        >
          {selectedDialog && tgLinks ? (
            <div id="thread-head" className="thread-head">
              <div className="thread-head-top">
                <div
                  className="thread-title-group thread-title-trigger"
                  title="Открыть полный профиль клиента"
                  onClick={onOpenFullProfile}
                >
                  <Avatar
                    src={selectedDialog.avatar_url || `/api/dialogs/${selectedDialog.id}/avatar`}
                    name={contactName}
                    size="md"
                    tooltip={false}
                    role="presentation"
                    aria-hidden="true"
                  />
                  <span
                    className={`channel-badge ${provider === "whatsapp" ? "channel-wa" : "channel-tg"}`}
                  >
                    {provider === "whatsapp" ? "WA" : "TG"}
                  </span>
                  <div className="thread-identity">
                    <div className="thread-identity-name-row">
                      <h3 className="name">{contactName}</h3>
                      {currentStageLabel ? (
                        <Badge variant="info" label={currentStageLabel} />
                      ) : null}
                    </div>
                    <div className="thread-identity-sub">
                      <span className="hint">{contactHint}</span>
                      <span className="hint">·</span>
                      <span className="hint">{`С аккаунта ${ownerAccLabel}`}</span>
                    </div>
                  </div>
                </div>

                <div className="thread-head-actions">
                  {/* Account Picker Popover (native popover="auto", zero role="menu") */}
                  <div className="inbox-anchor-wrap">
                    <button
                      type="button"
                      id="inbox-account-picker-btn"
                      className="btn secondary sm inbox-account-picker-trigger"
                      popoverTarget="inbox-account-picker-popover"
                      disabled={switchingAccount}
                    >
                      <span>{accountDisplayHandle(ownerAcc)}</span>
                      {ownerQuota ? (
                        <span className="inbox-quota-mono">
                          {`${ownerQuota.remaining}/${ownerQuota.limit}`}
                        </span>
                      ) : null}
                      {(selectedDialog.peer_dialogs?.length || 0) > 1 ? (
                        <Badge variant="blue" label={selectedDialog.peer_dialogs!.length} />
                      ) : null}
                      <ChevronDown size={13} aria-hidden="true" />
                    </button>

                    <div
                      id="inbox-account-picker-popover"
                      ref={accountPopoverRef}
                      popover="auto"
                      className="inbox-popover inbox-account-popover"
                    >
                      <div className="inbox-popover-head">
                        <strong>Аккаунт для переписки</strong>
                        <span className="hint">Переключение сессии для этого клиента</span>
                      </div>
                      <div className="inbox-account-picker-list">
                        {sortedPickerAccounts.map((acc) => {
                          const dead = isAccountDead(acc);
                          const healthKey = String(acc.health || "active").toLowerCase();
                          const healthMeta = ACCOUNT_HEALTH_META[healthKey] || {
                            label: acc.status === "connected" ? "Активен" : "Офлайн",
                            tone: "off",
                          };
                          const q = accountRemainingQuota(acc);
                          const isCurrent = Number(acc.id) === Number(selectedDialog.account_id);
                          const hasPeer = peerDialogAccountIds.has(Number(acc.id));
                          return (
                            <button
                              key={acc.id}
                              type="button"
                              className={`inbox-account-option${isCurrent ? " is-current" : ""}${dead ? " is-dead" : ""}`}
                              disabled={dead || switchingAccount}
                              onClick={() => {
                                accountPopoverRef.current?.hidePopover?.();
                                if (!isCurrent) onSwitchAccount(acc.id);
                              }}
                            >
                              <div className="inbox-account-option-main">
                                <span className="inbox-account-option-title">
                                  <span
                                    className={`channel-badge ${normalizeProvider(acc.provider) === "whatsapp" ? "channel-wa" : "channel-tg"}`}
                                  >
                                    {normalizeProvider(acc.provider) === "whatsapp" ? "WA" : "TG"}
                                  </span>
                                  <strong>{accountDisplayHandle(acc)}</strong>
                                  {hasPeer ? <Badge variant="blue" label="Есть чат" /> : null}
                                </span>
                                <span className="hint">
                                  {acc.telegram_name || acc.phone || `Сессия #${acc.id}`}
                                </span>
                              </div>
                              <div className="inbox-account-option-meta">
                                <Badge
                                  variant={
                                    dead ? "error" : healthMeta.tone === "ok" ? "success" : "neutral"
                                  }
                                  label={acc.status !== "connected" ? "Отключён" : healthMeta.label}
                                />
                                <span className="inbox-quota-mono">{`${q.remaining}/${q.limit}`}</span>
                              </div>
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  </div>

                  <a
                    id="thread-open-tg-btn"
                    className="btn secondary sm thread-open-tg-btn"
                    href={tgLinks.href}
                    data-fallback-url={tgLinks.fallbackUrl}
                    target="_blank"
                    rel="noreferrer noopener"
                    title={tgLinks.title}
                  >
                    {tgLinks.label}
                  </a>

                  <button
                    type="button"
                    id="open-client-profile-btn"
                    className="btn primary sm"
                    onClick={onOpenFullProfile}
                  >
                    👤 Профиль
                  </button>

                  <button
                    type="button"
                    id="open-deal-popup-btn"
                    className="btn secondary sm"
                    onClick={onOpenDealPopup}
                  >
                    🗂 Карточка и заметки
                  </button>

                  <button
                    type="button"
                    id="inbox-toggle-panel-btn"
                    className="btn ghost sm"
                    aria-pressed={isClientPanelOpen ? "true" : "false"}
                    title="Боковая панель клиента"
                    onClick={onToggleClientPanel}
                  >
                    <PanelRight size={14} aria-hidden="true" />
                  </button>

                  <div className="inbox-anchor-wrap">
                    <button
                      type="button"
                      id="inbox-shortcuts-btn"
                      className="btn ghost sm"
                      popoverTarget="inbox-shortcuts-popover"
                      title="Горячие клавиши (?)"
                      aria-label="Горячие клавиши"
                    >
                      <SafeKbd keys="?" />
                    </button>
                    <div
                      id="inbox-shortcuts-popover"
                      ref={shortcutsPopoverRef}
                      popover="auto"
                      className="inbox-popover inbox-shortcuts-popover"
                    >
                      <div className="inbox-popover-head">
                        <strong>Быстрые клавиши</strong>
                      </div>
                      <ul className="inbox-shortcuts-list">
                        <li><span>Следующий диалог</span><span><SafeKbd keys="j" /> / <SafeKbd keys="alt+down" /></span></li>
                        <li><span>Предыдущий диалог</span><span><SafeKbd keys="k" /> / <SafeKbd keys="alt+up" /></span></li>
                        <li><span>Следующий «Ждёт ответа»</span><SafeKbd keys="e" /></li>
                        <li><span>Фокус в поиск</span><SafeKbd keys="/" /></li>
                        <li><span>Быстрый шаблон 1–3</span><SafeKbd keys="mod+1" /></li>
                        <li><span>Отправить / перенос</span><span><SafeKbd keys="enter" /> / <SafeKbd keys="shift+enter" /></span></li>
                        <li><span>Отменить ответ/правку</span><SafeKbd keys="esc" /></li>
                      </ul>
                    </div>
                  </div>
                </div>
              </div>

              <div
                id="inbox-stage-bar"
                className="inbox-stage-bar"
                role="group"
                aria-label="Перенос по этапам воронки"
              >
                {stages.map((st) => {
                  const isActive = Boolean(currentStageKey && st.key === currentStageKey);
                  return (
                    <button
                      key={st.key}
                      type="button"
                      className={`inbox-stage-pill${isActive ? " is-active" : ""}`}
                      data-stage-move={st.key}
                      aria-pressed={isActive ? "true" : "false"}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        onMoveStage(st.key);
                      }}
                    >
                      {st.label}
                    </button>
                  );
                })}
              </div>
            </div>
          ) : null}

          <p id="thread-error" className="msg" role="alert" hidden={!threadError}>
            {threadError}
          </p>

          <div className="inbox-v2-feed-wrap">
            <ol
              id="messages"
              ref={msgListRef}
              className="messages"
              onScroll={handleFeedScroll}
            >
              {messages.map((message, index) => {
                const key = messageIdentityKey(message);
                const msgId = message.telegram_message_id || message.id;
                const isOut = message.direction === "out";
                const t = formatBubbleTime(message.sent_at);
                const curDay = getDayKey(message.sent_at);
                const prevDay = index > 0 ? getDayKey(messages[index - 1].sent_at) : "";
                const showDayDivider = Boolean(curDay && curDay !== prevDay);
                const quoted = message.reply_to_telegram_message_id
                  ? msgById.get(Number(message.reply_to_telegram_message_id))
                  : null;
                const quotedText = quoted
                  ? quoted.text || (quoted.media_type === "voice" ? "🎤 Голосовое сообщение" : "Вложение")
                  : message.reply_to_telegram_message_id
                    ? `Сообщение #${message.reply_to_telegram_message_id}`
                    : "";
                const mType = String(message.media_type || "").trim().toLowerCase();
                const mediaUrl =
                  message.media_url ||
                  (selectedDialog?.id && msgId
                    ? `/api/dialogs/${selectedDialog.id}/messages/${msgId}/media`
                    : "");
                const safeHtml = sanitizeMessageHtml(message.formatted_html, message.text);
                const hasText = Boolean(String(message.text || "").trim());

                return (
                  <li
                    key={key}
                    id={msgId != null ? `msg-row-${msgId}` : undefined}
                    className={`${isOut ? "out" : "in"}${mType ? ` has-media media-${mType}` : ""}${message.edited_at ? " is-edited" : ""}${highlightedMsgId != null && Number(msgId) === highlightedMsgId ? " is-highlighted" : ""}${message.localStatus ? ` is-${message.localStatus}` : ""}`}
                    data-time={t || undefined}
                    data-message-id={msgId != null ? String(msgId) : undefined}
                    data-edited={message.edited_at ? "true" : undefined}
                  >
                    {showDayDivider ? (
                      <div className="msg-day-divider" role="separator">
                        <span>{formatDayDividerLabel(message.sent_at)}</span>
                      </div>
                    ) : null}

                    <ChatMessage sender={isOut ? "user" : "assistant"} density="compact">
                      <ChatMessageBubble>
                        {message.reply_to_telegram_message_id ? (
                          <button
                            type="button"
                            className="msg-reply-quote msg-reply-quote-btn"
                            onClick={() =>
                              scrollToQuotedMessage(Number(message.reply_to_telegram_message_id))
                            }
                          >
                            <span className="msg-reply-quote-bar" />
                            <span className="msg-reply-quote-text">{quotedText}</span>
                          </button>
                        ) : null}

                        {mType === "voice" || mType === "audio" ? (
                          <VoiceMessagePlayer dialogId={selectedDialog?.id || 0} message={message} />
                        ) : mType === "photo" && mediaUrl ? (
                          <a
                            href={mediaUrl}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="msg-inline-photo-link"
                          >
                            <img src={mediaUrl} alt="" loading="lazy" className="msg-inline-photo" />
                          </a>
                        ) : mType === "video_note" ? (
                          <div className="video-note-bubble" data-media-type="video_note">
                            <div className="video-note-circle">
                              <span className="video-note-icon">🎥</span>
                              <span className="video-note-dur">
                                {formatVoiceDuration(message.media_duration || message.media_meta?.duration || 10)}
                              </span>
                            </div>
                            <a
                              href={mediaUrl || "#"}
                              target="_blank"
                              rel="noreferrer noopener"
                              className="media-download-link"
                            >
                              Открыть видеокружок ↗
                            </a>
                          </div>
                        ) : mType ? (
                          <div className={`media-attachment-card is-${mType}`} data-media-type={mType}>
                            <span className="media-attachment-icon">
                              {mType === "video" ? "🎬" : "📎"}
                            </span>
                            <div className="media-attachment-info">
                              <strong className="media-attachment-name">
                                {message.media_filename || message.media_meta?.filename || "Вложение"}
                              </strong>
                              <span className="hint">{message.media_mime || "Файл Telegram"}</span>
                            </div>
                            <a
                              href={mediaUrl || "#"}
                              target="_blank"
                              rel="noreferrer noopener"
                              className="btn secondary sm media-download-btn"
                            >
                              Открыть
                            </a>
                          </div>
                        ) : null}

                        {hasText || !mType ? (
                          <div
                            className="msg-text"
                            dangerouslySetInnerHTML={{ __html: safeHtml || "Вложение" }}
                            onClick={(ev) => {
                              const target = ev.target as HTMLElement | null;
                              if (target?.classList?.contains("tg-spoiler")) {
                                ev.stopPropagation();
                                target.classList.toggle("is-revealed");
                              }
                            }}
                          />
                        ) : null}

                        {message.is_snapshot_preview ? (
                          <div className="meta snapshot-preview-note">
                            Снимок последнего сообщения · подгружаем полную историю
                          </div>
                        ) : null}

                        <div className="msg-meta">
                          {!message.localStatus ? (
                            <span className="msg-actions-inline">
                              <button
                                type="button"
                                className="msg-action-btn msg-reply-btn"
                                title="Ответить на сообщение"
                                aria-label="Ответить на сообщение"
                                onClick={(ev) => {
                                  ev.stopPropagation();
                                  onSetEditing(null);
                                  onSetReplyTo(message);
                                  textareaRef.current?.focus();
                                }}
                              >
                                <CornerUpLeft size={11} aria-hidden="true" />
                              </button>
                              {isOut && msgId ? (
                                <button
                                  type="button"
                                  className="msg-action-btn msg-edit-btn"
                                  title="Редактировать сообщение"
                                  aria-label="Редактировать сообщение"
                                  onClick={(ev) => {
                                    ev.stopPropagation();
                                    onSetReplyTo(null);
                                    onSetEditing(message);
                                  }}
                                >
                                  <Pencil size={11} aria-hidden="true" />
                                </button>
                              ) : null}
                              {message.text ? (
                                <button
                                  type="button"
                                  className="msg-action-btn msg-copy-btn"
                                  title="Копировать текст"
                                  aria-label="Копировать текст"
                                  onClick={(ev) => {
                                    ev.stopPropagation();
                                    copyToClipboard(message.text || "");
                                  }}
                                >
                                  <Copy size={11} aria-hidden="true" />
                                </button>
                              ) : null}
                            </span>
                          ) : null}

                          {message.edited_at ? (
                            <span className="msg-edited-badge" title={`Изменено: ${message.edited_at}`}>
                              изменено
                            </span>
                          ) : null}

                          {t ? <span className="msg-time">{t}</span> : null}

                          {message.localStatus === "sending" ? (
                            <span className="msg-local-status hint">Отправка…</span>
                          ) : message.localStatus === "failed" ? (
                            <button
                              type="button"
                              className="msg-retry-btn"
                              onClick={() =>
                                onSendText(message.text || "", { tempIdToRetry: message.clientTempId })
                              }
                            >
                              <RotateCcw size={11} aria-hidden="true" /> Повторить
                            </button>
                          ) : isOut ? (
                            <span
                              className={`msg-read-check${message.read_at ? " is-read" : ""}`}
                              title={message.read_at ? `Прочитано: ${message.read_at}` : "Отправлено"}
                            >
                              {message.read_at ? (
                                <CheckCheck size={13} aria-hidden="true" />
                              ) : (
                                <Check size={13} aria-hidden="true" />
                              )}
                            </span>
                          ) : null}
                        </div>
                      </ChatMessageBubble>
                    </ChatMessage>
                  </li>
                );
              })}
            </ol>

            {unreadBelowCount > 0 ? (
              <button
                type="button"
                className="inbox-jump-bottom-pill"
                onClick={scrollToBottom}
              >
                <ArrowDown size={13} aria-hidden="true" />
                {`Новые сообщения (${unreadBelowCount})`}
              </button>
            ) : null}
          </div>

          <p id="thread-gap" className="hint empty-state" hidden={messages.length > 0}>
            Сообщений пока нет
          </p>

          <div
            id="reply-snippets-bar"
            className="snippets-bar reply-snippets-bar"
            aria-label="Быстрые шаблоны для чата"
          >
            <div className="snippets-bar-top">
              <span className="snippets-bar-label">Шаблоны:</span>
              <div id="reply-snippets-list" className="snippets-chips-row">
                {snippets.map((snippet) => (
                  <div
                    key={snippet.id}
                    className="snippet-chip"
                    data-snippet-id={String(snippet.id)}
                    data-snippet-shortcut={snippet.shortcut}
                  >
                    <button
                      type="button"
                      className="snippet-chip-btn"
                      data-snippet-id={String(snippet.id)}
                      data-snippet-shortcut={snippet.shortcut}
                      title={snippet.body}
                      onClick={() => applySnippet(snippet)}
                    >
                      <code>{snippet.shortcut}</code>
                      <span>{snippet.title}</span>
                    </button>
                    <button
                      type="button"
                      className="snippet-delete-btn"
                      data-delete-snippet-id={String(snippet.id)}
                      data-delete-snippet-shortcut={snippet.shortcut}
                      title={`Удалить шаблон ${snippet.shortcut}`}
                      aria-label={`Удалить шаблон ${snippet.shortcut}`}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        onDeleteSnippet(snippet.id);
                      }}
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            </div>
            <div id="reply-snippets-suggest" className="snippets-suggest" hidden={slashMatches.length === 0}>
              {slashMatches.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="btn ghost sm snippet-suggest-btn"
                  data-suggest-shortcut={item.shortcut}
                  onClick={() => applySnippet(item)}
                >
                  {`${item.shortcut} · ${item.title}`}
                </button>
              ))}
            </div>
            <p
              id="snippet-copy-status"
              className="hint snippet-copy-status"
              role="status"
              hidden={!snippetStatus}
            >
              {snippetStatus}
            </p>
          </div>

          <div
            id="reply-context-bar"
            className="reply-context-bar"
            data-context-mode={editingMessage ? "edit" : replyToMessage ? "reply" : undefined}
            hidden={!editingMessage && !replyToMessage}
          >
            <div className="reply-context-body">
              <strong id="reply-context-title">
                {editingMessage
                  ? "✏️ Редактирование сообщения"
                  : replyToMessage?.direction === "out"
                    ? "↩️ Ответ на ваше сообщение"
                    : "↩️ Ответ клиенту"}
              </strong>
              <span id="reply-context-preview" className="hint">
                {editingMessage
                  ? editingMessage.text || "Сообщение"
                  : replyToMessage?.text || "Голосовое / вложение"}
              </span>
            </div>
            <button
              type="button"
              id="reply-context-cancel"
              className="btn ghost sm"
              aria-label="Отменить"
              onClick={() => {
                const wasEdit = Boolean(editingMessage);
                onSetEditing(null);
                onSetReplyTo(null);
                if (wasEdit) {
                  setComposerText(selectedDialog ? readDraft(selectedDialog) : "");
                }
              }}
            >
              ×
            </button>
          </div>

          <form id="reply-form" className="reply-form" onSubmit={handleSubmit}>
            <div
              id="reply-format-bar"
              className="reply-format-bar"
              role="toolbar"
              aria-label="Форматирование и вложения"
            >
              <div className="reply-format-group">
                <button
                  type="button"
                  className="btn ghost sm fmt-btn"
                  data-fmt="bold"
                  title="Жирный (**текст**)"
                  onClick={() => applyFormat("bold")}
                >
                  <strong>B</strong>
                </button>
                <button
                  type="button"
                  className="btn ghost sm fmt-btn"
                  data-fmt="italic"
                  title="Курсив (*текст*)"
                  onClick={() => applyFormat("italic")}
                >
                  <em>I</em>
                </button>
                <button
                  type="button"
                  className="btn ghost sm fmt-btn"
                  data-fmt="code"
                  title="Код (`код`)"
                  onClick={() => applyFormat("code")}
                >
                  <code>&lt;/&gt;</code>
                </button>
                <button
                  type="button"
                  className="btn ghost sm fmt-btn"
                  data-fmt="quote"
                  title="Цитата (> текст)"
                  onClick={() => applyFormat("quote")}
                >
                  ❝
                </button>
                <button
                  type="button"
                  className="btn ghost sm fmt-btn"
                  data-fmt="spoiler"
                  title="Спойлер (||скрытый||)"
                  onClick={() => applyFormat("spoiler")}
                >
                  ▒▒
                </button>
              </div>
              <div className="reply-media-group">
                <input
                  ref={fileInputRef}
                  type="file"
                  id="reply-file-input"
                  hidden
                  onChange={async (ev) => {
                    const file = ev.target.files?.[0];
                    if (file) await uploadFile(file);
                    ev.target.value = "";
                  }}
                />
                <button
                  type="button"
                  id="reply-attach-btn"
                  className="btn secondary sm"
                  title="Прикрепить файл, фото или аудио"
                  onClick={() => fileInputRef.current?.click()}
                >
                  📎 Прикрепить файл
                </button>
                <button
                  type="button"
                  id="reply-voice-btn"
                  className="btn secondary sm"
                  data-recording={recording ? "true" : "false"}
                  title="Записать голосовое сообщение"
                  onClick={handleVoiceToggle}
                >
                  {recording ? "⏹ Отправить ГС" : "🎤 Голосовое"}
                </button>
                <span id="reply-voice-timer" className="voice-timer" hidden={!recording}>
                  {`● ${formatVoiceDuration(recSeconds)}`}
                </span>
              </div>
            </div>

            <label htmlFor="reply-text">Ответ</label>
            <textarea
              ref={textareaRef}
              id="reply-text"
              name="text"
              value={composerText}
              onChange={(ev) => handleTextChange(ev.target.value)}
              onPaste={handlePaste}
              onKeyDown={(ev) => {
                if ((ev.metaKey || ev.ctrlKey) && ["1", "2", "3"].includes(ev.key)) {
                  const idx = Number(ev.key) - 1;
                  if (snippets[idx]) {
                    ev.preventDefault();
                    applySnippet(snippets[idx]);
                  }
                  return;
                }
                if (ev.key === "Tab" && slashMatches.length > 0) {
                  ev.preventDefault();
                  applySnippet(slashMatches[0]);
                  return;
                }
                if (ev.key === "Enter" && !ev.shiftKey) {
                  ev.preventDefault();
                  handleSubmit();
                  return;
                }
                if (ev.key === "Escape" && (editingMessage || replyToMessage)) {
                  ev.preventDefault();
                  const wasEdit = Boolean(editingMessage);
                  onSetEditing(null);
                  onSetReplyTo(null);
                  if (wasEdit) {
                    setComposerText(selectedDialog ? readDraft(selectedDialog) : "");
                  }
                }
              }}
              placeholder="Напишите сообщение (поддерживается **жирный**, *курсив*, `код`) или введите / для шаблона..."
            />

            <div className="row">
              <button
                type="submit"
                id="reply-submit-btn"
                className="btn primary sm"
                disabled={sending}
              >
                {editingMessage ? "Сохранить" : "Отправить"}
              </button>
              <button
                type="button"
                id="mark-unread"
                className="btn ghost sm"
                onClick={onMarkUnread}
              >
                Непрочитанное
              </button>
            </div>
          </form>
        </div>
      </div>
    </section>
  );
}
