"""
Chatterbox Multilingual TTS — all 23 supported languages.
Run: cd /tmp && uv run --no-project --with chatterbox-tts python /home/minh-ngu/VieNeu-TTS/test_chatterbox_multilang.py
"""
import torchaudio as ta
from chatterbox.mtl_tts import ChatterboxMultilingualTTS

device = "cpu"  # change to "cuda" or "mps" if available
OUT_DIR = "/tmp/chatterbox_langs"

import os
os.makedirs(OUT_DIR, exist_ok=True)

SAMPLES = {
    "ar": "مرحباً، هذا اختبار لنظام تحويل النص إلى كلام.",
    "da": "Hej, dette er en test af tekst-til-tale systemet.",
    "de": "Hallo, dies ist ein Test des Text-zu-Sprache-Systems.",
    "el": "Γεια σας, αυτή είναι μια δοκιμή του συστήματος μετατροπής κειμένου σε ομιλία.",
    "en": "Hello, this is a test of the text-to-speech system.",
    "es": "Hola, esta es una prueba del sistema de texto a voz.",
    "fi": "Hei, tämä on tekstistä puheeksi -järjestelmän testi.",
    "fr": "Bonjour, ceci est un test du système de synthèse vocale.",
    "he": "שלום, זהו בדיקה של מערכת המרת טקסט לדיבור.",
    "hi": "नमस्ते, यह टेक्स्ट-टू-स्पीच सिस्टम का परीक्षण है।",
    "it": "Ciao, questo è un test del sistema di sintesi vocale.",
    "ja": "こんにちは、これはテキスト読み上げシステムのテストです。",
    "ko": "안녕하세요, 이것은 텍스트 음성 변환 시스템 테스트입니다.",
    "ms": "Helo, ini adalah ujian sistem teks ke pertuturan.",
    "nl": "Hallo, dit is een test van het tekst-naar-spraak systeem.",
    "no": "Hei, dette er en test av tekst-til-tale systemet.",
    "pl": "Cześć, to jest test systemu zamiany tekstu na mowę.",
    "pt": "Olá, este é um teste do sistema de síntese de fala.",
    "ru": "Привет, это тест системы преобразования текста в речь.",
    "sv": "Hej, detta är ett test av text-till-tal-systemet.",
    "sw": "Habari, hii ni majaribio ya mfumo wa kubadilisha maandishi kuwa sauti.",
    "tr": "Merhaba, bu bir metinden konuşmaya sistemi testidir.",
    "zh": "你好，这是文字转语音系统的测试。",
}

AUDIO_PROMPT = None  # set to path of a 5-10s .wav for voice cloning, or leave None

model = ChatterboxMultilingualTTS.from_pretrained(device=device)
print(f"Model loaded. Testing {len(SAMPLES)} languages...\n")

if AUDIO_PROMPT is None:
    # Generate a short silent ref wav if no prompt provided
    import torch
    import tempfile
    silence = torch.zeros(1, model.sr * 5)  # 5s silence
    ref_path = tempfile.mktemp(suffix=".wav")
    ta.save(ref_path, silence, model.sr)
    print(f"No audio prompt set — using silence as ref (quality will be poor). Set AUDIO_PROMPT for real voice cloning.\n")
else:
    ref_path = AUDIO_PROMPT

failed = []
for lang, text in SAMPLES.items():
    out_path = f"{OUT_DIR}/{lang}.wav"
    try:
        wav = model.generate(text, language_id=lang, audio_prompt_path=ref_path)
        ta.save(out_path, wav, model.sr)
        print(f"[OK] {lang} → {out_path}")
    except Exception as e:
        print(f"[FAIL] {lang}: {e}")
        failed.append(lang)

print(f"\nDone. {len(SAMPLES) - len(failed)}/{len(SAMPLES)} succeeded.")
if failed:
    print(f"Failed: {failed}")
