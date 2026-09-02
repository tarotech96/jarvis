/*
 * pcm-worklet.js - taps raw mic PCM off the audio thread.
 *
 * MediaRecorder can't do this job: it only hands you audio AFTER you've
 * decided to record, so the first syllable of every sentence is gone by the
 * time a level meter has noticed you started talking. Reading raw frames
 * instead means voice.js can keep a rolling buffer and reach BACKWARDS in
 * time once speech is detected - no clipped words, no click-to-arm.
 *
 * Runs on the audio render thread, so it stays alive in a backgrounded tab.
 */
class PCMTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = new Float32Array(1024);
    this._n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    // Batch 128-sample render quanta into ~1024-sample posts: same data,
    // an eighth of the postMessage traffic.
    for (let i = 0; i < ch.length; i++) {
      this._buf[this._n++] = ch[i];
      if (this._n === this._buf.length) {
        this.port.postMessage(this._buf.slice(0));
        this._n = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-tap", PCMTap);
