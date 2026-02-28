"""Tests for voice channel helpers."""

from __future__ import annotations

import numpy as np

from emergent.channels.voice import (
    VoiceChannel,
    VoiceSettings,
    _load_voice_settings,
    _normalize_audio,
    _prepare_tts_text,
    _resolve_ptt_key,
)


def test_load_voice_settings_defaults() -> None:
    cfg = _load_voice_settings({})
    assert cfg.enabled is False
    assert cfg.ptt_key == "right ctrl"
    assert cfg.ptt_scan_code is None
    assert cfg.sample_rate == 16_000
    assert cfg.stt_language == "es"
    assert cfg.tts_enabled is False
    assert cfg.tts_command == "piper"
    assert cfg.tts_model_path == ""
    assert cfg.ui_beep is False
    assert cfg.vad_mode == 2
    assert cfg.silence_end_ms == 700
    assert cfg.min_speech_ms == 250
    assert cfg.max_utterance_seconds == 20


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


def test_load_voice_settings_vad_bounds() -> None:
    cfg = _load_voice_settings(
        {
            "vad_mode": 8,
            "silence_end_ms": 10,
            "min_speech_ms": 5,
            "max_utterance_seconds": 1,
        }
    )
    assert cfg.vad_mode == 3
    assert cfg.silence_end_ms == 200
    assert cfg.min_speech_ms == 80
    assert cfg.max_utterance_seconds == 3


def test_status_does_not_reset_recording_state() -> None:
    channel = object.__new__(VoiceChannel)
    channel._voice_settings = VoiceSettings(ui_beep=False)
    channel._recording = True
    channel._chunks = [np.array([[0.1]], dtype=np.float32)]
    stream_sentinel = object()
    channel._input_stream = stream_sentinel

    channel._status("ok")

    assert channel._recording is True
    assert len(channel._chunks) == 1
    assert channel._input_stream is stream_sentinel


def test_status_callback_receives_state_updates() -> None:
    captured: list[tuple[str, str]] = []
    channel = object.__new__(VoiceChannel)
    channel._voice_settings = VoiceSettings(ui_beep=False)
    channel._status_callback = lambda state, message: captured.append((state, message))

    channel._status("escuchando", state="listening")

    assert captured == [("listening", "escuchando")]
