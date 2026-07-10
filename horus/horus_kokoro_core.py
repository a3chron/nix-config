# Shared Kokoro-82M synthesis core for Horus voice replies.
# Used by both the one-shot CLI (horus-kokoro-tts.py) and the warm daemon
# (horus-kokoro-daemon.py) so the actual audio is byte-identical either way.
# No pip packages: onnxruntime (CPU) + phonemizer (espeak-ng) + numpy.
import json
import os
import re
import wave

import numpy as np
import onnxruntime as ort
from phonemizer.backend import EspeakBackend

MODEL = "/var/lib/llm/models/kokoro-v1.0.onnx"
VOICES = "/var/lib/llm/models/voices-v1.0.bin"  # npz: name -> [510,1,256] styles
VOICE = "am_michael"
RATE = 24000
MAX_TOKENS = 510  # model limit per inference; longer text is split on sentences

VOCAB = json.loads(
    '{";":1,":":2,",":3,".":4,"!":5,"?":6,"\\u2014":9,"\\u2026":10,"\\"":11,'
    '"(":12,")":13,"\\u201c":14,"\\u201d":15," ":16,"\\u0303":17,"\\u02a3":18,'
    '"\\u02a5":19,"\\u02a6":20,"\\u02a8":21,"\\u1d5d":22,"\\uab67":23,"A":24,'
    '"I":25,"O":31,"Q":33,"S":35,"T":36,"W":39,"Y":41,"\\u1d4a":42,"a":43,'
    '"b":44,"c":45,"d":46,"e":47,"f":48,"h":50,"i":51,"j":52,"k":53,"l":54,'
    '"m":55,"n":56,"o":57,"p":58,"q":59,"r":60,"s":61,"t":62,"u":63,"v":64,'
    '"w":65,"x":66,"y":67,"z":68,"\\u0251":69,"\\u0250":70,"\\u0252":71,'
    '"\\u00e6":72,"\\u03b2":75,"\\u0254":76,"\\u0255":77,"\\u00e7":78,'
    '"\\u0256":80,"\\u00f0":81,"\\u02a4":82,"\\u0259":83,"\\u025a":85,'
    '"\\u025b":86,"\\u025c":87,"\\u025f":90,"\\u0261":92,"\\u0265":99,'
    '"\\u0268":101,"\\u026a":102,"\\u029d":103,"\\u026f":110,"\\u0270":111,'
    '"\\u014b":112,"\\u0273":113,"\\u0272":114,"\\u0274":115,"\\u00f8":116,'
    '"\\u0278":118,"\\u03b8":119,"\\u0153":120,"\\u0279":123,"\\u027e":125,'
    '"\\u027b":126,"\\u0281":128,"\\u027d":129,"\\u0282":130,"\\u0283":131,'
    '"\\u0288":132,"\\u02a7":133,"\\u028a":135,"\\u028b":136,"\\u028c":138,'
    '"\\u0263":139,"\\u0264":140,"\\u03c7":142,"\\u028e":143,"\\u0292":147,'
    '"\\u0294":148,"\\u02c8":156,"\\u02cc":157,"\\u02d0":158,"\\u02b0":162,'
    '"\\u02b2":164,"\\u2193":169,"\\u2192":171,"\\u2197":172,"\\u2198":173,'
    '"\\u1d7b":177}'
)


def tokenize(backend, text):
    phonemes = backend.phonemize([text], strip=True)[0]
    return [VOCAB[p] for p in phonemes if p in VOCAB][:MAX_TOKENS]


def trim_silence(audio, thresh=0.005, margin=int(0.05 * RATE)):
    loud = np.flatnonzero(np.abs(audio) > thresh)
    if len(loud) == 0:
        return audio
    return audio[max(0, loud[0] - margin) : min(len(audio), loud[-1] + margin)]


class Engine:
    """Holds the (expensive to build) ONNX session, espeak backend and voice
    styles. Build once, reuse for every synth — this is what the daemon keeps
    warm; the one-shot CLI builds a throwaway one."""

    def __init__(self):
        ort.set_default_logger_severity(3)
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # CPU-only synthesis; use all cores for the per-sentence inference
        so.intra_op_num_threads = os.cpu_count() or 4
        self.sess = ort.InferenceSession(
            MODEL, sess_options=so, providers=["CPUExecutionProvider"]
        )
        self.voices = np.load(VOICES)  # lazy npz: indexed per voice on demand
        self.backend = EspeakBackend(
            "en-us", preserve_punctuation=True, with_stress=True
        )

    def synth_to_wav(self, text, out, voice=VOICE, speed=1.0):
        """Synthesize `text` to a mono 16-bit WAV at `out`. Returns True on
        success; raises on hard failure, returns False if nothing speakable."""
        styles = self.voices[voice]  # row = style for that token count
        # one inference per sentence keeps every call well under MAX_TOKENS
        sentences = [s for s in re.split(r"(?<=[.!?;:])\s+", text.strip()) if s]
        parts = []
        for sentence in sentences:
            tokens = tokenize(self.backend, sentence)
            if not tokens:
                continue
            audio = self.sess.run(None, {
                "tokens": np.array([[0, *tokens, 0]], dtype=np.int64),
                "style": styles[len(tokens)].astype(np.float32),
                "speed": np.array([speed], dtype=np.float32),
            })[0].squeeze()
            parts.append(trim_silence(audio))
            parts.append(np.zeros(int(0.15 * RATE), dtype=np.float32))
        if not parts:
            return False

        pcm = (np.clip(np.concatenate(parts), -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm.tobytes())
        return True


def socket_path():
    """Where the daemon listens and the client connects. Both run as the same
    user service, so the per-user runtime dir is shared."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, "horus-tts.sock")
