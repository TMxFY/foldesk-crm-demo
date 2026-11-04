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

const state = {
  mode: "crm",
  scope: "open",
  loginId: null,
  accounts: [],
  leads: [],
  leadCache: {},
  reminders: [],
  dialogs: [],
  messages: [],
  dialogQuery: "",
  leadQuery: "",
  inbox: "empty",
  selectedDialog: null,
  selectedLeadId: null,
  cards: {},
  folderLists: {},
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
  const message = error && error.message ? error.message : "Ошибка запроса";
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
    throw new Error(typeof detail === "string" ? detail : "Ошибка запроса");
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
  const pad = (part) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
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
  document.querySelector("#code-form").hidden = account.status !== "pending_code";
  document.querySelector("#password-form").hidden = account.status !== "pending_password";
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

function renderAccounts() {
  const list = document.querySelector("#account-list");
  list.replaceChildren();
  if (!state.accounts.length) {
    fillFolderAccounts();
    setView("#accounts-view", "empty");
    return;
  }
  let pending = null;
  state.accounts.forEach((account) => {
    const label = account.telegram_name || account.telegram_username || account.phone;
    const pair = ACCOUNT_STATUS[account.status] || [account.status, "b-off"];
    const row = el("li", { className: "account" }, [
      el("span", { className: "name" }, [label]),
      el("span", { className: `badge ${pair[1]}` }, [pair[0]]),
    ]);
    if (account.health) {
      const healthPair = ACCOUNT_HEALTH[account.health] || [account.health, "b-off"];
      row.append(el("span", { className: `badge ${healthPair[1]}` }, [healthPair[0]]));
    }
    if (account.status === "pending_code" || account.status === "pending_password") {
      pending = account;
      const caption = account.status === "pending_password" ? "Ввести пароль" : "Ввести код";
      row.append(el("button", { type: "button", className: "btn ghost sm", onclick: () => resumeLogin(account) }, [caption]));
    }
    if (account.status === "connected") {
      row.append(el("button", { type: "button", className: "btn ghost sm", onclick: () => reconnectAccount(account) }, ["Переподключить"]));
    }
    list.append(row);
  });
  if (pending && state.loginId == null) resumeLogin(pending);
  const select = document.querySelector("#tg-account");
  const previous = select.value;
  const connected = state.accounts.filter((account) => account.status === "connected");
  select.replaceChildren();
  connected.forEach((account) => {
    const label = account.telegram_name || account.phone;
    select.append(el("option", { value: String(account.id) }, [label]));
  });
  if (previous) select.value = previous;
  fillFolderAccounts();
  setView("#accounts-view", "filled");
}

function fillFolderAccounts() {
  const select = document.querySelector("#folder-account");
  if (!select) return;
  const previous = select.value;
  const connected = state.accounts.filter((account) => account.status === "connected");
  select.replaceChildren();
  connected.forEach((account) => {
    const label = account.telegram_name || account.phone;
    select.append(el("option", { value: String(account.id) }, [label]));
  });
  if (previous) select.value = previous;
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

function chipOn(name) {
  const button = document.querySelector(`[data-chip="${name}"]`);
  return button && button.getAttribute("aria-pressed") === "true";
}

function passChips(dialog) {
  if (chipOn("waiting") && !dialog.waiting && !dialog.last_from_other) return false;
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
    const user = dialog.username ? `@${dialog.username}` : "без ника";
    const current = state.selectedDialog
      && state.selectedDialog.telegram_user_id === dialog.telegram_user_id;
    const who = el("span", {}, [
      el("span", { className: "name" }, [name]),
      el("span", { className: "preview" }, [user]),
    ]);
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
  document.querySelector("#thread-head").replaceChildren(
    el("h3", { className: "name" }, [name]),
    el("p", { className: "hint" }, [`С аккаунта ${accountLabel()}`]),
    el("p", { className: "hint" }, [dialog.username ? `@${dialog.username}` : "без ника"]),
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
  document.querySelector("#side-card").replaceChildren(
    el("div", { className: "panel" }, [
      el("h3", { className: "name" }, [lead.name || "Без имени"]),
      el("span", { className: `badge ${badge}` }, [label]),
      el("p", { className: "hint" }, [lead.username ? `@${lead.username}` : "без ника"]),
      el("p", { className: "note" }, [sideNoteText(lead)]),
      el("p", { className: "hint" }, [lead.next_step ? `Дальше: ${lead.next_step}` : "Следующий шаг не задан"]),
      el("a", { className: "btn secondary sm", href: "#s09" }, ["Открыть карточку"]),
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
    toast("Напоминание поставлено", "ok");
    return true;
  });
}

function openChat(lead) {
  const dialog = state.dialogs.find((item) => Number(item.telegram_user_id) === Number(lead.telegram_user_id));
  if (!dialog) {
    toast("Этот чат ещё не открыт в списке", "error");
    return;
  }
  selectDialog(dialog);
  document.querySelector("#s05").scrollIntoView({ block: "nearest" });
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
  return "—";
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
  state.reminders.filter((item) => item.lead_id === lead.id).forEach((item) => {
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
  const profile = [
    el("h3", { id: "card-name", className: "name" }, [lead.name || "Без имени"]),
    el("label", {}, ["Имя", textField(lead, "name", "Имя")]),
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
      el("p", { className: "hint" }, ["Срока у задачи нет. Дата остаётся у напоминания."]),
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

function updateFilterNote() {
  const stage = document.querySelector("#stage-filter").value;
  const on = Boolean(stage || state.leadQuery);
  document.querySelector("#filter-note").hidden = !on;
  document.querySelector("#board").dataset.filtered = on ? "true" : "false";
}

function renderBoard() {
  const stage = document.querySelector("#stage-filter").value;
  const board = document.querySelector("#board");
  board.replaceChildren();
  STATUSES.forEach(([value, label]) => {
    const cards = state.leads.filter((lead) => lead.status === value && (!stage || stage === value));
    const items = cards.length
      ? cards.map((lead) => {
        const current = lead.id === state.selectedLeadId;
        const link = el("a", {
          className: "card",
          href: "#s09",
          onclick: () => {
            state.selectedLeadId = lead.id;
            renderCard();
            renderBoard();
          },
        }, [
          el("span", { className: "name" }, [lead.name || "Без имени"]),
          el("span", { className: "meta" }, [lead.username ? `@${lead.username}` : "без ника"]),
        ]);
        if (current) link.setAttribute("aria-current", "true");
        return link;
      })
      : [el("p", { className: "hint" }, ["Пусто"])];
    const count = el("span", { className: "meta" }, [String(cards.length)]);
    board.append(el("section", { className: "col" }, [
      el("header", {}, [el("h3", {}, [label]), count]),
      ...items,
    ]));
  });
  updateFilterNote();
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
        toast("Напоминание поставлено", "ok");
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
  state.leads.forEach((lead) => {
    const row = el("tr", {
      onclick: () => {
        state.selectedLeadId = lead.id;
        renderCard();
        renderBoard();
      },
    }, [
      el("td", {}, [textField(lead, "name", "Имя")]),
      el("td", {}, [lead.username ? `@${lead.username}` : ""]),
      el("td", {}, [lead.telegram_user_id ?? ""]),
      el("td", {}, [lead.phone || ""]),
      el("td", {}, [statusSelect(lead)]),
      el("td", {}, [textField(lead, "note", "Заметка")]),
      el("td", {}, [textField(lead, "next_step", "Следующий шаг")]),
      el("td", {}, [reminderForm(lead)]),
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
}

async function loadReminders() {
  setView("#digest-view", "loading");
  state.reminders = await api(`/api/reminders?scope=${state.scope}`);
  renderReminders();
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
  const idx = state.dialogs.findIndex((row) => row.id === dialog.id);
  if (idx >= 0) state.dialogs[idx] = dialog;
  else state.dialogs.unshift(dialog);
  if (state.inbox !== "filled") state.inbox = "filled";
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
  // Пока сервер не прислал live ok, оборванный сокет не выглядит живым.
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
  const dialogs = await api(`/api/inbox?account_id=${select.value}`);
  state.dialogs = dialogs;
  state.dialogQuery = "";
  state.inbox = "filled";
  renderDialogs();
  openInboxLive(select.value);
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

bindBusy(document.querySelector("#login-form"), async (event) => {
  const phone = new FormData(event.target).get("phone");
  const result = await api("/api/accounts/start", { method: "POST", body: { phone } });
  state.loginId = result.id;
  document.querySelector("#code-form").hidden = false;
  document.querySelector("#password-form").hidden = true;
  await loadAccounts();
});

bindBusy(document.querySelector("#code-form"), async (event) => {
  const code = new FormData(event.target).get("code");
  const result = await api(`/api/accounts/${state.loginId}/code`, { method: "POST", body: { code } });
  document.querySelector("#password-form").hidden = result.status !== "pending_password";
  if (result.status === "connected") document.querySelector("#code-form").hidden = true;
  await loadAccounts();
  if (result.status === "connected") await loadInbox();
});

bindBusy(document.querySelector("#password-form"), async (event) => {
  const password = new FormData(event.target).get("password");
  await api(`/api/accounts/${state.loginId}/password`, { method: "POST", body: { password } });
  document.querySelector("#password-form").hidden = true;
  document.querySelector("#code-form").hidden = true;
  await loadAccounts();
  await loadInbox();
});

document.querySelector("#tg-account").addEventListener("change", () => {
  state.dialogQuery = "";
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

document.querySelector("#manual-lead").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError(null);
  const form = new FormData(event.target);
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const lead = await api("/api/leads", {
      method: "POST",
      body: {
        name: form.get("name"),
        username: form.get("username"),
        phone: form.get("phone"),
        note: form.get("note"),
        url: form.get("url"),
        email: form.get("email"),
      },
    });
    event.target.reset();
    state.leadCache[lead.id] = lead;
    await loadLeads(state.leadQuery, lead.id);
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
  renderBoard();
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
      field.value = "";
      renderThread();
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

document.querySelector(".nav").addEventListener("click", (event) => {
  const link = event.target.closest("a");
  if (!link) return;
  document.querySelectorAll(".nav a").forEach((item) => item.removeAttribute("aria-current"));
  link.setAttribute("aria-current", "page");
});

document.querySelector("#theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "" : "dark";
  if (next) document.documentElement.dataset.theme = next;
  else delete document.documentElement.dataset.theme;
  document.querySelector("#theme").setAttribute("aria-pressed", next ? "true" : "false");
  try { localStorage.setItem("theme", next || "light"); } catch (_err) { /* хранилище может быть закрыто */ }
});

try {
  if (localStorage.getItem("theme") === "dark") {
    document.documentElement.dataset.theme = "dark";
    document.querySelector("#theme").setAttribute("aria-pressed", "true");
  }
} catch (_err) { /* без хранилища остаётся светлая тема */ }

async function boot() {
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
    await loadReminders();
  } catch (error) {
    failRegion("#digest-view", "#digest-error", error);
  }
}

boot();
