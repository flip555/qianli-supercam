#!/usr/bin/env node
/* Load the GUI's inline script in a stub DOM and report what happens.
 *
 * Usage: node tools/smoke.js web/index.html
 *
 * Catches the class of bug that a browser would show as "half the page is dead":
 * a load-time exception (a const used before its declaration, a missing element)
 * stops every line after it, so handlers silently never get attached.
 *
 * Elements are stubbed per id, so after loading we can assert that the controls
 * that matter have something listening. It does not execute the handlers.
 */
const fs = require('fs');
const file = process.argv[2] || 'web/index.html';
const body = fs.readFileSync(file, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

const style = () => new Proxy({}, { get: (t, k) => (k === 'setProperty' ? () => {} : ''), set: () => true });
const els = new Map();
const mkEl = (id) => new Proxy({}, {
  get(t, k) {
    switch (k) {
      case 'style': return style();
      case 'classList': return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
      case 'dataset': return t.dataset || (t.dataset = {});
      case 'getBoundingClientRect': return () => ({ width: 800, height: 450, left: 0, top: 0 });
      case 'getContext': return () => mkEl(id + ':ctx');
      case 'addEventListener': return () => {};
      case 'removeEventListener': return () => {};
      case 'setAttribute': case 'removeAttribute': case 'click': case 'focus':
      case 'appendChild': case 'removeChild': return () => {};
      case 'querySelectorAll': return () => [];
      case 'getAttribute': return () => null;
      case 'naturalWidth': return 1280;
      case 'naturalHeight': return 720;
      case 'width': case 'height': return 1280;
      default: return (k in t) ? t[k] : '';
    }
  },
  set(t, k, v) { t[k] = (k === 'value' ? String(v) : v); return true; }   // DOM coerces value to a string
});
const getEl = (id) => { if (!els.has(id)) els.set(id, mkEl(id)); return els.get(id); };

global.document = { getElementById: getEl, querySelectorAll: () => [], createElement: () => mkEl('new'),
                    documentElement: mkEl('html'), body: mkEl('body'), addEventListener: () => {}, hidden: false };
global.window = global;
global.navigator = { language: 'en-GB' };
global.location = { search: '', href: 'http://localhost/', reload: () => {} };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.fetch = () => Promise.resolve({ ok: true, json: async () => ({}), body: null, text: async () => '' });
global.setInterval = () => 0; global.clearInterval = () => {}; global.setTimeout = () => 0; global.clearTimeout = () => {};
global.addEventListener = () => {};
global.ResizeObserver = class { observe() {} };
global.getComputedStyle = () => ({ getPropertyValue: () => '' });
global.confirm = () => true; global.alert = () => {};
global.URL = { createObjectURL: () => '' }; global.Image = class {};

let failed = false;
try { new Function(body)(); console.log('load: OK (script ran to completion)'); }
catch (e) { console.log('load: THREW ' + e.constructor.name + ': ' + e.message); failed = true; }

const wired = { fliph: 'onclick', flipv: 'onclick', rot90: 'onclick', nup: 'onclick', ndown: 'onclick',
                nleft: 'onclick', nright: 'onclick', calreset: 'onclick', calfactory: 'onclick',
                calsave: 'onclick', snap: 'onclick', cog: 'onclick', size: 'onclick',
                clipauto: 'onclick', spanauto: 'onclick', wipe: 'oninput', fuse: 'oninput',
                ts: 'oninput', pal: 'onchange', clipLo: 'onchange', spanLo: 'onchange' };
const missing = Object.entries(wired).filter(([id, ev]) => typeof getEl(id)[ev] !== 'function').map(([id]) => id);
if (missing.length) { console.log('not wired: ' + missing.join(', ')); failed = true; }
else { console.log('controls wired: ' + Object.keys(wired).length + '/' + Object.keys(wired).length); }
process.exitCode = failed ? 1 : 0;

/* --- simulate a slider drag, to check the wiring actually does something --- */
(function(){
  const lo = getEl('clipLoR'), hi = getEl('clipHiR');
  if (typeof lo.oninput !== 'function' || typeof hi.oninput !== 'function') {
    console.log('slider: no oninput handler'); failed = true; return;
  }
  lo.value = '30'; hi.value = '150';
  let sent = null;
  global.fetch = (u) => { sent = String(u); return Promise.resolve({ ok: true, json: async () => ({}), body: null, text: async () => '' }); };
  try { lo.oninput(); } catch (e) { console.log('slider: oninput threw ' + e.message); failed = true; return; }
  const box = getEl('clipLo').value;
  console.log('slider: box after drag = ' + JSON.stringify(box) + '  request = ' + sent);
  if (box !== 30 && box !== '30') { console.log('slider: the number box did not follow the slider'); failed = true; }
  if (!sent || !/[?&]clip_lo=30/.test(sent)) { console.log('slider: request used the wrong parameter name: ' + sent); failed = true; }
  /* and the same for the second band, whose id is spanLo/spanHi */
  const slo = getEl('spanLoR'), shi = getEl('spanHiR');
  slo.value = '20'; shi.value = '22.5';
  sent = null;
  try { slo.oninput(); } catch (e) { console.log('slider: span oninput threw ' + e.message); failed = true; }
  if (!sent || !/[?&]span_lo=20/.test(sent)) { console.log('slider: span request used the wrong parameter name: ' + sent); failed = true; }
  else console.log('slider: both bands send the names the relay accepts');
  process.exitCode = failed ? 1 : 0;
})();
