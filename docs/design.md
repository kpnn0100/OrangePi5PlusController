# Arstro design: one look for every screen

Every Arstro screen follows this page: the device manager (`relay/arstro_relay/static/`), the
machine's web UI (`server/arstro_remote/web/static/`), the backup terminal
(`server/arstro_backup/static/`) and the Android apps (`devices_app/`, `client_app/`, `app/`).
Change a value here first, then in each place listed under *Where the tokens live*.

**The idea:** quiet, dark and flat. Warm near-black surfaces, off-white text, thin borders, one
off-white primary button per view, colour only where it means something (online, warning,
error, recording). No gradients, no glow, no heavy shadows, no emoji.

## Tokens

| Token | Value | Use |
|---|---|---|
| `bg` | `#0c0c0b` | page / scaffold |
| `surface` | `#131312` | cards, sheets, the side bar |
| `surface-2` | `#1a1a18` | inputs, hover, pressed rows |
| `surface-3` | `#222220` | selected rows, tooltips, chips |
| `line` | `#242422` | hairline borders, dividers (always 1px) |
| `line-2` | `#32322f` | input borders, stronger dividers, scrollbars |
| `text` | `#ededea` | primary text, icons that matter |
| `text-2` | `#b2b0a9` | secondary text, labels |
| `muted` | `#868379` | meta data, hints, placeholders, idle icons |
| `primary` | `#ededea` | the one primary button, checked controls, focus ring, active nav marker |
| `on-primary` | `#0c0c0b` | text / icons on `primary` |
| `ok` | `#7cc495` | online, success (with `ok-soft` `rgba(124,196,149,.12)`) |
| `warn` | `#ddb764` | warnings (with `warn-soft` `rgba(221,183,100,.12)`) |
| `err` | `#ec7e76` | errors, destructive actions (with `err-soft` `rgba(236,126,118,.12)`) |
| `rec` | `#f0645a` | recording only |
| `info` | `#8fb3e0` | rare: links inside long text, a chart's second series |

Charts and graphs: `text` for the main series, then `info`, `ok`, `warn`; fills at 10-14 % alpha.

## Type

- **Geist** (sans) and **Geist Mono**, bundled with the UI (never fetched from the internet; OFL
  licence next to the files). Web: `fonts/Geist-Variable.woff2`, `fonts/GeistMono-Variable.woff2`
  with `font-display: swap`; Flutter: the static TTFs (Regular, Medium, SemiBold) as assets.
  Fallback stack: `"Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif` and
  `"Geist Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace`.
- Sizes: page title 26-28 px / 600 / letter-spacing -0.025em / line-height 1.15; section title
  15-16 px / 600; body 14-15 px / 400 / line-height 1.55; meta 12.5-13 px in `muted`;
  overline labels 11.5 px / 500 / uppercase / letter-spacing 0.06em in `muted`.
- Mono for ids, ports, keys, versions, addresses, numbers that line up (`tabular-nums`).
- Weights: 400, 500, 600 only.

## Shape and space

- Radius: 10 px cards and sheets, 8 px buttons and inputs, 6 px chips and small badges,
  50 % only for status dots and round icon buttons.
- Borders: 1 px `line` on every card, 1 px `line-2` on inputs; no shadows (one exception:
  floating menus and toasts get `0 8px 24px rgba(0,0,0,.4)`).
- Space on a 4 px grid: 4, 8, 12, 16, 20, 24, 32, 48. Cards pad 16-20 px (16 on phones).
  Content max width 1080 px (lists 880 px). 16 px side gutter on phones; works from 360 px wide.
- Lists are rows separated by hairlines inside one bordered container, not a stack of cards.

## Components

- **Primary button**: `primary` background, `on-primary` text, 8 px radius, 36-40 px tall
  (44 on touch), weight 500; hover a touch darker (`#d9d8d4`), active `scale(.98)`. One per view.
- **Secondary button**: transparent, 1 px `line-2` border, `text`; hover `surface-2`.
- **Ghost / icon button**: no border, `text-2`; hover `surface-2` and `text`.
- **Destructive**: secondary style with `err` text; filled `err` only in a confirm dialog.
- **Input**: `surface-2` background, 1 px `line-2` border, 8 px radius, 38-40 px tall; focus:
  border `text-2` + 3 px ring `rgba(237,237,234,.12)`. Placeholder `muted`.
- **Status dot**: 7-8 px circle; online `ok` with a 3 px `ok-soft` ring, offline `muted`,
  connecting `warn` (slow blink), error `err`.
- **Badge / pill**: 6 px radius (or full round for a status pill), 11.5-12 px, 500, `surface-3`
  background and `text-2` text; semantic badges use the `*-soft` background with the colour.
- **Navigation**: side bar `surface` with a hairline; the active item `surface-3` with `text`,
  others `text-2`; a 2 px `primary` marker on the active item. Phones: bottom tab bar, `surface`.
- **Dialogs and sheets**: `surface`, 1 px `line`, 12 px radius, scrim `rgba(0,0,0,.6)`.
- **Toasts / snack bars**: `surface-3`, 1 px `line-2`, `text`.
- **Icons**: one stroke icon set, 1.75 px stroke (web SVG) or Material *outlined* / *rounded*
  (Flutter), 18-20 px; `text-2` by default.
- **Logo**: the "A" mark in `on-primary` on a `primary` rounded square (no gradient).
- **Motion**: 160-220 ms, `cubic-bezier(.2,.8,.2,1)`, only opacity and transform; respect
  `prefers-reduced-motion`.

## Where the tokens live

| UI | File |
|---|---|
| Device manager (web) | `relay/arstro_relay/static/style.css` (`:root`) |
| Machine web UI | `server/arstro_remote/web/static/app.css` (`:root`) |
| Backup terminal | `server/arstro_backup/static/backup.css` (`:root`) |
| Arstro Devices | `devices_app/lib/src/theme.dart` (`Arstro`: tokens + `ThemeData`) |
| Arstro Remote Client | `client_app/lib/src/theme.dart` (a copy of the same file) |
| Arstro Remote (Pi remote) | `app/lib/src/theme.dart` (a copy of the same file) |
