#!/usr/bin/env python3
"""Test HY-MT1.5-1.8B translation model."""

from pathlib import Path
from transformers import AutoModelForCausalLM, AutoTokenizer
import torch

model_path = Path(__file__).parent / "models" / "HY-MT1.5-1.8B"

print("Loading model and tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(str(model_path))

device = "cuda" if torch.cuda.is_available() else "cpu"
dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32

model = AutoModelForCausalLM.from_pretrained(
    str(model_path),
    dtype=dtype,
).to(device)

print(f"✓ Model loaded on device: {model.device}")
print()

def translate(text: str, source_lang: str, target_lang: str) -> str:
    """Translate text using the model."""
    lang_names = {
        "en": "English",
        "vi": "Vietnamese",
        "zh": "Chinese",
        "fr": "French",
    }

    source_name = lang_names.get(source_lang, source_lang)
    target_name = lang_names.get(target_lang, target_lang)

    # Use appropriate prompt template
    if source_lang == "zh" or target_lang == "zh":
        prompt = f"将以下文本翻译为{target_name}，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}"
    else:
        prompt = f"Translate the following segment into {target_name}, without additional explanation.\n\n{text}"

    messages = [{"role": "user", "content": prompt}]

    tokenized_chat = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=False,
        return_tensors="pt",
    )

    outputs = model.generate(
        tokenized_chat.to(model.device),
        max_new_tokens=512,
        top_k=20,
        top_p=0.6,
        repetition_penalty=1.05,
        temperature=0.7,
    )

    output_text = tokenizer.decode(outputs[0])
    # Extract only the translation part (after the prompt)
    translation = output_text.split(text)[-1].strip()
    # Remove placeholder tokens
    translation = translation.replace("<｜hy_place▁holder▁no▁8｜>", "")
    translation = translation.replace("<｜hy_place▁holder▁no▁2｜>", "")
    return translation.strip()

# Test cases
test_cases = [
    ("Hello, how are you?", "en", "vi"),
    ("Xin chào, bạn khỏe không?", "vi", "en"),
    ("It's on the house.", "en", "zh"),
]

print("Testing translations:")
print("=" * 70)

for text, src, tgt in test_cases:
    print(f"\n📝 Source ({src.upper()}): {text}")
    try:
        result = translate(text, src, tgt)
        print(f"🎯 Target ({tgt.upper()}): {result}")
    except Exception as e:
        print(f"❌ Error: {e}")

print("\n" + "=" * 70)
print("✓ Translation model test completed!")
