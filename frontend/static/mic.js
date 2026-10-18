// Voice for the chat (Part E): hold the microphone button to speak, or press it once to start and
// again to send. The recording (at most 30 seconds) goes to /api/voice/ask; the answer joins the chat
// log as text and is read aloud. It records WebM/Opus (Chrome, Edge, Firefox) or MP4 (Safari), the
// formats the server accepts. Without a secure page, a microphone, or a MediaRecorder that can make one
// of those, the button stays hidden and the text box works as before. States: chat-mic (ready), chat-mic--listening,
// --thinking, --speaking, --error; the words for each are in the .chat-voice status line.
// A voice note (Part F, the suggestion box): in a panel whose text box carries data-voice-fill, the
// recording goes to /api/voice/transcribe instead, and the words heard are added to that box for the
// person to check and send. Nothing is asked of the chat and nothing is read aloud.
(function () {
  "use strict";
  var TYPES = ["audio/webm;codecs=opus", "audio/mp4"];
  var MAX_MS = 30000, HOLD_MS = 400;
  var WORDS = {
    idle: "Hold the microphone to ask by voice, or press it once to start and again to send.",
    listening: "Listening. Let go, or press again, to send. Up to 30 seconds.",
    thinking: "Working out the answer…",
    speaking: "Reading the answer aloud. Press the microphone to stop.",
    error: "The microphone isn't working here, so allow it for this site and try again, or type your question.",
    offline: "Voice isn't available right now, so please type your question.",
    signin: "Please sign in again, then try the microphone.",
    recorder: "This browser couldn't start recording, so please type your question instead.",
    failed: "Something went wrong sending your question, so please try again or type it."
  };
  var NOTE_WORDS = {
    idle: "Hold the microphone to say your suggestion, or press it once to start and again to stop.",
    listening: "Listening. Let go, or press again, to stop. Up to 30 seconds.",
    thinking: "Writing down what you said…",
    filled: "Your words are in the box. Check them, then press Send.",
    error: "The microphone isn't working here, so allow it for this site and try again, or type your suggestion.",
    offline: "Voice isn't available right now, so please type your suggestion.",
    signin: "Please sign in again, then try the microphone.",
    recorder: "This browser couldn't start recording, so please type your suggestion instead.",
    failed: "Something went wrong sending your recording, so please try again or type your suggestion."
  };

  function supported(w) {
    var md = w.navigator && w.navigator.mediaDevices;
    var rec = w.MediaRecorder;
    return !!(w.isSecureContext && rec && md && md.getUserMedia && typeof rec.isTypeSupported === "function" &&
      TYPES.some(function (t) { return rec.isTypeSupported(t); }));
  }

  function attach(w, panel) {
    var btn = panel.querySelector(".chat-mic"), status = panel.querySelector(".chat-voice");
    if (!btn || !status || !supported(w)) return null;
    var log = panel.querySelector(".chat__log"), words = status.querySelector(".chat-voice__text");
    var box = panel.querySelector("[data-voice-fill]"), W = box ? NOTE_WORDS : WORDS;
    var rec = null, stream = null, chunks = [], timer = null, pressedAt = 0, audio = null, state = "", asked = 0;

    function set(s, text) {
      state = s;
      ["listening", "thinking", "speaking", "error"].forEach(function (m) { btn.classList.toggle("chat-mic--" + m, s === m); });
      btn.setAttribute("aria-pressed", s === "listening" ? "true" : "false");
      words.textContent = text || W[s];
      status.querySelector(".chat-voice__wave").hidden = s !== "listening";
      status.querySelector(".chat-voice__dots").hidden = s !== "thinking";
    }
    function close() { if (stream) stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    function start() {
      var mine = ++asked; // only the latest request for the microphone may record; any other is closed
      set("listening");
      w.navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
        if (mine !== asked || state !== "listening" || rec) {
          return s.getTracks().forEach(function (t) { t.stop(); }); // let go first, or superseded
        }
        stream = s;
        try {
          var type = TYPES.filter(function (t) { return w.MediaRecorder.isTypeSupported(t); })[0];
          rec = new w.MediaRecorder(s, type ? { mimeType: type } : {});
          chunks = [];
          rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
          rec.onstop = send;
          rec.start();
          timer = w.setTimeout(stop, MAX_MS);
        } catch (err) { // the recorder would not start: let the microphone go
          w.clearTimeout(timer); timer = null; rec = null; close();
          set("error", W.recorder);
        }
      }, function () { if (mine === asked) set("error"); });
    }
    function stop() {
      pressedAt = 0;
      if (!rec) { close(); return set("idle"); }
      if (rec.state === "recording") rec.stop();
    }
    function send() {
      w.clearTimeout(timer); close();
      var type = (rec.mimeType || "audio/webm").split(";")[0], blob = new w.Blob(chunks, { type: type });
      rec = null;
      if (!blob.size) return set("idle");
      set("thinking");
      var form = new w.FormData(), last = !box && log && log.lastElementChild;
      form.append("audio", blob, (box ? "note." : "question.") + (type.split("/")[1] || "webm"));
      var reply = last && last.querySelector(".chat-exchange__reply");
      if (reply) ["prior_question", "prior_clarify"].forEach(function (n) {
        form.append(n, reply.querySelector("[name=" + n + "]").value);
      });
      w.fetch(box ? "/api/voice/transcribe" : "/api/voice/ask", { method: "POST", body: form, credentials: "same-origin" })
        .then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (d) { return [r, d || {}]; });
        })
        .then(function (res) {
          var r = res[0], d = res[1];
          if (box && r.ok && typeof d.transcript === "string") return fill(d.transcript);
          if (!box && r.ok && d.html) return show(d, reply);
          if (d.message) return set("idle", d.message);
          set("error", r.status === 401 || r.status === 403 ? W.signin : W.failed);
        })
        .catch(function () { set("idle", W.offline); });
    }
    function fill(text) { // a voice note: the words join what is already in the box, as plain text
      box.value = box.value ? box.value.replace(/\s+$/, "") + " " + text : text;
      if (box.focus) box.focus();
      set("idle", W.filled);
    }
    function show(d, reply) {
      if (reply) reply.remove();
      log.insertAdjacentHTML("beforeend", d.html);
      var added = log.lastElementChild;
      if (w.htmx) w.htmx.process(added);
      if (!d.audio) return set("idle", d.voice_note || null);
      audio = new w.Audio("data:" + d.audio.content_type + ";base64," + d.audio.base64);
      audio.onended = function () { set("idle"); };
      set("speaking");
      audio.play().then(function () {
        var how = added.querySelector(".chat-exchange__how");
        if (how) how.textContent = "Read aloud";
      }, function () { set("idle", "This browser didn't let the answer play aloud, so it is shown below."); });
    }
    function press(e) {
      if (e.type === "keydown") {
        if (e.key !== " " && e.key !== "Enter") return;
        e.preventDefault();
        if (e.repeat) return;
      } else if (e.button > 0) return; // only the main mouse button, a finger or a pen
      if (state === "speaking") { audio.pause(); return set("idle"); }
      if (state === "listening") return stop();
      if (state === "thinking") return;
      pressedAt = Date.now(); start();
    }
    function release(e) {
      if (e.type === "keyup" && e.key !== " " && e.key !== "Enter") return;
      if (state === "listening" && pressedAt && Date.now() - pressedAt >= HOLD_MS) stop();
    }
    btn.addEventListener("pointerdown", press);
    btn.addEventListener("keydown", press);
    ["pointerup", "pointercancel", "keyup"].forEach(function (n) { btn.addEventListener(n, release); });
    btn.addEventListener("contextmenu", function (e) { e.preventDefault(); }); // a long press on a phone
    btn.hidden = false; status.hidden = false;
    set("idle");
    return { state: function () { return state; } };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { attach: attach, supported: supported };
  else document.querySelectorAll(".chat, .suggest").forEach(function (p) { attach(window, p); });
})();
