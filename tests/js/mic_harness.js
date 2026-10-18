// Runs frontend/static/mic.js under node with a stand-in page, microphone, MediaRecorder, fetch and
// Audio, and checks the states it shows. Usage: node mic_harness.js <path to mic.js>. Prints one JSON
// line of results; exits non-zero on the first failed check (node:assert).
"use strict";
const assert = require("node:assert/strict");
const mic = require(process.argv[2]);

const tick = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };
let clock = 1000;
Date.now = () => clock;

function node(cls) {
  const classes = new Set(cls ? [cls] : []);
  return {
    hidden: true, textContent: "", attrs: {}, listeners: {}, children: [], removed: false, sel: {},
    classList: { toggle(c, on) { if (on) classes.add(c); else classes.delete(c); }, has: (c) => classes.has(c) },
    get className() { return [...classes].join(" "); },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    fire(t, extra) { const e = Object.assign({ type: t, button: 0, preventDefault() { e.prevented = true; } }, extra);
      (this.listeners[t] || []).forEach((f) => f(e)); return e; },
    querySelector(s) { return this.sel[s] || null; },
    remove() { this.removed = true; },
  };
}

function page() {
  const panel = node("chat"), btn = node("chat-mic"), status = node("chat-voice"), log = node("chat__log");
  const words = node(), wave = node(), dots = node();
  status.sel = { ".chat-voice__text": words, ".chat-voice__wave": wave, ".chat-voice__dots": dots };
  panel.sel = { ".chat-mic": btn, ".chat-voice": status, ".chat__log": log };
  log.lastElementChild = null;
  log.insertAdjacentHTML = (where, html) => {
    const ex = node("chat-exchange"); ex.html = html; ex.sel[".chat-exchange__how"] = node("chat-exchange__how");
    log.children.push(ex); log.lastElementChild = ex;
  };
  return { panel, btn, status, log, words, wave, dots };
}

function browser(opts = {}) {
  const w = { isSecureContext: opts.secure !== false, calls: [], timers: [], processed: 0, audios: [] };
  const supportedTypes = opts.types || ["audio/webm;codecs=opus", "audio/mp4"];
  const tracks = opts.tracks || 1;  // a stream's tracks, each counting its stop() into stream.stopped
  const stream = { stopped: 0, getTracks: () => Array.from({ length: tracks }, () => ({ stop() { stream.stopped++; } })) };
  w.stream = stream;
  if (opts.recorder !== false) {
    w.MediaRecorder = class {
      static isTypeSupported(t) { return supportedTypes.includes(t); }
      constructor(s, o) {
        if (opts.ctorThrows) throw new Error("NotSupportedError");
        this.mimeType = (o && o.mimeType) || ""; this.state = "inactive"; w.recorder = this;
        w.recorders = (w.recorders || 0) + 1; }
      start() { if (opts.startThrows) throw new Error("InvalidStateError"); this.state = "recording"; }
      stop() { this.state = "inactive"; this.ondataavailable({ data: new Blob(["sound"]) }); this.onstop(); }
    };
  }
  w.pending = [];  // with opts.deferred: the open permission prompts, answered by the test
  const media = opts.denied ? () => Promise.reject(new Error("NotAllowedError"))
    : opts.deferred ? () => new Promise((resolve) => { w.pending.push(resolve); })
    : () => Promise.resolve(stream);
  w.navigator = opts.media === false ? {} : { mediaDevices: { getUserMedia: media } };
  w.Blob = Blob; w.FormData = FormData;
  w.setTimeout = (f, ms) => { w.timers.push([f, ms]); return w.timers.length; };
  w.clearTimeout = () => {};
  w.htmx = { process() { w.processed++; } };
  w.reply = opts.reply || { ok: true, body: { html: "<article>…</article>",
    audio: { content_type: "audio/wav", base64: "UklGRg==" }, voice_note: "" } };
  w.fetch = (url, init) => {
    w.calls.push({ url, init });
    if (w.reply === "network") return Promise.reject(new TypeError("offline"));
    const r = w.reply;
    return Promise.resolve({ ok: r.ok, status: r.status || (r.ok ? 200 : 500),
      json: () => (r.body === "not json" ? Promise.reject(new SyntaxError("not json")) : Promise.resolve(r.body)) });
  };
  w.Audio = class {
    constructor(src) { this.src = src; this.paused = true; w.audios.push(this); }
    play() { if (opts.blockPlay) return Promise.reject(new Error("NotAllowedError")); this.paused = false;
      return Promise.resolve(); }
    pause() { this.paused = true; }
  };
  return w;
}

function state(p) {
  const mods = ["listening", "thinking", "speaking", "error"].filter((m) => p.btn.classList.has("chat-mic--" + m));
  return mods.length ? mods[0] : "idle";
}

const results = {};

(async () => {
  // Hidden where the browser cannot record: not a secure page, no MediaRecorder, no microphone.
  for (const [name, opts] of [["insecure", { secure: false }], ["no_recorder", { recorder: false }],
    ["no_microphone", { media: false }]]) {
    const p = page();
    assert.equal(mic.attach(browser(opts), p.panel), null, name);
    assert.equal(p.btn.hidden, true, name); assert.equal(p.status.hidden, true, name);
    assert.equal(p.btn.listeners.pointerdown, undefined, name);
    results[name] = "hidden";
  }

  // Press and hold: listening while held, thinking after letting go, speaking, then ready.
  {
    const p = page(), w = browser();
    mic.attach(w, p.panel);
    assert.equal(p.btn.hidden, false); assert.equal(p.status.hidden, false);
    assert.equal(state(p), "idle"); assert.match(p.words.textContent, /Hold the microphone/);
    p.btn.fire("pointerdown"); await tick();
    assert.equal(state(p), "listening"); assert.equal(p.btn.attrs["aria-pressed"], "true");
    assert.equal(p.wave.hidden, false); assert.match(p.words.textContent, /Listening/);
    assert.equal(w.recorder.mimeType, "audio/webm;codecs=opus");
    assert.deepEqual(w.timers.map((t) => t[1]), [30000]);
    clock += 1500; p.btn.fire("pointerup"); await tick();
    assert.equal(w.calls.length, 1);
    assert.equal(w.calls[0].url, "/api/voice/ask"); assert.equal(w.calls[0].init.method, "POST");
    const sent = w.calls[0].init.body.get("audio");
    assert.equal(sent.name, "question.webm"); assert.equal(sent.type, "audio/webm");
    assert.equal(w.stream.stopped, 1);
    assert.equal(p.log.children.length, 1); assert.equal(w.processed, 1);
    assert.equal(state(p), "speaking"); assert.match(p.words.textContent, /Reading the answer aloud/);
    assert.equal(w.audios[0].src, "data:audio/wav;base64,UklGRg==");
    assert.equal(p.log.children[0].sel[".chat-exchange__how"].textContent, "Read aloud");
    w.audios[0].onended(); assert.equal(state(p), "idle");
    results.hold = "listening, thinking, speaking, idle";
  }

  // Tap to start and tap again to send; the thinking state shows the dots.
  {
    const p = page(), w = browser();
    let release;
    w.fetch = (url, init) => { w.calls.push({ url, init }); return new Promise((r) => { release = r; }); };
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick(); clock += 100; p.btn.fire("pointerup"); await tick();
    assert.equal(state(p), "listening");               // a short tap keeps listening
    clock += 3000; p.btn.fire("pointerdown"); await tick();
    assert.equal(state(p), "thinking"); assert.equal(p.dots.hidden, false); assert.equal(p.wave.hidden, true);
    p.btn.fire("pointerup"); await tick();
    assert.equal(state(p), "thinking"); assert.equal(w.calls.length, 1);
    p.btn.fire("pointerdown"); await tick(); assert.equal(w.calls.length, 1); // ignored while thinking
    release({ ok: true, json: () => Promise.resolve({ html: "<article></article>", audio: null, voice_note:
      "Voice isn't available right now, so the answer is in text only." }) });
    await tick();
    assert.equal(state(p), "idle"); assert.match(p.words.textContent, /answer is in text only/);
    results.tap = "listening, thinking, idle with the voice note";
  }

  // The keyboard: Space held down, then let go.
  {
    const p = page(), w = browser();
    mic.attach(w, p.panel);
    const down = p.btn.fire("keydown", { key: " " }); await tick();
    assert.equal(down.prevented, true); assert.equal(state(p), "listening");
    p.btn.fire("keydown", { key: " ", repeat: true }); await tick(); assert.equal(state(p), "listening");
    p.btn.fire("keydown", { key: "a" }); await tick(); assert.equal(state(p), "listening");
    clock += 1000; p.btn.fire("keyup", { key: " " }); await tick();
    assert.equal(w.calls.length, 1); assert.equal(state(p), "speaking");
    p.btn.fire("keydown", { key: "Enter" }); await tick();  // pressing while speaking stops playback
    assert.equal(w.audios[0].paused, true); assert.equal(state(p), "idle");
    results.keyboard = "space to talk, enter to stop";
  }

  // The 30-second hard stop sends the recording.
  {
    const p = page(), w = browser();
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick(); clock += 100; p.btn.fire("pointerup"); await tick();
    w.timers[0][0](); await tick();
    assert.equal(w.calls.length, 1); assert.equal(state(p), "speaking");
    results.hard_stop = "sent at 30 seconds";
  }

  // A browser that can record only Ogg (the server refuses Ogg), or no format at all: the mic stays hidden.
  for (const [types, name] of [[["audio/ogg;codecs=opus", "audio/ogg"], "ogg_only"], [[], "no_format"]]) {
    const p = page(), w = browser({ types });
    assert.equal(mic.attach(w, p.panel), null, name);
    assert.equal(p.btn.hidden, true, name); assert.equal(p.status.hidden, true, name);
    results[name] = "hidden";
  }
  // Recording formats: WebM/Opus first (above), then MP4 (Safari).
  for (const [types, name] of [[["audio/mp4", "audio/ogg;codecs=opus"], "question.mp4"]]) {
    const p = page(), w = browser({ types });
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick(); clock += 1000; p.btn.fire("pointerup"); await tick();
    assert.equal(w.calls[0].init.body.get("audio").name, name);
    results[name] = w.recorder.mimeType;
  }

  // A clarifying question waiting for a reply: the spoken reply carries it, and the typed form goes.
  {
    const p = page(), w = browser();
    const ex = node("chat-exchange"), form = node("chat-exchange__reply");
    form.sel = { "[name=prior_question]": { value: "How is the project?" },
      "[name=prior_clarify]": { value: "Which project do you mean?" } };
    ex.sel[".chat-exchange__reply"] = form; p.log.lastElementChild = ex;
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick(); clock += 1000; p.btn.fire("pointerup"); await tick();
    const body = w.calls[0].init.body;
    assert.equal(body.get("prior_question"), "How is the project?");
    assert.equal(body.get("prior_clarify"), "Which project do you mean?");
    assert.equal(form.removed, true);
    results.follow_up = "carried";
  }

  // Failures: no microphone permission; the server refuses; the network fails; playback is blocked.
  {
    const p = page(), w = browser({ denied: true });
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick();
    assert.equal(state(p), "error"); assert.match(p.words.textContent, /allow it for this site/);
    results.denied = "error";
  }
  {
    const p = page(), w = browser({ reply: { ok: false, body: { message: "Voice isn't available right now, so I couldn't hear the question. Please type it in the box instead." } } });
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick(); clock += 1000; p.btn.fire("pointerup"); await tick();
    assert.equal(state(p), "idle"); assert.match(p.words.textContent, /couldn't hear the question/);
    assert.equal(p.log.children.length, 0);
    results.refused = "idle with the server's sentence";
  }
  {
    const p = page(), w = browser({ reply: "network" });
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick(); clock += 1000; p.btn.fire("pointerup"); await tick();
    assert.equal(state(p), "idle"); assert.match(p.words.textContent, /please type your question/);
    results.network = "idle, type instead";
  }
  {
    const p = page(), w = browser({ blockPlay: true });
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick(); clock += 1000; p.btn.fire("pointerup"); await tick();
    assert.equal(state(p), "idle"); assert.match(p.words.textContent, /didn't let the answer play aloud/);
    assert.equal(p.log.children.length, 1);
    results.play_blocked = "idle, answer shown";
  }
  // Not signed in any more (the reply has no message), or a reply that isn't JSON: the error state.
  for (const [reply, words, name] of [
    [{ ok: false, status: 401, body: { detail: "login required" } }, /Please sign in again/, "signed_out"],
    [{ ok: false, status: 403, body: { detail: "operator only" } }, /Please sign in again/, "forbidden"],
    [{ ok: false, status: 502, body: "not json" }, /Something went wrong sending your question/, "bad_gateway"],
  ]) {
    const p = page(), w = browser({ reply });
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick(); clock += 1000; p.btn.fire("pointerup"); await tick();
    assert.equal(state(p), "error", name); assert.match(p.words.textContent, words, name);
    results[name] = "error";
  }
  // Two permission prompts answered together (tap start, tap stop, tap start, then Allow): only the
  // latest request records, the other microphone is closed at once, and nothing is left on.
  {
    const p = page(), w = browser({ deferred: true });
    mic.attach(w, p.panel);
    const tap = async () => { p.btn.fire("pointerdown"); clock += 100; p.btn.fire("pointerup"); clock += 1000;
      await tick(); };
    await tap(); assert.equal(state(p), "listening");
    await tap(); assert.equal(state(p), "idle");
    await tap(); assert.equal(state(p), "listening");
    const streams = [0, 1].map(() => { const s = { stopped: 0, getTracks: () => [{ stop() { s.stopped++; } }] };
      return s; });
    w.pending[0](streams[0]); w.pending[1](streams[1]); await tick();
    assert.equal(w.recorders, 1); assert.equal(streams[0].stopped, 1); assert.equal(streams[1].stopped, 0);
    assert.equal(state(p), "listening");
    p.btn.fire("pointerdown"); await tick();
    assert.equal(state(p), "speaking"); assert.equal(streams[1].stopped, 1); assert.equal(w.calls.length, 1);
    results.overlapping_prompts = "one recording, both microphones closed";
  }
  // The recorder will not be made, or will not start, after the microphone opened: every track is
  // stopped, the error state says so in one sentence, no 30-second timer is left, and it can try again.
  for (const [opt, name] of [["ctorThrows", "recorder_throws"], ["startThrows", "start_throws"]]) {
    const p = page(), w = browser({ [opt]: true, tracks: 2 });
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick();
    assert.equal(state(p), "error", name);
    assert.match(p.words.textContent, /couldn't start recording/, name);
    assert.equal(w.stream.stopped, 2, name);           // both tracks
    assert.equal(w.timers.length, 0, name);
    clock += 1000; p.btn.fire("pointerup"); await tick();
    assert.equal(state(p), "error", name);
    p.btn.fire("pointerdown"); await tick();            // trying again asks for the microphone again
    assert.equal(state(p), "error", name); assert.equal(w.stream.stopped, 4, name);
    assert.equal(w.calls.length, 0, name);
    results[name] = "error, microphone released";
  }
  // Let go before the microphone opened: nothing is recorded and the microphone is released.
  {
    const p = page(), w = browser();
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); clock += 1000; p.btn.fire("pointerup"); await tick();
    assert.equal(state(p), "idle"); assert.equal(w.recorder, undefined); assert.equal(w.stream.stopped, 1);
    results.early_release = "idle, microphone released";
  }
  // Only the main mouse button.
  {
    const p = page(), w = browser();
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown", { button: 2 }); await tick();
    assert.equal(state(p), "idle");
    results.right_click = "ignored";
  }
  // Part F, the voice note: in the suggestion box (a text box marked data-voice-fill) the recording goes to
  // /api/voice/transcribe, the words heard join the box, nothing is read aloud and no chat log is touched.
  {
    const p = page(), w = browser({ reply: { ok: true, body: { transcript: "Make the Features page shorter." } } });
    const box = { value: "Two things. ", focused: false, focus() { this.focused = true; } };
    p.panel.sel["[data-voice-fill]"] = box;
    delete p.panel.sel[".chat__log"];
    mic.attach(w, p.panel);
    assert.match(p.words.textContent, /say your suggestion/);
    p.btn.fire("pointerdown"); await tick(); clock += 1500; p.btn.fire("pointerup"); await tick();
    assert.equal(w.calls.length, 1); assert.equal(w.calls[0].url, "/api/voice/transcribe");
    assert.equal(w.calls[0].init.body.get("audio").name, "note.webm");
    assert.equal(w.calls[0].init.body.get("prior_question"), null);
    assert.equal(box.value, "Two things. Make the Features page shorter."); assert.equal(box.focused, true);
    assert.equal(w.audios.length, 0); assert.equal(state(p), "idle");
    assert.match(p.words.textContent, /Your words are in the box/);
    results.voice_note = "words in the box, nothing read aloud";
  }
  // The voice note refused (the server's sentence), signed out, and the network failing: in its own words.
  for (const [reply, st, words, name] of [
    [{ ok: false, status: 429, body: { message: "You have recorded 10 voice notes in the last hour." } }, "idle",
      /10 voice notes/, "note_refused"],
    [{ ok: false, status: 401, body: { detail: "login required" } }, "error", /Please sign in again/, "note_signed_out"],
    ["network", "idle", /please type your suggestion/, "note_network"],
  ]) {
    const p = page(), w = browser({ reply });
    const box = { value: "" };
    p.panel.sel["[data-voice-fill]"] = box;
    mic.attach(w, p.panel);
    p.btn.fire("pointerdown"); await tick(); clock += 1000; p.btn.fire("pointerup"); await tick();
    assert.equal(state(p), st, name); assert.match(p.words.textContent, words, name);
    assert.equal(box.value, "", name);
    results[name] = st;
  }
  console.log(JSON.stringify(results));
})().catch((e) => { console.error(e); process.exit(1); });
