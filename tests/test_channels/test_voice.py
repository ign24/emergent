"""Tests for voice channel helpers."""

from __future__ import annotations

import numpy as np

from emergent.channels.voice import (
    _load_voice_settings,
    _normalize_audio,
    _prepare_tts_text,
    _resolve_ptt_key,
)


def test_load_voice_settings_defaults() -> None:
    cfg = _load_voice_settings({})
    assert cfg.enabled is False
    assert cfg.ptt_key == "f24"
    assert cfg.ptt_scan_code is None
    assert cfg.sample_rate == 16_000
    assert cfg.stt_language == "es"
    assert cfg.tts_enabled is False
    assert cfg.tts_command == "piper"
    assert cfg.tts_model_path == ""
    assert cfg.ui_beep is False


def test_resolve_ptt_key_empty_invalid() -> None:
    try:
        _resolve_ptt_key("  ")
    except ValueError as e:
        assert "ptt_key cannot be empty" in str(e)
    else:
        raise AssertionError("Expected ValueError for empty ptt_key")


def test_normalize_audio_mono_limit() -> None:
    chunks = [
        np.array([[0.1], [0.2], [0.3]], dtype=np.float32),
        np.array([[0.4], [0.5]], dtype=np.float32),
    ]
    audio = _normalize_audio(chunks, channels=1, max_samples=4)
    assert audio is not None
    assert audio.shape == (4,)
    assert np.allclose(audio, np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32))


def test_normalize_audio_stereo_to_mono() -> None:
    chunks = [
        np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
    ]
    audio = _normalize_audio(chunks, channels=2, max_samples=10)
    assert audio is not None
    assert np.allclose(audio, np.array([0.5, 0.5], dtype=np.float32))


def test_prepare_tts_text_truncates_and_strips_markdown() -> None:
    raw = "Hola `mundo`\n```python\nprint('x')\n```"
    text = _prepare_tts_text(raw, max_chars=14)
    assert "`" not in text
    assert "\n" not in text
    assert len(text) <= 15


def test_load_voice_settings_scan_code() -> None:
    cfg = _load_voice_settings({"ptt_scan_code": 115})
    assert cfg.ptt_scan_code == 115
