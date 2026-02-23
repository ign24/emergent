"""Quick voice environment diagnostics for local setup."""

from __future__ import annotations


def main() -> int:
    print("[voice-check] starting")

    try:
        import webrtcvad  # noqa: F401
    except Exception as exc:
        print(f"[voice-check] FAIL webrtcvad import: {exc}")
        return 1
    print("[voice-check] OK webrtcvad import")

    try:
        import sounddevice as sd
    except Exception as exc:
        print(f"[voice-check] FAIL sounddevice import: {exc}")
        return 1
    print("[voice-check] OK sounddevice import")

    try:
        devices = sd.query_devices()
    except Exception as exc:
        print(f"[voice-check] FAIL query devices: {exc}")
        return 1

    if not devices:
        print("[voice-check] FAIL no audio devices found")
        return 1

    input_devices = [d for d in devices if d.get("max_input_channels", 0) > 0]
    print(f"[voice-check] devices total={len(devices)} input={len(input_devices)}")

    if not input_devices:
        print("[voice-check] FAIL no input microphone detected")
        return 1

    default_input = sd.default.device[0] if sd.default.device else None
    print(f"[voice-check] default input index={default_input}")
    print("[voice-check] OK environment ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
