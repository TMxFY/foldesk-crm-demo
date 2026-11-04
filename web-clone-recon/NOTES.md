# crmchat.ai · разведка (не клон)

## 源信息
- 原站 URL: https://crmchat.ai/
- 源码仓库: исходников этого продукта нет. `gh api search/repositories?q=crmchat` → 45 репозиториев, ни у одного homepage не crmchat.ai. `q=crmchat.ai` → 2 репозитория, оба не сайт: `asby135/framer-seobot-internal` (лицензия NONE, генератор блог-текстов) и `SergSof/telegram-outreach-automation-architecture` (MIT, чужая архитектура аутрича, не продукт).
- Похожие имена, это не crmchat.ai: `crmeb/CRMchat` лицензия MIT, homepage пустой, описание пустое. Не использовать как исходник этого SaaS.
- Упоминания домена в code search — каталоги и заметки, не исходники маркетингового сайта и не приложения.
- 原作者: не установлен по репозиторию. Сайт — чужой проприетарный SaaS.
- 许可证: у продукта лицензии на код нет (проприетарный). Публичный GitHub-код продукта не найден. MIT у одноимённых чужих репозиториев к этому сайту не относится.
- 致谢要求: не клонируем и не перевыкладываем их код, тексты и логотип.

## 技术栈
- Публичный маркетинг: Framer (HTML `generator` Framer c439c68, `server: Framer/ad3cf8f`, комментарий Published Nov 2, 2026). Сигналы recon: react/next/vue/astro/three/gsap/lenis = false.
- Шрифты по computed style (1440): заголовки Manrope 600, длинный текст Manrope 400, нав и подписи Inter Tight. `document.fonts` в recon отдал только Inter Tight (лимит 40) — это не полный список.
- Один `<canvas>` на главной (backing 2170, css 1280). Контекст 2D/WebGL не снимался.
- Трекеры в HTML (не сохранялись как фикстуры): GTM, gtag, Meta pixel, Yandex Metrika, PostHog, CookieFirst, Leadfeeder.

## 复刻前预判
- 复杂度等级: L2. Многостраничный маркетинговый Framer-сайт, без своего исходника. Не L6 целиком: логин и продукт живут на другом хосте и в эту разведку не входили. Canvas есть, но как основной визуальный движок не доказан.
- 推荐模式: визуальная / свой контент. Не верная 1:1 копия и не зеркало бандла.
- 可高保真的部分: каркас маркетинговых страниц, светлая zinc-поверхность, синий CTA, пара шрифтов, ритм секций. Токены: `RECON/design-dna.json`.
- 需要近似或替代的部分: Framer-рантайм, иллюстрации, кейсы, блог. Свой текст и свои картинки, не их.
- 不克隆的部分: логин и всё на `app.crmchat.ai`; их API и приватные ответы; их тексты; их логотип; трекинг-пиксели; mirror-site всего сайта не делался.
- 主要风险: проприетарный продукт, нет лицензии на переразвёртывание. Одноимённый MIT-репозиторий CRMEB — другой продукт.

## Что снято
- `RECON/original-recon.json`, `RECON/original-summary.md`
- Скриншоты главной: `RECON/screenshots/original-1440.png`, `original-768.png`, `original-390.png`
- Маршруты (max-pages 8, max-depth 1, без поддоменов): `/`, `/pricing`, `/changelog`, `/telegram-outreach`, `/telegram-outreach-setup`, `/case-studies`, два case study. `app.crmchat.ai` в карте нет. `RECON/routes/original-route-map.json`
- Цвета и шрифты: `RECON/design-dna.json`, сырьё `RECON/style-facts.json`
- Playwright поставлен в эту папку (`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`), скрипты скилла находили его через `NODE_PATH`. Браузер — channel chrome, отдельный Chromium не качался.

## Не запускалось
- `mirror-site.mjs` — сознательно.
- `network-capture.mjs` — приватные API не снимались.
- `asset-harvest`, `sourcemap-hunt` — не входили в этот шаг.
- `interaction-probe.mjs` прогнан отдельно, см. абзац ниже.
- Клон, локальный сервер и visual-diff не делались. Оценки «реплика готова» нет.

## Цвета и шрифты (факт, 1440)
- CTA `#127ef1` rgb(18, 126, 241), подпись на кнопке `#ffffff`.
- Текст `#09090b`, вторичный текст `#71717a` и `#52525b`.
- Фон body `#fafafa`, полоса секции `#f4f4f5`, чипы `#ffffff`.
- Инлайн-ссылка `#0099ff` — не путать с заливкой CTA.
- Семантические `#48d87d` / `#ffb66e` / `#ff6969` / `#0d75ba` есть в CSS variables, роли на странице не доказаны.

## interaction-probe
`RECON/interactions/original-interactions.json`, exit 0. 23 действия, `app.crmchat.ai` не открывался. Scroll до середины (y 4080) и низа (y 8159) на той же главной, длина текста не выросла. Hover по Industries, Product, Help Center и Company открыл dialog (0→1); hover по логотипу и переключателю English/Russian DOM не менял; у Pricing и Changelog меню не появилось. Клики ушли на публичные страницы `/pricing`, `/changelog`, `/telegram-outreach`, setup и case studies. Клик Theme switch переключил светлую страницу в тёмную (средний цвет кропа 248,249,250 → 17,18,19). Клик Search открыл dialog и пригасил страницу. Canvas-drag сдвинул scroll на y 1483, отдельной сцены canvas не видно. Не увидел: форм, video, popover-счётчик остался 0, логин приложения не нажимался.
