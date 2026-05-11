#!/usr/bin/env python3
"""Test tiny-tts Python package (backtracking/tiny-tts from HuggingFace)."""

import os
import wave

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs", "tiny_tts")
os.makedirs(OUTPUT_DIR, exist_ok=True)

from tiny_tts import TinyTTS

TEST_TEXTS = [
    "Hello world",
    "The quick brown fox jumps over the lazy dog",
]

_tts = None


def get_tts():
    global _tts
    if _tts is None:
        print("  Loading TinyTTS model (downloads from HF if needed)...")
        _tts = TinyTTS(device="cpu")
    return _tts


def validate_wav(path: str) -> dict:
    with wave.open(path, "rb") as w:
        return {
            "channels": w.getnchannels(),
            "framerate": w.getframerate(),
            "duration_s": w.getnframes() / w.getframerate(),
        }


def out(name: str) -> str:
    return os.path.join(OUTPUT_DIR, name)


def test_basic_synthesis():
    print("=== test_basic_synthesis ===")
    tts = get_tts()
    for i, text in enumerate(TEST_TEXTS):
        path = out(f"basic_{i}.wav")
        tts.speak(text, output_path=path)
        info = validate_wav(path)
        size = os.path.getsize(path)
        print(f"  [{text[:30]}] -> {info['duration_s']:.2f}s, {info['framerate']}Hz, {size} bytes  ({path})")
        assert size > 44, "WAV too small"
        assert info["duration_s"] > 0.1, "Audio too short"
    print("  PASS")


def test_speaker_variants():
    print("=== test_speaker_variants ===")
    tts = get_tts()
    text = "Testing speaker voices"
    for speaker in ("MALE",):
        path = out(f"speaker_{speaker.lower()}.wav")
        tts.speak(text, output_path=path, speaker=speaker)
        info = validate_wav(path)
        print(f"  speaker={speaker} -> {info['duration_s']:.2f}s  ({path})")
        assert info["duration_s"] > 0.1
    print("  PASS")


def test_speed_variants():
    print("=== test_speed_variants ===")
    tts = get_tts()
    text = "Speed test sentence for timing"
    durations = {}
    for speed in (0.75, 1.0, 1.5):
        path = out(f"speed_{str(speed).replace('.', '_')}.wav")
        tts.speak(text, output_path=path, speed=speed)
        info = validate_wav(path)
        durations[speed] = info["duration_s"]
        print(f"  speed={speed} -> {info['duration_s']:.2f}s  ({path})")
    assert durations[0.75] > durations[1.5], "Speed scaling not working"
    print("  PASS")


def test_output_file_created():
    print("=== test_output_file_created ===")
    tts = get_tts()
    path = out("output_file_test.wav")
    tts.speak("Output file test", output_path=path)
    assert os.path.exists(path)
    size = os.path.getsize(path)
    print(f"  output size: {size} bytes  ({path})")
    assert size > 44
    print("  PASS")


def test_normalization_disabled():
    print("=== test_normalization_disabled ===")
    tts = get_tts()
    path = out("no_norm.wav")
    tts.speak("Hello world", output_path=path, use_advanced_normalization=False)
    info = validate_wav(path)
    print(f"  no-norm -> {info['duration_s']:.2f}s  ({path})")
    assert info["duration_s"] > 0.1
    print("  PASS")


if __name__ == "__main__":
    tests = [
        test_basic_synthesis,
        test_speaker_variants,
        test_speed_variants,
        test_output_file_created,
        test_normalization_disabled,
    ]
    failed = []
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"  FAIL: {e}")
            failed.append(t.__name__)

    print()
    if failed:
        print(f"FAILED: {failed}")
        raise SystemExit(1)
    else:
        print(f"All {len(tests)} tests passed.")
        print(f"Output files in: {OUTPUT_DIR}")
