"""
Chatterbox Turbo TTS sample.
Run: uv run --no-project --with chatterbox-tts python test_chatterbox.py
"""
import torchaudio as ta
from chatterbox.tts import ChatterboxTTS

device = "cpu"  # change to "cuda" or "mps" if available

model = ChatterboxTTS.from_pretrained(device=device)

text = "Hello! This is a Chatterbox TTS test. The model supports zero-shot voice cloning."

# Without reference audio (random voice)
wav = model.generate(text)
ta.save("/tmp/output_chatterbox.wav", wav, model.sr)
print(f"Saved /tmp/output_chatterbox.wav (sr={model.sr})")

# With reference audio (voice cloning) — uncomment and set path
# wav = model.generate(text, audio_prompt_path="your_ref.wav")
# ta.save("/tmp/output_chatterbox_cloned.wav", wav, model.sr)
