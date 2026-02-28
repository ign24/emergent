"""Command-driven voice channel for terminal interactions."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import structlog
from faster_whisper import WhisperModel

from emergent.agent.context import ContextBuilder
from emergent.agent.runtime import AgentRuntime
from emergent.config import EmergentSettings
from emergent.memory.store import MemoryStore

logger = structlog.get_logger(__name__)

SESSION_ID = "voice_session"
VoiceStatusCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class VoiceSettings:
    enabled: bool = False
    ptt_key: str = "right ctrl"
    ptt_scan_code: int | None = None
    sample_rate: int = 16_000
    channels: int = 1
    max_record_seconds: int = 15
    stt_model: str = "small"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    stt_language: str = "es"
    tts_enabled: bool = False
    tts_command: str = "piper"
    tts_model_path: str = ""
    tts_speaker_id: int | None = None
    tts_length_scale: float = 1.0
    tts_noise_scale: float = 0.667
    tts_noise_w: float = 0.8
    tts_sentence_silence: float = 0.15
    tts_max_chars: int = 500
    ui_beep: bool = False
    vad_mode: int = 2
    silence_end_ms: int = 700
    min_speech_ms: int = 250
    max_utterance_seconds: int = 20


def _load_voice_settings(cfg: dict[str, Any]) -> VoiceSettings:
    """Parse YAML voice config with safe defaults."""
    speaker_raw = cfg.get("tts_speaker_id")
    speaker_id = int(speaker_raw) if speaker_raw is not None else None
    scan_raw = cfg.get("ptt_scan_code")
    scan_code = int(scan_raw) if scan_raw is not None else None
    return VoiceSettings(
        enabled=bool(cfg.get("enabled", False)),
        ptt_key=str(cfg.get("ptt_key", "right ctrl")).strip().lower(),
        ptt_scan_code=scan_code,
        sample_rate=max(8_000, int(cfg.get("sample_rate", 16_000))),
        channels=max(1, int(cfg.get("channels", 1))),
        max_record_seconds=max(3, int(cfg.get("max_record_seconds", 15))),
        stt_model=str(cfg.get("stt_model", "small")),
        stt_device=str(cfg.get("stt_device", "cpu")),
        stt_compute_type=str(cfg.get("stt_compute_type", "int8")),
        stt_language=str(cfg.get("stt_language", "es")),
        tts_enabled=bool(cfg.get("tts_enabled", False)),
        tts_command=str(cfg.get("tts_command", "piper")).strip() or "piper",
        tts_model_path=str(cfg.get("tts_model_path", "")).strip(),
        tts_speaker_id=speaker_id,
        tts_length_scale=float(cfg.get("tts_length_scale", 1.0)),
        tts_noise_scale=float(cfg.get("tts_noise_scale", 0.667)),
        tts_noise_w=float(cfg.get("tts_noise_w", 0.8)),
        tts_sentence_silence=float(cfg.get("tts_sentence_silence", 0.15)),
        tts_max_chars=max(80, int(cfg.get("tts_max_chars", 500))),
        ui_beep=bool(cfg.get("ui_beep", False)),
        vad_mode=min(3, max(0, int(cfg.get("vad_mode", 2)))),
        silence_end_ms=max(200, int(cfg.get("silence_end_ms", 700))),
        min_speech_ms=max(80, int(cfg.get("min_speech_ms", 250))),
        max_utterance_seconds=max(3, int(cfg.get("max_utterance_seconds", 20))),
    )


def _resolve_ptt_key(ptt_key: str) -> str:
    """Validate and normalize configured key string."""
    key_name = ptt_key.strip().lower()
    if not key_name:
        raise ValueError("ptt_key cannot be empty")
    return key_name


def _prepare_tts_text(text: str, max_chars: int) -> str:
    """Normalize markdown-ish output into short speech-friendly text."""
    plain = text.replace("```", " ").replace("`", " ")
    plain = plain.replace("\n", " ").replace("  ", " ").strip()
    if len(plain) > max_chars:
        plain = plain[:max_chars].rstrip() + "."
    return plain


def _normalize_audio(
    chunks: list[np.ndarray], channels: int, max_samples: int
) -> np.ndarray | None:
    """Merge chunks into a mono float32 array for STT."""
    if not chunks:
        return None

    merged = np.concatenate(chunks, axis=0)
    if channels > 1 and merged.ndim == 2:
        merged = merged.mean(axis=1)
    elif merged.ndim == 2:
        merged = merged[:, 0]

    merged = merged.astype(np.float32, copy=False)
    if len(merged) > max_samples:
        merged = merged[:max_samples]

    return merged


class VoiceChannel:
    """Global PTT voice channel that feeds transcribed text to AgentRuntime."""

    def __init__(
        self,
        settings: EmergentSettings,
        runtime: AgentRuntime,
        store: MemoryStore,
        context_builder: ContextBuilder,
        status_callback: VoiceStatusCallback | None = None,
    ) -> None:
        self._app_settings = settings
        self._voice_settings = _load_voice_settings(settings.voice or {})
        self._ptt_key = _resolve_ptt_key(self._voice_settings.ptt_key)

        self._runtime = runtime
        self._store = store
        self._context_builder = context_builder
        self._running = False

        self._stt_model: WhisperModel | None = None
        self._stt_model_lock = asyncio.Lock()
        self._tts_warned = False
        self._recording = False
        self._chunks: list[np.ndarray] = []
        self._input_stream: Any = None
        self._status_callback = status_callback
        self._continuous_task: asyncio.Task[None] | None = None
        self._continuous_stop = asyncio.Event()
        self._capture_lock = asyncio.Lock()

    def _status(self, message: str, beep: bool = False, state: str | None = None) -> None:
        """Print compact voice state updates in terminal."""
        status_callback = getattr(self, "_status_callback", None)
        line = f"[voice] {message}"
        if beep and self._voice_settings.ui_beep and status_callback is None:
            line = "\a" + line
        if status_callback is None:
            print(line, file=sys.stderr, flush=True)

        if status_callback is not None and state is not None:
            try:
                status_callback(state, message)
            except Exception as e:
                logger.warning("voice_status_callback_failed", error=str(e))

    @property
    def enabled(self) -> bool:
        return self._voice_settings.enabled

    @property
    def continuous_active(self) -> bool:
        task = self._continuous_task
        return task is not None and not task.done()

    async def start(self) -> None:
        """Initialize voice channel for command-driven capture."""
        if not self._voice_settings.enabled:
            logger.info("voice_channel_disabled")
            self._status("canal de voz desactivado", state="off")
            return

        if self._running:
            return

        self._running = True
        self._status("canal de voz iniciando", state="starting")

        logger.info(
            "voice_channel_started",
            ptt_key=self._voice_settings.ptt_key,
            ptt_scan_code=self._voice_settings.ptt_scan_code,
            sample_rate=self._voice_settings.sample_rate,
            stt_model=self._voice_settings.stt_model,
            tts_enabled=self._voice_settings.tts_enabled,
        )
        self._status(
            "modo comando listo (/voice)",
            state="ready",
        )

    async def start_continuous(self) -> None:
        """Enable continuous listen/transcribe/respond mode."""
        if not self._voice_settings.enabled:
            self._status("canal de voz desactivado", state="off")
            return
        if not self._running:
            await self.start()
        if self.continuous_active:
            self._status("modo voz continuo ya activo", state="listening")
            return

        self._continuous_stop.clear()
        self._continuous_task = asyncio.create_task(
            self._continuous_loop(),
            name="voice_continuous_loop",
        )
        self._status("modo voz continuo activo (/voice-off para salir)", state="listening")

    async def stop_continuous(self) -> None:
        """Disable continuous voice mode."""
        self._continuous_stop.set()
        task = self._continuous_task
        self._continuous_task = None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._stop_recording()
        self._status("modo voz continuo desactivado", state="ready")

    async def capture_once(self, duration_seconds: int | None = None) -> None:
        """Capture one utterance via terminal command and process it."""
        if not self._voice_settings.enabled:
            self._status("canal de voz desactivado", state="off")
            return

        if self.continuous_active:
            self._status("modo continuo activo; usa /voice-off primero", state="listening")
            return

        if not self._running:
            await self.start()
        if self._recording:
            return

        requested = (
            duration_seconds
            if duration_seconds is not None
            else self._voice_settings.max_utterance_seconds
        )
        duration = max(1, min(int(requested), self._voice_settings.max_utterance_seconds))

        async with self._capture_lock:
            try:
                await self._start_recording()
                self._status(f"grabando {duration}s... habla ahora", state="listening")
                await asyncio.sleep(duration)
                await self._finalize_utterance()
            except Exception as e:
                logger.error("voice_command_capture_failed", error=str(e))
                self._status("fallo captura de voz", state="error")

    async def _continuous_loop(self) -> None:
        """Capture utterances continuously until mode is disabled."""
        try:
            while self._running and not self._continuous_stop.is_set():
                audio = await self._capture_until_silence()
                if audio is None:
                    continue

                async with self._capture_lock:
                    await self._process_audio(audio)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("voice_continuous_loop_failed", error=str(e))
            self._status("fallo en modo continuo", state="error")

    async def _capture_until_silence(self) -> np.ndarray | None:
        """Capture one utterance using WebRTC VAD start/end detection."""
        if self._continuous_stop.is_set():
            return None

        sample_rate = self._voice_settings.sample_rate
        if sample_rate not in {8000, 16000, 32000, 48000}:
            logger.warning("voice_invalid_vad_sample_rate", sample_rate=sample_rate)
            sample_rate = 16000

        frame_ms = 20
        frame_samples = int(sample_rate * frame_ms / 1000)
        start_frames_needed = max(1, self._voice_settings.min_speech_ms // frame_ms)
        silence_frames_needed = max(1, self._voice_settings.silence_end_ms // frame_ms)
        max_frames = max(1, self._voice_settings.max_utterance_seconds * 1000 // frame_ms)

        try:
            import sounddevice as sd
            import webrtcvad
        except ModuleNotFoundError as e:
            logger.error("voice_dependency_missing", dependency=str(e))
            self._status("faltan dependencias de voz (sounddevice/webrtcvad)", state="error")
            self._continuous_stop.set()
            return None
        except OSError as e:
            logger.error("voice_audio_backend_missing", error=str(e))
            self._status("falta backend de audio (PortAudio). instala libportaudio2", state="error")
            self._continuous_stop.set()
            return None

        vad = webrtcvad.Vad(self._voice_settings.vad_mode)
        speech_started = False
        speech_frames = 0
        silence_frames = 0
        total_frames = 0
        collected = bytearray()
        pre_roll: list[bytes] = []

        self._status("escuchando...", state="listening")

        try:
            with sd.RawInputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=frame_samples,
            ) as stream:
                while self._running and not self._continuous_stop.is_set():
                    frame_bytes, overflowed = await asyncio.to_thread(stream.read, frame_samples)
                    if overflowed:
                        logger.warning("voice_input_overflow")

                    chunk = bytes(frame_bytes)
                    is_speech = vad.is_speech(chunk, sample_rate)
                    total_frames += 1

                    if not speech_started:
                        pre_roll.append(chunk)
                        if len(pre_roll) > 10:
                            pre_roll.pop(0)

                        if is_speech:
                            speech_frames += 1
                        else:
                            speech_frames = 0

                        if speech_frames >= start_frames_needed:
                            speech_started = True
                            for frame in pre_roll:
                                collected.extend(frame)
                            pre_roll.clear()
                        continue

                    collected.extend(chunk)
                    if is_speech:
                        silence_frames = 0
                    else:
                        silence_frames += 1

                    if silence_frames >= silence_frames_needed or total_frames >= max_frames:
                        break
        except Exception as e:
            logger.error("voice_input_stream_failed", error=str(e))
            if "PortAudio" in str(e):
                self._status("PortAudio no disponible. instala libportaudio2", state="error")
            else:
                self._status(
                    "no pude abrir el microfono (revisa permisos/dispositivo)", state="error"
                )
            self._continuous_stop.set()
            return None

        if not speech_started or not collected:
            return None

        audio = np.frombuffer(bytes(collected), dtype=np.int16).astype(np.float32) / 32768.0
        if len(audio) < sample_rate // 4:
            return None
        return audio

    async def stop(self) -> None:
        """Stop channel and release resources."""
        await self.stop_continuous()
        self._running = False

        await self._stop_recording()
        logger.info("voice_channel_stopped")
        self._status("canal de voz detenido", state="stopped")

    async def _start_recording(self) -> None:
        """Start microphone recording while key is held."""
        if self._recording:
            return

        self._chunks = []
        self._recording = True
        import sounddevice as sd

        loop = asyncio.get_running_loop()

        def _callback(indata: np.ndarray, frames: int, t: Any, status: Any) -> None:
            del frames, t
            if status:
                logger.warning("voice_input_status", status=str(status))
            loop.call_soon_threadsafe(self._chunks.append, indata.copy())

        self._input_stream = sd.InputStream(
            samplerate=self._voice_settings.sample_rate,
            channels=self._voice_settings.channels,
            dtype="float32",
            callback=_callback,
        )
        try:
            self._input_stream.start()
            logger.info("voice_recording_started")
            self._status("escuchando...", beep=True, state="listening")
        except Exception:
            self._recording = False
            self._input_stream.close()
            self._input_stream = None
            raise

    async def _stop_recording(self) -> None:
        """Stop and dispose the active input stream."""
        self._recording = False
        if self._input_stream is None:
            return

        try:
            self._input_stream.stop()
            self._input_stream.close()
        finally:
            self._input_stream = None
            self._status("microfono liberado", beep=True, state="ready")

    async def _finalize_utterance(self) -> None:
        """Stop recording, transcribe, and run the agent."""
        if not self._recording:
            return

        await self._stop_recording()
        max_samples = self._voice_settings.sample_rate * self._voice_settings.max_utterance_seconds
        audio = _normalize_audio(self._chunks, self._voice_settings.channels, max_samples)
        self._chunks = []

        await self._process_audio(audio)

    async def _process_audio(self, audio: np.ndarray | None) -> None:
        """Transcribe and run agent for one normalized audio utterance."""

        if audio is None or len(audio) < self._voice_settings.sample_rate // 4:
            logger.info("voice_ignored_short_audio")
            return

        self._status("transcribiendo...", state="transcribing")
        transcript = await self._transcribe(audio)
        if not transcript:
            logger.info("voice_empty_transcript")
            self._status("no te escuche claro, proba de nuevo", state="ready")
            return

        logger.info("voice_transcript_ready", transcript_preview=transcript[:80])
        self._status("transcripcion lista, procesando...", state="thinking")
        await self._run_agent_for_transcript(transcript)

    async def _ensure_stt_model(self) -> WhisperModel:
        """Lazily load and cache the STT model."""
        if self._stt_model is not None:
            return self._stt_model

        async with self._stt_model_lock:
            if self._stt_model is None:
                self._stt_model = await asyncio.to_thread(
                    WhisperModel,
                    self._voice_settings.stt_model,
                    device=self._voice_settings.stt_device,
                    compute_type=self._voice_settings.stt_compute_type,
                )
                logger.info(
                    "voice_stt_model_loaded",
                    model=self._voice_settings.stt_model,
                    device=self._voice_settings.stt_device,
                    compute_type=self._voice_settings.stt_compute_type,
                )
        return self._stt_model

    async def _transcribe(self, audio: np.ndarray) -> str:
        """Transcribe audio to text using faster-whisper."""
        model = await self._ensure_stt_model()

        def _transcribe_sync() -> str:
            segments, _info = model.transcribe(
                audio,
                language=self._voice_settings.stt_language,
                vad_filter=True,
                beam_size=5,
            )
            parts = [segment.text.strip() for segment in segments if segment.text.strip()]
            return " ".join(parts).strip()

        return await asyncio.to_thread(_transcribe_sync)

    async def _run_agent_for_transcript(self, user_text: str) -> None:
        """Run agent turn from transcript and persist outputs."""
        t0 = time.monotonic()
        try:
            self._status("pensando...", state="thinking")
            profile_text, memories, summary, history = await self._context_builder.build_context(
                session_id=SESSION_ID,
                current_query=user_text,
            )
            response_text, trace_data = await self._runtime.run(
                user_message=user_text,
                session_id=SESSION_ID,
                history=history,
                user_profile=profile_text,
                semantic_memories=memories,
                session_summary=summary,
                confirm_callback=self._confirm,
            )
        except Exception as e:
            logger.error("voice_runtime_error", error=str(e))
            self._status("fallo al ejecutar runtime", state="error")
            return

        elapsed = time.monotonic() - t0
        logger.info(
            "voice_response", elapsed_s=round(elapsed, 2), response_preview=response_text[:120]
        )
        print(f"\n[voice] you: {user_text}")
        print(f"[voice] emergent: {response_text}\n")
        self._status("respuesta lista", state="responded")
        await self._speak_response(response_text)

        try:
            await self._store.save_conversation_turn(
                SESSION_ID,
                "user",
                user_text,
                model=self._settings.agent.model,
            )
            await self._store.save_conversation_turn(
                SESSION_ID,
                "assistant",
                response_text,
                model=self._settings.agent.model,
            )
            await self._store.save_trace(trace_data)
            asyncio.create_task(
                self._context_builder._retriever.upsert_session(
                    session_id=SESSION_ID,
                    turns=[
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": response_text},
                    ],
                )
            )
        except Exception as e:
            logger.error("voice_persistence_failed", error=str(e))

    async def _speak_response(self, response_text: str) -> None:
        """Speak assistant response using local Piper TTS when enabled."""
        if not self._voice_settings.tts_enabled:
            self._status("listo para siguiente turno", state="ready")
            return

        model_path = self._voice_settings.tts_model_path
        if not model_path:
            if not self._tts_warned:
                logger.warning("voice_tts_disabled_missing_model_path")
                self._tts_warned = True
            self._status("TTS desactivado (falta modelo)", state="ready")
            return

        text = _prepare_tts_text(response_text, self._voice_settings.tts_max_chars)
        if not text:
            self._status("listo para siguiente turno", state="ready")
            return

        try:
            self._status("hablando...", state="speaking")
            await asyncio.to_thread(self._speak_with_piper, text)
            self._status("fin de voz", state="ready")
        except Exception as e:
            logger.error("voice_tts_failed", error=str(e))
            self._status("fallo TTS (reviso logs)", state="error")
            self._status("listo para siguiente turno", state="ready")

    def _speak_with_piper(self, text: str) -> None:
        """Run Piper CLI and play generated WAV through default speaker."""
        import sounddevice as sd

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            wav_path = tmp_wav.name

        try:
            cmd = [
                self._voice_settings.tts_command,
                "-m",
                self._voice_settings.tts_model_path,
                "-f",
                wav_path,
                "--length-scale",
                str(self._voice_settings.tts_length_scale),
                "--noise-scale",
                str(self._voice_settings.tts_noise_scale),
                "--noise-w",
                str(self._voice_settings.tts_noise_w),
                "--sentence-silence",
                str(self._voice_settings.tts_sentence_silence),
            ]
            if self._voice_settings.tts_speaker_id is not None:
                cmd.extend(["-s", str(self._voice_settings.tts_speaker_id)])

            subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, check=True)

            with wave.open(wav_path, "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_rate = wav_file.getframerate()
                sample_width = wav_file.getsampwidth()
                frames = wav_file.readframes(wav_file.getnframes())

            if sample_width != 2:
                raise RuntimeError(f"Unsupported sample width from Piper: {sample_width}")

            audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            if channels > 1:
                audio = audio.reshape(-1, channels)

            sd.play(audio, sample_rate, blocking=True)
        finally:
            if os.path.exists(wav_path):
                os.remove(wav_path)

    async def _confirm(self, tool_name: str, command_preview: str) -> bool:
        """Terminal confirmation fallback for TIER_2 actions from voice."""
        loop = asyncio.get_running_loop()
        timeout = self._app_settings.agent.CONFIRMATION_TIMEOUT_SECONDS
        prompt = (
            "\n[voice] Confirmacion requerida\n"
            f"Tool: {tool_name}\n"
            f"Comando: {command_preview}\n"
            "Permitir? [y/N] > "
        )
        try:
            answer = await asyncio.wait_for(
                loop.run_in_executor(None, input, prompt), timeout=timeout
            )
        except TimeoutError:
            return False
        except (EOFError, asyncio.CancelledError):
            return False
        return answer.strip().lower() in ("y", "yes")
