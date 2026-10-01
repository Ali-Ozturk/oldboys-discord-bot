# Esportligaen CS2 match scraping findings

This document describes the HTML structures found in the supplied captures and a resilient approach for turning them into bot data. The captures are pages from `https://esportligaen.dk/kampe/<match-id>/` (Danish locale). These are rendered HTML pages, not a documented API. Treat selectors and status strings as site-specific and re-check them against fresh captures when the site changes.

## Source captures

| File | Observed top-level match classes | Observed state |
|---|---|---|
| `data/not-started-match.txt` | `.match.upcoming` | Scheduled match before veto/check-in |
| `data/ban-phase-no-bans.txt` | `.match.veto` | Map veto / ban-pick voting; no ban has yet been selected in this capture |
| `data/start-0-rounds.txt` | `.match.ongoing.csgo` | Series has started; first map Nuke is ongoing at round 0 and 0–0 |
| `data/map1-started-error.txt` | `.match.ongoing.csgo` | Map 1 is live at round 0; map 2 is queued. The queued status mentions the previous map finishing, which is not a completion status for map 2 |
| `data/ongoing-match-1-map-finished-2ndplaying.txt` | `.match.ongoing.csgo` | Map 1 is completed with a score card while map 2 is live |
| `data/finished-match-bo2.txt` | `.match.finished` | Completed BO2 series, with two map results and detailed scoreboards |

All four have a `div.site_layout.single_match` page shell. Most navigation/footer content is unrelated; scope selectors to the unique `.match` element to avoid accidentally scraping similar names elsewhere on the page. The finished capture has a substantially different, newer result-page layout from the live capture.

## Recommended state model and detection

Fetch the match URL and parse the response body as HTML. First confirm that the response contains a match page and the expected match ID; a login page, 404, proxy error, or changed template should be an `unknown/error` result, not a match state.

Use the wrapper's classes as the primary state signal, checking specifically for tokens (not substring matches):

1. `.match.finished` → `finished`
2. `.match.veto` → `veto`
3. `.match.ongoing` → `live` (the capture also has `.csgo`)
4. `.match.upcoming` → `upcoming`
5. Otherwise → `unknown` until another page state is observed and explicitly supported.

For veto, `.intro.match_header[attr-phase="voting"]` and `.status` text `Voting` are corroborating signals. For live play, `.intro.match_header[attr-phase="ongoing"]` and the per-map status badge `Igangværende` corroborate the wrapper. For finished, `.match-hero__status.is-finished` displays `Færdig`.

For upcoming matches, `.intro.match_header[attr-phase="upcoming"]` corroborates the `.match.upcoming` wrapper. The start time appears in `.roster_info[attr-start]` as Unix seconds (`1791144000` in the sample), and also in `.time-datepicker-match-start[value]` and `.match_header .status` as `DD-MM-YYYY HH:mm` (`04-10-2026 20:00`). In this capture, the timestamp's UTC clock value is `20:00`, while the page's visible match time is `20:00` Copenhagen time; interpreting the epoch normally shifts the result by two hours. Prefer the visible/input value, parse it in the configured `Europe/Copenhagen` timezone, and normalize to UTC internally. Use `attr-start` only as a wall-clock fallback (reapply the configured local timezone to its UTC clock digits), not as an absolute instant. The match format and game are labeled fields under `.match_settings .field`: `.label` / `.value` (sample: Type = Best of 2, Spil = CS2).

Do not classify from page title or date text alone. Shared navigation contains unrelated text, and labels may be localized. A page that has no recognized `.match` state should remain unknown rather than being treated as upcoming.

## Upcoming / not-started match

- Root state: `.match.upcoming`; header phase: `.intro.match_header[attr-phase="upcoming"]`.
- Teams: `.match_header .team-1-name` and `.team-2-name`; links identify teams.
- Scheduled time: prefer `input.time-datepicker-match-start[value]` or `.match_header .status`, formatted `DD-MM-YYYY HH:mm` in the capture; parse in the configured local timezone and normalize to UTC internally. `.roster_info[attr-start]` is available as a fallback, but this capture's value behaves like a local wall time represented as UTC and must not be used as an absolute UTC timestamp.
- Format and game: `.match_settings .field` entries, identified by `.label` and `.value`; sample is `Type` → `Best of 2`, `Spil` → `CS2`.
- Match readiness: `.readyness .field` shows team check-in state, and `.status_text` reports overall readiness (`Venter på, at begge hold tjekker ind` in the sample). `Strict mode` and tournament information are optional context, not match state.
- The match ID still comes from the page's canonical breadcrumb/share URL path. The `attr-start` timestamp is the time suitable for `/matches` ordering and the 30-minute reminder.

Suggested payload: `state=upcoming`, match ID/URL, team names/URLs, UTC start time, format/game, and optional readiness text.

## Common match identity and fetch considerations

- The canonical match link appears in `.match_breadcrumbs li.is-current a` and the share button's `data-share-url`. Extract either href/data attribute as a canonical URL. The identifier is the final path segment (e.g. `01M3AC8R58QA4WZFJAKDRHF1D3`). Validate it before using it as an identity key.
- Team names in veto/live appear under `.match_header .team-1-name` and `.team-2-name`; use their anchor text and href. In finished pages, use `.match-hero__team.is-team1/.is-team2 .match-hero__name`, with the team links for stable team identity. Display names may use different dash characters or accents across markup; preserve the source string for display and normalize only for comparisons.
- Tournament/competition context can be taken from breadcrumb links (`.match_breadcrumbs`) or `.match-hero__trail`; these are useful metadata, not match state.
- `data/ban-phase-no-bans.txt` begins with WordPress hidden inputs (`heartbeat-nonce`, `_wp_http_referer`). They are page plumbing and should not be used for match identity or state. Do not log nonce values.
- The sample HTML is UTF-8 and contains Danish text, accented names, and emoji-like glyphs. Decode response bytes using the declared charset (normally UTF-8), preserve Unicode, and normalize whitespace when extracting text.
- Network behavior: set timeouts, handle non-2xx responses, retry transient failures with backoff, and avoid polling too aggressively. Persist last successful state/data and distinguish fetch/parse failures from real changes. No polling interval or rate policy can be derived from these static samples.

## Veto / banning phase

### Signals

- Root state: `.match.veto`; header phase: `.intro.match_header[attr-phase="voting"]`; status text `Voting`.
- Teams: `.match_header .team-1-name` and `.team-2-name`.
- Veto step order: `.veto_info .order > span`. The supplied capture has seven stages: Ban, Ban, Ban, Ban, Vælg, Vælg, Ban. The first has `.active.v-0`; classes/active position may change as voting proceeds.
- Voting metadata is on `.veto_info`: `attr-lsvote`, `attr-tscurrent`, and `attr-visualvotetime`. These look like Unix timestamps/duration inputs used by the page timer, but the sample includes a disclaimer that the displayed timer is only an indicator. Avoid treating it as authoritative match start time or deadline.
- Team turn marker: `.veto_upper .team_wrapper.team1/team2`; a `banning` class marks the active team in this sample. `.turn-info` says `Vent venligst, til det er din tur` (wait until your turn). Team logos and names are presentation data.
- Available/remaining maps: `.map_voting_match .map`, with map identifier in `attr-map` (e.g. `de_dust2`) and a display name in `.map-inner` (e.g. `dust2`).

### Extracting current veto state

For each veto step in `.veto_info .order`, preserve its 0-based sequence and translate labels (`Ban` → ban, `Vælg` → pick). Correlate with the visible maps and current step only when their structure supports it. A `.map` may carry classes such as `v-` or selected/removed state classes as voting progresses; the sample is an untouched initial state and does not establish all resulting class meanings. Do not infer which team selected a map by list order alone. As the vote progresses, inspect further captures and validate against page classes/text before implementing that correlation.

The initial page's `.map_voting_match[attr-v]` has `attr-v="47679"`, likely an internal vote identifier; it is not the match ID. The `attr-map` values are the useful map keys. The sample shows the seven-map pool: Dust2, Inferno, Ancient, Cache, Nuke, Mirage, Anubis. Do not hardcode that pool; future maps or tournament rules can change it.

### Suggested veto payload

`state`, `match_id`, `team1`, `team2`, `phase_label`, `steps[]` (index, action, active flag), `active_team` (nullable), `maps[]` (map key, display name, observed status), and optional timer metadata explicitly marked non-authoritative.

## Live / ongoing match

### Series and map structure

- Root state: `.match.ongoing.csgo`; header phase attribute `ongoing`.
- Format label: `.estag_wrapper .estag.danger.type` (sample: `CS2 - Bedst af 2` = CS2, Best of 2).
- Each map is a `.single_match_map` under `.matches__ongoing__list`, with series-map identifier `attr-id` and type `attr-type="series_match"`.
- Map name: `.single_match_map .map_title` text; standardized map key in its `attr-map` (sample `de_nuke`).
- Map/server state: `.ongoing_server_status .estag`. Sample first map is `Igangværende`; second map is `Dette map kan startes, når det forrige er afsluttet` (not yet playable until preceding map completes). Treat each map independently: a series can have a live map and a queued next map at once.
- A completed map while the series is still ongoing has `.single_match_map.finished`; its `.ongoing_server_status` contains `.score_finished_tag` with `.score_team1` and `.score_team2`. Read these score fields in team order. Do not classify by substring matching `afsluttet`: the queued-map message says the next map can start when the previous one is finished, but that does not mean the queued map itself has finished. The class and score card are the completion signal. Do not announce a completed map until both scores are present; some databases may retain an earlier false completion with no scores.

### Score extraction

For a map with `.single_match_map_body`, read `.rounds` (sample `Runde 0`) and `.fighters .team1/.team2`. Each side has `.name` and `.score`. The order of `.team1` and `.team2` corresponds to the page's series team order. Parse scores/round counts as integers after stripping text; retain the raw values if parsing fails. In the sample, Nuke is at 0 rounds and 0–0. The next Cache map has no body yet, so return its name/status with `score = null`, not 0–0.

A zero score is legitimate for an active map at round 0. Missing score markup means unavailable/not started, not zero. Re-fetching and comparing structured map scores and states is the natural basis for live update notifications.

### Access limitations and unavailable details

The sample shows a `.matchroom-login-note`: server and GOTV addresses are shown only to signed-in members. Do not assume these are available in anonymous HTML. The live capture contains only current round/map scores; it does not show player stats, live round log, or team sides. The finished page's round overview is historical and must not be assumed to exist during live play.

### Suggested live payload

`state`, series identity/team names, `format`, and `maps[]` (series-map ID, map key/name, status text, nullable rounds played, nullable team scores). Also retain fetch timestamp so Discord messages can state when the page was observed.

## Finished match / result summary

### Header and series result

- Root: `.match.finished`.
- Team 1 and team 2: `.match-hero__team.is-team1/.is-team2 .match-hero__name`; tags are `.match-hero__tag`.
- Winner/loser: wrapper team classes `.is-winner` / `.is-loser`, and a crown icon; do not infer winner from team 1 ordering.
- Series map score: `.match-hero__score .match-hero__num` values in team order. Sample is 2–0. `.match-hero__score-note` says `2 ud af 2 maps spillet` (2 of 2 maps played).
- Status `.match-hero__status` (sample `Færdig`).
- Match date/time: `time[datetime]` in ISO format with offset is preferable; visible Danish date is presentation only. Capture sample datetime is `2026-09-30T21:15:00+00:00`.
- Format: `.match-hero__meta` includes `BO2`; game label `.match-hero__game` is `CS2`.

### Per-map results

Each `.map-results__card` in `.map-results` represents one played map, in series order:

- Map display name: `.map-results__map`.
- Score: two values in `.map-results__score` separated by `.map-results__dash`, in team 1/team 2 order (sample Nuke 13–4, Inferno 13–10).
- Winner: `.map-results__winner .map-results__team`; card class `is-won` indicates a winner exists. Map banner art may identify map via style URL but visible text is preferred.
- `.map-results--2` reflects the sample's two map cards; do not assume BO2 for all matches.

### Veto timeline

`.veto-timeline__step` is ordered chronology. `.is-ban` / `.is-pick` identify action, `.veto-timeline__kind` contains the visible localized action, `.veto-timeline__map` is map name, `.veto-timeline__team-name` identifies the acting team. A `.veto-timeline__side` may provide starting side via `.is-ct` / `.is-t`, title, and team tag. This sample's two picked maps show that Copenhagen Malmø Port starts CT. Bans have no side data. Use this timeline when reporting veto history; unlike the in-progress veto capture, it has completed, attributable actions.

### MVP

`.matchroom-mvp` may provide a match MVP. Player: `.matchroom-mvp__name` and profile href. Team: `.matchroom-mvp__team`. Stats are repeated `.matchroom-mvp__stat` entries with `<dt>` label and `<dd>` value. The sample includes ESR-rating, kills, deaths, K/D, ADR, HS%, multikills, clutches. Parse by label rather than fixed DOM order. Decimal commas are used in several values (`1,62`, `108,4`), and `%` suffixes occur; store normalized numeric values alongside raw display strings where needed. MVP is optional and should be null if section absent.

### Scoreboard and player statistics

`.matchroom-scoreboard` has tabs: one all-maps panel and one panel for each played map. Tab buttons `.matchroom-scoreboard__tab` link their `aria-controls` to panel IDs. Parse all panels from the HTML, not only the tab with `aria-selected=true`; inactive tabs are still present in the capture.

Each `.matchroom-scoreboard__panel` includes a metadata bar with map (`.matchroom-scoreboard__meta-map`), time (`.matchroom-scoreboard__meta-time`), and map score (`.matchroom-scoreboard__meta-score`, where present), then two `.matchroom-scoreboard__team` blocks. Team blocks may carry `.is-winner`; team label is `.matchroom-scoreboard__team-label`. Each has a `.matchroom-scoreboard__table`.

Table headers define player stat columns and each player row is a `.matchroom-scoreboard__player`. Prefer reading table `<thead>`/`<th>` labels and aligning cells per row rather than hardcoding positional indices. Stable column classes include `col-player`, `col-esr`, `col-kd`, `col-adr`, `col-kast`, `col-kills`, `col-deaths`, `col-assists`, `col-hs`, `col-fk`, `col-fk_ct`, `col-fk_t`, `col-tk`, `col-5k`, `col-4k`, `col-3k`, `col-2k`, `col-clutch`, `col-mvps`, `col-ud`, `col-ef`, `col-fa`, `col-bp`, and `col-bd`. Header `<abbr title>` explains the stat (e.g. KAST, first kills, utility damage). Team and player identity anchors are useful where provided. All-maps panel aggregates played maps; per-map panel values are scoped to that map. Do not sum or mix them when composing a map-specific announcement.

### Round overview

`.round-overview` contains per-map round-by-round results; this is optional enrichment for finished matches. The `<select data-round-overview-select>` options identify map panels with `value` IDs, and each `.round-overview__panel` contains a table. Rows correspond to the two teams in team order; row team identity is available via logo `alt`/`title`. Round cells `.round-overview__cell` use `.is-win` and side class `.is-ct` or `.is-t`; screen-reader text gives a localized description such as `Runde 1: vundet (CT)`. Half-time and final score are in `.round-overview__half` and `.round-overview__final`. Match-point markers use `.round-overview__mark.is-mp`. This dataset can be large and need not be scraped for a basic result announcement.

### Suggested finished payload

`state`, match ID/URL, team identity/names/tags, winner, series score, format/game, scheduled or played datetime, maps played (`map`, team scores, winner, optional duration), veto timeline, optional MVP, optional all-map/per-map player stat tables, optional round summaries. Keep `round_overview` and full stats optional so the bot can choose a compact Discord message.

## Change detection and Discord notification design

HTML is a snapshot, so compare normalized structured data over successive successful fetches. Suggested event keys:

- `match_id + state`: state transitions such as veto → live → finished.
- `match_id + map_id/map_key + rounds + scores`: live score changes; emit only when values change.
- `match_id + map + result`: newly completed map result.
- `match_id + finished + series_score`: final result announcement, once only.

Persist the last emitted event key (or a compact snapshot) to prevent repeated notifications on every polling cycle and support process restarts. A transition from live to finished can make the same final score visible repeatedly; deduplicate on match ID and final series result. If HTML is unchanged, do not announce again. If a page fetch or parse fails, do not convert missing values to zeros or emit a state-change notification.

## Robust parsing rules

- Use a real HTML parser (e.g. BeautifulSoup/lxml in Python or a DOM parser in the bot runtime), not regex over the full document. HTML is long and markup is nested.
- Select the `.match` root and parse relative to it; classes are token sets, and class order can change.
- Use semantic attributes (`datetime`, `attr-map`, `attr-id`, `aria-controls`, team URLs) and table headers where available. `attr-*` are nonstandard HTML attributes but are present in the source.
- Extract `get_text(" ", strip=True)`-style text, collapse whitespace, and keep source language strings. Map known Danish status/action labels to internal enums while retaining raw label.
- Convert numbers defensively: integers for round counts/scores; locale-aware decimals for stats; percentages may retain `%`. Keep null for absent/unparseable fields.
- Scope all queries to the intended card/panel/team. Avoid selecting the first `.score` globally or relying on visual order outside a known container.
- Treat absent sections as optional, not parse failures, unless required state fields are absent. Validate invariants: exactly two teams for ordinary matches, scores parse nonnegative, result map count agrees with displayed series metadata where available.
- Store raw HTML snapshots during development (with sensitive tokens removed) and add fixture-based parser checks as the bot implementation is developed. These samples do not cover every transition, overtime, abandoned/postponed matches, forfeits, missing stats, or future markup.

## Important coverage gaps

The captures do not show a veto after one or more bans/picks, a live match after round 0, a live map after half-time, overtime, a completed match with a draw/forfeit, a BO1/BO3, or the HTML listing from which to discover all team matches. Those states need additional real captures before assuming the same selectors or semantics. In particular, the completed BO2 provides the richest statistics but cannot establish what updates are available while a match is live.
