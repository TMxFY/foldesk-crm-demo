# Компоненты кабинета

Экраны: S04 входящие, S05 переписка, S06 карточка у чата, S07 воронка, S09 карточка лида, панель дайджеста вместо бота. Лендинг не описывается.

Размеры с пометкой measured сняты с публичного сайта (style-facts, скрины 1440 и 390). Размеры inferred выведены из сетки 4 и высоты кнопки 40. Пикселей кабинета нет: `app` не открывался, `replica/screens/` нет.

Иконки — свой набор Lucide (MIT), 16 или 24, stroke 1.5. Чужие глифы не обводить. Подписи свои.

Фокус везде: 2px кольцо `accent`, сдвиг 2px цветом `bg`, чтобы кольцо не сливалось с заливкой. Клавиатура: Tab по порядку, Enter/Space на кнопке, Escape закрывает модалку и возвращает фокус.

## Button

  variants  primary, secondary, ghost, danger
  sizes     icon 24px measured (Theme switch), sm 32px inferred, md 40px measured, lg 48px inferred (герой был 50, не копируем)
  states    default, hover, active, focus-visible, disabled, loading
  tokens    primary: bg accent, text on-accent, hover accent-hover, radius md, font sm/600
            secondary: bg fill-muted, text text, hover fill-muted-hover, radius sm (вкладка была 8)
            ghost: bg transparent, text text, hover sunken
            danger: bg danger, text on-danger
  a11y      настоящий button, type=button. Loading не убирает подпись (aria-busy, спиннер декоративный). Disabled не в tab order смысла не теряет: остаётся в дереве с disabled.
  used on   S04, S05, S06, S07, S09, дайджест

## TextField

  variants  text, textarea, single-select, amount
  sizes     высота md 40 inferred вровень с кнопкой; textarea min 96 inferred
  states    empty, filled, focus-visible, invalid, disabled
  tokens    bg surface, text text, border 1px border-input, radius md, подпись xs text-muted, ошибка sm danger
  a11y      label связан через for/id. Ошибка — aria-invalid и aria-describedby, не только цвет. Плейсхолдер не заменяет подпись.
  used on   S09 свойства, напоминание, поиск по форме
  measure   рамка поля на главной не снималась; толщина и контраст 3:1 заданы правилом AA, цвет border-input = измеренный #71717a

## Search

  variants  диалоги (S04), лиды (тот же контрол)
  sizes     высота 40 inferred, на всю колонку списка
  states    empty, typing, no results, focus-visible
  tokens    как TextField; пустой ответ — sm text-muted, не отдельный цвет
  a11y      role=search, подпись «Поиск» видимая или aria-label. Стрелки не обязательны: это фильтр списка, не combobox.
  used on   S04
  copy      плейсхолдер «Имя или @», не их маркетинговый текст

## DialogRow

  variants  личка, группа, без звука
  sizes     min-height 64 inferred (аватар 40 + 12×2). Аватар 48 measured только у маркетинговых дисков, в списке не используем
  states    default, unread, selected, loading (скелет), empty-список
  tokens    bg surface, selected/hover sunken, имя sm/600 text, превью sm/400 text-muted, бейдж счётчика xs on-accent/accent, radius md у строки
  a11y      button внутри ul/li. Непрочитанное не только точкой: aria-label «не прочитано, N». Выбранная строка aria-current=true.
  used on   S04
  copy      пустой список: «Пока нет разговоров»

## FilterChip

  variants  ждёт ответа, непрочитанные, группы, без звука
  sizes     высота 32 inferred, padding 8×12, radius pill
  states    off, on, focus-visible
  tokens    off: bg surface, text text, border border; on: bg accent, text on-accent
  a11y      button aria-pressed. Группа — role=group с именем «Фильтр».
  used on   S04 (в срезе m3, в системе уже есть)

## KanbanColumn

  variants  стадия (свой набор имён, не подписи с их сайта)
  sizes     колонка 280 inferred (7×40), зазор 16, карточка padding 12, radius lg
  states    empty, filled, drop-target, filter applied
  tokens    колонка bg sunken, карточка bg surface, shadow card, заголовок sm/600, счётчик xs text-muted
  a11y      перенос с клавиатуры не обещать, пока нет ручки: карточка — ссылка на S09. Пустая колонка — текст «Пусто», не картинка.
  used on   S07

## LeadCard

  variants  карточка в колонке
  sizes     ширина колонки минус 16, min-height не фиксировать
  states    default, hover, focus-visible, dragging (opacity, inferred)
  tokens    имя sm/600 text, мета xs text-muted, статус — StatusBadge
  a11y      ссылка с именем человека. Статус в тексте ссылки не дублировать, если бейдж внутри.
  used on   S07

## LeadPanel

  variants  нет лида (S06), заполнена (S06 и S09)
  sizes     360 inferred; на ширине <768 колонка на весь экран, шаг inferred от кадра 390
  states    empty, filled, saving, invalid field
  tokens    bg surface, разделители border, заголовок lg text, секции space 24
  a11y      регион с aria-labelledby по имени. Кнопка «В лиды», если карточки нет. «Открыть чат» ведёт в S05.
  used on   S06, S09
  copy      пусто: «К этому чату лид ещё не заведён»

## StatusBadge

  variants  стадии сделки; здоровье сессии: на связи, нет сети, нужен вход, блок, заморожен
  sizes     высота 24 inferred, font xs/600, radius pill, padding 4×8
  states    static (стадия не кнопка), кнопка только если бейдж меняет стадию
  tokens    новый: text + sunken; переписка: accent on surface + border accent
            оффер: warning on warning-soft; согласование: text on fill-muted
            сделка: success on success-soft
            блок и заморожен: danger on danger-soft; нет сети: text-muted on sunken; нужен вход: warning on warning-soft; на связи: success on success-soft
  a11y      текст внутри, не цвет один. Если кнопка — aria-haspopup=listbox.
  used on   S06, S07, S09, шапка сессии

## Timeline

  variants  заметка, задача, система
  sizes     строка с точкой 8 и отступом 16 inferred
  states    empty, filled
  tokens    текст sm text, время xs text-muted, карточка заметки surface + border
  a11y      ol. Время — time datetime. Пусто: «Заметок пока нет».
  used on   S09

## Reminder

  variants  форма (заголовок, дата, время, комментарий) и строка в панели
  sizes     поля 40; строка min 72 inferred
  states    empty form, invalid, due, overdue, done, postponed
  tokens    due: border warning; overdue: text danger; done: text-muted и text-decoration на заголовке не использовать (плохо читается) — бейдж «Сделано» success-soft
            кнопки «Сделано» secondary, «Отложить» ghost, «К лиду» primary sm
  a11y      дата и время — отдельные input type=date и type=time с подписями. Кнопки строки имеют имя вместе с заголовком напоминания.
  used on   S09, панель дайджеста

## DigestPanel

  variants  сегодня, просрочено
  sizes     панель 360 inferred, как LeadPanel; на узком экране под списком, не бот
  states    empty, filled, loading
  tokens    секция sunken, заголовок sm/600, группа «Просрочено» danger xs, «Сегодня» text
  a11y      region «Напоминания». Две группы — h3. Пусто: «На сегодня ничего не стоит».
  used on   панель дайджеста (вместо S17/S18)

## Modal

  variants  заметка, напоминание, подтверждение удаления
  sizes     max-width 480 inferred (12×40), padding 24, radius lg, shadow pop
  states    open, validation, submitting
  tokens    bg surface, overlay rgba(9,9,11,.48) — декоративный, в контраст текста не входит
  a11y      role=dialog, aria-modal, фокус в первое поле, Tab заперт, Escape закрывает. Подвал: primary слева по-русски не путать — отмена ghost, подтверждение primary.
  used on   S09 заметка и напоминание

## Toast

  variants  успех, ошибка, повтор
  sizes     max-width 360 inferred, padding 12, radius md
  states    enter, visible, dismiss
  tokens    bg surface, border border, текст sm; маркер 4px success или danger
  a11y      role=status для успеха, role=alert для ошибки. Не единственный носитель: поле уже показывает invalid. Источник состояния в разведке — guess.
  used on   сохранение лида, сбой отправки S05
