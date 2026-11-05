"""Lazy, cached, local-only CPU Moonshine adapter for live 16 kHz audio."""
from __future__ import annotations

import os
import threading

import numpy as np


class MoonshineUnavailableError(RuntimeError):
    """Moonshine dependency or locally cached model is unavailable."""


class MoonshineLive:
    """One lazily prepared ONNX model; all model calls are serialized by a lock.

    ``transcribe`` accepts one mono float32 16 kHz ndarray and returns text. The
    default loader is strictly local-only: it never downloads model files.
    """

    def __init__(self, model_name="moonshine/base", *, model_factory=None,
                 transcribe_fn=None):
        self.model_name = model_name
        self._factory = model_factory
        self._transcribe_fn = transcribe_fn
        self._model = None
        self._inference_lock = threading.Lock()

    @staticmethod
    def _model_directory(model_name, hub=None):
        if hub is None:
            try:
                import huggingface_hub as hub
            except ImportError as exc:
                raise MoonshineUnavailableError(
                    "Moonshine requires huggingface-hub and useful-moonshine-onnx"
                ) from exc
        model = model_name.split("/")[-1]
        paths = []
        try:
            for filename in ("encoder_model", "decoder_model_merged"):
                paths.append(hub.hf_hub_download(
                    repo_id="UsefulSensors/moonshine",
                    filename=f"{filename}.onnx",
                    subfolder=f"onnx/merged/{model}/float",
                    local_files_only=True,
                ))
        except Exception as exc:
            raise MoonshineUnavailableError(
                f"Moonshine model {model_name!r} is not available in the local cache"
            ) from exc
        return os.path.dirname(paths[0])

    @staticmethod
    def _load_local_model(model_name, importer=__import__):
        try:
            moonshine_onnx = importer("moonshine_onnx")
            if moonshine_onnx is None:
                raise ImportError("moonshine_onnx unavailable")
        except ImportError as exc:
            raise MoonshineUnavailableError(
                "Moonshine dependency missing; install useful-moonshine-onnx"
            ) from exc
        directory = MoonshineLive._model_directory(model_name)
        try:
            return moonshine_onnx.MoonshineOnnxModel(models_dir=directory,
                                                    model_name=model_name)
        except Exception as exc:
            raise MoonshineUnavailableError(
                f"Could not initialize cached Moonshine model {model_name!r}"
            ) from exc

    def _prepare(self):
        if self._model is None:
            if self._factory is not None:
                self._model = self._factory(self.model_name)
            else:
                self._model = self._load_local_model(self.model_name)
        return self._model

    def prepare(self):
        """Load and validate the cached model now, for startup status reporting."""
        with self._inference_lock:
            model = self._prepare()
            if self._transcribe_fn is None:
                try:
                    import moonshine_onnx
                except ImportError as exc:
                    raise MoonshineUnavailableError(
                        "Moonshine dependency missing; install useful-moonshine-onnx"
                    ) from exc
                if not hasattr(moonshine_onnx, "transcribe"):
                    raise MoonshineUnavailableError("Moonshine package has no transcribe function")
            return model

    def transcribe(self, audio_16k):
        audio = np.asarray(audio_16k, dtype=np.float32)
        if audio.ndim != 1:
            raise ValueError("MoonshineLive expects mono audio")
        if len(audio) < 1600:
            return ""
        with self._inference_lock:
            model = self._prepare()
            fn = self._transcribe_fn
            if fn is None:
                try:
                    import moonshine_onnx
                except ImportError as exc:
                    raise MoonshineUnavailableError(
                        "Moonshine dependency missing; install useful-moonshine-onnx"
                    ) from exc
                fn = moonshine_onnx.transcribe
            result = fn(audio, model)
            return " ".join(result).strip() if isinstance(result, (tuple, list)) else str(result).strip()
