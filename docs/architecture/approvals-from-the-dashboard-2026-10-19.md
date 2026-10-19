# Answering the factory's cards from the dashboard (design, 19 October 2026)

## What Rich asked for

On 19 October 2026, Rich wrote: "It would be really useful to be able to see the approve and reject buttons in the
dashboard on the merge card — from an architectural standpoint I think the dashboard was intended to be read-only but
the convienence factor would be worth changing this viewpoint I think?"

He settled the scope the same day:
- "1) i think all human touch points please". Every card a person answers gets buttons on the dashboard.
- "2) yes James can approve/decline".

## The idea in one paragraph

The dashboard becomes a third front door, next to Slack and the Linear door, as ADR-DASH-007 in the July
architecture (`factory-dashboard-system-arch-2026-07-08.md`) already foresaw. When someone presses a button, the
dashboard sends the same `ApprovalResponsePayload` that Jarvis sends, on the card's own response subject. Forge then
decides exactly as it does now: it checks the identity, enforces "first answer wins", and refuses stale answers. The
dashboard still never writes Forge's record, LiteLLM's spend log or the read model. What it shows is always what Forge
recorded, never what it sent.

## Which cards, and what the buttons mean

All of these cards are already visible in the dashboard's read-only copy of Forge's record (see "What is open"). The
button labels are plain words, and each label maps to the decision Forge already accepts.

| Card | Buttons, and what each sends | Notes |
|---|---|---|
| **Spec review** (the planning digest card, `plan-{cid}`, stage `feature-spec-digest-review`) | **Approve the spec**: `approve`, every assumption accepted, as the Linear door does. **Ask for changes** (needs a note): `reject` with notes `"Please change: " + the person's text`. Forge reads a note whose first word is `reject` as "cancel" (`driver.py` `_reject_word_split`, ~926-947 and ~6207-6224), so the fixed prefix makes the person's text always mean "revise from this note", even if they typed "reject…". **Stop this request** (optional reason): `reject` with notes `"reject"`, or `"reject: " + reason`, which cancels | The card's full text is in the record (`planning_run_events.details_json.digest_review`), so the dashboard shows it beside the buttons. A test sends "reject this and that" through Ask for changes and checks the notes start "Please change: " |
| **Sign-in question** (`plan-{cid}`, stage `qa-pass-bars-auth-confirm`) | **No sign-in needed**: `approve`. **It needs sign-in**: `reject` | Approve and reject mean the opposite of a plan card here, so the labels say what each answer means and never just "approve" or "reject" |
| **Build gate** (`{build_id}`, stage `autobuild`) | **Start the build**: `approve`. **Cancel** (optional reason): `reject` with the reason | Forge's record shows when the gate closes, but not who answered it (gaps, below) |
| **Mission plan** (`mission-{id}`) | **Start the mission**: `approve`. **Don't start it**: `reject` | The plan is shown from the record: the mission's title, and its features in order with each feature's sentence and what it needs. That needs the `mission_features` and `mission_needs` tables in the mirror (see "What is open"). The buttons are shown only when the plan is complete in the copy: the mission row plus at least one feature row. Otherwise the page says "Answer this one in Slack" |
| **Merge card** (`merge-{feature_id}`, request id `merge-{build_id}`; ordinary builds, fix journeys and a mission's merge) | **Merge and deploy**: `approve`. **Decline**: `reject`. **Rebuild the preview**: `defer` with notes `rebuild-preview` | Shown beside the card's preview link, report link and summary from the saved offer (`stage_log` `merge_deploy_offer`). Rebuild is not an answer: Forge writes no decision row for it, and the card stays open (`merge_executor.py` ~7418-7438). It has its own rule (see "Rebuilding the preview") |
| **Older assumptions checkpoint** (`plan-{cid}`, `planning_runs.state = PAUSED`) | No buttons. The page says "Answer this one in Slack" | Its assumption list exists only on the bus, so the dashboard can't show what it would be answering. This card has mostly been replaced by the spec review |

The budget-breach card is switched off in Forge, and its options are not valid decisions, so it's left out.

## Who may answer, and as whom

- **Who sees the buttons:** operator accounts only. A client account never sees a button, and every answer route
  refuses it (403).
- **The account file.** Each operator account in `accounts.yaml` (outside the repository) gains one setting,
  `approver_identity`. That value is sent as `decided_by`, the identity Forge checks.
  - **Rich:** his existing Slack member id, the same mapping the Linear door uses. His answers therefore pass Forge's
    current `expected_approver` check with no Forge change.
  - **James:** `dashboard:james`, added once to `approval.additional_approvers` in the live `forge.yaml`. That setting
    is a release step that goes through ai-transition-03.
- **No identity, no buttons.** An account without `approver_identity` sees the cards but no buttons.
- **Forge still decides.** If Forge refuses an identity, the dashboard says so plainly, because the card stays open in
  Forge's record.
- **Which door answered.** Nothing in the record says which door an answer came through. Where Forge records the
  answerer, the page shows who answered, using that `decided_by`. It doesn't claim to know which door it was.
- **What each card can show once it has closed.** This depends on what Forge records, so the page never says more
  than the record does:

  | Card | Shown when closed |
  |---|---|
  | Spec review, sign-in question | who and which answer: the verdict row in `planning_run_events`, with `actor_identity`, decision and notes |
  | Merge card | who and which answer: the `merge_deploy_decision` row with `decided_by` and the decision |
  | Mission plan | approved: who and when (`missions.approved_by` and `approved_at`). Rejected: only "not started", with Forge's closing reason. **Forge doesn't record who rejected a mission** |
  | Build gate | only that the gate closed and how the build carried on: running, cancelled or deferred. **Forge doesn't record who answered** |

  A card that closed without a person's answer, such as a timeout, a cancellation elsewhere or a stopped mission,
  shows what Forge recorded for it and never "answered".

## How an answer travels

1. The person presses a button. This is a POST under Part C's guards: signed in, same origin, operator account, and the
   size check before any reading.
2. In one snapshot of the board's own copy of the record, the board checks that the card is still open. If it is
   already answered, it says who answered it and when, and sends nothing.
3. The board writes a claim: card request id, decision and person, in its own small store next to `suggestions.db`.
   The claim is unique per request id. It is a **pending submission**, not an answer. A second press, or a second
   person, finds it and is told "An answer from the dashboard is waiting for the factory to record it (sent by … at …)",
   never "already answered". This mirrors the Linear door, which claims first and never sends twice.
4. The board publishes the answer:
   - **Envelope:** a `MessageEnvelope` with `event_type = approval_response`, `source_id = "dashboard"`, and
     `correlation_id` set to the card's own correlation id. A merge answer is refused without the right one.
   - **Subject:** `agents.approval.forge.<token>.response`.
   - **Method:** a plain core-NATS publish and flush, not a JetStream publish.
   - **Credential:** a separate bus user, `dashboard_approve`, whose permissions allow publishing on
     `agents.approval.forge.*.response` and nothing else. It can't subscribe to anything except its own replies.
   - **Where the code lives:** only in the answer module. The projector never holds this credential.
5. The page shows "Sent. Waiting for the factory to record it". Its existing freshness poll then re-reads Forge's
   record:
   - when Forge records the decision, the card shows the recorded answer: who, which decision, when;
   - if the card is still open two successful, fresh feed passes later, the page says "The factory hasn't recorded this answer. It may
     have refused it", and the claim is cleared so the person can try again. This is the Linear door's "couldn't
     confirm" rule. Forge logs a refusal, for example an identity it doesn't accept, but records nothing about it in
     the ledger (`approval_subscriber.py` ~735-748). So the page never invents a reason. It only says the card is
     still open;
   - for a build gate and a rejected mission plan, "recorded" means the card closed in the way the answer asks for:
     the build left PAUSED, or the mission left PROPOSED. Who answered isn't shown, because Forge doesn't record it.

### Rebuilding the preview

"Rebuild the preview" asks Forge to rebuild the merge card's preview. It doesn't answer the card.
- **No claim.** It writes no claim against the card's request id, so the Merge and Decline buttons stay available
  throughout.
- **One rebuild at a time.** The board keeps its own short "rebuild requested" mark per offer, holding the offer's
  current rebuild count, so a second press while one is waiting is ignored.
- **What the page shows.** Forge reports a rebuild in stages, so the page shows the state the record proves:
  - **"Rebuild requested":** the board has sent it, and the record shows nothing new yet.
  - **"Waiting for room to rebuild":** the offer carries `rebuild_wanted_at`. Forge is waiting for capacity
    (`merge_offer.py` ~3295-3313).
  - **"Rebuilt":** the offer's review evidence and `preview_rebuilds` count have changed (`merge_offer.py`
    ~3233-3284). The new preview link appears.
  - **"Can't confirm the rebuild yet":** a long time has passed with no change, three successful fresh feed passes or
    more. A slow rebuild, preparing the preview and the merge check, can take minutes, so the page never says Forge
    hasn't started. After that the mark is cleared, so the person may ask again. Forge's own record of a rebuild
    already in preparation decides whether that starts a second one.
- **Rebuilding never closes the card.**
- **Tests:** a slow rebuild, where the count changes only after several passes; capacity waiting, where only
  `rebuild_wanted_at` appears; and an amended offer.

## What is open (all from the read-only copy)

The mirror also gains two whole tables, copied the same way as the others: `mission_features` (mission id, key,
sentence, no-sign-in flag, in Forge's order) and `mission_needs` (mission id, key, needs key). That way the mission
plan comes from the same record Forge built the card from (`missions.py` ~556-578; `missions_flow.py` ~436-444).


The Linear door's `ForgeRecord.cards()` queries are the template for this. The copy already mirrors `stage_log` and
`planning_run_events` with their details. Part G adds these columns to the mirror's named lists:
- `builds.pending_approval_request_id`;
- `planning_runs.pending_approval_request_id` and `parent_request_id`;
- `missions.plan_request_id`, `originating_user`, `approved_by` and `approved_at`.

A missing column reads as empty, as before.

| Card | Open when |
|---|---|
| Spec review, sign-in question | the latest `planning_run_events` row for the stage is GATED or reopened. Its details carry the request id, attempt, expected approver and the card |
| Build gate | `builds.status = PAUSED` with `pending_approval_request_id` set |
| Mission plan | `missions.status = PROPOSED` with `plan_request_id` set |
| Merge card | the latest `merge_deploy_offer` row is GATED, and there is no `merge_deploy_decision` row with that request id |

A build gate "defer" creates a new request id, so the dashboard re-reads after any change and never reuses a request
id it has already shown.

## Where the buttons appear

- **"Needs you"** on the Now page: each open card shows its age and its buttons.
- **The page the card belongs to:** the feature page (spec, sign-in, build gate, merge) or the mission page (plan,
  merge). There it sits beside the card's own evidence: the spec text, the preview and report links, and the plan.
- **Phones:** the buttons are full-width and sit on the same pages.
- **Markup:** semantic, using the redesign's existing button and card classes. The redesign session (ai-transition-30)
  styles them.
- **No extra confirmation step.** The card's summary sits beside the button, and Forge's own rules stop a second
  answer.

## What changes where

- **factory-dashboard, the main change:**
  - an answers module holding the only NATS client and publish call in the app;
  - the claim store;
  - the open-card read model;
  - the routes;
  - the templates;
  - `approver_identity` in the account parser;
  - the bus password read from a file, by name.

  The fence tests in `tests/test_honesty_and_fences.py` are rewritten rather than removed:
  - no NATS import, publish call or JetStream use anywhere except the answers module;
  - the answers module publishes only on `agents.approval.forge.*.response`;
  - the projector never imports it.
- **nats-infrastructure** (`config/accounts/accounts.conf.template`): a `dashboard_approve` user in the APPMILLA
  account, allowed to publish only on `agents.approval.forge.*.response` and to subscribe only to `_INBOX.>`.
- **Forge's estate compose:** the new password file, alongside the bus's other account passwords.
- **fleet-secrets:** the password, by name only, never printed. The dashboard reads it from a file.
- **Live `forge.yaml`:** `approval.additional_approvers` gains `dashboard:james`. That's a release step.
- **Forge and Jarvis code:** no change.

## Safety

- **No new door into Forge.** The answer goes through Forge's existing checks. Those checks differ by card, so this
  design doesn't claim more than each one gives:

  | Card | What Forge checks |
  |---|---|
  | Merge card | identity; the offer's correlation id, refused when absent or different (`merge_executor.py` ~7402-7408); the saved request id; a durable decision row, so the first answer wins even across restarts |
  | Spec review, sign-in question | identity; the card must still be paused; an answer for a different request id is ignored as stale; the verdict row stops a second answer |
  | Mission plan | identity, against the originating user or the approver list; the first answer that changes the mission's status wins |
  | Build gate | identity; correlation id only when both sides carry one; a duplicate request id within 300 s in memory. **An approve, override or defer is not checked against the gate's current request id** (`approval_subscriber.py` `_answers_this_gate`, ~297-314; only a reject is). This is the same for Slack today |

  To keep the dashboard's answers tight where Forge is looser, the board, in the same snapshot as step 2:
  - re-reads the card's **current** request id, attempt and correlation id;
  - sends only for that request id;
  - always sets the envelope's correlation id.

  If the record has moved on (a newer attempt, or the card has closed), it sends nothing and shows the current card
  instead.

  **The copy must be fresh.** The mirror keeps its last good copy when Forge's record can't be read
  (`forge_mirror.py` ~208-222), so a copy can be old. In the same snapshot, the answer route checks the factory
  record's freshness (`dbread` freshness: last successful read and heartbeat). It refuses to send when:
  - freshness is unknown;
  - the last successful read is older than 60 seconds;
  - the feed has stopped.

  The page then says "The factory's record is out of date, so answers are paused. Try again in a minute." Confirmation
  and its "two passes" rule count only successful, fresh reads, so a stopped feed never clears a claim or declares an
  answer unrecorded.

  **The race that remains.** Even with a fresh copy, the card can change between the board's last successful read
  and Forge receiving the answer. That window is the age of the copy, at most the 60-second limit, plus delivery time.
  For a build gate, Forge doesn't re-check the request id of an approve, so an answer that reaches it after a newer
  gate card has opened on the same build would act on that newer card. Slack has the same race today. It is recorded
  here and not fixed in Forge in this change.

  **Tests:** a stopped feed, a failed read, and a copy older than the limit each refuse with that sentence and send
  nothing.
- **Least permission.** The bus user can post card answers and nothing else. A leaked credential could only post an
  answer under an identity Forge would refuse, unless the attacker also knew an approver identity. Approver identities
  are not secrets, which is why Forge's own identity check is not the only protection: the bus user, the board's
  sign-in and the tailnet are the others.
- **A stolen dashboard session could approve a merge,** and a merge also deploys. This is the same risk as a stolen
  Slack session. It's limited by sign-in, the same-origin check, the cookie rules and the tailnet. As Rich asked, there's
  no extra confirmation step.
- **Honest display.** What the page shows is always Forge's recorded answer. "Sent" never pretends to be "recorded".
- **What the board writes:** `suggestions.db` and the new claims table, both board-owned. The projector writes the
  read model, as before.

## Gaps, said plainly

- **The build gate's answerer** isn't in Forge's record, so the page shows "answered" without a name.
- **The older assumptions checkpoint** can't be answered here; the page says to use Slack.
- **Which door answered** isn't recorded, so the page shows only who answered.
- **Two answers at once** from different doors (Slack and the dashboard): Forge's first-answer rule decides, and the
  page shows Forge's answer. For a build gate that rule is the in-memory 300-second duplicate check (see "Safety").
- **Who rejected a mission plan** isn't recorded by Forge, so the page doesn't show it.
- **Why a card stayed open after an answer** isn't recorded either: Forge logs refusals without writing them down.

## Tests

- One end-to-end test per card kind, publishing to a fake bus client. Each checks:
  - the exact subject and payload;
  - the envelope's correlation id;
  - `decided_by`.
- Already answered: nothing is sent, and the page names the recorded answer.
- Claim races: two presses at once send only one answer.
- Guards: not signed in, a client account, a request from another site, and an account with no `approver_identity`.
- The plan-card `reject` prefix, and the meaning of the sign-in card's buttons.
- The new fence tests.
- The bus permission. A check against a real `nats-server` started for the test from the template shows the user can
  publish on a response subject and is refused on anything else, including `agents.approval.forge.x` and `pipeline.>`.
  Where nats-server isn't available, the test is skipped and says so.
- A real-browser test answering a merge card, against a fake bus and a fake record update.

## Rollout

- **Through ai-transition-03, as a release:** the bus account change, which needs a bus configuration reload; the
  password file; and the `forge.yaml` approver entry.
- **The dashboard rebuild,** after those, under the dashboard handoff's authority.
- **First live use:** answer one real card from the dashboard, for example a declined merge on a test-fixture
  feature, and confirm Forge recorded it.
