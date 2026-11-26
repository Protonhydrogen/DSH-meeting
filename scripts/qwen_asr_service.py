"""Serve the official Qwen3-ASR streaming API on the local WSL loopback."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "Qwen/Qwen3-ASR-0.6B"
SAMPLE_RATE = 16000
MAX_SAMPLES = 60 * SAMPLE_RATE
MAX_BODY = MAX_SAMPLES * 6 + 8192
WINDOW_IDLE_SECONDS = 180


class Streams:
    def __init__(self, model):
        self.model = model
        self._active = {}
        self._lock = threading.Lock()

    def apply(self, action, data):
        window = data.get("window")
        if not isinstance(window, str) or not re.fullmatch(r"[a-f0-9]{32}", window):
            raise ValueError("Invalid streaming window ID")
        with self._lock:
            now = time.monotonic()
            self._active = {
                key: entry for key, entry in self._active.items()
                if now - entry[2] < WINDOW_IDLE_SECONDS
            }
            if action == "cancel":
                self._active.pop(window, None)
                return {}
            if action == "begin":
                if window in self._active or len(self._active) >= 4:
                    raise ValueError("Streaming window already exists or model is busy")
                language = data.get("language")
                chunk_seconds = data.get("chunk_seconds")
                if language not in (None, "Chinese", "English"):
                    raise ValueError("Invalid recognition language")
                if type(chunk_seconds) not in (float, int) or not 0.5 <= chunk_seconds <= 4:
                    raise ValueError("Invalid streaming chunk size")
                state = self.model.init_streaming_state(
                    language=language, chunk_size_sec=chunk_seconds,
                )
                self._active[window] = (state, 0, now)
                return {}
            if window not in self._active:
                raise ValueError("Streaming window has expired")
            state, count, _ = self._active[window]
            if action == "push":
                import numpy as np

                try:
                    audio = np.frombuffer(base64.b64decode(data.get("audio", ""), validate=True), dtype="<f4")
                except (TypeError, ValueError, binascii.Error) as exc:
                    raise ValueError("Invalid mono 16 kHz PCM payload") from exc
                if not audio.size or audio.size + count > MAX_SAMPLES or not np.isfinite(audio).all():
                    raise ValueError("Streaming audio exceeds the allowed sample range")
                self.model.streaming_transcribe(audio, state)
                self._active[window] = (state, count + audio.size, time.monotonic())
            else:
                self._active.pop(window)
                self.model.finish_streaming_transcribe(state)
            return {"text": state.text, "language": state.language}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _allowed(self):
        port = self.server.server_port
        return (not self.headers.get("Origin") and
                self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"})

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "Local client required"})
        if self.path != "/health":
            return self._send(404, {"error": "Unknown ASR operation"})
        self._send(200, {"model": MODEL, "protocol": 1})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "Local client required"})
        action = self.path.removeprefix("/")
        if action not in {"begin", "push", "finish", "cancel"}:
            return self._send(404, {"error": "Unknown ASR operation"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY or self.headers.get_content_type() != "application/json":
                raise ValueError("Invalid request body")
            self.connection.settimeout(10)
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Invalid request body")
            self._send(200, self.server.streams.apply(action, data))
        except (ValueError, OSError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception:
            import traceback

            traceback.print_exc()
            self._send(500, {"error": "Local model inference failed. See the Qwen ASR service log."})

    def log_message(self, format, *args):
        if args[1] != "200":
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.55)
    args = parser.parse_args()
    for required in ("config.json", "model.safetensors"):
        if not (args.model_dir / required).is_file():
            parser.error(f"Missing local checkpoint file {required}")
    if not 0.25 <= args.gpu_memory_utilization <= 0.9:
        parser.error("GPU memory utilization must be between 0.25 and 0.9")

    import numpy as np
    from qwen_asr import Qwen3ASRModel

    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        model = Qwen3ASRModel.LLM(
            model=str(args.model_dir.resolve()),
            gpu_memory_utilization=args.gpu_memory_utilization,
            max_model_len=4096, max_num_seqs=1, enforce_eager=True, max_new_tokens=128,
            attention_config={"backend": "TRITON_ATTN"},
        )
        print("Warming Qwen streaming inference on the GPU", flush=True)
        state = model.init_streaming_state(chunk_size_sec=2.0)
        # Exercise two full streaming chunks and a short final tail.
        model.streaming_transcribe(np.zeros(round(4.2 * SAMPLE_RATE), dtype=np.float32), state)
        model.finish_streaming_transcribe(state)
        server.streams = Streams(model)
        print(f"{MODEL} ready on 127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
