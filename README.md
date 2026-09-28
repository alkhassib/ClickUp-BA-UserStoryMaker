# BA Flow

Automates the Business Analyst gap-resolution flow on ClickUp:

1. A User Story document is attached to a task in **US Intake** and moved to `to analyze`.
2. An LLM finds the gaps and creates a **Draft Ticket (DT)** with one subtask per gap and suggested options.
3. The BA picks an option per gap (`Selected Option`) and moves the DT to `submitted`.
4. The answers are validated (deterministic checks first, then an LLM contradiction check).
   Problems -> the DT goes back to `ba review` with a comment. All good -> a User Story ticket is created
   from the template in `templates/` and the DT and gaps are marked compiled.

No ClickUp Automations are used; every change is made by this app through the ClickUp API.

> Status: **Phases 1–4 done** (foundation, analysis W1, validation W2, finalization), tested end-to-end on ClickUp,
> including a real 19-story Arabic document.
> Webhook and hybrid triggers are available (`ba-flow serve`).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env   # then fill in the values
```

`.env`:

| Variable | Notes |
|---|---|
| `CLICKUP_API_TOKEN` | Token of a **dedicated bot user**, not a BA's personal token. The workspace needs a paid ClickUp plan: the free plan limits custom field usage and blocks the flow after a few tickets. Status changes made by this user are treated as the bot's own actions. |
| `CLICKUP_TEAM_ID` | Optional, overrides `clickup.team_id`. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Only the one for `llm.provider` is needed. |

Everything else lives in [`config/settings.yaml`](config/settings.yaml). ClickUp lists, statuses and custom
fields are referenced **by name**; IDs are resolved automatically.

## Checking the configuration

`check-config` validates `settings.yaml` against the real ClickUp workspace: token and bot identity, team,
space, lists, every configured status, custom fields and dropdown options, template files and API keys.

```powershell
.venv\Scripts\ba-flow check-config
```

**When it runs**

- **Automatically at startup.** The app refuses to start if anything configured is missing in ClickUp.
  The resolved IDs are cached in SQLite and re-checked automatically when `settings.yaml` changes, when the
  cache expires (`storage.cache_ttl_seconds`), or when ClickUp rejects an ID.
- **Manually** — run it yourself:
  - before the first run, and after editing `settings.yaml` or `.env`;
  - after renaming or changing lists, statuses or custom fields in ClickUp;
  - when the app reports a configuration error.

Exit code: `0` passed, `1` a check failed, `2` `settings.yaml` itself is invalid.
Warnings (for example a missing optional story metadata field) do not block the app.

## Running

```powershell
ba-flow run                        # poll every trigger.interval_seconds (check-config runs first)
ba-flow run --once                 # one poll cycle, then exit
ba-flow run --once --dry-run       # analyze but write nothing to ClickUp
ba-flow analyze <task_id> [--dry-run]   # analyze one US Intake task now, whatever its status
ba-flow validate <dt_id> [--dry-run]    # validate one submitted Draft Ticket now
```

W1 (analysis): attach a `.docx`, `.pdf`, `.md` or `.txt` to a task in **US Intake** and move it to
`to analyze`. The bot moves it to `analyzing`, reads the newest supported attachment once, builds the
Context Pack, creates the Draft Ticket (assigned to the intake task's creator) with one subtask per gap,
moves the DT to `ba review` and the intake task to `analyzed`. On failure the intake task goes to
`needs attention` with a comment explaining why.

**Documents with several stories** (e.g. one "### قصة المستخدم: …" / "### User Story: …" section per story)
are split automatically: each story gets its own analysis, Context Pack and Draft Ticket (`DT · US-IND-01 …`).
Content before the first story (actors table, conventions) is shared context for every story. Stories whose
body only says they were cancelled are skipped. To analyse only some stories, write a line in the intake
task description:

```
stories: US-IND-01, US-COMP-01
```

The intake task becomes `done` only when every Draft Ticket created from it is compiled (progress is posted
as comments, e.g. "1/3 compiled"). A story that fails does not block the others; move the intake task to
`to analyze` again to retry it — finished stories are skipped. `.docx` tables nested inside table cells
(typical "required data" field tables) are read too. `llm.max_doc_tokens` applies per story.

If `analyze` returns no gaps, it is retried once with the `analyze_retry` model; if there are still none, the
DT is created without gaps and the BA just reviews and submits it.

Re-running is safe: finished steps are skipped, and every created task carries a `Source Key` that is
looked up before creating anything, so nothing is duplicated even after a crash. LLM results are cached by
document + prompt version + model, so re-analysing an unchanged document costs no tokens.

W2 (validation): when the BA moves a DT to `submitted`:

1. **Code checks (0 tokens):** every gap has a value, the letter exists for that gap, `Other` has a
   `BA Custom Answer`, `TBD` / `N/A` have a `Justification`, no gap subtask was deleted. Subtasks the BA
   added by hand are treated as extra gaps (answer them with `Other`, `TBD` or `N/A`).
   Problems -> comment on the DT, DT back to `ba review`; answered gaps go to `gap closed`, the rest stay
   `gap open`.
2. **One LLM call** (only the Context Pack + decisions, never the document) checks contradictions and, if
   there are none, writes the story in `output.user_story_language`. Contradictions -> comment, DT back
   to `ba review`.
3. **Checks on the story:** no template placeholders, no invented ids (MSG-, CFG-…), every AC/EX points to an
   existing BR/AC, right language. One automatic retry; anything left is posted as a warning on the DT.
4. **Finalization (resumable):** the story is created in **User Stories** from
   `templates/user_story.{ar,en}.md.j2` (TBD -> open questions, N/A -> out of scope, decisions table),
   gaps -> `gap compiled`, DT -> `dt compiled`, intake -> `done`.

Model per step is configurable (`llm.steps`, `llm.step_effort`). All steps use `gpt-5.6-luna`: on the
hardest real cases it matched `gpt-5-mini` at about half the cost, while `gpt-5-nano` had returned no gaps for
rich stories and missed a clear contradiction.

Prompts live in `config/prompts/*.md` with a `<!-- version: … -->` header; bump it when you edit a prompt.

## Webhook and hybrid mode

Polling (`ba-flow run`) needs nothing public. Webhooks react in seconds and can also see **who** changed a
status, which enables one more rule: if a BA moves a Draft Ticket somewhere only the bot may move it (e.g. straight
to `dt compiled`), the bot moves it back and explains why in a comment.

| `trigger.mode` | Command | What runs |
|---|---|---|
| `polling` | `ba-flow run` | poll every `interval_seconds` |
| `webhook` | `ba-flow serve` | HTTP endpoint + one catch-up poll at startup |
| `hybrid` (recommended) | `ba-flow serve` | HTTP endpoint + catch-up poll + slow safety poll every `safety_poll_seconds` |

Setup:

```powershell
# 1. expose the service on a public https URL (reverse proxy, cloud host or a tunnel), then:
ba-flow webhook register --url https://<public-host>/clickup/webhook --save   # writes id + secret to .env
# 2. set trigger.mode to hybrid (or webhook) in config/settings.yaml, then:
ba-flow check-config
ba-flow serve                    # listens on trigger.webhook.host:port; GET /health for monitoring
```

Running it on a server (Docker, Linux service or Windows, reverse proxy, what IT must provide): see
[DEPLOY.md](DEPLOY.md).

How it behaves:
- The endpoint only verifies the `X-Signature` (HMAC-SHA256 of the body with the webhook secret), queues the
  event and answers immediately — ClickUp marks a webhook as failing after 7 seconds. Invalid signatures get
  `403` (a `401` would make ClickUp suspend the webhook).
- One worker thread processes webhook events and polls in order, so the same ticket is never processed twice
  in parallel. Status changes made by the bot user are ignored.
- `debounce_seconds`: the worker waits, re-reads the task, and skips the event if the status has changed again
  (e.g. a BA clicked `submitted` and moved it back).
- `ba-flow webhook list` shows the webhook health; `ba-flow webhook reactivate <id>` restores a suspended one;
  `ba-flow webhook delete <id>` removes it.

## Other commands

```powershell
ba-flow refresh-cache              # drop cached ClickUp IDs and resolve again
ba-flow runs list [--task ID]      # recent processing runs
ba-flow runs show <run_id|prefix>  # one run: tokens, results, errors
```

Every run has a `run_id`. It appears in every log line (`logs/ba_flow.jsonl`), in the `runs` table and at the
end of every comment the bot writes on ClickUp (`run: 1a2b3c4d`).

## ClickUp workspace

| List | Statuses |
|---|---|
| US Intake | new, to analyze, analyzing, analyzed, needs attention, done |
| Draft Tickets | draft, gap open, ba review, submitted, needs attention, gap closed, gap compiled, dt compiled |
| User Stories | ready, in progress, complete |

Draft Tickets fields: `Selected Option` (A–E, TBD, Other, N/A), `BA Custom Answer` (required for Other),
`Justification` (required for TBD and N/A), `Source Key`.
User Stories fields: `Source Key`, `Epic`, `Actor`, `Channel`, `Story Type`, `MoSCoW`, `Version`.
