#!/usr/bin/env python3
"""Test HY-MT1.5-1.8B translation model."""

from transformers import AutoTokenizer, AutoModelForCausalLM

model_path = "downloads/models/HY-MT1.5-1.8B"
print(f"Loading model from {model_path}...")

tokenizer = AutoTokenizer.from_pretrained(model_path)
model = AutoModelForCausalLM.from_pretrained(model_path)

print(f"Model loaded: {model.__class__.__name__}")

# Test 1: English to Chinese
test_en = "It's on the house."
print(f"\n=== Test 1: English → Chinese ===")
print(f"Input: {test_en}")

messages = [
    {"role": "user", "content": f"Translate the following segment into Chinese, without additional explanation.\n\n{test_en}"},
]
tokenized = tokenizer.apply_chat_template(
    messages,
    tokenize=True,
    add_generation_prompt=False,
    return_tensors="pt"
)

outputs = model.generate(
    tokenized.to(model.device),
    max_new_tokens=512,
    top_k=20,
    top_p=0.6,
    temperature=0.7,
    repetition_penalty=1.05
)
output_text = tokenizer.decode(outputs[0], skip_special_tokens=True)
print(f"Output: {output_text}")

# Test 2: Vietnamese to English
test_vi = "Xin chào"
print(f"\n=== Test 2: Vietnamese → English ===")
print(f"Input: {test_vi}")

messages = [
    {"role": "user", "content": f"Translate the following segment into English, without additional explanation.\n\n{test_vi}"},
]
tokenized = tokenizer.apply_chat_template(
    messages,
    tokenize=True,
    add_generation_prompt=False,
    return_tensors="pt"
)

outputs = model.generate(
    tokenized.to(model.device),
    max_new_tokens=512,
    top_k=20,
    top_p=0.6,
    temperature=0.7,
    repetition_penalty=1.05
)
output_text = tokenizer.decode(outputs[0], skip_special_tokens=True)
print(f"Output: {output_text}")

print("\nTest passed!")
