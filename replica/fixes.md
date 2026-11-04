# Fix plan from public CRMChat reviews

Sample: 4 reviews, 1 source (Product Hunt). All 5★, all praise. One is the founder. One (proxies / team outreach) is only on a Wayback snapshot of the same PH page.

`reviews.py` matched no hate themes. The only request theme is thin: 1 review, 1 source («Teams and sharing»).

## What they hate

Nothing in the sample. No public complaint about connecting Telegram, finding a lead, statuses, reminders, or handing off a note.

## What is missing

Nobody named a missing feature. Do not treat the thin «team» quote as a request.

## What is unsolved

Not enough independent reviews to name a job or group the original ignores.

## Fix plan

Публичных отзывов недостаточно. Не чинить в нашей CRM (подключение Telegram, поиск лидов, статусы, напоминания, передача заметки) ничего «из жалоб»: жалоб нет. В `replica/features.csv` новых строк с `original=no` нет.

## Angle

Нет. Позиционирование «for who hate X» из этого набора не строится.

## Sources checked (no review text copied)

| source | result |
| --- | --- |
| Product Hunt reviews | 3 live 5★ at https://www.producthunt.com/products/crm-chat/reviews ; 4th only at Wayback 2025-08-04 |
| Product Hunt launch comments | congratulations / maker tips, not reviews |
| G2 | listing exists; reviews page blocked («Access is temporarily restricted»); no quotes |
| Capterra | no CRMChat / crmchat.ai product page found |
| Trustpilot | no crmchat.ai review page (bot check) |
| Software Advice | https://www.softwareadvice.com/product/639973-CRMchat/ — «No reviews yet» |
| GetApp | https://www.getapp.com/all-software/a/crmchat/ — 0 reviews |
| Hacker News Algolia | 0 hits for `"crmchat.ai"` and `"CRMChat" telegram` |
| Reddit | founder promo threads; independent comment bodies not readable without login |
| App Store / Play | no official crmchat.ai app (other apps named CRMChat are unrelated vendors) |
| Public Canny / Featurebase | none found |
| crmchat.ai homepage quotes | vendor testimonials, not independent reviews — not used |
