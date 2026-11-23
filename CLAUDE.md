# CLAUDE.md

Switchboard is Hamlet's workflow builder: a Django 6 app where an event in one building (Town
Hall, Pavilion) starts a workflow that calls actions in other buildings. Server-rendered
templates, plain CSS, vanilla JS only in the builder. No build step.

The design is the Claude Design canvas "Switchboard — app design"
(<https://claude.ai/artifact/8QY7BF3moND9eE7Xtf7qpz>): sign in, workflows, runs, the builder
with its trigger picker and node editor. Treat the artboards as the visual spec. Buildings
and Settings have no artboard and follow the same look. The product spec is the doc
"Switchboard — Hamlet workflow builder (spec)".

## Commands

```sh
.venv/bin/python manage.py runserver
.venv/bin/python manage.py test              # board/tests.py; buildings are faked at board.buildings._http
.venv/bin/python manage.py makemigrations && .venv/bin/python manage.py migrate
.venv/bin/python manage.py runworker         # the worker as its own process (SWITCHBOARD_WORKER=off)
```

`db.sqlite3` is gitignored. Settings switch on `ENVIRONMENT=production` (`.env.example`).

## Layout

One app, `board`.

| File | What it holds |
| --- | --- |
| `docs/PROTOCOL.md` | The contract with buildings. Town Hall and Pavilion implement it in their own `switchboard` apps; change it only together with them |
| `models.py` | `Settings` (singleton, `Settings.load()`), `Secret`, `Folder`, `Connection`, `Workflow` (steps are JSON), `Event`, `Run`, `RunStep` |
| `nodes.py` | What every kind of step is. Built-ins (`manual`, `schedule`, `webhook`, `failure`, `condition` = Filter, `if`, `switch`, `http`) are described here; `event` and `action` nodes are generated from a connection's manifest. `catalog()` feeds the picker, `problems()` says why a workflow can't run |
| `templating.py` | `{{uid.path}}` tokens, `{{secret.NAME}}`, lookup tables (`{"map": …}`), `compare()` for conditions, `coerce()` to a param's type |
| `buildings.py` | Manifest, handshake, action calls, signature check, and the HTTP step. Every outgoing request goes through `_http` |
| `engine.py` | `start_run`, `execute` (retries on network errors and 5xx, never 4xx), `retry` (from the failed step), `receive_event`, missed events and replay, "a workflow failed" |
| `worker.py` | The background thread (`submit`, `kick`), schedules (`next_fire`, `fire_schedules`), pruning |
| `recipes.py` | Ready-made workflows built from connected buildings |
| `views/` | `account` (setup, sign in), `workflows`, `builder` (page + JSON calls), `runs`, `connections` (Buildings), `settings`, `hooks` (what other servers call) |
| `static/board/board.css` | All styles: tokens at the top, then shared pieces, then one block per screen |
| `static/board/builder.js` | The builder: canvas, picker, node editor. `ui.js`: toasts, confirms, live regions |

## How it works

- **Steps are a tree.** `if` and `switch` steps hold `branches` (`[{"id", "steps": [...]}]`: `true`/`false`
  for If; one per case, `{"id", "op", "value", "value2", "label"}`, then `default` for Switch). One
  branch runs, then the steps after the If or Switch. `condition` is the Filter node (the name is
  kept because workflows store it): it stops the whole run. Never loop over `workflow.steps` as if
  it were flat: use `nodes.walk`, `nodes.find` and `nodes.before` (the steps a step can read from;
  sibling branches are left out). The builder's `walk`/`locate`/`before` in `builder.js` mirror them.
- **A step** is `{"uid", "node", "connection", "key", "params"}`; the first step is the trigger.
  A param is text with tokens, or a lookup table. A setting that is exactly one token keeps
  the value's type.
- **A run** snapshots the workflow's steps and keeps each step's output in `Run.context` by
  uid. `Run.dedupe` + the step's uid make the `Idempotency-Key`. A retry is the same run executed
  again: steps that already have a result are skipped and an If or Switch keeps the branch it
  took (its output holds `branch`), so nothing is done twice. `Run.failed_uid` is where it stopped. One run per (workflow, event).
- **Events** arrive at `/hooks/building/<Connection.hook_token>`, signed with the connection's
  webhook secret. They are stored (`Event`) before anything runs; duplicates are dropped by
  (connection, event id). Events for a paused workflow show as missed calls in its builder
  and can be replayed.
- **The worker** is one daemon thread per web process, started on the first request. Runs are
  claimed with a conditional update, so several processes can share the queue. Tests set
  `RUN_INLINE`, which runs on the calling thread.
- **Reconnecting** a building reuses its `Connection` row (matched by `base_url`), so
  workflows keep pointing at it. A 401 from a building marks the connection `broken`.
- **Secrets** (building keys, webhook secrets, `Secret` values) are Fernet-encrypted with a key
  derived from `SECRET_KEY` (`crypto.py`), never rendered back, and redacted from run logs.
- Times: the database is UTC; `ZoneMiddleware` activates the organization's zone for pages,
  and schedules are computed in it.

## Conventions

- Function-based views, `login_required`, POST forms that redirect back with a Django message
  (messages render as toasts). The builder's calls are JSON (`builder.json_call`).
- Everyone with a login is an admin; there are no roles.
- Colours and fonts come from the tokens at the top of `board.css` (night violet `--night`,
  Vice pink `--pink`, rose `--rose` for actions). Space Grotesk for headings, Inter for text,
  JetBrains Mono for field names, Material Symbols Rounded for icons.
- In JS, build DOM with `h()` and `textContent`; never `innerHTML` with data.
- Copy is for non-technical admins: say what happened and what to do, no jargon. Error
  messages from buildings are shown as they come.
- Add a test with every change; fake buildings with `FakeWorld` in `board/tests.py`.
- Matching people between buildings is by email for now.
