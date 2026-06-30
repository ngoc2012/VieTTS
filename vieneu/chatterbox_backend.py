import os
import tempfile

import numpy as np
import torch


class ChatterboxBackend:
    """Adapts chatterbox API to match VieNeuTTS interface."""

    def __init__(self, model, language_id=None):
        self._model = model
        self.language_id = language_id
        self.sample_rate = model.sr

    def encode_reference(self, audio_path: str):
        import torchaudio
        wav, sr = torchaudio.load(audio_path)
        return (wav, sr)

    def infer(self, text, ref_codes=None, ref_text=None, temperature=0.8):
        kwargs = {}
        if self.language_id is not None:
            kwargs["language_id"] = self.language_id

        tmp_path = None
        if ref_codes is not None:
            import torchaudio
            wav, sr = ref_codes
            tmp_path = tempfile.mktemp(suffix=".wav")
            torchaudio.save(tmp_path, wav, sr)
            kwargs["audio_prompt_path"] = tmp_path

        try:
            result = self._model.generate(text, **kwargs)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        if isinstance(result, torch.Tensor):
            return result.squeeze().cpu().numpy()
        return np.array(result)

    def list_preset_voices(self):
        return []

    def get_preset_voice(self, voice_id):
        return {"codes": None, "text": ""}


def make_chatterbox(backbone_cfg: dict, device: str) -> ChatterboxBackend:
    backend = backbone_cfg.get("backend")
    language_id = backbone_cfg.get("language_id")
    if backend == "chatterbox_mtl":
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        model = ChatterboxMultilingualTTS.from_pretrained(device=device)
    else:
        from chatterbox.tts import ChatterboxTTS
        model = ChatterboxTTS.from_pretrained(device=device)
    return ChatterboxBackend(model, language_id=language_id)
