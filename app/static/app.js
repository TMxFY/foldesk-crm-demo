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

const state = {
  mode: "crm",
  scope: "open",
  loginId: null,
  accounts: [],
  leads: [],
  reminders: [],
  dialogs: [],
  dialogQuery: "",
  leadQuery: "",
  inbox: "empty",
  selectedDialog: null,
  selectedLeadId: null,
};

let modalSubmit = null;
let modalReturn = null;
let toastTimer = 0;

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

function textField(lead, field, label) {
  return el("input", {
    value: lead[field] || "",
    "aria-label": label,
    onchange: (event) => patchLead(lead.id, { [field]: event.target.value }),
  });
}

async function patchLead(id, body) {
  try {
    await api(`/api/leads/${id}`, { method: "PATCH", body });
    await loadLeads(state.leadQuery);
  } catch (error) {
    toast(error.message, "error");
    await loadLeads(state.leadQuery);
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

function renderAccounts() {
  const list = document.querySelector("#account-list");
  list.replaceChildren();
  if (!state.accounts.length) {
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
    if (account.status === "pending_code" || account.status === "pending_password") {
      pending = account;
      const caption = account.status === "pending_password" ? "Ввести пароль" : "Ввести код";
      row.append(el("button", { type: "button", className: "btn ghost sm", onclick: () => resumeLogin(account) }, [caption]));
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
  setView("#accounts-view", "filled");
}

async function loadAccounts() {
  state.accounts = await api("/api/accounts");
  renderAccounts();
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
  document.querySelector("#messages").replaceChildren();
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

function selectDialog(dialog) {
  state.selectedDialog = dialog;
  setView("#thread-view", "loading");
  renderDialogs();
  renderThread();
  renderSide();
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
      el("p", { className: "note" }, [lead.note || "Заметок пока нет"]),
      el("p", { className: "hint" }, [lead.next_step ? `Дальше: ${lead.next_step}` : "Следующий шаг не задан"]),
      el("a", { className: "btn secondary sm", href: "#s09" }, ["Открыть карточку"]),
    ]),
  );
  setView("#side-view", "filled");
  renderCard();
  renderBoard();
}

function selectedLead() {
  return state.leads.find((lead) => lead.id === state.selectedLeadId) || null;
}

function openNoteModal(lead) {
  const area = el("textarea", { id: "note-body", name: "body" });
  area.value = lead.note || "";
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
    await patchLead(lead.id, { note: body });
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

function renderCard() {
  const lead = selectedLead();
  if (!lead) {
    setView("#card-view", "empty");
    return;
  }
  const notes = [];
  if (lead.note) {
    notes.push(el("li", {}, [
      el("span", { className: "dot", "aria-hidden": "true" }, []),
      el("div", { className: "note" }, [
        el("p", {}, [lead.note]),
        el("p", { className: "meta" }, ["Заметка"]),
      ]),
    ]));
  }
  state.reminders.filter((item) => item.lead_id === lead.id).forEach((item) => {
    notes.push(el("li", {}, [
      el("span", { className: "dot", "aria-hidden": "true" }, []),
      el("div", {}, [
        el("p", {}, [item.text]),
        el("p", { className: "meta" }, [el("time", { datetime: item.due_at || "" }, [formatDue(item.due_at)])]),
      ]),
    ]));
  });
  const timeline = notes.length
    ? el("ol", { className: "timeline" }, notes)
    : el("p", { className: "hint" }, ["Заметок пока нет"]);
  document.querySelector("#lead-card").replaceChildren(
    el("div", { className: "panel", "aria-labelledby": "card-name" }, [
      el("h3", { id: "card-name", className: "name" }, [lead.name || "Без имени"]),
      el("label", {}, ["Имя", textField(lead, "name", "Имя")]),
      el("label", {}, ["Статус", statusSelect(lead)]),
      el("p", { className: "hint" }, [
        lead.username ? `@${lead.username}` : "без ника",
        lead.phone ? ` · ${lead.phone}` : "",
        lead.telegram_user_id ? ` · ${lead.telegram_user_id}` : "",
      ]),
      el("label", {}, ["Следующий шаг", textField(lead, "next_step", "Следующий шаг")]),
      timeline,
      el("div", { className: "row" }, [
        el("button", { type: "button", className: "btn secondary sm", onclick: () => openNoteModal(lead) }, ["Заметка"]),
        el("button", { type: "button", className: "btn secondary sm", onclick: () => openReminderModal(lead) }, ["Напоминание"]),
        el("button", { type: "button", className: "btn ghost sm", onclick: () => openChat(lead) }, ["Открыть чат"]),
      ]),
    ]),
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
  return el("form", {
    className: "reminder-form",
    onsubmit: async (event) => {
      event.preventDefault();
      const due = fromDateAndTime(date.value, time.value);
      if (!due || !text.value.trim()) {
        toast("Укажите дату, время и текст", "error");
        return;
      }
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

async function loadLeads(query) {
  state.leadQuery = query || "";
  setView("#leads-view", "loading");
  setView("#board-view", "loading");
  const params = new URLSearchParams();
  if (state.leadQuery) params.set("q", state.leadQuery);
  const leads = await api(`/api/leads?${params.toString()}`);
  state.leads = leads;
  if (state.selectedLeadId && !leads.some((lead) => lead.id === state.selectedLeadId)) {
    state.selectedLeadId = null;
  }
  renderLeadTable();
  renderBoard();
  renderCard();
  renderSide();
  setView("#leads-view", leads.length ? "filled" : "empty");
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
    el("p", { className: "err", hidden: "hidden" }, ["Сервер пока не принимает новый срок."]),
    el("button", { type: "submit", className: "btn ghost sm" }, ["Сохранить срок"]),
  ]);
  postpone.addEventListener("submit", (event) => {
    event.preventDefault();
    const err = postpone.querySelector(".err");
    err.hidden = false;
    toast("Сервер пока не принимает новый срок.", "error");
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
      onclick: () => {
        state.selectedLeadId = reminder.lead_id;
        renderCard();
        renderBoard();
      },
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
});

bindBusy(document.querySelector("#password-form"), async (event) => {
  const password = new FormData(event.target).get("password");
  await api(`/api/accounts/${state.loginId}/password`, { method: "POST", body: { password } });
  document.querySelector("#password-form").hidden = true;
  document.querySelector("#code-form").hidden = true;
  await loadAccounts();
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
      },
    });
    event.target.reset();
    state.selectedLeadId = lead.id;
    await loadLeads(state.leadQuery);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
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
    state.selectedLeadId = lead.id;
    await loadLeads(state.leadQuery);
    toast("Записан в лиды", "ok");
  } catch (error) {
    document.querySelector("#side-error").textContent = error.message;
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});

document.querySelector("#reply-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const error = document.querySelector("#thread-error");
  error.hidden = false;
  error.textContent = "Отправка в чат ещё не подключена.";
  toast(error.textContent, "error");
});

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
    await loadAccounts();
  } catch (error) {
    failRegion("#accounts-view", "#accounts-error", error);
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
