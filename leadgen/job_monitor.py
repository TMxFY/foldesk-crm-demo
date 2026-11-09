#!/usr/bin/env python3
"""Монитор вакансий в телеграме для Foldesk CRM (crmtg/leadgen).

Читает чаты вакансий из уже существующих диалогов рабочего аккаунта (@tim_cros,
tim2.session), отсеивает мусор через TypeSafe Jev (job_jev.py) с LLM-карточками
фитов, сохраняет job_monitor_<YYYY-MM-DD>.md и .json для следующего шага
(job_reply.py -> прямая заливка в CRM через MCP ingest_discovered_leads).
"""
import argparse
import atexit
import concurrent.futures as futures
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zlib

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

try:
    from leadgen import job_jev
except ImportError:
    try:
        import job_jev
    except ImportError:
        job_jev = None

try:
    from leadgen import relay_route
except ImportError:
    try:
        import relay_route
    except ImportError:
        relay_route = None

VAULT = "/Users/timofei/obsidian/Second Brain"
SESSION = os.environ.get(
    "TG_JOBS_SESSION",
    os.path.expanduser("~/.claude/skill-telegram/tim2.session"),
)

_DEFAULT_STATE_DIR = os.environ.get("LEADGEN_STATE_DIR") or (
    "/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline"
    if os.path.isdir("/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline")
    else str(WORKTREE_ROOT / "data" / "leadgen_state")
)
OUTDIR = _DEFAULT_STATE_DIR
STATE = os.path.join(OUTDIR, "job_monitor_seen.json")
LEGACY_STATE = os.path.join(VAULT, ".raw", "data", "job_monitor_seen.json")
SAVED = os.path.join(VAULT, ".raw", "data", "tim_saved_jobs.jsonl")

STATE_TTL_DAYS = 45
PAUSE_BETWEEN_CHATS = 1.5
WORKERS = 4

SOURCE_WORDS = re.compile(
    r"ваканс|vacanc|работ[аыу]|\bjob|фриланс|freelance|биржа|удал[её]н|подработ"
    r"|\bhr\b|hr'?tg|найм|hiring|карьер|career|заказ",
    re.I,
)
SOURCE_STOP = re.compile(r"kurilo|аренда|прокат|spam|premium bot|wallet", re.I)
EXTRA_USERS = {"itconnectbelgrade"}
EXTRA_TITLE = re.compile(r"\bit connect\b", re.I)


def _load_vacancies_ui_config() -> dict:
    try:
        from app.leadgen_settings import get_leadgen_config

        return (get_leadgen_config().get("vacancies") or {})
    except Exception:
        pass
    cfg_path = Path(os.environ.get("LEADGEN_CONFIG_PATH") or (WORKTREE_ROOT / "data" / "leadgen_config.json"))
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("vacancies"), dict):
                return data["vacancies"]
        except Exception:
            pass
    return {}


def apply_vacancies_config(vac_cfg: dict | None = None) -> dict:
    global PAUSE_BETWEEN_CHATS, SOURCE_WORDS, SOURCE_STOP, EXTRA_USERS
    cfg = vac_cfg if isinstance(vac_cfg, dict) else _load_vacancies_ui_config()
    if cfg.get("source_words"):
        try:
            SOURCE_WORDS = re.compile(str(cfg["source_words"]), re.I)
        except re.error:
            pass
    if cfg.get("source_stop"):
        try:
            SOURCE_STOP = re.compile(str(cfg["source_stop"]), re.I)
        except re.error:
            pass
    if isinstance(cfg.get("extra_chats"), list):
        EXTRA_USERS = {str(u).strip().lower().lstrip("@") for u in cfg["extra_chats"] if str(u or "").strip()}
    if cfg.get("pause_between_chats") is not None:
        try:
            PAUSE_BETWEEN_CHATS = max(0.0, float(cfg["pause_between_chats"]))
        except (TypeError, ValueError):
            pass
    return cfg


apply_vacancies_config()

RESUME_RX = re.compile(
    r"#помогу|#резюме|#ищу_?работу|ищу работу|ищу подработ|ищу заказы"
    r"|ищу проект[ыи]?|возьму заказ|возьму проект|свободен для|открыт[ае]? для"
    r"|мо[её] портфолио|моё резюме|мое резюме|о себе:|стаж работы",
    re.I,
)
CONTACT_RX = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,31}|t\.me/|https?://")
NOISE_RX = re.compile(
    r"^\s*(спасибо|ок|окей|привет|да|нет|\+|плюс|готово|принял[аи]?)\b",
    re.I,
)

TRIAGE = """Ниже {n} сообщений из телеграм-чатов с вакансиями. Отбери те, где
ЗАКАЗЧИК ИЩЕТ ИСПОЛНИТЕЛЯ по теме: AI-контент, видео, UGC, монтаж, соцсети и
трафик, AI-автоматизация, боты, воронки, AI-разработка.

Выкидывай: резюме и саморекламу исполнителей («делаю», «помогу», «мой опыт»,
портфолио), рекламу курсов и услуг, приглашения на опросы, обсуждения и болтовню
в чате, офлайн и физический труд, вакансии без всякой связи с темой выше
(бухгалтер, продавец, грузчик, оператор колл-центра).

Сомневаешься — оставляй: дальше есть второй, более строгий проход.

{items}

Верни СТРОГО один JSON-массив номеров тех сообщений, которые прошли:
[1, 4, 7]
Ничего кроме массива."""

VERDICT = """Ты отбираешь вакансии и заказы для Тима. Он — практик AI-UGC:
делает контент нейросетями, строит органические воронки и контент-заводы, и
раздаёт руки — часть работы выполняет сам, часть отдаёт своей команде.

Что он забирал сам (выведено из 410 его сохранённых объявлений, не выдумано):
1. AI-контент и короткое вертикальное видео — нейрокреатор, AI-видео,
   ИИ-аватары и липсинк, UGC для Reels/Shorts/TikTok, монтаж вертикалок,
   рилсмейкер, клипер-нарезчик, AI-дизайн и визуал, сценарист под рилсы.
2. AI-автоматизация и техспец — n8n, Make, телеграм-боты, автоворонки,
   ManyChat, AI-агенты, вайб-кодинг, Claude Code, интеграции с CRM, парсинг.
3. Трафик и контент-система — SMM и Head of SMM, где реально нужен AI,
   контент-завод, контент-продюсер, УБТ и органика, growth и воронки,
   продюсер запусков.
4. Сильные AI-first роли — Head of Content GenAI, Product Lead, CTO или
   техпартнёр в AI-агентство, инженер в AI-продукт. Здесь планка выше: берём,
   только если AI в ядре продукта и оплата или доля названы серьёзные.

Условия, которые выполнялись почти во всех его сохранённых:
— работа удалённая (офис допустим только как опция, не требование);
— в объявлении есть контакт для отклика;
— ищут исполнителя, а не предлагают свои услуги;
— формат любой: штат, проект, разовый заказ, подряд, партнёрство.

Чего среди сохранённых нет вообще: резюме исполнителей, офлайн и физический
труд, продажи и менеджмент без AI, HR, бухгалтерия, обычная разработка без AI,
дизайн и копирайтинг без AI, обучение с оплатой ОТ соискателя, «заработок на
дому», крипто-схемы и опросы.

Объявление:
Источник: {source}
{text}

Верни СТРОГО один JSON, без пояснений:
{{"fit": true/false,
  "layer": "AI-контент|автоматизация|трафик|AI-роль|нет",
  "mode": "руками|отдать своим|оба",
  "role": "должность или суть заказа, до 6 слов",
  "why": "одной строкой по-русски, почему подходит именно ему — по фактам из
          объявления, без общих слов; если не подходит — почему нет",
  "pay": "оплата как написана в объявлении; не написана — пусто",
  "contact": "@ник или иной способ отклика из текста; нет — пусто",
  "remote": true/false,
  "hot": true/false}}

Правила:
— fit=false для резюме и саморекламы исполнителя, даже если тема совпала.
— fit=false, если AI, контент, трафик или автоматизация в задаче не при чём.
— fit=false, если требуется присутствие в офисе или личное появление в кадре в
  конкретном городе.
— «Нужен человек в кадр» без привязки к городу — fit=true.
— hot=true, если названа оплата от 100 000 ₽ / $1000 в месяц, или это долгосрок
  с потоком задач, или прямо ищут AI-видео и аватары.
— «pay» и «contact» не выдумывай: чего нет в тексте — оставляй пустым."""

EXTRACT = """Из вакансии ниже вытащи поля. Она уже отобрана как подходящая —
фит не оценивай, только заполни карточку.

Объявление:
Источник: {source}
{text}

Верни СТРОГО один JSON, без пояснений:
{{"role": "должность или суть заказа, до 6 слов",
  "why": "одной строкой по-русски, почему подходит Тиму — по фактам из
          объявления (задача, объём, оплата), без общих слов",
  "pay": "оплата как написана; не написана — пусто",
  "contact": "@ник или иной способ отклика из текста; нет — пусто"}}

«pay» и «contact» не выдумывай: чего нет в тексте — оставляй пустым."""


def load_env():
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return
    candidates = [
        WORKTREE_ROOT / ".env",
        Path("/Users/timofei/dev/Python/new era/crmtg/.env"),
        Path("/Users/timofei/dev/Python/new era/creatoros/.env"),
        Path(os.path.expanduser("~/.telegram/.env")),
        Path(VAULT) / ".env",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            continue


def has_jev():
    return job_jev is not None and bool(os.environ.get("TYPESAFE_API_KEY"))


def _opencode_ask(prompt, timeout=300, model=None):
    opencode_bin = os.environ.get("OPENCODE_BIN", "/opt/homebrew/bin/opencode")
    chosen = model or os.environ.get("OPENCODE_MODEL", "relayfast/kimi-k3")
    cmd = [opencode_bin, "run", "-m", chosen, "--format", "json", prompt]
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:" + env.get("PATH", "")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd="/tmp", env=env)
    text = None
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "text":
            t = (data.get("part") or {}).get("text")
            if t:
                text = t
    if p.returncode != 0 or text is None:
        detail = (p.stderr or "").strip() or (p.stdout or "")[-400:] or "без вывода"
        raise RuntimeError(f"opencode exit {p.returncode}: {detail[-400:]}")
    return text


def ask_llm(prompt: str, timeout: int = 240) -> str:
    if relay_route is not None and (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY")):
        try:
            return relay_route.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=900,
                temperature=0.3,
                timeout=min(timeout, 90),
            )
        except Exception:
            pass
    try:
        return _opencode_ask(prompt, timeout=timeout)
    except Exception:
        return _opencode_ask(
            prompt,
            timeout=timeout,
            model=os.environ.get("OPENCODE_FALLBACK_MODEL", "relayfast/glm-5.3"),
        )


def known_sources():
    users, titles = set(), set()
    if not os.path.exists(SAVED):
        return users, titles
    for line in open(SAVED, encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        u = (r.get("src_user") or "").strip().lower().lstrip("@")
        if u:
            users.add(u)
        t = (r.get("src_title") or "").strip().lower()
        if len(t) > 6:
            titles.add(t)
    return users, titles


def sources(client, verbose=False):
    trusted_users, trusted_titles = known_sources()
    out = []
    for d in client.iter_dialogs():
        if d.is_user:
            continue
        title = d.name or ""
        if SOURCE_STOP.search(title):
            continue
        user = (getattr(d.entity, "username", "") or "").lower()
        by_name = bool(SOURCE_WORDS.search(title) or SOURCE_WORDS.search(user))
        by_tim = user in trusted_users or title.strip().lower() in trusted_titles
        by_extra = user in EXTRA_USERS or bool(EXTRA_TITLE.search(title))
        if not (by_name or by_tim or by_extra):
            continue
        if by_tim:
            why = "пересылал сам"
        elif by_extra and not by_name:
            why = "добавлен"
        else:
            why = "по названию"
        out.append({
            "entity": d.entity,
            "id": d.id,
            "title": title,
            "user": getattr(d.entity, "username", "") or "",
            "why": why,
        })
    out.sort(key=lambda s: (s["why"] != "пересылал сам", s["title"].lower()))
    if verbose:
        for s in out:
            print(f"  {s['id']:>15} @{s['user'][:20]:20} {s['title'][:44]:44} ({s['why']})")
    return out


def link(src, msg_id):
    if src["user"]:
        return f"https://t.me/{src['user']}/{msg_id}"
    sid = str(src["id"])
    if sid.startswith("-100"):
        return f"https://t.me/c/{sid[4:]}/{msg_id}"
    return ""


def fetch(client, srcs, hours, per_chat=400):
    from telethon.errors import FloodWaitError

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    posts = []
    for i, s in enumerate(srcs, 1):
        got, seen_all = 0, 0
        try:
            for m in client.iter_messages(s["entity"], limit=per_chat):
                seen_all += 1
                if m.date < since:
                    break
                text = (m.text or "").strip()
                if not text:
                    continue
                posts.append({
                    "src": s["title"],
                    "src_user": s["user"],
                    "chat_id": s["id"],
                    "id": m.id,
                    "date": m.date.isoformat(),
                    "text": text[:4000],
                    "url": link(s, m.id),
                })
                got += 1
        except FloodWaitError as e:
            print(f"  [{i}/{len(srcs)}] {s['title'][:38]:38} flood-wait {e.seconds}с — пропуск")
            continue
        except Exception as e:
            print(f"  [{i}/{len(srcs)}] {s['title'][:38]:38} не прочитан: {type(e).__name__}")
            continue
        cut = " ⚠ упёрлись в лимит, окно прочитано не целиком" if seen_all >= per_chat else ""
        print(f"  [{i}/{len(srcs)}] {s['title'][:38]:38} {got}{cut}")
        time.sleep(PAUSE_BETWEEN_CHATS)
    return posts


_MD = re.compile(r"[*_`>#\[\]()]|https?://\S+|@[A-Za-z0-9_]+|[^\w\s]", re.U)


def norm(text):
    t = _MD.sub(" ", (text or "").lower())
    t = re.sub(r"\d+", "0", t)
    return re.sub(r"\s+", " ", t).strip()


def sig(text, k=4, n=14):
    w = [x for x in norm(text).split() if len(x) > 2]
    if len(w) < k:
        return []
    sh = {zlib.crc32(" ".join(w[i:i + k]).encode()) for i in range(len(w) - k + 1)}
    return sorted(sh)[:n]


def hard(text):
    return hashlib.sha1(norm(text).encode()).hexdigest()[:16]


def same(a, b, need=5):
    return len(set(a) & set(b)) >= need


def dedup(posts):
    out = []
    for p in sorted(posts, key=lambda x: x["date"]):
        p["sig"], p["hash"] = sig(p["text"]), hard(p["text"])
        for q in out:
            if p["hash"] == q["hash"] or same(p["sig"], q["sig"]):
                if p["src"] not in q["also"]:
                    q["also"].append(p["src"])
                break
        else:
            p["also"] = []
            out.append(p)
    return out


def load_state():
    edge = (dt.date.today() - dt.timedelta(days=STATE_TTL_DAYS)).isoformat()
    merged = []
    seen_hashes = set()
    for path in (STATE, LEGACY_STATE):
        if not os.path.exists(path):
            continue
        try:
            rows = json.load(open(path, encoding="utf-8")).get("seen", [])
        except (json.JSONDecodeError, OSError):
            continue
        for r in rows:
            if r.get("date", "") < edge:
                continue
            h = r.get("hash")
            if h and h in seen_hashes:
                continue
            if h:
                seen_hashes.add(h)
            merged.append(r)
    return merged


def save_state(old, shown):
    os.makedirs(OUTDIR, exist_ok=True)
    today = dt.date.today().isoformat()
    rows = old + [
        {
            "date": today,
            "hash": p["hash"],
            "sig": p["sig"],
            "title": p["text"][:60].replace("\n", " "),
        }
        for p in shown
    ]
    json.dump({"updated": today, "seen": rows}, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)


def unseen(posts, state):
    out = []
    for p in posts:
        if any(p["hash"] == s.get("hash") or same(p["sig"], s.get("sig", [])) for s in state):
            continue
        out.append(p)
    return out


def prefilter(posts, min_len=150):
    out = []
    for p in posts:
        t = p["text"]
        if len(t) < min_len or NOISE_RX.match(t):
            continue
        if RESUME_RX.search(t[:400]):
            continue
        if not CONTACT_RX.search(t):
            continue
        out.append(p)
    return out


def _json_block(res, want_list=False):
    m = re.search(r"\[.*\]" if want_list else r"\{.*\}", res or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def triage(posts, batch=12):
    kept = []

    def one(chunk):
        items = "\n\n".join(f"[{i}] {p['text'][:420]}" for i, p in enumerate(chunk, 1))
        try:
            res = ask_llm(TRIAGE.format(n=len(chunk), items=items), timeout=240)
        except Exception:
            return chunk
        nums = _json_block(res, want_list=True)
        if not isinstance(nums, list):
            return chunk
        return [chunk[i - 1] for i in nums if isinstance(i, int) and 1 <= i <= len(chunk)]

    chunks = [posts[i:i + batch] for i in range(0, len(posts), batch)]
    with futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, part in enumerate(ex.map(one, chunks), 1):
            kept += part
            print(f"  пачка {i}/{len(chunks)}: осталось {len(part)}")
    return kept


def verdict(p):
    src = p["src"] + (f" (+ ещё {len(p['also'])} чата)" if p["also"] else "")
    try:
        res = ask_llm(VERDICT.format(source=src, text=p["text"][:3500]))
    except Exception as e:
        return {"fit": False, "why": f"модель не ответила: {type(e).__name__}"}
    v = _json_block(res)
    return v if isinstance(v, dict) else {"fit": False, "why": "ответ без JSON"}


def judge(posts):
    out = []
    with futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for p, v in zip(posts, ex.map(verdict, posts)):
            p["v"] = v
            out.append(p)
            mark = "ФИТ" if v.get("fit") else "мимо"
            print(f"  {mark:5} {v.get('layer','') or '-':14} {(v.get('role') or p['text'][:40]).strip()[:52]}")
    return out


def extract_card(p):
    src = p["src"] + (f" (+ ещё {len(p['also'])} чата)" if p["also"] else "")
    try:
        res = ask_llm(EXTRACT.format(source=src, text=p["text"][:3500]), timeout=240)
    except Exception as e:
        return {"why": f"карточка не собрана: {type(e).__name__}"}
    v = _json_block(res)
    return {k: x for k, x in (v or {}).items() if x} if isinstance(v, dict) else {}


def judge_jev(posts):
    judged = job_jev.judge_all(posts)
    failed = [p for p in judged if p["v"].get("error")]
    if failed:
        print(f"  jev не ответил по {len(failed)} — добиваю LLM")
        with futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for p, v in zip(failed, ex.map(verdict, failed)):
                p["v"] = v
    for p in judged:
        v = p["v"]
        mark = "ФИТ" if v.get("fit") else "мимо"
        print(f"  {mark:5} {v.get('layer','') or '-':14} {p['text'][:52].splitlines()[0]}")
    fits = [p for p in judged if p["v"].get("fit")]
    vac_cfg = apply_vacancies_config()
    enabled_layers = {k for k, v in (vac_cfg.get("layers") or {}).items() if v}
    if enabled_layers:
        fits = [
            p
            for p in fits
            if not (p["v"].get("layer") or "").strip() or (p["v"].get("layer") or "").strip() in enabled_layers
        ]
    if vac_cfg.get("remote_only", True):
        fits = [p for p in fits if p["v"].get("remote") is not False]
    if fits:
        print(f"\nкарточки для {len(fits)} фитов (LLM, только они):")
        with futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for p, card in zip(fits, ex.map(extract_card, fits)):
                p["v"].update(card)
                print(f"  ✓ {(card.get('role') or p['text'][:40]).strip()[:56]}")
    return judged


def report(fits, hours, checked):
    when = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Вакансии за {hours} ч — {when}",
        "",
        f"Проверено {checked} объявлений, подошло {len(fits)}.",
        "",
    ]
    groups = [
        ("Можно отдать своим", ("отдать своим", "оба")),
        ("Работа руками", ("руками",)),
        ("Не разобрано", ("",)),
    ]
    order = {"отдать своим": 0, "оба": 1, "руками": 2}

    for title, modes in groups:
        chunk = [p for p in fits if (p["v"].get("mode") or "") in modes]
        if not chunk:
            continue
        lines += [f"# {title} — {len(chunk)}", ""]
        chunk.sort(
            key=lambda x: (
                not x["v"].get("hot"),
                order.get(x["v"].get("mode", ""), 9),
                x["v"].get("layer", ""),
            )
        )
        for p in chunk:
            v = p["v"]
            lines += [
                f"## {'🔥 ' if v.get('hot') else ''}{v.get('role') or 'без названия'}",
                f"- **почему:** {v.get('why','')}",
            ]
            if v.get("pay"):
                lines.append(f"- **оплата:** {v['pay']}")
            if v.get("contact"):
                lines.append(f"- **отклик:** {v['contact']}")
            where = p["src"] + (f" + ещё {len(p['also'])}" if p["also"] else "")
            lines.append(f"- **где:** {where} · {p['url'] or 'ссылки нет'}")
            lines.append("")
    return "\n".join(lines)


def sidecar(fits):
    return [
        {
            "head": ("🔥 " if p["v"].get("hot") else "") + (p["v"].get("role") or "без названия"),
            "role": p["v"].get("role") or "",
            "why": p["v"].get("why") or "",
            "pay": p["v"].get("pay") or "",
            "contact": p["v"].get("contact") or "",
            "layer": p["v"].get("layer") or "",
            "mode": p["v"].get("mode") or "",
            "hot": bool(p["v"].get("hot")),
            "src": p["src"],
            "src_user": p["src_user"],
            "chat_id": p.get("chat_id"),
            "msg_id": p["id"],
            "url": p["url"],
            "text": p["text"],
            "hash": p["hash"],
            "sig": p["sig"],
        }
        for p in fits
    ]


def lock():
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, "job_monitor.lock")
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < 3600:
            sys.exit(f"монитор уже работает ({int(age / 60)} мин назад) — выхожу")
        os.remove(path)
    open(path, "w", encoding="utf-8").write(str(os.getpid()))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--per-chat", type=int, default=400)
    ap.add_argument("--fresh", action="store_true", help="не учитывать память о показанном")
    ap.add_argument("--sources", action="store_true", help="только показать найденные источники и выйти")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    if not a.sources:
        path = lock()
        atexit.register(lambda: os.path.exists(path) and os.remove(path))

    load_env()
    from telethon.sync import TelegramClient

    client = TelegramClient(SESSION, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    client.start()

    print("источники из диалогов:")
    srcs = sources(client, verbose=True)
    print(f"  всего: {len(srcs)}\n")
    if a.sources:
        try:
            client.disconnect()
        except Exception:
            pass
        return

    print(f"читаю сообщения за {a.hours} ч:")
    posts = fetch(client, srcs, a.hours, a.per_chat)
    try:
        client.disconnect()
    except Exception as e:
        print(f"  ⚠ disconnect: {type(e).__name__}: {e}")
    print(f"\nвсего сообщений: {len(posts)}")

    posts = prefilter(posts)
    print(f"после дешёвого фильтра: {len(posts)}")
    posts = dedup(posts)
    print(f"после дедупа по смыслу: {len(posts)}")

    state = [] if a.fresh else load_state()
    posts = unseen(posts, state)
    print(f"новых (не показывали раньше): {len(posts)}\n")
    if not posts:
        print("нового нет")
        return

    if has_jev():
        print("вердикты: typesafe jev")
        judged = judge_jev(posts)
    else:
        print("typesafe недоступен (нет TYPESAFE_API_KEY) — откат на LLM")
        print("быстрый отбор по теме:")
        cands = triage(posts)
        print(f"\nк разбору: {len(cands)}\n")
        judged = judge(cands) if cands else []
    fits = [p for p in judged if p["v"].get("fit")]
    vac_cfg = apply_vacancies_config()
    enabled_layers = {k for k, v in (vac_cfg.get("layers") or {}).items() if v}
    if enabled_layers:
        fits = [
            p
            for p in fits
            if not (p["v"].get("layer") or "").strip() or (p["v"].get("layer") or "").strip() in enabled_layers
        ]
    if vac_cfg.get("remote_only", True):
        fits = [p for p in fits if p["v"].get("remote") is not False]

    text = report(fits, a.hours, len(posts))
    out = a.out or os.path.join(OUTDIR, f"job_monitor_{dt.date.today()}.md")
    open(out, "w", encoding="utf-8").write(text)
    side = out[:-3] + ".json" if out.endswith(".md") else out + ".json"
    json.dump(sidecar(fits), open(side, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)
    print(f"файл: {out}\nданные: {side}")

    if not a.fresh:
        save_state(state, posts)


if __name__ == "__main__":
    main()
