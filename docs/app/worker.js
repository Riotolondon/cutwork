// Speech-to-text in the browser. Runs in a worker so the page stays smooth.
import { pipeline, env } from 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.8.1';

env.allowLocalModels = false;
let asr = null, loaded = '';

async function load(model) {
  if (asr && loaded === model) return;
  asr = null;
  let gpu = false;
  try { gpu = !!(navigator.gpu && await navigator.gpu.requestAdapter()); } catch (e) { gpu = false; }
  const tries = gpu ? [['webgpu', { encoder_model: 'fp32', decoder_model_merged: 'q4' }], ['wasm', 'q8']] : [['wasm', 'q8']];
  const sizes = {};
  const progress_callback = p => {
    if (p.status !== 'progress' || !p.total) return;
    sizes[p.file] = [p.loaded, p.total];
    let a = 0, b = 0;
    for (const k in sizes) { a += sizes[k][0]; b += sizes[k][1]; }
    postMessage({ type: 'model', pct: Math.round(a / b * 100) });
  };
  let last;
  for (const [device, dtype] of tries) {
    try {
      asr = await pipeline('automatic-speech-recognition', model, { device, dtype, progress_callback });
      loaded = model;
      postMessage({ type: 'ready', device });
      return;
    } catch (e) { last = e; }
  }
  throw last;
}

// Cut the audio where it is quietest near each boundary, so a word is not split in two.
function cutPoints(audio, sliceSec) {
  const R = 16000, n = audio.length, cuts = [0];
  const win = Math.round(0.3 * R), reach = 4 * R;
  for (let at = sliceSec * R; at < n - 10 * R; at += sliceSec * R) {
    let best = at, low = Infinity;
    for (let s = Math.max(0, at - reach); s + win < Math.min(n, at + reach); s += win / 2) {
      let e = 0;
      for (let i = s; i < s + win; i += 8) e += audio[i] * audio[i];
      if (e < low) { low = e; best = s + win / 2; }
    }
    cuts.push(Math.round(best));
  }
  cuts.push(n);
  return cuts;
}

onmessage = async ev => {
  const { audio, model, language } = ev.data;
  try {
    await load(model);
    const R = 16000, cuts = cutPoints(audio, 90);
    for (let i = 0; i + 1 < cuts.length; i++) {
      const part = audio.subarray(cuts[i], cuts[i + 1]), off = cuts[i] / R;
      const opt = { return_timestamps: 'word', chunk_length_s: 30, stride_length_s: 5 };
      if (!model.includes('.en')) { opt.language = language || 'en'; opt.task = 'transcribe'; }
      const out = await asr(part, opt);
      const words = [];
      let prev = 0;
      for (const c of out.chunks || []) {
        const w = (c.text || '').trim().replace(/^-+\s*/, '');      // the model marks a new speaker with a dash
        if (!w || !/[\p{L}\p{N}]/u.test(w) || !c.timestamp || c.timestamp[0] == null) continue;
        const s = Math.max(c.timestamp[0], prev);
        const e = Math.max(c.timestamp[1] == null ? s + 0.3 : c.timestamp[1], s + 0.02);
        words.push({ s: +(s + off).toFixed(2), e: +(e + off).toFixed(2), w });
        prev = s;
      }
      postMessage({ type: 'slice', words, done: cuts[i + 1] / R, total: audio.length / R });
    }
    postMessage({ type: 'done' });
  } catch (e) {
    postMessage({ type: 'error', message: String(e && e.message || e) });
  }
};
