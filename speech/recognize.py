"""Recognition preview ONLY: no robot socket, motor control or service manipulation."""
import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import wave

from prepare import ROOT, catalog, verify

COMMANDS = {"前进": "walk", "后退": "backward", "停止": "stop", "坐下": "sit", "起来": "rise"}


def pcm16(data, channels=1, channel=0, gain=1.0):
    import numpy as np
    if len(data) % (2 * channels):
        raise ValueError("Truncated PCM16 frame")
    samples = np.frombuffer(data, dtype="<i2").reshape(-1, channels)[:, channel]
    return np.clip(samples.astype("float32") * (gain / 32768.0), -1., 1.)


class Preview:
    def __init__(self, cooldown=1.5):
        self.cooldown = cooldown
        self.last = {}

    def event(self, keyword, now):
        if keyword not in COMMANDS or now - self.last.get(keyword, -math.inf) < self.cooldown:
            return None
        self.last[keyword] = now
        return {"keyword": keyword, "intent": COMMANDS[keyword], "preview_only": True,
                "monotonic_s": round(now, 3)}


@contextmanager
def alsa_capture(device):
    if os.name != "posix":
        raise RuntimeError("ALSA input requires Linux; use --source sounddevice or --wav on Windows")
    process = subprocess.Popen(["arecord", "-q", "-D", device, "-t", "raw", "-f", "S16_LE",
                                "-r", "16000", "-c", "1"], stdout=subprocess.PIPE)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        process.stdout.close()


def make_spotter(model_dir, spec, threshold, keywords_file=None):
    import sherpa_onnx
    return sherpa_onnx.KeywordSpotter(
        tokens=str(model_dir / spec["tokens"]), encoder=str(model_dir / spec["encoder"]),
        decoder=str(model_dir / spec["decoder"]), joiner=str(model_dir / spec["joiner"]),
        keywords_file=str(keywords_file or model_dir / "keywords.txt"), num_threads=1,
        keywords_threshold=threshold, keywords_score=1.0, num_trailing_blanks=2, provider="cpu",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id")
    parser.add_argument("--source", choices=("alsa", "sounddevice"), default="alsa")
    parser.add_argument("--device", default="plughw:microducki2s3,0", help="ALSA input device")
    parser.add_argument("--input-device", type=int, help="sounddevice microphone index")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--wav", type=Path, help="Mono/stereo 16-bit PCM WAV, no live microphone")
    parser.add_argument("--channel", type=int, default=0, help="WAV channel index (0 based)")
    parser.add_argument("--gain", type=float, default=1.0)
    parser.add_argument("--threshold", type=float, default=0.35)
    parser.add_argument("--seconds", type=float, default=60., help="Bounded live preview duration")
    parser.add_argument("--check", action="store_true", help="Verify files and load KWS, no recording")
    parser.add_argument("--heartbeat", action="store_true", help="Emit live decoded-audio health JSON for the separate action bridge")
    args = parser.parse_args()
    if not (math.isfinite(args.seconds) and args.seconds > 0 and math.isfinite(args.gain)
            and args.gain > 0 and math.isfinite(args.threshold) and 0 < args.threshold <= 1):
        parser.error("Invalid seconds/gain/threshold")
    if args.list_devices:
        import sounddevice
        print(sounddevice.query_devices())
        return 0
    model_id, spec = catalog(args.model_id)
    model_dir = ROOT / "models" / model_id
    verify(model_dir, model_id, spec)
    if (ROOT / "keywords_raw.txt").read_bytes() != (model_dir / "keywords_raw.txt").read_bytes():
        raise ValueError("Keywords changed; run prepare.py again")
    spotter = make_spotter(model_dir, spec, args.threshold)
    print(f"PREVIEW ONLY / model={model_id} / CPU threads=1 / no robot control", file=sys.stderr, flush=True)
    if args.check:
        print("Model files, keywords and KWS load: OK")
        return 0
    preview = Preview()
    stream = spotter.create_stream()
    next_heartbeat = 0.0

    def decode(samples, rate):
        nonlocal next_heartbeat
        stream.accept_waveform(rate, samples)
        while spotter.is_ready(stream):
            spotter.decode_stream(stream)
            result = spotter.get_result(stream)
            if result:
                event = preview.event(result, time.monotonic())
                if event:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                spotter.reset_stream(stream)
        # Only after an audio block has actually passed through the decoder.
        # No timer thread: a stuck capture/decoder must also stop this heartbeat.
        now = time.monotonic()
        if args.heartbeat and now >= next_heartbeat:
            print(json.dumps({"kind": "audio_heartbeat", "monotonic_s": round(now, 3)}), flush=True)
            next_heartbeat = now + 0.25

    if args.wav:
        with wave.open(str(args.wav), "rb") as source:
            channels = source.getnchannels()
            if source.getsampwidth() != 2 or not 0 <= args.channel < channels:
                raise ValueError("Use PCM16 WAV and a valid --channel")
            rate = source.getframerate()
            while data := source.readframes(max(1, rate // 10)):
                decode(pcm16(data, channels, args.channel, args.gain), rate)
            import numpy as np
            decode(np.zeros(rate, dtype="float32"), rate)  # Flush final trailing blanks.
        return 0

    deadline = time.monotonic() + args.seconds
    if args.source == "sounddevice":
        import numpy as np
        import sounddevice
        with sounddevice.InputStream(samplerate=16000, channels=1, dtype="float32", device=args.input_device) as source:
            while time.monotonic() < deadline:
                samples, overflow = source.read(1600)
                if overflow:
                    raise RuntimeError("Microphone overflow; reduce load before testing recognition")
                decode(np.clip(samples.reshape(-1) * args.gain, -1., 1.), 16000)
    else:
        with alsa_capture(args.device) as source:
            pending = b""
            last_data = time.monotonic()
            while time.monotonic() < deadline:
                if not select.select([source.stdout], [], [], 0.2)[0]:
                    if time.monotonic() - last_data > 3.:
                        raise RuntimeError("Microphone stalled; check ALSA device / exclusive owner")
                    continue
                chunk = os.read(source.stdout.fileno(), 3200)
                if not chunk:
                    raise RuntimeError(f"arecord ended ({source.poll()}); check device and mic ownership")
                last_data = time.monotonic()
                pending += chunk
                complete = len(pending) // 2 * 2
                if complete:
                    decode(pcm16(pending[:complete], gain=args.gain), 16000)
                    pending = pending[complete:]
    return 0


if __name__ == "__main__":
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Recognition preview failed: {error}", file=sys.stderr)
        sys.exit(1)
