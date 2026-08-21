"""
Always-on screen recording service owned by the Telegram bot process.

Lifecycle:
  bot start  → start_service()     (service stays alive)
  /start app → begin_session()     (record + upload every N seconds)
  app ends   → end_session()       (upload remainder < N seconds; service stays up)
  bot stop   → stop_service()      (service stops)
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

import requests
from dotenv import load_dotenv


DEFAULT_SEGMENT_SECONDS = 30 * 60  # production: upload every 30 minutes
TELEGRAM_MAX_BYTES = 49 * 1024 * 1024
MIN_VALID_VIDEO_BYTES = 32 * 1024
SESSION_FFMPEG_STOP_SEC = 5.0


def _log(message: str) -> None:
    print(f"[RECORDER] {message}", flush=True)


def _segment_seconds() -> int:
    """Seconds per upload segment. Default 1800 (30 min). Override via .env."""
    raw = os.environ.get("SCREEN_RECORD_SECONDS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_SEGMENT_SECONDS


class RecordingService:
    """
    Long-lived recorder that outlives each automation run.

    The service thread is started with the Telegram bot and only stops when the
    bot stops. Actual ffmpeg capture runs only during an automation session.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.output_dir = self.project_dir / "recordings"
        self.chat_file = self.project_dir / "logs" / "notify_chats.txt"
        self.ffmpeg_log = self.project_dir / "logs" / "screen_recording_ffmpeg.log"
        self.ffmpeg = shutil.which("ffmpeg")
        self.input_name = os.environ.get("SCREEN_RECORD_INPUT", "").strip()
        self.segment_seconds = _segment_seconds()

        self._service_stop = threading.Event()
        self._session_stop = threading.Event()
        self._service_started = False
        self._in_session = False
        self._lock = threading.Lock()

        self._upload_queue: queue.Queue[Optional[Path]] = queue.Queue()
        self._uploaded: Set[str] = set()
        self._queued: Set[str] = set()
        self._uploaded_lock = threading.Lock()

        self._process: Optional[subprocess.Popen] = None
        self._current_path: Optional[Path] = None
        self._process_lock = threading.Lock()

        self._upload_thread: Optional[threading.Thread] = None
        self._session_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ service
    def start_service(self) -> bool:
        """Start the always-on service (called once when the Telegram bot starts)."""
        with self._lock:
            if self._service_started:
                return True
            if sys.platform != "darwin":
                _log("Service disabled: full-screen recording is macOS-only")
                return False
            if not self.ffmpeg:
                _log(
                    "Service disabled: ffmpeg missing. Install with: brew install ffmpeg"
                )
                return False

            load_dotenv(self.project_dir / ".env")
            self.segment_seconds = _segment_seconds()
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._service_stop.clear()
            self._upload_thread = threading.Thread(
                target=self._upload_loop,
                name="recording-upload",
                daemon=True,
            )
            self._upload_thread.start()
            self._service_started = True
            _log(
                "Service RUNNING (idle). Recording begins when automation starts. "
                f"Upload interval={self.segment_seconds}s"
            )
            return True

    def stop_service(self) -> None:
        """Stop the service when the Telegram bot shuts down."""
        with self._lock:
            if not self._service_started:
                return
            self._service_started = False
        _log("Service stopping with Telegram bot...")
        self.end_session()
        self._service_stop.set()
        try:
            self._upload_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._upload_thread is not None:
            self._upload_thread.join(timeout=30)
        _log("Service stopped")

    # ------------------------------------------------------------------ session
    def begin_session(self) -> bool:
        """Start capturing for one automation run (called on /start automation)."""
        with self._lock:
            if not self._service_started:
                if not self.start_service():
                    return False
            if self._in_session:
                _log("Session already active — ignoring duplicate begin_session")
                return True
            if not self.ffmpeg:
                _log("Cannot begin session: ffmpeg is not available")
                return False
            self._session_stop.clear()
            self._in_session = True
            self._session_thread = threading.Thread(
                target=self._session_loop,
                name="recording-session",
                daemon=True,
            )
            self._session_thread.start()
        _log("Session STARTED — recording for upload")
        return True

    def end_session(self) -> None:
        """
        End the current automation recording session.

        Finalizes the open segment (even if < 30 minutes), queues it for upload,
        then returns the service to idle. The service itself keeps running.
        """
        with self._lock:
            if not self._in_session:
                return
            self._session_stop.set()
            thread = self._session_thread
        self._finish_current_ffmpeg()
        if thread is not None:
            thread.join(timeout=SESSION_FFMPEG_STOP_SEC + 5.0)
        with self._lock:
            self._in_session = False
            self._session_thread = None
        _log("Session ENDED — service still running (idle), remainder queued for upload")

    # ------------------------------------------------------------------ internals
    def _detect_screen_input(self) -> str:
        try:
            result = subprocess.run(
                [
                    str(self.ffmpeg),
                    "-hide_banner",
                    "-f",
                    "avfoundation",
                    "-list_devices",
                    "true",
                    "-i",
                    "",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            listing = f"{result.stdout or ''}\n{result.stderr or ''}"
            match = re.search(r"\[(\d+)\]\s+Capture screen 0\b", listing, re.IGNORECASE)
            if match:
                index = match.group(1)
                _log(f"Detected primary display: AVFoundation video device {index}")
                return index
        except Exception as exc:  # noqa: BLE001
            _log(f"Could not enumerate displays: {exc}")
        _log("Falling back to video device 1 — set SCREEN_RECORD_INPUT in .env if needed")
        return "1"

    def _new_path(self) -> Path:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return self.output_dir / f"automation_screen_{stamp}.mp4"

    def _ffmpeg_command(self, path: Path) -> list[str]:
        return [
            str(self.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "avfoundation",
            "-capture_cursor",
            "1",
            "-framerate",
            "5",
            "-i",
            self.input_name,
            "-an",
            "-vf",
            "fps=3,scale=960:-2:force_original_aspect_ratio=decrease",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-threads",
            "1",
            "-b:v",
            "140k",
            "-maxrate",
            "180k",
            "-bufsize",
            "360k",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-t",
            str(self.segment_seconds),
            str(path),
        ]

    def _mark_uploaded(self, path: Path) -> None:
        with self._uploaded_lock:
            self._uploaded.add(str(path.resolve()))

    def _was_uploaded(self, path: Path) -> bool:
        with self._uploaded_lock:
            return str(path.resolve()) in self._uploaded

    def _queue_upload(self, path: Path) -> None:
        if not path.is_file() or path.stat().st_size < MIN_VALID_VIDEO_BYTES:
            return
        key = str(path.resolve())
        with self._uploaded_lock:
            if key in self._uploaded or key in self._queued:
                return
            self._queued.add(key)
        self._upload_queue.put(path)
        _log(
            f"Queued for Telegram upload: {path.name} "
            f"({path.stat().st_size / 1024 / 1024:.1f} MB)"
        )

    def _session_loop(self) -> None:
        if not self.input_name:
            self.input_name = self._detect_screen_input()
        while not self._session_stop.is_set() and not self._service_stop.is_set():
            path = self._new_path()
            _log(f"Recording segment: {path.name}")
            try:
                self.ffmpeg_log.parent.mkdir(parents=True, exist_ok=True)
                with self.ffmpeg_log.open("ab") as ffmpeg_log:
                    process = subprocess.Popen(
                        self._ffmpeg_command(path),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=ffmpeg_log,
                        text=False,
                        start_new_session=True,
                    )
                    with self._process_lock:
                        self._process = process
                        self._current_path = path
                    process.wait()
            except Exception as exc:  # noqa: BLE001
                _log(f"Could not start ffmpeg: {exc}")
                break

            with self._process_lock:
                if self._process is process:
                    self._process = None

            self._queue_upload(path)
            if path.is_file() and path.stat().st_size < MIN_VALID_VIDEO_BYTES:
                _log(
                    "Segment too small — check Screen Recording permission. "
                    f"See {self.ffmpeg_log}"
                )
                path.unlink(missing_ok=True)

            if self._session_stop.is_set() or self._service_stop.is_set():
                break
            if process.returncode not in (0, 255, -signal.SIGTERM, -signal.SIGINT):
                _log(f"ffmpeg exited with code {process.returncode}; ending session capture")
                break

    def _finish_current_ffmpeg(self) -> None:
        with self._process_lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        _log("Finalizing current segment for end-of-task upload")
        try:
            if process.stdin is not None:
                process.stdin.write(b"q\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.wait(timeout=SESSION_FFMPEG_STOP_SEC)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=2.0)
            return
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
            process.wait(timeout=1.0)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            _log("ffmpeg did not exit after kill")

    def _chat_ids(self) -> list[int]:
        if not self.chat_file.is_file():
            return []
        result: set[int] = set()
        try:
            for raw in self.chat_file.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if raw.lstrip("-").isdigit():
                    result.add(int(raw))
        except OSError as exc:
            _log(f"Could not read Telegram chat list: {exc}")
        return sorted(result)

    def _upload(self, path: Path) -> bool:
        if self._was_uploaded(path):
            return True
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chats = self._chat_ids()
        if not token:
            _log(f"Upload skipped (TELEGRAM_BOT_TOKEN missing); retained {path}")
            return False
        if not chats:
            _log(f"Upload skipped (no connected Telegram chats); retained {path}")
            return False
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size < MIN_VALID_VIDEO_BYTES:
            return False
        if size > TELEGRAM_MAX_BYTES:
            _log(
                f"Upload skipped: {path.name} is {size / 1024 / 1024:.1f} MB "
                "(over Telegram limit); file retained"
            )
            return False

        caption = (
            "🎥 ЗАПИСЬ ЭКРАНА\n"
            f"Файл: {path.name}\n"
            f"Размер: {size / 1024 / 1024:.1f} MB"
        )
        all_sent = True
        telegram_file_id: Optional[str] = None
        for chat_id in chats:
            try:
                if telegram_file_id:
                    response = requests.post(
                        f"https://api.telegram.org/bot{token}/sendVideo",
                        data={
                            "chat_id": str(chat_id),
                            "caption": caption,
                            "video": telegram_file_id,
                            "supports_streaming": "true",
                        },
                        timeout=(20, 60),
                    )
                else:
                    with path.open("rb") as video:
                        response = requests.post(
                            f"https://api.telegram.org/bot{token}/sendVideo",
                            data={
                                "chat_id": str(chat_id),
                                "caption": caption,
                                "supports_streaming": "true",
                            },
                            files={"video": (path.name, video, "video/mp4")},
                            timeout=(20, 180),
                        )
                if not response.ok:
                    all_sent = False
                    _log(
                        f"Telegram upload failed for chat {chat_id}: "
                        f"HTTP {response.status_code} {response.text[:300]}"
                    )
                else:
                    _log(f"Uploaded {path.name} to Telegram chat {chat_id}")
                    if telegram_file_id is None:
                        try:
                            telegram_file_id = response.json()["result"]["video"][
                                "file_id"
                            ]
                        except (KeyError, TypeError, ValueError):
                            telegram_file_id = None
            except Exception as exc:  # noqa: BLE001
                all_sent = False
                _log(f"Telegram upload failed for chat {chat_id}: {type(exc).__name__}")

        if all_sent:
            self._mark_uploaded(path)
        else:
            _log(f"Upload incomplete; local file retained at {path}")
        return all_sent

    def _upload_loop(self) -> None:
        while True:
            path = self._upload_queue.get()
            try:
                if path is None:
                    return
                if path.is_file():
                    self._upload(path)
            finally:
                self._upload_queue.task_done()


_SERVICE: Optional[RecordingService] = None
_SERVICE_LOCK = threading.Lock()


def get_recording_service(project_dir: Path) -> RecordingService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = RecordingService(project_dir)
        return _SERVICE
