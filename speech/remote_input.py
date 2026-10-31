"""robotd-owned speech adapter: JSON on stdin/stdout, never a robot socket."""
import sys

if "SpeechInput" not in globals():
    from keyboard_input import SpeechInput, finite
    import json
    import select
    import time


class RemoteInput:
    def __init__(self, speech):
        self.speech = speech
        self.generation = None
        self.direction = None

    def step(self, context, packets, now):
        generation = context.get("generation", 0)
        manual = self.generation is not None and generation != self.generation
        if manual:
            self.speech.cancel()
            self.direction = None
        self.generation = generation
        state = context.get("state")
        age = context.get("age")
        velocity = state.get("move", {}).get("requested", [0., 0., 0.]) if state else [0., 0., 0.]
        action = self.speech.decide(
            packets, now, manual_input=manual, enabled=context.get("enabled") is True,
            mode=context.get("mode", "unknown"), state=state,
            state_at=now-age if finite(age) and age >= 0 else None, velocity=velocity,
        )
        if action in ("walk", "backward"):
            self.direction = action
        if not self.speech.owns_walk:
            self.direction = None
        ready = (not self.speech.failed and self.speech.audio_at is not None
                 and 0 <= now-self.speech.audio_at <= .8)
        return {
            "generation": generation, "drive": self.direction,
            "action": action if action in ("stop", "stop_for_sit", "sit", "rise") else None,
            "ready": ready, "reason": self.speech.reason,
        }


def main():
    import signal

    # robotd starts this process and its recognizer in one process group.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    speech = SpeechInput("on", sys.argv[1], report=lambda text: print(text, file=sys.stderr, flush=True))
    adapter = RemoteInput(speech)
    context, received = {}, time.monotonic()
    began = received
    heard_audio = False
    try:
        speech.start()
        while True:
            if select.select([sys.stdin], [], [], .1)[0]:
                line = sys.stdin.readline()
                if not line:
                    break
                context = json.loads(line)
                received = time.monotonic()
            now = time.monotonic()
            current = dict(context)
            if finite(current.get("age")):
                current["age"] += now-received
            result = adapter.step(current, speech.poll(), now)
            heard_audio |= result["ready"]
            if speech.failed or (heard_audio and not result["ready"]) or (not heard_audio and now-began > 20):
                raise RuntimeError("麦克风音频中断或识别未就绪，请查看 robotd 日志")
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        speech.close()


if __name__ == "__main__":
    main()
