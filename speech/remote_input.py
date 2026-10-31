"""robotd-owned speech adapter: JSON on stdin/stdout, never a robot socket."""
import sys

if "SpeechInput" not in globals():
    from keyboard_input import SpeechInput, finite
    import json
    import select
    import time


STARTUP_TIMEOUT = 90.0


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
            "ready": ready,
            "reason": self.speech.reason if ready else "正在加载语音模型，等待麦克风音频",
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
            if speech.failed:
                raise RuntimeError(speech.status)
            if heard_audio and not result["ready"]:
                raise RuntimeError("麦克风音频心跳中断，语音控制已停止")
            if not heard_audio and now-began > STARTUP_TIMEOUT:
                raise RuntimeError(f"语音模型启动超时（{STARTUP_TIMEOUT:g} 秒），请检查模型加载、麦克风和 CPU 温度")
            print(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as error:
        import traceback
        traceback.print_exc()
        print(json.dumps({"generation": context.get("generation", 0), "drive": None,
                          "action": None, "ready": False, "failed": True,
                          "reason": str(error)}, ensure_ascii=False), flush=True)
        return 1
    finally:
        speech.close()


if __name__ == "__main__":
    sys.exit(main())
