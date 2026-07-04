# Kokoro-82M TTS via onnxruntime (CPU) — replaces Piper for Horus replies.
# No pip packages: phonemizer (espeak-ng backend) + embedded vocab from
# hexgrad/Kokoro-82M config.json + the community ONNX export do everything.
# Invoked through the `horus-tts` wrapper (voice.nix), which provides the
# python env and PHONEMIZER_ESPEAK_LIBRARY.
#
#   horus-tts --out /tmp/t.wav [--voice af_heart] [--speed 1.0] "text"
import argparse
import json
import re
import sys
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--out", required=True)
    ap.add_argument("--voice", default=VOICE)
    ap.add_argument("--speed", type=float, default=1.0)
    args = ap.parse_args()

    ort.set_default_logger_severity(3)
    sess = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    styles = np.load(VOICES)[args.voice]  # row = style for that token count
    backend = EspeakBackend("en-us", preserve_punctuation=True, with_stress=True)

    # one inference per sentence keeps every call well under MAX_TOKENS
    sentences = [s for s in re.split(r"(?<=[.!?;:])\s+", args.text.strip()) if s]
    parts = []
    for sentence in sentences:
        tokens = tokenize(backend, sentence)
        if not tokens:
            continue
        audio = sess.run(None, {
            "tokens": np.array([[0, *tokens, 0]], dtype=np.int64),
            "style": styles[len(tokens)].astype(np.float32),
            "speed": np.array([args.speed], dtype=np.float32),
        })[0].squeeze()
        parts.append(trim_silence(audio))
        parts.append(np.zeros(int(0.15 * RATE), dtype=np.float32))
    if not parts:
        print("nothing speakable", file=sys.stderr)
        sys.exit(1)

    pcm = (np.clip(np.concatenate(parts), -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


main()
