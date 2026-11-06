#!/usr/bin/env python3
"""Фильтр вакансий на TypeSafe Jev (System One) вместо генеративного LLM.

Заменяет двухстадийный LLM-отбор в job_monitor.py (triage + verdict) одним
типизированным запросом на пост: пять вопросов (искатель/слой/способ/удалёнка/
горячесть) задаются параллельно в одном вызове, ответ — вероятности, решение
складывается кодом. Jev не генерирует текст, поэтому извлечение полей
(role/why/pay/contact) для подошедших вакансий остаётся за LLM-экстрактором —
он вызывается только для фитов, которых в день единицы.
"""
import concurrent.futures as futures
import os
import time

import requests

API_URL = os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")
WORKERS = int(os.environ.get("JEV_WORKERS", "8"))

T_SEEKING = float(os.environ.get("JEV_T_SEEKING", "0.5"))
T_REMOTE = float(os.environ.get("JEV_T_REMOTE", "0.5"))
T_HOT = float(os.environ.get("JEV_T_HOT", "0.6"))

QUESTIONS = {
    "is_seeking": {
        "type": "noul",
        "instructions": (
            "The author of this Telegram post is a client or employer LOOKING "
            "FOR a person to do work: a job opening, a project order, a freelance "
            "task, an outsourcing request. The post invites people to apply."
        ),
        "criteria": {
            "true": "Someone offers paid work and waits for applications — "
                    "a vacancy, an order, a project, a one-off task, an "
                    "ongoing engagement, any format (staff, project, gig).",
            "false": "The opposite: a specialist's resume or self-promo "
                     "(\"I do\", \"I can help\", portfolio), an ad for courses "
                     "or services, a survey invitation, news, a reposted chat "
                     "message, small talk, a discussion thread, an easy-money "
                     "scheme (\"we need N people to work with AI, no experience "
                     "needed\"), network marketing.",
        },
    },
    "topic": {
        "type": "choice",
        "instructions": (
            "Which work domain does this post belong to? Judge the actual task "
            "or role described, not the chat it appeared in."
        ),
        "criteria": {
            "ai_content": "AI-generated content and SHORT VIDEO production: "
                          "AI/\"нейро\" creator, AI video generation, AI avatars "
                          "and lipsync, UGC creators, clippers/cutters, video "
                          "editing of Reels/Shorts/TikTok (with OR without AI "
                          "mentioned: editing studios and ongoing editing work "
                          "belong here), motion design, reels scriptwriting, "
                          "AI design and visual.",
            "automation": "AI automation and technical work: n8n/Make, Telegram "
                          "bots, sales funnels, ManyChat, AI agents, vibe-coding, "
                          "Claude Code, CRM integrations, parsing.",
            "traffic": "Traffic and content systems WHERE AI IS CENTRAL to the "
                       "job: SMM or Head of SMM explicitly driven by AI, AI "
                       "content factory, content producer building AI pipelines, "
                       "organic growth with AI tooling, launch producer with AI. "
                       "NOT here: manual SMM without AI, storymaking, Pinterest, "
                       "email/WhatsApp mass mailing, assistants and community "
                       "managers.",
            "ai_role": "Senior AI-first roles with real money: Head of Content "
                       "GenAI, Product Lead, CTO/tech partner in an AI agency, "
                       "AI Engineering Architect/Lead automating development "
                       "processes with AI. NOT classic data-science or "
                       "ML-research engineer roles.",
            "other": "Any other domain: accounting, sales or management without "
                     "AI, HR, offline or physical work, software development "
                     "without AI, design or copywriting without AI, crypto "
                     "schemes, easy-money/WFH schemes, paid training where the "
                     "applicant pays.",
            "not_a_vacancy": "This is not a job or order post at all: a resume, "
                             "self-promotion, an ad, a survey, a discussion.",
        },
    },
    "mode": {
        "type": "choice",
        "instructions": (
            "Assume a skilled operator wants to take this work. Which way of "
            "taking it does the post describe: piecework he must do himself, or "
            "an ongoing flow he can set up and hand to his own team?"
        ),
        "criteria": {
            "hands": "Pure piecework per item with no volume and no growth: "
                     "\"edit videos, 300 RUB per clip\", \"cut 20 videos\". "
                     "Nothing to delegate, no margin.",
            "delegate": "A flow, volume, long-term engagement or an operator "
                        "role: \"25+ clips per month ongoing\", content factory, "
                        "producer, tech partner, revenue share. Can be staffed "
                        "with hired people.",
            "both": "Piecework task, but the volume and regularity allow "
                    "putting a hired person on it.",
            "na": "This is not a job or order post at all.",
        },
    },
    "remote_ok": {
        "type": "noul",
        "instructions": (
            "Can this work be done fully remotely, from anywhere?"
        ),
        "criteria": {
            "true": "Remote is stated, or unstated but nothing requires a "
                    "specific place. An office mentioned only as an option "
                    "counts as remote-friendly. \"Remote from Europe/CIS\", "
                    "\"remote with relocation as an option\", \"need a person "
                    "on camera\" without a named city — all count as remote.",
            "false": "Office presence is required, or the work requires being "
                     "in a specific city (on-camera filming in a named city, "
                     "offline events, physical labor).",
        },
    },
    "hot": {
        "type": "noul",
        "instructions": (
            "Is this an unusually attractive opportunity: high stated pay, an "
            "ongoing stream of work, or squarely about AI video generation "
            "and AI avatars?"
        ),
        "criteria": {
            "true": "Pay is stated at 100,000 RUB / $1,000 per month or more, "
                    "OR it is a long-term engagement with a steady flow of "
                    "tasks, OR it directly seeks AI video generation / AI "
                    "avatar work.",
            "false": "None of the above: pay unstated or low, one-off task, "
                     "unrelated to AI video.",
        },
    },
    "mentions_ai": {
        "type": "noul",
        "instructions": (
            "Does the post mention AI / нейросети as part of the actual work: "
            "AI tools (HeyGen, Kling, Veo, Midjourney, ComfyUI, Runway, "
            "ElevenLabs), AI generation, AI avatars, нейроконтент — or AI is "
            "explicitly in the task? Mentions of AI in passing, in the chat "
            "name, or in marketing fluff do not count."
        ),
        "criteria": {
            "true": "AI tools or AI-generated content are part of the work "
                    "described, even as one of several duties.",
            "false": "No AI in the task at all: plain manual work.",
        },
    },
    "is_editing": {
        "type": "noul",
        "instructions": (
            "Is the core work video EDITING of ready footage — монтаж, нарезка, "
            "клипы из готового материала (downloads, webinars, podcasts, raw "
            "files)?"
        ),
        "criteria": {
            "true": "The person edits existing footage into clips.",
            "false": "The person creates/films content themselves (UGC "
                     "creator, креатор, актёр, блогер) or the work is not "
                     "video at all.",
        },
    },
}

LAYER_RU = {"ai_content": "AI-контент", "automation": "автоматизация",
            "traffic": "трафик", "ai_role": "AI-роль",
            "other": "нет", "not_a_vacancy": "нет"}
MODE_RU = {"hands": "руками", "delegate": "отдать своим", "both": "оба", "na": ""}


def system_one(state, questions=None, timeout=60, retries=5):
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("нет TYPESAFE_API_KEY")
    payload = {"state": state, "model": MODEL,
               "questions": questions or QUESTIONS}
    for attempt in range(retries):
        try:
            r = requests.post(API_URL, json=payload, timeout=timeout,
                              headers={"Authorization": f"Bearer {key}"})
        except requests.RequestException as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"jev недоступен: {e}") from e
        if r.status_code in (429, 529) and attempt < retries - 1:
            wait = float(r.headers.get("retry-after") or 2 ** attempt)
            time.sleep(min(wait, 30))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"jev HTTP {r.status_code}: {r.text[:300]!r}")
        return r.json()
    raise RuntimeError("jev: исчерпаны повторы")


def verdict(text, source=""):
    state = {"source_chat": source, "post": text}
    ans = system_one(state)["answers"]
    topic = ans["topic"]
    layer = LAYER_RU.get(topic["choice"], "нет")
    mode = ans["mode"]["choice"]
    fit = (ans["is_seeking"]["noul"] >= T_SEEKING
           and topic["choice"] not in ("other", "not_a_vacancy")
           and ans["remote_ok"]["noul"] >= T_REMOTE)
    if (fit and topic["choice"] == "ai_content" and mode == "hands"
            and ans["mentions_ai"]["noul"] < 0.5
            and ans["is_editing"]["noul"] >= 0.5):
        fit = False
    return {
        "fit": bool(fit),
        "layer": layer,
        "mode": MODE_RU.get(mode, ""),
        "remote": ans["remote_ok"]["noul"] >= T_REMOTE,
        "hot": bool(ans["hot"]["noul"] >= T_HOT),
        "jev": {"seeking": ans["is_seeking"]["noul"],
                "topic": topic["probabilities"],
                "mode": ans["mode"]["probabilities"],
                "remote": ans["remote_ok"]["noul"],
                "hot": ans["hot"]["noul"],
                "mentions_ai": ans["mentions_ai"]["noul"],
                "is_editing": ans["is_editing"]["noul"]},
    }


def judge_all(posts, workers=WORKERS):
    out = []

    def one(p):
        try:
            return p, verdict(p["text"], p.get("src", ""))
        except Exception as e:
            return p, {"fit": False, "layer": "", "mode": "", "remote": False,
                       "hot": False, "error": f"jev: {type(e).__name__}: {e}"}

    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for p, v in ex.map(one, posts):
            p["v"] = v
            out.append(p)
    return out
