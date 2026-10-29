"""Optional real CPU model test on the upstream 'France' sample, no recording."""
import io
import importlib.util
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare
import recognize

MODEL_ID, SPEC = prepare.catalog()
MODEL = prepare.ROOT / "models" / MODEL_ID
ARCHIVE = prepare.ROOT / "downloads" / (SPEC["name"] + ".tar.bz2")


@unittest.skipUnless(importlib.util.find_spec("sherpa_onnx") and ARCHIVE.is_file()
                     and (MODEL / "manifest.json").is_file(), "Run speech setup first")
class ModelSmokeTests(unittest.TestCase):
    def test_official_known_phrase_and_no_robot_control(self):
        import numpy as np
        from sherpa_onnx import text2token
        prepare.verify(MODEL, MODEL_ID, SPEC)
        with tempfile.TemporaryDirectory() as temporary:
            keywords = Path(temporary) / "keywords.txt"
            tokens = text2token(["法国"], tokens=str(MODEL / SPEC["tokens"]),
                                tokens_type="phone+ppinyin", lexicon=str(MODEL / SPEC["lexicon"]))[0]
            keywords.write_text(" ".join(tokens) + " @法国\n", encoding="utf-8")
            spotter = recognize.make_spotter(MODEL, SPEC, .25, keywords)
            stream = spotter.create_stream()
            with tarfile.open(ARCHIVE) as archive:
                raw = archive.extractfile(SPEC["name"] + "/test_wavs/zh_3.wav").read()
            with wave.open(io.BytesIO(raw)) as source:
                rate = source.getframerate()
                self.assertEqual(source.getsampwidth(), 2)
                samples = recognize.pcm16(source.readframes(source.getnframes()), source.getnchannels())
            samples = np.concatenate([samples, np.zeros(rate, dtype="float32")])
            results = []
            for start in range(0, len(samples), rate // 10):
                stream.accept_waveform(rate, samples[start:start + rate // 10])
                while spotter.is_ready(stream):
                    spotter.decode_stream(stream)
                    result = spotter.get_result(stream)
                    if result:
                        results.append(result)
                        spotter.reset_stream(stream)
            self.assertIn("法国", results)
            self.assertIsNone(recognize.Preview().event("法国", 0.))


if __name__ == "__main__":
    unittest.main()
