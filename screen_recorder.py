"""Isolated, low-bandwidth macOS screen recording with Telegram delivery."""
from __future__ import annotations

import atexit
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
from typing import Optional

import requests
from dotenv import load_dotenv


DEFAULT_SEGMENT_SECONDS = 30 * 60
TESTING_SEGMENT_SECONDS = 60  # current testing phase: 1-minute segments
TELEGRAM_MAX_BYTES = 49 * 1024 * 1024
MIN_VALID_VIDEO_BYTES = 32 * 1024
# Hard budget for stop(): kill capture and return so automation can exit.
# Must stay under ~10s. Never wait for Telegram uploads on shutdown.
SHUTDOWN_FFMPEG_WAIT_SEC = 2.0
SHUTDOWN_THREAD_JOIN_SEC = 2.0
SHUTDOWN_UPLOAD_JOIN_SEC = 1.0


def _log(message: str) -> None:
    print(f"[RECORDER] {message}", flush=True)


def _segment_seconds() -> int:
    """
    Length of each recording in seconds.

    Testing phase default is 60s. Override with SCREEN_RECORD_SECONDS in .env;
    set it to 1800 to return to the production 30-minute segments.
    """
    raw = os.environ.get("SCREEN_RECORD_SECONDS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return TESTING_SEGMENT_SECONDS


class ScreenRecorder:
    """
    Record the whole main display in independent 30-minute MP4 segments.

    Recording and uploading run in background threads and never use pyautogui,
    Quartz, Accessibility, or the automation's mouse/keyboard thread.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.output_dir = self.project_dir / "recordings"
        self.chat_file = self.project_dir / "logs" / "notify_chats.txt"
        self.ffmpeg_log = self.project_dir / "logs" / "screen_recording_ffmpeg.log"
        self.shutdown_marker = (
            self.project_dir / "logs" / "screen_recording_shutdown.active"
        )
        self.ffmpeg = shutil.which("ffmpeg")
        self.input_name = os.environ.get("SCREEN_RECORD_INPUT", "").strip()
        self.segment_seconds = _segment_seconds()
        self._stop_event = threading.Event()
        self._upload_queue: queue.Queue[Optional[Path]] = queue.Queue()
        self._process_lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._record_thread: Optional[threading.Thread] = None
        self._upload_thread: Optional[threading.Thread] = None
        self._started = False
        self._stopped = False
        self._stop_lock = threading.Lock()

    def start(self) -> bool:
        """Start recording without blocking automation startup."""
        if self._started:
            return True
        if sys.platform != "darwin":
            _log("Disabled: full-screen recording is supported only on macOS")
            return False
        if not self.ffmpeg:
            _log(
                "Disabled: ffmpeg is not installed. Install once with: "
                "brew install ffmpeg"
            )
            return False

        self.output_dir.mkdir(parents=True, exist_ok=True)
        load_dotenv(self.project_dir / ".env")
        self.segment_seconds = _segment_seconds()
        self._started = True
        # Daemon threads: while the run is active they still upload every
        # segment, but they must never keep main.py alive after the workflow
        # has already finished and asked to exit.
        self._upload_thread = threading.Thread(
            target=self._upload_loop,
            name="screen-upload",
            daemon=True,
        )
        self._record_thread = threading.Thread(
            target=self._record_loop,
            name="screen-record",
            daemon=True,
        )
        self._upload_thread.start()
        self._record_thread.start()
        _log(
            "Started: entire screen, 3 fps, 960px wide, ~140 kbit/s; "
            f"new Telegram upload every {self.segment_seconds}s "
            "(sent to all connected users)"
        )
        return True

    def _detect_screen_input(self) -> str:
        """Find AVFoundation's numeric index for the primary macOS display."""
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
        _log(
            "Could not identify Capture screen 0; falling back to video device 1. "
            "Set SCREEN_RECORD_INPUT in .env if needed."
        )
        return "1"

    def _new_path(self) -> Path:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return self.output_dir / f"automation_screen_{stamp}.mp4"

    def _ffmpeg_command(self, path: Path) -> list[str]:
        # 140 kbit/s gives a theoretical video payload of about 31.5 MB per
        # 30 minutes, safely below Telegram Bot API's 50 MB upload ceiling.
        # Shorter testing segments are proportionally smaller.
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

    def _record_loop(self) -> None:
        if not self.input_name:
            # Device enumeration runs only in this background thread so it
            # cannot delay BlackBird startup or any workflow action.
            self.input_name = self._detect_screen_input()
        while not self._stop_event.is_set():
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
                    process.wait()
            except Exception as exc:  # noqa: BLE001
                _log(f"Could not start ffmpeg: {exc}")
                break

            with self._process_lock:
                if self._process is process:
                    self._process = None

            if path.is_file() and path.stat().st_size >= MIN_VALID_VIDEO_BYTES:
                _log(
                    f"Segment complete: {path.name} "
                    f"({path.stat().st_size / 1024 / 1024:.1f} MB)"
                )
                self._upload_queue.put(path)
            else:
                _log(
                    "No valid recording was produced. Check macOS Screen "
                    f"Recording permission. See {self.ffmpeg_log}"
                )
                path.unlink(missing_ok=True)

            if process.returncode not in (0, 255, -signal.SIGTERM, -signal.SIGINT):
                _log(f"ffmpeg exited with code {process.returncode}; recording stopped")
                break

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

    def _upload(self, path: Path) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chats = self._chat_ids()
        if not token:
            _log(f"Upload skipped (TELEGRAM_BOT_TOKEN missing); retained {path}")
            return
        if not chats:
            _log(f"Upload skipped (no connected Telegram chats); retained {path}")
            return
        size = path.stat().st_size
        if size > TELEGRAM_MAX_BYTES:
            _log(
                f"Upload skipped: {path.name} is {size / 1024 / 1024:.1f} MB, "
                "over Telegram's 49 MB safety limit; file retained"
            )
            return

        caption = (
            "🎥 ЗАПИСЬ ЭКРАНА\n"
            f"Файл: {path.name}\n"
            f"Период: до {self.segment_seconds} сек. работы автоматизации."
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
                            telegram_file_id = response.json()["result"]["video"]["file_id"]
                        except (KeyError, TypeError, ValueError):
                            # Delivery succeeded. If Telegram omits a reusable
                            # ID, later chats simply receive a normal upload.
                            telegram_file_id = None
            except Exception as exc:  # noqa: BLE001
                all_sent = False
                _log(
                    f"Telegram upload failed for chat {chat_id}: "
                    f"{type(exc).__name__}"
                )

        if not all_sent:
            _log(f"At least one upload failed; local recording retained at {path}")

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

    def _finish_current_segment(self) -> None:
        with self._process_lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        _log("Automation ending: stopping ffmpeg immediately")
        try:
            if process.stdin is not None:
                process.stdin.write(b"q\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.wait(timeout=SHUTDOWN_FFMPEG_WAIT_SEC)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
            process.wait(timeout=1.0)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            _log("ffmpeg did not exit after kill request")

    def stop(self) -> None:
        """
        Stop capture and return immediately so automation can exit.

        Mid-run segment uploads are unaffected. On shutdown we only kill ffmpeg
        and release the process — we never wait for Telegram uploads, because
        that blocked exit for many minutes after every proxy was already done.
        """
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        if not self._started:
            return

        self.shutdown_marker.parent.mkdir(parents=True, exist_ok=True)
        self.shutdown_marker.touch()
        try:
            self._stop_event.set()
            self._finish_current_segment()
            if self._record_thread is not None:
                self._record_thread.join(timeout=SHUTDOWN_THREAD_JOIN_SEC)
            # Do not drain the upload queue. Daemon upload thread may finish a
            # short in-flight send, but it must not delay process exit.
            try:
                self._upload_queue.put_nowait(None)
            except queue.Full:
                pass
            if self._upload_thread is not None:
                self._upload_thread.join(timeout=SHUTDOWN_UPLOAD_JOIN_SEC)
            _log("Stopped: automation may exit now (uploads no longer block shutdown)")
        finally:
            self.shutdown_marker.unlink(missing_ok=True)


_SESSION: Optional[ScreenRecorder] = None


def start_screen_recording(project_dir: Path) -> ScreenRecorder:
    """Start one process-wide recorder and register reliable exit cleanup."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    session = ScreenRecorder(project_dir)
    _SESSION = session
    session.start()
    atexit.register(session.stop)

    if threading.current_thread() is threading.main_thread():
        previous = signal.getsignal(signal.SIGTERM)

        def _handle_sigterm(signum, frame) -> None:
            session.stop()
            if callable(previous):
                previous(signum, frame)
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, _handle_sigterm)
    return session
