"use strict";
/*
 * voice.js - the hands-free conversation loop.
 *
 * The old flow was: click mic -> talk -> reply -> click mic again. This
 * replaces it with a mic that stays open and a state machine that decides on
 * its own when a turn starts and ends, so a back-and-forth conversation costs
 * zero clicks after the first one.
 *
 * Why raw PCM instead of MediaRecorder (which the previous version used):
 * MediaRecorder only captures from the moment you press record, so by the time
 * a level meter notices speech the first syllable is already gone. Here an
 * AudioWorklet (pcm-worklet.js) feeds a 30-second ring buffer continuously,
 * and when the detector decides "that was speech" it reaches BACKWARDS past
 * the onset to grab a pre-roll. Nothing gets clipped.
 *
 * Web Speech API is still deliberately unused: Chrome-only, ships your audio
 * to Google, silently dead in Brave. Detection is local; only the audio of an
 * actual utterance is sent, and only to ElevenLabs via our own server.
 *
 * Guardrail note: this listens continuously but does NOT transcribe
 * continuously. Audio leaves the browser only when the local detector has
 * seen a real utterance (see MIN_VOICED_MS), so an idle room costs nothing.
 */

const VOICE = {
  RATE: 16000,               // what we resample to; Scribe is happy with it
  RING_SECONDS: 30,
  BLOCK_MS: 10,              // VAD decision granularity

  PREROLL_MS: 350,           // audio kept from BEFORE speech was detected
  ONSET_MS: 140,             // sustained loudness before it counts as speech
  END_SILENCE_MS: 750,       // quiet this long ends your turn
  MAX_UTTERANCE_MS: 15000,   // hard stop, so a stuck-open mic can't run away
  MIN_VOICED_MS: 300,        // shorter than this is a cough - don't send it

  NOISE_ALPHA: 0.015,        // how fast the noise floor adapts
  SPEECH_FACTOR: 3.2,        // threshold = noise floor * this
  MIN_THRESHOLD: 0.012,      // absolute floor, for a very quiet room
  BARGE_FACTOR: 2.2,         // extra strictness while JARVIS is talking
  BARGE_ONSET_MS: 260,       // and you have to mean it for this long

  POST_SPEECH_GRACE_MS: 350, // ignore the room right after our own audio stops
  FOLLOWUP_WINDOW_MS: 25000, // wake word not needed again for this long
};

const FILLER_PREFIXES = ["hey", "ok", "okay", "hi", "hello", "yo", "um", "uh"];

// ---------------------------------------------------------------- wake word

function levenshtein(a, b) {
  const m = a.length, n = b.length;
  let prev = Array.from({ length: n + 1 }, (_, j) => j);
  for (let i = 1; i <= m; i++) {
    const cur = [i];
    for (let j = 1; j <= n; j++) {
      cur[j] = Math.min(
        prev[j] + 1,
        cur[j - 1] + 1,
        prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)
      );
    }
    prev = cur;
  }
  return prev[n];
}

/*
 * Fuzzy on purpose. Scribe mishears "Jarvis" as Travis / Javis / Jervis
 * constantly, and a wake word that only fires on a perfect transcript is a
 * wake word that feels broken. Distance <= 2 on a 6-letter word catches the
 * near misses; adjacent tokens are also joined and tested, because it often
 * splits the word in two.
 */
function findWake(text) {
  const tokens = text.toLowerCase().replace(/[^a-z0-9\s]/g, " ").split(/\s+/).filter(Boolean);
  const limit = Math.min(tokens.length, 5);
  for (let i = 0; i < limit; i++) {
    if (levenshtein(tokens[i], "jarvis") <= 2) return { start: i, end: i + 1 };
    if (i + 1 < tokens.length &&
        levenshtein(tokens[i] + tokens[i + 1], "jarvis") <= 2) {
      return { start: i, end: i + 2 };
    }
  }
  return null;
}

function stripWake(text, hit) {
  const raw = text.split(/\s+/).filter(Boolean);
  const kept = raw.slice(hit.end).join(" ").replace(/^[\s,.\-–—:]+/, "");
  return kept.trim();
}

function normalizeForCompare(s) {
  return s.toLowerCase().replace(/[^a-z0-9]/g, "");
}

// -------------------------------------------------------------------- WAV

function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeStr = (off, str) => {
    for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, "WAVEfmt ");
  view.setUint32(16, 16, true);          // PCM header size
  view.setUint16(20, 1, true);           // format = PCM
  view.setUint16(22, 1, true);           // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);           // block align
  view.setUint16(34, 16, true);          // bits per sample
  writeStr(36, "data");
  view.setUint32(40, samples.length * 2, true);
  let off = 44;
  for (let i = 0; i < samples.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

// ============================================================ VoiceEngine

class VoiceEngine {
  /*
   * Callbacks (all optional):
   *   onState(state)        off | armed | capturing | transcribing | thinking | speaking
   *   onLevel(level, thr)   0-1 mic level plus the current speech threshold
   *   onCaption(text, kind) what to show the human right now
   *   onError(message)      something the human needs to know about
   *   onUtterance(text)     async; run the turn, return {speech} to be spoken
   */
  constructor(callbacks = {}) {
    this.cb = callbacks;
    this.mode = "off";           // off | live | wake
    this.state = "off";
    this.muted = false;          // mutes OUTPUT (TTS), not the mic

    this.ctx = null;
    this.stream = null;
    this.node = null;

    this.ring = new Float32Array(VOICE.RATE * VOICE.RING_SECONDS);
    this.written = 0;            // absolute sample count, monotonic

    this._accSum = 0; this._accN = 0; this._accPhase = 0;
    this._blockSum = 0; this._blockN = 0;
    this._blockSamples = Math.round(VOICE.RATE * VOICE.BLOCK_MS / 1000);

    this.noiseFloor = 0.01;
    this._voicedBlocks = 0;
    this._silentBlocks = 0;
    this._captureStart = 0;      // absolute sample index
    this._voicedMs = 0;
    this._levelSmoothed = 0;
    this._levelTick = 0;

    this._audio = null;
    this._resolveSpeak = null;
    this._speechEndedAt = 0;
    this._lastSpoken = "";
    this._openUntil = 0;         // follow-ups need no wake word until this time
    this._starting = null;
  }

  // ------------------------------------------------------------ lifecycle

  get running() { return this.mode !== "off"; }

  /* Idempotent, and safe to call from any user gesture. */
  async start(mode = "live") {
    if (this._starting) return this._starting;
    if (this.running) { this.setMode(mode); return true; }
    this._starting = this._open(mode).finally(() => { this._starting = null; });
    return this._starting;
  }

  async _open(mode) {
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          // Echo cancellation is what stops JARVIS from hearing itself
          // through the speakers and answering its own reply.
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
    } catch (e) {
      this._fail("Microphone permission was denied - voice input is off.");
      return false;
    }

    try {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      await this.ctx.audioWorklet.addModule("/pcm-worklet.js");
      if (this.ctx.state === "suspended") await this.ctx.resume();
      const source = this.ctx.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.ctx, "pcm-tap");
      this.node.port.onmessage = (e) => this._onFrames(e.data);
      source.connect(this.node);
      // A zero-gain sink: some browsers won't pump a worklet that isn't
      // connected to anything downstream. Nothing is actually output.
      const sink = this.ctx.createGain();
      sink.gain.value = 0;
      this.node.connect(sink).connect(this.ctx.destination);
    } catch (e) {
      this._fail("Couldn't start the audio pipeline: " + e.message);
      this.stopAll();
      return false;
    }

    this.mode = mode;
    this._openUntil = mode === "live" ? Infinity : 0;
    this._setState("armed");
    return true;
  }

  stopAll() {
    this._stopSpeaking();
    if (this.node) { try { this.node.disconnect(); } catch (e) {} this.node = null; }
    if (this.ctx) { this.ctx.close().catch(() => {}); this.ctx = null; }
    if (this.stream) { this.stream.getTracks().forEach((t) => t.stop()); this.stream = null; }
    this.mode = "off";
    this._resetDetector();
    this._setState("off");
  }

  setMode(mode) {
    if (mode === this.mode) return;
    if (mode === "off") { this.stopAll(); return; }
    this.mode = mode;
    // Switching to live is itself the permission to talk freely; switching to
    // wake closes any open follow-up window so the next thing said is gated.
    this._openUntil = mode === "live" ? Infinity : 0;
    this._resetDetector();
    if (this.state === "capturing") this._setState("armed");
    if (this.cb.onModeChange) this.cb.onModeChange(mode);
  }

  setMuted(muted) {
    this.muted = muted;
    if (muted) this._stopSpeaking();
  }

  /* Escape hatch: drop everything in flight, keep the mic open. */
  panic() {
    this._stopSpeaking();
    this._resetDetector();
    if (this.running) this._setState("armed");
    this._caption("");
  }

  // ------------------------------------------------------------- capture

  _onFrames(frame) {
    if (!this.ctx) return;
    const ratio = this.ctx.sampleRate / VOICE.RATE;
    for (let i = 0; i < frame.length; i++) {
      this._accSum += frame[i];
      this._accN++;
      this._accPhase++;
      if (this._accPhase < ratio) continue;
      this._accPhase -= ratio;
      const s = this._accSum / this._accN;
      this._accSum = 0; this._accN = 0;

      this.ring[this.written % this.ring.length] = s;
      this.written++;

      this._blockSum += s * s;
      this._blockN++;
      if (this._blockN >= this._blockSamples) {
        this._onBlock(Math.sqrt(this._blockSum / this._blockN));
        this._blockSum = 0; this._blockN = 0;
      }
    }
  }

  _threshold() {
    return Math.max(VOICE.MIN_THRESHOLD, this.noiseFloor * VOICE.SPEECH_FACTOR);
  }

  _onBlock(rms) {
    const thr = this._threshold();
    const voiced = rms > thr;

    // The noise floor only learns from quiet blocks, and never while we're
    // capturing - otherwise a long sentence would slowly teach it that speech
    // is the new silence and the turn would never end.
    if (!voiced && this.state !== "capturing") {
      this.noiseFloor += (rms - this.noiseFloor) * VOICE.NOISE_ALPHA;
    }

    this._levelSmoothed += (rms - this._levelSmoothed) * 0.35;
    if (++this._levelTick % 3 === 0 && this.cb.onLevel) {
      this.cb.onLevel(Math.min(1, this._levelSmoothed / (thr * 4)), thr);
    }

    const blocksFor = (ms) => Math.max(1, Math.round(ms / VOICE.BLOCK_MS));

    switch (this.state) {
      case "armed": {
        if (performance.now() - this._speechEndedAt < VOICE.POST_SPEECH_GRACE_MS) {
          this._voicedBlocks = 0;
          return;
        }
        this._voicedBlocks = voiced ? this._voicedBlocks + 1 : 0;
        if (this._voicedBlocks >= blocksFor(VOICE.ONSET_MS)) {
          this._beginCapture(VOICE.ONSET_MS);
        }
        return;
      }

      case "speaking": {
        // Barge-in: interrupting mid-sentence should just work, but the bar
        // is higher here so a stray keyboard clack can't cut JARVIS off.
        const bargeThr = thr * VOICE.BARGE_FACTOR;
        this._voicedBlocks = rms > bargeThr ? this._voicedBlocks + 1 : 0;
        if (this._voicedBlocks >= blocksFor(VOICE.BARGE_ONSET_MS)) {
          this._stopSpeaking();
          this._beginCapture(VOICE.BARGE_ONSET_MS);
        }
        return;
      }

      case "capturing": {
        if (voiced) {
          this._silentBlocks = 0;
          this._voicedMs += VOICE.BLOCK_MS;
        } else {
          this._silentBlocks++;
        }
        const elapsed = (this.written - this._captureStart) / VOICE.RATE * 1000;
        if (this._silentBlocks >= blocksFor(VOICE.END_SILENCE_MS) ||
            elapsed >= VOICE.MAX_UTTERANCE_MS) {
          this._endCapture();
        }
        return;
      }

      default:
        // transcribing / thinking / off: the room is not being judged.
        this._voicedBlocks = 0;
    }
  }

  _beginCapture(onsetMs) {
    const back = Math.round((onsetMs + VOICE.PREROLL_MS) / 1000 * VOICE.RATE);
    this._captureStart = Math.max(0, this.written - back);
    this._voicedMs = onsetMs;
    this._silentBlocks = 0;
    this._voicedBlocks = 0;
    this._setState("capturing");
    this._caption("Listening…", "listening");
  }

  _endCapture() {
    // No explicit tail needed: the turn only ends after END_SILENCE_MS of
    // quiet, so that trailing silence is already inside the slice.
    const from = this._captureStart;
    const to = this.written;
    this._silentBlocks = 0;

    if (this._voicedMs < VOICE.MIN_VOICED_MS) {
      // A door, a cough, a chair. Never leaves the browser.
      this._setState("armed");
      this._caption("");
      return;
    }
    this._setState("transcribing");
    this._caption("Transcribing…", "working");
    this._transcribe(this._slice(from, to));
  }

  _slice(from, to) {
    const len = this.ring.length;
    from = Math.max(from, this.written - len);   // anything older is overwritten
    const out = new Float32Array(Math.max(0, to - from));
    for (let i = 0; i < out.length; i++) out[i] = this.ring[(from + i) % len];
    return out;
  }

  _resetDetector() {
    this._voicedBlocks = 0;
    this._silentBlocks = 0;
    this._voicedMs = 0;
  }

  // ---------------------------------------------------------------- turn

  async _transcribe(samples) {
    let text = "";
    try {
      const res = await fetch("/api/listen", {
        method: "POST",
        headers: { "Content-Type": "audio/wav" },
        body: encodeWav(samples, VOICE.RATE),
      });
      const result = await res.json();
      if (result.error) { this._fail(result.error); return this._rearm(); }
      text = (result.text || "").trim();
    } catch (e) {
      this._fail("Couldn't reach the transcriber.");
      return this._rearm();
    }

    if (!text || normalizeForCompare(text).length < 2) return this._rearm();

    // Self-transcription guard: if echo cancellation let some of our own
    // reply through, don't answer ourselves. (This is the failure mode the
    // whole "mic goes deaf during playback" approach used to paper over.)
    const heard = normalizeForCompare(text);
    const said = normalizeForCompare(this._lastSpoken);
    if (heard.length > 8 && said.length > 8 &&
        (said.includes(heard) || heard.includes(said))) {
      return this._rearm();
    }

    let message = text;
    if (this.mode === "wake" && performance.now() > this._openUntil) {
      const hit = findWake(text);
      if (!hit) {
        // Heard, understood, deliberately ignored: not addressed to JARVIS.
        this._caption("", "");
        return this._rearm();
      }
      message = stripWake(text, hit);
      if (!message) {
        // Just the name - acknowledge and hold the floor open.
        this._openUntil = performance.now() + VOICE.FOLLOWUP_WINDOW_MS;
        this._caption("Jarvis", "you");
        await this.speak("Here.");
        return;
      }
    } else {
      const hit = findWake(text);
      if (hit) {
        const rest = stripWake(text, hit);
        if (rest) message = rest;   // saying the name in live mode is harmless
      }
    }

    message = message.replace(new RegExp(`^(${FILLER_PREFIXES.join("|")})[\\s,]+`, "i"), "");
    if (!message.trim()) return this._rearm();

    this._caption(message, "you");
    this._setState("thinking");

    let reply = null;
    try {
      reply = this.cb.onUtterance ? await this.cb.onUtterance(message) : null;
    } catch (e) {
      this._fail("That turn didn't complete.");
    }

    this._openUntil = this.mode === "live"
      ? Infinity
      : performance.now() + VOICE.FOLLOWUP_WINDOW_MS;

    if (reply && reply.speech) await this.speak(reply.speech);
    else this._rearm();
  }

  _rearm() {
    this._resetDetector();
    if (this.running) this._setState("armed");
    else this._setState("off");
  }

  // ----------------------------------------------------------------- TTS

  async speak(text) {
    if (!text || !text.trim()) return this._rearm();
    if (this.muted) { this._lastSpoken = ""; return this._rearm(); }

    this._stopSpeaking();
    this._lastSpoken = text;
    this._resetDetector();

    // GET, not POST, so the browser can start playing the moment the first
    // bytes of the stream land instead of waiting for the whole clip.
    const url = "/api/speak?text=" + encodeURIComponent(text);
    const audio = new Audio(url);
    this._audio = audio;
    this._setState("speaking");
    this._caption(text, "jarvis");

    await new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        this._resolveSpeak = null;
        resolve();
      };
      this._resolveSpeak = finish;
      audio.onended = finish;
      audio.onerror = async () => {
        // The <audio> element only ever reports "it failed". Ask the server
        // what actually went wrong so the human gets the real reason.
        try {
          const res = await fetch("/api/speak", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text }),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            this._fail(err.error || "Voice output failed.");
          }
        } catch (e) {
          this._fail("Voice output failed.");
        }
        finish();
      };
      audio.play().catch(() => {
        this._fail("The browser blocked audio playback - click the page once.");
        finish();
      });
    });

    if (this._audio === audio) this._audio = null;
    // A barge-in resolves this promise early and has ALREADY moved us into
    // capturing. Only re-arm if playback is what actually finished.
    if (this.state === "speaking") {
      this._speechEndedAt = performance.now();
      this._rearm();
      this._caption("");
    }
  }

  _stopSpeaking() {
    if (this._audio) {
      const a = this._audio;
      this._audio = null;
      a.onended = null;   // nulled first: clearing src fires onerror otherwise
      a.onerror = null;
      a.pause();
      a.src = "";
    }
    if (this._resolveSpeak) this._resolveSpeak();
    this._speechEndedAt = performance.now();
  }

  // ------------------------------------------------------------- plumbing

  _setState(state) {
    if (this.state === state) return;
    this.state = state;
    if (this.cb.onState) this.cb.onState(state);
  }

  _caption(text, kind = "") {
    if (this.cb.onCaption) this.cb.onCaption(text, kind);
  }

  _fail(message) {
    if (this.cb.onError) this.cb.onError(message);
  }
}

window.VoiceEngine = VoiceEngine;
window.VOICE_TUNING = VOICE;
