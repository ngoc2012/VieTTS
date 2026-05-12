"""
Create English voice preset from reference audio + transcript.
Usage: python make_english_voice.py
Output: voices/Matt.pt  (cached codec codes for NeuTTS)
        voices/Matt_ref.wav (trimmed reference clip)
"""

import sys
import subprocess
from pathlib import Path

VOICES_DIR = Path("voices")
AUDIO_IN   = VOICES_DIR / "Matt.mp3"
AUDIO_WAV  = VOICES_DIR / "Matt_ref.wav"
CODES_OUT  = VOICES_DIR / "Matt.pt"
TEXT_FILE  = VOICES_DIR / "Matt.txt"

# First ~10s of Matt.txt (matches audio intro)
REF_TEXT = (
    "Welcome. I'm very excited today to talk about effective speaking in spontaneous situations. "
    "I thank you all for joining us, even though the title of my talk is grammatically incorrect. "
    "I thought that might scare a few of you away."
)

CLIP_SECONDS = 10


def convert_mp3_to_wav():
    if AUDIO_WAV.exists():
        print(f"[skip] {AUDIO_WAV} already exists")
        return
    print(f"[convert] {AUDIO_IN} -> {AUDIO_WAV} ({CLIP_SECONDS}s clip, 24kHz mono)")
    result = subprocess.run([
        "ffmpeg", "-y",
        "-i", str(AUDIO_IN),
        "-t", str(CLIP_SECONDS),
        "-ar", "24000",
        "-ac", "1",
        str(AUDIO_WAV),
    ], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr)
        sys.exit(1)


def encode_voice():
    import torch
    from neutts import NeuTTS

    if CODES_OUT.exists():
        print(f"[skip] {CODES_OUT} already exists — delete to re-encode")
        return

    print("[load] NeuTTS (neutts-nano, CPU)...")
    tts = NeuTTS(
        backbone_repo="neuphonic/neutts-nano",
        backbone_device="cpu",
        codec_repo="neuphonic/neucodec",
        codec_device="cpu",
    )

    print(f"[encode] {AUDIO_WAV}")
    ref_codes = tts.encode_reference(str(AUDIO_WAV))

    torch.save(ref_codes, str(CODES_OUT))
    print(f"[saved] {CODES_OUT}  shape={ref_codes.shape}")

    # Quick smoke-test
    print("[test] generating 1-sentence sample...")
    import soundfile as sf
    wav = tts.infer(
        "Hello, my name is Matt. Welcome to today's talk.",
        ref_codes,
        REF_TEXT,
    )
    test_out = VOICES_DIR / "Matt_test.wav"
    sf.write(str(test_out), wav, 24000)
    print(f"[ok] test output -> {test_out}")


if __name__ == "__main__":
    convert_mp3_to_wav()
    encode_voice()
