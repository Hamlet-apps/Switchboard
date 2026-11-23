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
| `nodes.py` | What every kind of step is. Built-ins (`manual`, `schedule`, `webhook`, `failure`, `condition` = Filter, `if`, `switch`, `loop` = For each, `http`) are described here; `event` and `action` nodes are generated from a connection's manifest. `catalog()` feeds the picker, `problems()` says why a workflow can't run |
| `templating.py` | `{{uid.path}}` tokens, `{{secret.NAME}}`, lookup tables (`{"map": …}`: no longer offered in the builder, a Switch does that; still resolved for workflows that have one), `compare()` for conditions, `coerce()` to a param's type |
| `schema.py` | Holding buildings to their manifest: `manifest_problems` (refuse or note, at connect and Check again), `check_params` and `literal_problem` (a step's settings against the params' types and limits, before sending), `mismatches` + `remember` (answers and events that don't match, kept on `Connection.contract_notes` and shown on the Buildings page; never fails a run) |
| `buildings.py` | Manifest, handshake, action calls, signature check, and the HTTP step. Every outgoing request goes through `_http` |
| `engine.py` | `start_run`, `execute` (retries on network errors and 5xx, never 4xx), `retry` (from the failed step), `receive_event`, missed events and replay, "a workflow failed" |
| `worker.py` | The background thread (`submit`, `kick`), schedules (`next_fire`, `fire_schedules`), pruning |
| `recipes.py` | Ready-made workflows built from connected buildings |
| `views/` | `account` (setup, sign in), `workflows`, `builder` (page + JSON calls), `runs`, `connections` (Buildings), `settings`, `hooks` (what other servers call) |
| `static/board/board.css` | All styles: tokens at the top, then shared pieces, then one block per screen |
| `static/board/builder.js` | The builder: canvas, picker, node editor. `ui.js`: toasts, confirms, live regions |

## How it works

- **Steps are a wired graph**, kept as a flat list with the trigger first. Each step has `next`,
  a map from each of its outputs to the uid it leads to (`main`; `true`/`false` for `if`; one per
  case plus `default` for `switch`, whose cases are in `step["cases"]`; `each`/`done` for `loop`), and `x`/`y`, its place on
  the canvas. An output may be unwired (the run ends there) and several may lead to one step;
  loops are refused. A run follows one path. `condition` is the Filter node (the name is kept
  because workflows store it): it stops the run. Use `nodes.ports`, `nodes.reachable`,
  `nodes.before` (the steps a step can read from) and `nodes.find`; `nodes.upgrade` turns the two
  older shapes (a plain list, and a tree with `branches`) into this one, so call it before
  reading stored steps. `builder.js` mirrors these (`ports`, `downstream`, `before`, `tidy`).
- **For each** (`loop`): its `list` setting is a list field (protocol type `list`: an array of
  objects whose fields the manifest gives as `items`) or text with commas. The steps wired to
  `each` run once per item and read `{{<loop uid>.item.<field>}}`, `.index`, `.count`; then the
  run carries on from `done`. Each pass is an *iteration* (`'3'`, or `'3.2'` for a loop in a
  loop): `RunStep.iteration`, the context key `uid@3`, and its own `Idempotency-Key`, so a retry
  picks up at the item that failed. Inside a loop a Filter skips only that item. A step can't be
  wired both inside a loop and after it (`nodes.problems`). Limits: `LOOP_LIMIT` items,
  `RUN_STEP_LIMIT` steps per run. Lists are never flattened into `people.0.name` fields.
- **The canvas** (`builder.js`): steps are absolutely placed `.gnode`s, wires are SVG paths drawn
  by `drawWires()` from measured heights. Dragging from an output dot onto a step connects them
  (`connect`), onto empty canvas opens the picker there; the + on a wire inserts a step and the
  rest of the line carries on from the new step's first output; removing a step joins what led
  into it to what came after. Steps without a position are laid out by `tidy()` on first render.
- **Logos**: a building's tile comes from `buildings.look()`: the copy of its manifest `icon`
  (`Connection.icon_data`, served by `building_icon` as an image only), else the bundled logo in
  `static/board/buildings/<slug>.svg`, else its initials.
- **A step** is `{"uid", "node", "connection", "key", "params"}`; the first step is the trigger.
  A param is text with tokens, or a lookup table. A setting that is exactly one token keeps
  the value's type.
- **A run** snapshots the workflow's steps and keeps each step's output in `Run.context` by
  uid. `Run.dedupe` + the step's uid make the `Idempotency-Key`. A retry is the same run executed
  again: steps that already have a result are skipped and an If or Switch keeps the output it
  left by (its result holds `branch`), so nothing is done twice. `Run.failed_uid` is where it stopped. One run per (workflow, event).
- **Events** arrive at `/hooks/building/<Connection.hook_token>`, signed with the connection's
  webhook secret. They are stored (`Event`) before anything runs; duplicates are dropped by
  (connection, event id). Events for a paused workflow show as missed calls in its builder
  and can be replayed. "Test this step" on a building or webhook trigger listens: the builder
  polls `workflow_listen` (every 2 s, up to 10 min) and what comes in becomes the trigger's
  sample. Nothing runs. A building trigger waits for a matching `Event` newer than when it
  started. A webhook has two URLs: production (`/hooks/w/<token>`, starts runs while on) and
  test (`…/test`), which answers only while `Workflow.test_until` is ahead (each poll pushes it
  30 s on) and leaves the call in `Workflow.test_call` for the next poll.
- **Runaway guard**: buildings' actions emit events, so a workflow can trigger itself. `engine._runaway` pauses a workflow that starts `RUNAWAY_LIMIT` (120) times within `RUNAWAY_WINDOW` (60 s) and leaves a failed run saying why.
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
- Link our CSS and JS with `{% asset %}` (`board_tags`), not `{% static %}`: in development it
  adds the file's modified time so browsers don't keep a stale copy.
- Canvas classes are short (`.out`, `.port`, `.in`) and so are the editor's (`.pane.out`,
  `.pane.in`): scope canvas rules under `.gnode` so they don't reach the editor.
- Copy is for non-technical admins: say what happened and what to do, no jargon. Error
  messages from buildings are shown as they come.
- Add a test with every change; fake buildings with `FakeWorld` in `board/tests.py`.
- Matching people between buildings is by email for now.
