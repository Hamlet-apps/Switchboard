/* The workflow builder: canvas, step picker and node editor.
   The page hands over everything as JSON (#builder-data); this script edits `wf.steps`
   in memory and posts it back on Save, Test and the Running switch. All text that comes
   from data is set with textContent. */
(function () {
  'use strict';

  var data = JSON.parse(document.getElementById('builder-data').textContent);
  var csrf = document.getElementById('csrf').value;
  var wf = data.workflow;
  var samples = data.samples || {};
  var results = {};          // uid -> {status, error, output, ms, real, stopped}
  var editing = null;        // uid of the step open in the editor
  var picker = null;         // {mode: 'trigger' | 'step', at, query, open}
  var dirty = false;
  var busy = false;
  var activeSlot = null;     // the setting that field clicks go into
  var savedRange = null;
  var view = { x: 0, y: 0, zoom: 1 };
  var TOKEN = /\{\{\s*([A-Za-z0-9_]+)\.([^{}]+?)\s*\}\}/g;

  var $ = function (id) { return document.getElementById(id); };
  var canvas = $('canvas'), flow = $('flow'), pickerEl = $('picker'), veil = $('veil');

  // ---------- helpers ----------
  function h(tag, attrs) {
    var el = document.createElement(tag);
    for (var key in attrs || {}) {
      var value = attrs[key];
      if (value == null || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'text') el.textContent = value;
      else if (key === 'style') el.style.cssText = value;
      else if (key.slice(0, 2) === 'on') el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? '' : value);
    }
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, child) {
    if (child == null || child === false) return;
    if (Array.isArray(child)) child.forEach(function (c) { add(el, c); });
    else el.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  function icon(name, cls) { return h('span', { class: 'ms ' + (cls || ''), 'aria-hidden': 'true', text: name }); }
  function tile(t, size) {
    if (t && t.img) return h('img', { class: 'tile logo ' + (size || ''), src: t.img, alt: t.label || '', draggable: 'false' });
    var el = h('span', { class: 'tile ' + (size || '') });
    if (t && t.code) { el.textContent = t.code; el.style.background = t.bg; el.style.color = t.fg; }
    else el.appendChild(icon((t && t.icon) || 'help'));
    return el;
  }
  function uid() { return 's' + Math.random().toString(16).slice(2, 10); }
  function toast(text, bad) { window.sbToast(text, bad); }
  function post(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().catch(function () { return { error: 'Switchboard answered ' + response.status + '.' }; });
    }).catch(function () { return { error: "Couldn't reach Switchboard. Check your connection." }; });
  }
  function touch() { dirty = true; renderHead(); }

  // ---------- definitions and sample data ----------
  var BUILTIN = { manual: 1, schedule: 1, webhook: 1, failure: 1, condition: 1, 'if': 1, 'switch': 1, http: 1 };
  
  function defOf(step) {
    var id = BUILTIN[step.node] ? step.node : step.node + ':' + step.connection + ':' + step.key;
    return data.defs[id] || data.missing[step.uid] || {
      node: step.node, title: step.key || 'Unknown step', desc: 'This step is no longer available.',
      kind: 'Not available', tile: { icon: 'link_off' }, params: [], out: [], missing: true
    };
  }

  // ---------- the step graph ----------
  // Steps are a flat list. Each has `next`, a map from each of its outputs to the uid of the
  // step that output leads to, and `x`/`y`, where it sits on the canvas.
  var W = 360;                 // a node's width, as in the stylesheet
  function ports(step) {
    if (step.node === 'if') return ['true', 'false'];
    if (step.node === 'switch') return (step.cases || []).map(function (c) { return c.id; }).concat(['default']);
    return ['main'];
  }
  function portLabel(step, port) {
    if (step.node === 'if') return port === 'true' ? 'True' : 'False';
    if (step.node !== 'switch') return '';
    if (port === 'default') return 'Otherwise';
    var c = (step.cases || []).filter(function (x) { return x.id === port; })[0];
    if (!c) return '';
    if (c.label) return c.label;
    var op = data.mapOps.filter(function (o) { return o.value === c.op; })[0];
    var words = op ? op.label : 'is';
    if (c.op === 'between') return words + ' ' + (c.value || '…') + ' and ' + (c.value2 || '…');
    return words + ' ' + (c.value || '…');
  }
  function newCase() { return { id: 'c' + Math.random().toString(16).slice(2, 8), op: 'eq', value: '', value2: '', label: '' }; }
  function allSteps() { return wf.steps; }
  function stepByUid(id) { return wf.steps.filter(function (s) { return s.uid === id; })[0]; }
  function targets(step) {
    return ports(step).map(function (p) { return (step.next || {})[p]; }).filter(Boolean);
  }
  function hasTrigger() { return !!wf.steps.length && !!defOf(wf.steps[0]).trigger; }
  // Everything a run can reach starting at `from` (inclusive).
  function downstream(from) {
    var seen = {}, queue = [from], out = [];
    while (queue.length) {
      var id = queue.shift(), step = stepByUid(id);
      if (seen[id] || !step) continue;
      seen[id] = 1; out.push(step);
      queue = queue.concat(targets(step));
    }
    return out;
  }
  // The steps whose fields `id` can use: every step a run may pass through on its way there.
  function before(id) {
    var found = {}, queue = [id];
    while (queue.length) {
      var current = queue.pop();
      wf.steps.forEach(function (s) {
        if (!found[s.uid] && s.uid !== id && targets(s).indexOf(current) >= 0) { found[s.uid] = 1; queue.push(s.uid); }
      });
    }
    var order = wf.steps.length ? downstream(wf.steps[0].uid) : [];
    return order.filter(function (s) { return found[s.uid]; });
  }
  function wouldLoop(from, to) { return downstream(to).some(function (s) { return s.uid === from; }); }

  // Lay the whole workflow out as a tidy tree under the trigger.
  function tidy() {
    if (!wf.steps.length) return;
    var root = wf.steps[0], depth = {}, guard = 0;
    (function visit(step, d) {
      if (guard++ > 5000) return;
      if (depth[step.uid] === undefined || depth[step.uid] < d) {
        depth[step.uid] = d;
        targets(step).forEach(function (t) { var next = stepByUid(t); if (next) visit(next, d + 1); });
      }
    })(root, 0);
    // Each step hangs under one parent: the first one a row above it. An If or Switch keeps
    // a column for every output, so a wire that goes off to join another line has room to
    // run down beside its neighbours instead of behind them (null marks such a column).
    var kids = {}, owned = {};
    downstream(root.uid).forEach(function (step) {
      var many = ports(step).length > 1;
      kids[step.uid] = [];
      ports(step).forEach(function (port) {
        var t = (step.next || {})[port];
        if (t && !owned[t] && depth[t] === depth[step.uid] + 1) { owned[t] = step.uid; kids[step.uid].push(t); }
        else if (many && t) kids[step.uid].push(null);
      });
    });
    var width = {};
    (function measure(id) {
      var total = 0;
      kids[id].forEach(function (k) { total += k ? measure(k) : 1; });
      return (width[id] = Math.max(1, total));
    })(root.uid);
    var column = {};
    (function place(id, left) {
      column[id] = left + width[id] / 2;
      var total = 0; kids[id].forEach(function (k) { total += k ? width[k] : 1; });
      var at = left + (width[id] - total) / 2;
      kids[id].forEach(function (k) { if (k) place(k, at); at += k ? width[k] : 1; });
    })(root.uid, 0);
    var rows = [], tops = [0];
    wf.steps.forEach(function (step) {
      if (depth[step.uid] === undefined) return;
      var tall = heightOf(step) + (ports(step).length > 1 ? 30 : 0);
      rows[depth[step.uid]] = Math.max(rows[depth[step.uid]] || 0, tall);
    });
    rows.forEach(function (tall, d) { tops[d + 1] = tops[d] + tall + 96; });
    var right = 0;
    wf.steps.forEach(function (step) {
      if (depth[step.uid] === undefined) return;
      step.x = Math.round((column[step.uid] - column[root.uid]) * 400);
      step.y = tops[depth[step.uid]];
      right = Math.max(right, step.x);
    });
    // Steps that aren't wired to the trigger wait in a column on the right.
    var parkY = 0;
    wf.steps.forEach(function (step) {
      if (depth[step.uid] !== undefined) return;
      step.x = right + 460; step.y = parkY; parkY += heightOf(step) + 60;
    });
  }

  function flatten(obj) {
    var out = [];
    (function walk(value, key) {
      if (out.length >= 60) return;
      if (value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length) {
        Object.keys(value).forEach(function (k) { walk(value[k], key ? key + '.' + k : k); });
      } else if (Array.isArray(value) && value.length && typeof value[0] === 'object' && value[0]) {
        value.slice(0, 3).forEach(function (v, i) { walk(v, key + '.' + i); });
      } else if (key) out.push([key, value]);
    })(obj, '');
    return out;
  }
  function fieldsOf(step) {
    var sample = samples[step.uid];
    if (sample && typeof sample === 'object' && Object.keys(sample).length) return { real: true, fields: flatten(sample) };
    return { real: false, fields: defOf(step).out.map(function (f) { return [f.key, f.example]; }) };
  }
  function show(value) {
    if (value === null || value === undefined) return ['null', 'v-null'];
    if (typeof value === 'string') return [JSON.stringify(value), 'v-str'];
    if (typeof value === 'boolean') return [String(value), 'v-bool'];
    if (typeof value === 'number') return [String(value), 'v-num'];
    return [JSON.stringify(value), 'v-null'];
  }
  function tokensIn(value) {
    var found = [];
    var scan = function (text) { String(text || '').replace(TOKEN, function (_, src, key) { found.push([src, key]); return ''; }); };
    if (value && typeof value === 'object' && value.map) {
      scan(value.map.on); scan(value.map['else']);
      (value.map.rules || []).forEach(function (r) { scan(r.value); scan(r.value2); scan(r.then); });
    } else scan(value);
    return found;
  }
  function paramValue(step, param) {
    var value = (step.params || {})[param.key];
    return value === undefined ? (param['default'] !== undefined ? param['default'] : '') : value;
  }
  function visible(step, def, param) {
    for (var key in param.show_if || {}) {
      var other = def.params.filter(function (p) { return p.key === key; })[0];
      var current = other ? paramValue(step, other) : '';
      if (param.show_if[key].indexOf(current) < 0) return false;
    }
    return true;
  }

  // ---------- header ----------
  var nameInput = $('wf-name'), switchBtn = $('wf-switch'), testBtn = $('wf-test'), saveBtn = $('wf-save'), runNow = $('run-now');
  function renderHead() {
    switchBtn.setAttribute('aria-checked', wf.enabled ? 'true' : 'false');
    switchBtn.lastElementChild.textContent = wf.enabled ? 'Running' : 'Paused';
    saveBtn.querySelector('.dirty').hidden = !dirty;
    testBtn.disabled = !wf.steps.length || busy;
    testBtn.firstElementChild.textContent = busy ? 'progress_activity' : 'science';
    testBtn.firstElementChild.classList.toggle('spin', busy);
    testBtn.lastElementChild.textContent = busy ? 'Testing…' : 'Test workflow';
    runNow.hidden = !(wf.steps[0] && wf.steps[0].node === 'manual');
  }
  nameInput.addEventListener('input', function () { wf.name = nameInput.value; touch(); });
  nameInput.addEventListener('keydown', function (event) { if (event.key === 'Enter') nameInput.blur(); });

  function payload() { return { name: wf.name, steps: wf.steps }; }
  function save(quiet) {
    return post(data.urls.save, payload()).then(function (answer) {
      if (answer.error) { toast(answer.error, true); return false; }
      dirty = false; wf.enabled = answer.enabled; data.problems = answer.problems || [];
      renderHead();
      if (answer.paused) toast('Saved, and paused: ' + answer.problems[0], true);
      else if (!quiet) toast(data.problems.length && wf.steps.length ? 'Saved. Before it can run: ' + data.problems[0] : 'Saved');
      return true;
    });
  }
  saveBtn.addEventListener('click', function () { save(false); });
  switchBtn.addEventListener('click', function () {
    var want = !wf.enabled;
    post(data.urls.toggle, Object.assign({ on: want }, payload())).then(function (answer) {
      if (answer.error) { toast(want ? "Can't switch it on yet: " + answer.error : answer.error, true); dirty = false; renderHead(); return; }
      dirty = false; wf.enabled = answer.enabled; renderHead();
      toast(wf.enabled ? 'Running. It will pick up new calls.' : "Paused. It won't pick up new calls.");
    });
  });
  runNow.addEventListener('submit', function (event) {
    if (!dirty) return;
    event.preventDefault();
    save(true).then(function (ok) { if (ok) runNow.submit(); });
  });
  testBtn.addEventListener('click', function () {
    if (busy || !wf.steps.length) return;
    var hasAction = allSteps().some(function (s) { return s.node === 'action' && !defOf(s).lookup || s.node === 'http'; });
    if (hasAction && !window.confirm('Testing really runs every step with sample data, so actions happen for real. Go ahead?')) return;
    busy = true; results = {};
    allSteps().forEach(function (s) { results[s.uid] = { status: 'running' }; });
    renderHead(); renderFlow();
    post(data.urls.test, payload()).then(function (answer) {
      busy = false; dirty = false;
      if (!answer.steps) { results = {}; renderHead(); renderFlow(); toast(answer.error || 'The test could not start.', true); return; }
      answer.steps.forEach(function (s, i) {
        setTimeout(function () {
          results[s.uid] = s.status === 'skipped' ? undefined : { status: s.status, error: s.error, output: s.output, real: true, stopped: s.status === 'stopped' };
          if (s.output && typeof s.output === 'object') samples[s.uid] = s.output;
          renderFlow();
        }, 180 * i);
      });
      renderHead();
      var words = { succeeded: 'Test run worked. It’s in Runs.', stopped: 'Test run stopped at a check. It’s in Runs.', failed: 'Test run failed: ' + answer.error };
      toast(words[answer.status] || 'Test finished.', answer.status === 'failed');
    });
  });
  window.addEventListener('beforeunload', function (event) { if (dirty) { event.preventDefault(); event.returnValue = ''; } });

  // ---------- canvas ----------
  var NS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs) {
    var el = document.createElementNS(NS, tag);
    for (var key in attrs || {}) el.setAttribute(key, attrs[key]);
    return el;
  }
  function applyView() {
    flow.style.transform = 'translate(' + view.x + 'px, ' + view.y + 'px) scale(' + view.zoom + ')';
    canvas.style.backgroundPosition = view.x + 'px ' + view.y + 'px';
    canvas.style.backgroundSize = (22 * view.zoom) + 'px ' + (22 * view.zoom) + 'px';
    $('zoom-label').textContent = Math.round(view.zoom * 100) + '%';
  }
  function centre() { view = { x: Math.round(canvas.clientWidth / 2 - W / 2), y: 36, zoom: 1 }; applyView(); }
  // A point on the screen, in the canvas's own coordinates.
  function toFlow(clientX, clientY) {
    var box = canvas.getBoundingClientRect();
    return { x: (clientX - box.left - view.x) / view.zoom, y: (clientY - box.top - view.y) / view.zoom };
  }
  function portX(step, port) {
    var all = ports(step);
    return step.x + W * (all.indexOf(port) + 1) / (all.length + 1);
  }
  function heightOf(step) {
    var el = flow.querySelector('[data-uid="' + step.uid + '"]');
    return el ? el.offsetHeight : 150;
  }
  function curve(x1, y1, x2, y2) {
    var bend = Math.max(40, Math.abs(y2 - y1) / 2);
    return 'M' + x1 + ' ' + y1 + ' C' + x1 + ' ' + (y1 + bend) + ', ' + x2 + ' ' + (y2 - bend) + ', ' + x2 + ' ' + y2;
  }

  function renderFlow() {
    flow.textContent = '';
    applyView();
    $('tip').hidden = !wf.steps.length;
    $('tidy').hidden = wf.steps.length < 2;
    if (!wf.steps.length) {
      flow.appendChild(h('div', { class: 'start' },
        h('button', { type: 'button', 'aria-label': 'Add a trigger', onclick: function () { openPicker('trigger'); } }, icon('add')),
        h('div', null, h('strong', { text: 'Add a trigger' }),
          h('span', { text: 'Every workflow starts with something that happens. Pick what should start this one.' }))));
      return;
    }
    var needsLayout = wf.steps.some(function (s) { return typeof s.x !== 'number' || typeof s.y !== 'number'; });
    if (needsLayout) wf.steps.forEach(function (s) { s.x = s.x || 0; s.y = s.y || 0; });
    var wires = svg('svg', { class: 'wires', 'aria-hidden': 'true' });
    flow.appendChild(wires);
    var wired = {};
    wf.steps.forEach(function (s) { targets(s).forEach(function (t) { wired[t] = 1; }); });
    wf.steps.forEach(function (step, index) {
      var def = defOf(step), result = results[step.uid], isTrigger = index === 0 && def.trigger;
      var node = h('button', {
        type: 'button', class: 'node' + (editing === step.uid ? ' open' : '') + (def.missing ? ' bad' : ''),
        onclick: function () { if (!justDragged) openEditor(step.uid); }
      });
      if (isTrigger) node.appendChild(h('div', { class: 'node-bar' }, icon('bolt', 'fill'), 'Trigger'));
      var state = null;
      if (result && result.status) {
        var look = { running: ['progress_activity', 'spin is-running'], ok: ['check_circle', 'fill is-ok'],
          failed: ['error', 'fill is-failed'], stopped: ['do_not_disturb_on', 'fill is-stopped'] }[result.status];
        if (look) state = h('span', { class: 'ms node-state ' + look[1], role: 'img', 'aria-label': result.status, text: look[0] });
      }
      node.appendChild(h('div', { class: 'node-main' }, tile(def.tile),
        h('div', { class: 'txt' }, h('span', { class: 'node-kind', text: def.kind }), h('h2', { text: def.title }),
          h('p', { text: summary(step, def) })), state));
      var chips = [];
      (def.params || []).forEach(function (p) {
        tokensIn((step.params || {})[p.key]).forEach(function (t) { if (chips.length < 3 && t[0] !== 'secret' && chips.indexOf(t[1]) < 0) chips.push(t[1]); });
      });
      if (chips.length) node.appendChild(h('div', { class: 'node-chips' }, chips.map(function (c) { return h('span', { text: c }); })));

      var loose = !isTrigger && !wired[step.uid];
      var box = h('div', { class: 'gnode' + (loose ? ' loose' : ''), 'data-uid': step.uid, style: 'left: ' + step.x + 'px; top: ' + step.y + 'px' }, node);
      if (!isTrigger) box.appendChild(h('span', { class: 'port in', 'aria-hidden': 'true' }));
      if (loose) box.appendChild(h('span', { class: 'loose-note', text: 'Not connected yet. Drag a wire onto it.' }));
      var all = ports(step);
      all.forEach(function (port, i) {
        var label = portLabel(step, port), target = (step.next || {})[port];
        var out = h('div', { class: 'out' + (target ? ' wired' : ''), style: 'left: ' + (100 * (i + 1) / (all.length + 1)) + '%' },
          h('span', { class: 'port', 'data-port': port, title: 'Drag to another step to connect' }),
          label ? h('span', { class: 'port-label' + (port === 'false' || port === 'default' ? ' quiet' : ''), text: label, title: label,
            style: 'max-width: ' + Math.floor(W / (all.length + 1) - 6) + 'px' }) : null);
        if (!target) {
          out.appendChild(h('button', { type: 'button', class: 'plus', 'aria-label': 'Add a step after ' + def.title + (label ? ' (' + label + ')' : ''),
            onclick: function () { openPicker('step', { from: step.uid, port: port }); } }, icon('add')));
        }
        box.appendChild(out);
      });
      flow.appendChild(box);
    });
    if (!hasTrigger()) {
      var first = wf.steps[0];
      flow.appendChild(h('button', { type: 'button', class: 'plus end add-trigger', 'aria-label': 'Add a trigger', title: 'Add a trigger',
        style: 'left: ' + (first.x + W / 2 - 22) + 'px; top: ' + (first.y - 70) + 'px', onclick: function () { openPicker('trigger'); } }, icon('bolt')));
    }
    if (needsLayout) {
      tidy();
      wf.steps.forEach(function (s) { var el = flow.querySelector('[data-uid="' + s.uid + '"]'); el.style.left = s.x + 'px'; el.style.top = s.y + 'px'; });
    }
    drawWires();
  }

  // The wires, and the little buttons in the middle of each one.
  function drawWires() {
    var wires = flow.querySelector('.wires');
    if (!wires) return;
    wires.textContent = '';
    Array.prototype.forEach.call(flow.querySelectorAll('.wire-tools'), function (el) { el.remove(); });
    wf.steps.forEach(function (step) {
      var all = ports(step), bottom = step.y + heightOf(step);
      all.forEach(function (port) {
        var target = stepByUid((step.next || {})[port]);
        if (!target) return;
        var x1 = portX(step, port), y1 = bottom + 7, x2 = target.x + W / 2, y2 = target.y - 7;
        var from = results[step.uid], to = results[target.uid];
        var went = from && from.status === 'ok' && to && to.status && to.status !== 'running' &&
          (all.length === 1 || (from.output && from.output.branch === port));
        wires.appendChild(svg('path', { d: curve(x1, y1, x2, y2), class: 'wire-line' + (went ? ' went' : '') }));
        // With these control points the curve's midpoint is simply halfway. Labelled outputs
        // keep their label clear, so their buttons sit a little lower.
        var lower = all.length > 1 ? 18 : 0;
        var tools = h('div', { class: 'wire-tools', style: 'left: ' + ((x1 + x2) / 2) + 'px; top: ' + ((y1 + y2) / 2 + lower) + 'px' },
          h('button', { type: 'button', 'aria-label': 'Add a step on this wire', title: 'Add a step here',
            onclick: function () { openPicker('step', { from: step.uid, port: port, to: target.uid }); } }, icon('add')),
          h('button', { type: 'button', class: 'cut', 'aria-label': 'Remove this wire', title: 'Remove this wire',
            onclick: function () { delete step.next[port]; touch(); renderFlow(); } }, icon('close')));
        flow.appendChild(tools);
      });
    });
  }

  function connect(from, port, to) {
    var source = stepByUid(from), target = stepByUid(to);
    if (!source || !target || from === to) return false;
    if (target === wf.steps[0] && hasTrigger()) { toast('Nothing can lead into the trigger. It starts the workflow.', true); return false; }
    if (wouldLoop(from, to)) { toast('That would send the workflow round in a loop.', true); return false; }
    source.next = source.next || {};
    source.next[port] = to;
    touch(); renderFlow();
    return true;
  }

  function summary(step, def) {
    if (step.node === 'if') return 'Sends the run out of True or False.';
    if (step.node === 'switch') return 'Sends the run out of the first case that matches.';
    if (step.node === 'condition') return 'Stops the workflow here unless the condition is true.';
    if (step.node === 'schedule') {
      var p = step.params || {}, at = p.time || '08:00', days = { mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday' };
      if (p.repeat === 'hour') return 'Starts every hour at :' + at.split(':')[1] + '.';
      if (p.repeat === 'day') return 'Starts every day at ' + at + '.';
      return 'Starts every ' + (days[p.weekday] || 'Monday') + ' at ' + at + '.';
    }
    return def.desc;
  }

  // One set of pointer handlers for the three things you can drag: the canvas, a step, a wire.
  var drag = null, justDragged = false;
  canvas.addEventListener('pointerdown', function (event) {
    if (event.button !== 0 || event.target.closest('.zoom, .wire-tools, .plus, .start')) return;
    var port = event.target.closest('.out .port'), box = event.target.closest('.gnode');
    justDragged = false;
    if (port && box) {
      var step = stepByUid(box.getAttribute('data-uid')), name = port.getAttribute('data-port');
      var line = svg('path', { class: 'wire-line pulling' });
      flow.querySelector('.wires').appendChild(line);
      drag = { kind: 'wire', step: step, port: name, line: line, x: portX(step, name), y: step.y + heightOf(step) + 7, moved: false };
      canvas.classList.add('wiring');
      event.preventDefault();
    } else if (box && event.target.closest('.node')) {
      var moving = stepByUid(box.getAttribute('data-uid'));
      drag = { kind: 'node', step: moving, el: box, sx: event.clientX, sy: event.clientY, ox: moving.x, oy: moving.y, moved: false };
    } else if (!event.target.closest('button, input')) {
      drag = { kind: 'pan', sx: event.clientX, sy: event.clientY, ox: view.x, oy: view.y };
      canvas.classList.add('panning');
    }
    // A step is only captured once it really moves, so a plain click still reaches it.
    if (drag && drag.kind !== 'node') canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener('pointermove', function (event) {
    if (!drag) return;
    if (drag.kind === 'pan') {
      view.x = drag.ox + event.clientX - drag.sx; view.y = drag.oy + event.clientY - drag.sy; applyView();
    } else if (drag.kind === 'node') {
      var dx = event.clientX - drag.sx, dy = event.clientY - drag.sy;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 6) return;
      if (!drag.moved) { drag.moved = true; canvas.setPointerCapture(event.pointerId); drag.el.classList.add('moving'); }
      drag.step.x = Math.round(drag.ox + dx / view.zoom); drag.step.y = Math.round(drag.oy + dy / view.zoom);
      drag.el.style.left = drag.step.x + 'px'; drag.el.style.top = drag.step.y + 'px';
      drawWires();
    } else {
      var at = toFlow(event.clientX, event.clientY);
      drag.moved = drag.moved || Math.abs(at.x - drag.x) + Math.abs(at.y - drag.y) > 12;
      drag.line.setAttribute('d', curve(drag.x, drag.y, at.x, at.y));
      var over = nodeAt(event.clientX, event.clientY);
      Array.prototype.forEach.call(flow.querySelectorAll('.gnode.target'), function (el) { el.classList.remove('target'); });
      if (over && over !== drag.step.uid) flow.querySelector('[data-uid="' + over + '"]').classList.add('target');
    }
  });
  function nodeAt(clientX, clientY) {
    var hit = document.elementsFromPoint(clientX, clientY).filter(function (el) { return el.closest && el.closest('.gnode'); })[0];
    return hit ? hit.closest('.gnode').getAttribute('data-uid') : null;
  }
  function endDrag(event) {
    if (!drag) return;
    var was = drag; drag = null;
    canvas.classList.remove('panning'); canvas.classList.remove('wiring');
    if (was.kind === 'node' && was.moved) {
      justDragged = true; setTimeout(function () { justDragged = false; }, 0);
      was.el.classList.remove('moving'); touch();
    } else if (was.kind === 'wire') {
      was.line.remove();
      Array.prototype.forEach.call(flow.querySelectorAll('.gnode.target'), function (el) { el.classList.remove('target'); });
      if (!was.moved || event.type === 'pointercancel') return;
      var over = nodeAt(event.clientX, event.clientY);
      if (over) connect(was.step.uid, was.port, over);
      else {
        // Dropped on empty canvas: add a step right there, on the end of this wire.
        var at = toFlow(event.clientX, event.clientY);
        openPicker('step', { from: was.step.uid, port: was.port, x: Math.round(at.x - W / 2), y: Math.round(at.y) });
      }
    }
  }
  canvas.addEventListener('pointerup', endDrag); canvas.addEventListener('pointercancel', endDrag);
  canvas.addEventListener('wheel', function (event) {
    event.preventDefault();
    if (event.ctrlKey || event.metaKey) setZoom(view.zoom - Math.sign(event.deltaY) * 0.1, event);
    else { view.x -= event.deltaX; view.y -= event.deltaY; applyView(); }
  }, { passive: false });
  function setZoom(z, around) {
    var box = canvas.getBoundingClientRect();
    var cx = around ? around.clientX - box.left : box.width / 2, cy = around ? around.clientY - box.top : box.height / 2;
    var next = Math.min(1.6, Math.max(0.3, Math.round(z * 10) / 10));
    // Keep the point under the cursor (or the middle) where it is.
    view.x = Math.round(cx - (cx - view.x) * next / view.zoom); view.y = Math.round(cy - (cy - view.y) * next / view.zoom);
    view.zoom = next; applyView();
  }
  $('zoom-in').addEventListener('click', function () { setZoom(view.zoom + 0.1); });
  $('zoom-out').addEventListener('click', function () { setZoom(view.zoom - 0.1); });
  $('zoom-fit').addEventListener('click', function () {
    var first = wf.steps[0];
    centre();
    if (first) { view.x -= first.x; view.y -= first.y; applyView(); }
  });
  $('tidy').addEventListener('click', function () { tidy(); touch(); renderFlow(); toast('Tidied up'); });

  // ---------- picker ----------
  // `at` says where a new step goes: {from, port} on the end of an output, plus `to` when
  // it lands on an existing wire, or x/y when a wire was dropped on empty canvas.
  function openPicker(mode, at) {
    closeEditor();
    var side = data.catalog[mode === 'trigger' ? 'triggers' : 'steps'];
    var first = side.buildings.filter(function (b) { return b.items.length; })[0];
    picker = { mode: mode, at: at || {}, query: '', open: first ? first.connection : null };
    renderPicker();
    var search = pickerEl.querySelector('input'); if (search) search.focus();
  }
  function closePicker() { picker = null; pickerEl.hidden = true; pickerEl.textContent = ''; }
  function pick(def) {
    var params = {};
    (def.params || []).forEach(function (p) { if (p['default'] !== undefined) params[p.key] = String(p['default']); });
    var step = { uid: uid(), node: def.node, connection: def.connection, key: def.key || '', params: params, next: {}, x: 0, y: 0 };
    if (def.node === 'switch') step.cases = [newCase()];
    var at = picker.at, from = stepByUid(at.from), onto = stepByUid(at.to);
    if (picker.mode === 'trigger') {
      var old = wf.steps[0];
      if (old && hasTrigger()) {                  // swap the trigger, keeping its place and its wire
        step.x = old.x; step.y = old.y;
        if ((old.next || {}).main) step.next = { main: old.next.main };
        wf.steps[0] = step;
        delete results[old.uid]; delete samples[old.uid];
      } else {
        if (old) { step.x = old.x; step.y = old.y - 230; step.next = { main: old.uid }; }
        wf.steps.unshift(step);
      }
    } else {
      if (onto && from) {
        // On a wire: take the place of the step below and push everything after it down.
        // The rest of the line carries on from the new step's first output.
        step.x = onto.x; step.y = onto.y;
        var push = 230 + (ports(step).length > 1 ? 40 : 0);
        downstream(onto.uid).forEach(function (s) { s.y += push; });
        step.next[ports(step)[0]] = onto.uid;
        from.next[at.port] = step.uid;
      } else if (from) {
        var all = ports(from), i = all.indexOf(at.port);
        step.x = typeof at.x === 'number' ? at.x : Math.round(from.x + (i - (all.length - 1) / 2) * 400);
        step.y = typeof at.y === 'number' ? at.y : from.y + heightOf(from) + (all.length > 1 ? 126 : 96);
        while (typeof at.x !== 'number' && wf.steps.some(function (s) { return Math.abs(s.x - step.x) < 300 && Math.abs(s.y - step.y) < 120; })) step.x += 400;
        from.next = from.next || {};
        from.next[at.port] = step.uid;
      }
      wf.steps.push(step);
    }
    closePicker(); touch(); renderFlow(); openEditor(step.uid);
  }
  function renderPicker() {
    var trig = picker.mode === 'trigger';
    var side = data.catalog[trig ? 'triggers' : 'steps'];
    pickerEl.hidden = false; pickerEl.textContent = '';
    var list = h('div', { class: 'picker-list' });
    var search = h('input', { type: 'search', placeholder: trig ? 'Search triggers and events' : 'Search actions',
      'aria-label': trig ? 'Search triggers' : 'Search actions', value: picker.query,
      oninput: function () { picker.query = search.value; fill(); } });
    pickerEl.appendChild(h('div', { class: 'picker-head' },
      h('div', null, h('h2', { text: trig ? 'What starts this workflow?' : 'What happens next?' }),
        h('button', { type: 'button', class: 'x', 'aria-label': 'Close', onclick: closePicker }, icon('close'))),
      h('label', { class: 'search' }, icon('search'), search)));
    pickerEl.appendChild(list);

    function direct(def) {
      return h('button', { type: 'button', class: 'pick', onclick: function () { pick(def); } }, tile(def.tile),
        h('span', null, h('b', { text: def.title }), h('small', { text: def.kind && def.connection ? def.kind + ' · ' + def.desc : def.desc })));
    }
    function fill() {
      list.textContent = '';
      var q = picker.query.trim().toLowerCase();
      if (q) {
        var all = side.basics.slice();
        side.buildings.forEach(function (b) { all = all.concat(b.items); });
        var hits = all.filter(function (d) { return (d.title + ' ' + d.desc + ' ' + d.kind + ' ' + d.key).toLowerCase().indexOf(q) >= 0; });
        list.appendChild(h('h3', { class: 'eyebrow', text: hits.length ? 'Results' : 'Nothing matches' }));
        hits.forEach(function (d) { list.appendChild(direct(d)); });
        return;
      }
      list.appendChild(h('h3', { class: 'eyebrow', text: trig ? 'Basics' : 'Logic and web' }));
      side.basics.forEach(function (d) { list.appendChild(direct(d)); });
      list.appendChild(h('h3', { class: 'eyebrow', text: 'Hamlet buildings' }));
      side.buildings.slice().sort(function (a, b) { return (b.items.length ? 1 : 0) - (a.items.length ? 1 : 0); }).forEach(function (b) {
        var open = picker.open === b.connection;
        var group = h('div', { class: 'pick-group' + (open ? ' open' : '') },
          h('button', { type: 'button', class: 'pick', 'aria-expanded': open ? 'true' : 'false',
            onclick: function () { picker.open = open ? null : b.connection; fill(); } }, tile(b.tile),
            h('span', null, h('b', { text: b.instance ? b.name + ' · ' + b.instance : b.name }), h('small', { class: b.broken ? 'warn' : '', text: b.sub })),
            icon(open ? 'expand_less' : 'expand_more')));
        if (open) {
          group.appendChild(h('div', { class: 'pick-events' }, b.items.length ? b.items.map(function (d) {
            return h('button', { type: 'button', title: d.desc, onclick: function () { pick(d); } },
              icon(trig ? 'bolt' : (d.lookup ? 'search' : 'play_arrow')), d.title);
          }) : h('span', { class: 'hint', text: trig ? 'This building sends no events.' : 'This building offers no actions.' })));
        }
        list.appendChild(group);
      });
      list.appendChild(h('a', { class: 'pick', href: data.buildingsUrl, style: 'text-decoration: none' },
        h('span', { class: 'tile ghost' }, icon('add')),
        h('span', null, h('b', { text: 'Connect another building' }),
          h('small', { class: side.buildings.length ? '' : 'warn', text: side.buildings.length ? 'Bring more events and actions in' : 'Not connected yet' }))));
    }
    fill();
  }

  // ---------- editor ----------
  function openEditor(id) { closePicker(); editing = id; activeSlot = null; savedRange = null; renderFlow(); renderEditor(); }
  function closeEditor() { if (!editing) return; editing = null; veil.hidden = true; veil.textContent = ''; renderFlow(); }
  veil.addEventListener('mousedown', function (event) { if (event.target === veil) closeEditor(); });
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (editing) closeEditor(); else if (picker) closePicker();
  });


  function tokenChip(src, key) {
    var chip = h('span', { class: 'token', contenteditable: 'false', 'data-token': '{{' + src + '.' + key + '}}' });
    if (src === 'secret') { chip.classList.add('secret'); chip.appendChild(icon('key')); chip.appendChild(document.createTextNode(key)); return chip; }
    var from = stepByUid(src);
    if (!from) { chip.classList.add('lost'); chip.appendChild(icon('link_off')); chip.title = 'The step this came from was removed'; }
    else { chip.appendChild(tile(defOf(from).tile, 'xs')); chip.title = 'From “' + defOf(from).title + '”'; }
    chip.appendChild(document.createTextNode(key));
    return chip;
  }
  function fillSlot(slot, text) {
    slot.textContent = '';
    var last = 0, match; TOKEN.lastIndex = 0;
    while ((match = TOKEN.exec(text))) {
      if (match.index > last) slot.appendChild(document.createTextNode(text.slice(last, match.index)));
      slot.appendChild(tokenChip(match[1], match[2]));
      last = TOKEN.lastIndex;
    }
    if (last < text.length) slot.appendChild(document.createTextNode(text.slice(last)));
  }
  function readSlot(slot) {
    var out = '';
    (function walk(node) {
      Array.prototype.forEach.call(node.childNodes, function (child) {
        if (child.nodeType === 3) out += child.nodeValue;
        else if (child.nodeType === 1) {
          if (child.hasAttribute('data-token')) out += child.getAttribute('data-token');
          else if (child.tagName === 'BR') out += '\n';
          else { if (/^(DIV|P)$/.test(child.tagName) && out && out.slice(-1) !== '\n') out += '\n'; walk(child); }
        }
      });
    })(slot);
    return out.replace(/ /g, ' ');
  }
  function makeSlot(text, opts) {
    var slot = h('div', {
      class: 'slot' + (opts.multi ? ' multi' : ''), contenteditable: 'true', role: 'textbox', tabindex: '0',
      'aria-label': opts.label, 'aria-multiline': opts.multi ? 'true' : 'false', spellcheck: 'false',
      'data-placeholder': opts.placeholder || ''
    });
    fillSlot(slot, String(text == null ? '' : text));
    var commit = function () { opts.onchange(readSlot(slot)); touch(); };
    slot.addEventListener('input', commit);
    slot.addEventListener('focus', function () { activeSlot = slot; });
    slot.addEventListener('blur', function () {
      var sel = window.getSelection();
      if (sel.rangeCount && slot.contains(sel.anchorNode)) savedRange = sel.getRangeAt(0).cloneRange();
    });
    slot.addEventListener('keydown', function (event) { if (event.key === 'Enter' && !opts.multi) event.preventDefault(); });
    slot.addEventListener('paste', function (event) {
      event.preventDefault();
      var text = (event.clipboardData || window.clipboardData).getData('text');
      if (!opts.multi) text = text.replace(/[\r\n]+/g, ' ');
      insertAtCaret(slot, document.createTextNode(text)); commit();
    });
    slot.addEventListener('dragover', function (event) { event.preventDefault(); slot.classList.add('over'); });
    slot.addEventListener('dragleave', function () { slot.classList.remove('over'); });
    slot.addEventListener('drop', function (event) {
      event.preventDefault(); slot.classList.remove('over');
      var raw = event.dataTransfer.getData('application/x-switchboard-token');
      if (!raw) return;
      var range = null;
      if (document.caretRangeFromPoint) range = document.caretRangeFromPoint(event.clientX, event.clientY);
      else if (document.caretPositionFromPoint) {
        var pos = document.caretPositionFromPoint(event.clientX, event.clientY);
        if (pos) { range = document.createRange(); range.setStart(pos.offsetNode, pos.offset); }
      }
      if (range && slot.contains(range.startContainer) && !(range.startContainer.parentNode.closest && range.startContainer.parentNode.closest('.token'))) {
        var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
      } else placeCaretAtEnd(slot);
      var parts = JSON.parse(raw);
      insertAtCaret(slot, tokenChip(parts[0], parts[1])); activeSlot = slot; commit();
    });
    slot.commit = commit;
    return slot;
  }
  function placeCaretAtEnd(slot) {
    var range = document.createRange(); range.selectNodeContents(slot); range.collapse(false);
    var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  }
  function insertAtCaret(slot, node) {
    var sel = window.getSelection();
    if (!sel.rangeCount || !slot.contains(sel.anchorNode)) placeCaretAtEnd(slot);
    var range = sel.getRangeAt(0);
    range.deleteContents(); range.insertNode(node);
    range.setStartAfter(node); range.collapse(true);
    sel.removeAllRanges(); sel.addRange(range);
  }
  function insertToken(src, key) {
    var slot = activeSlot && document.body.contains(activeSlot) ? activeSlot : veil.querySelector('.slot');
    if (!slot) { toast('This step has no settings to fill'); return; }
    slot.focus();
    if (savedRange && slot.contains(savedRange.startContainer)) { var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(savedRange); }
    else placeCaretAtEnd(slot);
    insertAtCaret(slot, tokenChip(src, key));
    savedRange = window.getSelection().getRangeAt(0).cloneRange();
    slot.commit();
  }

  function fieldRow(src, key, value) {
    var shown = show(value);
    return h('button', {
      type: 'button', class: 'fieldrow', draggable: 'true',
      ondragstart: function (event) {
        event.dataTransfer.setData('application/x-switchboard-token', JSON.stringify([src, key]));
        event.dataTransfer.setData('text/plain', '{{' + src + '.' + key + '}}');
        event.dataTransfer.effectAllowed = 'copy';
      },
      onclick: function () { insertToken(src, key); }
    }, icon('drag_indicator'), h('span', { class: 'key', text: key }),
      value === undefined ? null : h('span', { class: 'val ' + shown[1], text: shown[0] }));
  }

  function renderEditor() {
    var step = stepByUid(editing);
    if (!step) { closeEditor(); return; }
    var def = defOf(step);
    // `index` only tells the first step (0) from the rest, as the panes below expect.
    var index = step === wf.steps[0] ? 0 : 1, isTrigger = index === 0 && def.trigger;
    var path = before(step.uid);
    step.params = step.params || {};
    veil.hidden = false; veil.textContent = '';

    // ----- input pane
    var inPane = h('section', { class: 'pane in', 'aria-label': 'Input' },
      h('div', { class: 'pane-head' }, icon('input'), h('h3', { text: 'Input' })));
    if (index === 0) {
      inPane.appendChild(h('p', { class: 'big', text: 'This is the first step, so nothing comes in. Its output is what the next steps can use.' }));
    } else {
      inPane.appendChild(h('p', null, icon('drag_pan'), 'Drag a field into a setting, or click it to add it where your cursor is'));
      var groups = h('div', { class: 'groups' });
      if (!path.length) groups.appendChild(h('p', { class: 'hint', style: 'font-size: 14px; padding: 4px 6px', text: 'Nothing leads into this step yet. Connect a wire to it and the fields that flow in will show up here.' }));
      path.forEach(function (prev) {
        var pdef = defOf(prev), info = fieldsOf(prev);
        groups.appendChild(h('div', { class: 'group' },
          h('div', { class: 'group-head' }, tile(pdef.tile, 's'), h('span', { text: pdef.title }), info.real ? null : h('small', { text: 'sample data' })),
          info.fields.length ? info.fields.map(function (f) { return fieldRow(prev.uid, f[0], f[1]); })
            : h('p', { class: 'hint', style: 'padding: 10px 12px', text: 'Test that step to see its fields.' })));
      });
      if (step.node === 'http' && data.secrets.length) {
        groups.appendChild(h('div', { class: 'group' },
          h('div', { class: 'group-head' }, h('span', { class: 'tile s' }, icon('key')), h('span', { text: 'Secrets' }), h('small', { text: 'from Settings' })),
          data.secrets.map(function (name) { return fieldRow('secret', name); })));
      }
      inPane.appendChild(groups);
    }

    // ----- settings pane
    var mid = h('section', { class: 'pane mid', 'aria-label': 'Settings' },
      h('div', { class: 'pane-head' }, icon('tune'), h('h3', { text: 'Settings' })),
      h('p', { class: 'about' + (def.missing ? ' warn' : ''), text: def.desc || def.title }));
    var params = h('div', { class: 'params' });
    if (step.node === 'webhook') {
      params.appendChild(h('div', { class: 'param' }, h('span', { class: 'param-label', text: 'Private URL' }),
        h('div', { class: 'hook' }, h('code', { text: data.hookUrl }),
          h('button', { type: 'button', class: 'btn small', onclick: function () {
            (navigator.clipboard ? navigator.clipboard.writeText(data.hookUrl) : Promise.reject()).then(function () { toast('Copied'); }, function () { toast('Select the address and copy it by hand', true); });
          } }, icon('content_copy'), 'Copy')),
        h('span', { class: 'help', text: 'POST JSON here to start this workflow. Anyone with this address can start it, so keep it private. What you send shows up as body.… fields; test with a real call to see them.' })));
    }
    var shown = def.params.filter(function (p) { return visible(step, def, p); });
    if (!shown.length && step.node !== 'webhook') {
      params.appendChild(h('p', { class: 'hint', style: 'font-size: 14px', text: isTrigger
        ? 'Nothing to set up here. Press Test to see what this trigger sends to the next step.'
        : 'Nothing to set up here. Press Test to see what this step sends to the next one.' }));
    }
    shown.forEach(function (p) { params.appendChild(paramControl(step, def, p, index)); });
    if (step.node === 'switch') params.appendChild(casesControl(step));
    if (step.node === 'if') {
      params.appendChild(h('span', { class: 'help', text: 'On the canvas, wire the True output and the False output to what should happen each way.' }));
    }
    if (step.node === 'action' || step.node === 'http') {
      params.appendChild(h('span', { class: 'help', text: 'If this step fails with a network error or a server error it is tried 3 times, then the run stops and is marked failed. You can retry it from Runs.' }));
    }
    mid.appendChild(params);

    // ----- output pane
    var out = h('section', { class: 'pane out', 'aria-label': 'Output' });
    renderOutput(out, step, def, index);

    var testOne = h('button', { type: 'button', class: 'btn primary', onclick: function () { testStep(step); } }, icon('play_arrow'), 'Test this step');
    veil.appendChild(h('section', { class: 'editor', role: 'dialog', 'aria-label': def.title },
      h('header', null, tile(def.tile, 'l'),
        h('div', { class: 'titles' },
          h('span', { text: (isTrigger ? 'Trigger · ' : '') + def.kind }),
          h('h2', { text: def.title })),
        isTrigger ? h('button', { type: 'button', class: 'btn', onclick: function () { openPicker('trigger'); } }, icon('swap_horiz'), 'Change trigger') : null,
        h('button', { type: 'button', class: 'btn danger', onclick: function () { removeStep(step); } }, icon('delete'), 'Remove step'),
        def.missing ? null : testOne,
        h('button', { type: 'button', class: 'x', 'aria-label': 'Close', onclick: closeEditor }, icon('close'))),
      h('div', { class: 'panes' }, inPane, mid, out)));
  }

  // A Switch's cases: each is a comparison with the field above, and gets its own branch.
  function casesControl(step) {
    var rerender = function () { touch(); renderEditor(); renderFlow(); };
    var box = h('div', { class: 'table' });
    var cases = step.cases = step.cases || [newCase()];
    cases.forEach(function (branch, i) {
      var op = h('select', { 'aria-label': 'Comparison', onchange: function () { branch.op = op.value; rerender(); } },
        data.mapOps.map(function (o) { return h('option', { value: o.value, text: o.label, selected: o.value === branch.op }); }));
      var first = h('input', { type: 'text', value: branch.value || '', 'aria-label': 'Value', placeholder: 'value',
        oninput: function () { branch.value = first.value; touch(); }, onchange: function () { renderFlow(); } });
      var second = branch.op === 'between' ? h('input', { type: 'text', value: branch.value2 || '', 'aria-label': 'Upper value',
        oninput: function () { branch.value2 = second.value; touch(); }, onchange: function () { renderFlow(); } }) : null;
      var name = h('input', { type: 'text', class: 'name', value: branch.label || '', 'aria-label': 'Name for this case', placeholder: 'Name (optional)',
        maxlength: '60', oninput: function () { branch.label = name.value; touch(); }, onchange: function () { renderFlow(); } });
      box.appendChild(h('div', { class: 'rule' }, 'Case ' + (i + 1) + ': it', op, first, second ? 'and' : null, second, name,
        h('button', { type: 'button', class: 'x', 'aria-label': 'Remove case ' + (i + 1), disabled: cases.length < 2, onclick: function () {
          cases.splice(cases.indexOf(branch), 1);
          if (step.next) delete step.next[branch.id];
          rerender();
        } }, icon('close'))));
    });
    box.appendChild(h('div', { class: 'table-foot rule' },
      h('button', { type: 'button', class: 'btn small', disabled: cases.length >= 12, onclick: function () {
        cases.push(newCase()); rerender();
      } }, icon('add'), 'Add a case'),
      h('span', { class: 'grow' }), h('span', { class: 'help', text: 'Each case is an output on the canvas. The first that matches wins; none matching goes out of Otherwise.' })));
    return h('div', { class: 'param' }, h('div', { class: 'param-label' }, h('span', { class: 'name', text: 'Cases' })), box);
  }

  function paramControl(step, def, param, index) {
    var value = paramValue(step, param);
    var wrap = h('div', { class: 'param' });
    var label = h('div', { class: 'param-label' }, h('span', { class: 'name' }, param.label, param.required ? null : h('em', { text: '  optional' })));
    var set = function (v) { step.params[param.key] = v; };
    var rerender = function () { touch(); renderEditor(); renderFlow(); };

    if (param.type === 'choice') {
      var select = h('select', { 'aria-label': param.label, onchange: function () { set(select.value); rerender(); } },
        param.required || param['default'] !== undefined ? null : h('option', { value: '', text: 'Not set' }),
        (param.choices || []).map(function (c) { return h('option', { value: c.value, text: c.label, selected: String(c.value) === String(value) }); }));
      wrap.appendChild(label); wrap.appendChild(select);
    } else if (param.type === 'bool') {
      var on = String(value) === 'true';
      wrap.appendChild(h('button', { type: 'button', class: 'toggle', role: 'switch', 'aria-checked': on ? 'true' : 'false',
        onclick: function () { set(on ? 'false' : 'true'); rerender(); } }, h('i'), param.label));
    } else if (param.type === 'time') {
      var time = h('input', { type: 'time', value: value || '08:00', 'aria-label': param.label,
        onchange: function () { set(time.value); touch(); renderFlow(); } });
      wrap.appendChild(label); wrap.appendChild(time);
    } else {
      // Lookup tables are from an older version (a Switch does that job now); one that is
      // already saved still shows, with a way out.
      var isMap = value && typeof value === 'object' && value.map;
      wrap.appendChild(label);
      if (isMap) {
        wrap.appendChild(mapTable(value.map, param, rerender));
        wrap.appendChild(h('button', { type: 'button', class: 'btn small', style: 'align-self: flex-start',
          onclick: function () { set(value.map['else'] || ''); rerender(); } }, 'Use one plain value instead'));
      } else {
        wrap.appendChild(makeSlot(value, {
          label: param.label, multi: param.type === 'text',
          placeholder: param.placeholder || (index > 0 ? 'Type, or drop a field here' : 'Type a value'),
          onchange: function (text) { set(text); }
        }));
      }
    }
    if (param.help) wrap.appendChild(h('span', { class: 'help', text: param.help }));
    return wrap;
  }

  function mapTable(spec, param, rerender) {
    var box = h('div', { class: 'table' });
    box.appendChild(h('div', { class: 'table-on' }, 'Based on',
      makeSlot(spec.on, { label: 'Field ' + param.label + ' depends on', placeholder: 'Drop a field here, like level',
        onchange: function (text) { spec.on = text; } })));
    (spec.rules || []).forEach(function (rule, i) {
      var op = h('select', { 'aria-label': 'Comparison', onchange: function () { rule.op = op.value; rerender(); } },
        data.mapOps.map(function (o) { return h('option', { value: o.value, text: o.label, selected: o.value === rule.op }); }));
      var first = h('input', { type: 'text', value: rule.value || '', 'aria-label': 'Value', oninput: function () { rule.value = first.value; touch(); } });
      var second = rule.op === 'between' ? h('input', { type: 'text', value: rule.value2 || '', 'aria-label': 'Upper value',
        oninput: function () { rule.value2 = second.value; touch(); } }) : null;
      var then = h('input', { type: 'text', class: 'then', value: rule.then || '', 'aria-label': param.label + ' for this rule',
        oninput: function () { rule.then = then.value; touch(); } });
      box.appendChild(h('div', { class: 'rule' }, 'When it', op, first, second ? 'and' : null, second, h('span', { class: 'grow' }),
        icon('arrow_forward'), then,
        h('button', { type: 'button', class: 'x', 'aria-label': 'Remove this rule', onclick: function () { spec.rules.splice(i, 1); rerender(); } }, icon('close'))));
    });
    var otherwise = h('input', { type: 'text', class: 'then', value: spec['else'] || '', 'aria-label': 'Otherwise',
      oninput: function () { spec['else'] = otherwise.value; touch(); } });
    box.appendChild(h('div', { class: 'table-foot rule' },
      h('button', { type: 'button', class: 'btn small', onclick: function () { spec.rules.push({ op: 'eq', value: '', value2: '', then: '' }); rerender(); } }, icon('add'), 'Add a rule'),
      h('span', { class: 'grow' }), 'Otherwise', icon('arrow_forward'), otherwise));
    return box;
  }

  function renderOutput(out, step, def, index) {
    out.textContent = '';
    var result = results[step.uid], sample = samples[step.uid];
    var head = h('div', { class: 'pane-head' }, icon('output'), h('h3', { text: 'Output' }));
    out.appendChild(head);
    if (result && result.status === 'running') {
      out.appendChild(h('div', { class: 'out-wait' }, icon('progress_activity', 'spin'), 'Running this step…'));
      return;
    }
    if (result && result.status === 'failed') {
      head.appendChild(h('span', { class: 'pill bad' }, icon('error', 'fill'), "Didn't work"));
      out.appendChild(h('div', { class: 'out-err', text: result.error || 'This step failed.' }));
      return;
    }
    var body = result && result.output ? result.output : (sample && typeof sample === 'object' ? sample : null);
    if (!body) {
      out.appendChild(h('div', { class: 'out-empty' }, icon('science'),
        index === 0 ? 'Test this trigger to see what it sends to the next step.' : 'Test this step to see what it sends to the next one.',
        def.missing ? null : h('button', { type: 'button', class: 'btn', onclick: function () { testStep(step); } }, 'Test this step')));
      return;
    }
    if (result && result.took) head.appendChild(h('span', { class: 'pill' }, icon('call_split'), 'Took ' + result.took));
    else if (result && result.stopped) head.appendChild(h('span', { class: 'pill stop' }, icon('do_not_disturb_on', 'fill'), "Check didn't pass"));
    else if (result) head.appendChild(h('span', { class: 'pill' }, icon('check_circle', 'fill'), 'Worked' + (result.ms != null ? ' · ' + result.ms + ' ms' : '')));
    out.appendChild(h('p', { class: 'out-note', text: result
      ? (index === 0 && result.real === false ? 'Sample data. Real data shows here once this trigger has fired.' : 'From the test you just ran')
      : 'From your last test' }));
    var rows = flatten(body), json = h('div', { class: 'json' }, h('div', { text: '{' }));
    rows.forEach(function (pair, i) {
      var shown = show(pair[1]);
      json.appendChild(h('div', { class: 'row' }, h('span', { class: 'k', text: '"' + pair[0] + '"' }), ': ',
        h('span', { class: shown[1], text: shown[0] }), i < rows.length - 1 ? ',' : ''));
    });
    json.appendChild(h('div', { text: '}' }));
    out.appendChild(json);
    out.appendChild(h('div', { class: 'out-tip' }, icon('lightbulb'), result && result.stopped
      ? 'In a real run the workflow would stop here, and the run is logged as Stopped.'
      : 'The next step can use any of these fields.'));
  }

  function testStep(step) {
    var def = defOf(step);
    var real = step.node === 'http' || (step.node === 'action' && !def.lookup);
    if (real && !window.confirm('This really does it, using the sample data on the left. Go ahead?')) return;
    results[step.uid] = { status: 'running' };
    renderEditor(); renderFlow();
    post(data.urls.test_step, { steps: wf.steps, uid: step.uid }).then(function (answer) {
      if (answer.ok) {
        results[step.uid] = { status: answer.stopped ? 'stopped' : 'ok', output: answer.output, ms: answer.ms, real: answer.real, stopped: answer.stopped, took: answer.took };
        samples[step.uid] = answer.output;
      } else results[step.uid] = { status: 'failed', error: answer.error || 'This step failed.' };
      if (editing === step.uid) renderEditor();
      renderFlow();
    });
  }

  function removeStep(step) {
    var first = step === wf.steps[0];
    if (first && wf.steps.length > 1 && !window.confirm('Remove the trigger? The other steps stay, but the workflow can’t run until you add a new one.')) return;
    // Whatever led into this step now leads to what came after it, so the line stays whole.
    var onward = targets(step)[0];
    wf.steps.forEach(function (s) {
      ports(s).forEach(function (p) {
        if ((s.next || {})[p] !== step.uid) return;
        if (onward && onward !== s.uid && !first) s.next[p] = onward; else delete s.next[p];
      });
    });
    wf.steps.splice(wf.steps.indexOf(step), 1);
    delete results[step.uid]; delete samples[step.uid];
    editing = null; veil.hidden = true; veil.textContent = '';
    touch(); renderFlow(); toast('Step removed');
    if (!wf.steps.length) openPicker('trigger');
    else if (!hasTrigger()) toast('Add a trigger: the workflow needs one to start.', true);
  }

  centre(); renderHead(); renderFlow();
  if (data.openPicker) openPicker('trigger');
})();
