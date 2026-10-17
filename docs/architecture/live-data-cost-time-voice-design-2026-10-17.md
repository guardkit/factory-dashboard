# Factory dashboard: live data, cost and time, chat and voice (design, 17 October 2026)

Written by Claude on 17 October 2026, after running the dashboard against the factory's live data
and evaluating every page (the evaluation itself is kept privately, because it quotes real build
records). It replaces nothing in the July documents. It changes where data comes from and adds
cost, time, chat and voice. The July design's rules still hold:

- the three questions (how far through, on track, what are the issues);
- a signal with no data is shown as a gap, never as a green zero;
- pages and chat answer from the same query functions;
- the dashboard reads and never writes to the factory.

This document is public. Accounts, plans, prices fetched on a given day and machine addresses
live in a configuration folder outside the repository.

## What the evaluation found, in short

- Only two parts are live and right: the Fleet page and the planning list.
- Four pages still read July sample data: Features, the feature page, the project page and
  Issues.
- The live pages mislabel things:
  - features that finished are shown as "in flight" and "stalled";
  - every delivery says "merge unverified";
  - the weekly report says "0 delivered" while listing deliveries;
  - the delivered date window is ignored.
- No page shows a feature's name.
- Cost and chat were never built, and the login is a placeholder.
- The page wording is internal shorthand.

The root causes are few:

1. **Build state comes from bus events.** The feed missed every event while it was stopped, and
   it mistakes merge decisions (`merge-FEAT-…`) for builds. Only successful builds are copied
   from Forge's record, and only once at start-up.
2. **Delivery was defined as "has a pull-request link or merge commit".** Today's factory merges
   directly, and the build table holds neither.
3. **The pages were written against sample data first**, and four were never switched over.
4. **Forge's build table has no title.**

## Design in one paragraph

Make **Forge's build record the dashboard's single source of truth** for features, builds,
planning, approvals, delivery and time. Mirror it in full every 15 seconds, read-only, and stop
reading build state from the bus. Take **cost** from LiteLLM's own spend log. LiteLLM is told to
store each call's existing `x-feature-id` header as a tag (one setting) and is given nominal
prices from OpenRouter's list. The dashboard reads the spend log through a read-only database
login. Rewrite the four sample pages and the report on these two sources, with feature titles
taken from the request sentence. Add a **chat** that calls the same query functions, using the
factory's model through its own LiteLLM name. Add **voice** as a microphone button on the chat,
plus an audio endpoint the robot can use later. James gets in over the tailnet with his own
login.

## Requirement by requirement

### 1. Runs reliably from live data, restarts on its own, says when it's stale

- **Found:**
  - running again since stage 1, with Forge's live record mounted read-only from its estate
    volume;
  - the feed crashed once on first start because the web container creates the tables;
  - the feed was left stopped by the 1 October rollout.
- **Missing:**
  - schema creation in the feed;
  - full mirroring of Forge;
  - a page-wide stale warning;
  - the dashboard in the factory's start steps.
- **Design:**
  - Both containers apply the schema at start (it is idempotent).
  - The feed mirrors all of Forge's builds, planning runs, planning events, stage records and
    queue items every 15 seconds. It does this in short read transactions and closes the
    database between passes; at a few hundred rows a pass this is light.
  - Bus subscriptions for build, stage and approval state are removed (see "The bus" below).
  - **Stale banner:** if the last successful mirror pass is older than 2 minutes, every page shows
    one line at the top: "Factory data last read at 08:14 — the dashboard cannot reach the
    factory's record". If Forge is stopped, the read-only reader cannot open the database. The
    banner then says so, and pages show the last known data with its time.
  - The containers already use `restart: unless-stopped`, so they come back after a reboot. What
    stopped them in October was a runbook step. So the dashboard is added:
    - to the Dell's factory start script, as one `docker compose -f … up -d` line;
    - to the release runbook's final checks ("the dashboard's banner is clear").

  The runbook files are private and are changed in the private operations repository.

### 2. James can open it from his own laptop or phone, with his own login

- **Found:** the login is `operator`/`operator`, with a built-in default session secret and plain
  HTTP on `127.0.0.1:8090`.
- **Design:**
  - **Access:** one tailnet-only HTTPS entry on the Dell, on its own port so pages stay at `/`:
    `tailscale serve --bg --https=8443 http://127.0.0.1:8090`. Undo:
    `tailscale serve --https=8443 off`. No Funnel. The board keeps binding to loopback only.
  - **Accounts:**
    - Stored in `accounts.yaml` in a configuration folder outside the repository, mounted
      read-only into the board (default `~/.config/factory-dashboard/`).
    - Each entry has a username, a display name and a password hash (scrypt, the same function
      the code already uses).
    - A small command prints a hash for a password typed at the terminal; Rich or this lane runs
      it, James never does.
    - The July seed users (`username == tenant slug`) are removed.
  - **Session secret:** read from a file in the same folder. The board refuses to start without it.
    The cookie is set `Secure`, `HttpOnly`, `SameSite=Lax`, and expires after 14 days.
  - **Login throttling:** after five failed attempts for a username in 15 minutes, further
    attempts wait 15 minutes. This is kept in memory; a restart clears it.
  - **Client tenants:** the July client views stay hidden. They need bus plumbing that does not
    exist, and nobody asked for them now.

### 3. Per feature: tokens in and out, calls, nominal cost

- **Found:** LiteLLM logs every call with tokens and times, but stores no feature identifier and
  has no prices. The dashboard's key is refused by `/spend/logs`.
- **The tag:** LiteLLM 1.95 has a built-in setting that stores chosen request headers as tags:

  ```yaml
  litellm_settings:
    extra_spend_tag_headers: ["x-feature-id"]
  ```

  Each call then carries the tag `x-feature-id: <routing id>` in `LiteLLM_SpendLogs.request_tags`
  and in the daily tag totals. Every factory caller already sends that header. This replaces the
  three options in the research (a hook, a client change, the model door): it needs no code, no
  client change and no seat release. The routing id is Forge's `correlation_id`, which a feature
  keeps from planning through build. So one tag covers the whole feature.
- **Reading it:**
  - A read-only Postgres login for the dashboard in LiteLLM's database, allowed `SELECT` on
    `"LiteLLM_SpendLogs"` only. Its password is kept in the dashboard's private environment file.
  - The feed reads new rows every 60 seconds, by `"startTime"` with a watermark. It stores
    per-call summaries in the dashboard's own store:
    - time;
    - model name;
    - prompt and completion tokens;
    - LiteLLM's `spend`;
    - the feature tag (or none);
    - the key alias.
  - About 2,500 calls a day at today's rate is small. Rows older than 180 days are summed per day
    and per feature, then dropped.
- **Shown per feature:** model calls, tokens in, tokens out, and nominal cost, with the price
  source and date. Machine and waiting time come from requirement 6.
- **The planner/coder/coach split** (Rich, 17 October: "might be interesting data for future
  analysis but not required to be surfaced on the dashboard"):
  - The model name (which shows the role) is stored with each call.
  - No page or chart shows the split.
  - The July table row asked for a split "by stage". This design follows Rich's later answer and
    shows feature and project totals, with stage *time* (requirement 6) but not stage *cost*.
- **History before tagging:**
  - Calls before the tag setting went live have no feature tag. Builds now overlap (up to six at
    once), so splitting by time window would hand one feature's calls to another.
  - Those features show cost as a gap: "not measured — calls before 17 October were not tagged
    by feature".
  - The project and weekly totals still show all calls, tagged or not, as "factory-wide usage".
  - This is simpler and more honest than labelled estimates. It is a deliberate change from the
    handoff's suggestion and is flagged for review.

### 4. Per project: the same totals, by week and in total

- Project means the build's `repo` (for planning, `target_repo`) until Forge fills `project`.
- The project page shows, for each week and in total:
  - features delivered;
  - features in progress;
  - tagged model calls, tokens and nominal cost;
  - elapsed, machine and waiting time.
- Untagged calls cannot be given to a project, so the project page shows only tagged usage and
  says how much factory-wide usage was untagged that week.
- **URL:** project names contain `/`, so the route becomes `/projects/{owner}/{name}` (a path
  parameter), with the name shown first.

### 5. Nominal prices from OpenRouter, dated

- On the day the tag setting is added:
  - fetch `https://openrouter.ai/api/v1/models` (public, no login);
  - save the list with its date in the private evidence folder;
  - take the price for the factory's model. Its local checkpoint is Qwen3.8 Flash Next; on
    17 October OpenRouter lists `qwen/qwen3.8-flash`, the nearest match, which is recorded with
    the reason.
- Set `input_cost_per_token` and `output_cost_per_token` on every LiteLLM model name that points
  at the factory's model, so new calls carry `spend`.
- The dashboard keeps the same prices, with source model and date, in `prices.yaml` (in the
  repository: prices are public). It uses LiteLLM's `spend` where it is set, and re-prices rows
  where it is zero (older or local-only names) from tokens. Every cost figure carries the label
  "nominal — OpenRouter price for qwen/qwen3.8-flash on 17 Oct 2026".
- A "same tokens at a frontier model's price" column is not built now. It can be added later as
  a second price row.

### 6. Time per feature and project: elapsed, machine, waiting

Forge's record already holds every timestamp needed. Checked on a feature built on 16 October:

| Period | From | To | Counts as |
|---|---|---|---|
| In the queue | `work_queue.queued_at` | `work_queue.admitted_at` | waiting (queue) |
| Planning | `planning_runs.started_at` | `planning_runs.completed_at` | machine, minus the approval waits inside it |
| Spec or plan approval | a planning event `GATED` | the next event for that stage answered by a person (`actor_identity` not a factory component) | waiting (approval) |
| Build gate | `stage_log` `autobuild` `GATED` | the `gate-resume` row | waiting (approval) |
| Build | `builds.started_at` | `builds.completed_at` | machine, minus the build gate wait |
| Merge decision | the first `the merge word` `GATED` row for the build | the decision row (`merge_decision.decided_by`, `decision`) | waiting (merge decision) |
| Merge and check | the decision | the final `merge-deploy` `PASSED` row | machine |

- **Elapsed** is the first queue time to delivery, or to now while in progress.
- `stage_log.duration_secs` is not used: Forge records 0 for every build stage.
- Approvals given by an automatic card policy (evaluation runs) show 0 wait.
- **Delivered** means the final `merge-deploy` stage `PASSED`. Its details name the commit that
  was checked and published. This replaces the July "merge unverified" bar, which can never be
  met today. A merge decision of "decline" is shown as "built, not merged".
- **What James can act on:** waiting time is shown first, broken into queue, approvals and merge
  decision.

### 7. The three questions, per feature and project, with today's thresholds

**How far through.** The journey, from Forge's record:

  sent → in queue → planning → spec approved → plan approved → building → built →
  merge decision → merged and checked

**On track.** The July thresholds are re-derived from the last 14 days of builds. The norms move
from the database seed into `norms.yaml` (in the repository; the numbers are not sensitive), each
with its date and how it was derived. Proposed rules, with the numbers fixed during part A from
the data:

| Signal | Green | Amber | Red |
|---|---|---|---|
| Attempts (builds per request) | 1 | 2 | 3 or more |
| Planning time | ≤ the 75th percentile | ≤ the 95th percentile | above it |
| Build time | ≤ the 75th percentile | ≤ the 95th percentile | above it |
| No activity while running (stall) | under 30 min | 30–60 min | over 60 min |
| Waiting for a person | under 1 h | 1–4 h | over 4 h |

- On 16 October, builds took 19–90 minutes from queue to finish. The July 120-minute stall
  limit is too loose for that.
- "Turns per task" and "SDK ceiling hits" have no feed, so they are dropped from the page rather
  than shown as permanent gaps.

**What are the issues**, all from Forge's record:

- failed planning (with Forge's error sentence);
- failed or cancelled builds;
- approvals waiting, with who and how long;
- stalled runs;
- failed merge checks.

Each issue closes when the record moves on. The four sample pages are rewritten on the mirrored
tables:

- **Features:** every feature, newest first, with title, project, stage, on-track colour,
  elapsed and cost.
- **Feature page:** the journey, the time table, cost, issues, and links to the records used.
- **Project page:** the totals above, plus its features.
- **Issues:** open and recently closed.

**Feature titles:**

- The planning run's request sentence, trimmed to its first sentence (at most 90 characters),
  with the full text on the feature page.
- Shown as "Search box that filters notes by title (FEAT-2555)": the title first, the code after.
- A feature without a planning run (queued as already planned) uses its feature file's name if
  Forge records it, otherwise its code.

### 8. The weekly report shows real spend and time; the export stays client-safe

- **Headline:**
  - delivered this week;
  - in progress;
  - tagged nominal cost;
  - total waiting time;
  - factory-wide calls and tokens.
- **Plan vs actual:** `plans.yaml` moves to the private configuration folder. Until a plan is
  written there, the report shows "no plan agreed" (never green). Taking a baseline from the
  factory's own feature plan is a later option: the plan has tasks, but no dates or effort.
- The **client-safe export** keeps its allow-list. It adds per-project spend and elapsed time,
  and never shows model names, keys, internal identifiers or other projects.
- **Spreadsheet download:** a CSV of the week's features, with project, title, status, elapsed,
  waiting, machine time, calls, tokens and nominal cost. The person who writes the client report
  worked from a spreadsheet tracker, so this suits how they work.

### 9. Chat that cites its records

- One text box on every page, with answers in a side panel.
- **Model:**
  - A new LiteLLM name, `dashboard-chat`, pointing at the factory's model, with its own key
    limited to that name. Its own use then shows in the cost figures, tagged `x-feature-id:
    dashboard-chat`.
  - Never the study tutor's model.
  - At most 30 questions an hour across all users, and one at a time; more are refused with a
    plain sentence.
- **Tools:** the same query functions the pages use, read-only:
  - `feature_status(title or code)`;
  - `project_summary(project, week)`;
  - `open_issues(project?)`;
  - `delivered(period, project?)`;
  - `cost_summary(project or feature, period)`;
  - `time_summary(project or feature, period)`;
  - `weekly_report(week, project?)`.

  Each returns plain figures plus a list of record references (feature code, build id, period).
- **Grounding:**
  - The system prompt allows answers only from tool results, in plain English, with the title
    before the code.
  - After the model answers, the server checks that every feature code, number of minutes and
    cost figure in the answer appears in that turn's tool results. If not, the answer is replaced
    with "I can't answer that from the dashboard's records" plus the records it did find.
  - The answer is shown with its references as links to the pages.
- **Limits:** at most four tool calls and 600 output tokens per answer, and a 60-second limit.
  A failure says why in one sentence.
- **Later, not built:** Jarvis (Slack) could call the same tool functions over a small read-only
  HTTP endpoint. That needs a Jarvis change, and it is not in this task.

### 10. Voice

- **Browser:**
  - A microphone button beside the chat box. Hold to talk on a phone, click to start and stop on
    a laptop.
  - Records with `MediaRecorder` and uploads the clip to `POST /api/voice/ask`.
  - Shows the transcript, the answer text and its references, and plays the reply.
  - If speech is unavailable, it falls back to text with one sentence saying so.
  - Needs HTTPS, which requirement 2 provides.
- **Server, one request at a time:**
  - At most 30 seconds of audio.
  - Speech to text through `STT_BASE_URL` / `STT_MODEL` (default `http://127.0.0.1:9000/v1`,
    `parakeet-tdt-0.6b-v3`, the OpenAI-compatible `/audio/transcriptions`).
  - Then the chat (requirement 9).
  - Then speech through `TTS_BASE_URL` / `TTS_MODEL` / `TTS_VOICE` (default the same address,
    `qwen3-tts-0.6b`, `/audio/speech`).
  - The answer comes back as JSON with the transcript, the answer, the references and the audio
    (base64 WAV).
  - On 17 October the speech models moved to the second machine. The Dell's model switcher
    forwards the same names, so the default address still works; it stays a setting.
- **No streaming at first.** Short answers and one round trip are simpler than the tutor's
  WebSocket stream. Streaming can be added if the wait is too long; it is measured in part E.
- **Robot-ready:**
  - The same endpoint accepts `Authorization: Bearer <device token>` in place of a session
    cookie.
  - Device tokens are listed (hashed) in `accounts.yaml`.
  - The endpoint has nothing browser-specific: audio in, JSON with speech out. The later Reachy
    Mini app needs only this.

### 11. James can refine it

- A "Suggest a change" link on every page opens a short form (text, or a voice note
  transcribed by requirement 10). Suggestions are listed on a Suggestions page with who, when
  and status.
- **Storage:** a separate small database, `suggestions.db`, which the board writes. This is the
  only thing the web side writes. The factory's record, LiteLLM and the read model stay
  read-only to it.
- A "What changed" page shows a short dated list kept in the repository (`CHANGES.md`), in plain
  words.
- Rich and the coordinating session read the Suggestions page. Turning suggestions into changes
  stays with people. James's own prototype route can come later.

### 12. Small and safe beside the factory

- **Memory:** about 86 MiB today. The budget is under 300 MiB for board, feed, chat and voice
  together. Audio is handled in memory per request, at most 30 seconds.
- **Model load:**
  - The pages make no model calls.
  - The chat is capped at 30 questions an hour.
  - Speech uses the speech models already loaded, which are no longer on the Dell.
- **Read-only access to the factory:**
  - Forge's record is mounted read-only;
  - LiteLLM through a SELECT-only login on one table;
  - no bus login at all (below).
- **LiteLLM changes** (one tag setting, prices, the chat name and key) are made once, with the
  factory idle, a dated backup, and every factory model name checked afterwards.

## The bus (NATS) and the read-only identity

- **Today:** the feed subscribes to the bus with a broad personal login. In July a read-only
  dashboard identity was planned but never made.
- **This design removes the dashboard's bus connection.** Build, planning, approval and delivery
  state come from Forge's record, which is complete and survives the dashboard being stopped;
  bus events do not.
- What the bus gave besides that:
  - **agent heartbeats** on the Fleet page: these come instead from the bus's monitoring page
    (`:8222/connz`, connection names, no login). If the connection names do not identify the
    agents, the agents list is dropped rather than shown from a guess;
  - **instant updates:** these come from the 15-second mirror, through the existing live-update
    channel.
- So no NATS account change and no factory release is needed. If a later feature needs bus
  events again, it should get a subscribe-only user then.

## Removing what isn't earning its place

- **Hidden:**
  - the client tenant views and client store (no feed, no request);
  - "graduation feed pending";
  - "turns per task" and "SDK ceiling hits";
  - "seam findings";
  - the July "S1 / D0" footer;
  - every internal code in visible text.
- **Changed:** the gate events panel becomes a plain "recent activity" list from Forge's stage
  record, with the title, step in words, result and time.
- **Kept but moved:** the planning list now shows only the last 7 days and open runs, with the
  requester's display name where an approver mapping exists in `accounts.yaml`, otherwise
  "the requester".
- **Plain words on every page:**
  - "Waiting for the merge decision", not `GATED`;
  - "Built, not merged", not `SKIPPED`;
  - "Planning failed: <Forge's sentence>", not `FAILED`.

## Later phase: Reachy Mini asks the factory (notes only, build nothing)

- **The template is Scholar,** the standalone Reachy Mini app built for the study tutor
  (`fleet-gateway/reachy/reachy_mini_scholar_app/`, on GitHub `main`). It:
  - records on touch;
  - uploads the audio to a backend voice endpoint;
  - plays the spoken reply;
  - sways its antennas while waiting, and speaks failure reasons.
- **The factory version:** a sibling app, or a second backend mode of Scholar, pointed at this
  design's `POST /api/voice/ask`. It uses a device token from requirement 10 and holds no
  factory credential.
- **Reactions** (merged feature: celebrate; stalled build: look concerned) could poll a small
  `GET /api/events/recent` that returns the last few plain-English events. They would use the
  robot's existing emotion moves.
- **Before any of this:**
  - Scholar's tutor address needs re-pointing. That is a settings change on the robot, owned by
    the tutor work.
  - Nothing is installed on the robot without Rich's word.

## Build order (stage 3)

| Part | Covers | Depends on |
|---|---|---|
| A | Requirements 1 and 7: Forge mirror in full, schema at start, stale banner, the four pages and the report on real data, titles, delivery, re-derived norms, plain wording, bus removed, start script and runbook | — |
| B | Requirements 3–6: LiteLLM tag setting, prices, chat name (one LiteLLM change), read-only DB login, spend mirror, cost and time on the feature, project and report pages, CSV. Proved on one feature built during the part | A's store layout |
| C | Requirement 2: accounts, secret, secure cookie, throttling, tailnet entry | — (can run beside A's review) |
| D | Requirement 9: chat | A |
| E | Requirement 10: voice | C and D |
| F | Requirement 11: suggestions and "what changed" | C |

## Checks for each part

- The existing test suite, plus new tests for the new queries, run on the Dell one run at a
  time under the shared lock.
- A headless-browser pass over the named pages against live data, with screenshots.
- Two or three figures per page checked against Forge's record or LiteLLM directly.
- For part B, one real feature built during the part. Its tagged calls, tokens, nominal cost,
  machine time and waiting time must match LiteLLM and Forge when checked directly.

## Questions settled by Rich (17 October)

- Access over Tailscale, tailnet-only HTTPS.
- Cost per project and per feature, with the role split kept as data only.
- OpenRouter prices, labelled nominal and dated.
