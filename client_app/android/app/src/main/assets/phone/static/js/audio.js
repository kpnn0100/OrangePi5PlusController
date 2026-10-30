// Audio of the phone (PHONE.md *Audio*): 16 kHz mono s16le PCM in 20 ms frames (640 bytes), on
// /ws/mic and inside /ws/call. PcmPlayer plays frames with a small jitter buffer; MicCapture turns
// the browser's microphone (echo cancellation on) into such frames.
//
// No AudioWorklet on purpose: its module fetch would have to pass the device manager's service
// worker and the CSP; a ScriptProcessor and scheduled AudioBuffers are enough for 16 kHz voice.

export const RATE = 16000;
export const FRAME_SAMPLES = 320;            // 20 ms

let shared = null;

/** The page's one AudioContext (resumed: call it from a click the first time). */
export function audioContext() {
  if (!shared) shared = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: "interactive" });
  if (shared.state === "suspended") shared.resume().catch(() => {});
  return shared;
}

/** RMS of float samples → 0..1 on a -60..0 dBFS scale. */
export function levelOf(f32) {
  let s = 0;
  for (let i = 0; i < f32.length; i++) s += f32[i] * f32[i];
  const rms = Math.sqrt(s / (f32.length || 1));
  const db = 20 * Math.log10(rms || 1e-9);
  return Math.max(0, Math.min(1, (db + 60) / 60));
}

function pcmToFloat(buf) {
  const bytes = buf instanceof ArrayBuffer ? new Uint8Array(buf) : buf;
  const n = bytes.byteLength >> 1;
  const dv = new DataView(bytes.buffer, bytes.byteOffset, n * 2);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = dv.getInt16(i * 2, true) / 32768;
  return out;
}

/** Plays PCM frames as they arrive, a little behind real time (jitter buffer 80..350 ms). */
export class PcmPlayer {
  constructor({ rate = RATE, onLevel, minLead = 0.08, maxLead = 0.35 } = {}) {
    this.ctx = audioContext();
    this.rate = rate;
    this.gain = this.ctx.createGain();
    this.gain.connect(this.ctx.destination);
    this.next = 0;
    this.minLead = minLead;
    this.maxLead = maxLead;
    this.onLevel = onLevel;
    this.frames = 0;                 // frames played / dropped / underruns (tests, stats)
    this.dropped = 0;
    this.underruns = 0;
    this.closed = false;
  }

  setRate(rate) { if (rate > 0) this.rate = rate; }
  setVolume(v) { this.gain.gain.value = v; }

  /** One binary frame of s16le PCM. */
  push(data) {
    if (this.closed) return;
    const f = pcmToFloat(data);
    if (!f.length) return;
    this.onLevel?.(levelOf(f));
    const ctx = this.ctx;
    const now = ctx.currentTime;
    if (this.next < now + 0.01) {                    // empty (start, or the network stalled): build a cushion
      if (this.frames) this.underruns++;
      this.next = now + this.minLead;
    } else if (this.next > now + this.maxLead) {     // too far behind real time: drop to stay live
      this.dropped++;
      return;
    }
    const buf = ctx.createBuffer(1, f.length, this.rate);
    buf.copyToChannel(f, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gain);
    src.start(this.next);
    this.next += f.length / this.rate;
    this.frames++;
  }

  close() {
    this.closed = true;
    try { this.gain.disconnect(); } catch (e) { /* gone */ }
  }
}

/** Linear-interpolating resampler for a stream of buffers (after a low-pass in the graph). */
class Resampler {
  constructor(inRate, outRate) {
    this.step = inRate / outRate;
    this.pos = 0;                    // in the coordinates of the current buffer; -1 = prev
    this.prev = 0;
  }

  process(input, emit) {
    let pos = this.pos;
    const n = input.length;
    while (Math.floor(pos) + 1 < n) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const a = i < 0 ? this.prev : input[i];
      const b = input[i + 1];
      emit(a + (b - a) * frac);
      pos += this.step;
    }
    this.pos = pos - n;
    this.prev = n ? input[n - 1] : this.prev;
  }
}

/** The browser's microphone → 20 ms frames of 16 kHz s16le (ArrayBuffer of 640 bytes). */
export class MicCapture {
  constructor({ onFrame, onLevel } = {}) {
    this.onFrame = onFrame;
    this.onLevel = onLevel;
    this.muted = false;
    this.frames = 0;                 // frames handed out (tests, stats)
    this.stream = null;
  }

  async start() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("This browser can't use a microphone here (it needs https).");
    }
    const ctx = audioContext();       // created before the await: the click still counts
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        video: false,
      });
    } catch (e) {
      if (e && (e.name === "NotAllowedError" || e.name === "SecurityError")) throw new Error("The browser did not allow the microphone.");
      if (e && e.name === "NotFoundError") throw new Error("This computer has no microphone.");
      throw new Error("The microphone did not start: " + ((e && e.message) || e));
    }
    this.stream = stream;
    if (ctx.state === "suspended") await ctx.resume().catch(() => {});
    this.src = ctx.createMediaStreamSource(stream);
    this.lp = ctx.createBiquadFilter();               // anti-alias before going down to 16 kHz
    this.lp.type = "lowpass";
    this.lp.frequency.value = 7200;
    this.lp.Q.value = 0.707;
    this.proc = ctx.createScriptProcessor(2048, 1, 1);
    this.sink = ctx.createGain();
    this.sink.gain.value = 0;                          // the processor must reach the destination to run
    this.src.connect(this.lp);
    this.lp.connect(this.proc);
    this.proc.connect(this.sink);
    this.sink.connect(ctx.destination);
    const rs = new Resampler(ctx.sampleRate, RATE);
    let acc = new Int16Array(FRAME_SAMPLES), fill = 0;
    const put = (s) => {
      const v = s < -1 ? -1 : s > 1 ? 1 : s;
      acc[fill++] = v < 0 ? v * 32768 : v * 32767;
      if (fill === FRAME_SAMPLES) {
        if (!this.muted) { this.frames++; this.onFrame?.(acc.buffer); acc = new Int16Array(FRAME_SAMPLES); }
        fill = 0;
      }
    };
    this.proc.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      this.onLevel?.(this.muted ? 0 : levelOf(input));
      rs.process(input, put);
    };
  }

  setMuted(m) {
    this.muted = !!m;
    if (this.stream) for (const t of this.stream.getAudioTracks()) t.enabled = !m;
  }

  stop() {
    if (this.proc) this.proc.onaudioprocess = null;
    for (const n of [this.src, this.lp, this.proc, this.sink]) { try { n && n.disconnect(); } catch (e) { /* gone */ } }
    if (this.stream) for (const t of this.stream.getTracks()) t.stop();
    this.stream = null;
  }
}

/** A level meter element: el.set(0..1). */
export function meter(h, label) {
  const bar = h("i");
  const el = h("div.meter", { role: "meter", "aria-label": label || "Level", "aria-valuemin": "0", "aria-valuemax": "100" }, bar);
  let shownV = 0, raf = 0, target = 0;
  const tick = () => {
    raf = 0;
    shownV = target > shownV ? target : shownV * 0.85 + target * 0.15;
    bar.style.transform = `scaleX(${shownV.toFixed(3)})`;
    el.setAttribute("aria-valuenow", String(Math.round(shownV * 100)));
    el.classList.toggle("hot", shownV > 0.9);
    if (Math.abs(shownV - target) > 0.01) raf = requestAnimationFrame(tick);
  };
  el.set = (v) => { target = v; if (!raf) raf = requestAnimationFrame(tick); };
  return el;
}
