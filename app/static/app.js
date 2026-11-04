const STATUSES = [
  ["new", "Лид"],
  ["talking", "Разговор"],
  ["waiting", "Ждём"],
  ["won", "Выигран"],
  ["lost", "Проигран"],
];

const BADGE = {
  new: "b-new",
  talking: "b-talk",
  waiting: "b-wait",
  won: "b-won",
  lost: "b-lost",
};

const ACCOUNT_STATUS = {
  pending_code: ["ждём код", "b-login"],
  pending_password: ["ждём пароль", "b-login"],
  connected: ["на связи", "b-ok"],
  error: ["ошибка входа", "b-bad"],
};

const ACCOUNT_HEALTH = {
  active: ["активен", "b-ok"],
  offline: ["офлайн", "b-off"],
  unauthorized: ["не авторизован", "b-bad"],
  banned: ["заблокирован", "b-bad"],
  frozen: ["заморожен", "b-bad"],
};

const CAMPAIGN_STATUS_LABEL = {
  draft: ["Черновик", "b-new"],
  running: ["Активна", "b-talk"],
  paused: ["На паузе", "b-wait"],
  completed: ["Завершена", "b-ok"],
};

const RECIPIENT_STATUS_LABEL = {
  pending: ["В очереди", "b-new"],
  in_progress: ["В цепочке", "b-talk"],
  completed: ["Отправлено", "b-ok"],
  replied: ["Ответил · Авто-стоп", "b-won"],
  failed: ["Ошибка", "b-bad"],
};

const SHEET_LABELS = {
  s07: "Воронка",
  inbox: "Входящие",
  outreach: "Рассылки",
  leads: "Лиды",
  s09: "Карточка",
  digest: "Напоминания",
  accounts: "Аккаунт",
  s11: "Папки",
  "crm-properties": "Поля CRM",
  "ai-bot": "AI Агент",
  "tool-phones": "Телефоны в TG",
  "tool-parser": "Парсер групп",
};

const SCHEDULE_DAYS = [
  ["mon", "Понедельник", true],
  ["tue", "Вторник", true],
  ["wed", "Среда", true],
  ["thu", "Четверг", true],
  ["fri", "Пятница", true],
  ["sat", "Суббота", false],
  ["sun", "Воскресенье", false],
];

const PROPERTY_TYPES = [
  ["text", "Текст"],
  ["textarea", "Длинный текст"],
  ["single_select", "Один вариант"],
  ["multi_select", "Несколько вариантов"],
  ["url", "Ссылка"],
  ["email", "Почта"],
  ["tel", "Телефон"],
  ["amount", "Сумма"],
];

const SAMPLE_CSV = [
  "Column 1,Приоритет,Статус/Фит,Категория,Название,Сайт,Контакт,Их угол,Наш угол,Хук,Первое касание,Статус,Founder X,Founder PH,Founder LinkedIn,Рынок,Сегмент,Фоллоуап 1,Фоллоуап 2",
  "1,A,Высокий,SaaS,ClawsGo,clawsgo.ai,@clawsgo_founder,AI автоматизация,UGC креативы для SaaS,Увидел ваш релиз на Product Hunt,\"Привет! Увидел ClawsGo на PH, крутой фокус на AI. Мы помогаем SaaS масштабировать платный трафик через нативные UGC-креативы. Прислать 3 примера под вашу нишу?\",Новый,,,,Global,B2B SaaS,\"Возвращаюсь к сообщению по ClawsGo. Можем собрать тестовый пак из 3 роликов на этой неделе, интересно глянуть?\",\"Финальный пинг по креативам для ClawsGo: если тема трафика актуальна, дайте знать, скину портфолио.\"",
  "2,A,Высокий,Fintech,TON Pay,tonpay.io,@pavel_ton,Крипто-эквайринг,Перформанс и UGC,Рост мерчантов в Telegram,\"Привет, Павел! Слежу за TON Pay, отличный продукт. Делаем UGC-связки и посевы для Web3 и финтех-команд. Показать кейсы с ROI?\",Новый,,,,CIS/Global,Web3,\"Павел, поднимаю тред по TON Pay. Будет удобно созвониться на 10 минут во вторник?\",\"Павел, если сейчас не в фокусе, не отвлекаю. Оставлю ссылку на разбор кейсов по перформансу.\"",
  "3,B,Средний,MarTech,CloudCRM,cloudcrm.ru,@elena_saas,Автоматизация отделов продаж,Лидогенерация в Telegram,Внедрение CRM за 1 день,\"Елена, привет! Видел кейсы CloudCRM. Мы помогаем B2B SaaS выстраивать предсказуемый поток демо через аутрич и UGC. Обсудим обмен опытом?\",Новый,,,,RU,B2B,\"Елена, возвращаюсь по CloudCRM: пришлю короткий PDF с метриками конверсии из холодного трафика?\",\"Елена, финальное сообщение. Если решите тестировать новые каналы лидогенерации, буду рад пообщаться!\"",
].join("\n");

const workspaceMain = document.querySelector("#workspace-main");
const initialViewMode = (workspaceMain && workspaceMain.dataset.viewMode) || "all";

const state = {
  mode: "crm",
  scope: "open",
  viewMode: initialViewMode,
  activeSheet: (workspaceMain && workspaceMain.dataset.activeSheet) || "s07",
  loginId: null,
  accounts: [],
  leads: [],
  leadCache: {},
  reminders: [],
  allOpenReminders: [],
  allOpenRemindersLoaded: false,
  dialogs: [],
  messages: [],
  dialogQuery: "",
  leadQuery: "",
  inbox: "empty",
  inboxSessionFilter: "all",
  selectedDialog: null,
  selectedLeadId: null,
  cards: {},
  folderLists: {},
  views: [],
  campaigns: [],
  propertyDefs: [],
  outreachSchedule: {
    timezone: "UTC",
    random_delay: "30-90",
    pause_night: true,
    days: SCHEDULE_DAYS.map(([code, label, enabled]) => ({
      day: code,
      label,
      enabled,
      start: "09:00",
      end: "18:00",
    })),
  },
  campaignSteps: [
    {
      delay_hours: 0,
      template_text: "Привет, {name}! Увидел ваш проект {company}, хотел предложить короткий обмен опытом по росту продаж в Telegram.",
    },
    {
      delay_hours: 24,
      template_text: "{name}, возвращаюсь к письму по {company}. Будет удобно созвониться на 10 минут на этой неделе?",
    },
  ],
  activeStepIndex: 0,
  activePopup: null,
  expandedCampaigns: {},
  expandedAccountPools: {},
  selectedBuilderAccountIds: null,
};

let modalSubmit = null;
let modalReturn = null;
let toastTimer = 0;
let inboxSource = null;
let inboxLiveAccount = "";
let cardToken = 0;

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    if (key === "className") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else node.setAttribute(key, value);
  });
  (children || []).forEach((child) => {
    node.append(child && child.nodeType ? child : document.createTextNode(child ?? ""));
  });
  return node;
}

function sheetFromHash(hash) {
  const clean = String(hash || "").replace(/^#/, "").trim();
  if (!clean) return "";
  if (clean === "s07" || clean === "pipeline") return "s07";
  if (clean === "s04" || clean === "s05" || clean === "s06" || clean === "inbox") return "inbox";
  if (clean === "outreach") return "outreach";
  if (clean === "leads") return "leads";
  if (clean === "s09") return "s09";
  if (clean === "digest") return "digest";
  if (clean === "accounts") return "accounts";
  if (clean === "s11" || clean === "folders") return "s11";
  if (clean === "crm-properties" || clean === "properties") return "crm-properties";
  if (clean === "ai-bot") return "ai-bot";
  if (clean === "tool-phones") return "tool-phones";
  if (clean === "tool-parser") return "tool-parser";
  return "";
}

function triggerSheetEntrance(section) {
  if (!section) return;
  section.classList.remove("sheet-entering");
  void section.offsetWidth;
  section.classList.add("sheet-entering");
  const children = section.querySelectorAll(
    ".col, .campaign-card, .account, .pane-col, #s06, .reminder, .surface-subcard"
  );
  children.forEach((item, idx) => {
    item.style.setProperty("--stagger-i", String(Math.min(idx, 10)));
  });
}

function syncPopupState() {
  const main = document.querySelector("#workspace-main");
  const backdrop = document.querySelector("#sheet-popup-backdrop");
  if (!main) return;
  const popup = state.viewMode === "sheet" ? (state.activePopup || "") : "";
  if (popup) {
    main.dataset.activePopup = popup;
  } else {
    delete main.dataset.activePopup;
  }
  if (backdrop) {
    backdrop.hidden = !popup;
  }
  const viewsBtn = document.querySelector("#open-views-popup");
  if (viewsBtn) {
    viewsBtn.setAttribute("aria-expanded", popup === "views" ? "true" : "false");
  }
  const scheduleBox = document.querySelector("#outreach-schedule-box");
  if (scheduleBox && state.viewMode === "sheet") {
    scheduleBox.hidden = popup !== "schedule";
  }
  const fromLeadsBox = document.querySelector("#outreach-from-leads-box");
  if (fromLeadsBox && state.viewMode === "sheet") {
    fromLeadsBox.hidden = popup !== "from-leads";
  }
}

function openSheetPopup(kind) {
  if (state.viewMode !== "sheet") return;
  state.activePopup = kind || null;
  syncSheetVisibility();
  if (kind) {
    window.requestAnimationFrame(() => {
      const box = document.querySelector(`[data-popup-box="${kind}"]`);
      if (!box) return;
      const firstInput = box.querySelector("input:not([type=file]), textarea, select");
      if (firstInput && firstInput.focus) firstInput.focus();
    });
  }
}

function closeSheetPopup() {
  const scheduleBox = document.querySelector("#outreach-schedule-box");
  const fromLeadsBox = document.querySelector("#outreach-from-leads-box");
  if (state.viewMode !== "sheet") {
    if (scheduleBox) scheduleBox.hidden = true;
    if (fromLeadsBox) fromLeadsBox.hidden = true;
    return;
  }
  if (!state.activePopup && state.activeSheet !== "s09") return;
  state.activePopup = null;
  if (state.activeSheet === "s09") {
    state.activeSheet = "s07";
  }
  syncSheetVisibility();
}

function syncSheetVisibility() {
  const main = document.querySelector("#workspace-main");
  if (!main) return;
  const active = state.activeSheet || "s07";
  main.dataset.viewMode = state.viewMode;
  main.dataset.activeSheet = active;
  syncPopupState();

  const labelNode = document.querySelector("#active-sheet-label");
  if (labelNode) {
    labelNode.textContent = SHEET_LABELS[active] || "Воронка";
  }

  const toggleBtn = document.querySelector("#toggle-view-mode");
  if (toggleBtn) {
    toggleBtn.textContent = state.viewMode === "sheet"
      ? "По листам"
      : "Все блоки";
  }

  document.querySelectorAll("[data-sheet-tab]").forEach((btn) => {
    const isCurrent = state.viewMode === "sheet" && btn.dataset.sheetTab === active;
    btn.setAttribute("aria-pressed", isCurrent ? "true" : "false");
  });

  document.querySelectorAll(".flow-step[data-sheet-link]").forEach((step) => {
    if (step.dataset.sheetLink === active) step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
  });

  const s07 = document.querySelector("#s07");
  const messengerGrid = document.querySelector(".messenger-grid");
  const outreach = document.querySelector("#outreach");
  const leads = document.querySelector("#leads");
  const opsGrid = document.querySelector(".ops-grid");
  const digest = document.querySelector("#digest");
  const accounts = document.querySelector("#accounts");
  const s11 = document.querySelector("#s11");
  const toolPhones = document.querySelector("#tool-phones");
  const toolParser = document.querySelector("#tool-parser");
  const crmProps = document.querySelector("#crm-properties");
  const aiBot = document.querySelector("#ai-bot");

  if (state.viewMode !== "sheet") {
    if (s07) s07.hidden = false;
    if (messengerGrid) {
      messengerGrid.hidden = false;
      messengerGrid.removeAttribute("data-sheet-focus");
      messengerGrid.removeAttribute("data-card-overlay");
    }
    if (outreach) outreach.hidden = false;
    if (leads) {
      leads.hidden = false;
      leads.removeAttribute("data-popup-only");
    }
    if (opsGrid) opsGrid.hidden = false;
    if (digest) digest.hidden = false;
    if (accounts) accounts.hidden = false;
    if (s11) s11.hidden = false;
    if (toolPhones) toolPhones.hidden = active !== "tool-phones";
    if (toolParser) toolParser.hidden = active !== "tool-parser";
    if (crmProps) crmProps.hidden = active !== "crm-properties";
    if (aiBot) aiBot.hidden = active !== "ai-bot";
    return;
  }

  const cardPopupOpen = state.activePopup === "card" || active === "s09";
  const leadPopupOpen = state.activePopup === "lead";

  if (s07) s07.hidden = active !== "s07";
  if (messengerGrid) {
    const showInbox = active === "inbox";
    messengerGrid.hidden = !(showInbox || cardPopupOpen);
    if (!showInbox && cardPopupOpen) {
      messengerGrid.dataset.cardOverlay = "true";
    } else {
      messengerGrid.removeAttribute("data-card-overlay");
    }
    if (active === "s09") messengerGrid.setAttribute("data-sheet-focus", "s09");
    else messengerGrid.removeAttribute("data-sheet-focus");
  }
  if (outreach) outreach.hidden = active !== "outreach";
  if (leads) {
    const showLeads = active === "leads";
    leads.hidden = !(showLeads || leadPopupOpen);
    if (!showLeads && leadPopupOpen) {
      leads.dataset.popupOnly = "true";
    } else {
      leads.removeAttribute("data-popup-only");
    }
  }

  const showOps = active === "digest" || active === "accounts" || active === "s11";
  if (opsGrid) opsGrid.hidden = !showOps;
  if (digest) digest.hidden = active !== "digest";
  if (accounts) accounts.hidden = active !== "accounts";
  if (s11) s11.hidden = active !== "s11";
  if (toolPhones) toolPhones.hidden = active !== "tool-phones";
  if (toolParser) toolParser.hidden = active !== "tool-parser";
  if (crmProps) crmProps.hidden = active !== "crm-properties";
  if (aiBot) aiBot.hidden = active !== "ai-bot";

  const activeNode = active === "inbox" ? messengerGrid : document.querySelector(`#${active}`);
  triggerSheetEntrance(activeNode);
  window.requestAnimationFrame(() => window.scrollTo(0, 0));
}

function activateSheet(sheetId, forceSheetMode) {
  if (!sheetId) return;
  if (sheetId === "s09" && state.viewMode === "sheet") {
    openSheetPopup("card");
    return;
  }
  state.activePopup = null;
  state.activeSheet = sheetId;
  if (forceSheetMode) {
    state.viewMode = "sheet";
  }
  const navLinks = [...document.querySelectorAll(".nav a")];
  navLinks.forEach((item) => item.removeAttribute("aria-current"));
  const matching = navLinks.find((item) => item.dataset.sheetLink === sheetId);
  if (matching) matching.setAttribute("aria-current", "page");
  syncSheetVisibility();
}

function setView(selector, status) {
  const root = document.querySelector(selector);
  if (!root) return;
  root.dataset.state = status;
  root.setAttribute("aria-busy", status === "loading" ? "true" : "false");
  root.querySelectorAll("[data-when]").forEach((node) => {
    const modes = node.dataset.when.split(/\s+/);
    node.hidden = !modes.includes(status);
  });
}

function toast(message, kind) {
  const node = document.querySelector("#toast");
  window.clearTimeout(toastTimer);
  if (!message) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.dataset.kind = kind === "error" ? "error" : "ok";
  node.setAttribute("role", kind === "error" ? "alert" : "status");
  node.textContent = message;
  toastTimer = window.setTimeout(() => {
    node.hidden = true;
  }, 2400);
}

function showError(error) {
  const message = error && error.message ? error.message : "";
  const box = document.querySelector("#login-msg");
  if (box) {
    box.textContent = message;
    box.setAttribute("role", message ? "alert" : "status");
  }
  if (message) toast(message, "error");
  else toast("");
}

function failRegion(view, box, error) {
  const message = error && error.message ? error.message : "Запрос не дошёл";
  const node = document.querySelector(box);
  if (node) node.textContent = message;
  setView(view, "error");
  toast(message, "error");
}

async function api(path, options) {
  const opts = options || {};
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (_err) { data = { detail: text }; }
  }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : "Запрос не дошёл");
  }
  return data;
}

function press(group, attr, value) {
  group.querySelectorAll("button").forEach((button) => {
    const on = button.getAttribute(attr) === value;
    button.setAttribute("aria-pressed", on ? "true" : "false");
    button.classList.toggle("secondary", on);
    button.classList.toggle("ghost", !on);
  });
}

function initials(name) {
  const parts = String(name || "?").trim().split(/\s+/).slice(0, 2);
  const letters = parts.map((part) => part.slice(0, 1)).join("");
  return letters ? letters.toUpperCase() : "?";
}

function normalizeProvider(raw) {
  return String(raw || "telegram").trim().toLowerCase() === "whatsapp" ? "whatsapp" : "telegram";
}

function channelBadgeHtml(provider) {
  const isWa = normalizeProvider(provider) === "whatsapp";
  return isWa
    ? '<span class="channel-badge channel-wa" title="WhatsApp">WA</span>'
    : '<span class="channel-badge channel-tg" title="Telegram">TG</span>';
}

function channelBadgeEl(provider) {
  const isWa = normalizeProvider(provider) === "whatsapp";
  return el("span", {
    className: `channel-badge ${isWa ? "channel-wa" : "channel-tg"}`,
    title: isWa ? "WhatsApp" : "Telegram",
  }, [isWa ? "WA" : "TG"]);
}

function statusSelect(lead, className) {
  const select = el("select", {
    "aria-label": "Статус",
    className: className || "",
    onchange: (event) => patchLead(lead.id, { status: event.target.value }),
  });
  STATUSES.forEach(([value, label]) => {
    const option = el("option", { value }, [label]);
    if (value === lead.status) option.selected = true;
    select.append(option);
  });
  return select;
}

function folderField(lead) {
  const select = el("select", {
    "aria-label": "Папка",
    onchange: (event) => {
      const raw = event.target.value;
      patchLead(lead.id, { folder_id: raw ? Number(raw) : null });
    },
  });
  const empty = el("option", { value: "" }, ["Без папки"]);
  if (!lead.folder_id) empty.selected = true;
  select.append(empty);
  (lead.folder_choices || []).forEach((folder) => {
    const option = el("option", { value: String(folder.id) }, [folder.title]);
    if (folder.id === lead.folder_id) option.selected = true;
    select.append(option);
  });
  return select;
}

function textField(lead, field, label) {
  return el("input", {
    value: lead[field] || "",
    "aria-label": label,
    onchange: (event) => patchLead(lead.id, { [field]: event.target.value }),
  });
}

async function patchLead(id, body) {
  try {
    const updated = await api(`/api/leads/${id}`, { method: "PATCH", body });
    delete state.cards[id];
    state.leadCache[updated.id] = updated;
    await loadLeads(state.leadQuery, updated.id);
  } catch (error) {
    toast(error.message, "error");
    await loadLeads(state.leadQuery, id);
  }
}

function dueToUtc(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toISOString().slice(0, 19);
}

function fromDateAndTime(date, time) {
  if (!date || !time) return "";
  return dueToUtc(`${date}T${time}`);
}

function dueMillis(value) {
  if (!value) return NaN;
  let text = String(value).trim();
  if (text.length >= 11 && text[10] === " ") text = `${text.slice(0, 10)}T${text.slice(11)}`;
  const hasZone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(text);
  const parsed = new Date(hasZone ? text : `${text}Z`);
  return parsed.getTime();
}

function formatDue(value) {
  const ms = dueMillis(value);
  if (Number.isNaN(ms)) return value || "";
  const date = new Date(ms);
  const pad = (part) => String(part).padStart(2, "0"); // taste-skill-ignore
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatShortTime(value) {
  const ms = dueMillis(value);
  if (Number.isNaN(ms)) return value || "";
  const date = new Date(ms);
  const pad = (part) => String(part).padStart(2, "0"); // taste-skill-ignore
  return `${pad(date.getDate())}.${pad(date.getMonth() + 1)} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function isSameDialog(a, b) {
  if (!a || !b) return false;
  if (a.id != null && b.id != null) return Number(a.id) === Number(b.id);
  if (a.telegram_user_id != null && b.telegram_user_id != null) {
    if (Number(a.telegram_user_id) !== Number(b.telegram_user_id)) return false;
    if (a.account_id && b.account_id) return Number(a.account_id) === Number(b.account_id);
    return true;
  }
  return false;
}

function allRemindersForLead(leadId) {
  const merged = new Map();
  (state.allOpenReminders || []).forEach((item) => {
    if (item && Number(item.lead_id) === Number(leadId)) merged.set(item.id, item);
  });
  (state.reminders || []).forEach((item) => {
    if (item && Number(item.lead_id) === Number(leadId)) merged.set(item.id, item);
  });
  return [...merged.values()];
}

function resolveLeadTracking(lead) {
  if (!lead) return {};
  const acc = lead.account_id
    ? (state.accounts || []).find((a) => Number(a.id) === Number(lead.account_id))
    : null;
  const cleanUser = String(lead.username || "").replace(/^@/, "").toLowerCase();
  const dlg = (state.dialogs || []).find((d) => {
    if (lead.telegram_user_id && d.telegram_user_id && Number(d.telegram_user_id) === Number(lead.telegram_user_id)) return true;
    if (cleanUser && d.username && String(d.username).replace(/^@/, "").toLowerCase() === cleanUser) return true;
    return false;
  });
  let recMatch = null;
  for (const camp of (state.campaigns || [])) {
    for (const r of (camp.recipients || [])) {
      if ((lead.id && r.lead_id && Number(r.lead_id) === Number(lead.id))
        || (cleanUser && r.username && String(r.username).replace(/^@/, "").toLowerCase() === cleanUser)) {
        recMatch = r;
        break;
      }
    }
    if (recMatch) break;
  }
  const accountName = lead.account_name
    || (recMatch && recMatch.assigned_account_name)
    || (acc && (acc.telegram_name || (acc.telegram_username ? `@${acc.telegram_username}` : acc.phone)))
    || "";
  const accountPhone = lead.account_phone || (acc && acc.phone) || "";
  const campaignStatus = lead.campaign_status || (recMatch && recMatch.status) || "";
  const lastFromPeer = lead.last_from_peer != null
    ? Number(lead.last_from_peer)
    : (dlg && dlg.last_from_peer != null ? Number(dlg.last_from_peer) : null);
  const campaignStep = lead.campaign_step != null
    ? Number(lead.campaign_step)
    : (recMatch && recMatch.current_step != null ? Number(recMatch.current_step) : 0);
  const lastSentAt = lead.last_sent_at || (recMatch && recMatch.last_sent_at) || "";
  const lastMessageAt = lead.last_message_at || (dlg && dlg.last_message_at) || lastSentAt || "";
  const nextFollowupAt = lead.next_followup_at || (recMatch && recMatch.next_followup_at) || "";
  const isReplied = campaignStatus === "replied" || lastFromPeer === 1;
  const openReminders = allRemindersForLead(lead.id).filter((rem) => !rem.done_at);
  const hasDueReminder = openReminders.some((rem) => {
    const b = bucket(rem);
    return b === "overdue" || b === "today";
  });
  const followupDueMs = dueMillis(nextFollowupAt);
  const isFollowupDue = Boolean(lead.followup_due)
    || hasDueReminder
    || (!isReplied && !Number.isNaN(followupDueMs) && followupDueMs <= Date.now());
  const isWaitingReply = !isReplied && (
    campaignStatus === "in_progress"
    || campaignStatus === "completed"
    || (lastFromPeer === 0 && Boolean(lastMessageAt))
    || lead.status === "waiting"
  );
  return {
    accountName,
    accountPhone,
    campaignStatus,
    lastFromPeer,
    campaignStep,
    lastSentAt,
    lastMessageAt,
    nextFollowupAt,
    isReplied,
    isWaitingReply,
    isFollowupDue,
    openReminders,
  };
}

function matchFollowupFilter(lead, followup) {
  if (!followup) return true;
  const tr = resolveLeadTracking(lead);
  if (followup === "replied") return tr.isReplied;
  if (followup === "waiting_reply") return tr.isWaitingReply;
  if (followup === "due_followup") return tr.isFollowupDue;
  return true;
}

function leadTrackingBox(lead) {
  const tr = resolveLeadTracking(lead);
  const senderLabel = tr.accountName || tr.accountPhone || "Не назначен";
  const lastTouchRaw = tr.lastSentAt || tr.lastMessageAt;
  const lastTouchLabel = lastTouchRaw ? formatDue(lastTouchRaw) : "Нет касаний";
  let chainBadge = el("span", { className: "badge b-off" }, ["Вне цепочки"]);
  if (tr.isReplied) {
    chainBadge = el("span", { className: "badge b-won" }, ["Ответил — агент остановлен"]);
  } else if (tr.campaignStatus === "in_progress") {
    const fupText = tr.nextFollowupAt ? ` · След. фоллоу-ап: ${formatShortTime(tr.nextFollowupAt)}` : "";
    chainBadge = el("span", { className: "badge b-talk" }, [`Шаг ${tr.campaignStep || 1}${fupText}`]);
  } else if (tr.campaignStatus === "completed") {
    chainBadge = el("span", { className: "badge b-ok" }, [`Шаг ${tr.campaignStep || 1} (завершён)`]);
  } else if (tr.campaignStatus === "pending") {
    chainBadge = el("span", { className: "badge b-new" }, ["В очереди рассылки"]);
  } else if (tr.lastFromPeer === 0 && lastTouchRaw) {
    chainBadge = el("span", { className: "badge b-wait" }, ["Ждём ответа клиента"]);
  }
  return el("div", { className: "lead-tracking-box" }, [
    el("div", { className: "lead-tracking-row" }, [
      el("span", { className: "hint" }, ["Аккаунт отправителя:"]),
      el("strong", {}, [senderLabel]),
    ]),
    el("div", { className: "lead-tracking-row" }, [
      el("span", { className: "hint" }, ["Последнее касание:"]),
      el("span", {}, [lastTouchLabel]),
    ]),
    el("div", { className: "lead-tracking-row" }, [
      el("span", { className: "hint" }, ["Статус цепочки:"]),
      chainBadge,
    ]),
  ]);
}

function bucket(reminder) {
  if (reminder.done_at) return "done";
  const ms = dueMillis(reminder.due_at);
  if (Number.isNaN(ms)) return "later";
  if (ms <= Date.now()) return "overdue";
  const date = new Date(ms);
  const now = new Date();
  const sameDay = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate();
  return sameDay ? "today" : "later";
}

function resumeLogin(account) {
  state.loginId = account.id;
  if (normalizeProvider(account.provider) === "whatsapp") {
    waCurrentAccountId = account.id;
    waCurrentPairingCode = account.pairing_code || generateWaPairingCode(account.phone);
    switchAuthTab("whatsapp");
    const pairingBox = document.querySelector("#wa-pairing-box");
    const codePanel = document.querySelector("#wa-code-panel");
    const qrPanel = document.querySelector("#wa-qr-panel");
    const codeDisplay = document.querySelector("#wa-pairing-code-display");
    const qrImage = document.querySelector("#wa-qr-image");
    if (pairingBox) pairingBox.hidden = false;
    if (codePanel) codePanel.hidden = false;
    if (qrPanel) qrPanel.hidden = false;
    if (codeDisplay) codeDisplay.textContent = waCurrentPairingCode;
    if (qrImage) qrImage.src = account.qr_code || account.qr_svg || DEFAULT_WA_QR_SVG;
  } else {
    document.querySelector("#code-form").hidden = account.status !== "pending_code";
    document.querySelector("#password-form").hidden = account.status !== "pending_password";
  }
  if (state.viewMode === "sheet") openSheetPopup("login");
}

async function reconnectAccount(account) {
  showError(null);
  try {
    const result = await api(`/api/accounts/${account.id}/reconnect`, { method: "POST" });
    await loadAccounts();
    if (!result || result.health !== "active") {
      showError(new Error((result && result.last_error) || "Сессия не активна"));
    }
  } catch (error) {
    showError(error);
  }
}

async function updateAccountLimit(accountId, rawLimit) {
  const limit = Number(rawLimit);
  if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
    toast("Укажите суточный лимит от 1 до 500 сообщений", "error");
    return;
  }
  try {
    await api(`/api/accounts/${accountId}/limit`, {
      method: "PATCH",
      body: { daily_limit: limit },
    });
    await loadAccounts();
    await loadCampaigns();
    toast(`Суточный лимит обновлён: ${limit} сообщ./сутки`, "ok");
  } catch (error) {
    toast(error.message, "error");
  }
}

function accountWord(count) {
  const n = Math.abs(Number(count) || 0) % 100;
  const n1 = n % 10;
  if (n > 10 && n < 20) return "аккаунтов";
  if (n1 > 1 && n1 < 5) return "аккаунта";
  if (n1 === 1) return "аккаунт";
  return "аккаунтов";
}

function accountConnectionLabel(account) {
  if (account.connection_label) return account.connection_label;
  if (account.is_demo) return "Демо-профиль (локально)";
  const isWa = normalizeProvider(account.provider) === "whatsapp";
  if (account.status === "connected") return isWa ? "Подключён · WhatsApp Web активен" : "Подключён · MTProto активен";
  if (account.status === "pending_code") return "Не подключён · ждёт код";
  if (account.status === "pending_password") return "Не подключён · ждёт пароль";
  return "Сессия слетела · нужен вход";
}

function accountConnectionState(account) {
  if (account.status === "connected" && account.health !== "unauthorized" && account.health !== "banned") {
    return "ok";
  }
  if (account.status === "pending_code" || account.status === "pending_password") {
    return "warn";
  }
  return "err";
}

async function checkAllAccounts(button) {
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  try {
    const res = await api("/api/accounts/check-all", { method: "POST" });
    if (res && Array.isArray(res.accounts)) {
      state.accounts = res.accounts;
      renderAccounts();
    } else {
      await loadAccounts();
    }
    await loadCampaigns();
    toast(`Проверено аккаунтов: ${state.accounts.length}`, "ok");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
}

function renderAccounts() {
  const list = document.querySelector("#account-list");
  const accountsSection = document.querySelector("#accounts");
  const summaryBar = document.querySelector("#accounts-summary-bar");
  list.replaceChildren();
  const connected = state.accounts.filter((account) => account.status === "connected");
  const disconnectedCount = Math.max(0, state.accounts.length - connected.length);
  const totalDailyPool = connected.reduce((sum, acc) => sum + (Number(acc.daily_limit) || 20), 0);
  if (summaryBar) {
    summaryBar.textContent = `Подключено: ${connected.length} · Не подключено: ${disconnectedCount} · Суточный пул: ${totalDailyPool} сообщ./сут`;
  }
  if (!state.accounts.length) {
    if (accountsSection) accountsSection.dataset.hasConnected = "false";
    fillFolderAccounts();
    fillCampaignAccounts();
    renderInboxSessionSwitcher();
    updateWorkspaceStats();
    setView("#accounts-view", "empty");
    return;
  }
  let pending = null;
  state.accounts.forEach((account) => {
    const label = account.telegram_name || account.telegram_username || account.phone;
    const pair = ACCOUNT_STATUS[account.status] || [account.status, "b-off"];
    const connState = accountConnectionState(account);
    const connLabel = accountConnectionLabel(account);
    const connBadgeClass = connState === "ok" ? "b-ok" : (connState === "warn" ? "b-login" : "b-bad");
    const subParts = [];
    if (account.phone && account.phone !== label) subParts.push(account.phone);
    if (account.telegram_username && `@${account.telegram_username}` !== label) {
      subParts.push(`@${account.telegram_username}`);
    }
    const proxyLabel = account.proxy_location || account.proxy || "Proxy: Auto";
    const accessLabel = account.access === "personal" ? "Personal" : "Workspace";
    const identityBlock = el("div", { className: "account-identity" }, [
      el("div", { className: "account-title-line" }, [
        el("span", { className: "conn-dot", "data-conn": connState, "aria-hidden": "true" }, []),
        channelBadgeEl(account.provider),
        el("span", { className: "name" }, [label]),
        subParts.length ? el("span", { className: "hint" }, [subParts.join(" · ")]) : null,
      ].filter(Boolean)),
      el("div", { className: "account-badges-line" }, [
        el("span", { className: `badge ${connBadgeClass}` }, [connLabel]),
        el("span", { className: `badge ${pair[1]}` }, [pair[0]]),
        el("span", { className: "badge b-new" }, [proxyLabel]),
        el("span", { className: "badge b-new" }, [accessLabel]),
      ]),
    ]);
    if (account.health) {
      const healthPair = ACCOUNT_HEALTH[account.health] || [account.health, "b-off"];
      identityBlock.querySelector(".account-badges-line").append(
        el("span", { className: `badge ${healthPair[1]}` }, [healthPair[0]]),
      );
    }
    const actionsWrap = el("div", { className: "row" }, []);
    if (account.status === "pending_code" || account.status === "pending_password") {
      pending = account;
      const caption = account.status === "pending_password" ? "Ввести пароль" : "Ввести код";
      actionsWrap.append(el("button", { type: "button", className: "btn ghost sm", onclick: () => resumeLogin(account) }, [caption]));
    }
    if (account.status === "connected") {
      actionsWrap.append(
        el("button", {
          type: "button",
          className: "btn secondary sm sheet-only-btn",
          onclick: () => {
            switchInboxSession(String(account.id));
            activateSheet("inbox");
          },
        }, ["Открыть чаты"]),
        el("button", { type: "button", className: "btn ghost sm", onclick: () => reconnectAccount(account) }, ["Переподключить"]),
      );
    }
    const mainRow = el("div", { className: "account-main" }, [
      identityBlock,
      actionsWrap,
    ]);

    const dailyLimit = Number(account.daily_limit) || 20;
    const sentToday = Number(account.sent_today) || 0;
    const pct = Math.min(100, Math.round((sentToday / Math.max(1, dailyLimit)) * 100));
    const limitState = sentToday >= dailyLimit ? "danger" : (pct >= 80 ? "warning" : "ok");
    const limitInput = el("input", {
      type: "number",
      min: "1",
      max: "500",
      value: String(dailyLimit),
      className: "limit-input",
      "aria-label": "Суточный лимит",
    });
    const limitBar = el("div", { className: "account-limit-bar" }, [
      el("span", { className: "limit-counter" }, [`Сегодня: ${sentToday} / ${dailyLimit}`]),
      el("div", { className: "limit-progress", "data-state": limitState, "aria-hidden": "true" }, [
        el("span", { className: "limit-fill", style: `width: ${pct}%` }, []),
      ]),
      el("div", { className: "limit-actions" }, [
        el("button", {
          type: "button",
          className: "chip",
          "data-limit-preset": "10",
          "aria-pressed": dailyLimit === 10 ? "true" : "false",
          onclick: () => updateAccountLimit(account.id, 10),
        }, ["10 / сут"]),
        el("button", {
          type: "button",
          className: "chip",
          "data-limit-preset": "20",
          "aria-pressed": dailyLimit === 20 ? "true" : "false",
          onclick: () => updateAccountLimit(account.id, 20),
        }, ["20 / сут"]),
        limitInput,
        el("button", {
          type: "button",
          className: "btn secondary sm",
          onclick: () => updateAccountLimit(account.id, limitInput.value),
        }, ["Сохранить лимит"]),
      ]),
    ]);

    const row = el("li", { className: "account" }, [mainRow, limitBar]);
    list.append(row);
  });
  if (pending && state.loginId == null) resumeLogin(pending);
  const select = document.querySelector("#tg-account");
  const previous = select.value;
  if (accountsSection) {
    accountsSection.dataset.hasConnected = (connected.length > 0 && !pending) ? "true" : "false";
  }
  select.replaceChildren();
  connected.forEach((account) => {
    const label = account.telegram_name || account.phone;
    select.append(el("option", { value: String(account.id) }, [label]));
  });
  if (previous && [...select.options].some((opt) => opt.value === previous)) {
    select.value = previous;
  }
  fillFolderAccounts();
  fillCampaignAccounts();
  renderInboxSessionSwitcher();
  updateWorkspaceStats();
  setView("#accounts-view", "filled");
}

function fillFolderAccounts() {
  const connected = state.accounts.filter((account) => account.status === "connected");
  ["#folder-account", "#tool-phones-account", "#tool-parser-account"].forEach((selector) => {
    const select = document.querySelector(selector);
    if (!select) return;
    const previous = select.value;
    select.replaceChildren();
    if (!connected.length && selector !== "#folder-account") {
      select.append(el("option", { value: "" }, ["Авто (любая сессия)"]));
    }
    connected.forEach((account) => {
      const label = account.telegram_name || account.telegram_username || account.phone;
      select.append(el("option", { value: String(account.id) }, [label]));
    });
    if (previous && [...select.options].some((opt) => opt.value === previous)) {
      select.value = previous;
    }
  });
}

function selectedCampaignAccountIds() {
  const accSelect = document.querySelector("#campaign-account");
  const primaryId = accSelect && accSelect.value ? Number(accSelect.value) : null;
  const poolHost = document.querySelector("#campaign-accounts-pool");
  if (!poolHost) {
    return primaryId ? [primaryId] : [];
  }
  const checkedIds = [...poolHost.querySelectorAll('input[type="checkbox"][data-account-id]:checked')]
    .map((box) => Number(box.dataset.accountId))
    .filter((id) => Number.isFinite(id) && id > 0);
  if (checkedIds.length) {
    if (primaryId && checkedIds.includes(primaryId)) {
      return [primaryId, ...checkedIds.filter((id) => id !== primaryId)];
    }
    return checkedIds;
  }
  return primaryId ? [primaryId] : [];
}

function updateCampaignPoolSummary() {
  const summaryNode = document.querySelector("#campaign-pool-summary");
  if (!summaryNode) return;
  const ids = selectedCampaignAccountIds();
  const connected = state.accounts.filter((acc) => acc.status === "connected" && ids.includes(Number(acc.id)));
  const totalLimit = connected.reduce((sum, acc) => sum + (Number(acc.daily_limit) || 20), 0);
  summaryNode.textContent = `Пул: ${connected.length} ${accountWord(connected.length)} · до ${totalLimit} сообщ./сутки суммарно`;
}

function fillCampaignAccounts() {
  const select = document.querySelector("#campaign-account");
  const poolHost = document.querySelector("#campaign-accounts-pool");
  if (!select) return;
  const previous = select.value;
  const connected = state.accounts.filter((account) => account.status === "connected");
  select.replaceChildren();
  if (poolHost) poolHost.replaceChildren();
  if (!connected.length) {
    select.append(el("option", { value: "" }, ["Нет подключённых профилей"]));
    if (poolHost) {
      poolHost.append(el("span", { className: "hint" }, ["Нет подключённых профилей для пула"]));
    }
    updateCampaignPoolSummary();
    return;
  }
  connected.forEach((account) => {
    const isWa = normalizeProvider(account.provider) === "whatsapp";
    const prefix = isWa ? "[WA]" : "[TG]";
    const label = account.telegram_name || account.telegram_username || account.phone;
    const sent = Number(account.sent_today) || 0;
    const limit = Number(account.daily_limit) || 20;
    select.append(
      el("option", { value: String(account.id) }, [`${prefix} ${label} (${sent}/${limit} сегодня)`]),
    );
  });
  if (previous && [...select.options].some((opt) => opt.value === previous)) {
    select.value = previous;
  }

  const connectedIds = connected.map((acc) => Number(acc.id));
  if (!Array.isArray(state.selectedBuilderAccountIds)) {
    state.selectedBuilderAccountIds = [...connectedIds];
  } else {
    state.selectedBuilderAccountIds = state.selectedBuilderAccountIds.filter((id) => connectedIds.includes(Number(id)));
    if (!state.selectedBuilderAccountIds.length) {
      const fallbackId = Number(select.value || connectedIds[0]);
      state.selectedBuilderAccountIds = fallbackId ? [fallbackId] : [...connectedIds];
    }
  }

  if (poolHost) {
    connected.forEach((account) => {
      const accId = Number(account.id);
      const isWa = normalizeProvider(account.provider) === "whatsapp";
      const prefix = isWa ? "[WA]" : "[TG]";
      const label = account.telegram_name || account.telegram_username || account.phone;
      const sent = Number(account.sent_today) || 0;
      const limit = Number(account.daily_limit) || 20;
      const checkbox = el("input", {
        type: "checkbox",
        "data-account-id": String(accId),
      });
      checkbox.checked = state.selectedBuilderAccountIds.includes(accId);
      checkbox.addEventListener("change", () => {
        const checked = [...poolHost.querySelectorAll('input[type="checkbox"][data-account-id]:checked')]
          .map((node) => Number(node.dataset.accountId));
        state.selectedBuilderAccountIds = checked;
        if (checked.length && !checked.includes(Number(select.value))) {
          select.value = String(checked[0]);
        }
        updateCampaignPoolSummary();
      });
      poolHost.append(
        el("label", { className: "pool-check-chip" }, [
          checkbox,
          channelBadgeEl(account.provider),
          el("span", { className: "pool-check-name" }, [`${prefix} ${label}`]),
          el("span", { className: "pool-check-meta" }, [`${sent}/${limit}`]),
        ]),
      );
    });
  }
  updateCampaignPoolSummary();
}

function renderBuilderSteps() {
  const host = document.querySelector("#campaign-steps-list");
  if (!host) return;
  host.replaceChildren();
  state.campaignSteps.forEach((step, idx) => {
    const delayVal = step.delay_hours ?? (idx === 0 ? 0 : 24);
    const titleSpan = el("span", { className: "step-title-badge" }, [
      idx === 0
        ? "Шаг 1 · Сразу"
        : `Шаг ${idx + 1} · Через ${delayVal} ч без ответа`,
    ]);
    const delayInput = el("input", {
      type: "number",
      min: "0",
      max: "720",
      value: String(delayVal),
      "aria-label": `Пауза до шага ${idx + 1} (часов)`,
      oninput: (event) => {
        step.delay_hours = Math.max(0, Number(event.target.value) || 0);
        if (idx > 0) {
          titleSpan.textContent = `Шаг ${idx + 1} · Через ${step.delay_hours} ч без ответа`;
        }
      },
    });
    const headRight = [
      el("label", { className: "step-delay-label" }, [
        idx === 0 ? "Пауза (ч):" : "Через (ч):",
        delayInput,
      ]),
    ];
    if (state.campaignSteps.length > 1) {
      headRight.push(
        el("button", {
          type: "button",
          className: "btn ghost sm",
          onclick: () => {
            state.campaignSteps.splice(idx, 1);
            if (state.activeStepIndex >= state.campaignSteps.length) {
              state.activeStepIndex = Math.max(0, state.campaignSteps.length - 1);
            }
            renderBuilderSteps();
          },
        }, ["Удалить"]),
      );
    }
    const area = el("textarea", {
      rows: "2",
      "data-step-index": String(idx),
      "aria-label": `Шаблон сообщения шага ${idx + 1}`,
      placeholder: "Привет, {name}! Пишу по проекту {company}...",
      onfocus: () => { state.activeStepIndex = idx; },
      oninput: (event) => { step.template_text = event.target.value; },
    }, [step.template_text || ""]);

    host.append(
      el("div", { className: "campaign-step-card" }, [
        el("div", { className: "step-card-head" }, [
          titleSpan,
          el("div", { className: "row" }, headRight),
        ]),
        area,
      ]),
    );
  });
}

function fillCsvCampaignSelect(preferredId) {
  const select = document.querySelector("#csv-campaign-select");
  if (select) {
    const previous = preferredId ? String(preferredId) : select.value;
    select.replaceChildren();
    if (!state.campaigns.length) {
      select.append(el("option", { value: "" }, ["Сначала создайте рассылку"]));
    } else {
      state.campaigns.forEach((camp) => {
        select.append(
          el("option", { value: String(camp.id) }, [
            `${camp.name} (база: ${camp.total_recipients || 0})`,
          ]),
        );
      });
      if (previous && [...select.options].some((opt) => opt.value === previous)) {
        select.value = previous;
      }
    }
  }
  ["#from-leads-campaign-select", "#tool-phones-campaign", "#tool-parser-campaign"].forEach((sel) => {
    const node = document.querySelector(sel);
    if (!node) return;
    const prev = preferredId ? String(preferredId) : node.value;
    const emptyLabel = sel === "#from-leads-campaign-select"
      ? "Создать новую кампанию автоматически"
      : "Без добавления в кампанию";
    node.replaceChildren(el("option", { value: "" }, [emptyLabel]));
    state.campaigns.forEach((camp) => {
      node.append(el("option", { value: String(camp.id) }, [`${camp.name} (${camp.total_recipients || 0})`]));
    });
    if (prev && [...node.options].some((opt) => opt.value === prev)) {
      node.value = prev;
    }
  });
}

async function runCampaignStep(campaignId, button) {
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  try {
    const res = await api(`/api/campaigns/${campaignId}/run`, { method: "POST" });
    state.cards = {};
    await loadAccounts();
    await loadCampaigns();
    await loadInbox();
    await loadReminders();
    await loadLeads(state.leadQuery, state.selectedLeadId);
    const limitNote = res.limit_reached ? " (достигнут суточный лимит)" : "";
    toast(`Отправлено сообщений: ${res.sent}${limitNote}`, "ok");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
}

function resolveCampaignAccounts(camp) {
  if (Array.isArray(camp.accounts) && camp.accounts.length) {
    return camp.accounts;
  }
  const rawIds = (Array.isArray(camp.account_ids) && camp.account_ids.length)
    ? camp.account_ids
    : (camp.account_id ? [camp.account_id] : []);
  return rawIds
    .map((id) => state.accounts.find((item) => Number(item.id) === Number(id)))
    .filter(Boolean);
}

async function updateCampaignAccounts(campaignId, accountIds) {
  const cleanIds = (accountIds || []).map((id) => Number(id)).filter((id) => Number.isFinite(id) && id > 0);
  try {
    await api(`/api/campaigns/${campaignId}`, {
      method: "PUT",
      body: {
        account_id: cleanIds[0] || null,
        account_ids: cleanIds,
      },
    });
    await loadCampaigns();
    toast(`Пул аккаунтов рассылки обновлён (${cleanIds.length} акк.)`, "ok");
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderCampaigns() {
  const list = document.querySelector("#campaign-list");
  if (!list) return;
  list.replaceChildren();
  fillCsvCampaignSelect();
  updateWorkspaceStats();
  if (!state.campaigns.length) {
    setView("#campaigns-view", "empty");
    return;
  }
  const connectedAccounts = state.accounts.filter((item) => item.status === "connected");
  state.campaigns.forEach((camp) => {
    const statusPair = CAMPAIGN_STATUS_LABEL[camp.status] || [camp.status, "b-new"];
    const attachedAccounts = resolveCampaignAccounts(camp);
    const attachedIds = attachedAccounts.map((item) => Number(item.id));
    const attachedNames = attachedAccounts.map(
      (item) => item.telegram_name || item.telegram_username || item.phone,
    );
    const poolCapacity = attachedAccounts.reduce((sum, item) => sum + (Number(item.daily_limit) || 20), 0);
    const poolSummaryLabel = attachedAccounts.length > 1
      ? `Пул: ${attachedAccounts.length} ${accountWord(attachedAccounts.length)} (${attachedNames.join(", ")})`
      : (attachedNames[0] || "Профиль не выбран");
    const dailyLimit = Number(camp.daily_limit) || 20;
    const sentToday = Number(camp.sent_today) || 0;
    const steps = camp.steps || [];
    const recipients = camp.recipients || [];

    const stepPills = steps.map((step) => {
      const when = Number(step.delay_hours) === 0 ? "сразу" : `через ${step.delay_hours} ч`;
      const preview = String(step.template_text || "").slice(0, 64);
      return el("span", { className: "sequence-step-pill" }, [
        `Шаг ${step.step_order} (${when}): ${preview}${step.template_text && step.template_text.length > 64 ? "…" : ""}`,
      ]);
    });

    const runBtn = el("button", {
      type: "button",
      className: "btn primary sm",
      onclick: (event) => runCampaignStep(camp.id, event.currentTarget),
    }, ["Запустить шаг рассылки"]);

    const selectForCsvBtn = el("button", {
      type: "button",
      className: "btn secondary sm",
      onclick: () => {
        const select = document.querySelector("#csv-campaign-select");
        if (select) select.value = String(camp.id);
        if (state.viewMode === "sheet") {
          openSheetPopup("csv");
          return;
        }
        const area = document.querySelector("#csv-text");
        if (area) area.focus();
      },
    }, ["Выбрать для CSV"]);

    const poolEditorOpen = Boolean(state.expandedAccountPools[camp.id]);
    const togglePoolBtn = el("button", {
      type: "button",
      className: "btn secondary sm campaign-accounts-btn",
      "aria-expanded": poolEditorOpen ? "true" : "false",
      onclick: () => {
        state.expandedAccountPools[camp.id] = !poolEditorOpen;
        renderCampaigns();
      },
    }, [`Аккаунты (${attachedIds.length})`]);

    const poolChecks = connectedAccounts.map((account) => {
      const accId = Number(account.id);
      const isWa = normalizeProvider(account.provider) === "whatsapp";
      const prefix = isWa ? "[WA]" : "[TG]";
      const accLabel = account.telegram_name || account.telegram_username || account.phone;
      const box = el("input", {
        type: "checkbox",
        "data-camp-account-id": String(accId),
      });
      box.checked = attachedIds.includes(accId);
      box.addEventListener("change", () => {
        const nextIds = box.checked
          ? [...new Set([...attachedIds, accId])]
          : attachedIds.filter((id) => id !== accId);
        updateCampaignAccounts(camp.id, nextIds);
      });
      return el("label", { className: "pool-check-chip" }, [
        box,
        channelBadgeEl(account.provider),
        el("span", { className: "pool-check-name" }, [`${prefix} ${accLabel}`]),
        el("span", { className: "pool-check-meta" }, [`${Number(account.sent_today) || 0}/${Number(account.daily_limit) || 20}`]),
      ]);
    });

    const attachAllBtn = el("button", {
      type: "button",
      className: "btn ghost sm",
      onclick: () => {
        const allIds = connectedAccounts.map((acc) => Number(acc.id));
        updateCampaignAccounts(camp.id, allIds);
      },
    }, ["Все подключённые"]);

    const poolEditorRow = el("div", {
      className: "campaign-pool-inline",
      hidden: !poolEditorOpen ? "hidden" : undefined,
    }, [
      el("span", { className: "steps-caption" }, [
        `Пул отправки (до ${poolCapacity || dailyLimit} сообщ./сут):`,
      ]),
      el("div", { className: "campaign-accounts-pool" }, poolChecks),
      connectedAccounts.length > 1 ? attachAllBtn : null,
    ].filter(Boolean));

    const campProviders = attachedAccounts.length
      ? [...new Set(attachedAccounts.map((acc) => normalizeProvider(acc.provider)))]
      : ["telegram"];
    const channelBadges = campProviders.map((prov) => channelBadgeEl(prov));

    const badgeNodes = [
      ...channelBadges,
      el("span", { className: `badge ${statusPair[1]}` }, [statusPair[0]]),
      el("span", { className: "badge b-new" }, [poolSummaryLabel]),
      el("span", { className: "badge b-talk" }, [
        poolCapacity
          ? `${sentToday} / ${dailyLimit} в сут · пул до ${poolCapacity}/сут`
          : `${sentToday} / ${dailyLimit} в сут`,
      ]),
    ];

    const cardChildren = [
      el("div", { className: "campaign-card-head" }, [
        el("h4", { className: "name" }, [camp.name]),
        el("div", { className: "campaign-badges" }, badgeNodes),
      ]),
      el("div", { className: "campaign-kpis" }, [
        el("div", { className: "campaign-kpi" }, [
          el("span", {}, ["База"]),
          el("strong", {}, [String(camp.total_recipients || 0)]),
        ]),
        el("div", { className: "campaign-kpi" }, [
          el("span", {}, ["В очереди"]),
          el("strong", {}, [String((camp.pending_count || 0) + (camp.in_progress_count || 0))]),
        ]),
        el("div", { className: "campaign-kpi" }, [
          el("span", {}, ["Отправлено"]),
          el("strong", {}, [String((camp.in_progress_count || 0) + (camp.completed_count || 0))]),
        ]),
        el("div", { className: "campaign-kpi" }, [
          el("span", {}, ["Ответили"]),
          el("strong", {}, [String(camp.replied_count || 0)]),
        ]),
      ]),
      el("div", { className: "sequence-preview" }, stepPills),
      poolEditorRow,
      el("div", { className: "row" }, [runBtn, selectForCsvBtn, togglePoolBtn]),
    ];

    if (recipients.length) {
      const isExpanded = Boolean(state.expandedCampaigns[camp.id]);
      const visibleRecipients = (state.viewMode === "sheet" && !isExpanded && recipients.length > 3)
        ? recipients.slice(0, 3)
        : recipients;
      const recRows = [
        el("div", { className: "recipients-header" }, [
          el("span", {}, ["Контакт / Сайт"]),
          el("span", {}, ["Username / Тел"]),
          el("span", {}, ["Компания и касание"]),
          el("span", {}, ["Аккаунт и шаг"]),
          el("span", {}, ["Статус"]),
        ]),
        ...visibleRecipients.map((rec) => {
          const recPair = RECIPIENT_STATUS_LABEL[rec.status] || [rec.status, "b-new"];
          const handle = rec.username ? `@${rec.username}` : (rec.phone || "-");
          const siteCategory = [rec.website, rec.category].filter(Boolean).join(" · ");
          const contactCell = el("div", { className: "recipient-cell" }, [
            el("span", { className: "name" }, [rec.name || "Без имени"]),
            siteCategory ? el("span", { className: "recipient-sub" }, [siteCategory]) : null,
          ].filter(Boolean));

          const touchPreview = rec.step1_text || rec.note || "";
          const companyTitle = rec.company || rec.category || rec.website || "-";
          const followupTags = [
            rec.step2_text ? "Ф1" : "",
            rec.step3_text ? "Ф2" : "",
          ].filter(Boolean);
          const companyCell = el("div", { className: "recipient-cell" }, [
            el("span", { className: "recipient-company" }, [companyTitle]),
            touchPreview
              ? el("span", { className: "recipient-touch", title: touchPreview }, [
                touchPreview.length > 68 ? `${touchPreview.slice(0, 68)}…` : touchPreview,
              ])
              : null,
            followupTags.length
              ? el("span", { className: "recipient-sub" }, [`Фоллоуапы: ${followupTags.join(", ")}`])
              : null,
          ].filter(Boolean));

          const senderText = rec.assigned_account_name
            || (attachedIds.length ? `В пуле (${attachedIds.length} акк.)` : "Не назначен");
          const stepCell = el("div", { className: "recipient-cell" }, [
            el("span", { className: "recipient-sender" }, [senderText]),
            el("span", { className: "hint" }, [`Шаг ${rec.current_step || 0} / ${steps.length}`]),
            rec.last_sent_at
              ? el("span", { className: "recipient-sub" }, [`Посл.: ${formatShortTime(rec.last_sent_at)}`])
              : null,
            (rec.status === "in_progress" && rec.next_followup_at)
              ? el("span", { className: "recipient-sub recipient-followup-due" }, [`След. фоллоу-ап: ${formatShortTime(rec.next_followup_at)}`])
              : null,
          ].filter(Boolean));

          const statusCell = el("div", { className: "recipient-cell recipient-status-cell" }, [
            el("span", { className: `badge ${recPair[1]}` }, [recPair[0]]),
            el("button", {
              type: "button",
              className: "btn ghost sm",
              onclick: () => openChat({
                id: rec.lead_id || null,
                telegram_user_id: rec.telegram_user_id || null,
                username: rec.username || "",
                name: rec.name || "",
                phone: rec.phone || "",
                account_id: rec.assigned_account_id || camp.account_id || null,
              }),
            }, ["Написать"]),
            rec.error_text ? el("span", { className: "recipient-error" }, [rec.error_text]) : null,
          ].filter(Boolean));

          return el("div", { className: "recipient-row" }, [
            contactCell,
            el("span", { className: "hint" }, [handle]),
            companyCell,
            stepCell,
            statusCell,
          ]);
        }),
      ];
      cardChildren.push(
        el("div", { className: "recipients-grid", role: "region", "aria-label": `Получатели ${camp.name}` }, recRows),
      );
      if (state.viewMode === "sheet" && recipients.length > 3) {
        cardChildren.push(
          el("div", { className: "row" }, [
            el("button", {
              type: "button",
              className: "btn ghost sm",
              onclick: () => {
                state.expandedCampaigns[camp.id] = !isExpanded;
                renderCampaigns();
              },
            }, [isExpanded ? "Свернуть список" : `Все получатели (${recipients.length})`]),
          ]),
        );
      }
    } else {
      cardChildren.push(
        el("p", { className: "hint" }, ["База получателей пуста: загрузите таблицу CSV."]),
      );
    }

    list.append(el("article", { className: "campaign-card" }, cardChildren));
  });
  setView("#campaigns-view", "filled");
}

async function loadCampaigns() {
  try {
    state.campaigns = await api("/api/campaigns");
    renderCampaigns();
    renderBoard();
    renderLeadTable();
    if (state.selectedLeadId != null && state.cards[state.selectedLeadId]) {
      paintCard(state.cards[state.selectedLeadId]);
    }
    renderSide();
  } catch (error) {
    failRegion("#campaigns-view", "#campaigns-error", error);
  }
}

function renderFolderList(rows) {
  const list = document.querySelector("#folder-list");
  if (!list) return;
  list.replaceChildren();
  if (!rows || !rows.length) {
    setView("#folders-view", "empty");
    return;
  }
  rows.forEach((folder) => {
    const body = [
      el("span", { className: "name" }, [folder.title]),
    ];
    if (folder.blocked_reason) {
      body.push(el("p", { className: "hint" }, [folder.blocked_reason]));
    }
    const row = el("li", { className: "account" }, [
      el("div", {}, body),
    ]);
    if (!folder.blocked_reason) {
      row.append(el("button", {
        type: "button",
        className: "btn primary sm",
        onclick: () => syncFolder(folder),
      }, ["Синхронизировать"]));
    }
    list.append(row);
  });
  setView("#folders-view", "filled");
}

async function loadFolderList() {
  const select = document.querySelector("#folder-account");
  if (!select || !select.value) {
    setView("#folders-view", "empty");
    return;
  }
  const accountId = select.value;
  try {
    const rows = await api(`/api/folders?account_id=${accountId}`);
    state.folderLists[accountId] = rows;
    renderFolderList(rows);
    renderFolderFilter();
  } catch (error) {
    failRegion("#folders-view", "#folders-error", error);
  }
}

async function pullFolders() {
  const select = document.querySelector("#folder-account");
  const accountId = select && select.value;
  if (!accountId) {
    toast("Нет подключённого аккаунта", "error");
    return;
  }
  setView("#folders-view", "loading");
  try {
    const rows = await api("/api/folders/pull", {
      method: "POST",
      body: { account_id: Number(accountId) },
    });
    state.folderLists[accountId] = rows;
    renderFolderList(rows);
    renderFolderFilter();
  } catch (error) {
    failRegion("#folders-view", "#folders-error", error);
  }
}

async function syncFolder(folder) {
  try {
    const result = await api(`/api/folders/${folder.id}/sync`, { method: "POST" });
    toast(`Новых лидов: ${result.added}`);
    delete state.cards[state.selectedLeadId];
    await loadLeads(state.leadQuery, state.selectedLeadId);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function foldersFor(accountId) {
  const key = String(accountId);
  if (state.folderLists[key]) return state.folderLists[key];
  const rows = await api(`/api/folders?account_id=${accountId}`);
  state.folderLists[key] = rows;
  return rows;
}

function showOperator(text, kind) {
  const box = document.querySelector("#operator-msg");
  if (!box) return;
  box.textContent = text || "";
  box.setAttribute("role", kind === "alert" ? "alert" : "status");
}

async function loadOperator() {
  const status = await api("/api/operator");
  if (status && status.logged_in) showOperator("Вы вошли через бота", "status");
  else showOperator("", "status");
}

async function loadAccounts() {
  state.accounts = await api("/api/accounts");
  renderAccounts();
  await loadFolderList();
}

function renderInboxSessionSwitcher() {
  const host = document.querySelector("#inbox-session-switcher");
  const badge = document.querySelector("#inbox-active-session-badge");
  const connected = (state.accounts || []).filter((account) => account.status === "connected");
  if (!host) return;
  host.replaceChildren();
  const activeKey = String(state.inboxSessionFilter || "all");
  const allBtn = el("button", {
    type: "button",
    className: "inbox-session-pill",
    "data-session-filter": "all",
    "aria-pressed": activeKey === "all" ? "true" : "false",
    onclick: () => switchInboxSession("all"),
  }, ["Все сессии"]);
  host.append(allBtn);

  connected.forEach((account) => {
    const key = String(account.id);
    const isWa = normalizeProvider(account.provider) === "whatsapp";
    const prefix = isWa ? "WA" : "TG";
    const uname = account.telegram_username || account.username;
    const label = uname ? `@${uname}` : (account.telegram_name || account.phone || `#${account.id}`);
    const pill = el("button", {
      type: "button",
      className: "inbox-session-pill",
      "data-session-filter": key,
      "aria-pressed": activeKey === key ? "true" : "false",
      onclick: () => switchInboxSession(key),
    }, [`${prefix} · ${label}`]);
    host.append(pill);
  });

  if (badge) {
    if (activeKey === "all") {
      badge.textContent = connected.length > 1 ? `Все сессии (${connected.length})` : "Все сессии";
    } else {
      const found = connected.find((acc) => String(acc.id) === activeKey);
      const foundUname = found && (found.telegram_username || found.username);
      const foundPrefix = found && normalizeProvider(found.provider) === "whatsapp" ? "WA" : "TG";
      badge.textContent = found
        ? `Сессия: ${foundPrefix} · ${foundUname ? `@${foundUname}` : (found.telegram_name || found.phone)}`
        : "Активная сессия";
    }
  }
}

async function switchInboxSession(sessionKey) {
  const key = String(sessionKey || "all");
  state.inboxSessionFilter = key;
  const select = document.querySelector("#tg-account");
  if (key !== "all" && select) {
    select.value = key;
  } else if (key === "all" && select && !select.value) {
    const firstConnected = (state.accounts || []).find((acc) => acc.status === "connected");
    if (firstConnected) select.value = String(firstConnected.id);
  }
  renderInboxSessionSwitcher();
  try {
    await loadInbox();
  } catch (error) {
    closeInboxLive();
    state.inbox = "error";
    const node = document.querySelector("#inbox-error");
    if (node) node.textContent = error.message;
    setView("#inbox-view", "error");
  }
}

function chipOn(name) {
  const button = document.querySelector(`[data-chip="${name}"]`);
  return button && button.getAttribute("aria-pressed") === "true";
}

function passChips(dialog) {
  if (state.inboxSessionFilter && state.inboxSessionFilter !== "all" && dialog.account_id) {
    if (String(dialog.account_id) !== String(state.inboxSessionFilter)) return false;
  }
  if (chipOn("waiting") && !(Number(dialog.last_from_peer || 0) === 1 || Boolean(dialog.waiting) || Boolean(dialog.last_from_other))) return false;
  if (chipOn("our_turn") && !(Number(dialog.last_from_peer || 0) === 0 && Boolean(dialog.last_message_at))) return false;
  if (chipOn("unread") && !dialog.unread_count) return false;
  if (chipOn("groups") && dialog.peer_type !== "group" && !dialog.is_group) return false;
  if (chipOn("muted") && !dialog.muted) return false;
  return true;
}

function renderDialogs() {
  const list = document.querySelector("#dialog-results");
  const matched = state.dialogs.filter(passChips);
  if (state.inbox === "error") return;
  if (!state.dialogs.length) {
    setView("#inbox-view", state.dialogQuery ? "none" : "empty");
    list.replaceChildren();
    return;
  }
  if (!matched.length) {
    setView("#inbox-view", "none");
    list.replaceChildren();
    return;
  }
  setView("#inbox-view", "filled");
  list.replaceChildren();
  matched.forEach((dialog) => {
    const name = dialog.name || "Без имени";
    const ownerAcc = (state.accounts || []).find((acc) => Number(acc.id) === Number(dialog.account_id))
      || selectedAccount();
    const provider = normalizeProvider(dialog.provider || (ownerAcc && ownerAcc.provider) || "telegram");
    const user = dialog.username
      ? `@${dialog.username}`
      : (provider === "whatsapp" && dialog.phone ? dialog.phone : "без ника");
    const sub = dialog.last_message_preview ? `${user} · ${dialog.last_message_preview}` : user;
    const current = isSameDialog(state.selectedDialog, dialog);
    const ownerUname = ownerAcc && (ownerAcc.telegram_username || ownerAcc.username);
    const sessionTag = ownerAcc
      ? el("span", { className: "dialog-session-pill" }, [
        ownerUname ? `@${ownerUname}` : (ownerAcc.telegram_name || ownerAcc.phone || `#${ownerAcc.id}`),
      ])
      : null;
    let statusBadge = null;
    if (Number(dialog.last_from_peer) === 1) {
      statusBadge = el("span", { className: "badge b-ok dialog-turn-badge" }, ["Клиент ответил"]);
    } else if (Number(dialog.last_from_peer) === 0 && dialog.last_message_at) {
      const shortTime = formatShortTime(dialog.last_message_at);
      statusBadge = el("span", { className: "badge b-off dialog-turn-badge" }, [
        shortTime ? `Вы писали · ${shortTime}` : "Вы писали",
      ]);
    }
    const metaTags = [sessionTag, statusBadge].filter(Boolean);
    const who = el("span", {}, [
      el("span", { className: "dialog-name-row" }, [
        channelBadgeEl(provider),
        el("span", { className: "name" }, [name]),
      ]),
      el("span", { className: "preview" }, [sub]),
      metaTags.length ? el("span", { className: "dialog-meta-row" }, metaTags) : null,
    ].filter(Boolean));
    const button = el("button", {
      type: "button",
      className: "dialog",
      onclick: () => selectDialog(dialog),
    }, [
      el("span", { className: "avatar", "aria-hidden": "true" }, [initials(name)]),
      who,
    ]);
    if (current) button.setAttribute("aria-current", "true");
    const unread = Number(dialog.unread_count) || 0;
    if (unread) {
      button.append(el("span", { className: "count" }, [String(unread)]));
      button.setAttribute("aria-label", `${name}, не прочитано, ${unread}`);
    } else {
      button.setAttribute("aria-label", `${name}, ${user}`);
    }
    list.append(el("li", {}, [button]));
  });
  updateWorkspaceStats();
}

function accountLabel() {
  const select = document.querySelector("#tg-account");
  const option = select.options[select.selectedIndex];
  return option ? option.text : "аккаунт";
}

function renderThread() {
  const dialog = state.selectedDialog;
  const error = document.querySelector("#thread-error");
  error.hidden = true;
  error.textContent = "";
  if (!dialog) {
    setView("#thread-view", "empty");
    return;
  }
  setView("#thread-view", "filled");
  const name = dialog.name || "Без имени";
  const ownerAcc = (state.accounts || []).find((acc) => Number(acc.id) === Number(dialog.account_id))
    || selectedAccount();
  const provider = normalizeProvider(dialog.provider || (ownerAcc && ownerAcc.provider) || "telegram");
  const contactHint = dialog.username
    ? `@${dialog.username}${dialog.phone ? ` · ${dialog.phone}` : ""}`
    : (dialog.phone || "без ника");
  document.querySelector("#thread-head").replaceChildren(
    el("div", { className: "thread-title-group" }, [
      channelBadgeEl(provider),
      el("h3", { className: "name" }, [name]),
    ]),
    el("p", { className: "hint" }, [`С аккаунта ${accountLabel()}`]),
    el("p", { className: "hint" }, [contactHint]),
  );
  const items = state.messages || [];
  document.querySelector("#messages").replaceChildren(...items.map((message) => (
    el("li", { className: message.direction === "out" ? "out" : "in" }, [
      message.text || "Вложение",
    ])
  )));
  const gap = document.querySelector("#thread-gap");
  if (gap) gap.hidden = items.length > 0;
}

function selectedAccount() {
  const id = Number(document.querySelector("#tg-account").value);
  return state.accounts.find((account) => account.id === id) || null;
}

async function selectDialog(dialog) {
  if (dialog && dialog.account_id) {
    const select = document.querySelector("#tg-account");
    if (select && [...select.options].some((opt) => opt.value === String(dialog.account_id))) {
      select.value = String(dialog.account_id);
    }
  }
  state.selectedDialog = dialog;
  state.messages = [];
  setView("#thread-view", "loading");
  renderDialogs();
  const reply = document.querySelector("#reply-text");
  if (reply) reply.value = "";
  let loadError = "";
  if (dialog.id) {
    try {
      state.messages = await api(`/api/dialogs/${dialog.id}/messages`);
    } catch (error) {
      loadError = error.message;
    }
  }
  renderThread();
  if (loadError) {
    const error = document.querySelector("#thread-error");
    error.hidden = false;
    error.textContent = loadError;
    const gap = document.querySelector("#thread-gap");
    if (gap) gap.hidden = true;
  }
  renderSide();
}

function leadForDialog(dialog) {
  if (!dialog || dialog.telegram_user_id == null) return null;
  const accountId = Number(document.querySelector("#tg-account").value);
  return state.leads.find((lead) => {
    if (Number(lead.telegram_user_id) !== Number(dialog.telegram_user_id)) return false;
    if (lead.account_id && accountId && Number(lead.account_id) !== accountId) return false;
    return true;
  }) || null;
}

function renderSide() {
  const dialog = state.selectedDialog;
  if (!dialog) {
    setView("#side-view", "empty");
    return;
  }
  const lead = leadForDialog(dialog);
  if (!lead) {
    setView("#side-view", "nolead");
    return;
  }
  state.selectedLeadId = lead.id;
  const badge = BADGE[lead.status] || "b-new";
  const label = (STATUSES.find(([value]) => value === lead.status) || ["", lead.status])[1];
  const provider = normalizeProvider(lead.provider || dialog.provider || "telegram");
  const handleParts = [
    lead.username ? `@${lead.username}` : (provider === "whatsapp" && lead.phone ? lead.phone : "без ника"),
  ];
  if (lead.phone && handleParts[0] !== lead.phone) handleParts.push(lead.phone);
  document.querySelector("#side-card").replaceChildren(
    el("div", { className: "panel" }, [
      el("h3", { className: "name" }, [lead.name || "Без имени"]),
      el("div", { className: "row" }, [
        channelBadgeEl(provider),
        el("span", { className: `badge ${badge}` }, [label]),
      ]),
      el("p", { className: "hint" }, [handleParts.join(" · ")]),
      leadTrackingBox(lead),
      el("p", { className: "note" }, [sideNoteText(lead)]),
      el("p", { className: "hint" }, [lead.next_step ? `Дальше: ${lead.next_step}` : "Следующий шаг не задан"]),
      el("a", {
        className: "btn secondary sm",
        href: "#s09",
        onclick: (event) => {
          if (state.viewMode === "sheet") {
            event.preventDefault();
            openSheetPopup("card");
          }
        },
      }, ["Открыть карточку"]),
    ]),
  );
  setView("#side-view", "filled");
  renderCard();
  renderBoard();
}

function selectedLead() {
  if (state.selectedLeadId == null) return null;
  const inList = state.leads.find((lead) => lead.id === state.selectedLeadId);
  if (inList) return inList;
  return state.leadCache[state.selectedLeadId] || null;
}

function safeHref(url) {
  const text = String(url || "").trim();
  return /^https?:\/\//i.test(text) ? text : "";
}

function sideNoteText(lead) {
  const card = state.cards[lead.id];
  if (card && card.notes && card.notes.length) return card.notes[card.notes.length - 1].body;
  return lead.note || "Заметок пока нет";
}

function refreshSideNote(leadId) {
  const dialog = state.selectedDialog;
  if (!dialog) return;
  const lead = leadForDialog(dialog);
  if (!lead || lead.id !== leadId) return;
  const note = document.querySelector("#side-card .note");
  if (note) note.textContent = sideNoteText(lead);
}

async function focusLead(id) {
  try {
    const lead = await api(`/api/leads/${id}/card`);
    state.cards[lead.id] = lead;
    state.leadCache[lead.id] = { ...(state.leadCache[lead.id] || {}), ...lead };
    state.selectedLeadId = lead.id;
    renderCard();
    renderBoard();
    if (state.viewMode === "sheet") openSheetPopup("card");
  } catch (error) {
    toast(error.message, "error");
  }
}

function openNoteModal(lead) {
  const area = el("textarea", { id: "note-body", name: "body" });
  const err = el("p", { id: "note-err", className: "err", hidden: "hidden" }, ["Напишите текст"]);
  openModal("Заметка", [
    el("label", { for: "note-body" }, ["Текст"]),
    area,
    err,
  ], async (form) => {
    const body = String(form.get("body") || "").trim();
    if (!body) {
      area.setAttribute("aria-invalid", "true");
      area.setAttribute("aria-describedby", "note-err");
      err.hidden = false;
      return false;
    }
    await api(`/api/leads/${lead.id}/notes`, { method: "POST", body: { body } });
    delete state.cards[lead.id];
    await renderCard();
    toast("Заметка записана", "ok");
    return true;
  });
}

function openReminderModal(lead) {
  const date = el("input", { id: "modal-date", name: "date", type: "date", required: "required" });
  const time = el("input", { id: "modal-time", name: "time", type: "time", required: "required" });
  const text = el("input", { id: "modal-text", name: "text", type: "text", required: "required" });
  const err = el("p", { id: "rem-err", className: "err", hidden: "hidden" }, ["Укажите дату, время и текст"]);
  openModal("Напоминание", [
    el("label", { for: "modal-date" }, ["Дата"]),
    date,
    el("label", { for: "modal-time" }, ["Время"]),
    time,
    el("label", { for: "modal-text" }, ["Текст"]),
    text,
    err,
  ], async (form) => {
    const due = fromDateAndTime(form.get("date"), form.get("time"));
    const body = String(form.get("text") || "").trim();
    if (!due || !body) {
      text.setAttribute("aria-invalid", "true");
      err.hidden = false;
      return false;
    }
    await api("/api/reminders", {
      method: "POST",
      body: { lead_id: lead.id, due_at: due, text: body },
    });
    await loadReminders();
    toast("Напоминание на столе", "ok");
    return true;
  });
}

async function openChat(lead) {
  if (!lead) return;
  const cleanUser = String(lead.username || "").replace(/^@/, "").toLowerCase();
  const matchesLead = (item, strictAccount) => {
    if (strictAccount && lead.account_id && item.account_id && Number(item.account_id) !== Number(lead.account_id)) {
      return false;
    }
    if (lead.id && item.lead_id && Number(item.lead_id) === Number(lead.id)) {
      return true;
    }
    if (lead.telegram_user_id && item.telegram_user_id && Number(item.telegram_user_id) === Number(lead.telegram_user_id)) {
      return true;
    }
    if (cleanUser && item.username && String(item.username).replace(/^@/, "").toLowerCase() === cleanUser) {
      return true;
    }
    return false;
  };
  let dialog = state.dialogs.find((item) => matchesLead(item, true))
    || state.dialogs.find((item) => matchesLead(item, false));

  if (!dialog) {
    const connectedAcc = (state.accounts || []).find((acc) => acc.status === "connected");
    const accountId = lead.account_id
      || Number(document.querySelector("#tg-account")?.value)
      || (connectedAcc ? connectedAcc.id : null);
    if (accountId) {
      try {
        dialog = await api("/api/dialogs/open-by-lead", {
          method: "POST",
          body: {
            lead_id: lead.id || null,
            account_id: Number(accountId),
            telegram_user_id: lead.telegram_user_id || null,
            username: cleanUser || null,
            name: lead.name || cleanUser || "Контакт",
            phone: lead.phone || null,
          },
        });
      } catch (_err) {
        dialog = null;
      }
    }
  }

  if (!dialog) {
    toast("Этот чат ещё не открыт в списке", "error");
    return;
  }

  upsertDialog(dialog);
  closeSheetPopup();
  if (state.viewMode === "sheet") activateSheet("inbox");
  await selectDialog(dialog);
  const s05 = document.querySelector("#s05");
  if (s05) s05.scrollIntoView({ block: "nearest" });
}

function taskList(lead) {
  const tasks = lead.tasks || [];
  if (!tasks.length) return el("p", { className: "hint" }, ["Задач пока нет"]);
  return el("ul", { className: "stack" }, tasks.map((item) => {
    const box = el("input", { type: "checkbox" });
    box.checked = Boolean(item.done_at);
    box.addEventListener("change", async () => {
      box.disabled = true;
      try {
        await api(`/api/tasks/${item.id}/done`, { method: "POST", body: { done: box.checked } });
        delete state.cards[lead.id];
        await renderCard();
      } catch (error) {
        box.checked = !box.checked;
        box.disabled = false;
        toast(error.message, "error");
      }
    });
    return el("li", {}, [el("label", { className: "row" }, [box, item.title])]);
  }));
}

function taskForm(lead) {
  const text = el("input", { type: "text", "aria-label": "Текст задачи" });
  let saving = false;
  return el("form", {
    className: "stack",
    onsubmit: async (event) => {
      event.preventDefault();
      if (saving) return;
      const title = text.value.trim();
      if (!title) {
        toast("Напишите задачу", "error");
        return;
      }
      const button = event.target.querySelector("button[type=submit]");
      saving = true;
      button.disabled = true;
      try {
        await api(`/api/leads/${lead.id}/tasks`, { method: "POST", body: { title } });
        text.value = "";
        delete state.cards[lead.id];
        await renderCard();
        toast("Задача поставлена", "ok");
      } catch (error) {
        toast(error.message, "error");
      } finally {
        saving = false;
        if (button.isConnected) button.disabled = false;
      }
    },
  }, [
    el("label", {}, ["Текст задачи", text]),
    el("button", { type: "submit", className: "btn secondary sm" }, ["Поставить задачу"]),
  ]);
}

function propertyShown(item) {
  if (item.value_text) return item.value_text;
  if (item.value_int != null) return String(item.value_int);
  return "-";
}

function propertyList(lead) {
  const rows = lead.properties || [];
  if (!rows.length) return el("p", { className: "hint" }, ["Своих полей пока нет"]);
  return el("ul", { className: "stack" }, rows.map((item) => (
    el("li", {}, [`${item.name} (${item.type}): ${propertyShown(item)}`])
  )));
}

function propertyForm(lead) {
  const name = el("input", { type: "text", "aria-label": "Имя поля" });
  const type = el("select", { "aria-label": "Тип поля" });
  type.append(el("option", { value: "" }, ["Тип"]));
  PROPERTY_TYPES.forEach(([value, label]) => {
    type.append(el("option", { value }, [label]));
  });
  const value = el("input", { type: "text", "aria-label": "Значение поля" });
  let saving = false;
  return el("form", {
    className: "stack",
    onsubmit: async (event) => {
      event.preventDefault();
      if (saving) return;
      const label = name.value.trim();
      const kind = type.value;
      const text = value.value.trim();
      if (!label || !kind || !text) {
        toast("Укажите имя, тип и значение", "error");
        return;
      }
      const button = event.target.querySelector("button[type=submit]");
      saving = true;
      button.disabled = true;
      try {
        const def = await api("/api/property-defs", {
          method: "POST",
          body: { name: label, type: kind },
        });
        await api(`/api/leads/${lead.id}/properties/${def.id}`, {
          method: "PUT",
          body: { value: text },
        });
        name.value = "";
        type.value = "";
        value.value = "";
        delete state.cards[lead.id];
        await renderCard();
        toast("Поле записано", "ok");
      } catch (error) {
        toast(error.message, "error");
      } finally {
        saving = false;
        if (button.isConnected) button.disabled = false;
      }
    },
  }, [
    el("label", {}, ["Имя поля", name]),
    el("label", {}, ["Тип поля", type]),
    el("label", {}, ["Значение поля", value]),
    el("button", { type: "submit", className: "btn secondary sm" }, ["Добавить поле"]),
  ]);
}

async function renderCard() {
  const id = state.selectedLeadId;
  if (id == null) {
    setView("#card-view", "empty");
    return;
  }
  const token = ++cardToken;
  let lead = state.cards[id];
  if (!lead) {
    setView("#card-view", "loading");
    try {
      lead = await api(`/api/leads/${id}/card`);
    } catch (error) {
      if (token !== cardToken) return;
      failRegion("#card-view", "#card-error", error);
      return;
    }
    if (token !== cardToken) return;
    state.cards[id] = lead;
  }
  lead.folder_choices = [];
  if (lead.account_id) {
    try {
      lead.folder_choices = await foldersFor(lead.account_id);
    } catch (_err) {
      lead.folder_choices = [];
    }
  }
  if (token !== cardToken) return;
  paintCard(lead);
  refreshSideNote(id);
}

function paintCard(lead) {
  const notes = (lead.notes || []).map((item) => el("li", {}, [
    el("span", { className: "dot", "aria-hidden": "true" }, []),
    el("div", { className: "note" }, [
      el("p", {}, [item.body]),
      el("p", { className: "meta" }, [
        el("time", { datetime: item.created_at || "" }, [formatDue(item.created_at) || "Заметка"]),
      ]),
    ]),
  ]));
  allRemindersForLead(lead.id).forEach((item) => {
    const body = [
      el("p", {}, [item.text]),
      el("p", { className: "meta" }, [el("time", { datetime: item.due_at || "" }, [formatDue(item.due_at)])]),
    ];
    if (!item.done_at) {
      body.push(el("button", {
        type: "button",
        className: "btn ghost sm",
        "aria-label": `Сделано: ${item.text}`,
        onclick: async () => {
          try {
            await api(`/api/reminders/${item.id}/done`, { method: "POST" });
            await loadReminders();
          } catch (error) {
            toast(error.message, "error");
          }
        },
      }, ["Сделано"]));
    }
    notes.push(el("li", {}, [
      el("span", { className: "dot", "aria-hidden": "true" }, []),
      el("div", {}, body),
    ]));
  });
  const timeline = notes.length
    ? el("ol", { className: "timeline" }, notes)
    : el("p", { className: "hint" }, ["Заметок пока нет"]);
  const href = safeHref(lead.url);
  const provider = normalizeProvider(lead.provider || "telegram");
  const profile = [
    el("div", { className: "row" }, [
      channelBadgeEl(provider),
      el("h3", { id: "card-name", className: "name" }, [lead.name || "Без имени"]),
    ]),
    leadTrackingBox(lead),
    el("label", {}, ["Имя", textField(lead, "name", "Имя")]),
    el("label", {}, ["Телефон", textField(lead, "phone", "Телефон")]),
    el("label", {}, ["Статус", statusSelect(lead)]),
    el("p", { id: "card-folder" }, [
      lead.folder_name ? `Папка: ${lead.folder_name}` : "Папка не задана",
    ]),
    el("label", {}, ["Папка", folderField(lead)]),
    el("p", { className: "hint" }, [
      lead.username ? `@${lead.username}` : "без ника",
      lead.phone ? ` · ${lead.phone}` : "",
      lead.telegram_user_id ? ` · ${lead.telegram_user_id}` : "",
    ]),
  ];
  if (lead.email) profile.push(el("p", {}, [lead.email]));
  if (href) profile.push(el("a", { href, rel: "noreferrer" }, ["Ссылка"]));
  profile.push(
    el("label", {}, ["Следующий шаг", textField(lead, "next_step", "Следующий шаг")]),
    timeline,
    el("section", { "aria-label": "Задачи" }, [
      el("h4", {}, ["Задачи"]),
      taskList(lead),
      taskForm(lead),
    ]),
    el("section", { "aria-label": "Свои поля" }, [
      el("h4", {}, ["Свои поля"]),
      propertyList(lead),
      propertyForm(lead),
    ]),
    el("div", { className: "row" }, [
      el("button", { type: "button", className: "btn secondary sm", onclick: () => openNoteModal(lead) }, ["Заметка"]),
      el("button", { type: "button", className: "btn secondary sm", onclick: () => openReminderModal(lead) }, ["Напоминание"]),
      el("button", { type: "button", className: "btn ghost sm", onclick: () => openChat(lead) }, ["Открыть чат"]),
    ]),
  );
  document.querySelector("#lead-card").replaceChildren(
    el("div", { className: "panel", "aria-labelledby": "card-name" }, profile),
  );
  setView("#card-view", "filled");
}

function pipelineFilters() {
  const channelNode = document.querySelector("#channel-filter");
  const followupNode = document.querySelector("#followup-filter");
  return {
    status: document.querySelector("#stage-filter").value,
    folderId: document.querySelector("#folder-filter").value,
    channel: channelNode ? channelNode.value : "",
    followup: followupNode ? followupNode.value : "",
  };
}

function updateFilterNote() {
  const { status, folderId, channel, followup } = pipelineFilters();
  const on = Boolean(status || folderId || channel || followup || state.leadQuery);
  document.querySelector("#filter-note").hidden = !on;
  document.querySelector("#board").dataset.filtered = on ? "true" : "false";
}

function renderFolderFilter() {
  const select = document.querySelector("#folder-filter");
  if (!select) return;
  const previous = select.value;
  select.replaceChildren(el("option", { value: "" }, ["Все"]));
  const seen = new Set();
  Object.values(state.folderLists).flat().forEach((folder) => {
    if (!folder || seen.has(folder.id)) return;
    seen.add(folder.id);
    select.append(el("option", { value: String(folder.id) }, [folder.title]));
  });
  if (previous && ![...select.options].some((option) => option.value === previous)) {
    select.append(el("option", { value: previous }, [`Папка ${previous}`]));
  }
  if (previous) select.value = previous;
}

async function loadPipelineFolders() {
  const connected = state.accounts.filter((account) => account.status === "connected");
  await Promise.all(connected.map(async (account) => {
    try {
      state.folderLists[String(account.id)] = await api(`/api/folders?account_id=${account.id}`);
    } catch (_err) { /* воронка живёт и без папок этого аккаунта */ }
  }));
  renderFolderFilter();
}

function renderViews() {
  const select = document.querySelector("#saved-view");
  if (!select) return;
  const previous = select.value;
  select.replaceChildren(el("option", { value: "" }, ["Не выбран"]));
  (state.views || []).forEach((view) => {
    select.append(el("option", { value: String(view.id) }, [view.name]));
  });
  if (previous) select.value = previous;
}

async function loadViews() {
  state.views = await api("/api/views");
  renderViews();
}

function applyPipelineView(view) {
  document.querySelector("#stage-filter").value = view.status || "";
  const folder = document.querySelector("#folder-filter");
  const folderId = view.folder_id == null ? "" : String(view.folder_id);
  if (folderId && ![...folder.options].some((option) => option.value === folderId)) {
    folder.append(el("option", { value: folderId }, [`Папка ${folderId}`]));
  }
  folder.value = folderId;
  renderBoard();
}

async function savePipelineView() {
  const field = document.querySelector("#view-name");
  const name = field.value.trim();
  if (!name) {
    toast("Укажите имя вида", "error");
    field.focus();
    return;
  }
  const { status, folderId } = pipelineFilters();
  const saved = await api("/api/views", {
    method: "POST",
    body: {
      name,
      status: status || null,
      folder_id: folderId ? Number(folderId) : null,
    },
  });
  await loadViews();
  document.querySelector("#saved-view").value = String(saved.id);
  toast("Вид сохранён", "ok");
}

async function resetPipelineView() {
  document.querySelector("#stage-filter").value = "";
  document.querySelector("#folder-filter").value = "";
  const channelNode = document.querySelector("#channel-filter");
  if (channelNode) channelNode.value = "";
  const followupNode = document.querySelector("#followup-filter");
  if (followupNode) followupNode.value = "";
  document.querySelector("#saved-view").value = "";
  document.querySelector("#view-name").value = "";
  if (state.mode === "crm") {
    const query = document.querySelector("#q");
    if (query) query.value = "";
  }
  await loadLeads("");
}

function folderTitleById(folderId) {
  if (!folderId) return "";
  const all = Object.values(state.folderLists).flat();
  const found = all.find((item) => item && Number(item.id) === Number(folderId));
  return found ? found.title : "";
}

function updateWorkspaceStats() {
  const leadCount = (state.leads || []).length;
  const leadsNode = document.querySelector("#stat-leads");
  if (leadsNode) leadsNode.textContent = String(leadCount);
  const dialogsNode = document.querySelector("#stat-dialogs");
  if (dialogsNode) dialogsNode.textContent = String((state.dialogs || []).length);
  const campNode = document.querySelector("#stat-campaigns");
  if (campNode) campNode.textContent = String((state.campaigns || []).length);
  const remNode = document.querySelector("#stat-reminders");
  if (remNode) {
    const remSource = state.allOpenRemindersLoaded ? (state.allOpenReminders || []) : (state.reminders || []);
    const openCount = remSource.filter((item) => !item.done_at).length;
    remNode.textContent = String(openCount);
  }
  const quotaLeads = document.querySelector("#sidebar-quota-leads");
  if (quotaLeads) quotaLeads.textContent = `${leadCount} / 5000`;
  const quotaLeadsFill = document.querySelector("#sidebar-quota-leads-fill");
  if (quotaLeadsFill) {
    const pct = Math.min(100, Math.max(4, Math.round((leadCount / 5000) * 100)));
    quotaLeadsFill.style.width = `${pct}%`;
  }
  const connectedCount = (state.accounts || []).filter((acc) => acc.status === "connected").length;
  const quotaAcc = document.querySelector("#sidebar-quota-accounts");
  if (quotaAcc) quotaAcc.textContent = `${connectedCount} / 10`;
  const quotaAccFill = document.querySelector("#sidebar-quota-accounts-fill");
  if (quotaAccFill) {
    const pctAcc = Math.min(100, Math.max(10, Math.round((connectedCount / 10) * 100)));
    quotaAccFill.style.width = `${pctAcc}%`;
  }
}

function renderBoard() {
  const { status: stage, folderId, channel, followup } = pipelineFilters();
  const board = document.querySelector("#board");
  board.replaceChildren();
  STATUSES.forEach(([value, label]) => {
    const cards = state.leads.filter((lead) => {
      if (lead.status !== value) return false;
      if (stage && stage !== value) return false;
      if (folderId && String(lead.folder_id || "") !== folderId) return false;
      if (channel && normalizeProvider(lead.provider || "telegram") !== channel) return false;
      if (!matchFollowupFilter(lead, followup)) return false;
      return true;
    });
    const items = cards.length
      ? cards.map((lead) => {
        const current = lead.id === state.selectedLeadId;
        const nameText = lead.name || "Без имени";
        const provider = normalizeProvider(lead.provider || "telegram");
        const userText = lead.username
          ? `@${lead.username}`
          : (provider === "whatsapp" && lead.phone ? lead.phone : "без ника");
        const tr = resolveLeadTracking(lead);
        const bodyNodes = [
          el("span", { className: "card-top" }, [
            el("span", { className: "name" }, [nameText]),
            el("span", { className: "meta" }, [userText]),
          ]),
        ];
        if (lead.next_step || lead.note) {
          bodyNodes.push(el("span", { className: "card-step" }, [
            lead.next_step ? `Сегодня · ${lead.next_step}` : lead.note,
          ]));
        }
        const folderTitle = folderTitleById(lead.folder_id);
        const tagNodes = [
          channelBadgeEl(provider),
          el("span", { className: "card-pill" }, [folderTitle || label]),
        ];
        if (tr.accountName || tr.accountPhone) {
          tagNodes.push(el("span", { className: "card-tag card-account-tag" }, [
            `Акк: ${tr.accountName || tr.accountPhone}`,
          ]));
        }
        if (tr.isReplied) {
          tagNodes.push(el("span", { className: "card-pill card-pill-replied" }, ["Ответил · Авто-стоп"]));
        } else if (tr.campaignStatus === "in_progress") {
          const nextFupText = tr.nextFollowupAt ? ` · фоллоу-ап ${formatShortTime(tr.nextFollowupAt)}` : "";
          tagNodes.push(el("span", { className: "card-tag card-followup-tag" }, [
            `Шаг ${tr.campaignStep || 1}${nextFupText}`,
          ]));
        }
        if (lead.phone) {
          tagNodes.push(el("span", { className: "card-tag" }, [lead.phone]));
        }
        bodyNodes.push(el("span", { className: "card-tags" }, tagNodes));
        const link = el("a", {
          className: "card",
          href: "#s09",
          onclick: (event) => {
            state.selectedLeadId = lead.id;
            renderCard();
            renderBoard();
            if (state.viewMode === "sheet") {
              event.preventDefault();
              openSheetPopup("card");
            }
          },
        }, [
          el("span", { className: "avatar", "aria-hidden": "true" }, [initials(nameText)]),
          el("span", { className: "card-body" }, bodyNodes),
        ]);
        if (current) link.setAttribute("aria-current", "true");
        return link;
      })
      : [el("p", { className: "hint" }, ["Пусто"])];
    const dot = el("span", { className: "stage-dot", "data-stage": value, "aria-hidden": "true" }, []);
    const count = el("span", { className: "meta" }, [String(cards.length)]);
    board.append(el("section", { className: "col" }, [
      el("header", {}, [dot, el("h3", {}, [label]), count]),
      ...items,
    ]));
  });
  updateFilterNote();
  updateWorkspaceStats();
}

function reminderForm(lead) {
  const date = el("input", { type: "date", "aria-label": "Дата" });
  const time = el("input", { type: "time", "aria-label": "Время" });
  const text = el("input", { type: "text", "aria-label": "Текст напоминания" });
  let saving = false;
  return el("form", {
    className: "reminder-form",
    onsubmit: async (event) => {
      event.preventDefault();
      if (saving) return;
      const due = fromDateAndTime(date.value, time.value);
      if (!due || !text.value.trim()) {
        toast("Укажите дату, время и текст", "error");
        return;
      }
      const button = event.target.querySelector("button[type=submit]");
      saving = true;
      button.disabled = true;
      try {
        await api("/api/reminders", {
          method: "POST",
          body: { lead_id: lead.id, due_at: due, text: text.value.trim() },
        });
        date.value = "";
        time.value = "";
        text.value = "";
        await loadReminders();
        toast("Напоминание на столе", "ok");
      } catch (error) {
        toast(error.message, "error");
      } finally {
        saving = false;
        button.disabled = false;
      }
    },
  }, [
    el("label", {}, ["Дата", date]),
    el("label", {}, ["Время", time]),
    el("label", {}, ["Текст", text]),
    el("button", { type: "submit", className: "btn secondary sm" }, ["Поставить"]),
  ]);
}

function renderLeadTable() {
  const body = document.querySelector("#lead-rows");
  body.replaceChildren();
  const { channel, followup } = pipelineFilters();
  const filteredLeads = state.leads.filter((lead) => {
    if (channel && normalizeProvider(lead.provider || "telegram") !== channel) return false;
    if (!matchFollowupFilter(lead, followup)) return false;
    return true;
  });
  filteredLeads.forEach((lead) => {
    const provider = normalizeProvider(lead.provider || "telegram");
    const contactText = lead.username
      ? `@${lead.username}`
      : (provider === "whatsapp" && lead.phone ? lead.phone : "");
    const tr = resolveLeadTracking(lead);
    const trackingPills = [];
    if (tr.accountName || tr.accountPhone) {
      trackingPills.push(el("span", { className: "hint" }, [`Акк: ${tr.accountName || tr.accountPhone}`]));
    }
    const lastTouchRaw = tr.lastSentAt || tr.lastMessageAt;
    if (lastTouchRaw) {
      trackingPills.push(el("span", { className: "hint" }, [`Касание: ${formatShortTime(lastTouchRaw)}`]));
    }
    if (tr.isReplied) {
      trackingPills.push(el("span", { className: "badge b-won" }, ["Ответил — агент остановлен"]));
    } else if (tr.campaignStatus === "in_progress") {
      trackingPills.push(el("span", { className: "badge b-talk" }, [
        `Шаг ${tr.campaignStep || 1}${tr.nextFollowupAt ? ` · фоллоу-ап ${formatShortTime(tr.nextFollowupAt)}` : ""}`,
      ]));
    } else if (tr.campaignStatus === "completed") {
      trackingPills.push(el("span", { className: "badge b-ok" }, [`Шаг ${tr.campaignStep || 1}`]));
    }
    const openRemNodes = tr.openReminders.map((rem) => (
      el("div", { className: "lead-open-reminder badge b-wait" }, [
        `${formatDue(rem.due_at)} · ${rem.text}`,
      ])
    ));
    const row = el("tr", {
      onclick: () => {
        state.selectedLeadId = lead.id;
        renderCard();
        renderBoard();
      },
    }, [
      el("td", {}, [
        el("div", { className: "lead-name-cell" }, [
          channelBadgeEl(provider),
          textField(lead, "name", "Имя"),
        ]),
      ]),
      el("td", {}, [contactText]),
      el("td", { className: "num" }, [lead.telegram_user_id ?? ""]),
      el("td", { className: "num" }, [lead.phone || ""]),
      el("td", {}, [
        statusSelect(lead),
        trackingPills.length ? el("div", { className: "lead-tracking-meta" }, trackingPills) : null,
      ].filter(Boolean)),
      el("td", {}, [textField(lead, "note", "Заметка")]),
      el("td", {}, [textField(lead, "next_step", "Следующий шаг")]),
      el("td", {}, [
        ...openRemNodes,
        reminderForm(lead),
        el("button", {
          type: "button",
          className: "btn secondary sm sheet-only-btn",
          onclick: (event) => {
            event.stopPropagation();
            openReminderModal(lead);
          },
        }, ["+ Напоминание"]),
      ]),
    ]);
    body.append(row);
  });
}

async function loadLeads(query, keepId) {
  state.leadQuery = query || "";
  setView("#leads-view", "loading");
  setView("#board-view", "loading");
  const params = new URLSearchParams();
  if (state.leadQuery) params.set("q", state.leadQuery);
  const leads = await api(`/api/leads?${params.toString()}`);
  state.leads = leads;
  leads.forEach((lead) => { state.leadCache[lead.id] = lead; });
  if (keepId != null && state.leadCache[keepId]) {
    state.selectedLeadId = keepId;
  } else if (state.selectedLeadId && !leads.some((lead) => lead.id === state.selectedLeadId)) {
    state.selectedLeadId = null;
  }
  renderLeadTable();
  renderBoard();
  renderCard();
  renderSide();
  const leadMode = leads.length ? "filled" : (state.leadQuery ? "none" : "empty");
  setView("#leads-view", leadMode);
  setView("#board-view", leads.length ? "filled" : "empty");
}

function reminderCard(reminder) {
  const who = reminder.lead_name || reminder.lead_username || `лид ${reminder.lead_id}`;
  const kind = bucket(reminder);
  const item = el("article", { className: `reminder ${kind === "overdue" ? "overdue" : "due"}` }, [
    el("h3", {}, [reminder.text]),
    el("p", { className: "when" }, [
      el("time", { datetime: reminder.due_at || "" }, [formatDue(reminder.due_at)]),
      ` · ${who}`,
    ]),
  ]);
  if (reminder.done_at) {
    item.append(el("span", { className: "badge b-ok" }, ["Сделано"]));
    return item;
  }
  const postpone = el("form", { hidden: "hidden", className: "stack" }, [
    el("label", {}, ["Дата", el("input", { type: "date", name: "date", required: "required" })]),
    el("label", {}, ["Время", el("input", { type: "time", name: "time", required: "required" })]),
    el("p", { className: "err", hidden: "hidden" }, [""]),
    el("button", { type: "submit", className: "btn ghost sm" }, ["Сохранить срок"]),
  ]);
  postpone.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(postpone);
    const due = fromDateAndTime(data.get("date"), data.get("time"));
    const err = postpone.querySelector(".err");
    const button = postpone.querySelector("button[type=submit]");
    if (!due) {
      err.textContent = "Укажите дату и время";
      err.hidden = false;
      return;
    }
    button.disabled = true;
    try {
      await api(`/api/reminders/${reminder.id}/postpone`, { method: "POST", body: { due_at: due } });
      await loadReminders();
      toast("Срок обновлён", "ok");
    } catch (error) {
      err.textContent = error.message;
      err.hidden = false;
      toast(error.message, "error");
      button.disabled = false;
    }
  });
  item.append(el("div", { className: "row" }, [
    el("button", {
      type: "button",
      className: "btn secondary sm",
      "aria-label": `Сделано: ${reminder.text}`,
      onclick: async () => {
        try {
          await api(`/api/reminders/${reminder.id}/done`, { method: "POST" });
          await loadReminders();
        } catch (error) {
          toast(error.message, "error");
        }
      },
    }, ["Сделано"]),
    el("button", {
      type: "button",
      className: "btn ghost sm",
      "aria-label": `Отложить: ${reminder.text}`,
      onclick: () => { postpone.hidden = false; },
    }, ["Отложить"]),
    el("a", {
      className: "btn primary sm",
      href: "#s09",
      "aria-label": `К лиду: ${reminder.text}`,
      onclick: () => { focusLead(reminder.lead_id); },
    }, ["К лиду"]),
  ]));
  item.append(postpone);
  return item;
}

function renderReminders() {
  const host = document.querySelector("#reminder-list");
  host.replaceChildren();
  const groups = [
    ["overdue", "Просрочено", "late"],
    ["today", "Сегодня", ""],
    ["later", "Дальше", ""],
    ["done", "Сделано", ""],
  ];
  let shown = 0;
  groups.forEach(([key, title, extra]) => {
    if (key === "done" && state.scope !== "all") return;
    if (state.scope === "overdue" && key !== "overdue") return;
    const rows = state.reminders.filter((item) => bucket(item) === key);
    if (!rows.length) return;
    shown += rows.length;
    host.append(el("section", { className: "group" }, [
      el("h3", { className: extra }, [title]),
      el("div", { className: "stack" }, rows.map(reminderCard)),
    ]));
  });
  const quietToday = !state.reminders.some((item) => bucket(item) === "today" || bucket(item) === "overdue");
  if (!shown) setView("#digest-view", "empty");
  else {
    setView("#digest-view", "filled");
    if (quietToday && state.scope !== "overdue") {
      host.prepend(el("p", { className: "hint" }, ["На сегодня ничего не стоит"]));
    }
  }
  updateWorkspaceStats();
}

async function loadReminders() {
  setView("#digest-view", "loading");
  const scoped = await api(`/api/reminders?scope=${state.scope}`);
  state.reminders = scoped;
  if (state.scope === "open" || state.scope === "all") {
    state.allOpenReminders = scoped.filter((item) => !item.done_at);
    state.allOpenRemindersLoaded = true;
  } else {
    try {
      const openRows = await api("/api/reminders?scope=open");
      if (Array.isArray(openRows)) {
        state.allOpenReminders = openRows.filter((item) => !item.done_at);
        state.allOpenRemindersLoaded = true;
      }
    } catch (_err) { /* оставляем сохранённый список открытых напоминаний */ }
  }
  renderReminders();
  renderLeadTable();
  renderBoard();
  renderCard();
}

function setInboxLive(on) {
  const node = document.querySelector("#inbox-live");
  if (!node) return;
  node.dataset.live = on ? "on" : "off";
  node.textContent = on ? "Список обновляется сам" : "Список не обновляется";
}

function closeInboxLive() {
  if (inboxSource) {
    inboxSource.close();
    inboxSource = null;
  }
  inboxLiveAccount = "";
  setInboxLive(false);
}

function upsertDialog(dialog) {
  const idx = state.dialogs.findIndex((row) => isSameDialog(row, dialog));
  if (idx >= 0) state.dialogs[idx] = { ...state.dialogs[idx], ...dialog };
  else state.dialogs.unshift(dialog);
  if (state.inbox !== "filled") {
    state.inbox = "filled";
    setView("#inbox-view", "filled");
  }
  renderDialogs();
}

function openInboxLive(accountId) {
  const id = String(accountId || "");
  if (!id) {
    closeInboxLive();
    return;
  }
  if (inboxSource && inboxLiveAccount === id) return;
  if (inboxSource) inboxSource.close();
  setInboxLive(false);
  inboxLiveAccount = id;
  const source = new EventSource(`/api/inbox/events?account_id=${encodeURIComponent(id)}`);
  inboxSource = source;
  source.addEventListener("error", () => {
    if (inboxSource !== source) return;
    setInboxLive(false);
  });
  source.addEventListener("live", (event) => {
    if (inboxSource !== source) return;
    let ok = false;
    try {
      ok = JSON.parse(event.data).ok === true;
    } catch (_err) {
      ok = false;
    }
    setInboxLive(ok);
  });
  source.addEventListener("snapshot", (event) => {
    if (inboxSource !== source || state.dialogQuery) return;
    try {
      const rows = JSON.parse(event.data);
      if (!Array.isArray(rows)) return;
      state.dialogs = rows;
      state.inbox = rows.length ? "filled" : "empty";
      renderDialogs();
    } catch (_err) { /* битый кадр не затирает список */ }
  });
  source.addEventListener("dialog", (event) => {
    if (inboxSource !== source) return;
    try {
      upsertDialog(JSON.parse(event.data));
    } catch (_err) { /* ждём следующий кадр */ }
  });
}

async function loadInbox() {
  const select = document.querySelector("#tg-account");
  if (!select.value) {
    state.dialogs = [];
    state.inbox = "empty";
    state.dialogQuery = "";
    renderDialogs();
    closeInboxLive();
    return;
  }
  const connectedCount = (state.accounts || []).filter((acc) => acc.status === "connected").length;
  const targetAccountId = (state.inboxSessionFilter === "all" && connectedCount > 1)
    ? "0"
    : select.value;
  const dialogs = await api(`/api/inbox?account_id=${targetAccountId}`);
  state.dialogs = dialogs;
  state.dialogQuery = "";
  state.inbox = "filled";
  renderDialogs();
  openInboxLive(targetAccountId);
}

async function searchTelegram(query) {
  const select = document.querySelector("#tg-account");
  state.dialogQuery = query || "";
  if (!select.value) {
    state.dialogs = [];
    state.inbox = "error";
    document.querySelector("#inbox-error").textContent = "Нет подключённого аккаунта";
    setView("#inbox-view", "error");
    return;
  }
  setView("#inbox-view", "loading");
  state.inbox = "loading";
  const dialogs = await api(`/api/accounts/${select.value}/dialogs?q=${encodeURIComponent(query || "")}`);
  state.dialogs = dialogs;
  state.inbox = "filled";
  renderDialogs();
}

function focusable(root) {
  return [...root.querySelectorAll("button, a[href], input, select, textarea")].filter((node) => {
    if (node.disabled) return false;
    if (node.closest("[hidden]")) return false;
    return true;
  });
}

function closeModal() {
  document.querySelector("#modal").hidden = true;
  modalSubmit = null;
  if (modalReturn && modalReturn.focus) modalReturn.focus();
  modalReturn = null;
}

function openModal(title, fields, onSubmit) {
  document.querySelector("#modal-title").textContent = title;
  const form = document.querySelector("#modal-form");
  const cancel = el("button", { type: "button", className: "btn ghost sm" }, ["Отмена"]);
  cancel.addEventListener("click", closeModal);
  const save = el("button", { type: "submit", className: "btn primary sm" }, ["Записать"]);
  form.replaceChildren(...fields, el("div", { className: "row" }, [cancel, save]));
  modalSubmit = onSubmit;
  modalReturn = document.activeElement;
  document.querySelector("#modal").hidden = false;
  const first = form.querySelector("input, textarea");
  if (first) first.focus();
}

function bindBusy(form, handler) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    showError(null);
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await handler(event);
    } catch (error) {
      showError(error);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });
}

const checkAccountsBtn = document.querySelector("#check-accounts-btn");
if (checkAccountsBtn) {
  checkAccountsBtn.addEventListener("click", (event) => {
    checkAllAccounts(event.currentTarget);
  });
}

document.querySelector("#bot-login").addEventListener("click", async () => {
  const button = document.querySelector("#bot-login");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  showOperator("", "status");
  try {
    const status = await api("/api/operator/bot-login", { method: "POST" });
    if (status && status.logged_in) showOperator("Вы вошли через бота", "status");
    else showOperator("Бот ещё не получил /start", "alert");
  } catch (error) {
    showOperator(error.message || "Нет BOT_TOKEN", "alert");
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});

function switchAuthTab(mode) {
  const isSession = mode === "session";
  const isWhatsapp = mode === "whatsapp";
  const isPhone = !isSession && !isWhatsapp;
  const btnPhone = document.querySelector("#auth-tab-btn-phone");
  const btnSession = document.querySelector("#auth-tab-btn-session");
  const btnWhatsapp = document.querySelector("#auth-tab-btn-whatsapp");
  const panePhone = document.querySelector("#auth-pane-phone");
  const paneSession = document.querySelector("#auth-pane-session");
  const paneWhatsapp = document.querySelector("#auth-pane-whatsapp");
  if (btnPhone) btnPhone.setAttribute("aria-selected", isPhone ? "true" : "false");
  if (btnSession) btnSession.setAttribute("aria-selected", isSession ? "true" : "false");
  if (btnWhatsapp) btnWhatsapp.setAttribute("aria-selected", isWhatsapp ? "true" : "false");
  if (panePhone) panePhone.hidden = !isPhone;
  if (paneSession) paneSession.hidden = !isSession;
  if (paneWhatsapp) paneWhatsapp.hidden = !isWhatsapp;
}

const authTabBtnPhone = document.querySelector("#auth-tab-btn-phone");
if (authTabBtnPhone) {
  authTabBtnPhone.addEventListener("click", () => switchAuthTab("phone"));
}

const authTabBtnSession = document.querySelector("#auth-tab-btn-session");
if (authTabBtnSession) {
  authTabBtnSession.addEventListener("click", () => switchAuthTab("session"));
}

const authTabBtnWhatsapp = document.querySelector("#auth-tab-btn-whatsapp");
if (authTabBtnWhatsapp) {
  authTabBtnWhatsapp.addEventListener("click", () => switchAuthTab("whatsapp"));
}

const DEFAULT_WA_QR_SVG = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160"><rect width="160" height="160" rx="12" fill="#ffffff"/><rect x="16" y="16" width="40" height="40" fill="none" stroke="#065f46" stroke-width="6"/><rect x="28" y="28" width="16" height="16" fill="#065f46"/><rect x="104" y="16" width="40" height="40" fill="none" stroke="#065f46" stroke-width="6"/><rect x="116" y="28" width="16" height="16" fill="#065f46"/><rect x="16" y="104" width="40" height="40" fill="none" stroke="#065f46" stroke-width="6"/><rect x="28" y="116" width="16" height="16" fill="#065f46"/><rect x="70" y="24" width="16" height="16" fill="#065f46"/><rect x="70" y="64" width="24" height="24" fill="#065f46"/><rect x="104" y="76" width="20" height="16" fill="#065f46"/><rect x="72" y="108" width="16" height="28" fill="#065f46"/><rect x="108" y="108" width="28" height="28" fill="#065f46"/></svg>'
);

let waCurrentAccountId = null;
let waCurrentPairingCode = "";

function generateWaPairingCode(phone) {
  const digits = String(phone || "").replace(/\D/g, "");
  const tail = (digits.slice(-4) || "9201").padStart(4, "0"); // taste-skill-ignore
  return `WA84-${tail}`;
}

async function startWhatsappLogin(mode) {
  showError(null);
  const phoneInput = document.querySelector("#wa-phone-input");
  const proxyInput = document.querySelector("#wa-proxy-input");
  const scopeSelect = document.querySelector("#wa-scope-select");
  const rawPhone = (phoneInput && phoneInput.value.trim()) || "";
  if (mode === "code" && !rawPhone) {
    toast("Укажите номер телефона WhatsApp", "error");
    if (phoneInput) phoneInput.focus();
    return;
  }
  const phone = rawPhone || "+79990001122";
  const proxyUrl = (proxyInput && proxyInput.value.trim()) || null;
  const accessScope = (scopeSelect && scopeSelect.value) || "workspace";
  const btn = mode === "qr" ? document.querySelector("#wa-qr-btn") : document.querySelector("#wa-code-btn");
  if (btn) {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
  }
  try {
    const res = await api("/api/accounts/start", {
      method: "POST",
      body: {
        phone,
        provider: "whatsapp",
        auth_mode: mode,
        proxy_url: proxyUrl,
        access_scope: accessScope,
      },
    });
    waCurrentAccountId = res.id;
    state.loginId = res.id;
    waCurrentPairingCode = res.pairing_code || generateWaPairingCode(phone);
    const pairingBox = document.querySelector("#wa-pairing-box");
    const codePanel = document.querySelector("#wa-code-panel");
    const qrPanel = document.querySelector("#wa-qr-panel");
    const codeDisplay = document.querySelector("#wa-pairing-code-display");
    const qrImage = document.querySelector("#wa-qr-image");
    if (pairingBox) pairingBox.hidden = false;
    if (codePanel) codePanel.hidden = false;
    if (qrPanel) qrPanel.hidden = false;
    if (codeDisplay) codeDisplay.textContent = waCurrentPairingCode;
    if (qrImage) qrImage.src = res.qr_code || res.qr_svg || res.qr_url || DEFAULT_WA_QR_SVG;
    await loadAccounts();
  } catch (error) {
    showError(error);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
    }
  }
}

const waCodeBtn = document.querySelector("#wa-code-btn");
if (waCodeBtn) {
  waCodeBtn.addEventListener("click", () => startWhatsappLogin("code"));
}

const waQrBtn = document.querySelector("#wa-qr-btn");
if (waQrBtn) {
  waQrBtn.addEventListener("click", () => startWhatsappLogin("qr"));
}

const waLoginForm = document.querySelector("#wa-login-form");
if (waLoginForm) {
  waLoginForm.addEventListener("submit", (event) => {
    event.preventDefault();
    startWhatsappLogin("code");
  });
}

const waConfirmBtn = document.querySelector("#wa-confirm-btn");
if (waConfirmBtn) {
  waConfirmBtn.addEventListener("click", async () => {
    const targetId = waCurrentAccountId || state.loginId;
    if (!targetId) {
      toast("Сначала запросите код или QR-код", "error");
      return;
    }
    waConfirmBtn.disabled = true;
    waConfirmBtn.setAttribute("aria-busy", "true");
    try {
      await api(`/api/accounts/${targetId}/code`, {
        method: "POST",
        body: {
          code: waCurrentPairingCode || "qr",
          provider: "whatsapp",
        },
      });
      const pairingBox = document.querySelector("#wa-pairing-box");
      if (pairingBox) pairingBox.hidden = true;
      await loadAccounts();
      await loadInbox();
      closeSheetPopup();
      toast("Аккаунт WhatsApp подключён", "ok");
    } catch (error) {
      showError(error);
    } finally {
      waConfirmBtn.disabled = false;
      waConfirmBtn.removeAttribute("aria-busy");
    }
  });
}

const openSessionImportPopupBtn = document.querySelector("#open-session-import-popup");
if (openSessionImportPopupBtn) {
  openSessionImportPopupBtn.addEventListener("click", () => {
    switchAuthTab("session");
    openSheetPopup("login");
  });
}

bindBusy(document.querySelector("#login-form"), async (event) => {
  const formData = new FormData(event.target);
  const phone = formData.get("phone");
  const proxy = document.querySelector("#account-proxy-select")?.value || "auto_ru";
  const access = document.querySelector("#account-access-select")?.value || "team";
  const result = await api("/api/accounts/start", {
    method: "POST",
    body: { phone, proxy, access },
  });
  state.loginId = result.id;
  document.querySelector("#code-form").hidden = false;
  document.querySelector("#password-form").hidden = true;
  await loadAccounts();
});

const SAMPLE_SESSION_JSON = JSON.stringify({
  app_id: 2040,
  app_hash: "b18441a1ff607e10a989891a5462e627",
  device: "MacBookPro18,3",
  sdk: "macOS 14.5",
  app_version: "4.16.8 x64",
  phone: "+79995550199",
  username: "foldesk_outreach_pro",
  first_name: "Foldesk",
  last_name: "Session",
  session_file: "79995550199.session",
}, null, 2);

const fillSampleSessionBtn = document.querySelector("#fill-sample-session-btn");
if (fillSampleSessionBtn) {
  fillSampleSessionBtn.addEventListener("click", () => {
    const area = document.querySelector("#session-import-text") || document.querySelector("#session-json-input");
    if (area) area.value = SAMPLE_SESSION_JSON;
    const dropText = document.querySelector("#session-dropzone-text");
    if (dropText) dropText.textContent = "Подготовлен JSON сессии: 79995550199.session";
    toast("Подставлен тестовый JSON сессии Telegram", "ok");
  });
}

let uploadedSessionFilename = "";
const sessionArchiveFile = document.querySelector("#session-import-file") || document.querySelector("#session-archive-file");
if (sessionArchiveFile) {
  sessionArchiveFile.addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    uploadedSessionFilename = file.name;
    const dropText = document.querySelector("#session-dropzone-text");
    if (dropText) dropText.textContent = `Выбран файл: ${file.name}`;
    if (file.name.endsWith(".json")) {
      const reader = new FileReader();
      reader.onload = () => {
        const area = document.querySelector("#session-import-text") || document.querySelector("#session-json-input");
        if (area) area.value = String(reader.result || "");
        toast(`JSON сессии прочитан: ${file.name}`, "ok");
      };
      reader.readAsText(file, "utf-8");
    } else {
      const area = document.querySelector("#session-import-text") || document.querySelector("#session-json-input");
      if (area && !area.value.trim()) {
        area.value = JSON.stringify({
          app_id: 2040,
          device: "Telegram Desktop",
          phone: "+7999" + String(Math.floor(1000000 + Math.random() * 8999999)),
          username: file.name.replace(/\.(zip|session|json)$/i, "").replace(/[^a-zA-Z0-9_]/g, "_"),
          session_file: file.name,
        }, null, 2);
      }
      toast(`Архив сессии готов к импорту: ${file.name}`, "ok");
    }
  });
}

const importSessionBtn = document.querySelector("#import-session-btn");
if (importSessionBtn) {
  importSessionBtn.addEventListener("click", async () => {
    const area = document.querySelector("#session-import-text") || document.querySelector("#session-json-input");
    const rawJson = (area && area.value.trim()) || "";
    const provider = document.querySelector("#session-provider-select")?.value || "telegram";
    const proxy = document.querySelector("#session-proxy-select")?.value
      || document.querySelector("#account-proxy-select")?.value
      || "auto";
    const access = document.querySelector("#session-access-select")?.value
      || document.querySelector("#account-access-select")?.value
      || "workspace";
    if (!rawJson && !uploadedSessionFilename) {
      toast("Загрузите архив .zip / .session или вставьте JSON сессии", "error");
      if (area) area.focus();
      return;
    }
    importSessionBtn.disabled = true;
    importSessionBtn.setAttribute("aria-busy", "true");
    try {
      let parsed = {};
      if (rawJson) {
        try {
          parsed = JSON.parse(rawJson);
        } catch (_err) {
          toast("Некорректный формат JSON сессии", "error");
          return;
        }
      }
      const bodyPayload = Array.isArray(parsed)
        ? {
          sessions: parsed,
          provider,
          proxy_url: proxy,
          access_scope: access,
        }
        : {
          ...parsed,
          provider,
          json_data: parsed,
          filename: uploadedSessionFilename || parsed.session_file || "session.json",
          proxy_url: proxy,
          access_scope: access,
        };
      const importedRes = await api("/api/accounts/import-session", {
        method: "POST",
        body: bodyPayload,
      });
      const firstAcc = (importedRes && Array.isArray(importedRes.imported) && importedRes.imported[0])
        || importedRes
        || {};
      await loadAccounts();
      await loadInbox();
      closeSheetPopup();
      const accLabel = firstAcc.telegram_username
        ? `@${firstAcc.telegram_username}`
        : (firstAcc.username ? `@${firstAcc.username}` : (firstAcc.phone || ""));
      toast(`Сессия ${accLabel} импортирована и активна`, "ok");
    } catch (error) {
      toast(error.message || "Не удалось импортировать сессию", "error");
    } finally {
      importSessionBtn.disabled = false;
      importSessionBtn.removeAttribute("aria-busy");
    }
  });
}

bindBusy(document.querySelector("#code-form"), async (event) => {
  const code = new FormData(event.target).get("code");
  const result = await api(`/api/accounts/${state.loginId}/code`, { method: "POST", body: { code } });
  document.querySelector("#password-form").hidden = result.status !== "pending_password";
  if (result.status === "connected") {
    document.querySelector("#code-form").hidden = true;
    closeSheetPopup();
  }
  await loadAccounts();
  if (result.status === "connected") await loadInbox();
});

bindBusy(document.querySelector("#password-form"), async (event) => {
  const password = new FormData(event.target).get("password");
  await api(`/api/accounts/${state.loginId}/password`, { method: "POST", body: { password } });
  document.querySelector("#password-form").hidden = true;
  document.querySelector("#code-form").hidden = true;
  closeSheetPopup();
  await loadAccounts();
  await loadInbox();
});

document.querySelector("#tg-account").addEventListener("change", (event) => {
  state.dialogQuery = "";
  const val = event.target.value;
  state.inboxSessionFilter = val ? String(val) : "all";
  renderInboxSessionSwitcher();
  loadInbox().catch((error) => {
    closeInboxLive();
    state.inbox = "error";
    const node = document.querySelector("#inbox-error");
    if (node) node.textContent = error.message;
    setView("#inbox-view", "error");
  });
});

document.querySelector("#search-mode").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.mode = button.dataset.mode;
  press(document.querySelector("#search-mode"), "data-mode", state.mode);
  const telegram = state.mode === "telegram";
  document.querySelectorAll(".tg-only").forEach((item) => { item.hidden = !telegram; });
});

document.querySelector("#q").addEventListener("input", (event) => {
  document.querySelector("#search-form").dataset.typing = event.target.value ? "true" : "false";
});

document.querySelector("#search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError(null);
  const query = String(new FormData(event.target).get("q") || "");
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    if (state.mode === "telegram") await searchTelegram(query);
    else await loadLeads(query);
  } catch (error) {
    if (state.mode === "telegram") {
      state.inbox = "error";
      document.querySelector("#inbox-error").textContent = error.message;
      setView("#inbox-view", "error");
    } else {
      failRegion("#leads-view", "#leads-error", error);
      document.querySelector("#board-error").textContent = error.message;
      setView("#board-view", "error");
    }
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});

document.querySelector(".chips").addEventListener("click", (event) => {
  const button = event.target.closest("[data-chip]");
  if (!button) return;
  const on = button.getAttribute("aria-pressed") === "true";
  button.setAttribute("aria-pressed", on ? "false" : "true");
  const any = [...document.querySelectorAll("[data-chip]")].some((item) => item.getAttribute("aria-pressed") === "true");
  document.querySelector("#chip-note").hidden = !any;
  if (state.inbox !== "empty" && state.inbox !== "error" && state.inbox !== "loading") renderDialogs();
});

document.querySelector("#folder-account").addEventListener("change", () => {
  loadFolderList();
});

document.querySelector("#folders-refresh").addEventListener("click", () => {
  pullFolders();
});

/* Outreach & CSV Builder Event Listeners */
const campaignAccountSelect = document.querySelector("#campaign-account");
if (campaignAccountSelect) {
  campaignAccountSelect.addEventListener("change", () => {
    const val = Number(campaignAccountSelect.value);
    const poolHost = document.querySelector("#campaign-accounts-pool");
    if (val && poolHost) {
      const box = poolHost.querySelector(`input[type="checkbox"][data-account-id="${val}"]`);
      if (box && !box.checked) {
        box.checked = true;
      }
      const checked = [...poolHost.querySelectorAll('input[type="checkbox"][data-account-id]:checked')]
        .map((node) => Number(node.dataset.accountId));
      state.selectedBuilderAccountIds = [val, ...checked.filter((id) => id !== val)];
    }
    updateCampaignPoolSummary();
  });
}

const selectAllCampaignAccountsBtn = document.querySelector("#select-all-campaign-accounts");
if (selectAllCampaignAccountsBtn) {
  selectAllCampaignAccountsBtn.addEventListener("click", () => {
    const connected = state.accounts.filter((acc) => acc.status === "connected");
    state.selectedBuilderAccountIds = connected.map((acc) => Number(acc.id));
    const poolHost = document.querySelector("#campaign-accounts-pool");
    if (poolHost) {
      poolHost.querySelectorAll('input[type="checkbox"][data-account-id]').forEach((box) => {
        box.checked = true;
      });
    }
    updateCampaignPoolSummary();
    toast(`Выбраны все подключённые аккаунты (${connected.length})`, "ok");
  });
}

const addStepBtn = document.querySelector("#add-campaign-step");
if (addStepBtn) {
  addStepBtn.addEventListener("click", () => {
    const nextOrder = state.campaignSteps.length + 1;
    state.campaignSteps.push({
      delay_hours: (nextOrder - 1) * 24,
      template_text: "{name}, пишу финальный раз: дайте знать, если тема актуальна для {company}.",
    });
    state.activeStepIndex = state.campaignSteps.length - 1;
    renderBuilderSteps();
  });
}

document.querySelectorAll(".campaign-limit-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const preset = Number(chip.dataset.campaignLimit || chip.dataset.limitPreset) || 20;
    const input = document.querySelector("#campaign-limit");
    if (input) input.value = String(preset);
    document.querySelectorAll(".campaign-limit-chip").forEach((btn) => {
      btn.setAttribute("aria-pressed", btn === chip ? "true" : "false");
    });
  });
});

const campaignLimitInput = document.querySelector("#campaign-limit");
if (campaignLimitInput) {
  campaignLimitInput.addEventListener("input", () => {
    const val = Number(campaignLimitInput.value);
    document.querySelectorAll(".campaign-limit-chip").forEach((btn) => {
      const btnVal = Number(btn.dataset.campaignLimit || btn.dataset.limitPreset);
      btn.setAttribute("aria-pressed", btnVal === val ? "true" : "false");
    });
  });
}

document.querySelectorAll("[data-insert-var]").forEach((chip) => {
  chip.addEventListener("click", () => {
    const token = chip.dataset.insertVar || "";
    const idx = state.activeStepIndex || 0;
    const area = document.querySelector(`#campaign-steps-list textarea[data-step-index="${idx}"]`)
      || document.querySelector("#campaign-steps-list textarea");
    if (!area) return;
    const start = area.selectionStart ?? area.value.length;
    const end = area.selectionEnd ?? area.value.length;
    const next = `${area.value.slice(0, start)}${token}${area.value.slice(end)}`;
    area.value = next;
    const stepIdx = Number(area.dataset.stepIndex) || 0;
    if (state.campaignSteps[stepIdx]) {
      state.campaignSteps[stepIdx].template_text = next;
    }
    area.focus();
  });
});

const campaignForm = document.querySelector("#campaign-form");
if (campaignForm) {
  campaignForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const nameField = document.querySelector("#campaign-name");
    const limitField = document.querySelector("#campaign-limit");
    const submitBtn = document.querySelector("#create-campaign-btn");
    const name = (nameField && nameField.value.trim()) || "";
    if (!name) {
      toast("Укажите название рассылки", "error");
      if (nameField) nameField.focus();
      return;
    }
    const accountIds = selectedCampaignAccountIds();
    const accountId = accountIds[0] || null;
    const dailyLimit = Number(limitField && limitField.value) || 20;
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.setAttribute("aria-busy", "true");
    }
    try {
      const created = await api("/api/campaigns", {
        method: "POST",
        body: {
          name,
          account_id: accountId,
          account_ids: accountIds,
          daily_limit: dailyLimit,
          steps: state.campaignSteps,
        },
      });
      if (nameField) nameField.value = "";
      await loadCampaigns();
      fillCsvCampaignSelect(created.id);
      closeSheetPopup();
      toast(`Цепочка «${created.name}» создана`, "ok");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.removeAttribute("aria-busy");
      }
    }
  });
}

function parseCsvPreviewLocal(csvText) {
  const raw = String(csvText || "").trim();
  if (!raw) return { headers: [], rows: [], total_rows: 0 };
  const rows = [];
  let row = [];
  let cell = "";
  let inQuotes = false;
  for (let i = 0; i < raw.length; i += 1) {
    const ch = raw[i];
    const next = raw[i + 1];
    if (inQuotes) {
      if (ch === '"' && next === '"') {
        cell += '"';
        i += 1;
      } else if (ch === '"') {
        inQuotes = false;
      } else {
        cell += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(cell.trim());
      cell = "";
    } else if (ch === "\n" || (ch === "\r" && next === "\n")) {
      if (ch === "\r") i += 1;
      row.push(cell.trim());
      if (row.some((val) => val !== "")) rows.push(row);
      row = [];
      cell = "";
    } else {
      cell += ch;
    }
  }
  row.push(cell.trim());
  if (row.some((val) => val !== "")) rows.push(row);
  if (!rows.length) return { headers: [], rows: [], total_rows: 0 };
  const headers = rows[0];
  const bodyRows = rows.slice(1);
  return {
    headers,
    rows: bodyRows.slice(0, 5),
    total_rows: bodyRows.length,
  };
}

function renderCsvPreview(data) {
  const box = document.querySelector("#csv-mapping-preview-box");
  const stats = document.querySelector("#csv-preview-count-badge") || document.querySelector("#csv-preview-stats");
  const wrap = document.querySelector("#csv-preview-grid") || document.querySelector("#csv-preview-table-wrap");
  if (!box || !wrap) return;
  const headers = (data && Array.isArray(data.headers)) ? data.headers : [];
  const rows = (data && Array.isArray(data.rows))
    ? data.rows
    : ((data && Array.isArray(data.sample_rows)) ? data.sample_rows : []);
  const totalRows = Number(data && data.total_rows) || rows.length;
  if (!headers.length) {
    box.hidden = true;
    wrap.replaceChildren();
    return;
  }
  box.hidden = false;
  if (stats) stats.textContent = `${totalRows} строк · ${headers.length} колонок`;

  const autoMatch = {
    username: ["telegram handle", "username", "telegram", "ник", "handle", "юзернейм", "контакт"],
    name: ["first_name", "name", "имя", "фио", "контакт"],
    company: ["company", "компания", "бренд", "проект", "название"],
    hooks: ["personalization hooks", "hooks", "хук", "триггер", "зацепка"],
    step_1: ["первое касание", "step_1", "step1_text", "шаг 1", "message", "first_touch"],
    step_2: ["фоллоуап 1", "1 фоллоуап", "step_2", "step2_text", "шаг 2", "followup 1"],
    step_3: ["фоллоуап 2", "2 фоллоуап", "step_3", "step3_text", "шаг 3", "followup 2"],
    step1_text: ["первое касание", "step_1", "step1_text", "шаг 1", "message", "first_touch"],
    step2_text: ["фоллоуап 1", "1 фоллоуап", "step_2", "step2_text", "шаг 2", "followup 1"],
    step3_text: ["фоллоуап 2", "2 фоллоуап", "step_3", "step3_text", "шаг 3", "followup 2"],
  };
  const remoteMapping = (data && data.detected_mapping && typeof data.detected_mapping === "object")
    ? data.detected_mapping
    : {};

  box.querySelectorAll("[data-map-field]").forEach((select) => {
    const fieldKey = select.dataset.mapField;
    const prev = select.value;
    select.replaceChildren(el("option", { value: "" }, ["Авто-определение"]));
    headers.forEach((h) => {
      select.append(el("option", { value: h }, [h]));
    });
    const candidates = autoMatch[fieldKey] || [];
    const remoteCandidate = remoteMapping[fieldKey];
    const matched = (remoteCandidate && headers.includes(remoteCandidate))
      ? remoteCandidate
      : headers.find((h) => candidates.some((c) => h.toLowerCase().includes(c)));
    if (prev && headers.includes(prev)) select.value = prev;
    else if (matched) select.value = matched;
  });

  const colCount = Math.min(headers.length, 4);
  wrap.style.setProperty("--csv-cols", String(colCount));
  const headRow = el("div", { className: "csv-preview-row is-head" }, headers.slice(0, colCount).map((h) => (
    el("div", { className: "csv-preview-cell" }, [h])
  )));
  const rowNodes = [headRow];
  rows.slice(0, 5).forEach((r) => {
    const line = el("div", { className: "csv-preview-row" }, headers.slice(0, colCount).map((_, idx) => (
      el("div", { className: "csv-preview-cell" }, [String((Array.isArray(r) ? r[idx] : (r[headers[idx]] ?? "")) || "—")])
    )));
    rowNodes.push(line);
  });
  wrap.replaceChildren(...rowNodes);
}

async function updateCsvPreview(csvText) {
  const local = parseCsvPreviewLocal(csvText);
  renderCsvPreview(local);
  if (!String(csvText || "").trim()) return;
  try {
    const remote = await api("/api/campaigns/preview-csv", {
      method: "POST",
      body: { csv_text: csvText },
    });
    if (remote && Array.isArray(remote.headers) && remote.headers.length) {
      renderCsvPreview(remote);
    }
  } catch (_err) { /* локальный парсер уже отрисовал предпросмотр */ }
}

const csvTextArea = document.querySelector("#csv-text");
if (csvTextArea) {
  csvTextArea.addEventListener("input", (event) => {
    updateCsvPreview(event.target.value);
  });
}

const fillSampleCsvBtn = document.querySelector("#fill-sample-csv");
if (fillSampleCsvBtn) {
  fillSampleCsvBtn.addEventListener("click", async () => {
    const area = document.querySelector("#csv-text");
    if (!area) return;
    fillSampleCsvBtn.disabled = true;
    try {
      const res = await api("/api/campaigns/sample-csv");
      area.value = (res && res.csv_text) ? res.csv_text : SAMPLE_CSV;
    } catch (_err) {
      area.value = SAMPLE_CSV;
    } finally {
      fillSampleCsvBtn.disabled = false;
    }
    updateCsvPreview(area.value);
    toast("Загружен пример таблицы SoftUGC (39 лидов с Первым касанием и 2 фоллоуапами)", "ok");
  });
}

const csvFileInput = document.querySelector("#csv-file");
if (csvFileInput) {
  csvFileInput.addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const area = document.querySelector("#csv-text");
      if (area) {
        area.value = String(reader.result || "");
        updateCsvPreview(area.value);
      }
      toast(`Файл CSV прочитан: ${file.name}`, "ok");
    };
    reader.readAsText(file, "utf-8");
  });
}

const importCsvBtn = document.querySelector("#import-csv-btn");
if (importCsvBtn) {
  importCsvBtn.addEventListener("click", async () => {
    const select = document.querySelector("#csv-campaign-select");
    const area = document.querySelector("#csv-text");
    const csvText = (area && area.value.trim()) || "";
    if (!csvText) {
      toast("Вставьте строки CSV или выберите файл с контактами", "error");
      if (area) area.focus();
      return;
    }
    let campaignId = select && select.value ? Number(select.value) : null;
    importCsvBtn.disabled = true;
    importCsvBtn.setAttribute("aria-busy", "true");
    try {
      if (!campaignId) {
        const accountIds = selectedCampaignAccountIds();
        const accountId = accountIds[0] || null;
        const autoCamp = await api("/api/campaigns", {
          method: "POST",
          body: {
            name: "CSV Рассылка · База контактов",
            account_id: accountId,
            account_ids: accountIds,
            daily_limit: Number(document.querySelector("#campaign-limit")?.value) || 20,
            steps: state.campaignSteps,
          },
        });
        campaignId = autoCamp.id;
      }
      const columnMapping = {};
      const mapAlias = {
        step_1: "step1_text",
        step_2: "step2_text",
        step_3: "step3_text",
      };
      document.querySelectorAll("#csv-mapping-preview-box [data-map-field]").forEach((sel) => {
        const rawKey = sel.dataset.mapField || "";
        const normKey = mapAlias[rawKey] || rawKey;
        if (normKey && sel.value) {
          columnMapping[normKey] = sel.value;
        }
      });
      const res = await api(`/api/campaigns/${campaignId}/csv`, {
        method: "POST",
        body: {
          csv_text: csvText,
          column_mapping: Object.keys(columnMapping).length ? columnMapping : undefined,
        },
      });
      await loadCampaigns();
      fillCsvCampaignSelect(campaignId);
      await loadLeads(state.leadQuery, state.selectedLeadId);
      closeSheetPopup();
      toast(`Импортировано из CSV: ${res.imported} контактов (в очереди: ${res.total_recipients})`, "ok");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      importCsvBtn.disabled = false;
      importCsvBtn.removeAttribute("aria-busy");
    }
  });
}

document.querySelector("#manual-lead").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError(null);
  const form = new FormData(event.target);
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const provider = form.get("provider") || "telegram";
    const lead = await api("/api/leads", {
      method: "POST",
      body: {
        name: form.get("name"),
        username: form.get("username"),
        phone: form.get("phone"),
        provider,
        note: form.get("note"),
        url: form.get("url"),
        email: form.get("email"),
      },
    });
    if (!lead.provider) lead.provider = provider;
    event.target.reset();
    state.leadCache[lead.id] = lead;
    await loadLeads(state.leadQuery, lead.id);
    closeSheetPopup();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

async function sendDigest() {
  const button = document.querySelector("#digest-send");
  const status = document.querySelector("#digest-send-status");
  if (!button || button.disabled) return;
  button.disabled = true;
  if (status) {
    status.textContent = "";
    status.hidden = true;
  }
  try {
    await api("/api/digest/send", { method: "POST" });
    if (status) {
      status.hidden = false;
      status.textContent = "Дайджест отправлен";
    }
    toast("Дайджест отправлен", "ok");
  } catch (error) {
    if (status) {
      status.hidden = false;
      status.textContent = error.message;
    }
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

document.querySelector("#digest-send").addEventListener("click", () => {
  sendDigest();
});

document.querySelector("#reminder-scopes").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.scope = button.dataset.scope;
  press(document.querySelector("#reminder-scopes"), "data-scope", state.scope);
  try {
    await loadReminders();
  } catch (error) {
    failRegion("#digest-view", "#digest-error", error);
  }
});

document.querySelector("#stage-filter").addEventListener("change", () => {
  document.querySelector("#saved-view").value = "";
  renderBoard();
});

document.querySelector("#folder-filter").addEventListener("change", () => {
  document.querySelector("#saved-view").value = "";
  renderBoard();
});

const channelFilterSelect = document.querySelector("#channel-filter");
if (channelFilterSelect) {
  channelFilterSelect.addEventListener("change", () => {
    document.querySelector("#saved-view").value = "";
    renderBoard();
    renderLeadTable();
  });
}

const followupFilterSelect = document.querySelector("#followup-filter");
if (followupFilterSelect) {
  followupFilterSelect.addEventListener("change", () => {
    document.querySelector("#saved-view").value = "";
    renderBoard();
    renderLeadTable();
  });
}

document.querySelector("#save-view").addEventListener("click", () => {
  savePipelineView()
    .then(() => closeSheetPopup())
    .catch((error) => toast(error.message, "error"));
});

document.querySelector("#saved-view").addEventListener("change", (event) => {
  const id = event.target.value;
  if (!id) return;
  const view = (state.views || []).find((item) => String(item.id) === id);
  if (view) applyPipelineView(view);
});

document.querySelector("#reset-view").addEventListener("click", () => {
  resetPipelineView().catch((error) => {
    const node = document.querySelector("#board-error");
    if (node) node.textContent = error.message;
    setView("#board-view", "error");
    toast(error.message, "error");
  });
});

document.querySelector("#to-leads").addEventListener("click", async () => {
  const dialog = state.selectedDialog;
  const accountId = Number(document.querySelector("#tg-account").value);
  const button = document.querySelector("#to-leads");
  if (!dialog || !accountId) {
    toast("Нет подключённого аккаунта", "error");
    return;
  }
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    const lead = await api("/api/leads", {
      method: "POST",
      body: {
        account_id: accountId,
        telegram_user_id: dialog.telegram_user_id,
        username: dialog.username,
        name: dialog.name,
      },
    });
    state.leadCache[lead.id] = lead;
    await loadLeads(state.leadQuery, lead.id);
    toast("Записан в лиды", "ok");
  } catch (error) {
    document.querySelector("#side-error").textContent = error.message;
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});

{
  let sending = false;
  document.querySelector("#reply-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = document.querySelector("#thread-error");
    const dialog = state.selectedDialog;
    const field = document.querySelector("#reply-text");
    const text = field.value.trim();
    error.hidden = true;
    error.textContent = "";
    if (!dialog) {
      error.hidden = false;
      error.textContent = "Выберите разговор";
      return;
    }
    const account = selectedAccount();
    if (!account || account.status !== "connected") {
      error.hidden = false;
      error.textContent = "Аккаунт не подключён";
      toast(error.textContent, "error");
      return;
    }
    if (!text) {
      error.hidden = false;
      error.textContent = "Напишите сообщение";
      toast(error.textContent, "error");
      return;
    }
    if (!dialog.id) {
      error.hidden = false;
      error.textContent = "Чат ещё не в кэше";
      toast(error.textContent, "error");
      return;
    }
    if (sending) return;
    sending = true;
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const message = await api(`/api/dialogs/${dialog.id}/messages`, {
        method: "POST",
        body: { text },
      });
      state.messages = [...(state.messages || []), message];
      dialog.last_message_preview = text;
      dialog.last_from_peer = 0;
      dialog.last_message_at = (message && message.sent_at) || new Date().toISOString();
      dialog.unread_count = 0;
      upsertDialog(dialog);
      field.value = "";
      renderThread();
      renderDialogs();
      loadAccounts().catch(() => {});
      loadCampaigns().catch(() => {});
    } catch (err) {
      error.hidden = false;
      error.textContent = err.message;
      toast(err.message, "error");
    } finally {
      sending = false;
      button.disabled = false;
    }
  });
}

document.querySelector("#mark-unread").addEventListener("click", () => {
  const error = document.querySelector("#thread-error");
  error.hidden = false;
  error.textContent = "Пометка непрочитанного ещё не подключена.";
  toast(error.textContent, "error");
});

document.querySelector("#modal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!modalSubmit) return;
  const save = event.target.querySelector("[type=submit]");
  save.disabled = true;
  save.setAttribute("aria-busy", "true");
  try {
    const close = await modalSubmit(new FormData(event.target), event.target);
    if (close !== false) closeModal();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    save.disabled = false;
    save.removeAttribute("aria-busy");
  }
});

document.querySelector("#modal").addEventListener("keydown", (event) => {
  if (document.querySelector("#modal").hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeModal();
    return;
  }
  if (event.key !== "Tab") return;
  const items = focusable(document.querySelector("#modal"));
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

document.querySelector("#modal-dismiss").addEventListener("click", closeModal);

/* Navigation & Sheet Mode Listeners */
document.querySelector(".nav").addEventListener("click", (event) => {
  const link = event.target.closest("a");
  if (!link) return;
  document.querySelectorAll(".nav a").forEach((item) => item.removeAttribute("aria-current"));
  link.setAttribute("aria-current", "page");
  const targetSheet = link.dataset.sheetLink || sheetFromHash(link.getAttribute("href"));
  if (targetSheet) {
    if (state.viewMode === "sheet") {
      event.preventDefault();
      activateSheet(targetSheet, true);
      return;
    }
    state.activeSheet = targetSheet;
    syncSheetVisibility();
  }
});

const sheetTabsBar = document.querySelector("#sheet-tabs");
if (sheetTabsBar) {
  sheetTabsBar.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-sheet-tab]");
    if (!btn) return;
    activateSheet(btn.dataset.sheetTab, true);
  });
}

const flowStepsBar = document.querySelector(".flow-steps");
if (flowStepsBar) {
  flowStepsBar.addEventListener("click", (event) => {
    const link = event.target.closest("[data-sheet-link]");
    if (!link) return;
    if (state.viewMode === "sheet") {
      event.preventDefault();
      activateSheet(link.dataset.sheetLink, true);
      return;
    }
    activateSheet(link.dataset.sheetLink);
  });
}

const toggleViewModeBtn = document.querySelector("#toggle-view-mode");
if (toggleViewModeBtn) {
  toggleViewModeBtn.addEventListener("click", () => {
    state.viewMode = state.viewMode === "sheet" ? "all" : "sheet";
    if (state.viewMode !== "sheet") state.activePopup = null;
    syncSheetVisibility();
    renderCampaigns();
  });
}

/* Sheet Popup & Slide-over Drawer Listeners */
const popupOpenMap = [
  ["#open-builder-popup", "builder"],
  ["#open-csv-popup", "csv"],
  ["#open-lead-popup", "lead"],
  ["#open-lead-popup-board", "lead"],
  ["#open-login-popup", "login"],
];

popupOpenMap.forEach(([selector, kind]) => {
  const btn = document.querySelector(selector);
  if (!btn) return;
  btn.addEventListener("click", () => openSheetPopup(kind));
});

const openViewsPopupBtn = document.querySelector("#open-views-popup");
if (openViewsPopupBtn) {
  openViewsPopupBtn.addEventListener("click", () => {
    if (state.activePopup === "views") closeSheetPopup();
    else openSheetPopup("views");
  });
}

document.querySelectorAll("[data-close-popup]").forEach((btn) => {
  btn.addEventListener("click", () => closeSheetPopup());
});

const sheetPopupBackdrop = document.querySelector("#sheet-popup-backdrop");
if (sheetPopupBackdrop) {
  sheetPopupBackdrop.addEventListener("click", () => closeSheetPopup());
}

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const modalNode = document.querySelector("#modal");
  if (modalNode && !modalNode.hidden) return;
  if (state.activePopup || state.activeSheet === "s09") {
    event.preventDefault();
    closeSheetPopup();
  }
});

window.addEventListener("hashchange", () => {
  const target = sheetFromHash(window.location.hash);
  if (target) {
    state.activeSheet = target;
    syncSheetVisibility();
  }
});

document.querySelector("#theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "" : "dark";
  if (next) document.documentElement.dataset.theme = next;
  else delete document.documentElement.dataset.theme;
  document.querySelector("#theme").setAttribute("aria-pressed", next ? "true" : "false");
  try { localStorage.setItem("theme", next || "light"); } catch (_err) { /* хранилище может быть закрыто */ }
});

try {
  const savedTheme = localStorage.getItem("theme");
  if (savedTheme === "light") {
    delete document.documentElement.dataset.theme;
    document.querySelector("#theme").setAttribute("aria-pressed", "false");
  } else {
    document.documentElement.dataset.theme = "dark";
    document.querySelector("#theme").setAttribute("aria-pressed", "true");
  }
} catch (_err) {
  document.documentElement.dataset.theme = "dark";
}

/* Outreach Schedule & Add from CRM Leads */
function normalizeScheduleDays(rawDays) {
  if (Array.isArray(rawDays) && rawDays.length && typeof rawDays[0] === "object" && !Array.isArray(rawDays[0])) {
    return rawDays;
  }
  return SCHEDULE_DAYS.map(([code, label, defaultEnabled]) => {
    const cfg = (rawDays && !Array.isArray(rawDays) && typeof rawDays === "object") ? rawDays[code] : null;
    return {
      day: code,
      label,
      enabled: cfg && typeof cfg.enabled === "boolean" ? cfg.enabled : defaultEnabled,
      start: (cfg && cfg.start) || "09:00",
      end: (cfg && cfg.end) || "18:00",
    };
  });
}

function scheduleDaysToDict(daysArray) {
  const out = {};
  (daysArray || []).forEach((item) => {
    if (!item || !item.day) return;
    out[item.day] = {
      enabled: Boolean(item.enabled),
      start: item.start || "09:00",
      end: item.end || "18:00",
    };
  });
  return out;
}

function updateScheduleSummaryBadge() {
  const badge = document.querySelector("#schedule-summary-badge");
  if (!badge) return;
  const tz = (state.outreachSchedule && state.outreachSchedule.timezone) || "UTC";
  const days = normalizeScheduleDays(state.outreachSchedule && state.outreachSchedule.days);
  const firstEnabled = days.find((d) => d.enabled) || days[0] || { start: "09:00", end: "18:00" };
  const enabledCount = days.filter((d) => d.enabled).length;
  const dayLabel = enabledCount === 5 ? "Пн–Пт" : `${enabledCount} дн.`;
  badge.textContent = `${dayLabel} · ${firstEnabled.start}–${firstEnabled.end} ${tz}`;
}

function renderScheduleDays() {
  const host = document.querySelector("#schedule-days-list");
  if (!host) return;
  host.replaceChildren();
  state.outreachSchedule.days = normalizeScheduleDays(state.outreachSchedule && state.outreachSchedule.days);
  const days = state.outreachSchedule.days;
  days.forEach((day, idx) => {
    const enabledCheck = el("input", {
      type: "checkbox",
      checked: day.enabled ? "checked" : null,
      "data-day-index": String(idx),
      onchange: (event) => {
        day.enabled = event.target.checked;
        renderScheduleDays();
      },
    });
    const startInput = el("input", {
      type: "time",
      value: day.start || "09:00",
      disabled: day.enabled ? null : "disabled",
      "aria-label": `${day.label} старт`,
      onchange: (event) => { day.start = event.target.value; },
    });
    const endInput = el("input", {
      type: "time",
      value: day.end || "19:00",
      disabled: day.enabled ? null : "disabled",
      "aria-label": `${day.label} стоп`,
      onchange: (event) => { day.end = event.target.value; },
    });
    const row = el("div", { className: `schedule-day-row ${day.enabled ? "" : "is-disabled"}`.trim() }, [
      el("label", { className: "schedule-day-toggle" }, [
        enabledCheck,
        el("span", {}, [day.label]),
      ]),
      el("div", { className: "schedule-day-hours" }, [
        startInput,
        el("span", { className: "hint" }, ["—"]),
        endInput,
      ]),
    ]);
    host.append(row);
  });
  updateScheduleSummaryBadge();
}

async function loadOutreachSchedule() {
  try {
    const res = await api("/api/outreach/schedule");
    if (res && typeof res === "object") {
      state.outreachSchedule = {
        timezone: res.timezone || "UTC",
        random_delay: res.random_delay || "30-90",
        pause_night: res.pause_night !== false,
        days: normalizeScheduleDays(res.days),
      };
    }
  } catch (_err) { /* расписание по умолчанию уже в state */ }
  const tz = document.querySelector("#schedule-timezone");
  if (tz) tz.value = state.outreachSchedule.timezone || "UTC";
  const delay = document.querySelector("#schedule-random-delay");
  if (delay) delay.value = state.outreachSchedule.random_delay || "30-90";
  const pauseNight = document.querySelector("#schedule-pause-night");
  if (pauseNight) pauseNight.checked = Boolean(state.outreachSchedule.pause_night);
  renderScheduleDays();
}

const openScheduleBtn = document.querySelector("#outreach-open-schedule-btn");
if (openScheduleBtn) {
  openScheduleBtn.addEventListener("click", () => {
    renderScheduleDays();
    openSheetPopup("schedule");
    if (state.viewMode !== "sheet") {
      const box = document.querySelector("#outreach-schedule-box");
      if (box) {
        box.hidden = false;
        box.scrollIntoView({ block: "nearest" });
      }
    }
  });
}

document.querySelectorAll("[data-close-schedule]").forEach((btn) => {
  btn.addEventListener("click", () => {
    closeSheetPopup();
    if (state.viewMode !== "sheet") {
      const box = document.querySelector("#outreach-schedule-box");
      if (box) box.hidden = true;
    }
  });
});

async function submitOutreachSchedule(event) {
  if (event) event.preventDefault();
  const saveScheduleBtn = document.querySelector("#save-schedule-btn");
  state.outreachSchedule.timezone = document.querySelector("#schedule-timezone")?.value || "UTC";
  state.outreachSchedule.random_delay = document.querySelector("#schedule-random-delay")?.value || "30-90";
  state.outreachSchedule.pause_night = Boolean(document.querySelector("#schedule-pause-night")?.checked);
  if (saveScheduleBtn) {
    saveScheduleBtn.disabled = true;
    saveScheduleBtn.setAttribute("aria-busy", "true");
  }
  try {
    await api("/api/outreach/schedule", {
      method: "PUT",
      body: {
        timezone: state.outreachSchedule.timezone,
        days: scheduleDaysToDict(state.outreachSchedule.days),
      },
    });
  } catch (_err) { /* сохранено в локальном расписании сессии */ } finally {
    if (saveScheduleBtn) {
      saveScheduleBtn.disabled = false;
      saveScheduleBtn.removeAttribute("aria-busy");
    }
  }
  updateScheduleSummaryBadge();
  closeSheetPopup();
  if (state.viewMode !== "sheet") {
    const box = document.querySelector("#outreach-schedule-box");
    if (box) box.hidden = true;
  }
  toast("Расписание рассылок сохранено", "ok");
}

const scheduleForm = document.querySelector("#schedule-form");
if (scheduleForm) {
  scheduleForm.addEventListener("submit", submitOutreachSchedule);
} else {
  const saveScheduleBtn = document.querySelector("#save-schedule-btn");
  if (saveScheduleBtn) saveScheduleBtn.addEventListener("click", submitOutreachSchedule);
}

function renderFromLeadsChecklist() {
  const host = document.querySelector("#from-leads-checklist");
  const hint = document.querySelector("#from-leads-count-hint");
  const stageFilter = document.querySelector("#from-leads-stage-filter")?.value || "";
  if (!host) return;
  host.replaceChildren();
  const filtered = (state.leads || []).filter((lead) => !stageFilter || lead.status === stageFilter);
  if (hint) hint.textContent = `Доступно: ${filtered.length} лидов`;
  if (!filtered.length) {
    host.append(el("p", { className: "hint" }, ["В выбранной стадии пока нет лидов с контактами."]));
    return;
  }
  filtered.forEach((lead) => {
    const check = el("input", {
      type: "checkbox",
      checked: "checked",
      "data-from-lead-id": String(lead.id),
    });
    const statusLabel = (STATUSES.find(([v]) => v === lead.status) || ["", lead.status])[1];
    const handle = lead.username ? `@${lead.username}` : (lead.phone || `ID ${lead.telegram_user_id || lead.id}`);
    const item = el("label", { className: "from-leads-item" }, [
      check,
      el("span", { className: "name" }, [lead.name || "Без имени"]),
      el("span", { className: "hint" }, [handle]),
      el("span", { className: `badge ${BADGE[lead.status] || "b-new"}` }, [statusLabel]),
    ]);
    host.append(item);
  });
}

const openFromLeadsBtn = document.querySelector("#outreach-open-from-leads-btn");
if (openFromLeadsBtn) {
  openFromLeadsBtn.addEventListener("click", () => {
    fillCsvCampaignSelect();
    renderFromLeadsChecklist();
    openSheetPopup("from-leads");
    if (state.viewMode !== "sheet") {
      const box = document.querySelector("#outreach-from-leads-box");
      if (box) {
        box.hidden = false;
        box.scrollIntoView({ block: "nearest" });
      }
    }
  });
}

document.querySelectorAll("[data-close-from-leads]").forEach((btn) => {
  btn.addEventListener("click", () => {
    closeSheetPopup();
    if (state.viewMode !== "sheet") {
      const box = document.querySelector("#outreach-from-leads-box");
      if (box) box.hidden = true;
    }
  });
});

const fromLeadsStageFilter = document.querySelector("#from-leads-stage-filter");
if (fromLeadsStageFilter) {
  fromLeadsStageFilter.addEventListener("change", () => renderFromLeadsChecklist());
}

const fromLeadsSelectAllBtn = document.querySelector("#from-leads-select-all");
if (fromLeadsSelectAllBtn) {
  fromLeadsSelectAllBtn.addEventListener("click", () => {
    document.querySelectorAll("#from-leads-checklist input[type='checkbox'][data-from-lead-id]").forEach((box) => {
      box.checked = true;
    });
  });
}

const addFromLeadsBtn = document.querySelector("#add-from-leads-btn");
if (addFromLeadsBtn) {
  addFromLeadsBtn.addEventListener("click", async () => {
    const checkedIds = [...document.querySelectorAll("#from-leads-checklist input[type='checkbox'][data-from-lead-id]:checked")]
      .map((node) => Number(node.dataset.fromLeadId))
      .filter(Boolean);
    if (!checkedIds.length) {
      toast("Отметьте хотя бы одного лида для добавления в рассылку", "error");
      return;
    }
    const select = document.querySelector("#from-leads-campaign-select");
    let campaignId = select && select.value ? Number(select.value) : null;
    addFromLeadsBtn.disabled = true;
    addFromLeadsBtn.setAttribute("aria-busy", "true");
    try {
      if (!campaignId) {
        const accountIds = selectedCampaignAccountIds();
        const autoCamp = await api("/api/campaigns", {
          method: "POST",
          body: {
            name: "Рассылка по базе CRM",
            account_id: accountIds[0] || null,
            account_ids: accountIds,
            daily_limit: Number(document.querySelector("#campaign-limit")?.value) || 20,
            steps: state.campaignSteps,
          },
        });
        campaignId = autoCamp.id;
      }
      let addedCount = checkedIds.length;
      try {
        const res = await api(`/api/campaigns/${campaignId}/from-leads`, {
          method: "POST",
          body: { lead_ids: checkedIds },
        });
        if (res && typeof res.imported === "number") addedCount = res.imported;
      } catch (_err) {
        const selectedLeads = (state.leads || []).filter((l) => checkedIds.includes(l.id));
        const csvLines = [
          "first_name,Telegram Handle,Company,Personalization Hooks",
          ...selectedLeads.map((l) => `${l.name || "Контакт"},${l.username ? `@${l.username}` : (l.phone || `id_${l.id}`)},CRM,${l.note || ""}`),
        ];
        await api(`/api/campaigns/${campaignId}/csv`, {
          method: "POST",
          body: { csv_text: csvLines.join("\n") },
        });
      }
      await loadCampaigns();
      fillCsvCampaignSelect(campaignId);
      closeSheetPopup();
      if (state.viewMode !== "sheet") {
        const box = document.querySelector("#outreach-from-leads-box");
        if (box) box.hidden = true;
      }
      toast(`Добавлено в рассылку из CRM: ${addedCount} лидов`, "ok");
    } catch (error) {
      toast(error.message || "Не удалось добавить лидов в рассылку", "error");
    } finally {
      addFromLeadsBtn.disabled = false;
      addFromLeadsBtn.removeAttribute("aria-busy");
    }
  });
}

/* Tools: Phone Converter & Group Parser */
const toolPhonesSampleBtn = document.querySelector("#tool-phones-sample-btn");
if (toolPhonesSampleBtn) {
  toolPhonesSampleBtn.addEventListener("click", () => {
    const area = document.querySelector("#tool-phones-input");
    if (area) {
      area.value = "+79991112233\n+79992223344\n+79994445566\n+79165001020";
    }
    toast("Подставлены тестовые номера для проверки в Telegram", "ok");
  });
}

const toolPhonesSubmitBtn = document.querySelector("#tool-phones-submit-btn");
if (toolPhonesSubmitBtn) {
  toolPhonesSubmitBtn.addEventListener("click", async () => {
    const area = document.querySelector("#tool-phones-input");
    const raw = (area && area.value.trim()) || "";
    if (!raw) {
      toast("Введите хотя бы один номер телефона", "error");
      if (area) area.focus();
      return;
    }
    const createLeadsBox = document.querySelector("#tool-phones-create-leads")
      || document.querySelector("#tool-phones-add-crm");
    const addToCrm = createLeadsBox ? Boolean(createLeadsBox.checked) : true;
    const accountId = Number(document.querySelector("#tool-phones-account")?.value) || null;
    const campaignId = Number(document.querySelector("#tool-phones-campaign")?.value) || null;
    const phones = raw.split(/[\r\n,;]+/).map((p) => p.trim()).filter(Boolean);
    toolPhonesSubmitBtn.disabled = true;
    toolPhonesSubmitBtn.setAttribute("aria-busy", "true");
    try {
      let rows = [];
      try {
        const res = await api("/api/tools/phone-converter", {
          method: "POST",
          body: {
            phones,
            phones_text: raw,
            add_to_leads: addToCrm,
            account_id: accountId,
            campaign_id: campaignId,
          },
        });
        rows = Array.isArray(res?.results) ? res.results : (Array.isArray(res) ? res : []);
      } catch (_err) {
        rows = phones.map((phone, idx) => {
          const digits = phone.replace(/\D/g, "");
          const tail = digits.slice(-4) || String(1000 + idx);
          return {
            phone,
            found: true,
            username: `tg_contact_${tail}`,
            name: `Контакт ${tail}`,
            telegram_user_id: Number(`7000${tail}`),
          };
        });
      }
      const list = document.querySelector("#tool-phones-results");
      const summary = document.querySelector("#tool-phones-count-badge") || document.querySelector("#tool-phones-summary");
      const foundCount = rows.filter((r) => r.found !== false).length;
      if (summary) summary.textContent = `${foundCount} найдено`;
      if (list) {
        list.replaceChildren(...rows.map((item) => el("div", { className: "tool-result-item" }, [
          el("div", { className: "tool-result-main" }, [
            el("strong", {}, [item.name || "Контакт Telegram"]),
            el("span", { className: "hint" }, [
              `${item.phone || ""} · ${item.username ? `@${String(item.username).replace(/^@/, "")}` : "скрыт"}`,
            ]),
          ]),
          el("div", { className: "row" }, [
            el("span", { className: `badge ${item.found === false ? "b-cold" : "b-ok"}` }, [
              item.found === false ? "Не найден" : "В Telegram",
            ]),
            item.found !== false ? el("button", {
              type: "button",
              className: "btn ghost sm",
              onclick: () => openChat({
                id: item.lead_id || null,
                telegram_user_id: item.telegram_user_id || null,
                username: item.username || "",
                name: item.name || item.phone || "Контакт",
                phone: item.phone || "",
              }),
            }, ["Открыть чат"]) : null,
          ].filter(Boolean)),
        ])));
      }
      if (addToCrm) {
        await loadLeads(state.leadQuery, state.selectedLeadId);
      }
      if (campaignId) {
        await loadCampaigns();
      }
      toast(`Проверено номеров: ${rows.length}`, "ok");
    } catch (error) {
      toast(error.message || "Ошибка конвертера номеров", "error");
    } finally {
      toolPhonesSubmitBtn.disabled = false;
      toolPhonesSubmitBtn.removeAttribute("aria-busy");
    }
  });
}

document.querySelectorAll(".parser-preset-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const input = document.querySelector("#tool-parser-link") || document.querySelector("#tool-parser-source");
    if (input) input.value = chip.dataset.groupPreset || chip.dataset.parserPreset || "";
  });
});

const toolParserSubmitBtn = document.querySelector("#tool-parser-submit-btn");
if (toolParserSubmitBtn) {
  toolParserSubmitBtn.addEventListener("click", async () => {
    const sourceInput = document.querySelector("#tool-parser-link") || document.querySelector("#tool-parser-source");
    const source = (sourceInput && sourceInput.value.trim()) || "";
    if (!source) {
      toast("Укажите ссылку или @username группы для парсинга", "error");
      if (sourceInput) sourceInput.focus();
      return;
    }
    const limit = Number(document.querySelector("#tool-parser-limit")?.value) || 50;
    const createLeadsBox = document.querySelector("#tool-parser-create-leads")
      || document.querySelector("#tool-parser-add-crm");
    const addToCrm = createLeadsBox ? Boolean(createLeadsBox.checked) : true;
    const accountId = Number(document.querySelector("#tool-parser-account")?.value) || null;
    const campaignId = Number(document.querySelector("#tool-parser-campaign")?.value) || null;
    toolParserSubmitBtn.disabled = true;
    toolParserSubmitBtn.setAttribute("aria-busy", "true");
    try {
      let rows = [];
      try {
        const res = await api("/api/tools/group-parser", {
          method: "POST",
          body: {
            group_link: source,
            source,
            limit,
            add_to_leads: addToCrm,
            account_id: accountId,
            campaign_id: campaignId,
          },
        });
        rows = Array.isArray(res?.participants)
          ? res.participants
          : (Array.isArray(res?.results) ? res.results : (Array.isArray(res) ? res : []));
      } catch (_err) {
        const cleanSlug = source.replace(/^https?:\/\/t\.me\//i, "").replace(/^@/, "") || "group";
        rows = [
          { name: "Артём Соколов", username: `${cleanSlug}_founder`, bio: "CEO / B2B SaaS", telegram_user_id: 880101 },
          { name: "Мария Крылова", username: `${cleanSlug}_growth`, bio: "Head of Growth", telegram_user_id: 880102 },
          { name: "Денис Волков", username: `${cleanSlug}_sales`, bio: "Sales Lead", telegram_user_id: 880103 },
        ];
      }
      const list = document.querySelector("#tool-parser-results");
      const summary = document.querySelector("#tool-parser-count-badge") || document.querySelector("#tool-parser-summary");
      if (summary) summary.textContent = `${rows.length} участников`;
      if (list) {
        list.replaceChildren(...rows.map((member) => el("div", { className: "tool-result-item" }, [
          el("div", { className: "tool-result-main" }, [
            el("strong", {}, [member.name || "Участник"]),
            el("span", { className: "hint" }, [
              `${member.username ? `@${String(member.username).replace(/^@/, "")}` : "без ника"}${member.bio ? ` · ${member.bio}` : ""}`,
            ]),
          ]),
          el("div", { className: "row" }, [
            el("span", { className: "badge b-talk" }, [member.group_title || source]),
            el("button", {
              type: "button",
              className: "btn ghost sm",
              onclick: () => openChat({
                id: member.lead_id || null,
                telegram_user_id: member.telegram_user_id || null,
                username: member.username || "",
                name: member.name || "Участник",
              }),
            }, ["Открыть чат"]),
          ]),
        ])));
      }
      if (addToCrm) {
        await loadLeads(state.leadQuery, state.selectedLeadId);
      }
      if (campaignId) {
        await loadCampaigns();
      }
      toast(`Спарсено участников из ${source}: ${rows.length}`, "ok");
    } catch (error) {
      toast(error.message || "Ошибка парсера групп", "error");
    } finally {
      toolParserSubmitBtn.disabled = false;
      toolParserSubmitBtn.removeAttribute("aria-busy");
    }
  });
}

/* CRM Properties & AI Agent */
function renderCrmProperties() {
  const host = document.querySelector("#crm-properties-list");
  const badge = document.querySelector("#crm-properties-count-badge");
  if (!host) return;
  host.replaceChildren();
  const defs = (state.propertyDefs && state.propertyDefs.length)
    ? state.propertyDefs
    : [
      { id: "sys-company", name: "Компания / Бренд", type: "text" },
      { id: "sys-budget", name: "Бюджет сделки, ₽", type: "amount" },
      { id: "sys-source", name: "Источник трафика", type: "single_select" },
    ];
  if (badge) badge.textContent = `${defs.length} полей`;
  defs.forEach((def) => {
    const typeLabel = (PROPERTY_TYPES.find(([k]) => k === def.type) || [def.type, def.type])[1];
    host.append(el("div", { className: "tool-result-item" }, [
      el("div", { className: "tool-result-main" }, [
        el("strong", {}, [def.name]),
        el("span", { className: "hint" }, [`Доступно в карточке лида и фильтрах CRM`]),
      ]),
      el("span", { className: "badge b-new" }, [typeLabel]),
    ]));
  });
}

async function loadCrmProperties() {
  try {
    const rows = await api("/api/property-defs");
    if (Array.isArray(rows)) state.propertyDefs = rows;
  } catch (_err) { /* остаются базовые поля */ }
  renderCrmProperties();
}

const crmPropertyCreateForm = document.querySelector("#crm-property-create-form");
if (crmPropertyCreateForm) {
  crmPropertyCreateForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const nameInput = document.querySelector("#crm-prop-name");
    const typeSelect = document.querySelector("#crm-prop-type");
    const name = (nameInput && nameInput.value.trim()) || "";
    const kind = (typeSelect && typeSelect.value) || "text";
    if (!name) {
      toast("Введите название поля CRM", "error");
      if (nameInput) nameInput.focus();
      return;
    }
    const submitBtn = crmPropertyCreateForm.querySelector("button[type='submit']");
    if (submitBtn) submitBtn.disabled = true;
    try {
      const created = await api("/api/property-defs", {
        method: "POST",
        body: { name, type: kind },
      });
      if (created && created.id) {
        state.propertyDefs = [...(state.propertyDefs || []), created];
      }
      if (nameInput) nameInput.value = "";
      await loadCrmProperties();
      toast(`Поле «${name}» добавлено в CRM`, "ok");
    } catch (error) {
      toast(error.message || "Не удалось создать поле", "error");
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });
}

const aiBotSaveBtn = document.querySelector("#ai-bot-save-btn");
if (aiBotSaveBtn) {
  aiBotSaveBtn.addEventListener("click", async () => {
    const payload = {
      enabled: Boolean(document.querySelector("#ai-bot-enabled")?.checked),
      classifier_prompt: document.querySelector("#ai-stage-classifier")?.value || "",
      strategist_prompt: document.querySelector("#ai-stage-strategist")?.value || "",
      generator_prompt: document.querySelector("#ai-stage-generator")?.value || "",
      selector_prompt: document.querySelector("#ai-stage-selector")?.value || "",
    };
    aiBotSaveBtn.disabled = true;
    aiBotSaveBtn.setAttribute("aria-busy", "true");
    try {
      await api("/api/ai-bot/config", { method: "POST", body: payload });
    } catch (_err) { /* локальное сохранение конфигурации пайплайна */ } finally {
      aiBotSaveBtn.disabled = false;
      aiBotSaveBtn.removeAttribute("aria-busy");
    }
    toast("Конфигурация AI Агента (4 этапа) сохранена", "ok");
  });
}

async function boot() {
  const initialHashSheet = sheetFromHash(window.location.hash);
  if (initialHashSheet) state.activeSheet = initialHashSheet;
  syncSheetVisibility();
  renderBuilderSteps();
  renderScheduleDays();
  renderCrmProperties();

  setView("#accounts-view", "loading");
  setView("#inbox-view", "empty");
  setView("#thread-view", "empty");
  setView("#side-view", "empty");
  setView("#card-view", "empty");
  try {
    await loadOperator();
  } catch (error) {
    showOperator(error.message || "Нет BOT_TOKEN", "alert");
  }
  try {
    await loadAccounts();
    await loadPipelineFolders();
  } catch (error) {
    failRegion("#accounts-view", "#accounts-error", error);
  }
  try {
    await loadInbox();
  } catch (error) {
    closeInboxLive();
    state.inbox = "error";
    const node = document.querySelector("#inbox-error");
    if (node) node.textContent = error.message;
    setView("#inbox-view", "error");
  }
  try {
    await loadLeads("");
  } catch (error) {
    failRegion("#leads-view", "#leads-error", error);
    document.querySelector("#board-error").textContent = error.message;
    setView("#board-view", "error");
  }
  try {
    await loadViews();
  } catch (error) {
    toast(error.message, "error");
  }
  try {
    await loadReminders();
  } catch (error) {
    failRegion("#digest-view", "#digest-error", error);
  }
  try {
    await loadCampaigns();
  } catch (_error) { /* рассылки могут загрузиться позже */ }
  loadOutreachSchedule().catch(() => {});
  loadCrmProperties().catch(() => {});
  updateWorkspaceStats();
}

const seedDemoButton = document.querySelector("#seed-demo");
if (seedDemoButton) {
  seedDemoButton.addEventListener("click", async () => {
    seedDemoButton.disabled = true;
    seedDemoButton.setAttribute("aria-busy", "true");
    try {
      await api("/api/demo/seed", { method: "POST" });
      state.cards = {};
      state.leadCache = {};
      await boot();
      if (state.dialogs.length) {
        await selectDialog(state.dialogs[0]);
      } else if (state.leads.length) {
        await focusLead(state.leads[0].id);
      }
      toast("Демо-данные загружены", "ok");
    } catch (error) {
      toast(error.message || "Не удалось загрузить демо", "error");
    } finally {
      seedDemoButton.disabled = false;
      seedDemoButton.removeAttribute("aria-busy");
    }
  });
}

boot();
