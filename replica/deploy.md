# Deploy checklist: Foldesk

Date: 2026-11-04  Commit: `54ce0b7` (`54ce0b735ed3757b99c5546949ba3db55a781ca3`)  Go from user: not yet

**Verdict: stop.** Live URL: none. Domain/DNS not set. Nothing shipped. Keys not inserted.

## Preflight (all must pass)

- [x] `./scripts/verify.sh`: exit **0** — 49 passed, 1 warning (Starlette BlockingPortal deprecation), 2.55s. This is pytest, not Playwright.
- [ ] e2e Playwright (`npx playwright test`): **not run this pass**. Skill lists it; this repo’s gate is `verify.sh`. Not claimed green.
- [x] no open S1 or S2 bugs. `replica/bugs.md`: BUG-001/002 S3 fixed; BUG-003 S2 fixed in `d7e6828`. Open items are “to check”, not S1/S2.
- [x] parity: must-haves **18/18**, feature **81.4**. `python3 .claude/skills/replica-diff/parity.py replica/features.csv` exit **0**. Views already in `df7c893`; this HEAD still 81.4. Layout score not computed (no original screen pairs).
- [ ] rebrand sweep clean: `python3 .claude/skills/replica-brand/sweep.py . --config replica/brand.json` exit **1**. **1165 hits** (color 61, domain 285, name 803, path 16). Bulk is `web-clone-recon/` (recon notes, JSON, screenshot filenames with crmchat.ai). Also `tests/test_landing.py:28-30` (asserts that avoid-names are absent). Sweep not clean → deploy stop. `app/` product templates were not in the hit list; that does not make exit 0.
- [x] store listing: `python3 .claude/skills/replica-launch/listing.py replica/launch/listing.json` exit **0** — 0 errors, 0 warnings. App Store / Play length checks ok. Stores not shipping this pass.
- [ ] production build: **no `npm run build`**. No root `package.json`. Python app; not claimed as a pass.
- [ ] privacy policy and terms live, every processor listed: **no**. No `/privacy`, `/terms`, or legal templates. Landing (`app/templates/landing.html`) has no policy/terms links. Cookie banner: none (and not claimed needed or present).
- [ ] account deletion works: **no**. No delete-account route, UI, or copy in `app/`.
- [ ] favicon, titles, OG image, emails, app icon are yours: **not ready**. Titles are “Foldesk”. Favicon is empty `data:,` in `app/templates/landing.html:8` and `index.html:7`. No `og:image` / `og:title`. No 1200×630 OG asset, no real favicon set, no app icon. `replica/brand.md` still lists those as to-deliver. Emails: sending domain not verified (step 2 not started).

## Commands this pass

```
./scripts/verify.sh
# exit 0  (49 passed, 1 warning)

python3 .claude/skills/replica-diff/parity.py replica/features.csv
# exit 0  Parity: 81.4 / 100  features 81.4  (38 counted, must-haves 18 of 18 done)

python3 .claude/skills/replica-brand/sweep.py . --config replica/brand.json
# exit 1  1165 hits … Replace every one before you launch.

python3 .claude/skills/replica-launch/listing.py replica/launch/listing.json
# exit 0  0 errors, 0 warnings

npm run build
# not applicable: no root package.json, Python project
```

## Production

- [ ] separate production database, backups on, migrations in the deploy — not started
- [ ] env vars set (names match .env.example) — not started; live keys not pasted
- [ ] Stripe live: products, prices, webhook endpoint + secret, one real purchase refunded — not started (landing says billing is off)
- [ ] OAuth redirect URIs and origins include the production domain — no production domain
- [ ] Google OAuth verification approved (if using sensitive scopes) — n/a this pass
- [ ] email sending domain verified — not started

## Domain

- [ ] A @ -> not set
- [ ] CNAME www -> not set
- [ ] SPF, DKIM, DMARC set — not set
- [ ] canonical host chosen, the other redirects — no
- [ ] HTTPS valid — no live host

## Watch

- [ ] error tracking
- [ ] uptime checks
- [ ] analytics
- [ ] core flow done on the live site, desktop and phone

## Why stop

Sweep exit 1 (`web-clone-recon/` plus landing-test avoid-name strings). Legal pages, account deletion, favicon and OG are not present. Production build command does not exist. User go: not yet. Steps 2–5 not started.
