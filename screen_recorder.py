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
from typing import Optional, Set

import requests
from dotenv import load_dotenv


DEFAULT_SEGMENT_SECONDS = 30 * 60
TESTING_SEGMENT_SECONDS = 60  # current testing phase: 1-minute segments
TELEGRAM_MAX_BYTES = 49 * 1024 * 1024
MIN_VALID_VIDEO_BYTES = 32 * 1024
# Stop capture fast, then hand remaining files to a detached uploader so
# automation can exit while the video is still delivered to Telegram.
SHUTDOWN_FFMPEG_WAIT_SEC = 5.0
SHUTDOWN_THREAD_JOIN_SEC = 3.0


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
    Record the whole main display in independent segments.

    Recording and uploading run in background threads and never use pyautogui,
    Quartz, Accessibility, or the automation's mouse/keyboard thread.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.output_dir = self.project_dir / "recordings"
        self.chat_file = self.project_dir / "logs" / "notify_chats.txt"
        self.ffmpeg_log = self.project_dir / "logs" / "screen_recording_ffmpeg.log"
        self.upload_helper_log = (
            self.project_dir / "logs" / "screen_recording_upload_helper.log"
        )
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
        self._current_path: Optional[Path] = None
        self._uploaded: Set[str] = set()
        self._uploaded_lock = threading.Lock()
        self._shutdown_mode = False
        self._deferred: list[Path] = []
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
        # Daemon mid-run workers must not keep main.py alive after stop().
        # Final undelivered files are handed to a detached helper on shutdown.
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
        # 140 kbit/s ≈ 31.5 MB per 30 minutes, under Telegram's 50 MB limit.
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

    def _queue_if_valid(self, path: Path) -> bool:
        if not path.is_file() or path.stat().st_size < MIN_VALID_VIDEO_BYTES:
            return False
        if self._was_uploaded(path):
            return False
        self._upload_queue.put(path)
        return True

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
                        self._current_path = path
                    process.wait()
            except Exception as exc:  # noqa: BLE001
                _log(f"Could not start ffmpeg: {exc}")
                break

            with self._process_lock:
                if self._process is process:
                    self._process = None

            if self._queue_if_valid(path):
                _log(
                    f"Segment complete: {path.name} "
                    f"({path.stat().st_size / 1024 / 1024:.1f} MB)"
                )
            else:
                if path.is_file() and path.stat().st_size < MIN_VALID_VIDEO_BYTES:
                    _log(
                        "No valid recording was produced. Check macOS Screen "
                        f"Recording permission. See {self.ffmpeg_log}"
                    )
                    path.unlink(missing_ok=True)
                elif not path.is_file():
                    _log(
                        "No recording file was written. Check macOS Screen "
                        f"Recording permission. See {self.ffmpeg_log}"
                    )

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
            _log(f"Upload skipped: {path.name} is too small ({size} bytes)")
            return False
        if size > TELEGRAM_MAX_BYTES:
            _log(
                f"Upload skipped: {path.name} is {size / 1024 / 1024:.1f} MB, "
                "over Telegram's 49 MB safety limit; file retained"
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
                _log(
                    f"Telegram upload failed for chat {chat_id}: "
                    f"{type(exc).__name__}"
                )

        if all_sent:
            self._mark_uploaded(path)
        else:
            _log(f"At least one upload failed; local recording retained at {path}")
        return all_sent

    def _upload_loop(self) -> None:
        while True:
            path = self._upload_queue.get()
            try:
                if path is None:
                    return
                if self._shutdown_mode:
                    # Leave undelivered files for the detached shutdown helper.
                    if path.is_file():
                        self._deferred.append(path)
                    continue
                if path.is_file():
                    self._upload(path)
            finally:
                self._upload_queue.task_done()

    def _finish_current_segment(self) -> None:
        with self._process_lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        _log("Automation ending: finalizing current recording segment")
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
            _log("ffmpeg did not exit after kill request")

    def _drain_pending_paths(self) -> list[Path]:
        pending: list[Path] = []
        seen: set[str] = set()

        def add(path: Path) -> None:
            key = str(path.resolve())
            if key in seen or self._was_uploaded(path):
                return
            if path.is_file() and path.stat().st_size >= MIN_VALID_VIDEO_BYTES:
                seen.add(key)
                pending.append(path)

        while True:
            try:
                item = self._upload_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            add(item)

        for item in list(self._deferred):
            add(item)

        with self._process_lock:
            current = self._current_path
        if current is not None:
            add(current)
        return pending

    def _spawn_detached_uploader(self, paths: list[Path]) -> None:
        if not paths:
            _log("No pending recording files to upload on shutdown")
            return
        self.upload_helper_log.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--upload-only",
            str(self.project_dir),
            *[str(path) for path in paths],
        ]
        try:
            with self.upload_helper_log.open("ab") as helper_log:
                subprocess.Popen(
                    cmd,
                    cwd=str(self.project_dir),
                    stdout=helper_log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env=os.environ.copy(),
                )
            names = ", ".join(path.name for path in paths)
            _log(
                f"Detached upload helper started for {len(paths)} file(s): {names}"
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"Could not start detached uploader: {exc}")
            # Last-resort sync upload so the video is not silently lost.
            for path in paths:
                self._upload(path)

    def stop(self) -> None:
        """
        Stop capture quickly, then deliver any unfinished recording.

        Automation exits after ffmpeg is stopped. Remaining videos are uploaded
        by a detached helper process so Telegram still receives the recording
        without blocking the CORRECT completion for many minutes.
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
            # Block the mid-run uploader from consuming the final file so the
            # detached helper can deliver it after automation exits.
            self._shutdown_mode = True
            self._stop_event.set()
            self._finish_current_segment()
            if self._record_thread is not None:
                self._record_thread.join(timeout=SHUTDOWN_THREAD_JOIN_SEC)

            pending = self._drain_pending_paths()
            try:
                self._upload_queue.put_nowait(None)
            except queue.Full:
                pass
            if self._upload_thread is not None:
                self._upload_thread.join(timeout=1.0)

            # Deferred files may have been captured while draining.
            for item in list(self._deferred):
                if item not in pending and not self._was_uploaded(item):
                    if item.is_file() and item.stat().st_size >= MIN_VALID_VIDEO_BYTES:
                        pending.append(item)

            self._spawn_detached_uploader(pending)
            _log(
                "Stopped capture: automation can exit now; "
                "pending video upload continues in the background"
            )
        finally:
            self.shutdown_marker.unlink(missing_ok=True)


def _upload_only_main(argv: list[str]) -> int:
    """CLI entry used by the detached shutdown uploader."""
    if len(argv) < 3:
        _log("upload-only usage: --upload-only <project_dir> <file> [file...]")
        return 2
    project_dir = Path(argv[1]).resolve()
    load_dotenv(project_dir / ".env")
    recorder = ScreenRecorder(project_dir)
    ok = True
    for raw in argv[2:]:
        path = Path(raw)
        _log(f"Helper uploading: {path}")
        if not recorder._upload(path):
            ok = False
    return 0 if ok else 1


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


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--upload-only":
        raise SystemExit(_upload_only_main(sys.argv[2:]))
    raise SystemExit("screen_recorder.py is imported by main.py; do not run directly")
