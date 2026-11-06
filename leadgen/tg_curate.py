#!/usr/bin/env python3
"""Curate enriched TG leads into CreatorOS ЦА categories with project research
+ a personalized outreach hook per lead.

Override map is hand-authored from bios + known projects. Leads not in the map
keep their auto-classified values.
"""
import argparse
import json
import re
import sys

# key: username.lower() or "id:<id>"
# value: (category, project, project_url, what_they_do_ru)
OVERRIDES = {
    "bill_htxpoloniex": ("CEX / биржа", "HTX / Poloniex", "https://htx.com",
        "Крупные биржи HTX (ex-Huobi) и Poloniex. Менеджер по листингу активов."),
    "dujunx": ("CEX / биржа", "Huobi / ChainUP / Vernal", "https://www.huobi.com",
        "Сооснователь Huobi и ChainUP (инфраструктура бирж). Сейчас AI-проект Vernal."),
    "pfbbf": ("CEX / биржа", "Omega Exchange", "",
        "CEO & Co-Founder Omega Digital Assets и Omega Exchange."),
    "jamesharden_fmcpay": ("CEX / биржа", "FMCPAY", "https://fmcpay.com",
        "Биржа FMCPAY. Листинги, маркетинг и партнёрки."),

    "cryptoczr": ("Проп трейдинг", "FWA Trade Club", "",
        "Закрытый крипто-торговый клуб №1 в Азии/Китае (invite-only)."),
    "nomadfury": ("Проп трейдинг", "Lever.io", "https://nomadfury.com",
        "BD Lever.io + AMA-хост и комьюнити-основатель. Торговля/левередж — уточнить продукт."),

    "deficaptainblaq": ("Медиа / контент", "DeFi-эдукатор", "",
        "DeFi-тютор/инфлюенсер, обучает DeFi. Нужен трафик на личный бренд."),
    "miketamago": ("Медиа / контент", "Brgy Tamago (YouTube)", "https://www.youtube.com/c/MikeTamago-",
        "Web3-билдер и контент-криэйтор с YouTube-каналом."),
    "bmolodec": ("Медиа / контент", "AI-quant trading + TG-канал", "https://t.me/zaBor_main",
        "AI-квант трейдер, строит торговые системы, ведёт крипто-канал. Есть продукт — нужен трафик."),
    "pemyla": ("Медиа / контент", "Binance Live / AQCommunityNews", "",
        "Binance Live хост, билдер AQCommunityNews. Крипто-медиа с эфирами — есть что резать."),
    "blockblanc": ("Медиа / контент", "CryptoCanal / CypherpunkWeek", "https://cryptocanal.org",
        "Основатель CryptoCanal, конференции CypherpunkWeek и хакатоны. Много контента и ивентов."),
    "pasechnikovada": ("Медиа / контент", "Beyond / Obsidian / BALIFM", "",
        "CEO консьерж-сервиса Beyond, family office Obsidian, адвайзор медиа BALIFM."),

    "galacticsurfeur": ("DeFi / DEX", "Dtreon", "",
        "Децентрализованная подписочная платформа Dtreon."),
    "hrushibytes": ("DeFi / DEX", "XO Market", "",
        "BD & Growth XO Market (prediction market). Ранее AITECH, Router Protocol."),
    "ly3333": ("DeFi / DEX", "OpenEden", "https://openeden.com",
        "Founder/CEO OpenEden — токенизация RWA (tokenized US Treasuries)."),
    "markfeldman13": ("DeFi / DEX", "Flex DEX / TON Labs", "",
        "CEO Standard Capital, сооснователь TON Labs и Flex DEX (DEX на TON)."),
    "robert_zaremba": ("DeFi / DEX", "Native (BTCFi)", "https://gonative.cc",
        "Сооснователь Native — Bitcoin DeFi (BTCFi)."),
    "merifdez": ("DeFi / DEX", "GoodDollar", "https://gooddollar.org",
        "Growth в GoodDollar — DeFi/UBI протокол."),
    "cryptocapitalist": ("DeFi / DEX", "Stakely.io", "https://stakely.io",
        "Сооснователь Stakely (staking-провайдер) и владелец Staked.vc. В крипте с 2013."),
    "matut_eth": ("DeFi / DEX", "Piggy Wallet", "",
        "Co-Founder & CPO Piggy Wallet — крипто-кошелёк."),
    "berlusconi0x": ("DeFi / DEX", "Berachain / Smilee Finance", "https://www.berachain.com",
        "Product Lead Berachain (L1). Ранее Co-Founder/CEO Smilee Finance (DeFi-опционы, acquired)."),
    "noahwhalescoin": ("Токен / мемкоин", "Whales Coin", "https://whalescoin.io",
        "CMO Whales Coin — токен-проект, growth и комьюнити."),

    "jintoken999": ("Gaming / AI", "AI compute token", "",
        "Сервис AI-вычислительных токенов для зарубежного рынка."),
    "airlinezgame": ("Gaming / AI", "Airlinez", "https://airlinez.io",
        "Основатель Airlinez (Лондон) — игра/геймификация."),
    "arulprak": ("Gaming / AI", "BukProtocol", "",
        "Founder BukProtocol — Travel+AI, Web3 AI-агенты."),
    "hola_ikalifa": ("Gaming / AI", "CodeXchain", "https://codexchain.xyz",
        "No-code AI-экосистема для массовой адаптации Web3. Ex-Tencent."),
    "eric_arkslabs": ("Gaming / AI", "Cygnus", "https://x.com/CygnusFi",
        "Co-Founder Cygnus — Web3 Instagram App Layer, онбординг массового юзера."),

    "manish_chetri": ("Платёжки / финтех", "Onramp.Money", "https://onramp.money",
        "Growth-менеджер Onramp.Money и echo.money — фиатный онрамп."),
    "agrimn": ("Платёжки / финтех", "Fintech Stable", "https://fintechstable.com",
        "Founder Fintech Stable (стейблкоин-финтех), партнёр Giakaa Capital."),
    "tomasdm": ("Платёжки / финтех", "ChatterPay", "",
        "Строит ChatterPay — платёжное приложение."),
    "bankstr": ("Платёжки / финтех", "SETL / SQFT", "",
        "Web3-банкир (SETL, SQFT)."),
    "sidgandhi_xyz": ("Платёжки / финтех", "Payy", "https://payy.link",
        "CEO Payy (NYC) — крипто-карта/необанк."),
    "fmontesj": ("Платёжки / финтех", "Kravata", "",
        "Co-founder Kravata — крипто on/off-ramp (LatAm)."),

    "ritikgmst": ("Инфраструктура / dev", "MST Blockchain", "",
        "Head of BD MST Blockchain."),
    "theo_dale": ("Инфраструктура / dev", "contract.dev", "https://contract.dev",
        "Founder contract.dev — инструменты для контрактов."),
    "no_autopilot": ("Инфраструктура / dev", "thirdweb", "https://thirdweb.com",
        "BD в thirdweb — web3 dev-платформа."),
    "gindagramm": ("Инфраструктура / dev", "NODERS.team", "https://noders.team",
        "Founder & CEO NODERS.team — нодовая инфраструктура."),
    "sergeevdms": ("Инфраструктура / dev", "DevLab.group", "https://devlab.group",
        "Blockchain-разработка, токенизация, DeFi, Rollups, ZK."),
    "alex12alex": ("Инфраструктура / dev", "dexStudios", "",
        "Founder dexStudios — dev-студия."),

    "vishaljaiswalofficial": ("Сервис / агентство", "Listing & Partnership", "",
        "Агентство листинга и партнёрств (SMM). Партнёр, не ЦА."),
    "theatharvshrivastava": ("Сервис / агентство", "BackersStage", "",
        "Founder BackersStage — платформа бэкеров/фандрейз."),
    "oxjac": ("Сервис / агентство", "BackersStage", "",
        "Partnership/Growth в BackersStage."),
    "masinvic": ("Сервис / агентство", "Always Wonder Video Agency", "",
        "Founder видео-агентства (Сингапур). Потенциальный партнёр, не ЦА."),
    "dhirendrarathore": ("Сервис / агентство", "Ventoralabs.io", "https://ventoralabs.io",
        "Founder-инкубатор/адвайзор, помогает масштабироваться с KOL."),
    "biyakiml": ("Сервис / агентство", "Olantra (HR)", "",
        "Рекрутер AI/Web3 (Olantra)."),
    "haleylabs": ("Сервис / агентство", "Crypto License compliance", "",
        "Крипто-лицензии VASP/CASP/MSB, комплаенс. Юридический сервис."),
    "pranshu_trj": ("Сервис / агентство", "TheReliableJobs", "",
        "BD & Community Lead TheReliableJobs."),
    "paul_list": ("Сервис / агентство", "ListingWise.me", "https://listingwise.me",
        "CEO ListingWise — помощь проектам с листингом на биржи. Партнёр."),
    "nick_cware": ("Сервис / агентство", "Cware Labs", "https://cwarelabs.com",
        "Founder web3-венчур студии, CMO."),
    "eobukhova": ("Сервис / агентство", "supermoongroup", "https://supermoongroup.com",
        "Founder supermoongroup — уточнить профиль."),
    "an_tutova": ("Сервис / агентство", "PR/marketing", "",
        "PR и маркетинг для tech/blockchain/необанков."),
    "kristynakitsenko": ("Сервис / агентство", "Web3 growth marketing", "",
        "Web3 growth через маркетинг."),
    "jeremybragin": ("Сервис / агентство", "Web3 Partnerships", "",
        "Web3-партнёрства и growth."),
    "web3adriann": ("Сервис / агентство", "Web3 Partnerships/MMs", "",
        "Web3-партнёрства, маркет-мейкинг, growth."),
    "michaelnans": ("Сервис / агентство", "Marketing", "",
        "Маркетинг, бренд-стратегия, growth."),
    "etty_algarisi": ("Сервис / агентство", "B2B Marketing", "",
        "Head of Marketing, B2B growth в крипте и кибербезопасности."),
    "patrik_trt": ("Сервис / агентство", "The Round Table", "https://www.trtcommunity.solutions",
        "Co-Founder The Round Table — digital community solutions."),
    "cxhelle_trt": ("Сервис / агентство", "The Round Table", "https://www.trtcommunity.solutions",
        "CFO The Round Table — digital community solutions."),
    "sparks_merch": ("Сервис / агентство", "Sparks Merch", "https://sparks-merch.com",
        "Крипто-мерч и сувениры (e-com)."),
    "tamjid0x01": ("Сервис / агентство", "Smart Contract Auditor", "",
        "Аудитор смарт-контрактов, web3 security."),
    "clementbarbier": ("Сервис / агентство", "Web3 security", "",
        "Web3-безопасность."),
    "huangsun": ("Сервис / агентство", "DeFiHackLabs / XREX", "",
        "CISO биржи XREX, Founder DeFiHackLabs (security)."),
    "hitsahuja_bdm": ("Сервис / агентство", "finfarmhq", "",
        "BDM, помогает web3-фаундерам (finfarmhq)."),

    # --- second pass additions ---
    "amaan029": ("Проп трейдинг", "PumpRobin.fun", "",
        "CEO PumpRobin.fun, ранее CEO bynomo/apt_casino, 2 exits. Трейдинг/казино-опыт — уточнить."),
    "cmonvivek": ("Gaming / AI", "Luvonlabs", "https://luvonlabs.com",
        "Founder & CEO Luvonlabs, ранее Fluxor.io."),
    "vit_i_ia": ("Gaming / AI", "RUMORS (web3 dating)", "",
        "Co-Founder венчур студии DIGITOPIA и web3 dating app RUMORS. CMO, адвайзор Web3 CMO Network."),
    "sharepriced": ("Токен / мемкоин", "Shiba Infinity", "",
        "CEO Shiba Infinity — мемкоин."),
    "thecryptoquesthub1": ("Gaming / AI", "TheCryptoQuestHub / Questworkpro", "https://questworkpro.com",
        "Founder & CEO TheCryptoQuesthub и Questworkpro — квесты/геймификация."),
    "ikhushalseth": ("Gaming / AI", "SpedaxAI", "https://spedaxai.com",
        "Founder & CEO SpedaxAI."),
    "jaymarweb3": ("Gaming / AI", "Communily AI / Pyto AI", "",
        "Founder Communily AI и Pyto AI."),
    "shee_0020": ("Gaming / AI", "desh.group", "https://desh.group",
        "Owner desh.group — Web3 / AI / iGaming."),
    "mukund0x": ("DeFi / DEX", "GhostUSD / Cryptocal", "https://ghostusd.xyz",
        "Founder GhostUSD (стейблкоин) и Cryptocal."),
    "rtaigaa": ("Gaming / AI", "Amua", "https://amua.ai",
        "CMO Amua — AI-проект."),
    "ehsanzigchain": ("Инфраструктура / dev", "ZIGChain", "",
        "Ecosystem Growth ZIGChain ($ZIG) — L1 блокчейн."),
    "raicevic": ("DeFi / DEX", "Streamflow", "",
        "Growth Lead Streamflow — токен-вестинг/дистрибуция на Solana."),
    "yury_blockchain": ("DeFi / DEX", "DZap", "https://www.dzap.io",
        "Web3 BizDev, ликвидность для DeFi-протоколов через DZap."),
    "jakubrowecki": ("Платёжки / финтех", "Radom Pay", "https://radom.com",
        "BD Radom — крипто-платежи и фининфраструктура."),
    "dblama": ("Инфраструктура / dev", "FlamaTech", "https://flamatech.io",
        "Bot-разработчик, FlamaTech."),
    "ocandocrypto": ("DeFi / DEX", "Uniswap Labs", "https://uniswap.org",
        "Dev Rel в Uniswap Labs — крупнейшая DEX."),
    "akashk_97": ("Медиа / контент", "Cryptopolitan", "https://www.cryptopolitan.com",
        "BD Manager Cryptopolitan — топ web3-новостной сайт."),
    "ian_crypto_ru": ("Медиа / контент", "Crypto.ru", "https://crypto.ru",
        "Crypto.ru — блокчейн-новости."),
    "rahim_coinsource": ("Сервис / агентство", "CoinSource", "",
        "Web3 growth, листинги на биржи, комьюнити."),
    "raulcg24": ("Медиа / контент", "CoinGape", "https://coingape.com",
        "PR & Web3 Marketing, запуск токенов, медиа-видимость (CoinGape)."),
    "harryhorsfall": ("Сервис / агентство", "Flight3 / Zebu Live", "",
        "Маркетинг-агентство Flight3 + ивент Zebu Live."),
    "futurumgo": ("Медиа / контент", "FUTURUM", "",
        "PR/контент FUTURUM — ивенты."),
    "joseph0201": ("Медиа / контент", "Event+Branding+PR", "",
        "Ивенты, брендинг, PR."),
    "mrworkhk": ("Медиа / контент", "Crypto MC Asia", "",
        "Крипто-ведущий/MC в Азии."),
    "narvaezchris": ("Сервис / агентство", "Rayo Capital / Stand With Crypto", "",
        "Founder Rayo Capital, президент Stand With Crypto NY."),
    "dolan_alesia": ("Сервис / агентство", "Alesia", "",
        "CEO Alesia — web3-ивент и продакшн агентство."),
    "tourou": ("Сервис / агентство", "Crypto marketing Japan", "",
        "CEO крипто-маркетинг команды в Японии."),
    "alvarez_crypto_pro": ("Сервис / агентство", "Marketing agency", "",
        "Владелец маркетинг-агентства."),
    "julia_icoda": ("Сервис / агентство", "ICODA", "https://icoda.io",
        "BDM ICODA — крипто-маркетинг агентство."),
    "zack_moore": ("Сервис / агентство", "inspira-labs", "https://inspira-labs.com",
        "Маркетинг в inspira-labs — акселератор/венчур студия."),
    "collablynetworkcoo": ("Сервис / агентство", "Collably Network", "",
        "COO Collably Network — web3-хаб стартапы+VC+лаунчпады."),
    "richardfounder": ("Сервис / агентство", "UniverseKOLs", "",
        "Founder UniverseKOLs — KOL-промо, CoinMarketCap комьюнити 6M+."),
    "tomketal": ("Сервис / агентство", "Kenomic / CryptoDays", "",
        "Kenomic Marketing & Partnerships, Founder CryptoDays."),
    "gobomojo": ("Платёжки / финтех", "litas.io", "https://litas.io",
        "CEO litas.io и daddywonderland."),
    "paypri": ("Платёжки / финтех", "PayPri", "",
        "Покупка пептидов за крипту."),
    "angusxbt": ("Платёжки / финтех", "Crypto Fight Night", "",
        "Crypto Fight Night — спортивная биржа/ивенты."),
    "smithcapitalhq": ("Сервис / агентство", "Smith Capital", "",
        "Инвестиции и ресёрч в крипте."),
    "u0048": ("Проп трейдинг", "crypto quant firm", "",
        "Крипто-квант фирма."),
    "opschefsalim": ("Сервис / агентство", "Institutional BD", "",
        "Institutional BD / AI / Web3."),
}

# username aliases with tricky case
ALIASES = {"hitsahuja_bdm": "hiteshahuja_bdm"}

HOOKS = {
    "CEX / биржа": "You're already paying KOLs and affiliates for signups - we get the same registrations from organic short-form at a fraction of the cost, production on our own software. We run quality signals into a TG channel and overflow to the exchange. Saw {proj}. Want the numbers on one campaign?",
    "Проп трейдинг": "Prop firms live and die by challenge sales. We run clip networks that send qualified traders straight to {proj}'s checkout - organic, not paid. Want me to show one funnel?",
    "Медиа / контент": "You have a deep content archive but none of it reaches short-form. We cut your episodes/AMAs into a daily clip stream across 10 accounts. Want me to cut one episode of {proj} as a demo?",
    "DeFi / DEX": "Your spaces/AMAs never make it to short-form, and TVL needs new users. We cut them into daily clips across 10 accounts pointing to {proj}. Want a demo cut?",
    "Токен / мемкоин": "Memecoin hype windows last about two days, and most teams can't produce content fast enough to catch them. We run clip production in-house and manage the account network, so a wave goes out the same day. Saw {proj}. Interested to see how it works?",
    "Gaming / AI": "Your users live in short video, but you can't produce at that speed by hand. We run AI UGC volume for launches like {proj}. Want examples from your niche?",
    "SaaS / AI app": "If your user growth has hit a ceiling, it's usually not the product - it's the cost of reach. We built an organic distribution system: in-house short-video production + a network of accounts. 183.5M views in 90 days on our last project, zero live shoots. Saw {proj}. Want to see how it's set up for a product like yours?",
    "Платёжки / финтех": "Organic short-form is the lowest CAC channel for consumer fintech right now. We produce 1000+ clips a month and post across a 10-account network, traffic through your referral link. Saw {proj}. Want a quick look?",
    "Инфраструктура / dev": "Short-form is where developers and projects discover tools now. We produce volume clips and post across a 10-account network. Saw {proj} - want examples from your niche?",
    "Сервис / агентство": "We run clip production and a 10-account posting network - could be a clean add-on for your clients at {proj}. Open to a quick partner chat?",
    "Низкий приоритет": "Short-form is the cheapest way to put {proj} in front of buyers right now. We produce 1000+ clips a month across a 10-account network. Want examples?",
}

CAT_PRIORITY = ["Проп трейдинг", "CEX / биржа", "Медиа / контент", "DeFi / DEX",
                 "Токен / мемкоин", "Gaming / AI", "SaaS / AI app", "Платёжки / финтех",
                 "Инфраструктура / dev", "Сервис / агентство", "Низкий приоритет"]


def key_of(r):
    u = (r.get("username") or "").lower()
    if u:
        return ALIASES.get(u, u)
    return f"id:{r.get('id')}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    rows = [json.loads(l) for l in open(args.input) if l.strip()]
    out = []
    for r in rows:
        k = key_of(r)
        ov = OVERRIDES.get(k)
        if ov:
            cat, proj, url, what = ov
        else:
            # keep enrich-classified category; only fall back to low-priority if truly generic
            cat = r.get("category") or "Низкий приоритет"
            if cat in ("Web3 (общее)", ""):
                cat = "Низкий приоритет"
            proj = r.get("project") or ""
            url = r.get("project_url") or ""
            what = r.get("what_they_do") or ""
        proj = proj or (r.get("project") or "")
        url = url or (r.get("project_url") or "")
        what = what or (r.get("what_they_do") or "")
        hook = HOOKS.get(cat, HOOKS["Низкий приоритет"]).format(proj=(proj or "your project"))
        r2 = dict(r)
        r2.update({"category": cat, "project": proj, "project_url": url,
                   "what_they_do": what, "hook": " ".join(hook.split())})
        out.append(r2)

    role_rank = {"Founder": 0, "Marketing": 1, "Community": 2, "Creator/KOL": 3,
                 "Dev": 4, "Project/Web3": 5, "Trader": 6}
    out.sort(key=lambda r: (CAT_PRIORITY.index(r["category"]) if r["category"] in CAT_PRIORITY else 9,
                            role_rank.get(r.get("role"), 9),
                            not r.get("premium")))

    with open(args.output, "w") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    print("Категории:", dict(Counter(r["category"] for r in out)), file=sys.stderr)
    print(f"Curated {len(out)} leads -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
