"""Scientific conference realtime recording and note-taking toolkit.

Package layout
--------------
``config``      configuration loading and validation
``db``          SQLite storage (sessions, chunks, segments, turns, events, tasks, notes)
``events``      persistent at-least-once event queue with consumer cursors and leases
``asr``         speech-to-text adapters (faster-whisper local, Azure cloud, mock)
``vad``         voice activity detection (Silero ONNX, energy fallback)
``diarization`` speaker labelling backends
``capture``     microphone / file audio sources
``segmenter``   utterance -> turn -> talk boundary state machine
``service``     control plane (start/stop/status) and the recording worker process
``runner``      local scheduler that turns events into notes via an agent adapter
``agents``      agent adapters (offline extractive, CLI, OpenAI-compatible HTTP)
``notes``       note schema validation and Markdown rendering
``cli``         command line interface
``mcp_server``  Model Context Protocol stdio adapter
"""

__version__ = "0.1.0"

#: Version of the on-disk event/note payload contract.
SCHEMA_VERSION = "1.0"

#: Version of the scientific-conference skill contract this build implements.
SKILL_VERSION = "1.0.0"
