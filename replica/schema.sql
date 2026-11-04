-- Fresh-database target for the local Telegram CRM.
-- Applicator today: app/db.py:init_db (accounts, leads, reminders only).
-- Do not pipe this file into an existing data/crm.db: the CREATEs will fail,
-- and CREATE TABLE IF NOT EXISTS would not add columns or ON DELETE rules.
-- Upgrade from that file is the commented block at the bottom.
--
-- SQLite has no timestamptz. Times are UTC text YYYY-MM-DDTHH:MM:SS,
-- same as app/db.py:utc_now. The caller sets PRAGMA foreign_keys = ON
-- (connect already does). journal_mode=WAL belongs in connect, not here:
-- it cannot run inside the transaction executescript opens.
--
-- One local operator. No user_id / org_id. Access checks live in the data layer.
-- Ids are INTEGER PRIMARY KEY AUTOINCREMENT so accounts.id is never reused:
-- the session file is data/sessions/{id}.session.

-- --- milestone 1 tables, plus nullable columns the later milestones add ---

create table accounts (
  id integer primary key autoincrement,
  phone text not null,
  session_path text not null,
  status text not null check (
    status in ('pending_code', 'pending_password', 'connected', 'error')
  ),
  phone_code_hash text,
  telegram_user_id integer,
  telegram_username text,
  telegram_name text,
  created_at text not null,
  -- null on rows written by the current start_login insert
  health text check (
    health is null
    or health in ('active', 'offline', 'unauthorized', 'banned', 'frozen')
  ),
  last_error text,
  updated_at text
);

create unique index accounts_tg_user
  on accounts(telegram_user_id)
  where telegram_user_id is not null;

create index accounts_status on accounts(status);
create index accounts_health on accounts(health);

create table settings (
  id integer primary key check (id = 1),
  timezone text not null default 'UTC',
  created_at text not null,
  updated_at text not null
);

-- One local operator web session. /start from BOT_CHAT_ID sets logged_in.
-- Applicator: app/db.py init_db. Not a Telegram account session file.
create table operator_session (
  id integer primary key check (id = 1),
  logged_in integer not null default 0 check (logged_in in (0, 1)),
  chat_id text,
  logged_in_at text
);

-- Labels for leads.status. Keys stay the v1 set. Do not delete these rows
-- while leads.status still has its check. Custom stage keys are out of scope
-- until that check is dropped on purpose.
create table pipeline_stages (
  key text primary key check (
    key in ('new', 'talking', 'waiting', 'won', 'lost')
  ),
  label text not null,
  position integer not null unique,
  color text,
  created_at text not null,
  updated_at text not null
);

-- milestone 3 (folder sync). Created now so leads.folder_id has a target.
create table folders (
  id integer primary key autoincrement,
  account_id integer not null references accounts(id) on delete cascade,
  telegram_folder_id integer not null,
  title text not null,
  sync_enabled integer not null default 0 check (sync_enabled in (0, 1)),
  blocked_reason text,
  created_at text not null,
  updated_at text not null,
  unique (account_id, telegram_folder_id)
);

create table leads (
  id integer primary key autoincrement,
  account_id integer references accounts(id) on delete set null,
  telegram_user_id integer,
  username text,
  name text,
  phone text,
  status text not null default 'new' check (
    status in ('new', 'talking', 'waiting', 'won', 'lost')
  ),
  note text,
  next_step text,
  created_at text not null,
  updated_at text not null,
  description text,
  email text,
  url text,
  folder_id integer references folders(id) on delete set null
);

-- Same name as app/db.py. Hard dedup: one lead per account + telegram user.
-- Same person on two accounts is two rows; find_duplicates only warns.
create unique index leads_account_tg_unique
  on leads(account_id, telegram_user_id)
  where telegram_user_id is not null;

create index leads_account on leads(account_id);
create index leads_status_updated on leads(status, updated_at);
create index leads_username on leads(username);
create index leads_phone on leads(phone);
create index leads_email on leads(email);
create index leads_folder on leads(folder_id);
create index leads_tg_user
  on leads(telegram_user_id)
  where telegram_user_id is not null;

create table notes (
  id integer primary key autoincrement,
  lead_id integer not null references leads(id) on delete cascade,
  body text not null check (length(trim(body)) > 0),
  source text not null default 'manual' check (source in ('manual', 'forwarded')),
  created_at text not null,
  updated_at text not null
);

create index notes_lead_created on notes(lead_id, created_at);

create table reminders (
  id integer primary key autoincrement,
  lead_id integer not null references leads(id) on delete cascade,
  due_at text not null,
  text text not null check (length(trim(text)) > 0),
  done_at text,
  created_at text not null,
  title text,
  updated_at text
);

create index reminders_lead on reminders(lead_id);
create index reminders_open_due on reminders(due_at) where done_at is null;

-- milestone 3 checkbox on the card. Dated reminders stay in reminders.
create table tasks (
  id integer primary key autoincrement,
  lead_id integer not null references leads(id) on delete cascade,
  title text not null check (length(trim(title)) > 0),
  done_at text,
  created_at text not null,
  updated_at text not null
);

create index tasks_lead_done on tasks(lead_id, done_at);

create table lead_events (
  id integer primary key autoincrement,
  lead_id integer not null references leads(id) on delete cascade,
  type text not null check (type in ('status', 'system')),
  body text not null,
  created_at text not null,
  updated_at text not null
);

create index lead_events_lead_created on lead_events(lead_id, created_at);

-- Inbox cache. Telegram is the source; rows may be deleted and refilled.
-- peer_type is part of the key: a user id and a chat id can share a number.
create table dialogs (
  id integer primary key autoincrement,
  account_id integer not null references accounts(id) on delete cascade,
  peer_type text not null default 'user' check (
    peer_type in ('user', 'chat', 'channel')
  ),
  peer_id integer not null,
  username text,
  title text,
  is_group integer not null default 0 check (is_group in (0, 1)),
  unread_count integer not null default 0 check (unread_count >= 0),
  last_message_at text,
  last_message_preview text,
  last_from_peer integer not null default 0 check (last_from_peer in (0, 1)),
  muted integer not null default 0 check (muted in (0, 1)),
  lead_id integer references leads(id) on delete set null,
  created_at text not null,
  updated_at text not null,
  unique (account_id, peer_type, peer_id)
);

create index dialogs_inbox on dialogs(account_id, last_message_at);
create index dialogs_lead on dialogs(lead_id);
create index dialogs_username on dialogs(username);
create index dialogs_private_unread
  on dialogs(account_id, unread_count)
  where peer_type = 'user';

create table messages (
  id integer primary key autoincrement,
  dialog_id integer not null references dialogs(id) on delete cascade,
  telegram_message_id integer not null,
  direction text not null check (direction in ('in', 'out')),
  text text,
  sent_at text not null,
  reply_to_telegram_message_id integer,
  created_at text not null,
  updated_at text not null,
  unique (dialog_id, telegram_message_id)
);

create index messages_thread on messages(dialog_id, sent_at);

-- milestone 3 custom fields. Built-in status is leads.status, not a row here.
create table property_defs (
  id integer primary key autoincrement,
  object_type text not null check (object_type in ('lead', 'account')),
  key text not null,
  name text not null,
  type text not null check (
    type in (
      'text', 'textarea', 'single_select', 'multi_select',
      'url', 'email', 'tel', 'amount'
    )
  ),
  options_json text not null default '[]',
  required integer not null default 0 check (required in (0, 1)),
  position integer not null default 0,
  created_at text not null,
  updated_at text not null,
  unique (object_type, key)
);

create index property_defs_object on property_defs(object_type, position);

create table property_values (
  id integer primary key autoincrement,
  property_id integer not null references property_defs(id) on delete cascade,
  lead_id integer references leads(id) on delete cascade,
  account_id integer references accounts(id) on delete cascade,
  value_text text,
  value_int integer, -- cents when type = amount
  currency text,
  created_at text not null,
  updated_at text not null,
  check (
    (lead_id is not null and account_id is null)
    or (lead_id is null and account_id is not null)
  )
);

create index property_values_property on property_values(property_id);
create index property_values_lead on property_values(lead_id);
create index property_values_account on property_values(account_id);

create unique index property_values_one_lead
  on property_values(property_id, lead_id)
  where lead_id is not null;

create unique index property_values_one_account
  on property_values(property_id, account_id)
  where account_id is not null;

create table pipeline_views (
  id integer primary key autoincrement,
  name text not null,
  filters_json text not null default '{}',
  is_default integer not null default 0 check (is_default in (0, 1)),
  created_at text not null,
  updated_at text not null
);

create unique index pipeline_views_one_default
  on pipeline_views(is_default)
  where is_default = 1;

create view lead_timeline as
select lead_id, 'note' as kind, id as ref_id, body as text, created_at
from notes
union all
select lead_id, 'reminder', id, coalesce(title, text), created_at
from reminders
union all
select lead_id, type, id, body, created_at
from lead_events;

insert into settings (id, timezone, created_at, updated_at)
values (1, 'UTC', strftime('%Y-%m-%dT%H:%M:%S', 'now'), strftime('%Y-%m-%dT%H:%M:%S', 'now'));

insert into pipeline_stages (key, label, position, color, created_at, updated_at)
values
  ('new', 'Лид', 1, null, strftime('%Y-%m-%dT%H:%M:%S', 'now'), strftime('%Y-%m-%dT%H:%M:%S', 'now')),
  ('talking', 'Разговор', 2, null, strftime('%Y-%m-%dT%H:%M:%S', 'now'), strftime('%Y-%m-%dT%H:%M:%S', 'now')),
  ('waiting', 'Ждём', 3, null, strftime('%Y-%m-%dT%H:%M:%S', 'now'), strftime('%Y-%m-%dT%H:%M:%S', 'now')),
  ('won', 'Выигран', 4, null, strftime('%Y-%m-%dT%H:%M:%S', 'now'), strftime('%Y-%m-%dT%H:%M:%S', 'now')),
  ('lost', 'Проигран', 5, null, strftime('%Y-%m-%dT%H:%M:%S', 'now'), strftime('%Y-%m-%dT%H:%M:%S', 'now'));

-- Upgrade sketch for a database that app/db.py:init_db already created.
-- Run by hand inside a migration, not as part of the creates above.
-- SQLite cannot add ON DELETE to an existing foreign key; leave the three
-- original FKs as NO ACTION until a copy-and-rebuild. disconnect_account
-- must UPDATE leads SET account_id = NULL before DELETE FROM accounts.
--
-- alter table accounts add column health text;
-- alter table accounts add column last_error text;
-- alter table accounts add column updated_at text;
-- alter table leads add column description text;
-- alter table leads add column email text;
-- alter table leads add column url text;
-- alter table leads add column folder_id integer references folders(id) on delete set null;
-- alter table reminders add column title text;
-- alter table reminders add column updated_at text;
-- Then CREATE the new tables (not accounts/leads/reminders) and the new indexes.
-- CREATE UNIQUE INDEX leads_account_tg_unique already exists; do not rename it.
-- After new tables exist, INSERT OR IGNORE the settings row and the five stages.
