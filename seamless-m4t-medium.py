import re
import sys
import argparse
import torch
import torchaudio
from transformers import AutoProcessor, SeamlessM4TModel
from datetime import timedelta

# =========================
# CONFIG
# =========================
_parser = argparse.ArgumentParser()
_parser.add_argument("--input",  default="/home/minh-ngu/Downloads/audio.wav")
_parser.add_argument("--output", default="output_french.srt")
_args = _parser.parse_args()

AUDIO_PATH = _args.input
OUTPUT_SRT = _args.output

MODEL_NAME = "facebook/hf-seamless-m4t-medium"

SAMPLE_RATE = 16000
MIN_SILENCE_LEN = 0.6        # seconds of silence to split
SILENCE_THRESHOLD = 0.01     # lower = more sensitive
SOURCE_LANG = "eng"
TARGET_LANG = "fra"
MAX_SEGMENT_SECONDS = 4
MAX_SEGMENT_SAMPLES = MAX_SEGMENT_SECONDS * SAMPLE_RATE
# =========================


def format_timestamp(seconds: float):
    td = timedelta(seconds=float(seconds))
    total_seconds = int(td.total_seconds())
    milliseconds = int((seconds - total_seconds) * 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


print("Loading model (CPU)...")
processor = AutoProcessor.from_pretrained(MODEL_NAME)
model = SeamlessM4TModel.from_pretrained(
    MODEL_NAME,
    tie_word_embeddings=False
)
model.eval()


print("Loading audio...")
audio, orig_freq = torchaudio.load(AUDIO_PATH)

# Mono
if audio.shape[0] > 1:
    audio = torch.mean(audio, dim=0, keepdim=True)

# Resample
if orig_freq != SAMPLE_RATE:
    audio = torchaudio.functional.resample(audio, orig_freq, SAMPLE_RATE)

audio = audio.squeeze()

print("Detecting silence segments...")

# Compute absolute amplitude
amplitude = torch.abs(audio)

silence_samples = int(MIN_SILENCE_LEN * SAMPLE_RATE)
segments = []

start = 0
silence_count = 0

for i in range(len(amplitude)):

    # Detect silence
    if amplitude[i] < SILENCE_THRESHOLD:
        silence_count += 1
    else:
        silence_count = 0

    current_length = i - start

    # 1️⃣ Split on silence
    if silence_count >= silence_samples:
        end = i - silence_samples
        if end > start:
            segments.append((start, end))
        start = i
        silence_count = 0

    # 2️⃣ Force split if too long (15s max)
    elif current_length >= MAX_SEGMENT_SAMPLES:
        end = i
        segments.append((start, end))
        start = i
        silence_count = 0

# Add last segment
if start < len(audio):
    segments.append((start, len(audio)))

print(f"Detected {len(segments)} speech segments")

# =========================
# Translate each segment
# =========================
subtitle_index = 1
srt_entries = []

total_segments = len(segments)
for seg_idx, (start_sample, end_sample) in enumerate(segments, 1):
    print(f"Translating segment {seg_idx}/{total_segments}...", flush=True)

    segment_audio = audio[start_sample:end_sample]

    if len(segment_audio) < SAMPLE_RATE * 0.5:
        continue

    inputs = processor(
        audio=segment_audio.numpy(),
        sampling_rate=SAMPLE_RATE,
        return_tensors="pt"
    )

    with torch.no_grad():
        output_tokens = model.generate(
            **inputs,
            tgt_lang=TARGET_LANG,
            generate_speech=False
        )

    french_text = processor.decode(output_tokens[0].tolist()[0], skip_special_tokens=True).strip()

    if not french_text:
        continue

    start_time = start_sample / SAMPLE_RATE
    end_time = end_sample / SAMPLE_RATE

    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', french_text) if s.strip()]
    duration = end_time - start_time
    time_per_sentence = duration / len(sentences)

    for j, sentence in enumerate(sentences):
        s_start = start_time + j * time_per_sentence
        s_end = start_time + (j + 1) * time_per_sentence
        srt_entries.append(
            f"{subtitle_index}\n"
            f"{format_timestamp(s_start)} --> {format_timestamp(s_end)}\n"
            f"{sentence}\n"
        )
        subtitle_index += 1

print("Writing SRT file...")
with open(OUTPUT_SRT, "w", encoding="utf-8") as f:
    f.write("\n".join(srt_entries))

print("Done ✅")
print("Subtitles saved to:", OUTPUT_SRT)