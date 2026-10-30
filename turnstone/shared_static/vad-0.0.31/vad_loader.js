// Adapter between stt_mic.js and the vendored Silero VAD stack.
//
// stt_mic.js only needs a 0..1 speech probability per frame; its own
// hysteresis decides when to stop.  @ricky0123/vad-web's MicVAD does its own
// start/end detection too, so this adapter ignores those callbacks and
// forwards onFrameProcessed's raw probability instead.
//
// Both vendored bundles are UMD scripts (vad-web reads the global `ort`), so
// they are loaded as classic <script> tags once per page, in order.

const BASE = new URL("./", import.meta.url).href;

let _ready = null;

function _script(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.async = false;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error("failed to load " + src));
    document.head.appendChild(el);
  });
}

function _loadBundles() {
  if (!_ready) {
    _ready = (async () => {
      if (!globalThis.ort) await _script(BASE + "ort.wasm.min.js");
      if (!globalThis.vad) await _script(BASE + "bundle.min.js");
      // Threaded wasm needs cross-origin isolation, which the console does
      // not set; one thread is plenty for a 1.5 MB model at 32 ms frames.
      globalThis.ort.env.wasm.numThreads = 1;
      globalThis.ort.env.wasm.wasmPaths = BASE;
      return globalThis.vad;
    })().catch((err) => {
      _ready = null;
      throw err;
    });
  }
  return _ready;
}

export async function loadVad() {
  const vad = await _loadBundles();
  return {
    async watch(stream, onProbability) {
      const mic = await vad.MicVAD.new({
        model: "v5",
        baseAssetPath: BASE,
        onnxWASMBasePath: BASE,
        getStream: async () => stream,
        // The page owns the stream: never let the VAD stop or pause it.
        pauseStream: async () => {},
        resumeStream: async () => stream,
        onFrameProcessed: (p) => onProbability(p.isSpeech),
      });
      mic.start();
      return {
        stop() {
          try {
            mic.destroy();
          } catch {
            // already torn down
          }
        },
      };
    },
  };
}
