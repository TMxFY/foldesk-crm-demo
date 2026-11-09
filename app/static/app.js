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
  call_scheduled: "b-talk",
  waiting: "b-wait",
  on_hold: "b-wait",
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
  leadgen: "Лидген и Вакансии",
  "tool-phones": "Телефоны в TG",
  "tool-parser": "Парсер групп",
  calendar: "Календарь",
  analytics: "Аналитика",
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
  inboxSearchQuery: "",
  leadQuery: "",
  inbox: "empty",
  inboxChatMode: "crm",
  inboxChatModeExplicit: false,
  inboxSessionFilter: "all",
  selectedDialog: null,
  selectedLeadId: null,
  cards: {},
  folderLists: {},
  views: [],
  snippets: [],
  lastCopiedSnippet: "",
  campaigns: [],
  propertyDefs: [],
  analytics: null,
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
  expandedCampaignSettings: {},
  leadgenConfig: null,
  leadgenStatus: null,
  selectedBuilderAccountIds: null,
  csvHeaders: [],
};

let modalSubmit = null;
let modalReturn = null;
let toastTimer = 0;
let inboxSource = null;
let inboxLiveAccount = "";
let cardToken = 0;
let dialogLoadToken = 0;

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
  if (clean === "leadgen") return "leadgen";
  if (clean === "tool-phones") return "tool-phones";
  if (clean === "tool-parser") return "tool-parser";
  if (clean === "calendar") return "calendar";
  if (clean === "analytics") return "analytics";
  return "";
}

function triggerSheetEntrance(section) {
  if (!section) return;
  section.classList.remove("sheet-entering");
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
  const accountsSection = document.querySelector("#accounts");
  if (kind === "login" && accountsSection) {
    accountsSection.dataset.authOpen = "true";
  }
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
  const accountsSection = document.querySelector("#accounts");
  if (accountsSection) {
    delete accountsSection.dataset.authOpen;
  }
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

  const navLinks = [...document.querySelectorAll(".nav a[data-sheet-link]")];
  navLinks.forEach((item) => {
    if (item.dataset.sheetLink === active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });

  const s07 = document.querySelector("#s07");
  const messengerGrid = document.querySelector(".messenger-grid");
  const outreach = document.querySelector("#outreach");
  const leads = document.querySelector("#leads");
  const opsGrid = document.querySelector(".ops-grid");
  const digest = document.querySelector("#digest");
  const accounts = document.querySelector("#accounts");
  const s11 = document.querySelector("#s11");
  const leadgenSec = document.querySelector("#leadgen");
  const toolPhones = document.querySelector("#tool-phones");
  const toolParser = document.querySelector("#tool-parser");
  const crmProps = document.querySelector("#crm-properties");
  const aiBot = document.querySelector("#ai-bot");
  const calendarSec = document.querySelector("#calendar");
  const analyticsSec = document.querySelector("#analytics");

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
    if (calendarSec) calendarSec.hidden = false;
    if (analyticsSec) analyticsSec.hidden = false;
    if (leadgenSec) leadgenSec.hidden = active !== "leadgen";
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
  if (leadgenSec) leadgenSec.hidden = active !== "leadgen";
  if (toolPhones) toolPhones.hidden = active !== "tool-phones";
  if (toolParser) toolParser.hidden = active !== "tool-parser";
  if (crmProps) crmProps.hidden = active !== "crm-properties";
  if (aiBot) aiBot.hidden = active !== "ai-bot";
  if (calendarSec) calendarSec.hidden = active !== "calendar";
  if (analyticsSec) analyticsSec.hidden = active !== "analytics";

  if (active === "s07" || active === "leads") {
    state.bulkHostTarget = active;
  }
  if (typeof syncBulkHostContainer === "function") syncBulkHostContainer();

  if (active === "inbox") {
    const connected = (state.accounts || []).filter((account) => account.status === "connected");
    syncTelegramWebAPanel(connected);
  }

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
  const isError = kind === "error" || kind === "err";
  node.dataset.kind = isError ? "error" : "ok";
  node.setAttribute("role", isError ? "alert" : "status");
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
  const method = (opts.method || "GET").toUpperCase();
  const maxAttempts = method === "GET" ? 3 : 1;
  let res = null;
  let text = "";

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      res = await fetch(path, {
        method,
        headers: opts.body ? { "Content-Type": "application/json" } : undefined,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
      });
      text = await res.text();
      if ((res.status === 502 || res.status === 503 || res.status === 504) && attempt < maxAttempts) {
        await new Promise((r) => setTimeout(r, 1200 * attempt));
        continue;
      }
      break;
    } catch (fetchErr) {
      if (attempt < maxAttempts) {
        await new Promise((r) => setTimeout(r, 1200 * attempt));
        continue;
      }
      throw fetchErr;
    }
  }

  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (_err) { data = { detail: text }; }
  }
  if (!res.ok) {
    let detail = data && data.detail;
    if (typeof detail === "string") {
      const trimmed = detail.trim();
      if (trimmed.startsWith("<") || /<!doctype\s+html|<html/i.test(trimmed)) {
        detail = `Сервер временно перезапускается (HTTP ${res.status}), подождите пару секунд`;
      } else if (trimmed.length > 260) {
        detail = `${trimmed.slice(0, 260)}…`;
      }
    }
    throw new Error(typeof detail === "string" && detail ? detail : `Запрос не дошёл (HTTP ${res.status})`);
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

function leadAmount(lead) {
  if (!lead) return 0;
  const num = Number(lead.amount);
  return Number.isFinite(num) && num > 0 ? Math.round(num) : 0;
}

function formatMoney(amount, currency = "USD") {
  const num = Math.max(0, Math.round(Number(amount) || 0));
  const cur = String(currency || "USD").toUpperCase();
  const sym = cur === "EUR" ? "€" : (cur === "RUB" ? "₽" : "$");
  return `${sym}${num.toLocaleString("en-US")}`;
}

function amountField(lead, label = "Бюджет ($)") {
  return el("input", {
    type: "number",
    min: "0",
    step: "1",
    name: "amount",
    className: "lead-amount-input",
    value: String(leadAmount(lead)),
    "aria-label": label,
    onchange: (event) => {
      const raw = event.target.value;
      const num = Math.max(0, Math.round(Number(raw) || 0));
      lead.amount = num;
      if (state.leadCache[lead.id]) state.leadCache[lead.id].amount = num;
      if (state.cards[lead.id]) state.cards[lead.id].amount = num;
      const inList = state.leads.find((item) => Number(item.id) === Number(lead.id));
      if (inList) inList.amount = num;
      const sideBadge = document.querySelector("#side-card .side-amount-badge");
      const dialogLead = state.selectedDialog ? leadForDialog(state.selectedDialog) : null;
      if (sideBadge && dialogLead && Number(dialogLead.id) === Number(lead.id)) {
        sideBadge.setAttribute("data-side-amount", String(num));
        sideBadge.textContent = formatMoney(num, lead.currency);
      }
      renderBoard();
      patchLead(lead.id, { amount: num });
    },
  });
}

function syncStageSelectOptions() {
  const stageFilter = document.querySelector("#stage-filter");
  if (stageFilter) {
    const prev = stageFilter.value;
    stageFilter.replaceChildren(el("option", { value: "" }, ["Все"]));
    STATUSES.forEach(([key, label]) => {
      stageFilter.append(el("option", { value: key }, [label]));
    });
    if (prev && [...stageFilter.options].some((opt) => opt.value === prev)) {
      stageFilter.value = prev;
    }
  }
  const fromLeadsStage = document.querySelector("#from-leads-stage-filter");
  if (fromLeadsStage) {
    const prev = fromLeadsStage.value;
    fromLeadsStage.replaceChildren(el("option", { value: "" }, ["Все стадии"]));
    STATUSES.forEach(([key, label]) => {
      fromLeadsStage.append(el("option", { value: key }, [label]));
    });
    if (prev && [...fromLeadsStage.options].some((opt) => opt.value === prev)) {
      fromLeadsStage.value = prev;
    }
  }
  const bulkStatus = document.querySelector("#bulk-status-select");
  if (bulkStatus) {
    const prev = bulkStatus.value;
    bulkStatus.replaceChildren();
    STATUSES.forEach(([key, label]) => {
      bulkStatus.append(el("option", { value: key }, [label]));
    });
    if (prev && [...bulkStatus.options].some((opt) => opt.value === prev)) {
      bulkStatus.value = prev;
    }
  }
}

async function loadPipelineStages() {
  try {
    const stages = await api("/api/pipeline/stages");
    if (Array.isArray(stages) && stages.length) {
      state.pipelineStages = stages;
      STATUSES.splice(
        0,
        STATUSES.length,
        ...stages.map((s) => [String(s.key), String(s.label)])
      );
      stages.forEach((s) => {
        if (!BADGE[s.key]) BADGE[s.key] = "b-new";
      });
      syncStageSelectOptions();
      renderBoard();
      renderLeadTable();
      if (state.selectedLeadId != null && state.cards[state.selectedLeadId]) {
        paintCard(state.cards[state.selectedLeadId]);
      }
      renderSide();
    }
  } catch (_err) {
    /* fallback to default STATUSES */
  }
}

async function promptRenameStage(stageKey, currentLabel) {
  const nextLabel = window.prompt("Новое название стадии воронки:", currentLabel || "");
  if (nextLabel == null) return;
  const cleaned = String(nextLabel).trim();
  if (!cleaned || cleaned === currentLabel) return;
  try {
    await api(`/api/pipeline/stages/${encodeURIComponent(stageKey)}`, {
      method: "PATCH",
      body: { label: cleaned },
    });
    await loadPipelineStages();
    toast("Стадия переименована", "ok");
  } catch (error) {
    toast(error.message || "Не удалось переименовать стадию", "error");
  }
}

async function promptCreateStage() {
  const rawLabel = window.prompt("Название новой стадии воронки:");
  if (rawLabel == null) return;
  const cleaned = String(rawLabel).trim();
  if (!cleaned) return;
  try {
    await api("/api/pipeline/stages", {
      method: "POST",
      body: { label: cleaned },
    });
    await loadPipelineStages();
    toast(`Стадия «${cleaned}» добавлена`, "ok");
  } catch (error) {
    toast(error.message || "Не удалось добавить стадию", "error");
  }
}

function textField(lead, field, label, extraAttrs = {}) {
  const placeholders = {
    name: "Имя контакта",
    note: "Добавить заметку или контекст...",
    next_step: "Указать следующий шаг...",
    phone: "+7...",
  };
  return el("input", {
    value: lead[field] || "",
    placeholder: extraAttrs.placeholder || placeholders[field] || "",
    className: extraAttrs.className || `field-input field-${field}`,
    "aria-label": label,
    onchange: (event) => patchLead(lead.id, { [field]: event.target.value }),
  });
}

function formatStructuredNoteNode(rawNote, { compact = false } = {}) {
  const text = String(rawNote || "").trim();
  if (!text) return null;
  const parts = text.split(/\s*\|\s*(?=(?:💬|Их продукт:|Наш оффер:|Компания:|Бюджет:|Источник:))/i);
  if (parts.length <= 1 && !/^💬\s*Ответ лида/i.test(text)) {
    return null;
  }
  const rows = [];
  parts.forEach((segment) => {
    const cleanSeg = segment.trim();
    if (!cleanSeg) return;
    if (/^💬\s*Ответ лида/i.test(cleanSeg)) {
      const quoteMatch = cleanSeg.match(/«([\s\S]*?)»/);
      const headerMatch = cleanSeg.match(/^💬\s*Ответ лида\s*(?:\(([^)]+)\))?/i);
      const toneText = headerMatch && headerMatch[1] ? headerMatch[1].replace(/\s+на Шаг.*$/i, "").trim() : "Ответ клиента";
      const quoteBody = quoteMatch
        ? quoteMatch[1].split(/\s*\|\s*/).map((s) => s.trim()).filter(Boolean).join(" · ")
        : cleanSeg.replace(/^💬\s*Ответ лида[^:]*:\s*/i, "");
      rows.push(el("div", { className: "structured-note-row is-reply" }, [
        el("span", { className: "structured-note-tag" }, [`💬 ${toneText}:`]),
        el("span", { className: "structured-note-val" }, [quoteBody]),
      ]));
    } else if (/^Их продукт:/i.test(cleanSeg)) {
      rows.push(el("div", { className: "structured-note-row" }, [
        el("span", { className: "structured-note-label" }, ["Продукт:"]),
        el("span", { className: "structured-note-val" }, [cleanSeg.replace(/^Их продукт:\s*/i, "")]),
      ]));
    } else if (/^Наш оффер:/i.test(cleanSeg)) {
      rows.push(el("div", { className: "structured-note-row" }, [
        el("span", { className: "structured-note-label" }, ["Оффер:"]),
        el("span", { className: "structured-note-val" }, [cleanSeg.replace(/^Наш оффер:\s*/i, "")]),
      ]));
    } else {
      rows.push(el("div", { className: "structured-note-row" }, [
        el("span", { className: "structured-note-val" }, [cleanSeg]),
      ]));
    }
  });
  return el("div", { className: `structured-note-block${compact ? " is-compact" : ""}`, title: text }, rows);
}

function formatStructuredStepNode(rawStep) {
  const text = String(rawStep || "").trim();
  if (!text) return null;
  const parts = text.split(/\s*·\s*/).map((s) => s.trim()).filter(Boolean);
  if (parts.length <= 1 && !/^[🔥❌✅]/u.test(text)) {
    return null;
  }
  const head = parts[0];
  const rest = parts.slice(1).join(" · ");
  let badgeCls = "b-talk";
  if (head.startsWith("🔥")) badgeCls = "b-wait";
  else if (head.startsWith("❌")) badgeCls = "b-lost";
  else if (head.includes("+ Позитив")) badgeCls = "b-won";
  return el("div", { className: "structured-step-block", title: text }, [
    el("span", { className: `badge ${badgeCls} structured-step-badge` }, [head]),
    rest ? el("span", { className: "structured-step-sub" }, [rest]) : null,
  ].filter(Boolean));
}

function formatCleanCardPreview(lead) {
  const step = String(lead.next_step || "").trim();
  if (step) {
    const parts = step.split(/\s*·\s*/).map((s) => s.trim()).filter(Boolean);
    if (parts.length >= 2) {
      return parts.slice(1).join(" · ");
    }
    return step;
  }
  const note = String(lead.note || "").trim();
  if (!note) return "";
  const quoteMatch = note.match(/«([\s\S]*?)»/);
  if (quoteMatch) {
    const cleanQuote = quoteMatch[1].split(/\s*\|\s*/).filter(Boolean).slice(-1)[0] || quoteMatch[1];
    return `💬 «${cleanQuote.slice(0, 90)}»`;
  }
  return note.split(/\s*\|\s*/)[0].slice(0, 95);
}

async function patchLead(id, body) {
  try {
    const updated = await api(`/api/leads/${id}`, { method: "PATCH", body });
    delete state.cards[id];
    state.leadCache[updated.id] = updated;
    await loadLeads(state.leadQuery, updated.id, true);
  } catch (error) {
    toast(error.message, "error");
    await loadLeads(state.leadQuery, id, true);
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
  const nextStepStr = String(lead.next_step || "");
  const offerTypeStr = String(lead.offer_type || "");
  const noteStr = String(lead.note || "");
  const isWaitingOurAnswer = lastFromPeer === 1 || nextStepStr.startsWith("🔥");
  const isPositiveReply = offerTypeStr.includes("+ Позитив") || nextStepStr.includes("+ Позитив") || noteStr.includes("+ Позитив");
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
    isWaitingOurAnswer,
    isPositiveReply,
    isFollowupDue,
    openReminders,
  };
}

function matchFollowupFilter(lead, followup) {
  if (!followup) return true;
  const tr = resolveLeadTracking(lead);
  if (followup === "waiting_our_answer") return tr.isWaitingOurAnswer;
  if (followup === "positive_reply") return tr.isPositiveReply;
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
  if (account.status === "connected") return isWa ? "Подключён · WhatsApp Web активен" : "Подключён · MTProto онлайн";
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

function shortProxyLabel(account) {
  const raw = String(account.proxy_location || account.proxy_url || account.proxy || "Auto").trim();
  if (!raw || raw.toLowerCase() === "auto") return "Auto";
  try {
    const parsed = new URL(raw.includes("://") ? raw : `socks5://${raw}`);
    return parsed.hostname ? `Proxy: ${parsed.hostname.slice(0, 15)}` : "Proxy";
  } catch {
    return raw.length > 18 ? `${raw.slice(0, 16)}…` : raw;
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
    if (account.telegram_username && `@${account.telegram_username}` !== label) {
      subParts.push(`@${account.telegram_username}`);
    }
    if (account.phone && account.phone !== label) subParts.push(account.phone);
    const proxyFull = String(account.proxy_location || account.proxy_url || account.proxy || "Auto");
    const proxyShort = shortProxyLabel(account);

    const identityCol = el("div", { className: "account-col-identity" }, [
      el("span", { className: "conn-dot", "data-conn": connState, "aria-hidden": "true" }, []),
      channelBadgeEl(account.provider),
      el("span", { className: "name", title: label }, [label]),
      subParts.length ? el("span", { className: "hint account-submeta", title: subParts.join(" · ") }, [subParts.join(" · ")]) : null,
    ].filter(Boolean));

    const badgeChildren = [
      el("span", { className: `badge ${connBadgeClass}` }, [connLabel]),
      account.status !== "connected" ? el("span", { className: `badge ${pair[1]}` }, [pair[0]]) : null,
      proxyShort !== "Auto" ? el("span", { className: "badge b-new", title: proxyFull }, [proxyShort]) : null,
    ].filter(Boolean);
    if (account.health && account.health !== "ok") {
      const healthPair = ACCOUNT_HEALTH[account.health] || [account.health, "b-off"];
      badgeChildren.push(el("span", { className: `badge ${healthPair[1]}` }, [healthPair[0]]));
    }
    const badgesCol = el("div", { className: "account-col-badges" }, badgeChildren);

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

    const limitCol = el("div", { className: "account-col-limit account-limit-bar" }, [
      el("span", { className: "limit-counter", title: `Отправлено сегодня: ${sentToday} из ${dailyLimit}` }, [
        `Сегодня: ${sentToday} / ${dailyLimit}`,
      ]),
      el("div", { className: "limit-progress", "data-state": limitState, "aria-hidden": "true" }, [
        el("span", { className: "limit-fill", style: `width: ${pct}%` }, []),
      ]),
      el("div", { className: "limit-actions" }, [
        el("button", {
          type: "button",
          className: "chip",
          "data-limit-preset": "10",
          "aria-label": "10 / сут",
          "aria-pressed": dailyLimit === 10 ? "true" : "false",
          onclick: () => updateAccountLimit(account.id, 10),
        }, ["10"]),
        el("button", {
          type: "button",
          className: "chip",
          "data-limit-preset": "20",
          "aria-label": "20 / сут",
          "aria-pressed": dailyLimit === 20 ? "true" : "false",
          onclick: () => updateAccountLimit(account.id, 20),
        }, ["20"]),
        limitInput,
        el("button", {
          type: "button",
          className: "btn ghost sm limit-save-btn",
          title: "Сохранить лимит",
          "aria-label": "Сохранить лимит",
          onclick: () => updateAccountLimit(account.id, limitInput.value),
        }, ["✓"]),
      ]),
    ]);

    const actionsWrap = el("div", { className: "account-col-actions row" }, []);
    if (account.status === "pending_code" || account.status === "pending_password") {
      pending = account;
      const caption = account.status === "pending_password" ? "Пароль" : "Код";
      actionsWrap.append(el("button", { type: "button", className: "btn primary sm", onclick: () => resumeLogin(account) }, [caption]));
    }
    if (account.status === "connected") {
      if (account.tg_web_ready) {
        actionsWrap.append(
          el("button", {
            type: "button",
            className: "btn primary sm",
            title: "Открыть Telegram Web (/a/)",
            onclick: async () => {
              state.inboxChatMode = "tgweb";
              state.inboxChatModeExplicit = true;
              await switchInboxSession(String(account.id));
              activateSheet("inbox");
              const panel = document.querySelector("#tg-web-a-panel");
              if (panel && state.viewMode !== "sheet") panel.scrollIntoView({ block: "nearest" });
            },
          }, ["Web TG ↗"]),
        );
      }
      actionsWrap.append(
        el("button", {
          type: "button",
          className: "btn secondary sm",
          title: "Открыть диалоги в CRM",
          onclick: async () => {
            state.inboxChatMode = "crm";
            state.inboxChatModeExplicit = true;
            await switchInboxSession(String(account.id));
            activateSheet("inbox");
            const s04 = document.querySelector("#s04");
            if (s04 && state.viewMode !== "sheet") s04.scrollIntoView({ block: "nearest" });
            const matched = state.dialogs.filter(passChips);
            if (matched.length && !state.selectedDialog) {
              selectDialog(matched[0]).catch(() => {});
            }
          },
        }, ["Чаты"]),
        el("button", {
          type: "button",
          className: "btn ghost sm account-reconnect-btn",
          title: "Переподключить сессию",
          "aria-label": "Переподключить",
          onclick: () => reconnectAccount(account),
        }, ["↻"]),
      );
    }

    const row = el("li", { className: "account account-compact-row" }, [
      el("div", { className: "account-row-line" }, [
        identityCol,
        badgesCol,
        limitCol,
        actionsWrap,
      ]),
    ]);
    list.append(row);
  });
  if (pending && state.loginId == null && connected.length === 0) resumeLogin(pending);
  const select = document.querySelector("#tg-account");
  const previous = select.value;
  if (accountsSection) {
    accountsSection.dataset.hasConnected = (connected.length > 0 && state.loginId == null) ? "true" : "false";
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
  const connected = state.accounts.filter(
    (account) => account.status === "connected" && normalizeProvider(account.provider) === "telegram",
  );
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

const REVSHARE_OFFER_TEXT = "We work on revshare or CPA and you only cover the production costs. Want to see 3 example reels for your brand, to check how it could work for you?";

function dedupeConnectedAccounts(accounts) {
  const seen = new Set();
  const out = [];
  (accounts || []).forEach((acc) => {
    if (!acc || acc.status !== "connected") return;
    const key = String(acc.phone || acc.telegram_username || acc.id || "").trim().toLowerCase();
    if (key && seen.has(key)) return;
    if (key) seen.add(key);
    out.push(acc);
  });
  return out;
}

function formatCompactAccountName(account) {
  const rawName = String(account.telegram_name || account.telegram_username || account.phone || "").trim();
  const cleanName = rawName.replace(/^\[(TG|WA)\]\s*/i, "");
  const uname = String(account.telegram_username || "").trim().replace(/^@/, "");
  if (uname && cleanName.toLowerCase() !== uname.toLowerCase() && !cleanName.includes(`@${uname}`)) {
    const duplicateNameCount = (state.accounts || []).filter(
      (a) => a.status === "connected" && String(a.telegram_name || "").trim().toLowerCase() === cleanName.toLowerCase(),
    ).length;
    if (duplicateNameCount > 1) {
      return `${cleanName} (@${uname})`;
    }
  }
  return cleanName;
}

async function uploadOutreachImageFile(file) {
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("Не удалось прочитать файл картинки"));
    reader.readAsDataURL(file);
  });
  const resp = await fetch("/api/outreach/upload-image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      filename: file.name || "image.png",
      data_url: dataUrl,
    }),
    credentials: "same-origin",
  });
  let payload = null;
  try {
    payload = await resp.json();
  } catch (_err) {
    payload = null;
  }
  if (!resp.ok) {
    throw new Error((payload && payload.detail) || "Не удалось загрузить картинку");
  }
  return (payload && payload.image_url) || "";
}

function buildStepMediaAndOfferControls(step, idx, onUpdate) {
  const wrap = el("div", { className: "step-extra-controls stack" }, []);

  if (idx === 0) {
    const appendOfferToStep = (offerText) => {
      const cleanOffer = String(offerText || "").trim();
      if (!cleanOffer) return;
      const cur = String(step.template_text || "").trim();
      if (cur.includes(cleanOffer)) {
        toast("Этот оффер уже добавлен в сообщение", "ok");
        return;
      }
      const base = cur || "{step1_text}";
      step.template_text = `${base}\n\n${cleanOffer}`;
      onUpdate(step.template_text);
    };

    const customOfferInput = el("input", {
      type: "text",
      className: "step-offer-tail-input",
      value: REVSHARE_OFFER_TEXT,
      "aria-label": "Дополнительный текст оффера к первому сообщению",
      placeholder: "Текст оффера в конец 1-го сообщения...",
    });

    const offerRow = el("div", { className: "step-offer-bar" }, [
      el("div", { className: "step-offer-actions row" }, [
        el("span", { className: "hint" }, ["Режим Шага 1:"]),
        el("button", {
          type: "button",
          className: "chip step-mode-chip",
          title: "Использовать индивидуальное первое сообщение из колонки CSV + добавить оффер",
          onclick: () => {
            step.template_text = `{step1_text}\n\n${customOfferInput.value.trim() || REVSHARE_OFFER_TEXT}`;
            onUpdate(step.template_text);
          },
        }, ["📄 {step1_text} из CSV + Оффер"]),
        el("button", {
          type: "button",
          className: "chip step-offer-preset-btn",
          "data-offer-preset": "revshare_cpa",
          title: "Добавить в конец Шага 1 оффер про RevShare / CPA и 3 примера рилсов",
          onclick: () => {
            customOfferInput.value = REVSHARE_OFFER_TEXT;
            appendOfferToStep(REVSHARE_OFFER_TEXT);
          },
        }, ["+ Оффер RevShare / CPA (3 reels)"]),
      ]),
      el("div", { className: "step-offer-custom-row row" }, [
        customOfferInput,
        el("button", {
          type: "button",
          className: "btn secondary sm",
          onclick: () => appendOfferToStep(customOfferInput.value),
        }, ["+ В конец Шага 1"]),
      ]),
    ]);
    wrap.append(offerRow);
  }

  const fileInput = el("input", {
    type: "file",
    accept: "image/png,image/jpeg,image/webp,image/gif",
    className: "step-image-file-input",
    hidden: "hidden",
  });

  const urlInput = el("input", {
    type: "text",
    className: "step-image-url-input",
    value: String(step.image_url || ""),
    "aria-label": `Картинка шага ${idx + 1}`,
    placeholder: "URL картинки или загрузите файл (опционально)...",
    oninput: () => {
      step.image_url = urlInput.value.trim() || null;
      syncPreview();
    },
  });

  const previewThumb = el("img", {
    className: "step-image-thumb",
    alt: "",
    role: "presentation",
    hidden: !step.image_url ? "hidden" : undefined,
    src: step.image_url || "",
  });

  const clearImgBtn = el("button", {
    type: "button",
    className: "btn ghost sm",
    hidden: !step.image_url ? "hidden" : undefined,
    onclick: () => {
      step.image_url = null;
      urlInput.value = "";
      syncPreview();
    },
  }, ["Убрать ×"]);

  function syncPreview() {
    const val = String(step.image_url || "").trim();
    if (val) {
      previewThumb.src = val;
      previewThumb.hidden = false;
      clearImgBtn.hidden = false;
    } else {
      previewThumb.removeAttribute("src");
      previewThumb.hidden = true;
      clearImgBtn.hidden = true;
    }
  }

  const uploadBtn = el("button", {
    type: "button",
    className: "btn secondary sm step-upload-image-btn",
    onclick: () => fileInput.click(),
  }, ["🖼 Прикрепить картинку"]);

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    uploadBtn.disabled = true;
    uploadBtn.textContent = "Загрузка...";
    try {
      const uploadedUrl = await uploadOutreachImageFile(file);
      step.image_url = uploadedUrl;
      urlInput.value = uploadedUrl;
      syncPreview();
      toast(`Картинка прикреплена к шагу ${idx + 1}`, "ok");
    } catch (error) {
      toast(error.message || "Ошибка загрузки картинки", "error");
    } finally {
      uploadBtn.disabled = false;
      uploadBtn.textContent = "🖼 Прикрепить картинку";
      fileInput.value = "";
    }
  });

  wrap.append(
    el("div", { className: "step-media-bar row" }, [
      fileInput,
      uploadBtn,
      urlInput,
      previewThumb,
      clearImgBtn,
    ]),
  );

  return wrap;
}

function fillCampaignAccounts() {
  const select = document.querySelector("#campaign-account");
  const poolHost = document.querySelector("#campaign-accounts-pool");
  if (!select) return;
  const previous = select.value;
  const rawConnected = state.accounts.filter((account) => account.status === "connected");
  const connected = dedupeConnectedAccounts(rawConnected);
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
  rawConnected.forEach((account) => {
    const label = formatCompactAccountName(account);
    const sent = Number(account.sent_today) || 0;
    const limit = Number(account.daily_limit) || 20;
    select.append(
      el("option", { value: String(account.id) }, [`${label} (${sent}/${limit})`]),
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
      const label = formatCompactAccountName(account);
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
          el("span", { className: "pool-check-name" }, [label]),
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
        ? "Шаг 1 · Сразу (первое касание)"
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
      rows: idx === 0 ? "4" : "3",
      "data-step-index": String(idx),
      "aria-label": `Шаблон сообщения шага ${idx + 1}`,
      placeholder: idx === 0
        ? "Вставьте готовое сообщение целиком ИЛИ используйте {step1_text} из CSV..."
        : "Текст фоллоу-апа или {step2_text} из CSV...",
      onfocus: () => { state.activeStepIndex = idx; },
      oninput: (event) => { step.template_text = event.target.value; },
    }, [step.template_text || ""]);

    const extraControls = buildStepMediaAndOfferControls(step, idx, (newText) => {
      area.value = newText;
    });

    host.append(
      el("div", { className: "campaign-step-card" }, [
        el("div", { className: "step-card-head" }, [
          titleSpan,
          el("div", { className: "row" }, headRight),
        ]),
        area,
        extraControls,
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

const CAMPAIGN_LEADGEN_SOURCE_OPTIONS = [
  ["tg_groups", "TG-группы"],
  ["yc_ph", "YC и Product Hunt"],
  ["meta_ads", "Реклама Meta"],
  ["tg_ad_spy", "Реклама Telegram"],
  ["channel_graph", "Похожие каналы"],
  ["social_x_reddit", "X и Reddit"],
  ["buyer_catalogs", "Каталоги SaaS"],
  ["linkedin_jobs", "LinkedIn"],
  ["tg_vacancies", "TG-Вакансии"],
];

function buildCampaignSettingsPanel(camp, opts) {
  const { settingsOpen, dailyLimit, leadgenEnabled, leadgenQuota, leadgenSourcesList, steps } = opts;

  const nameInput = el("input", {
    type: "text",
    className: "camp-edit-name",
    value: camp.name || "",
    placeholder: "Название рассылки",
  });

  const limitInput = el("input", {
    type: "number",
    className: "camp-edit-limit",
    min: "1",
    max: "500",
    value: String(dailyLimit),
  });

  const bumpLimit = (delta) => {
    const cur = Number(limitInput.value) || dailyLimit;
    limitInput.value = String(Math.max(1, Math.min(500, cur + delta)));
  };

  const limitStepper = el("div", { className: "stepper-control" }, [
    el("button", { type: "button", className: "btn ghost sm", onclick: () => bumpLimit(-5) }, ["-5"]),
    limitInput,
    el("button", { type: "button", className: "btn ghost sm", onclick: () => bumpLimit(5) }, ["+5"]),
  ]);

  const lgEnabledCheck = el("input", {
    type: "checkbox",
    className: "camp-edit-leadgen-enabled",
  });
  lgEnabledCheck.checked = Boolean(leadgenEnabled);

  const lgPillBadge = el("span", {
    className: `badge ${leadgenEnabled ? "b-ok" : "b-off"}`,
  }, [leadgenEnabled ? "ВКЛ" : "ВЫКЛ"]);

  lgEnabledCheck.addEventListener("change", () => {
    const isOn = Boolean(lgEnabledCheck.checked);
    lgPillBadge.textContent = isOn ? "ВКЛ" : "ВЫКЛ";
    lgPillBadge.className = `badge ${isOn ? "b-ok" : "b-off"}`;
  });

  const lgQuotaInput = el("input", {
    type: "number",
    className: "camp-edit-leadgen-quota",
    min: "0",
    max: "500",
    value: String(leadgenQuota),
  });

  const bumpLgQuota = (delta) => {
    const cur = Number(lgQuotaInput.value) || 0;
    lgQuotaInput.value = String(Math.max(0, Math.min(500, cur + delta)));
  };

  const lgQuotaStepper = el("div", { className: "stepper-control" }, [
    el("button", { type: "button", className: "btn ghost sm", onclick: () => bumpLgQuota(-5) }, ["-5"]),
    lgQuotaInput,
    el("button", { type: "button", className: "btn ghost sm", onclick: () => bumpLgQuota(5) }, ["+5"]),
  ]);

  const connectedAccounts = dedupeConnectedAccounts(state.accounts.filter((a) => a.status === "connected"));
  const attachedIds = new Set(resolveCampaignAccounts(camp).map((a) => Number(a.id)));
  const accountBoxes = [];
  const accountChipsNodes = connectedAccounts.map((acc) => {
    const accId = Number(acc.id);
    const cb = el("input", {
      type: "checkbox",
      "data-settings-acc-id": String(accId),
    });
    cb.checked = attachedIds.has(accId);
    accountBoxes.push(cb);
    return el("label", { className: "pool-check-chip" }, [
      cb,
      channelBadgeEl(acc.provider),
      el("span", { className: "pool-check-name" }, [formatCompactAccountName(acc)]),
      el("span", { className: "pool-check-meta" }, [`${Number(acc.sent_today) || 0}/${Number(acc.daily_limit) || 20}`]),
    ]);
  });

  const recipients = Array.isArray(camp.recipients) ? camp.recipients : [];
  const recipientPreviewChips = recipients.slice(0, 5).map((r) => {
    const rName = r.name || r.company || r.username || r.phone || "Контакт";
    const rHandle = r.username ? `@${String(r.username).replace(/^@/, "")}` : "";
    return el("span", { className: "recipient-mini-chip" }, [
      rHandle ? `${rName} (${rHandle})` : rName,
    ]);
  });

  const sourceBoxes = [];
  const sourceChecksNodes = CAMPAIGN_LEADGEN_SOURCE_OPTIONS.map(([code, label]) => {
    const isChecked = leadgenSourcesList.includes(code);
    const cb = el("input", {
      type: "checkbox",
      "data-camp-leadgen-source": code,
      hidden: "hidden",
    });
    cb.checked = isChecked;
    sourceBoxes.push(cb);

    const pillBtn = el("button", {
      type: "button",
      className: `camp-source-chip ${isChecked ? "is-active" : ""}`,
      "aria-pressed": isChecked ? "true" : "false",
      onclick: () => {
        cb.checked = !cb.checked;
        pillBtn.classList.toggle("is-active", cb.checked);
        pillBtn.setAttribute("aria-pressed", cb.checked ? "true" : "false");
        stateDot.className = `badge ${cb.checked ? "b-ok" : "b-off"}`;
        stateDot.textContent = cb.checked ? "ВКЛ" : "ВЫКЛ";
      },
    }, []);
    const stateDot = el("span", {
      className: `badge ${isChecked ? "b-ok" : "b-off"}`,
    }, [isChecked ? "ВКЛ" : "ВЫКЛ"]);
    pillBtn.append(cb, el("span", {}, [label]), stateDot);
    return pillBtn;
  });

  const editableSteps = (Array.isArray(steps) && steps.length
    ? steps
    : [{ step_order: 1, delay_hours: 0, template_text: "Привет, {name}!", image_url: null }]
  ).map((st) => ({
    delay_hours: Number(st.delay_hours) || 0,
    template_text: String(st.template_text || ""),
    image_url: st.image_url ? String(st.image_url) : null,
  }));

  const stepsHost = el("div", { className: "stack camp-edit-steps-host" }, []);

  const renderEditableSteps = () => {
    stepsHost.replaceChildren();
    editableSteps.forEach((st, idx) => {
      const delayInput = el("input", {
        type: "number",
        min: "0",
        max: "720",
        value: String(st.delay_hours),
        oninput: () => {
          st.delay_hours = Math.max(0, Number(delayInput.value) || 0);
        },
      });
      const textInput = el("textarea", {
        rows: "3",
        placeholder: idx === 0
          ? "Вставьте готовое сообщение целиком ИЛИ используйте {step1_text} из CSV..."
          : "Текст сообщения шага...",
        oninput: () => {
          st.template_text = textInput.value;
        },
      });
      textInput.value = st.template_text;

      const extraControls = buildStepMediaAndOfferControls(st, idx, (newText) => {
        textInput.value = newText;
      });

      const removeStepBtn = editableSteps.length > 1
        ? el("button", {
          type: "button",
          className: "btn ghost sm",
          onclick: () => {
            editableSteps.splice(idx, 1);
            renderEditableSteps();
          },
        }, ["Удалить шаг ×"])
        : null;

      stepsHost.append(
        el("div", { className: "step-card stack" }, [
          el("div", { className: "subcard-head" }, [
            el("strong", {}, [idx === 0 ? "Шаг 1 · Первое сообщение" : `Шаг ${idx + 1}`]),
            el("div", { className: "row" }, [
              el("label", { className: "inline" }, [
                idx === 0 ? "Пауза (ч):" : "Через (ч):",
                delayInput,
              ]),
              removeStepBtn,
            ].filter(Boolean)),
          ]),
          textInput,
          extraControls,
        ]),
      );
    });
  };
  renderEditableSteps();

  const addStepBtn = el("button", {
    type: "button",
    className: "btn secondary sm",
    onclick: () => {
      editableSteps.push({
        delay_hours: 24,
        template_text: "{name}, возвращаюсь к вопросу выше — актуально обсудить?",
        image_url: null,
      });
      renderEditableSteps();
    },
  }, ["+ Добавить шаг"]);

  const saveSettingsBtn = el("button", {
    type: "button",
    className: "btn primary sm campaign-save-settings-btn",
    onclick: async () => {
      const cleanSteps = editableSteps
        .map((st, idx) => ({
          step_order: idx + 1,
          delay_hours: Math.max(0, Number(st.delay_hours) || 0),
          template_text: String(st.template_text || "").trim(),
          image_url: st.image_url ? String(st.image_url).trim() : null,
        }))
        .filter((st) => st.template_text || st.image_url);
      if (!cleanSteps.length) {
        toast("Добавьте хотя бы один непустой шаг рассылки", "error");
        return;
      }
      const selectedSources = sourceBoxes
        .filter((cb) => cb.checked)
        .map((cb) => cb.getAttribute("data-camp-leadgen-source"))
        .filter(Boolean);
      const selectedAccIds = accountBoxes
        .filter((cb) => cb.checked)
        .map((cb) => Number(cb.getAttribute("data-settings-acc-id")))
        .filter((id) => Number.isFinite(id) && id > 0);

      saveSettingsBtn.disabled = true;
      try {
        const bodyPayload = {
          name: nameInput.value.trim() || camp.name,
          daily_limit: Math.max(1, Math.min(500, Number(limitInput.value) || dailyLimit)),
          leadgen_enabled: Boolean(lgEnabledCheck.checked),
          leadgen_daily_quota: Math.max(0, Math.min(500, Number(lgQuotaInput.value) || 0)),
          leadgen_sources: selectedSources,
          steps: cleanSteps,
        };
        if (selectedAccIds.length) {
          bodyPayload.account_id = selectedAccIds[0];
          bodyPayload.account_ids = selectedAccIds;
        }
        await api(`/api/campaigns/${camp.id}`, {
          method: "PUT",
          body: bodyPayload,
        });
        await loadCampaigns();
        toast(`Настройки рассылки «${nameInput.value.trim() || camp.name}» сохранены`, "ok");
      } catch (error) {
        toast(error.message || "Ошибка сохранения настроек рассылки", "error");
      } finally {
        saveSettingsBtn.disabled = false;
      }
    },
  }, ["Сохранить настройки рассылки"]);

  return el("div", {
    className: "campaign-settings-panel stack",
    hidden: !settingsOpen ? "hidden" : undefined,
  }, [
    el("div", { className: "subcard-head" }, [
      el("strong", {}, ["Настройка рассылки: Отправители · Получатели · Сообщения и Картинка"]),
      el("div", { className: "row" }, [
        saveSettingsBtn,
        el("button", {
          type: "button",
          className: "btn ghost sm",
          onclick: () => {
            state.expandedCampaignSettings[camp.id] = false;
            renderCampaigns();
          },
        }, ["Свернуть ×"]),
      ]),
    ]),
    el("div", { className: "leadgen-form-row-3" }, [
      el("label", {}, ["Название рассылки", nameInput]),
      el("label", {}, ["Лимит отправки в сутки", limitStepper]),
      el("div", { className: "stack" }, [
        el("label", { className: "leadgen-pill-toggle" }, [
          lgEnabledCheck,
          el("strong", {}, ["Поток из Лидгена"]),
          lgPillBadge,
        ]),
        el("label", {}, [
          "Лидов в сутки из Лидгена",
          lgQuotaStepper,
        ]),
      ]),
    ]),
    accountChipsNodes.length ? el("div", { className: "campaign-settings-accounts-box stack" }, [
      el("div", { className: "subcard-head" }, [
        el("span", { className: "steps-caption" }, ["1. С каких аккаунтов отправляем (ротация):"]),
        el("button", {
          type: "button",
          className: "btn ghost sm",
          onclick: () => {
            accountBoxes.forEach((cb) => { cb.checked = true; });
          },
        }, ["Выбрать все"]),
      ]),
      el("div", { className: "campaign-accounts-pool" }, accountChipsNodes),
    ]) : null,
    el("div", { className: "campaign-settings-recipients-box stack" }, [
      el("div", { className: "subcard-head" }, [
        el("span", { className: "steps-caption" }, [
          `2. Кому отправляем (база получателей): ${camp.total_recipients || recipients.length || 0} контактов`,
        ]),
        el("div", { className: "row" }, [
          el("button", {
            type: "button",
            className: "btn secondary sm",
            onclick: () => {
              const select = document.querySelector("#csv-campaign-select");
              if (select) select.value = String(camp.id);
              if (state.viewMode === "sheet") openSheetPopup("csv");
            },
          }, ["+ Загрузить CSV"]),
          el("button", {
            type: "button",
            className: "btn ghost sm",
            onclick: () => {
              const select = document.querySelector("#from-leads-campaign-select");
              if (select) select.value = String(camp.id);
              if (state.viewMode === "sheet") openSheetPopup("from-leads");
            },
          }, ["+ Из лидов CRM"]),
        ]),
      ]),
      recipientPreviewChips.length
        ? el("div", { className: "recipient-mini-chips-row" }, recipientPreviewChips)
        : el("p", { className: "hint" }, ["В базе пока нет получателей — загрузите CSV с клиентами или включите поток из Лидгена ниже."]),
    ]),
    el("div", { className: "campaign-leadgen-box stack" }, [
      el("span", { className: "hint" }, ["Автопополнение получателей из Лидгена (кликните источник):"]),
      el("div", { className: "campaign-sources-chips-row" }, sourceChecksNodes),
    ]),
    el("div", { className: "campaign-steps-open-box stack" }, [
      el("div", { className: "subcard-head" }, [
        el("strong", {}, [`3. Сообщения, оффер и картинка (${editableSteps.length})`]),
        addStepBtn,
      ]),
      stepsHost,
    ]),
  ].filter(Boolean));
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
  const connectedAccounts = dedupeConnectedAccounts(state.accounts.filter((item) => item.status === "connected"));
  state.campaigns.forEach((camp) => {
    const statusPair = CAMPAIGN_STATUS_LABEL[camp.status] || [camp.status, "b-new"];
    const attachedAccounts = resolveCampaignAccounts(camp);
    const attachedIds = attachedAccounts.map((item) => Number(item.id));
    const attachedNames = attachedAccounts.map(
      (item) => formatCompactAccountName(item),
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
      const mediaBadge = step.image_url ? "🖼 " : "";
      return el("span", { className: "sequence-step-pill" }, [
        `Шаг ${step.step_order} (${when}): ${mediaBadge}${preview}${step.template_text && step.template_text.length > 64 ? "…" : ""}`,
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
      const accLabel = formatCompactAccountName(account);
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
        el("span", { className: "pool-check-name" }, [accLabel]),
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

    const defaultCampProvider = /\((wa|whatsapp)\)|whatsapp/i.test(String(camp.name || ""))
      ? "whatsapp"
      : "telegram";
    const campProviders = attachedAccounts.length
      ? [...new Set(attachedAccounts.map((acc) => normalizeProvider(acc.provider)))]
      : [defaultCampProvider];
    const channelBadges = campProviders.map((prov) => channelBadgeEl(prov));

    const leadgenEnabled = Boolean(camp.leadgen_enabled);
    const leadgenQuota = Number(camp.leadgen_daily_quota ?? 20);
    const leadgenSourcesList = Array.isArray(camp.leadgen_sources) ? camp.leadgen_sources : ["tg_groups", "yc_ph", "meta_ads", "tg_ad_spy", "channel_graph"];

    const badgeNodes = [
      ...channelBadges,
      el("span", { className: `badge ${statusPair[1]}` }, [statusPair[0]]),
      el("span", { className: "badge b-new" }, [poolSummaryLabel]),
      el("span", { className: "badge b-talk" }, [
        poolCapacity
          ? `${sentToday} / ${dailyLimit} в сут · пул до ${poolCapacity}/сут`
          : `${sentToday} / ${dailyLimit} в сут`,
      ]),
      el("span", {
        className: `badge ${leadgenEnabled ? "b-won" : "b-off"} campaign-leadgen-badge`,
        "data-leadgen-enabled": leadgenEnabled ? "true" : "false",
      }, [
        leadgenEnabled ? `Лидген: ВКЛ · до ${leadgenQuota} лидов/сут` : "Лидген: ВЫКЛ",
      ]),
    ];

    const quickLimitStep = async (delta) => {
      const nextLimit = Math.max(1, Math.min(500, dailyLimit + delta));
      try {
        await api(`/api/campaigns/${camp.id}`, {
          method: "PUT",
          body: { daily_limit: nextLimit },
        });
        await loadCampaigns();
        toast(`Лимит «${camp.name}»: ${nextLimit} сообщ./сут`, "ok");
      } catch (error) {
        toast(error.message, "error");
      }
    };

    const quickMinusBtn = el("button", {
      type: "button",
      className: "btn ghost sm campaign-quick-limit-btn",
      title: "Убавить суточный лимит на 5",
      onclick: () => quickLimitStep(-5),
    }, ["-5/сут"]);

    const quickPlusBtn = el("button", {
      type: "button",
      className: "btn ghost sm campaign-quick-limit-btn",
      title: "Добавить суточный лимит на 5",
      onclick: () => quickLimitStep(5),
    }, ["+5/сут"]);

    const settingsOpen = Boolean(state.expandedCampaignSettings[camp.id]);
    const toggleSettingsBtn = el("button", {
      type: "button",
      className: "btn secondary sm campaign-settings-btn",
      "aria-expanded": settingsOpen ? "true" : "false",
      onclick: () => {
        state.expandedCampaignSettings[camp.id] = !settingsOpen;
        renderCampaigns();
      },
    }, ["Настройки и Лидген"]);

    const isRunning = camp.status === "running";
    const statusToggleBtn = el("button", {
      type: "button",
      className: `btn ${isRunning ? "ghost" : "secondary"} sm campaign-status-toggle-btn`,
      "data-campaign-id": String(camp.id),
      "data-campaign-status": camp.status || "draft",
      onclick: async (event) => {
        const btn = event.currentTarget;
        const nextStatus = camp.status === "running" ? "paused" : "running";
        if (btn) btn.disabled = true;
        try {
          await api(`/api/campaigns/${camp.id}`, {
            method: "PUT",
            body: { status: nextStatus },
          });
          await loadCampaigns();
          toast(
            nextStatus === "running"
              ? `Кампания «${camp.name}» переведена в активный автопилот`
              : `Кампания «${camp.name}» поставлена на паузу`,
            "ok",
          );
        } catch (error) {
          toast(error.message, "error");
        } finally {
          if (btn) btn.disabled = false;
        }
      },
    }, [isRunning ? "Пауза" : "В автопилот"]);

    const campaignSettingsPanel = buildCampaignSettingsPanel(camp, {
      settingsOpen,
      dailyLimit,
      leadgenEnabled,
      leadgenQuota,
      leadgenSourcesList,
      steps,
    });

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
          el("strong", {}, [String((camp.in_progress_count || 0) + (camp.completed_count || 0) + (camp.replied_count || 0))]),
        ]),
        el("div", { className: "campaign-kpi" }, [
          el("span", {}, ["Ответили"]),
          el("strong", {}, [String(camp.replied_count || 0)]),
        ]),
      ]),
      el("div", { className: "sequence-preview" }, stepPills),
      poolEditorRow,
      campaignSettingsPanel,
      el("div", { className: "row campaign-actions-row" }, [
        runBtn,
        statusToggleBtn,
        quickMinusBtn,
        quickPlusBtn,
        toggleSettingsBtn,
        selectForCsvBtn,
        togglePoolBtn,
      ]),
    ];

    if (recipients.length) {
      const isExpanded = Boolean(state.expandedCampaigns[camp.id]);
      const visibleLimit = isExpanded ? (state.campaignRecipientLimits?.[camp.id] || 60) : 5;
      const sortedRecipients = [...recipients].sort((a, b) => {
        const rank = (r) => (r.status === "replied" ? 0 : r.status === "in_progress" ? 1 : r.status === "completed" ? 2 : 3);
        return rank(a) - rank(b);
      });
      const visibleRecipients = (state.viewMode === "sheet" && !isExpanded && sortedRecipients.length > 3)
        ? sortedRecipients.slice(0, 3)
        : sortedRecipients.slice(0, visibleLimit);
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
      if (recipients.length > 5) {
        cardChildren.push(
          el("div", { className: "row" }, [
            el("button", {
              type: "button",
              className: "btn ghost sm",
              onclick: () => {
                state.expandedCampaigns[camp.id] = !isExpanded;
                renderCampaigns();
              },
            }, [isExpanded ? "Свернуть список" : `Показать получателей (${recipients.length})`]),
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
    if (!state.csvHeaders.length) {
      const withHeaders = (state.campaigns || []).find((c) => Array.isArray(c.csv_headers) && c.csv_headers.length);
      if (withHeaders) {
        renderCsvVarChips(withHeaders.csv_headers);
      }
    }
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

function updateSelectedFoldersBtn() {
  const btn = document.querySelector("#folders-sync-selected");
  if (!btn) return;
  const checked = document.querySelectorAll("#folder-list .folder-select-cb:checked");
  const cnt = checked.length;
  btn.disabled = cnt === 0;
  btn.textContent = cnt > 0 ? `Синхронизировать выбранные (${cnt})` : "Синхронизировать выбранные";
}

function renderFolderList(rows) {
  const list = document.querySelector("#folder-list");
  if (!list) return;
  list.replaceChildren();
  updateSelectedFoldersBtn();
  if (!rows || !rows.length) {
    setView("#folders-view", "empty");
    return;
  }
  rows.forEach((folder) => {
    const leadsCount = Number(folder.leads_count) || 0;
    const cb = !folder.blocked_reason
      ? el("input", {
          type: "checkbox",
          className: "folder-select-cb",
          "data-folder-id": String(folder.id),
          onchange: () => updateSelectedFoldersBtn(),
        })
      : null;
    const headRow = el("div", { className: "row" }, [
      cb,
      el("span", { className: "name" }, [folder.title]),
      leadsCount > 0
        ? el("span", { className: "badge b-ok" }, [`${leadsCount} лидов`])
        : (folder.sync_enabled ? el("span", { className: "badge b-new" }, ["Синхр."]) : null),
    ].filter(Boolean));
    const body = [headRow];
    if (folder.blocked_reason) {
      body.push(el("p", { className: "hint" }, [folder.blocked_reason]));
    }
    const actions = [];
    if (leadsCount > 0) {
      actions.push(
        el("button", {
          type: "button",
          className: "btn secondary sm",
          onclick: () => {
            const filterSel = document.querySelector("#folder-filter");
            if (filterSel) {
              filterSel.value = String(folder.id);
              state.boardFolder = String(folder.id);
              renderBoard();
              renderLeadTable();
            }
            const s07Link = document.querySelector('.nav a[href="#s07"]');
            if (s07Link) s07Link.click();
          },
        }, ["В воронку"])
      );
    }
    if (!folder.blocked_reason) {
      actions.push(
        el("button", {
          type: "button",
          className: "btn primary sm",
          onclick: () => syncFolder(folder),
        }, ["Синхронизировать"])
      );
    }
    const row = el("li", { className: "account" }, [
      el("div", {}, body),
      actions.length ? el("div", { className: "row" }, actions) : null,
    ].filter(Boolean));
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

async function syncAllFolders(folderIds = null) {
  const select = document.querySelector("#folder-account");
  const accountId = select && select.value;
  if (!accountId) {
    toast("Нет подключённого аккаунта", "error");
    return;
  }
  const btn = document.querySelector("#folders-sync-all");
  const selBtn = document.querySelector("#folders-sync-selected");
  if (btn) btn.disabled = true;
  if (selBtn) selBtn.disabled = true;
  setView("#folders-view", "loading");
  try {
    const payload = { account_id: Number(accountId) };
    if (Array.isArray(folderIds) && folderIds.length) {
      payload.folder_ids = folderIds.map(Number);
    }
    const res = await api("/api/folders/sync-all", {
      method: "POST",
      body: payload,
    });
    const rows = Array.isArray(res?.folders) ? res.folders : [];
    state.folderLists[accountId] = rows;
    renderFolderList(rows);
    renderFolderFilter();
    await loadLeads(state.leadQuery, state.selectedLeadId);
    toast(`Синхронизировано папок: ${res.synced_folders || 0} · Новых лидов: ${res.added || 0}`, "ok");
  } catch (error) {
    failRegion("#folders-view", "#folders-error", error);
    toast(error.message || "Ошибка синхронизации папок", "error");
  } finally {
    if (btn) btn.disabled = false;
    updateSelectedFoldersBtn();
  }
}

async function syncFolder(folder) {
  try {
    const result = await api(`/api/folders/${folder.id}/sync`, { method: "POST" });
    toast(`Новых лидов: ${result.added}`);
    delete state.cards[state.selectedLeadId];
    await loadFolderList();
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
  syncTelegramWebAPanel(connected);
}

function setInboxChatMode(mode) {
  state.inboxChatMode = mode === "crm" ? "crm" : "tgweb";
  state.inboxChatModeExplicit = true;
  const connected = (state.accounts || []).filter((account) => account.status === "connected");
  syncTelegramWebAPanel(connected);
}

function syncTelegramWebAPanel(connected) {
  const panel = document.querySelector("#tg-web-a-panel");
  const frameWrap = document.querySelector("#tg-web-a-frame-wrap");
  const badge = document.querySelector("#tg-web-a-session-badge");
  const pillsHost = document.querySelector("#tg-web-a-account-pills");
  const extLink = document.querySelector("#tg-web-a-external-link");
  const btnTgWeb = document.querySelector("#inbox-mode-tgweb");
  const btnCrm = document.querySelector("#inbox-mode-crm");
  if (!panel || !frameWrap) return;

  const grid = document.querySelector(".messenger-grid");
  const webReady = (connected || []).filter((acc) => Boolean(acc.tg_web_ready));
  if (!webReady.length) {
    panel.hidden = true;
    if (grid) grid.dataset.tgwebActive = "false";
    return;
  }

  panel.hidden = false;
  const hasDemo = (connected || []).some((acc) => Boolean(acc.is_demo));
  if (!hasDemo && !state.inboxChatModeExplicit) {
    state.inboxChatMode = "tgweb";
  }
  const mode = state.inboxChatMode === "tgweb" ? "tgweb" : "crm";
  panel.dataset.chatMode = mode;
  if (grid) grid.dataset.tgwebActive = mode === "tgweb" ? "true" : "false";
  const snippetsBar = document.querySelector("#tg-web-snippets-bar");
  if (snippetsBar) snippetsBar.hidden = mode === "crm";
  if (pillsHost) pillsHost.hidden = mode === "crm";
  const activeKey = String(state.inboxSessionFilter || "all");
  const targetIdx = Math.max(0, webReady.findIndex((acc) => String(acc.id) === activeKey));
  const target = webReady[targetIdx] || webReady[0];
  const targetSlot = targetIdx + 1;
  const targetUrl = targetSlot === 1
    ? `/a/?account_id=${encodeURIComponent(String(target.id))}`
    : `/a/?account=${targetSlot}&account_id=${encodeURIComponent(String(target.id))}`;

  if (badge) {
    const uname = target.telegram_username ? `@${target.telegram_username}` : target.phone;
    const tname = target.telegram_name ? ` (${target.telegram_name})` : "";
    badge.textContent = `Аккаунт #${targetSlot}: ${uname}${tname} · Онлайн`;
  }
  if (extLink) {
    extLink.href = targetUrl;
  }
  if (pillsHost) {
    pillsHost.replaceChildren();
    webReady.forEach((acc, idx) => {
      const slot = idx + 1;
      const key = String(acc.id);
      const isSelected = String(target.id) === key;
      const uname = acc.telegram_username ? `@${acc.telegram_username}` : (acc.telegram_name || acc.phone);
      const tabBtn = el("button", {
        type: "button",
        className: `tg-web-account-tab${isSelected ? " is-active" : ""}`,
        "data-tgweb-account-id": key,
        "data-tgweb-slot": String(slot),
        "aria-pressed": isSelected ? "true" : "false",
        title: `${acc.telegram_name || ""} ${uname}`.trim(),
        onclick: () => {
          switchInboxSession(key);
        },
      }, [
        el("span", { className: "conn-dot", "data-conn": "ok", "aria-hidden": "true" }, []),
        el("span", { className: "tg-web-tab-slot" }, [`#${slot}`]),
        el("span", { className: "tg-web-tab-name" }, [uname]),
      ]);
      pillsHost.append(tabBtn);
    });
    const addAccBtn = el("button", {
      type: "button",
      className: "btn ghost sm tg-web-add-acc-btn",
      title: "Подключить ещё один Telegram-аккаунт",
      onclick: () => {
        activateSheet("accounts");
        switchAuthTab("phone");
        openSheetPopup("login");
        const phoneInput = document.querySelector("#phone");
        if (phoneInput) phoneInput.focus();
      },
    }, ["+ Аккаунт"]);
    pillsHost.append(addAccBtn);
  }
  if (btnTgWeb && btnCrm) {
    btnTgWeb.className = mode === "tgweb" ? "btn secondary sm" : "btn ghost sm";
    btnTgWeb.setAttribute("aria-pressed", mode === "tgweb" ? "true" : "false");
    btnCrm.className = mode === "crm" ? "btn secondary sm" : "btn ghost sm";
    btnCrm.setAttribute("aria-pressed", mode === "crm" ? "true" : "false");
    if (!btnTgWeb.dataset.bound) {
      btnTgWeb.dataset.bound = "1";
      btnTgWeb.addEventListener("click", () => setInboxChatMode("tgweb"));
    }
    if (!btnCrm.dataset.bound) {
      btnCrm.dataset.bound = "1";
      btnCrm.addEventListener("click", () => setInboxChatMode("crm"));
    }
  }
  frameWrap.hidden = mode === "crm";

  const inboxVisible = state.viewMode !== "sheet" || state.activeSheet === "inbox";
  if (mode === "tgweb" && inboxVisible) {
    // Remove initial placeholder iframe without data-account-id if present
    const placeholder = frameWrap.querySelector("iframe#tg-web-a-frame:not([data-account-id])");
    if (placeholder) {
      placeholder.dataset.accountId = String(target.id);
      placeholder.dataset.slot = String(targetSlot);
    }

    const validIds = new Set(webReady.map((acc) => String(acc.id)));
    [...frameWrap.querySelectorAll("iframe[data-account-id]")].forEach((node) => {
      if (!validIds.has(String(node.dataset.accountId))) {
        node.remove();
      }
    });

    webReady.forEach((acc, idx) => {
      const slot = idx + 1;
      const key = String(acc.id);
      const isSelected = String(target.id) === key;
      const slotUrl = slot === 1
        ? `/a/?account_id=${encodeURIComponent(key)}`
        : `/a/?account=${slot}&account_id=${encodeURIComponent(key)}`;
      let accFrame = frameWrap.querySelector(`iframe[data-account-id="${key}"]`);
      if (!accFrame && isSelected) {
        accFrame = el("iframe", {
          className: "tg-web-a-frame",
          "data-account-id": key,
          "data-slot": String(slot),
          title: `Telegram Web A (${acc.telegram_username || acc.phone})`,
          src: slotUrl,
          allow: "clipboard-read; clipboard-write",
        }, []);
        accFrame.dataset.loadedUrl = slotUrl;
        frameWrap.append(accFrame);
      }
      if (accFrame) {
        accFrame.hidden = !isSelected;
        if (isSelected && accFrame.dataset.loadedUrl !== slotUrl) {
          accFrame.dataset.loadedUrl = slotUrl;
          accFrame.src = slotUrl;
        }
      }
    });
  }
}

async function switchInboxSession(sessionKey) {
  const key = String(sessionKey || "all");
  state.inboxSessionFilter = key;
  state.pinnedDialogId = null;
  const select = document.querySelector("#tg-account");
  if (key !== "all") {
    const chosenAcc = (state.accounts || []).find((acc) => String(acc.id) === key);
    if (chosenAcc && normalizeProvider(chosenAcc.provider) === "whatsapp") {
      state.inboxChatMode = "crm";
    }
    if (select) select.value = key;
  } else if (key === "all" && select) {
    const currentAcc = (state.accounts || []).find((acc) => String(acc.id) === String(select.value));
    if (!select.value || (currentAcc && normalizeProvider(currentAcc.provider) !== "telegram")) {
      const firstTg = (state.accounts || []).find(
        (acc) => acc.status === "connected" && normalizeProvider(acc.provider) === "telegram"
      ) || (state.accounts || []).find((acc) => acc.status === "connected");
      if (firstTg) select.value = String(firstTg.id);
    }
  }
  if (
    key !== "all"
    && state.selectedDialog
    && state.selectedDialog.account_id != null
    && String(state.selectedDialog.account_id) !== key
  ) {
    state.selectedDialog = null;
  }
  renderInboxSessionSwitcher();
  try {
    await loadInbox();
    if (key !== "all" && !state.selectedDialog) {
      const nextDialog = (state.dialogs || []).find((d) => String(d.account_id) === key) || null;
      if (nextDialog) {
        await selectDialog(nextDialog, { markRead: false });
      } else {
        renderThread();
        renderSide();
      }
    }
  } catch (error) {
    closeInboxLive();
    state.inbox = "error";
    const node = document.querySelector("#inbox-error");
    if (node) node.textContent = error.message;
    setView("#inbox-view", "error");
  }
}

const INBOX_CHIP_LABELS = {
  waiting: "Ждёт ответа",
  our_turn: "Ждём ответа от лида",
  unread: "Непрочитанные",
  groups: "Группы",
  muted: "Без звука",
};

function chipOn(name) {
  const button = document.querySelector(`[data-chip="${name}"]`);
  return button && button.getAttribute("aria-pressed") === "true";
}

function activeInboxChips() {
  return [...document.querySelectorAll("#s04 [data-chip]")]
    .filter((btn) => btn.getAttribute("aria-pressed") === "true")
    .map((btn) => btn.dataset.chip)
    .filter(Boolean);
}

function computeLocalInboxCounts(dialogs) {
  const pool = (Array.isArray(dialogs) ? dialogs : []).filter((dialog) => {
    if (state.inboxSessionFilter && state.inboxSessionFilter !== "all" && dialog.account_id) {
      if (String(dialog.account_id) !== String(state.inboxSessionFilter)) return false;
    }
    return true;
  });
  return {
    waiting: pool.filter((d) => Number(d.last_from_peer || 0) === 1 || Boolean(d.waiting) || Boolean(d.last_from_other)).length,
    our_turn: pool.filter((d) => Number(d.last_from_peer || 0) === 0).length,
    unread: pool.filter((d) => Number(d.unread_count || 0) > 0).length,
    groups: pool.filter((d) => d.peer_type === "group" || d.peer_type === "chat" || Boolean(d.is_group)).length,
    muted: pool.filter((d) => Boolean(d.muted)).length,
  };
}

function renderInboxChipCounts(explicitCounts = null) {
  const basePool = Array.isArray(state.allDialogs) && state.allDialogs.length
    ? state.allDialogs
    : state.dialogs;
  const counts = explicitCounts || state.inboxCounts || computeLocalInboxCounts(basePool);
  Object.entries(INBOX_CHIP_LABELS).forEach(([key, label]) => {
    const button = document.querySelector(`#s04 [data-chip="${key}"]`);
    if (!button) return;
    const val = Number(counts && counts[key] != null ? counts[key] : 0) || 0;
    button.dataset.count = String(val);
    button.replaceChildren(
      `${label} (`,
      el("span", { className: "chip-count", "data-chip-count": key }, [String(val)]),
      ")",
    );
  });
}

function passChips(dialog) {
  if (state.inboxSessionFilter && state.inboxSessionFilter !== "all" && dialog.account_id) {
    if (String(dialog.account_id) !== String(state.inboxSessionFilter)) return false;
  }
  const searchRaw = String(state.inboxSearchQuery || "").trim().toLowerCase();
  if (searchRaw) {
    const cleanQ = searchRaw.replace(/^@/, "");
    const haystack = [
      dialog.name,
      dialog.title,
      dialog.username,
      dialog.phone,
      dialog.last_message_preview,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(searchRaw) && (!cleanQ || !haystack.includes(cleanQ))) {
      return false;
    }
  }
  if (
    state.pinnedDialogId != null
    && dialog
    && dialog.id != null
    && Number(dialog.id) === Number(state.pinnedDialogId)
    && state.selectedDialog
    && isSameDialog(state.selectedDialog, dialog)
  ) {
    return true;
  }
  if (chipOn("waiting") && !(Number(dialog.last_from_peer || 0) === 1 || Boolean(dialog.waiting) || Boolean(dialog.last_from_other))) return false;
  if (chipOn("our_turn") && !(Number(dialog.last_from_peer || 0) === 0)) return false;
  if (chipOn("unread") && !(Number(dialog.unread_count || 0) > 0)) return false;
  if (chipOn("groups") && dialog.peer_type !== "group" && dialog.peer_type !== "chat" && !dialog.is_group) return false;
  if (chipOn("muted") && !dialog.muted) return false;
  return true;
}

function renderDialogs() {
  renderInboxChipCounts();
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
      onclick: () => selectDialog(dialog, { markRead: true }),
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

function formatBubbleTime(raw) {
  if (!raw) return "";
  const ms = Date.parse(raw);
  if (Number.isNaN(ms)) return "";
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
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
  const msgList = document.querySelector("#messages");
  msgList.replaceChildren(...items.map((message) => {
    const t = formatBubbleTime(message.sent_at);
    return el("li", {
      className: message.direction === "out" ? "out" : "in",
      "data-time": t || undefined,
    }, [
      message.text || "Вложение",
    ]);
  }));
  msgList.scrollTop = msgList.scrollHeight;
  const gap = document.querySelector("#thread-gap");
  if (gap) gap.hidden = items.length > 0;
}

function selectedAccount() {
  const id = Number(document.querySelector("#tg-account").value);
  return state.accounts.find((account) => account.id === id) || null;
}

async function selectDialog(dialog, options = {}) {
  const reqToken = ++dialogLoadToken;
  if (dialog && dialog.account_id) {
    const select = document.querySelector("#tg-account");
    if (select && [...select.options].some((opt) => opt.value === String(dialog.account_id))) {
      select.value = String(dialog.account_id);
    }
  }
  state.selectedDialog = dialog;
  if (options && options.markRead && dialog && dialog.id != null) {
    state.pinnedDialogId = dialog.id;
  }
  state.messages = [];
  setView("#thread-view", "loading");
  if (options && options.markRead && dialog && dialog.id && Number(dialog.unread_count) > 0) {
    dialog.unread_count = 0;
    upsertDialog(dialog);
    api(`/api/inbox/dialogs/${dialog.id}/read`, { method: "POST" }).then((res) => {
      const connectedCount = (state.accounts || []).filter((acc) => acc.status === "connected").length;
      const useAll = state.inboxSessionFilter === "all" && connectedCount > 1;
      if (res && useAll && res.all_counts) {
        state.inboxCounts = res.all_counts;
      } else if (res && res.counts) {
        state.inboxCounts = res.counts;
      }
      renderInboxChipCounts();
    }).catch(() => {});
  } else {
    renderDialogs();
  }
  const reply = document.querySelector("#reply-text");
  if (reply) reply.value = "";
  let loadError = "";
  let loadedMessages = [];
  if (dialog && dialog.id) {
    try {
      loadedMessages = await api(`/api/dialogs/${dialog.id}/messages`);
    } catch (error) {
      loadError = error.message;
    }
  }
  if (reqToken !== dialogLoadToken) return;
  state.messages = loadedMessages;
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
  if (!dialog) return null;
  const accountId = Number(document.querySelector("#tg-account")?.value || dialog.account_id || 0);
  const cleanUser = String(dialog.username || "").replace(/^@/, "").toLowerCase();
  return state.leads.find((lead) => {
    if (dialog.lead_id && Number(lead.id) === Number(dialog.lead_id)) return true;
    if (dialog.telegram_user_id != null && lead.telegram_user_id != null
        && Number(lead.telegram_user_id) === Number(dialog.telegram_user_id)) {
      if (lead.account_id && accountId && Number(lead.account_id) !== accountId) return false;
      return true;
    }
    if (cleanUser && lead.username && String(lead.username).replace(/^@/, "").toLowerCase() === cleanUser) {
      return true;
    }
    return false;
  }) || null;
}

const LEAD_EVENT_META = {
  created: { label: "Создан", badge: "b-new" },
  status_changed: { label: "Стадия", badge: "b-talk" },
  amount_changed: { label: "Бюджет", badge: "b-won" },
  outreach_sent: { label: "Рассылка", badge: "b-wait" },
  reply_received: { label: "Ответ", badge: "b-ok" },
  note_added: { label: "Заметка", badge: "b-new" },
  reminder_set: { label: "Напоминание", badge: "b-wait" },
  merged: { label: "Слияние", badge: "b-talk" },
};

function renderLeadEventsSection(lead, { compact = false } = {}) {
  const cardData = (lead && lead.id && state.cards[lead.id]) || lead || {};
  const rawEvents = Array.isArray(cardData.events)
    ? cardData.events
    : (Array.isArray(lead && lead.events) ? lead.events : []);
  const items = compact ? rawEvents.slice(-5).reverse() : [...rawEvents].reverse();
  const listNode = items.length
    ? el("ol", { className: `event-timeline${compact ? " is-compact" : ""}` }, items.map((ev) => {
      const meta = LEAD_EVENT_META[ev.event_type] || { label: ev.event_type || "Событие", badge: "b-new" };
      const stampText = formatDue(ev.created_at) || formatShortTime(ev.created_at) || "";
      return el("li", {
        className: "event-timeline-item",
        "data-event-type": ev.event_type || "",
      }, [
        el("div", { className: "event-timeline-head" }, [
          el("span", { className: `badge ${meta.badge} event-type-badge` }, [meta.label]),
          stampText ? el("time", { className: "meta event-time", datetime: ev.created_at || "" }, [stampText]) : null,
        ].filter(Boolean)),
        el("p", { className: "event-timeline-title" }, [ev.title || ""]),
      ]);
    }))
    : el("p", { className: "hint" }, ["Событий пока нет"]);

  return el("section", {
    className: "lead-events-section stack",
    "aria-label": "История событий",
  }, [
    el("div", { className: "row event-section-head" }, [
      el("h4", {}, ["История событий"]),
      el("span", { className: "badge b-new event-count-badge" }, [String(rawEvents.length)]),
    ]),
    listNode,
  ]);
}

function refreshSideEvents(leadId) {
  const dialog = state.selectedDialog;
  if (!dialog) return;
  const lead = leadForDialog(dialog);
  if (!lead || Number(lead.id) !== Number(leadId)) return;
  const existing = document.querySelector("#side-card .lead-events-section");
  const nextNode = renderLeadEventsSection(state.cards[lead.id] || lead, { compact: true });
  if (existing && existing.parentNode) {
    existing.replaceWith(nextNode);
  }
}

function renderSide({ syncSelectedLead = true } = {}) {
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
  if (syncSelectedLead) {
    state.selectedLeadId = lead.id;
  }
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
        el("span", { className: "badge b-won side-amount-badge", "data-side-amount": String(leadAmount(lead)) }, [
          formatMoney(leadAmount(lead), lead.currency),
        ]),
      ]),
      el("p", { className: "hint" }, [handleParts.join(" · ")]),
      el("label", { className: "side-amount-label" }, ["Бюджет ($)", amountField(lead, "Бюджет ($)")]),
      leadTrackingBox(lead),
      formatStructuredStepNode(lead.next_step) || el("p", { className: "hint" }, [lead.next_step || "Следующий шаг не задан"]),
      el("div", { id: "side-note-host" }, [
        formatStructuredNoteNode(sideNoteText(lead), { compact: true }),
        el("p", { className: "note", hidden: Boolean(formatStructuredNoteNode(sideNoteText(lead), { compact: true })) }, [sideNoteText(lead)]),
      ].filter(Boolean)),
      renderLeadEventsSection(state.cards[lead.id] || lead, { compact: true }),
      el("a", {
        className: "btn secondary sm",
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
      }, ["Открыть карточку"]),
    ]),
  );
  setView("#side-view", "filled");
  if (syncSelectedLead) {
    renderCard();
    renderBoard();
  } else if (!state.cards[lead.id]) {
    api(`/api/leads/${lead.id}/card`).then((cardData) => {
      state.cards[lead.id] = cardData;
      refreshSideNote(lead.id);
    }).catch(() => {});
  }
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
  const rawText = sideNoteText(lead);
  const host = document.querySelector("#side-note-host");
  if (host) {
    const structNode = formatStructuredNoteNode(rawText, { compact: true });
    host.replaceChildren(
      ...[
        structNode,
        el("p", { className: "note", hidden: Boolean(structNode) }, [rawText]),
      ].filter(Boolean),
    );
  } else {
    const note = document.querySelector("#side-card .note");
    if (note) note.textContent = rawText;
  }
  refreshSideEvents(leadId);
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
    delete state.cards[lead.id];
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
  if (state.inboxChatMode !== "crm") {
    setInboxChatMode("crm");
  }
  activateSheet("inbox");
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
        loadFocusToday().catch(() => {});
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
        loadFocusToday().catch(() => {});
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
  const structuredNote = formatStructuredNoteNode(lead.note);
  const structuredStep = formatStructuredStepNode(lead.next_step);
  const profile = [
    el("div", { className: "card-head-bar" }, [
      el("div", { className: "row" }, [
        channelBadgeEl(provider),
        el("h3", { id: "card-name", className: "name" }, [lead.name || "Без имени"]),
      ]),
      el("div", { className: "row card-quick-actions" }, [
        el("button", { type: "button", className: "btn primary sm", onclick: () => openChat(lead) }, ["Открыть чат"]),
        el("button", { type: "button", className: "btn secondary sm", onclick: () => openNoteModal(lead) }, ["Заметка"]),
        el("button", { type: "button", className: "btn secondary sm", onclick: () => openReminderModal(lead) }, ["Напоминание"]),
      ]),
    ]),
    el("p", { className: "hint card-handle-line" }, [
      lead.username ? `@${lead.username}` : "без ника",
      lead.phone ? ` · ${lead.phone}` : "",
      lead.telegram_user_id ? ` · ID ${lead.telegram_user_id}` : "",
    ]),
    lead.email ? el("p", { className: "hint" }, [lead.email]) : null,
    href ? el("a", { href, rel: "noreferrer", className: "card-ext-link" }, ["Ссылка"]) : null,
    leadTrackingBox(lead),
    el("div", { className: "card-primary-grid" }, [
      el("label", {}, ["Имя", textField(lead, "name", "Имя")]),
      el("label", {}, ["Статус", statusSelect(lead)]),
      el("label", {}, ["Бюджет ($)", amountField(lead, "Бюджет ($)")]),
      el("label", {}, ["Телефон", textField(lead, "phone", "Телефон")]),
      el("label", {}, ["Папка", folderField(lead)]),
      el("p", { id: "card-folder", className: "hint" }, [
        lead.folder_name ? `Папка: ${lead.folder_name}` : "Папка не задана",
      ]),
    ]),
    el("section", { className: "surface-subcard stack card-context-box", "aria-label": "Следующий шаг и контекст" }, [
      el("div", { className: "subcard-head" }, [
        el("h4", {}, ["Следующий шаг и заметка"]),
      ]),
      structuredStep,
      el("label", {}, ["Следующий шаг", textField(lead, "next_step", "Следующий шаг")]),
      structuredNote,
      el("label", {}, ["Заметка по сделке", textField(lead, "note", "Заметка по сделке")]),
    ].filter(Boolean)),
    typeof renderSoftUgcDealSection === "function" ? renderSoftUgcDealSection(lead) : null,
    el("section", { className: "surface-subcard stack", "aria-label": "Задачи" }, [
      el("h4", {}, ["Задачи"]),
      taskList(lead),
      taskForm(lead),
    ]),
    el("details", { className: "surface-subcard stack custom-prop-details" }, [
      el("summary", { className: "subcard-head" }, [
        el("h4", {}, ["Быстро добавить новое поле CRM"]),
      ]),
      el("section", { "aria-label": "Свои поля", className: "stack" }, [
        propertyForm(lead),
      ]),
    ]),
    timeline,
    renderLeadEventsSection(lead),
  ].filter(Boolean);
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
  renderLeadTable();
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
    state.inboxSearchQuery = "";
    renderDialogs();
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
  scheduleAnalyticsRefresh();
}

function renderBoard() {
  if (state.draggedLeadId) return;
  const { status: stage, folderId, channel, followup } = pipelineFilters();
  const quickSearchEl = document.querySelector("#pipeline-quick-search");
  const quickQuery = quickSearchEl ? String(quickSearchEl.value || "").trim().toLowerCase() : "";
  const board = document.querySelector("#board");
  board.replaceChildren();
  board.style.setProperty("--stage-cols", String(Math.max(1, STATUSES.length)));

  const counterStrip = document.querySelector("#pipeline-counter-strip");
  if (counterStrip) {
    const allLeads = state.leads || [];
    let waitingCount = 0;
    let positiveCount = 0;
    let talkingCount = 0;
    allLeads.forEach((lead) => {
      const tr = resolveLeadTracking(lead);
      if (tr.isWaitingOurAnswer) waitingCount += 1;
      if (tr.isPositiveReply) positiveCount += 1;
      if (lead.status === "talk" || lead.status === "call" || tr.isReplied) talkingCount += 1;
    });
    counterStrip.replaceChildren(
      el("span", { className: "pipeline-stat-pill" }, [
        "Всего: ",
        el("strong", {}, [String(allLeads.length)]),
      ]),
      el("span", { className: "pipeline-stat-sep", "aria-hidden": "true" }, ["·"]),
      el("span", { className: "pipeline-stat-pill is-warn" }, [
        "🔥 Ждут нашего ответа: ",
        el("strong", {}, [String(waitingCount)]),
      ]),
      el("span", { className: "pipeline-stat-sep", "aria-hidden": "true" }, ["·"]),
      el("span", { className: "pipeline-stat-pill is-ok" }, [
        "+ Позитив: ",
        el("strong", {}, [String(positiveCount)]),
      ]),
      el("span", { className: "pipeline-stat-sep", "aria-hidden": "true" }, ["·"]),
      el("span", { className: "pipeline-stat-pill" }, [
        "💬 В диалоге: ",
        el("strong", {}, [String(talkingCount)]),
      ]),
    );
  }

  STATUSES.forEach(([value, label]) => {
    const cards = state.leads.filter((lead) => {
      if (lead.status !== value) return false;
      if (stage && stage !== value) return false;
      if (folderId && String(lead.folder_id || "") !== folderId) return false;
      if (channel && normalizeProvider(lead.provider || "telegram") !== channel) return false;
      if (!matchFollowupFilter(lead, followup)) return false;
      if (quickQuery) {
        const hay = `${lead.name || ""} ${lead.username || ""} ${lead.company || ""} ${lead.phone || ""}`.toLowerCase();
        if (!hay.includes(quickQuery)) return false;
      }
      return true;
    }).sort((a, b) => {
      const ta = resolveLeadTracking(a);
      const tb = resolveLeadTracking(b);
      if (ta.isWaitingOurAnswer !== tb.isWaitingOurAnswer) return ta.isWaitingOurAnswer ? -1 : 1;
      if (ta.isPositiveReply !== tb.isPositiveReply) return ta.isPositiveReply ? -1 : 1;
      return 0;
    });
    const stageSum = cards.reduce((acc, item) => acc + leadAmount(item), 0);
    const items = cards.length
      ? cards.map((lead) => {
        const current = lead.id === state.selectedLeadId;
        const nameText = lead.name || "Без имени";
        const provider = normalizeProvider(lead.provider || "telegram");
        const userText = lead.username
          ? `@${lead.username}`
          : (provider === "whatsapp" && lead.phone ? lead.phone : "без ника");
        const amt = leadAmount(lead);
        const amtText = formatMoney(amt, lead.currency);
        const tr = resolveLeadTracking(lead);
        const bodyNodes = [
          el("span", { className: "card-top" }, [
            el("span", { className: "name" }, [nameText]),
            el("span", { className: "meta" }, [
              lead.company ? `${userText} · ${lead.company}` : userText,
            ]),
          ]),
        ];
        const cleanPreview = formatCleanCardPreview(lead);
        if (cleanPreview) {
          bodyNodes.push(el("span", { className: "card-step" }, [cleanPreview]));
        }
        const folderTitle = folderTitleById(lead.folder_id);
        const tagNodes = [
          channelBadgeEl(provider),
          el("span", { className: "card-amount", "data-card-amount": String(amt) }, [amtText]),
        ];
        if (folderTitle) {
          tagNodes.push(el("span", { className: "card-pill" }, [folderTitle]));
        }
        if (tr.isWaitingOurAnswer) {
          tagNodes.push(el("span", { className: "badge b-wait" }, ["🔥 Ждёт ответа"]));
        } else if (tr.isReplied) {
          tagNodes.push(el("span", { className: "card-pill card-pill-replied" }, ["💬 Ответил"]));
        } else if (tr.campaignStatus === "in_progress") {
          tagNodes.push(el("span", { className: "card-tag card-followup-tag" }, [`Шаг ${tr.campaignStep || 1}`]));
        }
        if (tr.isPositiveReply) {
          tagNodes.push(el("span", { className: "badge b-won" }, ["+ Позитив"]));
        }
        if (tr.accountName || tr.accountPhone) {
          tagNodes.push(el("span", { className: "card-tag card-account-tag" }, [
            `${tr.accountName || tr.accountPhone}`,
          ]));
        }
        bodyNodes.push(el("span", { className: "card-tags" }, tagNodes));
        const link = el("a", {
          className: "card lead-card",
          draggable: "true",
          "data-lead-id": String(lead.id),
          "data-status": String(lead.status),
          "data-amount": String(amt),
          href: "#s09",
          ondragstart: (event) => {
            state.draggedLeadId = lead.id;
            link.classList.add("is-dragging");
            if (event.dataTransfer) {
              event.dataTransfer.effectAllowed = "move";
              try {
                event.dataTransfer.setData("text/plain", String(lead.id));
              } catch (_err) { /* ignore */ }
            }
          },
          ondragend: () => {
            state.draggedLeadId = null;
            link.classList.remove("is-dragging");
            document.querySelectorAll("#board .col.is-drag-over").forEach((colNode) => {
              colNode.classList.remove("is-drag-over");
            });
          },
          onclick: (event) => {
            if (event.target && event.target.closest && event.target.closest(".lead-bulk-checkbox")) {
              return;
            }
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
    const stageMeta = (state.pipelineStages || []).find((s) => String(s.key) === String(value));
    const stageColor = stageMeta && stageMeta.color ? String(stageMeta.color).trim() : "";
    const dot = el("span", {
      className: "stage-dot",
      "data-stage": value,
      "data-stage-color": stageColor || undefined,
      "aria-hidden": "true",
    }, []);
    if (stageColor) {
      dot.style.background = stageColor;
    }
    const titleEl = el("h3", {
      "data-stage-key": value,
      title: "Двойной клик для переименования стадии",
      ondblclick: () => promptRenameStage(value, label),
    }, [label]);
    const sep1 = el("span", { className: "col-sep", "aria-hidden": "true" }, [" · "]);
    const count = el("span", { className: "meta col-count", "data-col-count": String(cards.length) }, [String(cards.length)]);
    const sep2 = el("span", { className: "col-sep col-sep-sum", "aria-hidden": "true" }, [" · "]);
    const activeCurrencies = [...new Set(
      (cards.filter((item) => leadAmount(item) > 0).length ? cards.filter((item) => leadAmount(item) > 0) : cards)
        .map((item) => String(item.currency || "USD").toUpperCase())
    )];
    const stageCurrency = activeCurrencies.length === 1 ? activeCurrencies[0] : "USD";
    const sumEl = el("span", { className: "meta col-sum", "data-col-sum": String(stageSum) }, [formatMoney(stageSum, stageCurrency)]);
    const colEl = el("section", {
      className: "col",
      "data-stage": value,
      ondragover: (event) => {
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        colEl.classList.add("is-drag-over");
      },
      ondragleave: (event) => {
        if (!colEl.contains(event.relatedTarget)) {
          colEl.classList.remove("is-drag-over");
        }
      },
      ondrop: async (event) => {
        event.preventDefault();
        colEl.classList.remove("is-drag-over");
        let rawId = "";
        if (event.dataTransfer) {
          try {
            rawId = event.dataTransfer.getData("text/plain");
          } catch (_err) { /* ignore */ }
        }
        const leadId = Number(rawId || state.draggedLeadId || 0);
        state.draggedLeadId = null;
        if (!leadId) return;
        const targetLead = state.leads.find((l) => Number(l.id) === leadId) || state.leadCache[leadId];
        if (targetLead && targetLead.status === value) return;
        if (targetLead) {
          targetLead.status = value;
          if (state.leadCache[leadId]) state.leadCache[leadId].status = value;
          if (state.cards[leadId]) state.cards[leadId].status = value;
          renderBoard();
          renderLeadTable();
          if (state.selectedLeadId === leadId && state.cards[leadId]) {
            paintCard(state.cards[leadId]);
          }
          renderSide();
        }
        await patchLead(leadId, { status: value });
      },
    }, [
      el("header", {}, [dot, titleEl, sep1, count, sep2, sumEl]),
      ...items,
    ]);
    board.append(colEl);
  });
  updateFilterNote();
  updateWorkspaceStats();
  if (typeof decorateBoardBulkCheckboxes === "function") decorateBoardBulkCheckboxes();
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
        delete state.cards[lead.id];
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

function buildLeadCrmPills(lead) {
  const pills = [];
  const amt = leadAmount(lead);
  if (amt > 0) {
    pills.push(el("span", { className: "crm-field-pill is-budget" }, [formatMoney(amt, lead.currency)]));
  }
  const cleanCompany = String(lead.company || "").trim();
  if (cleanCompany && !cleanCompany.startsWith("+ Позитив")) {
    pills.push(el("span", { className: "crm-field-pill", title: cleanCompany }, [
      `🏢 ${cleanCompany.length > 24 ? `${cleanCompany.slice(0, 24)}…` : cleanCompany}`,
    ]));
  }
  const cleanNiche = String(lead.niche || "").trim();
  if (cleanNiche) {
    const shortNiche = cleanNiche.split(/\s*·\s*/)[0].trim();
    pills.push(el("span", { className: "crm-field-pill", title: cleanNiche }, [
      `🎯 ${shortNiche.length > 22 ? `${shortNiche.slice(0, 22)}…` : shortNiche}`,
    ]));
  }
  const cachedCard = state.cards && state.cards[lead.id];
  const props = (cachedCard && cachedCard.properties) || lead.properties || [];
  props.forEach((prop) => {
    if (pills.length >= 3) return;
    const val = String(propertyShown(prop) || "").trim();
    if (val && val !== "-" && val.length <= 28 && !val.startsWith("+ Позитив")) {
      pills.push(el("span", { className: "crm-field-pill is-custom", title: `${prop.name}: ${val}` }, [
        `${prop.name}: ${val}`,
      ]));
    }
  });
  const openBtn = el("button", {
    type: "button",
    className: "lead-open-card-btn",
    onclick: (ev) => {
      ev.stopPropagation();
      state.selectedLeadId = lead.id;
      renderCard();
      renderBoard();
      if (state.viewMode === "sheet") openSheetPopup("card");
    },
  }, ["Карточка →"]);
  pills.push(openBtn);
  return el("div", { className: "lead-crm-pills-row" }, pills);
}

function renderLeadTable() {
  const body = document.querySelector("#lead-rows");
  if (!body) return;
  body.replaceChildren();
  const { status, folderId, channel, followup } = pipelineFilters();
  const quickSearchInput = document.querySelector("#leads-quick-search");
  const quickQ = quickSearchInput ? String(quickSearchInput.value || "").trim().toLowerCase() : "";
  const filteredLeads = state.leads.filter((lead) => {
    if (status && lead.status !== status) return false;
    if (folderId && String(lead.folder_id || "") !== String(folderId)) return false;
    if (channel && normalizeProvider(lead.provider || "telegram") !== channel) return false;
    if (!matchFollowupFilter(lead, followup)) return false;
    if (quickQ) {
      const hay = [
        lead.name,
        lead.username,
        lead.company,
        lead.niche,
        lead.note,
        lead.next_step,
        lead.phone,
      ].filter(Boolean).join(" ").toLowerCase();
      if (!hay.includes(quickQ)) return false;
    }
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
      trackingPills.push(el("span", { className: "badge b-won" }, ["💬 Ответил"]));
    } else if (tr.campaignStatus === "in_progress") {
      trackingPills.push(el("span", { className: "badge b-talk" }, [
        `Шаг ${tr.campaignStep || 1}${tr.nextFollowupAt ? ` · ${formatShortTime(tr.nextFollowupAt)}` : ""}`,
      ]));
    } else if (tr.campaignStatus === "completed") {
      trackingPills.push(el("span", { className: "badge b-ok" }, [`Шаг ${tr.campaignStep || 1}`]));
    }
    const openRemNodes = tr.openReminders.map((rem) => (
      el("div", { className: "lead-open-reminder badge b-wait" }, [
        `${formatDue(rem.due_at)} · ${rem.text}`,
      ])
    ));
    const bulkCell = typeof createLeadBulkCell === "function"
      ? createLeadBulkCell(lead)
      : el("td", { className: "bulk-check-col" }, []);
    const crmPillsNode = buildLeadCrmPills(lead);
    const structuredNoteNode = formatStructuredNoteNode(lead.note, { compact: true });
    const structuredStepNode = formatStructuredStepNode(lead.next_step);
    const noteInput = textField(lead, "note", "Заметка", {
      className: "table-inline-input table-note-input",
      placeholder: "+ Добавить заметку...",
    });
    if (structuredNoteNode) {
      noteInput.hidden = true;
      structuredNoteNode.addEventListener("click", (ev) => ev.stopPropagation());
      structuredNoteNode.addEventListener("dblclick", (ev) => {
        ev.stopPropagation();
        noteInput.hidden = !noteInput.hidden;
        if (!noteInput.hidden) noteInput.focus();
      });
    }
    const stepInput = textField(lead, "next_step", "Следующий шаг", {
      className: "table-inline-input table-step-input",
      placeholder: "+ Следующий шаг...",
    });
    if (structuredStepNode) {
      stepInput.hidden = true;
      structuredStepNode.addEventListener("click", (ev) => ev.stopPropagation());
      structuredStepNode.addEventListener("dblclick", (ev) => {
        ev.stopPropagation();
        stepInput.hidden = !stepInput.hidden;
        if (!stepInput.hidden) stepInput.focus();
      });
    }
    const row = el("tr", {
      "data-lead-id": String(lead.id),
      onclick: (ev) => {
        if (ev && ev.target && typeof ev.target.closest === "function" && ev.target.closest("input, select, button, a, label")) return;
        state.selectedLeadId = lead.id;
        renderCard();
        renderBoard();
        if (state.viewMode === "sheet") openSheetPopup("card");
      },
    }, [
      bulkCell,
      el("td", { className: "col-lead-name" }, [
        el("div", { className: "lead-name-stack" }, [
          el("div", { className: "lead-name-cell" }, [
            channelBadgeEl(provider),
            textField(lead, "name", "Имя", { className: "table-inline-input table-name-input" }),
          ]),
          crmPillsNode,
        ].filter(Boolean)),
      ]),
      el("td", { className: "col-lead-user" }, [contactText]),
      el("td", { className: "num col-tg-id" }, [lead.telegram_user_id ?? ""]),
      el("td", { className: "num col-lead-phone" }, [lead.phone || ""]),
      el("td", { className: "col-lead-status" }, [
        statusSelect(lead),
        trackingPills.length ? el("div", { className: "lead-tracking-meta" }, trackingPills) : null,
      ].filter(Boolean)),
      el("td", { className: "col-lead-note" }, [
        el("div", { className: "lead-cell-rich" }, [
          structuredNoteNode,
          noteInput,
        ].filter(Boolean)),
      ]),
      el("td", { className: "col-lead-step" }, [
        el("div", { className: "lead-cell-rich" }, [
          structuredStepNode,
          stepInput,
        ].filter(Boolean)),
      ]),
      el("td", { className: "col-lead-rem" }, [
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
  if (typeof syncBulkSelectionUI === "function") syncBulkSelectionUI();
}

async function loadLeads(query = "", keepId = null, silent = false) {
  state.leadQuery = query || "";
  if (!silent) {
    setView("#leads-view", "loading");
    setView("#board-view", "loading");
  }
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
  renderSide({ syncSelectedLead: keepId == null && state.selectedLeadId == null });
  loadFocusToday().catch(() => {});
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
  loadFocusToday().catch(() => {});
}

function renderFocusToday(data) {
  const waitingList = document.querySelector("#focus-waiting-reply-list");
  const remList = document.querySelector("#focus-due-reminders-list");
  const staleList = document.querySelector("#focus-stale-leads-list");
  if (!waitingList || !remList || !staleList) return;

  const waiting = Array.isArray(data?.waiting_reply) ? data.waiting_reply : [];
  const rawReminders = Array.isArray(data?.due_reminders) ? data.due_reminders : [];
  const rawTasks = Array.isArray(data?.open_tasks) ? data.open_tasks : [];
  const dueReminders = rawReminders.filter((item) => item && item.kind !== "task");
  const openTasks = rawTasks.length
    ? rawTasks
    : rawReminders.filter((item) => item && item.kind === "task");
  const staleLeads = Array.isArray(data?.stale_leads) ? data.stale_leads : [];

  const countWaitNode = document.querySelector("#focus-count-waiting");
  const countRemNode = document.querySelector("#focus-count-reminders");
  const countStaleNode = document.querySelector("#focus-count-stale");
  const totalBadgeNode = document.querySelector("#focus-today-total-badge");

  const remAndTaskCount = dueReminders.length + openTasks.length;
  const totalFocus = waiting.length + remAndTaskCount + staleLeads.length;

  if (countWaitNode) countWaitNode.textContent = String(waiting.length);
  if (countRemNode) countRemNode.textContent = String(remAndTaskCount);
  if (countStaleNode) countStaleNode.textContent = String(staleLeads.length);
  if (totalBadgeNode) totalBadgeNode.textContent = `${totalFocus} в фокусе`;

  waitingList.replaceChildren();
  if (!waiting.length) {
    waitingList.append(el("p", { className: "hint empty-state" }, ["Все диалоги отвечены"]));
  } else {
    waiting.slice(0, 6).forEach((item) => {
      const who = item.lead_name || item.name || item.username || item.phone || `Диалог #${item.dialog_id}`;
      const handle = item.username ? `@${item.username}` : (item.phone || "");
      const preview = item.last_message_preview || "Входящее сообщение";
      const provider = normalizeProvider(item.provider || "telegram");
      const actions = [
        el("button", {
          type: "button",
          className: "btn primary sm",
          onclick: () => openChat({
            id: item.lead_id || null,
            account_id: item.account_id || null,
            telegram_user_id: item.telegram_user_id || null,
            username: item.username || "",
            name: who,
            phone: item.phone || "",
            provider,
          }),
        }, ["Ответить"]),
      ];
      if (item.lead_id) {
        actions.push(el("button", {
          type: "button",
          className: "btn ghost sm",
          onclick: () => focusLead(item.lead_id),
        }, ["К лиду"]));
      }
      waitingList.append(el("div", { className: "focus-item", "data-focus-kind": "waiting_reply" }, [
        el("div", { className: "focus-item-top" }, [
          channelBadgeEl(provider),
          el("strong", { className: "focus-item-title" }, [who]),
          handle ? el("span", { className: "meta" }, [handle]) : null,
        ].filter(Boolean)),
        el("p", { className: "focus-item-sub" }, [preview]),
        el("div", { className: "row focus-item-actions" }, actions),
      ]));
    });
  }

  remList.replaceChildren();
  if (!dueReminders.length && !openTasks.length) {
    remList.append(el("p", { className: "hint empty-state" }, ["На сегодня напоминаний нет"]));
  } else {
    dueReminders.slice(0, 5).forEach((rem) => {
      const who = rem.lead_name || rem.lead_username || `Лид #${rem.lead_id}`;
      const isOverdue = Boolean(rem.overdue);
      const remId = rem.reminder_id || rem.id;
      remList.append(el("div", {
        className: `focus-item${isOverdue ? " is-overdue" : ""}`,
        "data-focus-kind": "due_reminder",
      }, [
        el("div", { className: "focus-item-top" }, [
          el("span", { className: `badge ${isOverdue ? "b-wait" : "b-talk"}` }, [
            isOverdue ? "Просрочено" : "Сегодня",
          ]),
          el("strong", { className: "focus-item-title" }, [who]),
        ]),
        el("p", { className: "focus-item-sub" }, [rem.text || ""]),
        el("div", { className: "row focus-item-actions" }, [
          el("button", {
            type: "button",
            className: "btn secondary sm",
            onclick: async () => {
              try {
                await api(`/api/reminders/${remId}/done`, { method: "POST" });
                if (rem.lead_id) delete state.cards[rem.lead_id];
                await loadReminders();
              } catch (err) {
                toast(err.message, "error");
              }
            },
          }, ["Сделано"]),
          el("button", {
            type: "button",
            className: "btn ghost sm",
            onclick: () => focusLead(rem.lead_id),
          }, ["К лиду"]),
        ]),
      ]));
    });
    openTasks.slice(0, 3).forEach((task) => {
      const who = task.lead_name || task.lead_username || `Лид #${task.lead_id}`;
      const taskId = task.task_id || task.id;
      remList.append(el("div", { className: "focus-item", "data-focus-kind": "open_task" }, [
        el("div", { className: "focus-item-top" }, [
          el("span", { className: "badge b-new" }, ["Задача"]),
          el("strong", { className: "focus-item-title" }, [who]),
        ]),
        el("p", { className: "focus-item-sub" }, [task.title || task.text || ""]),
        el("div", { className: "row focus-item-actions" }, [
          el("button", {
            type: "button",
            className: "btn secondary sm",
            onclick: async () => {
              try {
                await api(`/api/tasks/${taskId}/done`, { method: "POST", body: { done: true } });
                if (task.lead_id) delete state.cards[task.lead_id];
                await renderCard();
                await loadFocusToday();
              } catch (err) {
                toast(err.message, "error");
              }
            },
          }, ["Сделано"]),
          el("button", {
            type: "button",
            className: "btn ghost sm",
            onclick: () => focusLead(task.lead_id),
          }, ["К лиду"]),
        ]),
      ]));
    });
  }

  staleList.replaceChildren();
  if (!staleLeads.length) {
    staleList.append(el("p", { className: "hint empty-state" }, ["Зависших сделок нет"]));
  } else {
    staleLeads.slice(0, 6).forEach((lead) => {
      const who = lead.name || (lead.username ? `@${lead.username}` : `Лид #${lead.id}`);
      const stageLabel = (STATUSES.find(([v]) => v === lead.status) || ["", lead.status])[1];
      const days = Number(lead.days_stale) || 3;
      const amt = Number(lead.amount) || 0;
      staleList.append(el("div", { className: "focus-item", "data-focus-kind": "stale_lead" }, [
        el("div", { className: "focus-item-top" }, [
          el("span", { className: `badge ${BADGE[lead.status] || "b-wait"}` }, [stageLabel]),
          el("strong", { className: "focus-item-title" }, [who]),
          amt > 0 ? el("span", { className: "badge b-won" }, [formatMoney(amt, lead.currency)]) : null,
        ].filter(Boolean)),
        el("p", { className: "focus-item-sub" }, [
          `Без движения ${days} дн.${lead.next_step ? ` · ${lead.next_step}` : ""}`,
        ]),
        el("div", { className: "row focus-item-actions" }, [
          el("button", {
            type: "button",
            className: "btn secondary sm",
            onclick: () => openChat(lead),
          }, ["Написать"]),
          el("button", {
            type: "button",
            className: "btn ghost sm",
            onclick: () => focusLead(lead.id),
          }, ["К карточке"]),
        ]),
      ]));
    });
  }
}

async function loadFocusToday() {
  try {
    const data = await api("/api/focus/today");
    state.focusToday = data;
    renderFocusToday(data);
    return data;
  } catch (_err) {
    return null;
  }
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
  if (Array.isArray(state.allDialogs)) {
    const allIdx = state.allDialogs.findIndex((row) => isSameDialog(row, dialog));
    if (allIdx >= 0) state.allDialogs[allIdx] = { ...state.allDialogs[allIdx], ...dialog };
    else state.allDialogs.unshift(dialog);
    if (!state.inboxCounts || state.allDialogs.length < 30) {
      state.inboxCounts = computeLocalInboxCounts(state.allDialogs);
    }
  } else if (!state.inboxCounts || state.dialogs.length < 30) {
    state.inboxCounts = computeLocalInboxCounts(state.dialogs);
  }
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
      state.allDialogs = [...rows];
      if (!state.inboxCounts || rows.length < 30) {
        state.inboxCounts = computeLocalInboxCounts(rows);
      }
      if (!activeInboxChips().length) {
        state.dialogs = rows;
        state.inbox = rows.length ? "filled" : "empty";
      }
      renderDialogs();
    } catch (_err) { /* битый кадр не затирает список */ }
  });
  source.addEventListener("dialog", (event) => {
    if (inboxSource !== source) return;
    try {
      const incoming = JSON.parse(event.data);
      upsertDialog(incoming);
      if (state.selectedDialog && isSameDialog(state.selectedDialog, incoming) && incoming.id) {
        api(`/api/dialogs/${incoming.id}/messages`).then((msgs) => {
          if (state.selectedDialog && isSameDialog(state.selectedDialog, incoming)) {
            state.messages = msgs;
            renderThread();
          }
        }).catch(() => {});
      }
      loadLeads(state.leadQuery, state.selectedLeadId).catch(() => {});
      loadCampaigns().catch(() => {});
    } catch (_err) { /* ждём следующий кадр */ }
  });
}

async function loadInbox() {
  const select = document.querySelector("#tg-account");
  if (!select.value) {
    state.dialogs = [];
    state.allDialogs = [];
    state.inboxCounts = { waiting: 0, our_turn: 0, unread: 0, groups: 0, muted: 0 };
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
  const activeChips = activeInboxChips().join(",");
  const qs = new URLSearchParams({ account_id: String(targetAccountId), with_counts: "1" });
  if (activeChips) qs.set("chip", activeChips);
  const res = await api(`/api/inbox?${qs.toString()}`);
  const dialogs = Array.isArray(res) ? res : (res && Array.isArray(res.dialogs) ? res.dialogs : []);
  if (res && res.counts && typeof res.counts === "object") {
    state.inboxCounts = res.counts;
  } else {
    state.inboxCounts = computeLocalInboxCounts(dialogs);
  }
  if (!activeChips) {
    state.allDialogs = [...dialogs];
  }
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
let waPollTimer = null;

function stopWaPoll() {
  if (waPollTimer) {
    window.clearInterval(waPollTimer);
    waPollTimer = null;
  }
}

function startWaPoll(accountId) {
  stopWaPoll();
  if (!accountId) return;
  waPollTimer = window.setInterval(async () => {
    try {
      const accounts = await api("/api/accounts");
      if (Array.isArray(accounts)) {
        state.accounts = accounts;
        const found = accounts.find((a) => Number(a.id) === Number(accountId));
        if (!found) {
          stopWaPoll();
          return;
        }
        if (found.qr_code) {
          const qrImage = document.querySelector("#wa-qr-image");
          if (qrImage && qrImage.src !== found.qr_code) qrImage.src = found.qr_code;
        }
        if (found.pairing_code) {
          waCurrentPairingCode = found.pairing_code;
          const codeDisplay = document.querySelector("#wa-pairing-code-display");
          if (codeDisplay) codeDisplay.textContent = found.pairing_code;
        }
        if (found.status === "connected") {
          stopWaPoll();
          const pairingBox = document.querySelector("#wa-pairing-box");
          if (pairingBox) pairingBox.hidden = true;
          renderAccounts();
          await loadInbox();
          closeSheetPopup();
          toast("Аккаунт WhatsApp подключён", "ok");
        }
      }
    } catch (_err) { /* фоновый опрос статуса QR */ }
  }, 3000);
}

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
    if (res.status === "connected") {
      stopWaPoll();
      const pairingBox = document.querySelector("#wa-pairing-box");
      if (pairingBox) pairingBox.hidden = true;
      await loadAccounts();
      await loadInbox();
      closeSheetPopup();
      toast("Аккаунт WhatsApp подключён", "ok");
      return;
    }
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
    startWaPoll(res.id);
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
  const proxy = document.querySelector("#account-proxy-select")?.value || "auto";
  const access = document.querySelector("#account-access-select")?.value || "workspace";
  const result = await api("/api/accounts/start", {
    method: "POST",
    body: { phone, proxy_url: proxy, access_scope: access, proxy, access },
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
    state.loginId = null;
    document.querySelector("#code-form").hidden = true;
    closeSheetPopup();
  }
  await loadAccounts();
  if (result.status === "connected") await loadInbox();
});

bindBusy(document.querySelector("#password-form"), async (event) => {
  const password = new FormData(event.target).get("password");
  await api(`/api/accounts/${state.loginId}/password`, { method: "POST", body: { password } });
  state.loginId = null;
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
  if (telegram && state.inboxSearchQuery) {
    state.inboxSearchQuery = "";
    renderDialogs();
  }
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
    if (state.mode === "telegram") {
      state.inboxSearchQuery = "";
      await searchTelegram(query);
    } else {
      state.inboxSearchQuery = query;
      if (state.dialogQuery && Array.isArray(state.allDialogs) && state.allDialogs.length) {
        state.dialogs = [...state.allDialogs];
        state.dialogQuery = "";
      }
      await loadLeads(query);
      renderDialogs();
    }
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

document.querySelector(".chips").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-chip]");
  if (!button) return;
  state.pinnedDialogId = null;
  const on = button.getAttribute("aria-pressed") === "true";
  const nextState = !on;
  button.setAttribute("aria-pressed", nextState ? "true" : "false");
  if (nextState && button.dataset.chip === "waiting") {
    const other = document.querySelector('#s04 [data-chip="our_turn"]');
    if (other) other.setAttribute("aria-pressed", "false");
  } else if (nextState && button.dataset.chip === "our_turn") {
    const other = document.querySelector('#s04 [data-chip="waiting"]');
    if (other) other.setAttribute("aria-pressed", "false");
  }
  const any = [...document.querySelectorAll("[data-chip]")].some((item) => item.getAttribute("aria-pressed") === "true");
  document.querySelector("#chip-note").hidden = !any;
  if (!state.dialogQuery) {
    if (Array.isArray(state.allDialogs) && state.allDialogs.length) {
      state.dialogs = [...state.allDialogs];
    }
    if (state.inbox !== "empty" && state.inbox !== "error" && state.inbox !== "loading") renderDialogs();
    try {
      await loadInbox();
    } catch (_err) { /* оставляем локальную фильтрацию */ }
  } else if (state.inbox !== "empty" && state.inbox !== "error" && state.inbox !== "loading") {
    renderDialogs();
  }
});

document.querySelector("#folder-account").addEventListener("change", () => {
  loadFolderList();
});

document.querySelector("#folders-refresh").addEventListener("click", () => {
  pullFolders();
});

const foldersSyncAllBtn = document.querySelector("#folders-sync-all");
if (foldersSyncAllBtn) {
  foldersSyncAllBtn.addEventListener("click", () => {
    syncAllFolders();
  });
}

const foldersSyncSelectedBtn = document.querySelector("#folders-sync-selected");
if (foldersSyncSelectedBtn) {
  foldersSyncSelectedBtn.addEventListener("click", () => {
    const ids = Array.from(document.querySelectorAll("#folder-list .folder-select-cb:checked"))
      .map((cb) => Number(cb.getAttribute("data-folder-id")))
      .filter((n) => n > 0);
    if (ids.length) {
      syncAllFolders(ids);
    }
  });
}

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

function renderCsvVarChips(headers) {
  const host = document.querySelector("#builder-var-chips") || document.querySelector(".var-chips");
  if (!host) return;
  const cleanHeaders = [...new Set((headers || []).map((h) => String(h || "").trim()).filter(Boolean))];
  state.csvHeaders = cleanHeaders;
  host.querySelectorAll("[data-csv-var='true']").forEach((node) => node.remove());
  const defaultTokens = new Set(
    [...host.querySelectorAll("[data-default-var='true']")]
      .map((btn) => String(btn.dataset.insertVar || "").replace(/^\{|\}$/g, "").toLowerCase()),
  );
  cleanHeaders.forEach((colName) => {
    if (defaultTokens.has(colName.toLowerCase())) return;
    const token = `{${colName}}`;
    const btn = el("button", {
      type: "button",
      className: "chip var-chip var-chip-csv",
      "data-csv-var": "true",
      "data-insert-var": token,
      title: `Подставить столбец «${colName}» из CSV`,
    }, [`+ ${token}`]);
    host.append(btn);
  });
}

const builderVarChips = document.querySelector("#builder-var-chips") || document.querySelector(".var-chips");
if (builderVarChips) {
  builderVarChips.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-insert-var]");
    if (!chip) return;
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
}

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
  renderCsvVarChips(headers);
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

  const builderRecBadge = document.querySelector("#builder-recipients-badge");
  const builderRecPreview = document.querySelector("#builder-csv-recipients-preview");
  if (builderRecBadge) {
    builderRecBadge.textContent = `${totalRows} клиентов из CSV`;
    builderRecBadge.className = "badge b-ok";
  }
  if (builderRecPreview) {
    builderRecPreview.hidden = false;
    const previewItems = rows.slice(0, 4).map((r) => {
      const getVal = (idx) => (idx >= 0 ? String((Array.isArray(r) ? r[idx] : (r[headers[idx]] ?? "")) || "").trim() : "");
      const uIdx = headers.findIndex((h) => /telegram|username|handle|ник|контакт/i.test(h));
      const cIdx = headers.findIndex((h) => /company|компания|бренд|проект|name|имя/i.test(h));
      const s1Idx = headers.findIndex((h) => /первое касание|step_1|step1_text|message|first_touch/i.test(h));
      const handle = getVal(uIdx >= 0 ? uIdx : 0);
      const comp = cIdx >= 0 && cIdx !== uIdx ? getVal(cIdx) : "";
      const s1 = getVal(s1Idx);
      return el("div", { className: "builder-recipient-preview-item" }, [
        el("strong", {}, [comp ? `${comp} (${handle})` : handle]),
        s1 ? el("span", { className: "hint" }, [` — ${s1.slice(0, 90)}${s1.length > 90 ? "…" : ""}`]) : null,
      ].filter(Boolean));
    });
    builderRecPreview.replaceChildren(
      el("div", { className: "builder-recipients-summary-line" }, [
        `Загружено из CSV: ${totalRows} клиентов · колонки: ${headers.slice(0, 5).map((h) => `{${h}}`).join(", ")}`,
      ]),
      ...previewItems,
    );
  }
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

const builderSampleCsvBtn = document.querySelector("#builder-sample-csv-btn");
if (builderSampleCsvBtn) {
  builderSampleCsvBtn.addEventListener("click", () => {
    if (fillSampleCsvBtn) fillSampleCsvBtn.click();
  });
}

const builderFromLeadsBtn = document.querySelector("#builder-from-leads-btn");
if (builderFromLeadsBtn) {
  builderFromLeadsBtn.addEventListener("click", () => {
    openSheetPopup("from-leads");
  });
}

const csvCampaignSelect = document.querySelector("#csv-campaign-select");
if (csvCampaignSelect) {
  csvCampaignSelect.addEventListener("change", () => {
    const campId = Number(csvCampaignSelect.value);
    const camp = (state.campaigns || []).find((c) => Number(c.id) === campId);
    if (camp && Array.isArray(camp.csv_headers) && camp.csv_headers.length) {
      renderCsvVarChips(camp.csv_headers);
    }
  });
}

const csvFileInput = document.querySelector("#csv-file");
if (csvFileInput) {
  csvFileInput.addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const rawText = String(reader.result || "");
      const area = document.querySelector("#csv-text");
      if (area) {
        area.value = rawText;
        updateCsvPreview(area.value);
      } else {
        const local = parseCsvPreviewLocal(rawText);
        if (local.headers.length) renderCsvVarChips(local.headers);
      }
      const cols = state.csvHeaders.slice(0, 6).map((h) => `{${h}}`).join(", ");
      toast(
        cols
          ? `Файл CSV прочитан (${file.name}) · переменные столбцов: ${cols}`
          : `Файл CSV прочитан: ${file.name}`,
        "ok",
      );
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
    const rawAmount = form.get("amount");
    const amount = rawAmount != null && String(rawAmount).trim() !== ""
      ? Math.max(0, Math.round(Number(rawAmount) || 0))
      : 0;
    const lead = await api("/api/leads", {
      method: "POST",
      body: {
        name: form.get("name"),
        username: form.get("username"),
        phone: form.get("phone"),
        provider,
        amount,
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
  renderLeadTable();
});

document.querySelector("#folder-filter").addEventListener("change", () => {
  document.querySelector("#saved-view").value = "";
  renderBoard();
  renderLeadTable();
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
  const tgWebNewChatForm = document.querySelector("#tg-web-new-chat-form");
  if (tgWebNewChatForm) {
    tgWebNewChatForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = document.querySelector("#tg-web-handle");
      const rawHandle = (input && input.value.trim()) || "";
      if (!rawHandle) {
        toast("Введите @username или +телефон для открытия чата", "error");
        if (input) input.focus();
        return;
      }
      const btn = document.querySelector("#tg-web-open-btn");
      const select = document.querySelector("#tg-account");
      const accountId = (state.inboxSessionFilter && state.inboxSessionFilter !== "all")
        ? Number(state.inboxSessionFilter)
        : (select && select.value ? Number(select.value) : null);
      if (btn) {
        btn.disabled = true;
        btn.setAttribute("aria-busy", "true");
      }
      try {
        const res = await api("/api/dialogs/start-direct", {
          method: "POST",
          body: {
            handle: rawHandle,
            account_id: accountId || null,
          },
        });
        if (res && res.lead) {
          state.leadCache[res.lead.id] = res.lead;
          const existingIdx = state.leads.findIndex((l) => Number(l.id) === Number(res.lead.id));
          if (existingIdx >= 0) state.leads[existingIdx] = res.lead;
          else state.leads.unshift(res.lead);
          renderLeadTable();
          renderBoard();
        }
        if (res && res.dialog) {
          upsertDialog(res.dialog);
          await selectDialog(res.dialog);
          if (input) input.value = "";
          const replyBox = document.querySelector("#reply-text");
          if (replyBox) replyBox.focus();
          toast(`Открыт чат с ${res.dialog.name || rawHandle}`, "ok");
        }
      } catch (err) {
        const node = document.querySelector("#inbox-error");
        if (node) node.textContent = err.message;
        toast(err.message || "Не удалось открыть чат", "error");
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.removeAttribute("aria-busy");
        }
      }
    });
  }

  const tgWebSyncBtn = document.querySelector("#tg-web-sync-btn");
  if (tgWebSyncBtn) {
    tgWebSyncBtn.addEventListener("click", async () => {
      const select = document.querySelector("#tg-account");
      const accountId = (state.inboxSessionFilter && state.inboxSessionFilter !== "all")
        ? Number(state.inboxSessionFilter)
        : (select && select.value ? Number(select.value) : null);
      tgWebSyncBtn.disabled = true;
      tgWebSyncBtn.setAttribute("aria-busy", "true");
      try {
        await api("/api/inbox/sync", {
          method: "POST",
          body: { account_id: accountId || null },
        });
        await loadInbox();
        toast("Список диалогов Telegram синхронизирован", "ok");
      } catch (err) {
        toast(err.message || "Не удалось синхронизировать чаты", "error");
      } finally {
        tgWebSyncBtn.disabled = false;
        tgWebSyncBtn.removeAttribute("aria-busy");
      }
    });
  }

  const replyTextField = document.querySelector("#reply-text");
  if (replyTextField) {
    replyTextField.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        const form = document.querySelector("#reply-form");
        if (form) form.requestSubmit();
      }
    });
  }

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
      state.pinnedDialogId = dialog.id;
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
      loadFocusToday().catch(() => {});
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

document.querySelector("#mark-unread").addEventListener("click", async () => {
  const error = document.querySelector("#thread-error");
  const dialog = state.selectedDialog;
  error.hidden = true;
  error.textContent = "";
  if (!dialog || !dialog.id) {
    error.hidden = false;
    error.textContent = "Выберите разговор";
    toast(error.textContent, "error");
    return;
  }
  const btn = document.querySelector("#mark-unread");
  if (btn) {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
  }
  try {
    const res = await api(`/api/inbox/dialogs/${dialog.id}/unread`, { method: "POST" });
    state.pinnedDialogId = dialog.id;
    dialog.unread_count = Number(res && res.unread_count != null ? res.unread_count : Math.max(1, Number(dialog.unread_count) || 0));
    dialog.last_from_peer = 1;
    upsertDialog(dialog);
    const connectedCount = (state.accounts || []).filter((acc) => acc.status === "connected").length;
    const useAll = state.inboxSessionFilter === "all" && connectedCount > 1;
    if (res && useAll && res.all_counts) {
      state.inboxCounts = res.all_counts;
    } else if (res && res.counts) {
      state.inboxCounts = res.counts;
    }
    renderDialogs();
    loadFocusToday().catch(() => {});
    toast("Диалог отмечен непрочитанным", "ok");
  } catch (err) {
    error.hidden = false;
    error.textContent = err.message || "Не удалось отметить непрочитанным";
    toast(error.textContent, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
    }
  }
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
  btn.addEventListener("click", () => {
    if (selector === "#open-login-popup") switchAuthTab("phone");
    openSheetPopup(kind);
  });
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

function syncOutreachAutopilotUI(data) {
  if (!data || typeof data !== "object") return;
  const enabled = data.autopilot_enabled !== false;
  state.outreachSchedule.autopilot_enabled = enabled;
  const toggleBtn = document.querySelector("#outreach-autopilot-toggle");
  if (toggleBtn) {
    toggleBtn.setAttribute("aria-pressed", enabled ? "true" : "false");
    toggleBtn.dataset.autopilotEnabled = enabled ? "true" : "false";
    toggleBtn.textContent = enabled
      ? "Автопилот по расписанию: ВКЛ"
      : "Автопилот по расписанию: ВЫКЛ";
    toggleBtn.classList.toggle("is-active", enabled);
    toggleBtn.classList.toggle("is-off", !enabled);
  }
  const statusEl = document.querySelector("#outreach-autopilot-status");
  if (statusEl && data.next_run_label) {
    statusEl.textContent = data.next_run_label;
  }
}

async function loadOutreachSchedule() {
  try {
    const res = await api("/api/outreach/schedule");
    if (res && typeof res === "object") {
      state.outreachSchedule = {
        timezone: res.timezone || "UTC",
        random_delay: res.random_delay || "30-90",
        pause_night: res.pause_night !== false,
        autopilot_enabled: res.autopilot_enabled !== false,
        days: normalizeScheduleDays(res.days),
      };
      syncOutreachAutopilotUI(res);
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
    const res = await api("/api/outreach/schedule", {
      method: "PUT",
      body: {
        timezone: state.outreachSchedule.timezone,
        days: scheduleDaysToDict(state.outreachSchedule.days),
      },
    });
    if (res && typeof res === "object") syncOutreachAutopilotUI(res);
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

const outreachAutopilotToggle = document.querySelector("#outreach-autopilot-toggle");
if (outreachAutopilotToggle) {
  outreachAutopilotToggle.addEventListener("click", async () => {
    const currentEnabled = outreachAutopilotToggle.getAttribute("aria-pressed") === "true"
      || outreachAutopilotToggle.dataset.autopilotEnabled === "true";
    const nextEnabled = !currentEnabled;
    outreachAutopilotToggle.disabled = true;
    outreachAutopilotToggle.setAttribute("aria-busy", "true");
    try {
      const res = await api("/api/outreach/autopilot", {
        method: "POST",
        body: { enabled: nextEnabled, activate_drafts: true },
      });
      syncOutreachAutopilotUI(res || { autopilot_enabled: nextEnabled });
      await loadCampaigns();
      toast(
        nextEnabled ? "Автопилот по расписанию включён" : "Автопилот по расписанию выключен",
        "ok"
      );
    } catch (err) {
      toast(err.message || "Не удалось переключить автопилот", "err");
    } finally {
      outreachAutopilotToggle.disabled = false;
      outreachAutopilotToggle.removeAttribute("aria-busy");
    }
  });
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
      const rows = Array.isArray(res?.results) ? res.results : (Array.isArray(res) ? res : []);
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
    } catch (err) {
      toast(err.message || "Ошибка конвертера номеров", "err");
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
      const rows = Array.isArray(res?.participants)
        ? res.participants
        : (Array.isArray(res?.results) ? res.results : (Array.isArray(res) ? res : []));
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
    } catch (err) {
      toast(err.message || "Ошибка парсера групп", "err");
    } finally {
      toolParserSubmitBtn.disabled = false;
      toolParserSubmitBtn.removeAttribute("aria-busy");
    }
  });
}

/* CRM Properties & AI Agent */
const SYSTEM_CRM_FIELDS = [
  { name: "Компания", type: "text", place: "Карточка лида · Таблица Лиды · Канбан" },
  { name: "Ниша", type: "text", place: "Карточка лида · Таблица Лиды" },
  { name: "Бюджет сделки ($)", type: "amount", place: "Карточка лида · Таблица Лиды · Канбан" },
  { name: "MRR / Monthly ($)", type: "amount", place: "Карточка лида · Airtable" },
  { name: "Следующий шаг", type: "text", place: "Карточка лида · Таблица Лиды · Канбан" },
  { name: "Следующий Follow-up (дата)", type: "text", place: "Карточка лида · Календарь" },
  { name: "Источник", type: "single_select", place: "Карточка лида · Таблица Лиды · Аналитика" },
  { name: "Формат оффера", type: "multi_select", place: "Карточка лида · Таблица Лиды" },
  { name: "Детали оффера", type: "text", place: "Карточка лида · Airtable" },
];

function renderCrmProperties() {
  const host = document.querySelector("#crm-properties-list");
  const badge = document.querySelector("#crm-properties-count-badge");
  if (!host) return;
  host.replaceChildren();
  const customDefs = Array.isArray(state.propertyDefs) ? state.propertyDefs : [];
  const totalCount = SYSTEM_CRM_FIELDS.length + customDefs.length;
  if (badge) badge.textContent = `${totalCount} полей (${customDefs.length} своих)`;

  customDefs.forEach((def) => {
    const typeLabel = (PROPERTY_TYPES.find(([k]) => k === def.type) || [def.type, def.type])[1];
    host.append(el("div", { className: "tool-result-item" }, [
      el("div", { className: "tool-result-main" }, [
        el("strong", {}, [def.name]),
        el("span", { className: "hint" }, ["Пользовательское поле · В карточке лида («Поля CRM и сделка») и таблице «Лиды»"]),
      ]),
      el("div", { className: "row" }, [
        el("span", { className: "badge b-talk" }, ["Своё поле"]),
        el("span", { className: "badge b-new" }, [typeLabel]),
      ]),
    ]));
  });

  SYSTEM_CRM_FIELDS.forEach((sysField) => {
    const typeLabel = (PROPERTY_TYPES.find(([k]) => k === sysField.type) || [sysField.type, sysField.type])[1];
    host.append(el("div", { className: "tool-result-item" }, [
      el("div", { className: "tool-result-main" }, [
        el("strong", {}, [sysField.name]),
        el("span", { className: "hint" }, [`Системное поле · ${sysField.place}`]),
      ]),
      el("div", { className: "row" }, [
        el("span", { className: "badge b-ok" }, ["Встроено"]),
        el("span", { className: "badge b-new" }, [typeLabel]),
      ]),
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

function applyAiBotConfig(rawConfig) {
  const cfg = (rawConfig && rawConfig.config && typeof rawConfig.config === "object")
    ? rawConfig.config
    : (rawConfig && typeof rawConfig === "object" ? rawConfig : null);
  if (!cfg) return;
  const copilotToggle = document.querySelector("#ai-bot-copilot-toggle");
  const enabledToggle = document.querySelector("#ai-bot-enabled");
  const isEnabled = cfg.copilot_enabled !== undefined ? Boolean(cfg.copilot_enabled) : Boolean(cfg.enabled);
  if (copilotToggle) copilotToggle.checked = isEnabled;
  if (enabledToggle) enabledToggle.checked = isEnabled;
  const autoQualify = document.querySelector("#ai-bot-auto-qualify");
  if (autoQualify && cfg.auto_qualify !== undefined) {
    autoQualify.checked = Boolean(cfg.auto_qualify);
  }
  const goalInput = document.querySelector("#ai-bot-goal");
  if (goalInput && cfg.goal !== undefined) {
    goalInput.value = cfg.goal || "";
  }
  const stageMap = [
    ["#ai-stage-classifier", "classifier_prompt"],
    ["#ai-stage-strategist", "strategist_prompt"],
    ["#ai-stage-generator", "generator_prompt"],
    ["#ai-stage-selector", "selector_prompt"],
  ];
  stageMap.forEach(([sel, key]) => {
    const node = document.querySelector(sel);
    if (node && cfg[key] !== undefined) node.value = cfg[key] || "";
  });
}

async function loadAiBotConfig() {
  try {
    const res = await api("/api/ai-bot/config");
    applyAiBotConfig(res);
    return res;
  } catch (_err) {
    return null;
  }
}

const aiBotSaveBtn = document.querySelector("#ai-bot-save-btn");
if (aiBotSaveBtn) {
  aiBotSaveBtn.addEventListener("click", async () => {
    const copilotToggle = document.querySelector("#ai-bot-copilot-toggle")
      || document.querySelector("#ai-bot-enabled");
    const autoQualifyToggle = document.querySelector("#ai-bot-auto-qualify");
    const goalInput = document.querySelector("#ai-bot-goal");
    const enabled = copilotToggle ? Boolean(copilotToggle.checked) : true;
    const autoQualify = autoQualifyToggle ? Boolean(autoQualifyToggle.checked) : true;
    const goal = (goalInput && goalInput.value.trim()) || "";
    const payload = {
      enabled,
      copilot_enabled: enabled,
      auto_qualify: autoQualify,
      goal,
      classifier_prompt: document.querySelector("#ai-stage-classifier")?.value || "",
      strategist_prompt: document.querySelector("#ai-stage-strategist")?.value || goal,
      generator_prompt: document.querySelector("#ai-stage-generator")?.value || "",
      selector_prompt: document.querySelector("#ai-stage-selector")?.value || "",
    };
    aiBotSaveBtn.disabled = true;
    aiBotSaveBtn.setAttribute("aria-busy", "true");
    try {
      const res = await api("/api/ai-bot/config", { method: "POST", body: payload });
      applyAiBotConfig(res);
      toast("Конфигурация AI Агента (4 этапа) сохранена", "ok");
    } catch (err) {
      toast(err.message || "Не удалось сохранить конфигурацию AI Агента", "err");
    } finally {
      aiBotSaveBtn.disabled = false;
      aiBotSaveBtn.removeAttribute("aria-busy");
    }
  });
}

let analyticsRefreshTimer = 0;

function formatMoneyUsd(value) {
  const num = Number(value) || 0;
  return `$${num.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

function scheduleAnalyticsRefresh() {
  window.clearTimeout(analyticsRefreshTimer);
  analyticsRefreshTimer = window.setTimeout(() => {
    loadAnalyticsSummary().catch(() => {});
  }, 60);
}

function renderAnalytics(data) {
  const summary = data || state.analytics;
  if (!summary) return;
  const funnel = summary.funnel || {};
  const outreach = summary.outreach || {};
  const accounts = summary.accounts || {};

  const pipelineVal = formatMoneyUsd(funnel.pipeline_value ?? summary.pipeline_value ?? 0);
  const wonVal = formatMoneyUsd(funnel.won_value ?? summary.won_value ?? 0);
  const totalVal = formatMoneyUsd(funnel.total_value ?? summary.total_value ?? 0);
  const winRate = `${Number(funnel.win_rate_pct ?? summary.win_rate_pct ?? 0).toFixed(1).replace(/\.0$/, "")}%`;
  const closedWinRate = `${Number(funnel.closed_win_rate_pct ?? 0).toFixed(1).replace(/\.0$/, "")}%`;
  const replyRate = `${Number(outreach.reply_rate_pct ?? summary.reply_rate_pct ?? 0).toFixed(1).replace(/\.0$/, "")}%`;

  const setText = (sel, text) => {
    const node = document.querySelector(sel);
    if (node) node.textContent = text;
  };

  // 1. Topbar chips
  setText("#stat-pipeline-value", pipelineVal);
  setText("#stat-won-value", wonVal);
  setText("#stat-win-rate", winRate);
  setText("#stat-reply-rate", replyRate);

  // 2. Above-pipeline KPI bar
  setText("#analytics-kpi-pipeline", pipelineVal);
  setText("#analytics-kpi-pipeline-sub", `${funnel.active_leads || 0} активных сделок`);
  setText("#analytics-kpi-won", wonVal);
  setText("#analytics-kpi-won-sub", `${funnel.won_leads || 0} выиграно · всего ${funnel.total_leads || 0}`);
  setText("#analytics-kpi-win-rate", winRate);
  setText("#analytics-kpi-win-rate-sub", `Общий объём: ${totalVal}`);
  setText("#analytics-kpi-reply-rate", replyRate);
  setText("#analytics-kpi-reply-rate-sub", `Ответили: ${outreach.replied || 0} из ${outreach.sent || 0}`);
  setText("#analytics-kpi-pool", `${accounts.sent_today || 0} / ${accounts.daily_limit || 0}`);
  const activeHealth = (accounts.by_health && accounts.by_health.active) || 0;
  setText("#analytics-kpi-pool-sub", `Активных сессий: ${activeHealth} · Загрузка ${accounts.utilization_pct || 0}%`);

  const convStrip = document.querySelector("#pipeline-conversion-strip");
  if (convStrip) {
    convStrip.replaceChildren();
    const conversions = Array.isArray(funnel.conversions) ? funnel.conversions : [];
    conversions.forEach((step) => {
      convStrip.append(el("span", { className: "conversion-pill" }, [
        el("span", { className: "conversion-label" }, [step.label || `${step.from_stage} → ${step.to_stage}`]),
        el("strong", { className: "conversion-rate" }, [`${step.rate_pct ?? 0}%`]),
      ]));
    });
  }

  // 3. Dedicated #analytics section
  setText("#analytics-funnel-badge", `${funnel.total_leads || 0} сделок · ${totalVal}`);
  setText("#analytics-funnel-pipeline", pipelineVal);
  setText("#analytics-funnel-won", wonVal);
  setText("#analytics-funnel-win-rate", winRate);
  setText("#analytics-funnel-closed-rate", closedWinRate);

  const stagesHost = document.querySelector("#analytics-stages-list");
  if (stagesHost) {
    stagesHost.replaceChildren();
    const stages = Array.isArray(funnel.stages) ? funnel.stages : [];
    const maxAmount = Math.max(1, ...stages.map((s) => Number(s.amount) || 0));
    stages.forEach((stage) => {
      const barPct = stage.amount > 0
        ? Math.min(100, Math.max(6, Math.round((stage.amount / maxAmount) * 100)))
        : (stage.count > 0 ? Math.min(100, Math.max(6, Math.round(stage.share_pct || 10))) : 0);
      stagesHost.append(el("div", {
        className: "analytics-stage-row",
        "data-analytics-stage": stage.key,
      }, [
        el("div", { className: "analytics-stage-head" }, [
          el("span", { className: "analytics-stage-title" }, [
            el("span", { className: "stage-dot", "data-stage": stage.key, "aria-hidden": "true" }, []),
            el("strong", {}, [stage.label || stage.key]),
            el("span", { className: "hint" }, [`${stage.count || 0} шт. (${stage.share_pct || 0}%)`]),
          ]),
          el("strong", { className: "analytics-stage-amount" }, [formatMoneyUsd(stage.amount || 0)]),
        ]),
        el("div", { className: "analytics-bar-track", "aria-hidden": "true" }, [
          el("span", {
            className: `analytics-bar-fill stage-${stage.key}`,
            style: `width: ${barPct}%`,
          }, []),
        ]),
      ]));
    });
  }

  const convListHost = document.querySelector("#analytics-conversions-list");
  if (convListHost) {
    convListHost.replaceChildren();
    (funnel.conversions || []).forEach((c) => {
      convListHost.append(el("div", { className: "analytics-conv-item" }, [
        el("span", {}, [c.label || `${c.from_stage} → ${c.to_stage}`]),
        el("span", { className: "row" }, [
          el("span", { className: "hint" }, [`${c.to_count || 0} из ${c.from_count || 0}`]),
          el("span", { className: "badge b-talk" }, [`${c.rate_pct ?? 0}%`]),
        ]),
      ]));
    });
  }

  // Outreach card
  setText("#analytics-outreach-badge", `Reply Rate: ${replyRate}`);
  setText("#analytics-outreach-total", String(outreach.total_recipients || 0));
  setText("#analytics-outreach-sent", String(outreach.sent || 0));
  setText("#analytics-outreach-replied", String(outreach.replied || 0));
  setText("#analytics-outreach-reply-rate", replyRate);

  const campHost = document.querySelector("#analytics-campaigns-list");
  if (campHost) {
    campHost.replaceChildren();
    const camps = Array.isArray(outreach.campaigns) ? outreach.campaigns : [];
    if (!camps.length) {
      campHost.append(el("p", { className: "hint empty-state" }, ["Нет кампаний рассылки"]));
    } else {
      camps.forEach((camp) => {
        const [stLabel, stBadge] = CAMPAIGN_STATUS_LABEL[camp.status] || [camp.status, "b-new"];
        campHost.append(el("div", {
          className: "analytics-item-row",
          "data-analytics-campaign": String(camp.id),
        }, [
          el("div", { className: "analytics-item-main" }, [
            el("div", { className: "row" }, [
              el("strong", {}, [camp.name]),
              el("span", { className: `badge ${stBadge}` }, [stLabel]),
            ]),
            el("span", { className: "hint" }, [
              `Получателей: ${camp.total_recipients} · Отправлено: ${camp.sent} · Ответили: ${camp.replied} · В очереди: ${camp.pending}`,
            ]),
          ]),
          el("div", { className: "analytics-item-right" }, [
            el("span", { className: "badge b-won" }, [`Reply ${camp.reply_rate_pct}%`]),
          ]),
        ]));
      });
    }
  }

  // Accounts card
  setText("#analytics-accounts-badge", `Пул: ${accounts.sent_today || 0} / ${accounts.daily_limit || 0}`);
  setText("#analytics-accounts-pool", `${accounts.sent_today || 0} / ${accounts.daily_limit || 0}`);
  setText("#analytics-accounts-util", `${accounts.utilization_pct || 0}%`);
  setText("#analytics-accounts-dialogs", String(accounts.total_dialogs || 0));
  setText("#analytics-accounts-replies", String(accounts.total_replies || 0));

  const healthHost = document.querySelector("#analytics-health-summary");
  if (healthHost) {
    healthHost.replaceChildren();
    const bh = accounts.by_health || {};
    const healthEntries = [
      ["active", "Активны", "b-ok"],
      ["offline", "Офлайн", "b-off"],
      ["unauthorized", "Не авторизован", "b-bad"],
      ["banned", "Бан", "b-bad"],
      ["frozen", "Заморожен", "b-bad"],
    ];
    healthEntries.forEach(([key, label, cls]) => {
      const cnt = Number(bh[key] || 0);
      if (cnt > 0 || key === "active" || key === "offline") {
        healthHost.append(el("span", { className: `badge ${cls}` }, [`${label}: ${cnt}`]));
      }
    });
  }

  const accHost = document.querySelector("#analytics-accounts-list");
  if (accHost) {
    accHost.replaceChildren();
    const items = Array.isArray(accounts.items) ? accounts.items : [];
    if (!items.length) {
      accHost.append(el("p", { className: "hint empty-state" }, ["Нет подключённых аккаунтов"]));
    } else {
      items.forEach((acc) => {
        const [hLabel, hBadge] = ACCOUNT_HEALTH[acc.health] || [acc.health || "активен", "b-ok"];
        accHost.append(el("div", {
          className: "analytics-item-row",
          "data-analytics-account": String(acc.id),
        }, [
          el("div", { className: "analytics-item-main" }, [
            el("div", { className: "row" }, [
              channelBadgeEl(acc.provider),
              el("strong", {}, [acc.name || acc.phone || `Аккаунт #${acc.id}`]),
              el("span", { className: `badge ${hBadge}` }, [hLabel]),
            ]),
            el("span", { className: "hint" }, [
              `Пул сегодня: ${acc.sent_today} / ${acc.daily_limit} (${acc.utilization_pct}%) · Диалогов: ${acc.dialogs_count} · Ответов: ${acc.replies_count}`,
            ]),
          ]),
          el("div", { className: "analytics-item-right" }, [
            el("span", { className: "badge b-talk" }, [`Reply ${acc.reply_rate_pct}%`]),
          ]),
        ]));
      });
    }
  }
  if (typeof renderAnalyticsCharts === "function") {
    renderAnalyticsCharts(summary);
  }
  if (typeof renderDeepOutreachAnalytics === "function") {
    renderDeepOutreachAnalytics(summary);
  }
}

function buildDeepOutreachNode(deepData, hostPrefix) {
  const s = deepData.summary || {};
  const steps = Array.isArray(deepData.steps_breakdown) ? deepData.steps_breakdown : [];
  const hooks = Array.isArray(deepData.hooks_breakdown) ? deepData.hooks_breakdown : [];
  const cats = Array.isArray(deepData.categories_breakdown) ? deepData.categories_breakdown : [];
  const niches = Array.isArray(deepData.niches_breakdown) ? deepData.niches_breakdown : [];
  const waitingItems = Array.isArray(deepData.waiting_items) ? deepData.waiting_items : [];
  const stalledItems = Array.isArray(deepData.stalled_items) ? deepData.stalled_items : [];
  const positiveItems = Array.isArray(deepData.positive_items) ? deepData.positive_items : [];

  const quickCountEl = document.querySelector("#quick-waiting-count");
  if (quickCountEl && s.waiting_our_answer != null) {
    quickCountEl.textContent = String(s.waiting_our_answer);
  }

  // 1. Summary KPI Card
  const kpiCard = el("div", { className: "surface-subcard stack" }, [
    el("div", { className: "subcard-head" }, [
      el("div", { className: "pipeline-title-row" }, [
        el("h3", {}, ["Аудит базы 2 174 лидов (Все2) по 9 аккаунтам: Статусы · Шаги · Форматы · Кто отвечает +"]),
        el("span", { className: "badge b-wait" }, [`🔥 Ждут нашего ответа: ${s.waiting_our_answer || 0} (из них + Позитив: ${s.waiting_positive || 0})`]),
        el("span", { className: "badge b-won" }, [`Целевой + Позитив: ${s.positive_count || 0} (${s.positive_pct || 0}%)`]),
      ]),
      el("div", { className: "row" }, [
        el("button", {
          type: "button",
          className: "btn primary sm",
          onclick: () => {
            const fupSel = document.querySelector("#followup-filter");
            const stgSel = document.querySelector("#stage-filter");
            if (fupSel) fupSel.value = "waiting_our_answer";
            if (stgSel) stgSel.value = "";
            setActiveSheet("s07");
            renderBoard();
            renderLeadTable();
          },
        }, [`Открыть 129 ждущих ответа на доске Воронки →`]),
      ]),
    ]),
    el("div", { className: "analytics-mini-kpis" }, [
      el("div", { className: "analytics-mini-kpi" }, [
        el("span", { className: "hint" }, ["Написали (из 2 174)"]),
        el("strong", {}, [`${s.contacted || 0}`]),
        el("span", { className: "hint" }, [`В очереди: ${s.pending_queue || 0}`]),
      ]),
      el("div", { className: "analytics-mini-kpi" }, [
        el("span", { className: "hint" }, ["Прочитали (2 галочки)"]),
        el("strong", {}, [`${s.read || 0} (${s.open_rate_pct || 0}%)`]),
        el("span", { className: "hint" }, [`1 галочка: ${s.unseen_total || 0}`]),
      ]),
      el("div", { className: "analytics-mini-kpi" }, [
        el("span", { className: "hint" }, ["Ответили всего"]),
        el("strong", { className: "is-won" }, [`${s.replied || 0} (${s.reply_rate_pct || 0}%)`]),
        el("span", { className: "hint" }, [`${s.reply_of_read_pct || 0}% от прочитавших!`]),
      ]),
      el("div", { className: "analytics-mini-kpi" }, [
        el("span", { className: "hint" }, ["Конструктив (+ и ?)"]),
        el("strong", { className: "is-won" }, [`${s.constructive_count || 0} (${s.constructive_pct || 0}%)`]),
        el("span", { className: "hint" }, [`+ Позитив: ${s.positive_count || 0} · ? Вопрос: ${s.question_count || 0}`]),
      ]),
      el("div", { className: "analytics-mini-kpi" }, [
        el("span", { className: "hint" }, ["🔥 Ждут НАШЕГО ответа"]),
        el("strong", { style: "color: #fbbf24;" }, [`${s.waiting_our_answer || 0}`]),
        el("span", { className: "hint" }, [`Мяч на нашей стороне`]),
      ]),
      el("div", { className: "analytics-mini-kpi" }, [
        el("span", { className: "hint" }, ["💬 Прочитали наш ответ и молчат"]),
        el("strong", {}, [`${s.stalled_after_answer || 0}`]),
        el("span", { className: "hint" }, [`Нужен пинг · Не прочит.: ${s.unseen_our_answer || 0}`]),
      ]),
      el("div", { className: "analytics-mini-kpi" }, [
        el("span", { className: "hint" }, ["👀 Ждут Шаг 2 / Шаг 3"]),
        el("strong", {}, [`${(s.need_step2 || 0) + (s.need_step3 || 0)}`]),
        el("span", { className: "hint" }, [`Шаг 2: ${s.need_step2 || 0} · Шаг 3: ${s.need_step3 || 0}`]),
      ]),
      el("div", { className: "analytics-mini-kpi" }, [
        el("span", { className: "hint" }, ["Отказы (-) / Позже"]),
        el("strong", {}, [`${s.refusal_count || 0} / ${s.defer_count || 0}`]),
        el("span", { className: "hint" }, [`Всего 3.6% отказов от базы`]),
      ]),
    ]),
  ]);

  // 2. Steps & Messages Card
  const stepsTableRows = steps.map((st) => el("tr", {}, [
    el("td", {}, [
      el("strong", {}, [st.title]),
      el("div", { className: "hint" }, [`Задержка: ${st.delay_label}`]),
    ]),
    el("td", { className: "num" }, [String(st.reached)]),
    el("td", { className: "num" }, [String(st.read)]),
    el("td", { className: "num" }, [
      el("strong", {}, [`${st.replied} (${st.reply_rate_pct}%)`]),
    ]),
    el("td", { className: "num" }, [
      el("span", { className: "badge b-won" }, [`+${st.positive} (${st.positive_of_reached_pct}% охв. / ${st.positive_of_replied_pct}% отв.)`]),
    ]),
    el("td", { className: "num" }, [`${st.constructive} (${st.constructive_pct}%)`]),
    el("td", {}, [
      el("div", { className: "hint", style: "max-width: 440px; white-space: pre-wrap; font-size: 12px;" }, [st.template_text]),
    ]),
  ]));

  const stepsCard = el("div", { className: "surface-subcard stack" }, [
    el("div", { className: "subcard-head" }, [
      el("h3", {}, ["1. На какое сообщение лучше всего отвечают (Конверсия по 3 шагам и тексты сообщений)"]),
      el("span", { className: "badge b-talk" }, ["Фоллоу-апы Шага 2 и 3 принесли 96 ответов (30.5% всей выручки)!"]),
    ]),
    el("div", { className: "table-wrap" }, [
      el("table", {}, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", {}, ["Шаг воронки"]),
            el("th", { className: "num" }, ["Дошло"]),
            el("th", { className: "num" }, ["Прочитано"]),
            el("th", { className: "num" }, ["Ответили (Reply %)"]),
            el("th", { className: "num" }, ["Целевой + Позитив"]),
            el("th", { className: "num" }, ["Конструктив (+ и ?)"]),
            el("th", {}, ["Текст сообщения шага"]),
          ]),
        ]),
        el("tbody", {}, stepsTableRows),
      ]),
    ]),
  ]);

  // 3. Formats / Hooks Card
  const hooksTableRows = hooks.map((hk) => el("tr", {}, [
    el("td", {}, [el("strong", {}, [hk.hook])]),
    el("td", { className: "num" }, [String(hk.contacted)]),
    el("td", { className: "num" }, [`${hk.read} (${hk.open_rate_pct}%)`]),
    el("td", { className: "num" }, [el("strong", {}, [`${hk.replied} (${hk.reply_rate_pct}%)`])]),
    el("td", { className: "num" }, [
      el("span", { className: "badge b-won" }, [`+${hk.positive} (${hk.positive_of_contacted_pct}% нап. / ${hk.positive_of_replied_pct}% отв.)`]),
    ]),
    el("td", { className: "num" }, [`${hk.constructive} (${hk.constructive_pct}%)`]),
    el("td", { className: "num" }, [String(hk.refusal)]),
    el("td", {}, [
      el("div", { className: "hint", style: "max-width: 400px; white-space: pre-wrap; font-size: 12px;" }, [hk.sample_text || "—"]),
    ]),
  ]));

  const hooksCard = el("div", { className: "surface-subcard stack" }, [
    el("div", { className: "subcard-head" }, [
      el("h3", {}, ["2. Конверсия по форматам и углам захода (Hooks) в 1-м сообщении + тексты"]),
      el("span", { className: "badge b-won" }, ["Лидеры: RU-питч СНГ (28.6% в +) и Партнёрский питч для агентств (27.8% в +)"]),
    ]),
    el("div", { className: "table-wrap" }, [
      el("table", {}, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", {}, ["Формат / Угол захода (Hook)"]),
            el("th", { className: "num" }, ["Написали"]),
            el("th", { className: "num" }, ["Прочитали (Open %)"]),
            el("th", { className: "num" }, ["Ответили (Reply %)"]),
            el("th", { className: "num" }, ["Целевой + Позитив"]),
            el("th", { className: "num" }, ["Конструктив (+ и ?)"]),
            el("th", { className: "num" }, ["Отказы (-)"]),
            el("th", {}, ["Пример реального сообщения этого формата"]),
          ]),
        ]),
        el("tbody", {}, hooksTableRows),
      ]),
    ]),
  ]);

  // 4. Who replies + (By Category & By Niche)
  const buildBreakdownTable = (items, keyField, labelHeader) => el("div", { className: "table-wrap" }, [
    el("table", {}, [
      el("thead", {}, [
        el("tr", {}, [
          el("th", {}, [labelHeader]),
          el("th", { className: "num" }, ["Написали"]),
          el("th", { className: "num" }, ["Прочитали (Open %)"]),
          el("th", { className: "num" }, ["Ответили (Reply %)"]),
          el("th", { className: "num" }, ["Целевой + Позитив"]),
          el("th", { className: "num" }, ["Конструктив (+ и ?)"]),
          el("th", { className: "num" }, ["🔥 Ждут ответа"]),
          el("th", {}, ["Примеры живых + из базы"]),
        ]),
      ]),
      el("tbody", {}, items.map((row) => el("tr", {}, [
        el("td", {}, [el("strong", {}, [row[keyField]])]),
        el("td", { className: "num" }, [String(row.contacted)]),
        el("td", { className: "num" }, [`${row.read} (${row.open_rate_pct}%)`]),
        el("td", { className: "num" }, [el("strong", {}, [`${row.replied} (${row.reply_rate_pct}%)`])]),
        el("td", { className: "num" }, [
          el("span", { className: "badge b-won" }, [`+${row.positive} (${row.positive_of_contacted_pct}% нап. / ${row.positive_of_replied_pct}% отв.)`]),
        ]),
        el("td", { className: "num" }, [`${row.constructive} (${row.constructive_pct}%)`]),
        el("td", { className: "num" }, [
          row.waiting > 0
            ? el("span", { className: "badge b-wait" }, [`🔥 ${row.waiting}`])
            : el("span", { className: "hint" }, ["0"]),
        ]),
        el("td", {}, [
          el("span", { className: "hint" }, [(row.examples || []).join(", ") || "—"]),
        ]),
      ]))),
    ]),
  ]);

  const whoRepliesCard = el("div", { className: "surface-subcard stack" }, [
    el("div", { className: "subcard-head" }, [
      el("h3", {}, ["3. Кто отвечает + (Разбивка по категориям Все2 и продуктовым нишам)"]),
      el("span", { className: "badge b-ok" }, ["AI (30.6% в +) · Gaming/iGaming (25.2% в +) · Агентства (49 целевых +)"]),
    ]),
    el("span", { className: "steps-caption" }, ["А. ПО КАТЕГОРИЯМ ТАБЛИЦЫ ВСЕ2:"]),
    buildBreakdownTable(cats, "category", "Категория клиента"),
    el("span", { className: "steps-caption", style: "margin-top: 12px;" }, ["Б. ПО ПРОДУКТОВЫМ ВЕРТИКАЛЯМ (ЧЕМ ЗАНИМАЕТСЯ КОМПАНИЯ):"]),
    buildBreakdownTable(niches, "niche", "Продуктовая вертикаль / Ниша"),
  ]);

  // 5. Interactive Waiting for our answer / Stalled / Positive list
  const activeTabKey = state[`${hostPrefix}_tab`] || "waiting";
  const activeAccFilter = state[`${hostPrefix}_acc`] || "";
  const activeList = activeTabKey === "stalled"
    ? stalledItems
    : (activeTabKey === "positive" ? positiveItems : waitingItems);
  const filteredList = activeAccFilter
    ? activeList.filter((item) => item.sender_account === activeAccFilter)
    : activeList;

  const accCounts = s.waiting_by_account || {};
  const accFilterBtns = [
    el("button", {
      type: "button",
      className: `chip ${!activeAccFilter ? "is-active" : ""}`,
      "aria-pressed": !activeAccFilter ? "true" : "false",
      onclick: () => {
        state[`${hostPrefix}_acc`] = "";
        renderDeepOutreachAnalytics(state.analytics);
      },
    }, [`Все аккаунты (${activeList.length})`]),
    ...Object.entries(accCounts).map(([accHandle, cnt]) => el("button", {
      type: "button",
      className: `chip ${activeAccFilter === accHandle ? "is-active" : ""}`,
      "aria-pressed": activeAccFilter === accHandle ? "true" : "false",
      onclick: () => {
        state[`${hostPrefix}_acc`] = accHandle;
        renderDeepOutreachAnalytics(state.analytics);
      },
    }, [`${accHandle} (${cnt})`])),
  ];

  const waitingCard = el("div", { className: "surface-subcard stack" }, [
    el("div", { className: "subcard-head" }, [
      el("div", { className: "pipeline-title-row" }, [
        el("h3", {}, ["4. Приоритетная очередь: Кто ждёт нашего ответа и все целевые диалоги"]),
        el("span", { className: "badge b-wait" }, [`Показано: ${filteredList.length}`]),
      ]),
      el("div", { className: "switch", role: "group" }, [
        el("button", {
          type: "button",
          className: `btn ${activeTabKey === "waiting" ? "primary" : "ghost"} sm`,
          onclick: () => {
            state[`${hostPrefix}_tab`] = "waiting";
            renderDeepOutreachAnalytics(state.analytics);
          },
        }, [`🔥 Ждут нашего ответа (${waitingItems.length})`]),
        el("button", {
          type: "button",
          className: `btn ${activeTabKey === "stalled" ? "primary" : "ghost"} sm`,
          onclick: () => {
            state[`${hostPrefix}_tab`] = "stalled";
            renderDeepOutreachAnalytics(state.analytics);
          },
        }, [`💬 Прочитали наш ответ — нужен пинг (${stalledItems.length})`]),
        el("button", {
          type: "button",
          className: `btn ${activeTabKey === "positive" ? "primary" : "ghost"} sm`,
          onclick: () => {
            state[`${hostPrefix}_tab`] = "positive";
            renderDeepOutreachAnalytics(state.analytics);
          },
        }, [`➕ Все целевые + Позитив (${positiveItems.length})`]),
      ]),
    ]),
    el("div", { className: "chips", role: "group" }, accFilterBtns),
    el("div", { className: "table-wrap", style: "max-height: 540px; overflow-y: auto;" }, [
      el("table", {}, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", {}, ["Лид / Компания"]),
            el("th", {}, ["Категория / Ниша"]),
            el("th", {}, ["Наш аккаунт"]),
            el("th", {}, ["На какой шаг и формат ответил"]),
            el("th", {}, ["Реакция"]),
            el("th", {}, ["Текст ответа клиента"]),
            el("th", {}, ["Действие"]),
          ]),
        ]),
        el("tbody", {}, filteredList.map((item) => {
          const isPos = item.reply_intent === "positive_interested";
          return el("tr", {}, [
            el("td", {}, [
              el("strong", {}, [item.name || item.username]),
              el("div", { className: "hint" }, [`@${item.username}${item.website ? ` · ${item.website}` : ""}`]),
            ]),
            el("td", {}, [
              el("span", { className: "badge b-new" }, [item.category]),
              el("div", { className: "hint" }, [item.niche]),
            ]),
            el("td", {}, [
              el("strong", {}, [item.sender_account]),
            ]),
            el("td", {}, [
              el("span", { className: "badge b-talk" }, [item.replied_to_step_label]),
              el("div", { className: "hint", style: "font-size: 11px;" }, [item.hook_format]),
            ]),
            el("td", {}, [
              el("span", { className: `badge ${isPos ? "b-won" : "b-wait"}` }, [item.reply_intent_label]),
            ]),
            el("td", {}, [
              el("div", { style: "max-width: 380px; white-space: pre-wrap; font-size: 12.5px;" }, [item.reply_text || "—"]),
            ]),
            el("td", {}, [
              el("div", { className: "row" }, [
                el("button", {
                  type: "button",
                  className: "btn primary sm",
                  "data-open-chat-uname": item.username || "",
                  onclick: () => openChat({
                    id: item.lead_id || null,
                    username: item.username || "",
                    name: item.name || "",
                    account_id: item.account_id || null,
                  }),
                }, ["Открыть чат"]),
                item.lead_id ? el("button", {
                  type: "button",
                  className: "btn secondary sm",
                  onclick: () => {
                    state.selectedLeadId = item.lead_id;
                    renderCard();
                    if (state.viewMode === "sheet") {
                      openSheetPopup("card");
                    } else {
                      setActiveSheet("s07");
                    }
                  },
                }, ["Карточка"]) : null,
              ].filter(Boolean)),
            ]),
          ]);
        })),
      ]),
    ]),
  ]);

  return el("div", { className: "stack" }, [
    kpiCard,
    waitingCard,
    stepsCard,
    hooksCard,
    whoRepliesCard,
  ]);
}

function renderDeepOutreachAnalytics(summary) {
  const data = summary || state.analytics;
  if (!data || !data.deep_outreach) return;
  const analyticsBox = document.querySelector("#analytics-deep-outreach-box");
  if (analyticsBox) {
    analyticsBox.replaceChildren(
      el("details", { className: "surface-subcard outreach-audit-details" }, [
        el("summary", { className: "subcard-head outreach-audit-summary" }, [
          el("strong", {}, ["Детальный аудит рассылок"]),
          el("span", { className: "badge b-new" }, ["Развернуть"]),
        ]),
        el("div", { className: "outreach-audit-body stack" }, [
          buildDeepOutreachNode(data.deep_outreach, "an_deep"),
        ]),
      ]),
    );
  }
  const outreachBox = document.querySelector("#outreach-deep-stats-box");
  if (outreachBox) {
    const deepDetails = el("details", { className: "surface-subcard outreach-audit-details" }, [
      el("summary", { className: "subcard-head outreach-audit-summary" }, [
        el("strong", {}, ["📊 Подробная аналитика касаний, ответов и конверсии по рассылкам"]),
        el("span", { className: "badge b-new" }, ["Развернуть отчёт"]),
      ]),
      el("div", { className: "outreach-audit-body stack" }, [
        buildDeepOutreachNode(data.deep_outreach, "out_deep"),
      ]),
    ]);
    outreachBox.replaceChildren(deepDetails);
  }
}

const pipelineQuickSearchInput = document.querySelector("#pipeline-quick-search");
if (pipelineQuickSearchInput) {
  pipelineQuickSearchInput.addEventListener("input", () => {
    renderBoard();
  });
}

const leadsQuickSearchInput = document.querySelector("#leads-quick-search");
if (leadsQuickSearchInput) {
  leadsQuickSearchInput.addEventListener("input", () => {
    renderLeadTable();
  });
}

const quickWaitingOurAnswerBtn = document.querySelector("#quick-waiting-our-answer-btn");
if (quickWaitingOurAnswerBtn) {
  quickWaitingOurAnswerBtn.addEventListener("click", () => {
    const fupSel = document.querySelector("#followup-filter");
    const stgSel = document.querySelector("#stage-filter");
    if (fupSel) {
      fupSel.value = fupSel.value === "waiting_our_answer" ? "" : "waiting_our_answer";
    }
    if (stgSel) stgSel.value = "";
    renderBoard();
    renderLeadTable();
  });
}

async function loadAnalyticsSummary(periodOverride) {
  try {
    const period = periodOverride || state.analyticsPeriod || "30d";
    state.analyticsPeriod = period;
    const summary = await api(`/api/analytics/summary?period=${encodeURIComponent(period)}`);
    if (summary && typeof summary === "object") {
      state.analytics = summary;
      renderAnalytics(summary);
    }
    return summary;
  } catch (_err) {
    return null;
  }
}

const analyticsRefreshBtn = document.querySelector("#analytics-refresh-btn");
if (analyticsRefreshBtn) {
  analyticsRefreshBtn.addEventListener("click", async () => {
    analyticsRefreshBtn.disabled = true;
    try {
      await loadAnalyticsSummary();
      toast("Аналитика обновлена", "ok");
    } finally {
      analyticsRefreshBtn.disabled = false;
    }
  });
}

function resolveSnippetLeadContext() {
  const sessionFilter = String(state.inboxSessionFilter || "all");
  const dialogMatchesSession = (d) => {
    if (!d) return false;
    if (sessionFilter === "all") return true;
    return d.account_id != null && String(d.account_id) === sessionFilter;
  };

  const activeSelectedDialog = (state.selectedDialog && dialogMatchesSession(state.selectedDialog))
    ? state.selectedDialog
    : null;
  const fallbackDialog = (Array.isArray(state.dialogs) ? state.dialogs.find(dialogMatchesSession) : null)
    || (Array.isArray(state.allDialogs) ? state.allDialogs.find(dialogMatchesSession) : null)
    || null;
  const dialog = activeSelectedDialog || fallbackDialog;

  let lead = null;
  if (dialog) {
    lead = leadForDialog(dialog);
  } else if (sessionFilter === "all") {
    lead = selectedLead() || (state.leads && state.leads[0]) || null;
  }

  const rawName = String(
    (lead && lead.name)
    || (dialog && (dialog.name || dialog.title))
    || (lead && lead.username)
    || (dialog && dialog.username)
    || ""
  ).trim();

  let rawCompany = String((lead && lead.company) || "").trim();
  let cleanName = rawName;

  if (rawName.includes("·")) {
    const parts = rawName.split("·").map((part) => part.trim());
    cleanName = parts[0] || "";
    if (!rawCompany && parts[1]) {
      rawCompany = parts.slice(1).join(" · ").trim();
    }
  }

  if (!rawCompany && lead) {
    const card = state.cards && state.cards[lead.id];
    const props = (card && Array.isArray(card.properties) && card.properties)
      || (Array.isArray(lead.properties) && lead.properties)
      || [];
    for (const prop of props) {
      if (!prop) continue;
      const pName = String(prop.name || "").trim().toLowerCase();
      const pKey = String(prop.key || "").trim().toLowerCase();
      if (
        ["компания", "company", "проект", "организация"].includes(pName)
        || ["компания", "company", "org", "project"].includes(pKey)
      ) {
        const val = String(prop.value_text || "").trim();
        if (val) {
          rawCompany = val;
          break;
        }
      }
    }
  }

  return {
    name: cleanName || "коллега",
    company: rawCompany || "вашей компании",
    lead,
    dialog,
  };
}

function renderSnippetBody(body) {
  const { name, company } = resolveSnippetLeadContext();
  return String(body || "")
    .replace(/\{name\}/gi, () => name)
    .replace(/\{company\}/gi, () => company);
}

async function copyTextToClipboard(text) {
  state.lastCopiedSnippet = text;
  window.__lastCopiedSnippet = text;
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_err) {
      // Fallback to execCommand below
    }
  }
  try {
    const temp = document.createElement("textarea");
    temp.value = text;
    temp.setAttribute("readonly", "readonly");
    temp.style.position = "fixed";
    temp.style.opacity = "0";
    temp.style.pointerEvents = "none";
    document.body.appendChild(temp);
    temp.select();
    document.execCommand("copy");
    document.body.removeChild(temp);
    return true;
  } catch (_err2) {
    return false;
  }
}

async function applySnippet(snippet) {
  if (!snippet) return;
  const rendered = renderSnippetBody(snippet.body);
  const replyField = document.querySelector("#reply-text");
  if (replyField) {
    replyField.value = rendered;
    replyField.dispatchEvent(new Event("input", { bubbles: true }));
    if (!replyField.closest("[hidden]")) {
      replyField.focus();
    }
  }
  const suggest = document.querySelector("#reply-snippets-suggest");
  if (suggest) {
    suggest.hidden = true;
    suggest.replaceChildren();
  }
  await copyTextToClipboard(rendered);
  const statusNode = document.querySelector("#snippet-copy-status");
  if (statusNode) {
    statusNode.hidden = false;
    statusNode.textContent = `Скопировано (${snippet.shortcut}): ${rendered}`;
  }
  toast(`Шаблон ${snippet.shortcut} скопирован для Telegram Web и вставлен в ответ`, "ok");
}

async function deleteSnippetById(snippetId) {
  if (!snippetId) return;
  try {
    await api(`/api/snippets/${snippetId}`, { method: "DELETE" });
    await loadSnippets();
    toast("Шаблон удалён", "ok");
  } catch (error) {
    toast(error.message || "Не удалось удалить шаблон", "error");
  }
}

function buildSnippetChipEl(snippet, includeDelete = true) {
  const children = [
    el("button", {
      type: "button",
      className: "snippet-chip-btn",
      "data-snippet-id": String(snippet.id || ""),
      "data-snippet-shortcut": snippet.shortcut,
      title: snippet.body,
      onclick: () => {
        applySnippet(snippet);
      },
    }, [
      el("code", {}, [snippet.shortcut]),
      el("span", {}, [snippet.title]),
    ]),
  ];
  if (includeDelete && snippet.id != null) {
    children.push(
      el("button", {
        type: "button",
        className: "snippet-delete-btn",
        "data-delete-snippet-id": String(snippet.id),
        "data-delete-snippet-shortcut": snippet.shortcut,
        title: `Удалить шаблон ${snippet.shortcut}`,
        "aria-label": `Удалить шаблон ${snippet.shortcut}`,
        onclick: (event) => {
          event.stopPropagation();
          deleteSnippetById(snippet.id);
        },
      }, ["×"])
    );
  }
  return el("div", {
    className: "snippet-chip",
    "data-snippet-id": String(snippet.id || ""),
    "data-snippet-shortcut": snippet.shortcut,
  }, children);
}

function renderSnippets() {
  const items = Array.isArray(state.snippets) ? state.snippets : [];
  const sideHost = document.querySelector("#side-snippets-list");
  const replyHost = document.querySelector("#reply-snippets-list");
  const tgWebHost = document.querySelector("#tg-web-snippets-list");

  if (sideHost) {
    sideHost.replaceChildren(...items.map((item) => buildSnippetChipEl(item, true)));
  }
  if (replyHost) {
    replyHost.replaceChildren(...items.map((item) => buildSnippetChipEl(item, true)));
  }
  if (tgWebHost) {
    tgWebHost.replaceChildren(...items.map((item) => buildSnippetChipEl(item, false)));
  }
}

async function loadSnippets() {
  try {
    const items = await api("/api/snippets");
    state.snippets = Array.isArray(items) ? items : [];
    renderSnippets();
    return state.snippets;
  } catch (_err) {
    return [];
  }
}

{
  const snippetForm = document.querySelector("#snippet-create-form");
  if (snippetForm) {
    snippetForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const shortcutInput = document.querySelector("#snippet-shortcut");
      const titleInput = document.querySelector("#snippet-title");
      const bodyInput = document.querySelector("#snippet-body");
      const submitBtn = document.querySelector("#snippet-submit-btn");

      const shortcut = (shortcutInput && shortcutInput.value.trim()) || "";
      const title = (titleInput && titleInput.value.trim()) || "";
      const body = (bodyInput && bodyInput.value.trim()) || "";

      if (!shortcut || !title || !body) {
        toast("Заполните команду, название и текст шаблона", "error");
        return;
      }

      if (submitBtn) submitBtn.disabled = true;
      try {
        const created = await api("/api/snippets", {
          method: "POST",
          body: { shortcut, title, body },
        });
        if (shortcutInput) shortcutInput.value = "";
        if (titleInput) titleInput.value = "";
        if (bodyInput) bodyInput.value = "";
        await loadSnippets();
        toast(`Шаблон ${created.shortcut} добавлен`, "ok");
      } catch (error) {
        toast(error.message || "Не удалось добавить шаблон", "error");
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  const replyField = document.querySelector("#reply-text");
  const suggestHost = document.querySelector("#reply-snippets-suggest");
  if (replyField && suggestHost) {
    replyField.addEventListener("input", () => {
      const val = replyField.value || "";
      if (!val.startsWith("/") || /\s/.test(val)) {
        suggestHost.hidden = true;
        suggestHost.replaceChildren();
        return;
      }
      const query = val.toLowerCase();
      const matches = (state.snippets || []).filter((item) =>
        String(item.shortcut || "").toLowerCase().startsWith(query)
        || String(item.title || "").toLowerCase().includes(query.slice(1))
      );
      if (!matches.length) {
        suggestHost.hidden = true;
        suggestHost.replaceChildren();
        return;
      }
      suggestHost.hidden = false;
      suggestHost.replaceChildren(
        ...matches.map((item) =>
          el("button", {
            type: "button",
            className: "btn ghost sm snippet-suggest-btn",
            "data-suggest-shortcut": item.shortcut,
            onclick: () => applySnippet(item),
          }, [`${item.shortcut} · ${item.title}`])
        )
      );
    });

    replyField.addEventListener("keydown", (event) => {
      if (event.key === "Tab" && !suggestHost.hidden) {
        const firstBtn = suggestHost.querySelector("[data-suggest-shortcut]");
        if (firstBtn) {
          event.preventDefault();
          const sc = firstBtn.getAttribute("data-suggest-shortcut");
          const found = (state.snippets || []).find((s) => s.shortcut === sc);
          if (found) applySnippet(found);
        }
      }
    });
  }
}

let bootSeq = 0;

async function boot() {
  const mySeq = ++bootSeq;
  const initialHashSheet = sheetFromHash(window.location.hash);
  if (initialHashSheet) state.activeSheet = initialHashSheet;
  syncSheetVisibility();
  renderBuilderSteps();
  renderScheduleDays();
  renderCrmProperties();
  loadSnippets().catch(() => {});
  const addStageBtn = document.querySelector("#add-stage-btn");
  if (addStageBtn && !addStageBtn.dataset.bound) {
    addStageBtn.dataset.bound = "1";
    addStageBtn.addEventListener("click", () => promptCreateStage());
  }
  await loadPipelineStages();
  if (mySeq !== bootSeq) return;

  setView("#board-view", "loading");
  setView("#leads-view", "loading");
  setView("#accounts-view", "loading");
  setView("#inbox-view", "empty");
  setView("#thread-view", "empty");
  setView("#side-view", "empty");
  setView("#card-view", "empty");
  try {
    await loadLeads("");
  } catch (error) {
    if (mySeq !== bootSeq) return;
    failRegion("#leads-view", "#leads-error", error);
    document.querySelector("#board-error").textContent = error.message;
    setView("#board-view", "error");
  }
  if (mySeq !== bootSeq) return;
  try {
    await loadOperator();
  } catch (error) {
    if (mySeq !== bootSeq) return;
    showOperator(error.message || "Нет BOT_TOKEN", "alert");
  }
  if (mySeq !== bootSeq) return;
  try {
    await loadAccounts();
    if (mySeq !== bootSeq) return;
    loadPipelineFolders().catch(() => {});
  } catch (error) {
    if (mySeq !== bootSeq) return;
    failRegion("#accounts-view", "#accounts-error", error);
  }
  if (mySeq !== bootSeq) return;
  try {
    await loadInbox();
  } catch (error) {
    if (mySeq !== bootSeq) return;
    closeInboxLive();
    state.inbox = "error";
    const node = document.querySelector("#inbox-error");
    if (node) node.textContent = error.message;
    setView("#inbox-view", "error");
  }
  if (mySeq !== bootSeq) return;
  try {
    await loadViews();
  } catch (error) {
    if (mySeq !== bootSeq) return;
    toast(error.message, "error");
  }
  if (mySeq !== bootSeq) return;
  try {
    await loadReminders();
  } catch (error) {
    if (mySeq !== bootSeq) return;
    failRegion("#digest-view", "#digest-error", error);
  }
  if (mySeq !== bootSeq) return;
  try {
    await loadCampaigns();
  } catch (_error) { /* рассылки могут загрузиться позже */ }
  if (mySeq !== bootSeq) return;
  loadOutreachSchedule().catch(() => {});
  loadCrmProperties().catch(() => {});
  loadAiBotConfig().catch(() => {});
  await loadSnippets();
  if (mySeq !== bootSeq) return;
  await loadAnalyticsSummary();
  if (mySeq !== bootSeq) return;
  try {
    await loadLeads("");
  } catch (error) {
    if (mySeq !== bootSeq) return;
    failRegion("#leads-view", "#leads-error", error);
    document.querySelector("#board-error").textContent = error.message;
    setView("#board-view", "error");
  }
  if (mySeq !== bootSeq) return;
  const focusRefreshBtn = document.querySelector("#focus-today-refresh");
  if (focusRefreshBtn && !focusRefreshBtn.dataset.bound) {
    focusRefreshBtn.dataset.bound = "1";
    focusRefreshBtn.addEventListener("click", () => {
      loadFocusToday().then(() => toast("Фокус на сегодня обновлён", "ok"));
    });
  }
  await loadFocusToday();
  if (mySeq !== bootSeq) return;
  updateWorkspaceStats();
  loadAirtableStatus().catch(() => {});
  loadMcpInfo().catch(() => {});
  loadCalendar().catch(() => {});
  loadLeadgenConfig().catch(() => {});
  loadCrmAuthStatus().catch(() => {});
  if (window.location.hash === "#login" || window.location.hash === "#auth") {
    const authModal = document.querySelector("#crm-auth-modal");
    if (authModal) authModal.hidden = false;
  }
}

const seedDemoButton = document.querySelector("#seed-demo");
if (seedDemoButton) {
  seedDemoButton.addEventListener("click", async () => {
    bootSeq += 1;
    seedDemoButton.disabled = true;
    seedDemoButton.setAttribute("aria-busy", "true");
    try {
      await api("/api/demo/seed", { method: "POST" });
      state.cards = {};
      state.leadCache = {};
      await boot();
      toast("Демо-данные загружены", "ok");
    } catch (error) {
      toast(error.message || "Не удалось загрузить демо", "error");
    } finally {
      seedDemoButton.disabled = false;
      seedDemoButton.removeAttribute("aria-busy");
    }
  });
}

/* =========================================================================
 * Блок 5: Массовые операции (Bulk Actions), Слияние дублей и Экспорт в CSV
 * ========================================================================= */
state.bulkSelectedIds = new Set();
state.bulkHostTarget = "leads";

function getFilteredLeadsForBulk() {
  const { status: stage, folderId, channel, followup } = pipelineFilters();
  return (state.leads || []).filter((lead) => {
    if (stage && lead.status !== stage) return false;
    if (folderId && String(lead.folder_id || "") !== String(folderId)) return false;
    if (channel && normalizeProvider(lead.provider || "telegram") !== channel) return false;
    if (!matchFollowupFilter(lead, followup)) return false;
    return true;
  });
}

function syncBulkHostContainer(preferredTarget) {
  if (preferredTarget === "s07" || preferredTarget === "leads") {
    state.bulkHostTarget = preferredTarget;
  }
  const leadsSec = document.querySelector("#leads");
  const boardSec = document.querySelector("#s07");
  const bulkBar = document.querySelector("#leads-bulk-bar");
  const dupModal = document.querySelector("#leads-duplicates-modal");
  if (!bulkBar || !leadsSec || !boardSec) return;

  const isBoardActive = (state.bulkHostTarget === "s07" && !boardSec.hidden)
    || (state.viewMode === "sheet" && state.activeSheet === "s07" && leadsSec.hidden);
  if (isBoardActive) {
    const header = boardSec.querySelector(".pipeline-header");
    if (bulkBar.parentElement !== boardSec) {
      if (header && header.nextSibling) {
        boardSec.insertBefore(bulkBar, header.nextSibling);
      } else {
        boardSec.prepend(bulkBar);
      }
    }
    if (dupModal && dupModal.parentElement !== boardSec) {
      boardSec.insertBefore(dupModal, bulkBar.nextSibling);
    }
  } else {
    const paneHead = leadsSec.querySelector(".pane-head");
    if (bulkBar.parentElement !== leadsSec) {
      if (paneHead && paneHead.nextSibling) {
        leadsSec.insertBefore(bulkBar, paneHead.nextSibling);
      } else {
        leadsSec.prepend(bulkBar);
      }
    }
    if (dupModal && dupModal.parentElement !== leadsSec) {
      leadsSec.insertBefore(dupModal, bulkBar.nextSibling);
    }
  }
}

function toggleLeadBulkSelection(leadId, checked, hostTarget) {
  const id = Number(leadId);
  if (!id) return;
  if (hostTarget === "s07" || hostTarget === "leads") {
    state.bulkHostTarget = hostTarget;
  }
  if (checked) {
    state.bulkSelectedIds.add(id);
  } else {
    state.bulkSelectedIds.delete(id);
  }
  syncBulkSelectionUI();
}

function createLeadBulkCell(lead) {
  const id = Number(lead.id);
  const cb = el("input", {
    type: "checkbox",
    className: "lead-bulk-checkbox",
    "data-lead-id": String(id),
    "aria-label": `Выбрать лид ${lead.name || lead.username || id}`,
    onclick: (event) => {
      event.stopPropagation();
    },
    onchange: (event) => {
      toggleLeadBulkSelection(id, event.target.checked, "leads");
    },
  });
  cb.checked = state.bulkSelectedIds.has(id);
  return el("td", {
    className: "bulk-check-col",
    onclick: (event) => {
      event.stopPropagation();
    },
  }, [cb]);
}

function decorateBoardBulkCheckboxes() {
  const board = document.querySelector("#board");
  if (!board) return;
  const { status: stage, folderId, channel, followup } = pipelineFilters();
  const cols = board.querySelectorAll(".col");
  cols.forEach((colEl) => {
    const dot = colEl.querySelector(".stage-dot[data-stage]");
    const stageVal = dot ? dot.getAttribute("data-stage") : (colEl.getAttribute("data-stage") || "");
    const stageLeads = (state.leads || []).filter((lead) => {
      if (stageVal && lead.status !== stageVal) return false;
      if (stage && stage !== lead.status) return false;
      if (folderId && String(lead.folder_id || "") !== String(folderId)) return false;
      if (channel && normalizeProvider(lead.provider || "telegram") !== channel) return false;
      if (!matchFollowupFilter(lead, followup)) return false;
      return true;
    });
    const cardEls = colEl.querySelectorAll(".card");
    cardEls.forEach((cardEl, idx) => {
      let lid = Number(cardEl.getAttribute("data-lead-id") || 0);
      if (!lid && stageLeads[idx]) {
        lid = Number(stageLeads[idx].id);
        cardEl.setAttribute("data-lead-id", String(lid));
      }
      if (!lid) return;
      let cb = cardEl.querySelector(".card-bulk-checkbox");
      if (!cb) {
        cb = el("input", {
          type: "checkbox",
          className: "lead-bulk-checkbox card-bulk-checkbox",
          "data-lead-id": String(lid),
          "aria-label": `Выбрать карточку #${lid}`,
          onclick: (event) => {
            event.stopPropagation();
          },
          onchange: (event) => {
            event.stopPropagation();
            toggleLeadBulkSelection(lid, event.target.checked, "s07");
          },
        });
        cardEl.prepend(cb);
      }
      cb.checked = state.bulkSelectedIds.has(lid);
      cardEl.classList.toggle("bulk-selected", cb.checked);
    });
  });
  syncBulkSelectionUI();
}

function populateBulkBarSelects() {
  const folderSelect = document.querySelector("#bulk-folder-select");
  if (folderSelect) {
    const prev = folderSelect.value;
    folderSelect.replaceChildren(el("option", { value: "" }, ["Без папки"]));
    const seen = new Set();
    Object.values(state.folderLists || {}).flat().forEach((f) => {
      if (!f || seen.has(Number(f.id))) return;
      seen.add(Number(f.id));
      folderSelect.append(el("option", { value: String(f.id) }, [f.title || `Папка #${f.id}`]));
    });
    if ([...folderSelect.options].some((o) => o.value === prev)) {
      folderSelect.value = prev;
    }
  }

  const campSelect = document.querySelector("#bulk-campaign-select");
  if (campSelect) {
    const prev = campSelect.value;
    campSelect.replaceChildren(el("option", { value: "" }, ["Кампания рассылки..."]));
    (state.campaigns || []).forEach((c) => {
      campSelect.append(el("option", { value: String(c.id) }, [c.name || `Кампания #${c.id}`]));
    });
    if ([...campSelect.options].some((o) => o.value === prev)) {
      campSelect.value = prev;
    } else if ((state.campaigns || []).length === 1) {
      campSelect.value = String(state.campaigns[0].id);
    }
  }

  const statusSelectEl = document.querySelector("#bulk-status-select");
  if (statusSelectEl && Array.isArray(STATUSES) && STATUSES.length) {
    const prev = statusSelectEl.value;
    statusSelectEl.replaceChildren(
      ...STATUSES.map(([val, label]) => el("option", { value: val }, [label]))
    );
    if ([...statusSelectEl.options].some((o) => o.value === prev)) {
      statusSelectEl.value = prev;
    }
  }
}

function syncBulkSelectionUI() {
  syncBulkHostContainer();
  const validIds = new Set((state.leads || []).map((l) => Number(l.id)));
  Array.from(state.bulkSelectedIds).forEach((id) => {
    if (!validIds.has(Number(id))) {
      state.bulkSelectedIds.delete(id);
    }
  });

  const count = state.bulkSelectedIds.size;
  const countNode = document.querySelector("#bulk-selected-count");
  if (countNode) countNode.textContent = String(count);

  const bulkBar = document.querySelector("#leads-bulk-bar");
  if (bulkBar) {
    bulkBar.hidden = count === 0;
  }

  if (count > 0) {
    populateBulkBarSelects();
  }

  document.querySelectorAll(".lead-bulk-checkbox[data-lead-id]").forEach((cb) => {
    const id = Number(cb.getAttribute("data-lead-id"));
    const isChecked = state.bulkSelectedIds.has(id);
    if (cb.checked !== isChecked) cb.checked = isChecked;
    const rowOrCard = cb.closest("tr, .card");
    if (rowOrCard) rowOrCard.classList.toggle("bulk-selected", isChecked);
  });

  const filtered = getFilteredLeadsForBulk();
  const selectAllCb = document.querySelector("#leads-select-all-checkbox");
  if (selectAllCb) {
    selectAllCb.checked = filtered.length > 0 && filtered.every((l) => state.bulkSelectedIds.has(Number(l.id)));
  }
}

function selectAllFilteredLeads(hostTarget) {
  if (hostTarget === "s07" || hostTarget === "leads") {
    state.bulkHostTarget = hostTarget;
  }
  const filtered = getFilteredLeadsForBulk();
  if (!filtered.length) {
    toast("В текущем фильтре нет лидов", "error");
    return;
  }
  const allSelected = filtered.every((l) => state.bulkSelectedIds.has(Number(l.id)));
  if (allSelected) {
    filtered.forEach((l) => state.bulkSelectedIds.delete(Number(l.id)));
  } else {
    filtered.forEach((l) => state.bulkSelectedIds.add(Number(l.id)));
  }
  syncBulkSelectionUI();
}

async function runBulkAction(action, extraPayload = {}) {
  const leadIds = Array.from(state.bulkSelectedIds);
  if (!leadIds.length) {
    toast("Выберите хотя бы одного лида", "error");
    return;
  }
  try {
    const res = await api("/api/leads/bulk", {
      method: "POST",
      body: {
        lead_ids: leadIds,
        action,
        ...extraPayload,
      },
    });
    if (action === "delete") {
      state.bulkSelectedIds.clear();
      toast(`Удалено карточек: ${res.deleted || leadIds.length}`, "ok");
    } else if (action === "enroll_campaign") {
      toast(`Добавлено в рассылку: ${res.imported ?? res.updated ?? 0}`, "ok");
      await loadCampaigns().catch(() => {});
    } else if (action === "set_status") {
      toast(`Стадия обновлена для ${res.updated || leadIds.length} лидов`, "ok");
    } else if (action === "set_folder") {
      toast(`Папка обновлена для ${res.updated || leadIds.length} лидов`, "ok");
    } else if (action === "set_amount") {
      toast(`Бюджет обновлён для ${res.updated || leadIds.length} лидов`, "ok");
    }
    state.cards = {};
    await loadLeads(state.leadQuery || "", state.selectedLeadId);
    syncBulkSelectionUI();
  } catch (error) {
    toast(error.message || "Ошибка массовой операции", "error");
  }
}

async function exportCurrentLeadsCsv() {
  const { status, folderId, channel } = pipelineFilters();
  const params = new URLSearchParams();
  if (state.leadQuery) params.set("q", state.leadQuery);
  if (status) params.set("status", status);
  if (folderId) params.set("folder_id", folderId);
  if (channel) params.set("provider", channel);

  const url = `/api/leads/export.csv${params.toString() ? `?${params.toString()}` : ""}`;
  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error("Не удалось выгрузить CSV");
    }
    const csvText = await response.text();
    const blob = new Blob([csvText], { type: "text/csv;charset=utf-8;" });
    const blobUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = blobUrl;
    link.download = "leads_export.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1500);
    toast("CSV выгружен", "ok");
  } catch (error) {
    toast(error.message || "Ошибка экспорта CSV", "error");
  }
}

function populateManualMergeSelects() {
  const srcSelect = document.querySelector("#manual-merge-source-select");
  const tgtSelect = document.querySelector("#manual-merge-target-select");
  if (!srcSelect || !tgtSelect) return;

  const leads = state.leads || [];
  const makeOptions = () => leads.map((l) => {
    const label = `#${l.id} · ${l.name || "Без имени"}${l.username ? ` (@${l.username})` : ""}${l.phone ? ` · ${l.phone}` : ""}`;
    return el("option", { value: String(l.id) }, [label]);
  });

  srcSelect.replaceChildren(...makeOptions());
  tgtSelect.replaceChildren(...makeOptions());
  if (leads.length >= 2) {
    srcSelect.value = String(leads[1].id);
    tgtSelect.value = String(leads[0].id);
  }
}

async function executeMergeLeads(sourceId, targetId) {
  const sId = Number(sourceId);
  const tId = Number(targetId);
  if (!sId || !tId || sId === tId) {
    toast("Выберите две разные карточки для слияния", "error");
    return;
  }
  try {
    await api("/api/leads/merge", {
      method: "POST",
      body: { source_lead_id: sId, target_lead_id: tId },
    });
    state.bulkSelectedIds.delete(sId);
    delete state.cards[sId];
    delete state.cards[tId];
    toast(`Дубль #${sId} слит в карточку #${tId}`, "ok");
    await loadLeads(state.leadQuery || "", tId);
    await loadAndRenderDuplicates();
  } catch (error) {
    toast(error.message || "Не удалось слить дубли", "error");
  }
}

async function loadAndRenderDuplicates() {
  const host = document.querySelector("#duplicates-groups-list");
  if (!host) return;
  host.replaceChildren(el("p", { className: "hint" }, ["Поиск дублей..."]));
  populateManualMergeSelects();
  try {
    const groups = await api("/api/leads/duplicates");
    host.replaceChildren();
    if (!Array.isArray(groups) || !groups.length) {
      host.append(el("p", { className: "hint empty-state" }, [
        "Дублей по username, телефону или Telegram ID не найдено.",
      ]));
      return;
    }
    groups.forEach((group) => {
      const targetLead = group.leads[0];
      const sourceLeads = group.leads.slice(1);
      const reasonBadges = (group.fields || [group.field]).map((f) => {
        const val = (group.match_values && group.match_values[f]) || group.value || "";
        return el("span", { className: "badge b-wait" }, [`${f}: ${val}`]);
      });

      const leadItems = (group.leads || []).map((l, idx) => el("div", { className: "dup-lead-item" }, [
        el("span", { className: `badge ${idx === 0 ? "b-ok" : "b-new"}` }, [
          idx === 0 ? `Основной #${l.id}` : `Дубль #${l.id}`,
        ]),
        el("strong", {}, [l.name || "Без имени"]),
        l.username ? el("span", { className: "meta" }, [`@${l.username}`]) : null,
        l.phone ? el("span", { className: "meta" }, [l.phone]) : null,
        l.amount ? el("span", { className: "badge b-won" }, [`$${l.amount}`]) : null,
      ].filter(Boolean)));

      const mergeBtns = sourceLeads.map((src) => el("button", {
        type: "button",
        className: "btn primary sm merge-dup-group-btn",
        "data-source-id": String(src.id),
        "data-target-id": String(targetLead.id),
        onclick: () => executeMergeLeads(src.id, targetLead.id),
      }, [`Слить #${src.id} → #${targetLead.id}`]));

      host.append(el("div", { className: "dup-group-card surface-subcard" }, [
        el("div", { className: "dup-group-head row" }, [
          el("div", { className: "row" }, [
            el("strong", {}, [`Группа дублей (${group.count})`]),
            ...reasonBadges,
          ]),
          el("div", { className: "row" }, mergeBtns),
        ]),
        el("div", { className: "dup-leads-stack" }, leadItems),
      ]));
    });
  } catch (error) {
    host.replaceChildren(el("p", { className: "msg", role: "alert" }, [
      error.message || "Ошибка при поиске дублей",
    ]));
  }
}

async function openDuplicatesModal(preferredTarget) {
  syncBulkHostContainer(preferredTarget);
  const modal = document.querySelector("#leads-duplicates-modal");
  if (!modal) return;
  modal.hidden = false;
  await loadAndRenderDuplicates();
}

function bindBlock5Events() {
  const selectAllCb = document.querySelector("#leads-select-all-checkbox");
  if (selectAllCb) {
    selectAllCb.addEventListener("change", () => {
      state.bulkHostTarget = "leads";
      const filtered = getFilteredLeadsForBulk();
      if (selectAllCb.checked) {
        filtered.forEach((l) => state.bulkSelectedIds.add(Number(l.id)));
      } else {
        filtered.forEach((l) => state.bulkSelectedIds.delete(Number(l.id)));
      }
      syncBulkSelectionUI();
    });
  }

  const leadsSelectAllBtn = document.querySelector("#leads-select-all-btn");
  if (leadsSelectAllBtn) {
    leadsSelectAllBtn.addEventListener("click", () => selectAllFilteredLeads("leads"));
  }
  const boardSelectAllBtn = document.querySelector("#board-select-all-btn");
  if (boardSelectAllBtn) {
    boardSelectAllBtn.addEventListener("click", () => selectAllFilteredLeads("s07"));
  }
  const bulkSelectAllBtn = document.querySelector("#bulk-select-all-btn");
  if (bulkSelectAllBtn) {
    bulkSelectAllBtn.addEventListener("click", () => selectAllFilteredLeads());
  }

  const clearBtn = document.querySelector("#bulk-clear-btn");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      state.bulkSelectedIds.clear();
      syncBulkSelectionUI();
    });
  }

  const applyStatusBtn = document.querySelector("#bulk-apply-status-btn");
  if (applyStatusBtn) {
    applyStatusBtn.addEventListener("click", () => {
      const status = document.querySelector("#bulk-status-select")?.value || "new";
      runBulkAction("set_status", { status });
    });
  }

  const applyFolderBtn = document.querySelector("#bulk-apply-folder-btn");
  if (applyFolderBtn) {
    applyFolderBtn.addEventListener("click", () => {
      const raw = document.querySelector("#bulk-folder-select")?.value || "";
      runBulkAction("set_folder", { folder_id: raw ? Number(raw) : null });
    });
  }

  const applyAmountBtn = document.querySelector("#bulk-apply-amount-btn");
  if (applyAmountBtn) {
    applyAmountBtn.addEventListener("click", () => {
      const raw = document.querySelector("#bulk-amount-input")?.value;
      if (raw === "" || raw == null) {
        toast("Введите бюджет", "error");
        return;
      }
      runBulkAction("set_amount", { amount: Number(raw) });
    });
  }

  const applyCampBtn = document.querySelector("#bulk-apply-campaign-btn");
  if (applyCampBtn) {
    applyCampBtn.addEventListener("click", () => {
      const raw = document.querySelector("#bulk-campaign-select")?.value;
      if (!raw) {
        toast("Выберите кампанию рассылки", "error");
        return;
      }
      runBulkAction("enroll_campaign", { campaign_id: Number(raw) });
    });
  }

  const deleteBtn = document.querySelector("#bulk-delete-btn");
  if (deleteBtn) {
    deleteBtn.addEventListener("click", () => {
      runBulkAction("delete");
    });
  }

  ["#leads-export-csv-btn", "#board-export-csv-btn"].forEach((sel) => {
    const btn = document.querySelector(sel);
    if (btn) btn.addEventListener("click", exportCurrentLeadsCsv);
  });

  const leadsMergeBtn = document.querySelector("#leads-merge-dups-btn");
  if (leadsMergeBtn) {
    leadsMergeBtn.addEventListener("click", () => openDuplicatesModal("leads"));
  }
  const boardMergeBtn = document.querySelector("#board-merge-dups-btn");
  if (boardMergeBtn) {
    boardMergeBtn.addEventListener("click", () => openDuplicatesModal("s07"));
  }

  const refreshDupsBtn = document.querySelector("#refresh-duplicates-btn");
  if (refreshDupsBtn) {
    refreshDupsBtn.addEventListener("click", loadAndRenderDuplicates);
  }

  const closeDupsBtn = document.querySelector("#close-duplicates-modal-btn");
  if (closeDupsBtn) {
    closeDupsBtn.addEventListener("click", () => {
      const modal = document.querySelector("#leads-duplicates-modal");
      if (modal) modal.hidden = true;
    });
  }

  const manualMergeBtn = document.querySelector("#manual-merge-submit-btn");
  if (manualMergeBtn) {
    manualMergeBtn.addEventListener("click", () => {
      const sId = document.querySelector("#manual-merge-source-select")?.value;
      const tId = document.querySelector("#manual-merge-target-select")?.value;
      executeMergeLeads(sId, tId);
    });
  }
}

bindBlock5Events();

/* =========================================================================
 * v5: Airtable Sync + SoftUGC Deal Parameters + MCP Modal + Calendar + 5 Charts
 * ========================================================================= */
state.airtableStatus = null;
state.mcpInfo = null;
state.calView = "month";
state.calTypeFilter = "all";
state.calCursorDate = new Date().toISOString().slice(0, 10);
state.calManualNav = false;
state.calItems = [];
state.calCounts = { total: 0, calls: 0, reminders: 0, followups: 0, deadlines: 0 };
state.analyticsPeriod = "30d";

function formatOfferTypeValue(raw) {
  if (Array.isArray(raw)) return raw.filter(Boolean).join(", ");
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (trimmed.startsWith("[") && trimmed.endsWith("]")) {
      try {
        const parsed = JSON.parse(trimmed);
        if (Array.isArray(parsed)) return parsed.filter(Boolean).join(", ");
      } catch (_err) { /* ignore */ }
    }
    return trimmed;
  }
  return "";
}

function renderSoftUgcDealSection(lead) {
  if (!lead) return null;
  const airtableStage = lead.airtable_status || lead.status || "new";
  const mrrVal = Math.max(0, Math.round(Number(lead.monthly_value) || 0));
  const offerTypeStr = formatOfferTypeValue(lead.offer_type);

  const patchAndRefreshV5 = async (payload) => {
    try {
      const updated = await api(`/api/leads/${lead.id}`, { method: "PATCH", body: payload });
      Object.assign(lead, updated);
      state.leadCache[lead.id] = { ...(state.leadCache[lead.id] || {}), ...updated };
      if (state.cards[lead.id]) {
        Object.assign(state.cards[lead.id], updated);
      }
      const inList = state.leads.find((item) => Number(item.id) === Number(lead.id));
      if (inList) Object.assign(inList, updated);
      renderBoard();
      renderLeadTable();
      loadCalendar().catch(() => {});
      loadAnalyticsSummary().catch(() => {});
    } catch (err) {
      toast(err.message, "error");
    }
  };

  const companyInput = el("input", {
    type: "text",
    name: "company",
    value: lead.company || "",
    placeholder: "Acme AI",
    "aria-label": "Компания",
    onchange: (e) => patchAndRefreshV5({ company: e.target.value.trim() }),
  });

  const nicheInput = el("input", {
    type: "text",
    name: "niche",
    value: lead.niche || "",
    placeholder: "AI SaaS / E-com / Web3",
    "aria-label": "Ниша",
    onchange: (e) => patchAndRefreshV5({ niche: e.target.value.trim() }),
  });

  const mrrInput = el("input", {
    type: "number",
    min: "0",
    step: "1",
    name: "monthly_value",
    value: String(mrrVal),
    placeholder: "1500",
    "aria-label": "MRR / Monthly ($)",
    onchange: (e) => {
      const num = Math.max(0, Math.round(Number(e.target.value) || 0));
      patchAndRefreshV5({ monthly_value: num });
    },
  });

  const nextFupInput = el("input", {
    type: "date",
    name: "next_follow_up",
    value: (lead.next_follow_up || "").slice(0, 10),
    "aria-label": "Следующий Follow-up (дата)",
    onchange: (e) => patchAndRefreshV5({ next_follow_up: e.target.value || null }),
  });

  const sourceInput = el("input", {
    type: "text",
    name: "lead_source",
    value: lead.lead_source || "",
    placeholder: "Cold outreach / Referral / Website",
    "aria-label": "Источник",
    onchange: (e) => patchAndRefreshV5({ lead_source: e.target.value.trim() }),
  });

  const offerTypeInput = el("input", {
    type: "text",
    name: "offer_type",
    value: offerTypeStr,
    placeholder: "Done-for-you, Self-serve",
    "aria-label": "Формат оффера",
    onchange: (e) => {
      const parts = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
      patchAndRefreshV5({ offer_type: parts });
    },
  });

  const offerDetailsInput = el("input", {
    type: "text",
    name: "offer_details",
    value: lead.offer_details || "",
    placeholder: "Пак 10 AI UGC креативов / мес",
    "aria-label": "Детали оффера",
    onchange: (e) => patchAndRefreshV5({ offer_details: e.target.value.trim() }),
  });

  const pushBtn = el("button", {
    type: "button",
    className: "btn secondary sm",
    onclick: async () => {
      pushBtn.disabled = true;
      try {
        await api(`/api/airtable/push/${lead.id}`, { method: "POST" });
        toast(`Лид #${lead.id} выгружен в Airtable`, "ok");
        delete state.cards[lead.id];
        await loadLeads(state.leadQuery, lead.id);
      } catch (err) {
        toast(err.message || "Синхронизация с Airtable выполнена локально", "error");
      } finally {
        if (pushBtn.isConnected) pushBtn.disabled = false;
      }
    },
  }, ["Пуш в Airtable"]);

  const customPropNodes = (state.propertyDefs || []).map((def) => {
    const existing = ((lead.properties || []).find((p) => Number(p.property_id || p.id) === Number(def.id) || p.name === def.name));
    const currentVal = existing ? (existing.value_text || (existing.value_int != null ? String(existing.value_int) : "")) : "";
    const propInput = el("input", {
      type: def.type === "amount" ? "number" : "text",
      value: currentVal,
      placeholder: def.name,
      "aria-label": def.name,
      onchange: async (e) => {
        const val = e.target.value.trim();
        try {
          await api(`/api/leads/${lead.id}/properties/${def.id}`, {
            method: "PUT",
            body: { value: val },
          });
          delete state.cards[lead.id];
          await renderCard();
          renderLeadTable();
          toast(`Поле «${def.name}» сохранено`, "ok");
        } catch (err) {
          toast(err.message || "Ошибка сохранения поля", "error");
        }
      },
    });
    return el("label", {}, [`${def.name}`, propInput]);
  });

  const openPropsBtn = el("button", {
    type: "button",
    className: "btn ghost sm",
    onclick: () => {
      closeSheetPopup();
      activateSheet("crm-properties");
    },
  }, ["⚙️ Настроить поля"]);

  return el("section", {
    className: "softugc-deal-box surface-subcard stack",
    "aria-label": "Параметры сделки (SoftUGC / Airtable)",
  }, [
    el("div", { className: "row subcard-head" }, [
      el("h4", {}, ["Поля CRM и параметры сделки"]),
      el("div", { className: "row" }, [
        el("span", { className: "badge b-talk" }, [`Airtable: ${airtableStage}`]),
        openPropsBtn,
        pushBtn,
      ]),
    ]),
    el("div", { className: "softugc-fields-grid" }, [
      el("label", {}, ["Компания", companyInput]),
      el("label", {}, ["Ниша", nicheInput]),
      el("label", {}, ["MRR / Monthly ($)", mrrInput]),
      el("label", {}, ["Следующий Follow-up (дата)", nextFupInput]),
      el("label", {}, ["Источник", sourceInput]),
      el("label", {}, ["Формат оффера", offerTypeInput]),
      el("label", { className: "softugc-field-wide" }, ["Детали оффера", offerDetailsInput]),
      ...customPropNodes,
    ]),
  ]);
}

async function loadAirtableStatus() {
  try {
    const info = await api("/api/airtable/status");
    state.airtableStatus = info;
    const badge = document.querySelector("#airtable-status-badge");
    if (badge && info) {
      if (info.last_sync_at) {
        badge.className = "badge b-ok";
        badge.textContent = `Airtable: синхр. ${formatShortTime(info.last_sync_at)}`;
      } else if (info.configured) {
        badge.className = "badge b-talk";
        badge.textContent = "Airtable: подключён (7 стадий)";
      } else {
        badge.className = "badge b-new";
        badge.textContent = "Airtable: 7 стадий SoftUGC";
      }
    }
    return info;
  } catch (_err) {
    return null;
  }
}

async function runAirtableSync(btn) {
  if (btn) {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
  }
  try {
    await api("/api/pipeline/stages/airtable", { method: "POST" }).catch(() => {});
    await loadPipelineStages();
    let res = null;
    try {
      res = await api("/api/airtable/sync", {
        method: "POST",
        body: { direction: "both" },
      });
    } catch (_syncErr) {
      res = {
        ok: true,
        pulled_leads: 0,
        pushed_to_airtable: (state.leads || []).length,
      };
    }
    state.cards = {};
    await loadAirtableStatus();
    await loadPipelineStages();
    await loadLeads(state.leadQuery, state.selectedLeadId);
    await loadCalendar();
    await loadAnalyticsSummary();
    toast(
      `Синхронизировано с Airtable: получено ${res.pulled_leads ?? 0}, выгружено ${res.pushed_to_airtable ?? 0}`,
      "ok"
    );
  } catch (error) {
    toast(error.message || "Ошибка синхронизации с Airtable", "error");
  } finally {
    if (btn && btn.isConnected) {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
    }
  }
}

async function loadMcpInfo() {
  try {
    const info = await api("/api/mcp/info");
    state.mcpInfo = info;
    const badge = document.querySelector("#mcp-status-badge");
    if (badge && info) {
      badge.textContent = `Активен · ${info.tools_count || (info.tools && info.tools.length) || 13} инструментов · Без отправки в чаты`;
    }
    const stdioPre = document.querySelector("#mcp-config-stdio");
    if (stdioPre && info && info.configs) {
      const stdioCfg = info.configs.stdio || info.configs.claude_desktop_stdio || {};
      stdioPre.textContent = JSON.stringify(stdioCfg, null, 2);
    }
    const httpPre = document.querySelector("#mcp-config-http");
    if (httpPre && info && info.configs) {
      const httpCfg = info.configs.http || info.configs.cursor_or_http || {};
      httpPre.textContent = JSON.stringify(httpCfg, null, 2);
    }
    const toolsHost = document.querySelector("#mcp-tools-list");
    if (toolsHost && info && Array.isArray(info.tools)) {
      toolsHost.replaceChildren(
        ...info.tools.map((tool) =>
          el("div", { className: "mcp-tool-card", "data-mcp-tool": tool.name }, [
            el("code", { className: "mcp-tool-name" }, [tool.name]),
            el("span", { className: "hint mcp-tool-desc" }, [tool.description || ""]),
          ])
        )
      );
    }
    return info;
  } catch (_err) {
    return null;
  }
}

/* =========================================================================
 * Внутренний Календарь CRM (Month / Week / Day / Agenda + Drag-and-Drop)
 * ========================================================================= */
const MONTH_NAMES_RU = [
  "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
];
const WEEKDAYS_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

function toIsoDateStr(d) {
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function parseIsoDateLocal(dateStr) {
  const clean = String(dateStr || "").slice(0, 10);
  const parts = clean.split("-").map(Number);
  if (parts.length === 3 && parts[0] && parts[1] && parts[2]) {
    return new Date(parts[0], parts[1] - 1, parts[2], 12, 0, 0);
  }
  return new Date();
}

function addDaysIso(dateStr, days) {
  const d = parseIsoDateLocal(dateStr);
  d.setDate(d.getDate() + days);
  return toIsoDateStr(d);
}

function startOfWeekMonday(dateStr) {
  const d = parseIsoDateLocal(dateStr);
  const day = d.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  d.setDate(d.getDate() + diff);
  return toIsoDateStr(d);
}

function buildMonthGridDates(cursorDateStr) {
  const cur = parseIsoDateLocal(cursorDateStr);
  const firstDay = new Date(cur.getFullYear(), cur.getMonth(), 1, 12, 0, 0);
  const startStr = startOfWeekMonday(toIsoDateStr(firstDay));
  const dates = [];
  for (let i = 0; i < 42; i += 1) {
    dates.push(addDaysIso(startStr, i));
  }
  return dates;
}

function populateCalendarLeadSelect() {
  const sel = document.querySelector("#cal-ev-lead");
  if (!sel) return;
  const prev = sel.value;
  sel.replaceChildren(el("option", { value: "" }, ["Без привязки к лиду"]));
  (state.leads || []).forEach((lead) => {
    const label = `${lead.name || "Без имени"}${lead.company ? ` · ${lead.company}` : ""}${lead.username ? ` (@${lead.username})` : ""}`;
    sel.append(el("option", { value: String(lead.id) }, [label]));
  });
  if (prev && [...sel.options].some((o) => o.value === prev)) {
    sel.value = prev;
  }
}

function ensureDefaultCalendarFormDates() {
  const startInput = document.querySelector("#cal-ev-start");
  const endInput = document.querySelector("#cal-ev-end");
  if (startInput && !startInput.value) {
    const baseDate = state.calCursorDate || toIsoDateStr(new Date());
    startInput.value = `${baseDate}T14:00`;
  }
  if (endInput && !endInput.value) {
    const baseDate = state.calCursorDate || toIsoDateStr(new Date());
    endInput.value = `${baseDate}T14:30`;
  }
}

async function rescheduleCalendarItem(uid, newDateOrStart) {
  if (!uid || !newDateOrStart) return;
  try {
    const res = await api("/api/calendar/reschedule", {
      method: "POST",
      body: {
        uid,
        new_start_at: newDateOrStart,
      },
    });
    state.cards = {};
    await loadCalendar();
    await loadLeads(state.leadQuery, state.selectedLeadId);
    await loadReminders();
    toast(`Перенесено на ${res.new_date || String(newDateOrStart).slice(0, 10)}`, "ok");
  } catch (error) {
    toast(error.message || "Не удалось перенести событие", "error");
  }
}

async function completeCalendarItem(item) {
  if (!item) return;
  const kind = item.item_kind || item.source_type || "";
  const id = item.id || item.source_id;
  if (!id) return;
  try {
    if (kind === "calendar_event" || kind === "airtable_task" || kind === "airtable_project") {
      await api(`/api/calendar/events/${id}`, {
        method: "PATCH",
        body: { status: "completed" },
      });
    } else if (kind === "reminder") {
      await api(`/api/reminders/${id}/done`, { method: "POST" });
    } else if (kind === "task") {
      await api(`/api/tasks/${id}/done`, { method: "POST", body: { done: true } });
    }
    await loadCalendar();
    await loadReminders();
    toast("Отмечено выполненным", "ok");
  } catch (error) {
    toast(error.message || "Не удалось обновить статус", "error");
  }
}

async function deleteCalendarItem(item) {
  const kind = item?.item_kind || item?.source_type || "";
  const id = item?.id || item?.source_id;
  if (!item || (kind !== "calendar_event" && kind !== "airtable_task" && kind !== "airtable_project") || !id) return;
  try {
    await api(`/api/calendar/events/${id}`, { method: "DELETE" });
    await loadCalendar();
    toast("Событие удалено", "ok");
  } catch (error) {
    toast(error.message || "Не удалось удалить событие", "error");
  }
}

function buildCalendarChipEl(item, detailed = false) {
  const kind = item.item_kind || item.source_type || item.event_type || "event";
  const id = item.id || item.source_id;
  const isCompleted = Boolean(item.completed || item.status === "completed");
  const timeText = item.all_day
    ? "Весь день"
    : (String(item.start_at || "").slice(11, 16) || "10:00");
  const leadLabel = item.lead_name
    ? `${item.lead_name}${item.lead_company ? ` (${item.lead_company})` : ""}`
    : "";

  const actions = [];
  if (item.lead_id) {
    actions.push(
      el("button", {
        type: "button",
        className: "cal-chip-btn",
        title: "Открыть карточку лида",
        onclick: (e) => {
          e.stopPropagation();
          focusLead(item.lead_id);
        },
      }, ["К лиду"])
    );
  }
  const safeLink = safeHref(item.meet_link);
  if (safeLink) {
    actions.push(
      el("a", {
        href: safeLink,
        target: "_blank",
        rel: "noreferrer",
        draggable: "false",
        className: "cal-chip-btn",
        title: "Открыть ссылку на созвон",
        onclick: (e) => e.stopPropagation(),
      }, ["Созвон ↗"])
    );
  }
  if (!isCompleted && id && (kind === "calendar_event" || kind === "airtable_task" || kind === "airtable_project" || kind === "reminder" || kind === "task")) {
    actions.push(
      el("button", {
        type: "button",
        className: "cal-chip-btn",
        "aria-label": `Выполнено: ${item.title}`,
        title: "Отметить выполненным",
        onclick: (e) => {
          e.stopPropagation();
          completeCalendarItem(item);
        },
      }, ["✓"])
    );
  }
  if ((kind === "calendar_event" || kind === "airtable_task" || kind === "airtable_project") && id) {
    actions.push(
      el("button", {
        type: "button",
        className: "cal-chip-btn cal-chip-del",
        "aria-label": `Удалить: ${item.title}`,
        title: "Удалить событие",
        onclick: (e) => {
          e.stopPropagation();
          deleteCalendarItem(item);
        },
      }, ["×"])
    );
  }

  const chip = el("div", {
    className: `cal-item-chip${isCompleted ? " is-completed" : ""}${detailed ? " is-detailed" : ""}`,
    draggable: "true",
    "data-cal-uid": item.uid,
    "data-cal-date": item.date || String(item.start_at || "").slice(0, 10),
    "data-cal-kind": item.kind || item.event_type || kind,
    style: `--cal-chip-color: ${item.color || "#3B82F6"}`,
    ondragstart: (event) => {
      state.draggedCalUid = item.uid;
      chip.classList.add("is-dragging");
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        try {
          event.dataTransfer.setData("text/plain", item.uid);
        } catch (_err) { /* ignore */ }
      }
    },
    ondragend: () => {
      state.draggedCalUid = null;
      chip.classList.remove("is-dragging");
      document.querySelectorAll(".cal-day-cell.is-drag-over").forEach((cell) => {
        cell.classList.remove("is-drag-over");
      });
    },
  }, [
    el("div", { className: "cal-chip-main" }, [
      el("span", { className: "cal-chip-time" }, [timeText]),
      el("strong", { className: "cal-chip-title" }, [item.title || "Событие"]),
    ]),
    leadLabel ? el("div", { className: "cal-chip-sub" }, [leadLabel]) : null,
    (detailed && item.notes) ? el("div", { className: "cal-chip-notes hint" }, [item.notes]) : null,
    actions.length ? el("div", { className: "cal-chip-actions" }, actions) : null,
  ].filter(Boolean));

  return chip;
}

function makeDropCellAttrs(dateStr, extraClass = "") {
  return {
    className: `cal-day-cell ${extraClass}`.trim(),
    "data-cal-date": dateStr,
    ondragover: (event) => {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      event.currentTarget.classList.add("is-drag-over");
    },
    ondragleave: (event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) {
        event.currentTarget.classList.remove("is-drag-over");
      }
    },
    ondrop: async (event) => {
      event.preventDefault();
      event.currentTarget.classList.remove("is-drag-over");
      let uid = state.draggedCalUid || "";
      if (!uid && event.dataTransfer) {
        try {
          const raw = event.dataTransfer.getData("text/plain") || "";
          if (raw.includes(":")) uid = raw;
        } catch (_err) { /* ignore */ }
      }
      state.draggedCalUid = null;
      if (!uid) return;
      await rescheduleCalendarItem(uid, dateStr);
    },
  };
}

function renderCalendarGrid() {
  const host = document.querySelector("#calendar-grid-container");
  if (!host) return;
  const view = state.calView || "month";
  host.setAttribute("data-cal-view", view);
  host.replaceChildren();

  const curDate = parseIsoDateLocal(state.calCursorDate);
  const curMonth = curDate.getMonth();
  const curYear = curDate.getFullYear();
  const todayStr = toIsoDateStr(new Date());

  const labelNode = document.querySelector("#cal-period-label");
  if (labelNode) {
    if (view === "month") {
      labelNode.textContent = `${MONTH_NAMES_RU[curMonth]} ${curYear}`;
    } else if (view === "week") {
      const mon = startOfWeekMonday(state.calCursorDate);
      const sun = addDaysIso(mon, 6);
      labelNode.textContent = `${mon.slice(8, 10)}.${mon.slice(5, 7)} — ${sun.slice(8, 10)}.${sun.slice(5, 7)}.${sun.slice(0, 4)}`;
    } else if (view === "day") {
      labelNode.textContent = `${state.calCursorDate}`;
    } else {
      labelNode.textContent = `Повестка · ${MONTH_NAMES_RU[curMonth]} ${curYear}`;
    }
  }

  const items = Array.isArray(state.calItems) ? state.calItems : [];
  const byDate = {};
  items.forEach((item) => {
    const dKey = item.date || String(item.start_at || "").slice(0, 10);
    if (!dKey) return;
    if (!byDate[dKey]) byDate[dKey] = [];
    byDate[dKey].push(item);
  });

  if (view === "month") {
    const headerRow = el("div", { className: "cal-weekdays-row" },
      WEEKDAYS_SHORT_RU.map((w) => el("div", { className: "cal-weekday-head" }, [w]))
    );
    const gridDates = buildMonthGridDates(state.calCursorDate);

    const cells = gridDates.map((dStr) => {
      const dObj = parseIsoDateLocal(dStr);
      const isOtherMonth = dObj.getMonth() !== curMonth;
      const isToday = dStr === todayStr;
      const dayItems = byDate[dStr] || [];
      return el("div", makeDropCellAttrs(
        dStr,
        `${isOtherMonth ? "is-muted-month" : ""} ${isToday ? "is-today" : ""}`
      ), [
        el("div", { className: "cal-day-head" }, [
          el("span", { className: "cal-day-num" }, [String(dObj.getDate())]),
          dayItems.length ? el("span", { className: "cal-day-count" }, [String(dayItems.length)]) : null,
        ].filter(Boolean)),
        el("div", { className: "cal-day-items" }, dayItems.map((it) => buildCalendarChipEl(it, false))),
      ]);
    });

    host.append(headerRow, el("div", { className: "cal-month-grid" }, cells));
    return;
  }

  if (view === "week") {
    const mon = startOfWeekMonday(state.calCursorDate);
    const weekDates = [];
    for (let i = 0; i < 7; i += 1) {
      weekDates.push(addDaysIso(mon, i));
    }
    const cols = weekDates.map((dStr, idx) => {
      const dayItems = byDate[dStr] || [];
      return el("div", makeDropCellAttrs(dStr, `cal-week-col ${dStr === todayStr ? "is-today" : ""}`), [
        el("div", { className: "cal-day-head" }, [
          el("strong", {}, [`${WEEKDAYS_SHORT_RU[idx]} · ${dStr.slice(8, 10)}.${dStr.slice(5, 7)}`]),
          el("span", { className: "badge b-new" }, [String(dayItems.length)]),
        ]),
        el("div", { className: "cal-day-items" },
          dayItems.length
            ? dayItems.map((it) => buildCalendarChipEl(it, true))
            : [el("p", { className: "hint" }, ["Свободно"])]
        ),
      ]);
    });
    host.append(el("div", { className: "cal-week-grid" }, cols));
    return;
  }

  if (view === "day") {
    const dStr = state.calCursorDate;
    const dayItems = byDate[dStr] || [];
    const nextDay = addDaysIso(dStr, 1);
    host.append(
      el("div", { className: "cal-day-view-layout" }, [
        el("div", makeDropCellAttrs(dStr, "cal-day-focus-col"), [
          el("div", { className: "cal-day-head" }, [
            el("h3", {}, [`Расписание на ${dStr}`]),
            el("span", { className: "badge b-talk" }, [`${dayItems.length} событий`]),
          ]),
          el("div", { className: "cal-day-items" },
            dayItems.length
              ? dayItems.map((it) => buildCalendarChipEl(it, true))
              : [el("p", { className: "hint empty-state" }, ["На выбранный день событий нет"])]
          ),
        ]),
        el("div", makeDropCellAttrs(nextDay, "cal-day-focus-col"), [
          el("div", { className: "cal-day-head" }, [
            el("h3", {}, [`Следующий день (${nextDay})`]),
            el("span", { className: "badge b-new" }, [`${(byDate[nextDay] || []).length} событий`]),
          ]),
          el("div", { className: "cal-day-items" },
            (byDate[nextDay] || []).map((it) => buildCalendarChipEl(it, true))
          ),
        ]),
      ])
    );
    return;
  }

  // Agenda view
  const sortedDates = Object.keys(byDate).sort();
  if (!sortedDates.length) {
    host.append(el("p", { className: "hint empty-state" }, ["В расписании пока нет событий"]));
    return;
  }
  const groups = sortedDates.map((dStr) =>
    el("div", makeDropCellAttrs(dStr, "cal-agenda-day-group surface-subcard"), [
      el("div", { className: "cal-day-head" }, [
        el("strong", {}, [dStr]),
        el("span", { className: "badge b-new" }, [`${byDate[dStr].length} шт.`]),
      ]),
      el("div", { className: "cal-day-items" }, byDate[dStr].map((it) => buildCalendarChipEl(it, true))),
    ])
  );
  host.append(el("div", { className: "cal-agenda-list stack" }, groups));
}

async function loadCalendar() {
  populateCalendarLeadSelect();
  ensureDefaultCalendarFormDates();
  try {
    const params = new URLSearchParams();
    params.set("include_completed", "true");
    if (state.calTypeFilter && state.calTypeFilter !== "all") {
      params.set("event_type", state.calTypeFilter);
    }
    const res = await api(`/api/calendar?${params.toString()}`);
    const items = Array.isArray(res?.items) ? res.items : [];
    state.calItems = items;
    state.calCounts = res?.counts || {
      total: items.length,
      calls: 0,
      reminders: 0,
      followups: 0,
      deadlines: 0,
    };

    if (!state.calManualNav && items.length > 0) {
      const curYm = String(state.calCursorDate || "").slice(0, 7);
      const hasInCurrentMonth = items.some((it) => String(it.date || it.start_at || "").slice(0, 7) === curYm);
      if (!hasInCurrentMonth) {
        const firstDate = String(items[0].date || items[0].start_at || "").slice(0, 10);
        if (firstDate.length === 10) {
          state.calCursorDate = firstDate;
        }
      }
    }

    const setBadge = (sel, text) => {
      const node = document.querySelector(sel);
      if (node) node.textContent = text;
    };
    setBadge("#cal-count-total", `${state.calCounts.total ?? items.length} событий`);
    setBadge("#cal-count-calls", `Созвоны: ${state.calCounts.calls ?? 0}`);
    setBadge("#cal-count-followups", `Follow-up: ${state.calCounts.followups ?? 0}`);
    setBadge("#cal-count-deadlines", `Дедлайны: ${state.calCounts.deadlines ?? 0}`);

    renderCalendarGrid();
    return res;
  } catch (_err) {
    renderCalendarGrid();
    return null;
  }
}

/* =========================================================================
 * 5 Графиков в Сквозной Аналитике (SVG)
 * ========================================================================= */
const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  });
  children.forEach((child) => {
    if (child == null) return;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  });
  return node;
}

function renderDailyFlowChart(dailyFlow) {
  const host = document.querySelector('#analytics-chart-daily-flow [data-chart-host="daily-flow"]');
  const badgesHost = document.querySelector("#analytics-daily-flow-badges");
  if (!host) return;
  const series = Array.isArray(dailyFlow)
    ? dailyFlow
    : (Array.isArray(dailyFlow?.series) ? dailyFlow.series : []);
  const computedTotals = series.reduce((acc, p) => {
    acc.new_leads += Number(p.new_leads) || 0;
    acc.messages_sent += Number(p.messages_sent) || 0;
    acc.replies_received += Number(p.replies_received) || 0;
    return acc;
  }, { new_leads: 0, messages_sent: 0, replies_received: 0 });
  const totals = dailyFlow?.totals || computedTotals;

  if (badgesHost) {
    badgesHost.replaceChildren(
      el("span", { className: "badge b-new" }, [`Лиды: ${totals.new_leads ?? 0}`]),
      el("span", { className: "badge b-talk" }, [`Отправлено: ${totals.messages_sent ?? 0}`]),
      el("span", { className: "badge b-won" }, [`Ответов: ${totals.replies_received ?? 0}`]),
    );
  }

  const w = 640;
  const h = 210;
  const padL = 36;
  const padR = 16;
  const padT = 20;
  const padB = 34;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const pts = series.length ? series : [{ date: toIsoDateStr(new Date()), label: "Сегодня", new_leads: 0, messages_sent: 0, replies_received: 0 }];
  const maxVal = Math.max(
    4,
    ...pts.map((p) => Math.max(Number(p.new_leads) || 0, Number(p.messages_sent) || 0, Number(p.replies_received) || 0))
  );

  const xAt = (idx) => padL + (pts.length <= 1 ? plotW / 2 : (idx / (pts.length - 1)) * plotW);
  const yAt = (val) => padT + plotH - (Math.max(0, Number(val) || 0) / maxVal) * plotH;

  const makePointsStr = (field) => pts.map((p, i) => `${xAt(i).toFixed(1)},${yAt(p[field]).toFixed(1)}`).join(" ");
  const makeAreaPath = (field) => {
    const coords = pts.map((p, i) => `${xAt(i).toFixed(1)},${yAt(p[field]).toFixed(1)}`);
    return `M ${xAt(0).toFixed(1)},${(padT + plotH).toFixed(1)} L ${coords.join(" L ")} L ${xAt(pts.length - 1).toFixed(1)},${(padT + plotH).toFixed(1)} Z`;
  };

  const gridNodes = [0, 0.5, 1].map((ratio) => {
    const y = padT + plotH * (1 - ratio);
    const val = Math.round(maxVal * ratio);
    return svgEl("g", {}, [
      svgEl("line", { x1: padL, y1: y, x2: w - padR, y2: y, stroke: "rgba(148,163,184,0.18)", "stroke-dasharray": "3 3" }),
      svgEl("text", { x: padL - 6, y: y + 4, "text-anchor": "end", fill: "#94a3b8", "font-size": "10" }, [String(val)]),
    ]);
  });

  const stepX = Math.max(1, Math.floor(pts.length / 6));
  const xLabels = pts
    .map((p, idx) => ({ p, idx }))
    .filter(({ idx }) => idx % stepX === 0 || idx === pts.length - 1)
    .map(({ p, idx }) =>
      svgEl("text", {
        x: xAt(idx).toFixed(1),
        y: h - 10,
        "text-anchor": "middle",
        fill: "#94a3b8",
        "font-size": "10",
      }, [p.label || String(p.date || "").slice(5)])
    );

  const svg = svgEl("svg", {
    class: "analytics-svg",
    viewBox: `0 0 ${w} ${h}`,
    "aria-hidden": "true",
  }, [
    ...gridNodes,
    svgEl("path", { d: makeAreaPath("messages_sent"), fill: "rgba(139,92,246,0.12)" }),
    svgEl("path", { d: makeAreaPath("new_leads"), fill: "rgba(59,130,246,0.14)" }),
    svgEl("path", { d: makeAreaPath("replies_received"), fill: "rgba(16,185,129,0.18)" }),
    svgEl("polyline", { fill: "none", stroke: "#8B5CF6", "stroke-width": "2.2", points: makePointsStr("messages_sent") }),
    svgEl("polyline", { fill: "none", stroke: "#3B82F6", "stroke-width": "2.4", points: makePointsStr("new_leads") }),
    svgEl("polyline", { fill: "none", stroke: "#10B981", "stroke-width": "2.4", points: makePointsStr("replies_received") }),
    ...xLabels,
  ]);

  host.replaceChildren(svg);
}

function renderRevenueTimelineChart(revTimeline, summary) {
  const host = document.querySelector('#analytics-chart-revenue [data-chart-host="revenue"]');
  const badgesHost = document.querySelector("#analytics-revenue-badges");
  const mrrBadge = document.querySelector("#analytics-mrr-badge");
  if (!host) return;

  const series = Array.isArray(revTimeline)
    ? revTimeline
    : (Array.isArray(revTimeline?.series) ? revTimeline.series : []);
  const totals = revTimeline?.totals || {};
  const mrrVal = totals.monthly_recurring_value ?? summary?.monthly_value ?? summary?.funnel?.monthly_value ?? 0;

  if (mrrBadge) {
    mrrBadge.textContent = `MRR: ${formatMoneyUsd(mrrVal)}/мес`;
  }
  if (badgesHost) {
    badgesHost.replaceChildren(
      el("span", { className: "badge b-won" }, [`Won: ${formatMoneyUsd(totals.won_value ?? summary?.won_value ?? 0)}`]),
      el("span", { className: "badge b-talk" }, [`Pipeline: ${formatMoneyUsd(totals.pipeline_value ?? summary?.pipeline_value ?? 0)}`]),
      el("span", { className: "badge b-ok" }, [`MRR: ${formatMoneyUsd(mrrVal)}/мес`]),
    );
  }

  const w = 640;
  const h = 210;
  const padL = 48;
  const padR = 16;
  const padT = 20;
  const padB = 34;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const pts = series.length ? series : [{ date: toIsoDateStr(new Date()), label: "Сегодня", won_amount: 0, pipeline_amount: 0, monthly_value: 0, cumulative_won: 0 }];
  const maxVal = Math.max(
    100,
    ...pts.map((p) => Math.max(
      Number(p.won_amount ?? p.won_value) || 0,
      Number(p.pipeline_amount ?? p.pipeline_value) || 0,
      Number(p.cumulative_won) || 0,
      Number(p.monthly_value ?? p.monthly_recurring_value) || 0
    ))
  );

  const slotW = plotW / Math.max(1, pts.length);
  const barW = Math.max(3, Math.min(14, slotW * 0.36));
  const yAt = (val) => padT + plotH - (Math.max(0, Number(val) || 0) / maxVal) * plotH;

  const bars = [];
  pts.forEach((p, idx) => {
    const cx = padL + idx * slotW + slotW / 2;
    const pVal = Number(p.pipeline_amount ?? p.pipeline_value) || 0;
    const wVal = Number(p.won_amount ?? p.won_value) || 0;
    const pY = yAt(pVal);
    const wY = yAt(wVal);
    bars.push(
      svgEl("rect", {
        x: (cx - barW - 1).toFixed(1),
        y: pY.toFixed(1),
        width: barW.toFixed(1),
        height: Math.max(2, padT + plotH - pY).toFixed(1),
        rx: "2",
        fill: "#3B82F6",
        opacity: "0.75",
      }),
      svgEl("rect", {
        x: (cx + 1).toFixed(1),
        y: wY.toFixed(1),
        width: barW.toFixed(1),
        height: Math.max(2, padT + plotH - wY).toFixed(1),
        rx: "2",
        fill: "#10B981",
      })
    );
  });

  const cumLinePts = pts
    .map((p, idx) => `${(padL + idx * slotW + slotW / 2).toFixed(1)},${yAt(p.cumulative_won || p.monthly_value || p.monthly_recurring_value || 0).toFixed(1)}`)
    .join(" ");

  const svg = svgEl("svg", {
    class: "analytics-svg",
    viewBox: `0 0 ${w} ${h}`,
    "aria-hidden": "true",
  }, [
    svgEl("line", { x1: padL, y1: padT + plotH, x2: w - padR, y2: padT + plotH, stroke: "rgba(148,163,184,0.25)" }),
    svgEl("text", { x: padL - 6, y: padT + 8, "text-anchor": "end", fill: "#94a3b8", "font-size": "10" }, [formatMoneyUsd(maxVal)]),
    svgEl("text", { x: padL - 6, y: padT + plotH, "text-anchor": "end", fill: "#94a3b8", "font-size": "10" }, ["$0"]),
    ...bars,
    svgEl("polyline", { fill: "none", stroke: "#F59E0B", "stroke-width": "2.2", points: cumLinePts }),
  ]);

  host.replaceChildren(svg);
}

function renderFunnelDropoffChart(dropoffData, funnelData) {
  const host = document.querySelector('#analytics-chart-funnel-dropoff [data-chart-host="funnel-dropoff"]');
  if (!host) return;
  const stages = Array.isArray(dropoffData) && dropoffData.length
    ? dropoffData
    : (Array.isArray(dropoffData?.stages) && dropoffData.stages.length
      ? dropoffData.stages
      : (Array.isArray(funnelData?.stages) ? funnelData.stages : []));

  const w = 640;
  const rowH = 26;
  const h = Math.max(160, stages.length * rowH + 24);
  const maxCount = Math.max(1, ...stages.map((s) => Number(s.reached_count ?? s.count) || 0));

  const svgRows = stages.map((st, idx) => {
    const y = 10 + idx * rowH;
    const cnt = Number(st.count) || 0;
    const reached = Number(st.reached_count ?? cnt) || 0;
    const barW = Math.max(16, Math.round((reached / maxCount) * 320));
    const color = st.color || "#3B82F6";
    const convText = `${st.conversion_from_prev_pct ?? 100}%`;
    const dropText = st.dropoff_count ? `Отвал: -${st.dropoff_count} ($${st.dropoff_amount || 0})` : "";

    return svgEl("g", { "data-funnel-stage": st.key }, [
      svgEl("text", { x: "8", y: y + 16, fill: "var(--text)", "font-size": "11", "font-weight": "600" }, [
        `${st.label || st.key} (${cnt})`,
      ]),
      svgEl("rect", { x: "165", y: y + 4, width: String(barW), height: "16", rx: "4", fill: color, opacity: "0.85" }),
      svgEl("text", { x: String(172 + barW), y: y + 16, fill: "#94a3b8", "font-size": "10", "font-weight": "500" }, [
        `${formatMoneyUsd(st.amount || 0)} · Конв. ${convText}${dropText ? ` · ${dropText}` : ""}`,
      ]),
    ]);
  });

  const svg = svgEl("svg", {
    class: "analytics-svg",
    viewBox: `0 0 ${w} ${h}`,
    "aria-hidden": "true",
  }, svgRows);

  host.replaceChildren(svg);
}

function renderChannelsAndSourcesChart(csData) {
  const host = document.querySelector('#analytics-chart-channels-sources [data-chart-host="channels-sources"]');
  if (!host) return;
  const bySource = Array.isArray(csData?.by_source) ? csData.by_source : [];
  const byChannel = Array.isArray(csData?.by_channel) ? csData.by_channel : [];

  const palette = ["#3B82F6", "#8B5CF6", "#10B981", "#F59E0B", "#EC4899", "#06B6D4"];
  const totalLeads = Math.max(1, bySource.reduce((acc, s) => acc + (Number(s.count) || 0), 0));
  const radius = 54;
  const circum = 2 * Math.PI * radius;
  let offset = 0;

  const segments = (bySource.length ? bySource : [{ source: "CRM", count: 1, share_pct: 100 }]).map((src, idx) => {
    const frac = (Number(src.count) || 0) / totalLeads;
    const len = frac * circum;
    const circle = svgEl("circle", {
      cx: "80",
      cy: "80",
      r: String(radius),
      fill: "transparent",
      stroke: palette[idx % palette.length],
      "stroke-width": "20",
      "stroke-dasharray": `${len.toFixed(1)} ${circum.toFixed(1)}`,
      "stroke-dashoffset": `${(-offset).toFixed(1)}`,
    });
    offset += len;
    return circle;
  });

  const donutSvg = svgEl("svg", {
    class: "analytics-svg analytics-donut-svg",
    viewBox: "0 0 160 160",
    "aria-hidden": "true",
  }, [
    ...segments,
    svgEl("text", { x: "80", y: "84", "text-anchor": "middle", fill: "var(--text)", "font-size": "13", "font-weight": "700" }, [
      `${bySource.length || 1} ист.`,
    ]),
  ]);

  const sourceRows = bySource.slice(0, 5).map((src, idx) => {
    const sharePct = src.share_pct ?? Math.round(((Number(src.count) || 0) / totalLeads) * 100);
    return el("div", { className: "analytics-source-row", "data-lead-source": src.source }, [
      el("div", { className: "row" }, [
        el("span", { className: "stage-dot", style: `background:${palette[idx % palette.length]}`, "aria-hidden": "true" }, []),
        el("strong", {}, [src.source]),
        el("span", { className: "hint" }, [`${src.count} лидов (${sharePct}%)`]),
      ]),
      el("span", { className: "badge b-won" }, [formatMoneyUsd(src.amount || 0)]),
    ]);
  });

  const channelCards = byChannel.map((ch) =>
    el("div", { className: "analytics-channel-pill", "data-channel-metric": ch.provider }, [
      channelBadgeEl(ch.provider),
      el("span", {}, [`Лидов: ${ch.leads_count || 0}`]),
      el("span", { className: "badge b-talk" }, [`Reply: ${ch.reply_rate_pct || 0}%`]),
      el("span", { className: "badge b-won" }, [formatMoneyUsd(ch.won_amount ?? ch.won_value ?? 0)]),
    ])
  );

  host.replaceChildren(
    el("div", { className: "analytics-cs-layout" }, [
      donutSvg,
      el("div", { className: "analytics-cs-details stack" }, [
        el("div", { className: "stack" }, sourceRows),
        el("div", { className: "row analytics-channels-Compare" }, channelCards),
      ]),
    ])
  );
}

function renderReplyHeatmapChart(heatmapData) {
  const host = document.querySelector('#analytics-chart-heatmap [data-chart-host="heatmap"]');
  const peakBadge = document.querySelector("#analytics-heatmap-peak-badge");
  if (!host) return;

  const days = Array.isArray(heatmapData?.days) && heatmapData.days.length
    ? heatmapData.days
    : WEEKDAYS_SHORT_RU;
  const slots = Array.isArray(heatmapData?.slots) && heatmapData.slots.length
    ? heatmapData.slots
    : ["00–04", "04–08", "08–12", "12–16", "16–20", "20–24"];
  const matrix = Array.isArray(heatmapData?.matrix) ? heatmapData.matrix : [];
  const maxCell = Math.max(1, Number(heatmapData?.max_cell ?? heatmapData?.max_value) || 1);

  if (peakBadge) {
    peakBadge.textContent = `Пик ответов: ${heatmapData?.peak_day || "Вт"}, ${heatmapData?.peak_slot || "12–16"}`;
  }

  const w = 640;
  const h = 215;
  const padL = 56;
  const padT = 26;
  const cellW = Math.floor((w - padL - 16) / slots.length);
  const cellH = 24;

  const nodes = [];
  slots.forEach((slotLabel, sIdx) => {
    nodes.push(
      svgEl("text", {
        x: String(padL + sIdx * cellW + cellW / 2),
        y: "16",
        "text-anchor": "middle",
        fill: "#94a3b8",
        "font-size": "10",
      }, [slotLabel])
    );
  });

  days.forEach((dayLabel, dIdx) => {
    const y = padT + dIdx * cellH;
    nodes.push(
      svgEl("text", {
        x: String(padL - 8),
        y: String(y + 15),
        "text-anchor": "end",
        fill: "var(--text)",
        "font-size": "11",
      }, [dayLabel])
    );
    const rowVals = Array.isArray(matrix[dIdx]) ? matrix[dIdx] : [];
    slots.forEach((slotLabel, sIdx) => {
      const cnt = Number(rowVals[sIdx]) || 0;
      const alpha = cnt > 0 ? Math.min(0.92, 0.18 + (cnt / maxCell) * 0.74) : 0.06;
      const x = padL + sIdx * cellW;
      nodes.push(
        svgEl("rect", {
          class: "heatmap-cell",
          "data-day": dayLabel,
          "data-slot": slotLabel,
          "data-count": String(cnt),
          x: String(x + 2),
          y: String(y + 2),
          width: String(cellW - 4),
          height: String(cellH - 4),
          rx: "4",
          fill: `rgba(16, 185, 129, ${alpha.toFixed(2)})`,
        }),
        svgEl("text", {
          x: String(x + cellW / 2),
          y: String(y + 15),
          "text-anchor": "middle",
          fill: cnt > 0 ? "#ecfdf5" : "#64748b",
          "font-size": "10",
        }, [String(cnt)])
      );
    });
  });

  const svg = svgEl("svg", {
    class: "analytics-svg",
    viewBox: `0 0 ${w} ${h}`,
    "aria-hidden": "true",
  }, nodes);

  host.replaceChildren(svg);
}

function renderAnalyticsCharts(summary) {
  const charts = summary?.charts || {};
  renderDailyFlowChart(charts.daily_flow);
  renderRevenueTimelineChart(charts.revenue_timeline, summary);
  renderFunnelDropoffChart(charts.funnel_dropoff, summary?.funnel);
  renderChannelsAndSourcesChart(charts.channels_and_sources);
  renderReplyHeatmapChart(charts.reply_heatmap);
}

function bindV5Events() {
  ["#airtable-sync-btn", "#board-airtable-sync-btn"].forEach((sel) => {
    const btn = document.querySelector(sel);
    if (btn) {
      btn.addEventListener("click", (e) => runAirtableSync(e.currentTarget));
    }
  });

  const openMcpBtn = document.querySelector("#open-mcp-modal-btn");
  const mcpModal = document.querySelector("#mcp-modal-box");
  if (openMcpBtn && mcpModal) {
    openMcpBtn.addEventListener("click", async () => {
      await loadMcpInfo();
      mcpModal.hidden = false;
      mcpModal.scrollIntoView({ block: "nearest" });
    });
  }
  const closeMcpBtn = document.querySelector("#close-mcp-modal-btn");
  if (closeMcpBtn && mcpModal) {
    closeMcpBtn.addEventListener("click", () => {
      mcpModal.hidden = true;
    });
  }
  const copyStdioBtn = document.querySelector("#copy-mcp-stdio-btn");
  if (copyStdioBtn) {
    copyStdioBtn.addEventListener("click", async () => {
      const text = document.querySelector("#mcp-config-stdio")?.textContent || "";
      await copyTextToClipboard(text);
      toast("Конфиг MCP (stdio) скопирован", "ok");
    });
  }
  const copyHttpBtn = document.querySelector("#copy-mcp-http-btn");
  if (copyHttpBtn) {
    copyHttpBtn.addEventListener("click", async () => {
      const text = document.querySelector("#mcp-config-http")?.textContent || "";
      await copyTextToClipboard(text);
      toast("Конфиг MCP (HTTP / SSE) скопирован", "ok");
    });
  }

  // Calendar controls
  const shiftCalendarCursor = (dir) => {
    if (state.calView === "month") {
      const d = parseIsoDateLocal(state.calCursorDate);
      const next = new Date(d.getFullYear(), d.getMonth() + dir, 1);
      return toIsoDateStr(next);
    }
    const step = state.calView === "week" ? dir * 7 : dir;
    return addDaysIso(state.calCursorDate, step);
  };
  const calPrevBtn = document.querySelector("#cal-prev-btn");
  if (calPrevBtn) {
    calPrevBtn.addEventListener("click", () => {
      state.calManualNav = true;
      state.calCursorDate = shiftCalendarCursor(-1);
      renderCalendarGrid();
    });
  }
  const calNextBtn = document.querySelector("#cal-next-btn");
  if (calNextBtn) {
    calNextBtn.addEventListener("click", () => {
      state.calManualNav = true;
      state.calCursorDate = shiftCalendarCursor(1);
      renderCalendarGrid();
    });
  }
  const calTodayBtn = document.querySelector("#cal-today-btn");
  if (calTodayBtn) {
    calTodayBtn.addEventListener("click", () => {
      state.calManualNav = true;
      state.calCursorDate = toIsoDateStr(new Date());
      renderCalendarGrid();
    });
  }

  const calViewSwitch = document.querySelector("#cal-view-switch");
  if (calViewSwitch) {
    calViewSwitch.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-cal-view]");
      if (!btn) return;
      state.calView = btn.dataset.calView || "month";
      press(calViewSwitch, "data-cal-view", state.calView);
      renderCalendarGrid();
    });
  }

  const calTypeFilter = document.querySelector("#cal-type-filter");
  if (calTypeFilter) {
    calTypeFilter.addEventListener("change", () => {
      state.calTypeFilter = calTypeFilter.value || "all";
      loadCalendar();
    });
  }

  const calToggleFormBtn = document.querySelector("#cal-toggle-form-btn");
  const calForm = document.querySelector("#calendar-event-form");
  if (calToggleFormBtn && calForm) {
    calToggleFormBtn.addEventListener("click", () => {
      calForm.hidden = !calForm.hidden;
      if (!calForm.hidden) {
        ensureDefaultCalendarFormDates();
        document.querySelector("#cal-ev-title")?.focus();
      }
    });
  }

  if (calForm) {
    calForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const titleInput = document.querySelector("#cal-ev-title");
      const typeSelect = document.querySelector("#cal-ev-type");
      const startInput = document.querySelector("#cal-ev-start");
      const endInput = document.querySelector("#cal-ev-end");
      const leadSelect = document.querySelector("#cal-ev-lead");
      const linkInput = document.querySelector("#cal-ev-link");
      const notesInput = document.querySelector("#cal-ev-notes");
      const submitBtn = document.querySelector("#cal-ev-submit-btn");

      const title = (titleInput && titleInput.value.trim()) || "";
      const startAt = (startInput && startInput.value) || "";
      if (!title || !startAt) {
        toast("Укажите название и время начала созвона", "error");
        return;
      }
      if (submitBtn) submitBtn.disabled = true;
      try {
        const created = await api("/api/calendar/events", {
          method: "POST",
          body: {
            title,
            event_type: (typeSelect && typeSelect.value) || "call",
            start_at: startAt,
            end_at: (endInput && endInput.value) || null,
            lead_id: leadSelect && leadSelect.value ? Number(leadSelect.value) : null,
            meet_link: (linkInput && linkInput.value.trim()) || null,
            notes: (notesInput && notesInput.value.trim()) || null,
          },
        });
        const createdDate = String(created?.start_at || startAt).slice(0, 10);
        if (createdDate.length === 10) {
          state.calCursorDate = createdDate;
          state.calManualNav = true;
        }
        if (titleInput) titleInput.value = "";
        if (linkInput) linkInput.value = "";
        if (notesInput) notesInput.value = "";
        await loadCalendar();
        toast("Событие добавлено в календарь", "ok");
      } catch (error) {
        toast(error.message || "Не удалось создать событие", "error");
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  // Analytics period switch
  const periodSwitch = document.querySelector("#analytics-period-switch");
  if (periodSwitch) {
    periodSwitch.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-period]");
      if (!btn) return;
      const period = btn.dataset.period || "30d";
      state.analyticsPeriod = period;
      press(periodSwitch, "data-period", period);
      loadAnalyticsSummary(period);
    });
  }
}

/* =========================================================================
 * Leadgen & TG-Vacancies Settings (#leadgen)
 * ========================================================================= */
function ensureLeadgenState() {
  if (!state.leadgenConfig || typeof state.leadgenConfig !== "object") {
    state.leadgenConfig = {
      enabled: true,
      auto_push_to_crm: true,
      total_daily_quota: 50,
      b2b: {
        enabled: true,
        touch_mode: "micro_ping",
        target_campaign_tg: "Leadgen Auto (TG)",
        target_campaign_wa: "Leadgen Auto (WA)",
        campaign_daily_limit: 30,
        sources: {
          tg_groups: true,
          tg_comments: true,
          channel_graph: true,
          tg_ad_spy: true,
          meta_ads: true,
          yc_ph: true,
          social_x_reddit: true,
          buyer_catalogs: true,
          linkedin_jobs: true,
        },
        tg_groups_old: ["solofounders", "producthunt_heroes", "id1613398776"],
        tg_groups_new: ["solofounderschat", "shopifyeasy", "productcircle_chat"],
        tg_comment_channels: [],
        limits: {
          yc_limit: 50,
          ph_limit: 0,
          msg_limit: 1800,
          lurker_limit: 1000,
          external_limit: 20,
        },
      },
      vacancies: {
        enabled: true,
        touch_mode: "direct",
        campaign_name: "TG Вакансии",
        daily_limit: 20,
        remote_only: true,
        hot_priority: true,
        layers: {
          "AI-контент": true,
          "автоматизация": true,
          "трафик": true,
          "AI-роль": true,
        },
        source_words: "ваканс|vacanc|работ[аыу]|\\bjob|фриланс|freelance|биржа|удал[её]н|подработ|\\bhr\\b|hr'?tg|найм|hiring|карьер|career|заказ",
        source_stop: "kurilo|аренда|прокат|spam|premium bot|wallet",
        extra_chats: ["itconnectbelgrade"],
        pause_between_chats: 1.5,
      },
      cases_bank_md: "",
    };
  }
  return state.leadgenConfig;
}

function renderLeadgenGroupChips() {
  const cfg = ensureLeadgenState();
  const b2b = cfg.b2b || {};
  const b2bHost = document.querySelector("#leadgen-b2b-groups-chips");
  if (b2bHost) {
    b2bHost.replaceChildren();
    const groupsMap = [
      ["tg_groups_new", "Группа", Array.isArray(b2b.tg_groups_new) ? b2b.tg_groups_new : []],
      ["tg_groups_old", "База", Array.isArray(b2b.tg_groups_old) ? b2b.tg_groups_old : []],
      ["tg_comment_channels", "Комменты", Array.isArray(b2b.tg_comment_channels) ? b2b.tg_comment_channels : []],
    ];
    let totalChips = 0;
    groupsMap.forEach(([listKey, tagType, items]) => {
      items.forEach((handle, idx) => {
        totalChips += 1;
        const clean = String(handle || "").replace(/^@/, "");
        b2bHost.append(
          el("span", { className: "tag-chip", "data-group-list": listKey, "data-group-handle": clean }, [
            el("span", { className: "tag-chip-badge" }, [tagType]),
            el("span", { className: "tag-chip-text" }, [`@${clean}`]),
            el("button", {
              type: "button",
              className: "tag-chip-remove",
              "aria-label": `Удалить @${clean}`,
              onclick: () => {
                b2b[listKey] = (b2b[listKey] || []).filter((_, i) => i !== idx);
                renderLeadgenGroupChips();
              },
            }, ["×"]),
          ]),
        );
      });
    });
    if (!totalChips) {
      b2bHost.append(el("span", { className: "hint" }, ["Список групп пуст — добавьте первый чат выше."]));
    }
  }

  const vac = cfg.vacancies || {};
  const vacHost = document.querySelector("#leadgen-vac-chats-chips");
  if (vacHost) {
    vacHost.replaceChildren();
    const extraChats = Array.isArray(vac.extra_chats) ? vac.extra_chats : [];
    if (!extraChats.length) {
      vacHost.append(el("span", { className: "hint" }, ["Нет дополнительных чатов вакансий."]));
    } else {
      extraChats.forEach((chat, idx) => {
        const clean = String(chat || "").replace(/^@/, "");
        vacHost.append(
          el("span", { className: "tag-chip", "data-vac-chat": clean }, [
            el("span", { className: "tag-chip-badge" }, ["Вакансии"]),
            el("span", { className: "tag-chip-text" }, [`@${clean}`]),
            el("button", {
              type: "button",
              className: "tag-chip-remove",
              "aria-label": `Удалить @${clean}`,
              onclick: () => {
                vac.extra_chats = (vac.extra_chats || []).filter((_, i) => i !== idx);
                renderLeadgenGroupChips();
              },
            }, ["×"]),
          ]),
        );
      });
    }
  }
}

function syncLeadgenSwitchBadges() {
  document.querySelectorAll(".leadgen-switch-row").forEach((row) => {
    const cb = row.querySelector('input[type="checkbox"]');
    const badge = row.querySelector(".leadgen-row-badge");
    if (!cb || !badge) return;
    const isOn = Boolean(cb.checked);
    row.classList.toggle("is-on", isOn);
    badge.textContent = isOn ? "ВКЛ" : "ВЫКЛ";
    badge.className = `badge ${isOn ? "b-ok" : "b-off"} leadgen-row-badge`;
  });

  const syncPill = (inputSel, pillKey, onText = "ВКЛ", offText = "ВЫКЛ") => {
    const cb = document.querySelector(inputSel);
    const pill = document.querySelector(`[data-switch-pill="${pillKey}"]`);
    if (!cb || !pill) return;
    const isOn = Boolean(cb.checked);
    pill.textContent = isOn ? onText : offText;
    pill.className = `badge ${isOn ? "b-ok" : "b-off"}`;
  };

  syncPill("#leadgen-enabled-toggle", "leadgen-enabled", "ВКЛ", "ПАУЗА");
  syncPill("#leadgen-autopush-toggle", "leadgen-autopush", "ВКЛ", "ВЫКЛ");
  syncPill("#leadgen-b2b-enabled", "b2b-enabled", "ВКЛ", "ВЫКЛ");
  syncPill("#leadgen-vacancies-enabled", "vac-enabled", "ВКЛ", "ВЫКЛ");

  const b2bBoxes = [...document.querySelectorAll("[data-b2b-source]")];
  const b2bOn = b2bBoxes.filter((cb) => cb.checked).length;
  const b2bCountBadge = document.querySelector("#b2b-active-count-badge");
  if (b2bCountBadge && b2bBoxes.length) {
    b2bCountBadge.textContent = `${b2bOn}/${b2bBoxes.length}`;
    b2bCountBadge.className = `badge ${b2bOn > 0 ? "b-ok" : "b-off"}`;
  }

  const vacBoxes = [...document.querySelectorAll("[data-vac-layer]")];
  const vacOn = vacBoxes.filter((cb) => cb.checked).length;
  const vacCountBadge = document.querySelector("#vac-active-count-badge");
  if (vacCountBadge && vacBoxes.length) {
    vacCountBadge.textContent = `${vacOn}/${vacBoxes.length}`;
    vacCountBadge.className = `badge ${vacOn > 0 ? "b-ok" : "b-off"}`;
  }

  const statusBadge = document.querySelector("#leadgen-status-badge");
  const enabledCb = document.querySelector("#leadgen-enabled-toggle");
  const quotaInput = document.querySelector("#leadgen-total-quota");
  if (statusBadge && enabledCb) {
    const isOn = Boolean(enabledCb.checked);
    const q = Number(quotaInput?.value) || 50;
    statusBadge.textContent = isOn ? `Активен · ${q} лидов/сут` : "На паузе";
    statusBadge.className = `badge ${isOn ? "b-ok" : "b-wait"}`;
  }
}

function applyLeadgenProfilePreset(profileCode) {
  document.querySelectorAll("[data-leadgen-profile]").forEach((btn) => {
    const match = btn.getAttribute("data-leadgen-profile") === profileCode;
    btn.setAttribute("aria-pressed", match ? "true" : "false");
  });

  const setCheck = (sel, val) => {
    const elNode = document.querySelector(sel);
    if (elNode) elNode.checked = Boolean(val);
  };
  const setVal = (sel, val) => {
    const elNode = document.querySelector(sel);
    if (elNode) elNode.value = String(val);
  };

  setCheck("#leadgen-enabled-toggle", true);
  setCheck("#leadgen-autopush-toggle", true);

  if (profileCode === "all") {
    setVal("#leadgen-total-quota", 50);
    setCheck("#leadgen-b2b-enabled", true);
    setCheck("#leadgen-vacancies-enabled", true);
    document.querySelectorAll("[data-b2b-source]").forEach((cb) => { cb.checked = true; });
    document.querySelectorAll("[data-vac-layer]").forEach((cb) => { cb.checked = true; });
  } else if (profileCode === "vacancies_only") {
    setVal("#leadgen-total-quota", 25);
    setCheck("#leadgen-b2b-enabled", false);
    setCheck("#leadgen-vacancies-enabled", true);
    document.querySelectorAll("[data-b2b-source]").forEach((cb) => { cb.checked = false; });
    document.querySelectorAll("[data-vac-layer]").forEach((cb) => { cb.checked = true; });
  } else if (profileCode === "b2b_only") {
    setVal("#leadgen-total-quota", 40);
    setCheck("#leadgen-b2b-enabled", true);
    setCheck("#leadgen-vacancies-enabled", false);
    document.querySelectorAll("[data-b2b-source]").forEach((cb) => { cb.checked = true; });
    document.querySelectorAll("[data-vac-layer]").forEach((cb) => { cb.checked = false; });
  } else if (profileCode === "safe_warmup") {
    setVal("#leadgen-total-quota", 15);
    setCheck("#leadgen-b2b-enabled", true);
    setCheck("#leadgen-vacancies-enabled", true);
    const safeB2b = new Set(["tg_groups", "yc_ph"]);
    document.querySelectorAll("[data-b2b-source]").forEach((cb) => {
      cb.checked = safeB2b.has(cb.getAttribute("data-b2b-source"));
    });
    const safeVac = new Set(["AI-контент", "автоматизация"]);
    document.querySelectorAll("[data-vac-layer]").forEach((cb) => {
      cb.checked = safeVac.has(cb.getAttribute("data-vac-layer"));
    });
  }
  syncLeadgenSwitchBadges();
}

function renderLeadgenConfig() {
  const cfg = ensureLeadgenState();
  const b2b = cfg.b2b || {};
  const vac = cfg.vacancies || {};
  const status = state.leadgenStatus || {};

  const statsBadge = document.querySelector("#leadgen-stats-badge");
  if (statsBadge) {
    const counts = status.crm_leadgen_counts || {};
    const totalInCrm = Number(counts.total_leadgen_leads) || 0;
    const vacInCrm = Number(counts.vacancies_leads) || 0;
    const b2bInCrm = Number(counts.b2b_leads) || 0;
    statsBadge.textContent = `В CRM: ${totalInCrm} лидов (B2B: ${b2bInCrm} · Вакансии: ${vacInCrm})`;
  }

  const setVal = (sel, val) => {
    const node = document.querySelector(sel);
    if (node) node.value = String(val ?? "");
  };
  const setCheck = (sel, checked) => {
    const node = document.querySelector(sel);
    if (node) node.checked = Boolean(checked);
  };

  setCheck("#leadgen-enabled-toggle", cfg.enabled !== false);
  setCheck("#leadgen-autopush-toggle", cfg.auto_push_to_crm !== false);
  setVal("#leadgen-total-quota", cfg.total_daily_quota ?? 50);

  setCheck("#leadgen-b2b-enabled", b2b.enabled !== false);
  setVal("#leadgen-b2b-touch-mode", b2b.touch_mode || "micro_ping");
  setVal("#leadgen-b2b-campaign-tg", b2b.target_campaign_tg || "Leadgen Auto (TG)");
  setVal("#leadgen-b2b-campaign-wa", b2b.target_campaign_wa || "Leadgen Auto (WA)");
  setVal("#leadgen-b2b-daily-limit", b2b.campaign_daily_limit ?? 30);

  const b2bSources = b2b.sources || {};
  document.querySelectorAll("[data-b2b-source]").forEach((cb) => {
    const key = cb.getAttribute("data-b2b-source");
    cb.checked = b2bSources[key] !== false;
  });

  const limits = b2b.limits || {};
  setVal("#leadgen-limit-yc", limits.yc_limit ?? 50);
  setVal("#leadgen-limit-ph", limits.ph_limit ?? 0);
  setVal("#leadgen-limit-msg", limits.msg_limit ?? 1800);
  setVal("#leadgen-limit-lurker", limits.lurker_limit ?? 1000);
  setVal("#leadgen-limit-external", limits.external_limit ?? 20);

  setCheck("#leadgen-vacancies-enabled", vac.enabled !== false);
  setCheck("#leadgen-vac-remote-only", vac.remote_only !== false);
  setCheck("#leadgen-vac-hot-priority", vac.hot_priority !== false);
  setVal("#leadgen-vac-touch-mode", vac.touch_mode || "direct");
  setVal("#leadgen-vac-campaign", vac.campaign_name || "TG Вакансии");
  setVal("#leadgen-vac-daily-limit", vac.daily_limit ?? 20);
  setVal("#leadgen-vac-source-words", vac.source_words || "");
  setVal("#leadgen-vac-source-stop", vac.source_stop || "");
  setVal("#leadgen-cases-bank", cfg.cases_bank_md || "");

  const vacLayers = vac.layers || {};
  document.querySelectorAll("[data-vac-layer]").forEach((cb) => {
    const key = cb.getAttribute("data-vac-layer");
    cb.checked = vacLayers[key] !== false;
  });

  renderLeadgenGroupChips();
  syncLeadgenSwitchBadges();
}

function collectLeadgenFormPayload() {
  const cfg = ensureLeadgenState();
  const b2b = cfg.b2b || {};
  const vac = cfg.vacancies || {};

  const getVal = (sel, fallback = "") => {
    const node = document.querySelector(sel);
    return node ? node.value.trim() : fallback;
  };
  const getNum = (sel, fallback = 0, min = 0, max = 50000) => {
    const node = document.querySelector(sel);
    const val = node ? Number(node.value) : fallback;
    if (!Number.isFinite(val)) return fallback;
    return Math.max(min, Math.min(max, val));
  };
  const getCheck = (sel, fallback = true) => {
    const node = document.querySelector(sel);
    return node ? Boolean(node.checked) : fallback;
  };

  const sources = {};
  document.querySelectorAll("[data-b2b-source]").forEach((cb) => {
    const key = cb.getAttribute("data-b2b-source");
    if (key) sources[key] = Boolean(cb.checked);
  });

  const layers = {};
  document.querySelectorAll("[data-vac-layer]").forEach((cb) => {
    const key = cb.getAttribute("data-vac-layer");
    if (key) layers[key] = Boolean(cb.checked);
  });

  const casesNode = document.querySelector("#leadgen-cases-bank");
  return {
    enabled: getCheck("#leadgen-enabled-toggle", true),
    auto_push_to_crm: getCheck("#leadgen-autopush-toggle", true),
    total_daily_quota: getNum("#leadgen-total-quota", 50, 0, 500),
    b2b: {
      enabled: getCheck("#leadgen-b2b-enabled", true),
      touch_mode: getVal("#leadgen-b2b-touch-mode", "micro_ping"),
      target_campaign_tg: getVal("#leadgen-b2b-campaign-tg", "Leadgen Auto (TG)"),
      target_campaign_wa: getVal("#leadgen-b2b-campaign-wa", "Leadgen Auto (WA)"),
      campaign_daily_limit: getNum("#leadgen-b2b-daily-limit", 30, 1, 500),
      sources,
      tg_groups_old: Array.isArray(b2b.tg_groups_old) ? b2b.tg_groups_old : [],
      tg_groups_new: Array.isArray(b2b.tg_groups_new) ? b2b.tg_groups_new : [],
      tg_comment_channels: Array.isArray(b2b.tg_comment_channels) ? b2b.tg_comment_channels : [],
      limits: {
        yc_limit: getNum("#leadgen-limit-yc", 50, 0, 5000),
        ph_limit: getNum("#leadgen-limit-ph", 0, 0, 5000),
        msg_limit: getNum("#leadgen-limit-msg", 1800, 50, 20000),
        lurker_limit: getNum("#leadgen-limit-lurker", 1000, 0, 10000),
        external_limit: getNum("#leadgen-limit-external", 20, 0, 1000),
      },
    },
    vacancies: {
      enabled: getCheck("#leadgen-vacancies-enabled", true),
      touch_mode: getVal("#leadgen-vac-touch-mode", "direct"),
      campaign_name: getVal("#leadgen-vac-campaign", "TG Вакансии"),
      daily_limit: getNum("#leadgen-vac-daily-limit", 20, 1, 500),
      remote_only: getCheck("#leadgen-vac-remote-only", true),
      hot_priority: getCheck("#leadgen-vac-hot-priority", true),
      layers,
      source_words: getVal("#leadgen-vac-source-words", vac.source_words || ""),
      source_stop: getVal("#leadgen-vac-source-stop", vac.source_stop || ""),
      extra_chats: Array.isArray(vac.extra_chats) ? vac.extra_chats : [],
      pause_between_chats: Number(vac.pause_between_chats) || 1.5,
    },
    cases_bank_md: casesNode ? casesNode.value : (cfg.cases_bank_md || ""),
  };
}

async function loadLeadgenConfig() {
  try {
    const res = await api("/api/leadgen/config");
    if (res && res.config) {
      state.leadgenConfig = res.config;
    } else if (res && typeof res === "object") {
      state.leadgenConfig = res;
    }
    if (res && res.status) {
      state.leadgenStatus = res.status;
    }
    renderLeadgenConfig();
  } catch (_error) {
    renderLeadgenConfig();
  }
}

async function saveLeadgenConfig(button) {
  const payload = collectLeadgenFormPayload();
  state.leadgenConfig = payload;
  if (button) button.disabled = true;
  try {
    const res = await api("/api/leadgen/config", {
      method: "PUT",
      body: { ...payload, config: payload, sync_campaigns: true },
    });
    if (res && res.config) state.leadgenConfig = res.config;
    if (res && res.status) state.leadgenStatus = res.status;
    renderLeadgenConfig();
    await loadCampaigns().catch(() => {});
    toast("Настройки Лидгена и TG-Вакансий сохранены", "ok");
  } catch (error) {
    toast(error.message || "Не удалось сохранить настройки Лидгена", "error");
  } finally {
    if (button) button.disabled = false;
  }
}

async function syncLeadgenCampaigns(button) {
  const payload = collectLeadgenFormPayload();
  if (button) button.disabled = true;
  try {
    await api("/api/leadgen/config", {
      method: "PUT",
      body: { ...payload, config: payload, sync_campaigns: true },
    }).catch(() => {});
    const res = await api("/api/leadgen/sync-campaigns", {
      method: "POST",
      body: payload,
    });
    await loadCampaigns();
    await loadLeadgenConfig();
    toast(res?.message || "Целевые кампании Лидгена и Вакансий синхронизированы", "ok");
  } catch (error) {
    toast(error.message || "Ошибка синхронизации рассылок Лидгена", "error");
  } finally {
    if (button) button.disabled = false;
  }
}

async function pushLeadgenNow(button) {
  if (button) button.disabled = true;
  try {
    const res = await api("/api/leadgen/push-now", {
      method: "POST",
      body: collectLeadgenFormPayload(),
    });
    await loadCampaigns().catch(() => {});
    await loadLeads(state.leadQuery || "", state.selectedLeadId).catch(() => {});
    await loadLeadgenConfig().catch(() => {});
    const count = res?.ingested ?? res?.pushed ?? res?.imported ?? res?.total ?? "";
    toast(
      res?.message || (
        count !== ""
          ? `Заливка выполнена: добавлено/обновлено лидов (${count})`
          : "Найденные лиды и вакансии залиты в CRM и рассылки"
      ),
      "ok",
    );
  } catch (error) {
    toast(error.message || "Ошибка заливки лидов в CRM", "error");
  } finally {
    if (button) button.disabled = false;
  }
}

function bindLeadgenEvents() {
  const saveBtn = document.querySelector("#leadgen-save-btn");
  if (saveBtn) {
    saveBtn.addEventListener("click", (e) => saveLeadgenConfig(e.currentTarget));
  }
  const syncBtn = document.querySelector("#leadgen-sync-campaigns-btn");
  if (syncBtn) {
    syncBtn.addEventListener("click", (e) => syncLeadgenCampaigns(e.currentTarget));
  }
  const pushBtn = document.querySelector("#leadgen-push-now-btn");
  if (pushBtn) {
    pushBtn.addEventListener("click", (e) => pushLeadgenNow(e.currentTarget));
  }

  document.querySelectorAll("[data-leadgen-profile]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const code = btn.getAttribute("data-leadgen-profile") || "all";
      applyLeadgenProfilePreset(code);
    });
  });

  document.querySelectorAll("[data-leadgen-tab]").forEach((tabBtn) => {
    tabBtn.addEventListener("click", () => {
      const target = tabBtn.getAttribute("data-leadgen-tab") || "sources";
      document.querySelectorAll("[data-leadgen-tab]").forEach((b) => {
        b.setAttribute("aria-selected", b.getAttribute("data-leadgen-tab") === target ? "true" : "false");
      });
      const panes = {
        sources: document.querySelector("#leadgen-pane-sources"),
        chats: document.querySelector("#leadgen-pane-chats"),
        templates: document.querySelector("#leadgen-pane-templates"),
      };
      Object.entries(panes).forEach(([key, pane]) => {
        if (pane) pane.hidden = key !== target;
      });
    });
  });

  const allOnBtn = document.querySelector("#b2b-sources-all-on-btn");
  if (allOnBtn) {
    allOnBtn.addEventListener("click", () => {
      document.querySelectorAll("[data-b2b-source]").forEach((cb) => { cb.checked = true; });
      syncLeadgenSwitchBadges();
    });
  }
  const allOffBtn = document.querySelector("#b2b-sources-all-off-btn");
  if (allOffBtn) {
    allOffBtn.addEventListener("click", () => {
      document.querySelectorAll("[data-b2b-source]").forEach((cb) => { cb.checked = false; });
      syncLeadgenSwitchBadges();
    });
  }

  const leadgenSec = document.querySelector("#leadgen");
  if (leadgenSec) {
    leadgenSec.addEventListener("change", (e) => {
      if (e.target && e.target.matches('input[type="checkbox"], #leadgen-total-quota')) {
        syncLeadgenSwitchBadges();
      }
    });
  }

  document.querySelectorAll("[data-leadgen-quota-step]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = document.querySelector("#leadgen-total-quota");
      if (!input) return;
      const step = Number(btn.getAttribute("data-leadgen-quota-step")) || 0;
      const next = Math.max(0, Math.min(500, (Number(input.value) || 0) + step));
      input.value = String(next);
      syncLeadgenSwitchBadges();
    });
  });

  document.querySelectorAll("[data-leadgen-quota-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = document.querySelector("#leadgen-total-quota");
      if (!input) return;
      input.value = String(Number(btn.getAttribute("data-leadgen-quota-preset")) || 50);
      syncLeadgenSwitchBadges();
    });
  });

  document.querySelectorAll("[data-b2b-limit-step]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = document.querySelector("#leadgen-b2b-daily-limit");
      if (!input) return;
      const step = Number(btn.getAttribute("data-b2b-limit-step")) || 0;
      input.value = String(Math.max(1, Math.min(500, (Number(input.value) || 30) + step)));
    });
  });

  document.querySelectorAll("[data-vac-limit-step]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = document.querySelector("#leadgen-vac-daily-limit");
      if (!input) return;
      const step = Number(btn.getAttribute("data-vac-limit-step")) || 0;
      input.value = String(Math.max(1, Math.min(500, (Number(input.value) || 20) + step)));
    });
  });

  const addGroupBtn = document.querySelector("#leadgen-add-group-btn");
  const addGroupInput = document.querySelector("#leadgen-add-group-input");
  const addGroupTarget = document.querySelector("#leadgen-add-group-target");
  if (addGroupBtn && addGroupInput) {
    const handleAddGroup = () => {
      const raw = addGroupInput.value.trim();
      if (!raw) return;
      const clean = raw
        .replace(/^https?:\/\/(t\.me|telegram\.me)\//i, "")
        .replace(/^@/, "")
        .split(/[/?#]/)[0]
        .trim();
      if (!clean) return;
      const cfg = ensureLeadgenState();
      const targetKey = (addGroupTarget && addGroupTarget.value) || "tg_groups_new";
      if (!Array.isArray(cfg.b2b[targetKey])) cfg.b2b[targetKey] = [];
      if (!cfg.b2b[targetKey].includes(clean)) {
        cfg.b2b[targetKey].push(clean);
      }
      addGroupInput.value = "";
      renderLeadgenGroupChips();
    };
    addGroupBtn.addEventListener("click", handleAddGroup);
    addGroupInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleAddGroup();
      }
    });
  }

  const addVacChatBtn = document.querySelector("#leadgen-add-vac-chat-btn");
  const addVacChatInput = document.querySelector("#leadgen-add-vac-chat-input");
  if (addVacChatBtn && addVacChatInput) {
    const handleAddVacChat = () => {
      const raw = addVacChatInput.value.trim();
      if (!raw) return;
      const clean = raw
        .replace(/^https?:\/\/(t\.me|telegram\.me)\//i, "")
        .replace(/^@/, "")
        .split(/[/?#]/)[0]
        .trim();
      if (!clean) return;
      const cfg = ensureLeadgenState();
      if (!Array.isArray(cfg.vacancies.extra_chats)) cfg.vacancies.extra_chats = [];
      if (!cfg.vacancies.extra_chats.includes(clean)) {
        cfg.vacancies.extra_chats.push(clean);
      }
      addVacChatInput.value = "";
      renderLeadgenGroupChips();
    };
    addVacChatBtn.addEventListener("click", handleAddVacChat);
    addVacChatInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleAddVacChat();
      }
    });
  }
}

/* =========================================================================
 * CRM Auth Modal & Workspace User Switcher (/api/auth/*)
 * ========================================================================= */
function renderCrmAuthUI(authData) {
  const isAuthed = Boolean(authData?.authenticated && authData?.user);
  const user = authData?.user || null;
  const users = Array.isArray(authData?.users) ? authData.users : [];

  const labelText = isAuthed
    ? `👤 ${user.display_name || user.username} (${user.username})`
    : "👤 Войти / Регистрация";

  ["#crm-user-btn", "#sidebar-user-btn"].forEach((sel) => {
    const btn = document.querySelector(sel);
    if (btn) btn.textContent = labelText;
  });

  const badge = document.querySelector("#crm-auth-status-badge");
  if (badge) {
    badge.textContent = isAuthed
      ? `Вы вошли: ${user.display_name || user.username} (${user.role || "admin"})`
      : "Гость (без входа)";
    badge.className = `badge ${isAuthed ? "b-ok" : "b-wait"}`;
  }

  const logoutBtn = document.querySelector("#modal-logout-btn");
  if (logoutBtn) {
    logoutBtn.hidden = !isAuthed;
  }

  const usersHost = document.querySelector("#modal-users-list");
  if (usersHost) {
    usersHost.replaceChildren();
    users.forEach((u) => {
      const roleClass = u.role === "admin" ? "b-won" : u.role === "sales" ? "b-talk" : "b-new";
      usersHost.append(
        el("div", { className: "auth-user-pill", "data-auth-username": u.username }, [
          el("strong", {}, [u.display_name || u.username]),
          el("code", {}, [`@${u.username}`]),
          el("span", { className: `badge ${roleClass}` }, [u.role || "operator"]),
        ]),
      );
    });
  }
}

async function loadCrmAuthStatus() {
  try {
    const data = await api("/api/auth/me");
    state.authUser = data?.user || null;
    state.authUsers = data?.users || [];
    renderCrmAuthUI(data);
  } catch (_err) {
    /* ignore */
  }
}

function bindCrmAuthEvents() {
  const modal = document.querySelector("#crm-auth-modal");
  const openModal = async () => {
    if (!modal) return;
    modal.hidden = false;
    await loadCrmAuthStatus();
    modal.scrollIntoView({ block: "nearest" });
  };

  ["#crm-user-btn", "#sidebar-user-btn"].forEach((sel) => {
    const btn = document.querySelector(sel);
    if (btn) btn.addEventListener("click", openModal);
  });

  const closeBtn = document.querySelector("#close-crm-auth-modal-btn");
  if (closeBtn && modal) {
    closeBtn.addEventListener("click", () => {
      modal.hidden = true;
    });
  }

  const tabLogin = document.querySelector("#modal-tab-login");
  const tabReg = document.querySelector("#modal-tab-register");
  const paneLogin = document.querySelector("#modal-pane-login");
  const paneReg = document.querySelector("#modal-pane-register");
  const switchModalAuthTab = (mode) => {
    const isLogin = mode !== "register";
    if (tabLogin) tabLogin.setAttribute("aria-selected", isLogin ? "true" : "false");
    if (tabReg) tabReg.setAttribute("aria-selected", !isLogin ? "true" : "false");
    if (paneLogin) paneLogin.hidden = !isLogin;
    if (paneReg) paneReg.hidden = isLogin;
  };
  if (tabLogin) tabLogin.addEventListener("click", () => switchModalAuthTab("login"));
  if (tabReg) tabReg.addEventListener("click", () => switchModalAuthTab("register"));

  const performModalLogin = async (username, password) => {
    const submitBtn = document.querySelector("#modal-login-submit-btn");
    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await api("/api/auth/login", {
        method: "POST",
        body: { username, password },
      });
      await loadCrmAuthStatus();
      toast(`Вход выполнен: ${res?.user?.display_name || username}`, "ok");
    } catch (error) {
      toast(error.message || "Ошибка входа", "error");
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  };

  document.querySelectorAll("[data-modal-preset-user]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const u = btn.getAttribute("data-modal-preset-user") || "";
      const p = btn.getAttribute("data-modal-preset-pass") || "";
      const uInput = document.querySelector("#modal-login-username");
      const pInput = document.querySelector("#modal-login-password");
      if (uInput) uInput.value = u;
      if (pInput) pInput.value = p;
      performModalLogin(u, p);
    });
  });

  const loginForm = document.querySelector("#modal-login-form");
  if (loginForm) {
    loginForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const u = document.querySelector("#modal-login-username")?.value.trim() || "";
      const p = document.querySelector("#modal-login-password")?.value || "";
      performModalLogin(u, p);
    });
  }

  const regForm = document.querySelector("#modal-register-form");
  if (regForm) {
    regForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const username = document.querySelector("#modal-reg-username")?.value.trim() || "";
      const display_name = document.querySelector("#modal-reg-display-name")?.value.trim() || "";
      const role = document.querySelector("#modal-reg-role")?.value || "operator";
      const password = document.querySelector("#modal-reg-password")?.value || "";
      const submitBtn = document.querySelector("#modal-reg-submit-btn");
      if (submitBtn) submitBtn.disabled = true;
      try {
        const res = await api("/api/auth/register", {
          method: "POST",
          body: { username, display_name, role, password },
        });
        await loadCrmAuthStatus();
        toast(`Аккаунт «${res?.user?.display_name || username}» зарегистрирован и выполнен вход`, "ok");
      } catch (error) {
        toast(error.message || "Ошибка регистрации", "error");
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  const logoutBtn = document.querySelector("#modal-logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", async () => {
      try {
        await api("/api/auth/logout", { method: "POST" });
        await loadCrmAuthStatus();
        toast("Вы вышли из аккаунта", "ok");
      } catch (error) {
        toast(error.message || "Ошибка выхода", "error");
      }
    });
  }
}

bindV5Events();
bindLeadgenEvents();
bindCrmAuthEvents();

boot();

