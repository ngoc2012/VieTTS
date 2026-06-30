"""Test audio generation with VoxCPM2 (openbmb/VoxCPM2, 2B params, 48kHz).

Install: pip install voxcpm soundfile
"""

from voxcpm import VoxCPM
import soundfile as sf

MODEL_ID = "openbmb/VoxCPM2"

print("Loading model...")
model = VoxCPM.from_pretrained(MODEL_ID, load_denoiser=False)

# 1. Basic TTS
print("Generating basic TTS...")
wav = model.generate(
    text="Xin chào, đây là bài kiểm tra tổng hợp giọng nói tiếng Việt.",
    cfg_value=2.0,
    inference_timesteps=10,
)
sf.write("test_tts.wav", wav, model.tts_model.sample_rate)
print("Saved test_tts.wav")

# 2. Voice design (no reference audio needed)
print("Generating voice design...")
wav = model.generate(
    text="(A young Vietnamese woman, clear and expressive voice)Xin chào, tôi là trợ lý giọng nói.",
    cfg_value=2.0,
    inference_timesteps=10,
)
sf.write("test_voice_design.wav", wav, model.tts_model.sample_rate)
print("Saved test_voice_design.wav")

# 3. Voice cloning (requires a reference wav — skip if not available)
import os
REF_WAV = "reference.wav"
if os.path.exists(REF_WAV):
    print(f"Generating voice clone from {REF_WAV}...")
    wav = model.generate(
        text="Đây là giọng nói được sao chép từ âm thanh tham khảo.",
        reference_wav_path=REF_WAV,
        cfg_value=2.0,
        inference_timesteps=10,
    )
    sf.write("test_clone.wav", wav, model.tts_model.sample_rate)
    print("Saved test_clone.wav")
else:
    print(f"Skipping voice cloning — place a reference.wav to enable it")

print("Done.")
