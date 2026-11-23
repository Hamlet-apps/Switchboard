# Switchboard building protocol, version 1

This is the contract between Switchboard and a Hamlet building (Town Hall, Pavilion, …).
A building does three things: it publishes a **manifest** of what it can emit and do, it
**sends events** to every Switchboard connected to it, and it **runs actions** Switchboard
asks for. Switchboard never needs building-specific code: its nodes are generated from the
manifest.

All bodies are JSON (UTF-8). All paths below are fixed and sit outside any language prefix.

## 1. Manifest

`GET /.well-known/switchboard` — public, no auth, `Cache-Control: no-store`.

```json
{
  "protocol": "1",
  "building": "town-hall",
  "name": "Town Hall",
  "instance": "Holy Eucharist Cathedral",
  "icon": "https://volunteers.example.org/static/base/town-hall.svg",
  "authorize_url": "https://volunteers.example.org/switchboard/authorize",
  "token_url": "https://volunteers.example.org/switchboard/v1/token",
  "api_base": "https://volunteers.example.org/switchboard/v1",
  "triggers": [
    {
      "type": "volunteer.level_reached",
      "title": "Volunteer reached a level",
      "description": "Starts every time a volunteer levels up.",
      "summary": "{person.name} reached level {level}",
      "fields": [
        {"key": "person.id", "type": "string", "label": "Person ID", "example": "42"},
        {"key": "person.name", "type": "string", "label": "Name", "example": "Maria K."},
        {"key": "level", "type": "int", "label": "Level", "example": 3}
      ]
    }
  ],
  "actions": [
    {
      "name": "person.find",
      "kind": "lookup",
      "title": "Find volunteer",
      "description": "Looks for a volunteer by email.",
      "method": "POST",
      "path": "/actions/person.find",
      "params": [
        {"key": "email", "type": "string", "label": "Email", "required": true,
         "help": "The address they signed up with."}
      ],
      "output": [
        {"key": "found", "type": "bool", "label": "Found", "example": true},
        {"key": "person_id", "type": "string", "label": "Person ID", "example": "42"}
      ]
    }
  ]
}
```

- `building` is the product's slug (`town-hall`, `pavilion`), `name` the product's name,
  `instance` the organization's name as set in that building.
- `icon` (optional) is the address of the building's logo: a square SVG (or PNG, WebP or
  JPEG) under 200 KB that looks right at 20 to 56 pixels, with its own background, like an
  app icon. Switchboard fetches it when the building is connected or checked again, keeps a
  copy, and draws every step of that building with it. Without one, the building is drawn
  with its initials.
- URLs are absolute and built from the request's host (https behind a proxy).
- `triggers[].type` and `actions[].name` are lowercase, dot-separated, and stable: workflows
  store them. Titles and descriptions are for people, in plain language, in English.
- Field `key`s are dotted paths into the event's `data` (triggers) or into the action's
  response (output). `type` is one of `string`, `int`, `number`, `bool`, `datetime`
  (ISO 8601 with offset), `money` (a decimal number in JSON, e.g. `10.5`). Every field has an
  `example`; Switchboard uses the examples as sample data in the builder.
- `summary` is a one-line template for the run log; `{key}` is replaced with the field.
- `kind` is `lookup` (reads, safe to repeat) or `action` (changes something).
- A trigger or action may give a `group` ("Orders", "Gift cards"): the heading it is listed
  under when a building offers many. Without one, Switchboard uses the first word of its
  `type` or `name`.
- Param `type`s are the same list; a param may add `"choices": [{"value", "label"}]` and
  `"default"`. Missing optional params are simply left out of the request.
- A param may state its limits: `"max_length"` for text, `"min"` and `"max"` for numbers.
  Switchboard shows them next to the setting and refuses a value outside them before sending
  anything, so say them here rather than only answering `422`.

### How strictly this is held

The manifest is a promise, and Switchboard checks it:

- **When connecting or checking again.** A manifest it can't build steps from is refused, with
  the reason shown to the admin: a trigger `type` or action `name` that isn't lowercase words
  joined by dots, a duplicate, a `kind` other than `lookup` or `action`, a field or param
  without a known `type`. Smaller slips (a missing example, an example or default of the wrong
  type, a missing label) don't block the connection; they are listed on Switchboard's Buildings
  page for whoever maintains the building.
- **Before every action.** Settings are converted to each param's type and checked against its
  limits; a value that doesn't fit fails the step in Switchboard with a plain message.
- **After every answer, and for every event.** Declared fields that are present must be of the
  declared type (a field that is missing or `null` is fine). A mismatch never fails a run; it
  is listed on the Buildings page, as is an event whose `type` the manifest doesn't list.

### Lists

A field (in an action's `output`, or a trigger's `fields`) may be a list of things, so a
workflow can loop over them with a "For each" step:

```json
{"key": "people", "type": "list", "label": "People",
 "items": [
   {"key": "person_id", "type": "string", "label": "Person ID", "example": "42"},
   {"key": "name", "type": "string", "label": "Name", "example": "Maria K."},
   {"key": "email", "type": "string", "label": "Email", "example": "maria@example.org"}
 ],
 "example": [{"person_id": "42", "name": "Maria K.", "email": "maria@example.org"}]}
```

- The value is a JSON array of objects. `items` declares each object's fields the same way
  as any other fields (keys may be dotted paths; no lists inside lists). The `example` is an
  array with at least one item.
- An empty list is `[]`, never left out or `null`.
- At most 200 items. If there are more, send the first 200 in a sensible order and say how
  many there are in all in a separate `count` field.
- Give each item what a later step needs to act on it without another lookup: its id, a
  name, and an email where it is a person.
- Lists are for outputs and events. Params stay single values.

JSON types: `string` is a JSON string (ids too, even numeric ones), `int` a whole number,
`number` and `money` a number (never text like `"10.00"`), `bool` true or false, `datetime`
a string in ISO 8601 with an offset.

Buildings should hold themselves to it as well: each has `switchboard/contract.py`, and its
tests check every action's answer and every event it emits against its own manifest.
- `method` is always `POST` and `path` is relative to `api_base`.

## 2. Connecting

Nobody copies keys. The handshake is the same shape as Town Hall's Bell Tower connection.

1. An admin types the building's address into Switchboard. Switchboard reads the manifest.
2. Switchboard sends the browser to
   `authorize_url?callback=<url>&state=<random>&name=<Switchboard's organization name>`.
3. The building requires a signed-in person who may manage integrations (Town Hall: active
   staff; Pavilion: `admin.switchboard.edit`) and shows a consent page that names the host of
   `callback` and says what the connection allows. `callback` must be http(s).
   - Approve: redirect to `callback?code=<one-time code>&state=<state>`.
   - Cancel: redirect to `callback?error=access_denied&state=<state>`.
   The code is single use and expires after 10 minutes.
4. Switchboard, server to server: `POST token_url`
   ```json
   {"code": "…", "webhook_url": "https://switchboard.example.org/hooks/building/<token>", "name": "…"}
   ```
   The building answers `200`
   ```json
   {"api_key": "sbk_…", "webhook_secret": "…", "connection_id": "7",
    "building": "town-hall", "instance": "Holy Eucharist Cathedral"}
   ```
   and from then on sends its events to `webhook_url`. The key and secret are shown this once;
   the building stores only a SHA-256 hash of the key. (It must keep the webhook secret in a
   form it can sign with.) A wrong, used or expired code is `400 invalid_code`.

A building can be connected to several Switchboards; each connection has its own key, secret
and webhook URL. The building has a settings page listing its connections (name, host, when it
connected, last delivery, recent failures) with a Disconnect button.

## 3. Calling the building

Every call carries `Authorization: Bearer <api_key>`.

| Call | What it does |
| --- | --- |
| `GET {api_base}/` | `{"ok": true, "building": "…", "instance": "…", "connection_id": "…"}`. Checks the key. |
| `POST {api_base}{path}` | Runs an action from the manifest. Body: an object of its params. |
| `DELETE {api_base}/connection` | Switchboard disconnecting. The key stops working, events stop. `{"ok": true}`. |

Action responses are `200` with a JSON **object** whose keys match the manifest's `output`.
A lookup that finds nothing is **not** an error: it answers `200 {"found": false}` so the
workflow can branch on it.

Errors are `{"error": {"code": "…", "message": "…"}}` with the message written for a
non-technical admin (it is shown in the run log):

| Status | Code | When |
| --- | --- | --- |
| 400 | `invalid_request` | Body isn't a JSON object |
| 401 | `unauthorized` | Missing, wrong or revoked key |
| 404 | `unknown_action` | No such action |
| 422 | `invalid_params` | A required param is missing or a value is wrong; say which |
| 409 | `conflict` | The action can't be done in the current state |
| 5xx | `server_error` | Anything unexpected |

Switchboard retries network errors and 5xx with backoff, and never retries 4xx.

### Idempotency

Every `kind: "action"` call carries `Idempotency-Key: <opaque, ≤ 200 chars>`. The building
stores (connection, key) → the response it gave, for at least 7 days, and answers a repeated
key with that stored response (same status and body) **without doing the work again**. If
Town Hall says "level 3" twice, the volunteer still gets one card. Lookups may ignore the key.

## 4. Events

When something in the manifest's `triggers` happens, the building POSTs this envelope to the
`webhook_url` of every connection, after the database transaction commits and off the
request thread:

```json
{
  "id": "evt_3f6c1d0e9b7a4c58a2d1",
  "type": "volunteer.level_reached",
  "source": {"building": "town-hall", "instance": "https://volunteers.example.org"},
  "occurred_at": "2026-11-19T16:20:04-08:00",
  "subject": {"type": "person", "id": "42", "email": "maria@example.org", "name": "Maria K."},
  "data": {"person": {"id": "42", "name": "Maria K.", "email": "maria@example.org"}, "level": 3}
}
```

- `id` is unique per event and identical on every retry and for every connection; Switchboard
  uses it to drop duplicates.
- `subject` is the person or thing the event is about. Until the village has shared accounts,
  people are matched between buildings **by email**, so events about a person carry
  `subject.email` and `data.person.email` (or `data.staff.email`, `data.customer.email`).
- `data` is nested JSON matching the manifest's dotted field keys.

Headers:

```
Content-Type: application/json
User-Agent: <Building>/1.0 (Switchboard)
X-Switchboard-Event: volunteer.level_reached
X-Switchboard-Delivery: evt_3f6c1d0e9b7a4c58a2d1
X-Switchboard-Signature: sha256=<hex HMAC-SHA256 of the raw body, keyed with webhook_secret>
```

Switchboard answers `2xx` once it has stored the event (it runs workflows afterwards).
Any other answer, a timeout (10 s) or a network error is a failed delivery: the building
retries up to 5 times in total with growing waits (about 5 s, 30 s, 2 min, 10 min). A `410`
means Switchboard no longer knows this connection: stop retrying and mark the connection as
broken on the settings page (don't delete it).

The building keeps a short delivery log (event type, time, status, attempts) per connection
so an admin can see that calls are going through, and prunes it (keep about two weeks).

Nothing is sent while no Switchboard is connected, and emitting must never slow down or
break the thing that caused the event: failures are logged, not raised.

## 5. What each building offers

A building's manifest is the full, current list: read `/.well-known/switchboard` on a running
building, or `switchboard/manifest.py` in its repository. This section only gives the shape of
each, and the few names Switchboard's ready-made recipes depend on (those must never change).

Shared shapes, so workflows can mix buildings: a person is `person.id|name|email` in Town Hall
and `staff.id|name|email` or `customer.name|email` in Pavilion; ids are strings; a lookup
answers `found`, and one that can match several answers `count`, the first match in full, and the matches as a
list a workflow can loop over.

### Town Hall (`town-hall`): 43 events, 36 actions

- **People**: registered, level reached, profile completed, email changed, deactivated or
  reactivated, role assigned or unassigned, points awarded, endorsement given. Find, search,
  update, deactivate; award points; give an endorsement; assign roles; check whether someone
  qualifies for a role.
- **Sign-ups and shifts**: signed up, cancelled, shift filled, shift lead and area lead
  assigned, invitations sent, accepted and declined, shift started and completed. Sign someone
  up (with the same checks a volunteer faces), cancel, invite, start and end a shift.
- **Events**: created, published, unpublished, rescheduled, coordinator added, feedback
  submitted. Find, create (always unpublished), update, publish, add a shift, message the
  event's volunteers.
- **Training, certificates, venues**: module published and started, quiz passed, training
  completed; certificate submitted, approved, rejected; venue note posted; lookups for each.
- **Time-based**, each sent once: event starting in 24 hours, started, ended; shift still
  short 48 hours ahead; shift starting soon; no-show; training expired; certificate expiring
  and expired.

Recipes use: trigger `volunteer.level_reached` (`person.email`, `person.name`, `level`),
trigger `shift.signed_up` (`person.name`, `person.email`, `shift.starts_at`, `shift.ends_at`).

### Pavilion (`pavilion`): 60 events, 33 actions

- **Orders**: placed, approved, a station done, ready, picked up, rejected, refunded, discount
  used; deliveries assigned, picked up, delivered. Find an order, send its receipt, a day's
  sales summary. Nothing creates, changes, charges or refunds an order.
- **Catering and services**: requested, confirmed, changes proposed and countered, message
  received, rejected, cancelled, deposit held, completed; service booked. Find a request and
  send its customer a message.
- **Gift and staff cards**: sold, issued, used, loaded, emptied; staff card issued and
  refilled. Issue, look up, load, freeze and resend a gift card (its number is never answered,
  only emailed to its owner); issue a staff card; create and find coupons.
- **Menu and stock**: dish sold out and back, station paused and resumed, stock low and run
  out, stock received, restock placed and delivered, batch cooked. Find dishes and stock, set
  a dish's availability, pause a station, adjust stock.
- **Cash, people and schedule**: drawer opened, closed, cash moved; customer registered; staff
  added, clocked in and out, break started and ended; schedule published; shift offered, taken,
  swapped; time off and availability requested and decided. Find staff and customers, add or
  cancel a shift, request time off, see who is on now.
- **Store**: ordering paused and resumed, store event created, a screen's dongle going offline
  and online. Store status, pause and resume ordering, create a store event, post an
  announcement to the screens.

Recipes use: `staff.find` (`email` → `found`, `staff_id`), `staff.ensure` (`name`, `email` →
`staff_id`), `staff_card.issue` (`staff_id`, `amount`, `label`, `replace_previous`; **the label
is the card type's name** on Pavilion's Staff cards page, so use one label for everyone),
`shift.add` (`staff_id`, `starts_at`, `ends_at`; a shift can't run past midnight).

## 6. Notes from the first two buildings

- A building that is being asked too fast answers `503` with `Retry-After`, not `429`,
  because Switchboard retries 5xx and never 4xx.
- Disconnecting on the building's side doesn't call Switchboard. Switchboard finds out from
  the `401` on its next call and shows the connection as needing to be reconnected.
- Idempotency records also keep 4xx answers, so a repeated key gets the same refusal.
- A building's own actions set off its events (issuing a gift card sends "gift card issued"),
  so a workflow can start itself. Switchboard pauses a workflow that starts 120 times in a
  minute and says why in its run log.
- Dates without a time are text `YYYY-MM-DD` and times of day `HH:MM`: there is no type for
  them yet.
