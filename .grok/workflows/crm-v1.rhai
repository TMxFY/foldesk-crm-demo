let meta = #{
    name: "crm-v1",
    description: "Build CRM v1, compare it to the plan from several sides, keep only confirmed gaps, and prove the tests",
    when_to_use: "Implement or re-check the local Telegram CRM v1 in this repo.",
    phases: [
        #{ title: "Build", detail: "Implement docs/plans/v1-leads.md" },
        #{ title: "Compare", detail: "Plan, correctness, scope, and tests in parallel" },
        #{ title: "Verify", detail: "A skeptic tries to refute each finding" },
        #{ title: "Fix", detail: "Apply only confirmed gaps" },
        #{ title: "Prove", detail: "Run scripts/verify.sh" },
    ],
};

let repo = "/Users/timofei/dev/Python/new era/crmtg";

let findings_schema = #{
    "type": "object",
    "required": ["findings"],
    "properties": #{
        "findings": #{
            "type": "array",
            "maxItems": 8,
            "items": #{
                "type": "object",
                "required": ["file", "issue", "evidence"],
                "properties": #{
                    "file": #{ "type": "string" },
                    "issue": #{ "type": "string" },
                    "evidence": #{ "type": "string" },
                },
            },
        },
    },
};

let verdict_schema = #{
    "type": "object",
    "required": ["real", "reason", "evidence"],
    "properties": #{
        "real": #{ "type": "boolean" },
        "reason": #{ "type": "string" },
        "evidence": #{ "type": "string" },
    },
};

let proof_schema = #{
    "type": "object",
    "required": ["passed", "tail"],
    "properties": #{
        "passed": #{ "type": "boolean" },
        "tail": #{ "type": "string" },
    },
};

fn is_map(v) {
    type_of(v) == "map"
}

fn findings_of(slot) {
    if slot == () {
        return [];
    }
    if slot.success != true {
        return [];
    }
    if !is_map(slot.output) {
        return [];
    }
    if slot.output.findings == () {
        return [];
    }
    slot.output.findings
}

fn proof_passed(slot) {
    if slot == () {
        return false;
    }
    if slot.success != true {
        return false;
    }
    if !is_map(slot.output) {
        return false;
    }
    slot.output.passed == true
}

fn proof_tail(slot) {
    if slot == () {
        return "";
    }
    if !is_map(slot.output) {
        return "";
    }
    if slot.output.tail == () {
        return "";
    }
    slot.output.tail
}

phase("Build");
let build_prompt = "Repo: " + repo + ". Read docs/plans/v1-leads.md and implement it fully. "
    + "Git is already initialized. Do not commit. Do not copy sessions or .env files from ~/.claude or anywhere else. "
    + "Stack: Python 3, FastAPI, uvicorn, Telethon, sqlite3, Jinja2, pytest, httpx. "
    + "Create .venv, requirements.txt, .env.example with empty TG_API_ID and TG_API_HASH, README.md in Russian, and scripts/verify.sh that runs .venv/bin/python -m pytest -q. chmod +x scripts/verify.sh. "
    + "Modules and function names must match the plan: app/db.py connect init_db; app/telegram_gateway.py TelegramGateway TelethonGateway FakeGateway; app/accounts.py start_login confirm_login confirm_password list_accounts; app/leads.py create_lead update_lead search_leads add_lead_from_dialog; app/reminders.py create_reminder list_reminders complete_reminder; app/main.py plus app/templates/index.html app/static/app.css app/static/app.js. "
    + "API, SQLite tables, statuses, and the one-page UI colors are specified in the plan. Follow them. "
    + "No mass messaging, account warmup, group member scraping, or sending reminders out to Telegram. "
    + "Tests must use FakeGateway and cover lead search, status change, overdue reminder, login states, and adding a dialog lead once. "
    + "Run ./scripts/verify.sh and fix failures before you finish. "
    + "Reply with the files you changed and the last lines of verify.sh.";
let built = agent(build_prompt, #{
    label: "build",
    phase: "Build",
    capability_mode: "all",
    agent_type: "implementer",
});
if built == () || built.success != true {
    pause("verification", "The build agent failed. Resume after the repo is readable.");
}
log("build finished");

phase("Compare");
let lenses = [
    "Сверь каждый пункт docs/plans/v1-leads.md с кодом. Ищи пропущенные маршруты, чужие имена функций, неверную схему и дыры в UI.",
    "Ищи ошибки логики: дубли лидов, просрочка напоминания, потеря phone_code_hash, поиск без учёта регистра, статус pending_password.",
    "Ищи выход за v1: рассылка, парсинг участников, прогрев, отправка сообщений, секреты или session-файлы в дереве git, код и пароль в логах.",
    "Запусти ./scripts/verify.sh. Если скрипта нет или он красный, это находка. Отдельно открой index.html и сверь блоки: аккаунт, поиск CRM/Telegram, таблица лидов, напоминания, цвета #f4f1ea #1c1915 #0f6e56.",
];
let compare_jobs = [];
for lens in lenses {
    let prompt = "Repo: " + repo + ". Read the code with read_file and grep before you answer. "
        + "Do not edit files. An empty findings list is valid only after you have read the plan and the relevant code. "
        + "Each finding needs file, issue, and evidence (a path plus what you saw). At most 8 findings.\n\n"
        + lens;
    compare_jobs.push(#{
        prompt: prompt,
        label: "compare",
        phase: "Compare",
        capability_mode: "execute",
        agent_type: "reviewer",
        output_schema: findings_schema,
    });
}
let compared = parallel(compare_jobs);

let findings = [];
for slot in compared {
    for item in findings_of(slot) {
        if findings.len() < 8 {
            if is_map(item) && item.file != () && item.issue != () && item.evidence != () {
                findings.push(item);
            }
        }
    }
}
log("compare findings kept: " + findings.len().to_string());

let confirmed = [];
if findings.len() > 0 {
    phase("Verify");
    let skeptic_jobs = [];
    for item in findings {
        let prompt = "Repo: " + repo + ". Refute this finding if you can. Read the file with read_file. "
            + "Set real=true only when the issue is still present and your evidence quotes what you read. "
            + "If the code already matches the plan, set real=false. Do not edit files.\n\n"
            + "file: " + item.file + "\nissue: " + item.issue + "\nevidence: " + item.evidence;
        skeptic_jobs.push(#{
            prompt: prompt,
            label: "skeptic",
            phase: "Verify",
            capability_mode: "read-only",
            agent_type: "reviewer",
            output_schema: verdict_schema,
        });
    }
    let verdicts = parallel(skeptic_jobs);
    let index = 0;
    for verdict in verdicts {
        if verdict != () && verdict.success == true && is_map(verdict.output) {
            if verdict.output.real == true && verdict.output.evidence != () && verdict.output.evidence != "" {
                confirmed.push(findings[index]);
            }
        }
        index += 1;
    }
    log("confirmed: " + confirmed.len().to_string() + " of " + findings.len().to_string());
} else {
    log("no findings to verify");
}

if confirmed.len() > 0 {
    phase("Fix");
    let fix_prompt = "Repo: " + repo + ". Fix only the confirmed gaps below. Read docs/plans/v1-leads.md first. "
        + "Do not add outreach, warmup, or group scraping. Do not commit. "
        + "Keep the module and function names from the plan. Run ./scripts/verify.sh and fix test failures you caused.\n\n"
        + json_encode(confirmed);
    agent(fix_prompt, #{
        label: "fix",
        phase: "Fix",
        capability_mode: "all",
        agent_type: "implementer",
    });
} else {
    log("nothing confirmed to fix");
}

phase("Prove");
let prove_prompt = "Repo: " + repo + ". Run ./scripts/verify.sh yourself with the shell. "
    + "Set passed=true only if that command exits 0. "
    + "tail is the last 40 lines of output. If the script is missing, passed=false and say so. Do not edit files unless verify.sh is absent, in which case do not invent a pass.";
let proof = agent(prove_prompt, #{
    label: "prove",
    phase: "Prove",
    capability_mode: "execute",
    agent_type: "reviewer",
    output_schema: proof_schema,
});

if !proof_passed(proof) {
    phase("Fix");
    let again = "Repo: " + repo + ". scripts/verify.sh did not pass. Read the tail, fix the cause, run ./scripts/verify.sh until it exits 0. "
        + "Stay inside docs/plans/v1-leads.md. Do not commit.\n\n"
        + proof_tail(proof);
    agent(again, #{
        label: "fix-tests",
        phase: "Fix",
        capability_mode: "all",
        agent_type: "implementer",
    });
    proof = agent(prove_prompt, #{
        label: "prove-again",
        phase: "Prove",
        capability_mode: "execute",
        agent_type: "reviewer",
        output_schema: proof_schema,
    });
}

let summary = "verify.sh did not pass";
if proof_passed(proof) {
    summary = "verify.sh passed";
}
log(summary);
complete(#{
    summary: summary,
    confirmed: confirmed.len(),
    tail: proof_tail(proof),
});
