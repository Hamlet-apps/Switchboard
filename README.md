# Switchboard

Switchboard wires [Hamlet](https://github.com/Hamlet-apps) buildings together. When something
happens in one building (a volunteer levels up in Town Hall), a workflow puts the call
through to the others (Pavilion issues them a bigger meal card). It is a small, no-code
workflow builder that already knows every Hamlet app: each building publishes what it can
send and do, and Switchboard turns that into steps you pick from a list.

Free, open source and self-hosted, like the rest of the village.

## What it does

- **Workflows**: a trigger, then steps you wire together on a canvas. Drag from a step's
  output to another step to connect them, drag steps where you like, or press Tidy. Triggers are a
  building's event, a schedule, a private webhook URL, a button, or "a workflow failed".
  Steps look something up in a building, do something there, or send an HTTP request to
  anything else. Three logic steps steer a run: **Filter** (only continue if a condition is
  true), **If** (a True branch and a False branch) and **Switch** (one branch per case, plus
  Otherwise). Each of those ways out is an output you wire to whatever should happen next,
  and two wires can lead to the same step.
- **Fields, not code**: every step shows the data that flows into it from the steps wired
  before it. Drag a field into a setting.
- **Recipes**: ready-made workflows you switch on, such as *Staff card by volunteer level*
  and *Sync shifts to time clock*.
- **Runs**: a plain log of every call: what started it, what each step sent and got back,
  where it stopped. Failed runs can be retried from the step that failed, and nothing is
  done twice (every action carries an idempotency key).
- **Buildings**: connect a building by typing its address and approving there. No keys to copy.

## Running it

Python 3.12 or newer.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

Open http://127.0.0.1:8000/. The first visit asks for your organization's name and makes the
first login. Then go to **Buildings** and type the address of your Town Hall or Pavilion.

```sh
.venv/bin/python manage.py test          # the test suite (buildings are faked)
.venv/bin/python manage.py runworker     # only when SWITCHBOARD_WORKER=off
.venv/bin/python manage.py changepassword <username>   # if everyone is locked out
```

## Deploying

`nixpacks.toml` builds and starts it with gunicorn. Set these (see `.env.example`):

| Variable | What it does |
| --- | --- |
| `ENVIRONMENT=production` | Turns DEBUG off and makes `SECRET_KEY` required |
| `SECRET_KEY` | Long random text. It also encrypts the building keys and secrets Switchboard stores, so changing it means reconnecting buildings and re-entering secrets |
| `DATABASE_URL` | Postgres connection string. Without it, SQLite next to `manage.py` |
| `ALLOWED_HOSTS` | Comma-separated host names, e.g. `switchboard.example.org` |
| `SWITCHBOARD_WORKER` | `off` if you run `manage.py runworker` as its own process. By default each web process runs workflows on a background thread |

Buildings must be able to reach Switchboard (they POST events to it) and Switchboard must be
able to reach them, so both need addresses the other can open.

## How buildings plug in

[docs/PROTOCOL.md](docs/PROTOCOL.md) is the whole contract: a manifest at
`/.well-known/switchboard`, an approve-in-the-browser handshake, signed event webhooks and
bearer-key actions with idempotency keys. Anything that speaks it shows up in the builder;
Switchboard has no code for any particular building.

Until the village has shared accounts, people are matched between buildings by email.

## Not built yet

Loops over lists, workflow versions, a recipe gallery
shared between organizations, and per-connection scopes (a connection can use every action
its building offers).
