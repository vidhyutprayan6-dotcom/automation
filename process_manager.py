"""
Lightweight subprocess controller for main.py (macOS).

Starts/stops the existing automation without changing its logic.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
MAIN_SCRIPT = PROJECT_DIR / "main.py"
LOG_DIR = PROJECT_DIR / "logs"
AUTOMATION_LOG = LOG_DIR / "automation.log"
PID_FILE = LOG_DIR / "automation.pid"

GRACEFUL_TIMEOUT_SEC = 15.0
BLACKBIRD_APP_PATH = Path("/Applications/BlackBird.app")
BLACKBIRD_PROCESS_NAMES = ("BlackBird", "BlackBird Network")
# Calibrated OK for "BlackBird Network is not open anymore" (1920x1080 top_left)
DISMISS_OK_COORD = (959, 395)


def _find_blackbird_pids() -> list[int]:
    """Find BlackBird.app process PIDs via pgrep (no Accessibility required)."""
    pids: set[int] = set()
    for name in BLACKBIRD_PROCESS_NAMES:
        out = subprocess.run(
            ["pgrep", "-x", name],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in (out.stdout or "").splitlines():
            if line.strip().isdigit():
                pids.add(int(line.strip()))
    out = subprocess.run(
        ["pgrep", "-f", "/Applications/BlackBird.app"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (out.stdout or "").splitlines():
        if line.strip().isdigit():
            pids.add(int(line.strip()))
    return sorted(pids)


def _is_blackbird_running() -> bool:
    """True if any BlackBird-related process is alive."""
    if sys.platform != "darwin":
        return False
    if _find_blackbird_pids():
        return True
    # Fallback: osascript (may fail without Accessibility)
    script = r"""
    tell application "System Events"
      return (exists process "BlackBird") or (exists process "BlackBird Network")
    end tell
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    return (out.stdout or "").strip().lower() == "true"


def _dismiss_network_modal_for_quit() -> None:
    """
    Click OK on the 'BlackBird Network is not open anymore' dialog so quit can succeed.
    Uses AppleScript first, then cliclick/osascript key fallback.
    """
    if sys.platform != "darwin":
        return

    logger.info("Attempting to dismiss BlackBird Network modal before quit...")
    script = r"""
    set clicked to false
    tell application "System Events"
      repeat with pname in {"BlackBird", "BlackBird Network"}
        if exists process pname then
          tell process pname
            repeat with w in windows
              try
                if exists button "OK" of w then
                  click button "OK" of w
                  set clicked to true
                end if
              end try
              try
                if exists sheet 1 of w then
                  if exists button "OK" of sheet 1 of w then
                    click button "OK" of sheet 1 of w
                    set clicked to true
                  end if
                end if
              end try
            end repeat
          end tell
        end if
      end repeat
    end tell
    return clicked
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if (out.stdout or "").strip().lower() == "true":
        logger.info("Dismissed network modal via Accessibility OK")
        time.sleep(0.5)
        return

    # Coordinate click via venv Python + Quartz (same as main.py)
    x, y = DISMISS_OK_COORD
    click_script = f"""
import sys
sys.path.insert(0, {str(PROJECT_DIR)!r})
try:
    from main import human_click_at
    human_click_at({x}, {y})
    print("clicked")
except Exception as e:
    print(f"fail:{{e}}")
"""
    py = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)
    out2 = subprocess.run(
        [str(py), "-c", click_script],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(PROJECT_DIR),
    )
    if "clicked" in (out2.stdout or ""):
        logger.info("Dismissed network modal via coordinate OK (%s, %s)", x, y)
        time.sleep(0.5)


def _quit_blackbird() -> bool:
    """
    Dismiss blocking modals, then quit BlackBird (graceful → killall → SIGKILL).
    Returns True if no BlackBird processes remain.
    """
    if sys.platform != "darwin":
        return False

    if not _is_blackbird_running():
        logger.info("BlackBird is not running — nothing to quit")
        return True

    logger.info("Quitting BlackBird (pids=%s)...", _find_blackbird_pids())

    # Modal blocks quit — dismiss first (retry)
    for _ in range(3):
        _dismiss_network_modal_for_quit()
        time.sleep(0.3)

    # Graceful quit
    for app_name in BLACKBIRD_PROCESS_NAMES:
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to quit'],
            capture_output=True,
            check=False,
        )
    subprocess.run(
        ["osascript", "-e", 'tell application "BlackBird" to quit'],
        capture_output=True,
        check=False,
    )

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if not _is_blackbird_running():
            logger.info("BlackBird quit gracefully")
            return True
        time.sleep(0.25)

    # Force quit — targeted names only
    logger.warning("Graceful quit failed — force quitting BlackBird processes")
    for name in BLACKBIRD_PROCESS_NAMES:
        subprocess.run(["killall", name], capture_output=True, check=False)
    subprocess.run(["killall", "-9", "BlackBird"], capture_output=True, check=False)
    subprocess.run(["killall", "-9", "BlackBird Network"], capture_output=True, check=False)

    # Kill any remaining PIDs from pgrep
    time.sleep(0.5)
    for pid in _find_blackbird_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    time.sleep(0.5)
    gone = not _is_blackbird_running()
    if gone:
        logger.info("BlackBird force-quit successful")
    else:
        logger.error("BlackBird still running after force quit: pids=%s", _find_blackbird_pids())
    return gone


class ProcessManager:
    """Manage one automation child process at a time."""

    def __init__(self, project_dir: Path = PROJECT_DIR) -> None:
        self.project_dir = project_dir.resolve()
        self._process: Optional[subprocess.Popen] = None
        self.last_exit_code: Optional[int] = None
        self.last_stop_was_user: bool = False
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _python_executable(self) -> Path:
        if VENV_PYTHON.is_file():
            return VENV_PYTHON
        return Path(sys.executable)

    def _expected_cmdline_fragment(self) -> str:
        return str(self.project_dir / "main.py")

    def _write_pid_file(self, pid: int) -> None:
        PID_FILE.write_text(f"{pid}\n", encoding="utf-8")

    def _clear_pid_file(self) -> None:
        if PID_FILE.is_file():
            PID_FILE.unlink(missing_ok=True)

    def _read_pid_file(self) -> Optional[int]:
        if not PID_FILE.is_file():
            return None
        try:
            return int(PID_FILE.read_text(encoding="utf-8").strip().split()[0])
        except (ValueError, OSError):
            return None

    def _pid_belongs_to_automation(self, pid: int) -> bool:
        """True if pid is alive and looks like our main.py worker."""
        try:
            os.kill(pid, 0)
        except OSError:
            return False

        if sys.platform == "darwin":
            try:
                out = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                cmd = (out.stdout or "").strip()
                if "main.py" in cmd and str(self.project_dir) in cmd:
                    return True
                # Frozen binary path
                if "BlackBirdAutomation" in cmd:
                    return True
                return "main.py" in cmd
            except Exception:  # noqa: BLE001
                return True  # process exists; best effort

        return True

    def _attach_existing_process(self, pid: int) -> bool:
        if not self._pid_belongs_to_automation(pid):
            self._clear_pid_file()
            return False
        # Bot restarted — we know pid but have no Popen; status/stop still work via os.kill
        self._process = None
        return True

    def current_pid(self) -> Optional[int]:
        if self._process is not None and self._process.poll() is None:
            return self._process.pid
        pid = self._read_pid_file()
        if pid is not None and self._pid_belongs_to_automation(pid):
            return pid
        return None

    def poll_exit_code(self) -> Optional[int]:
        """Return exit code if the child has finished; None if still running."""
        if self._process is not None:
            code = self._process.poll()
            if code is None:
                return None
            self.last_exit_code = code
            self._process = None
            self._clear_pid_file()
            return code
        if self.is_running():
            return None
        return self.last_exit_code

    def is_running(self) -> bool:
        if self._process is not None:
            code = self._process.poll()
            if code is None:
                return True
            self.last_exit_code = code
            self._process = None
            self._clear_pid_file()
            return False

        pid = self._read_pid_file()
        if pid is None:
            return False
        if self._pid_belongs_to_automation(pid):
            return True
        self._clear_pid_file()
        return False

    def start(self) -> Tuple[bool, str]:
        if self.is_running():
            return False, "already_running"

        if not MAIN_SCRIPT.is_file():
            logger.error("main.py not found at %s", MAIN_SCRIPT)
            return False, "start_failed"

        python_exe = self._python_executable()
        if not python_exe.is_file():
            logger.error("Python not found: %s (run ./run.sh once to create .venv)", python_exe)
            return False, "start_failed"

        for name in ("data.txt", "card.txt", "email.txt"):
            if not (self.project_dir / name).is_file():
                logger.error("Missing required file: %s", name)
                return False, "start_failed"

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_fp = open(AUTOMATION_LOG, "a", encoding="utf-8")  # noqa: SIM115

        cmd = [str(python_exe), str(MAIN_SCRIPT)]
        logger.info("Starting automation: %s (cwd=%s)", cmd, self.project_dir)

        try:
            kwargs: dict = {
                "cwd": str(self.project_dir),
                "stdout": log_fp,
                "stderr": subprocess.STDOUT,
                "env": os.environ.copy(),
            }
            if sys.platform != "win32":
                kwargs["start_new_session"] = True  # own process group on macOS

            self.last_stop_was_user = False
            self.last_exit_code = None
            self._process = subprocess.Popen(cmd, **kwargs)
            self._write_pid_file(self._process.pid)
            time.sleep(0.3)
            if self._process.poll() is not None:
                self.last_exit_code = self._process.returncode
                self._process = None
                self._clear_pid_file()
                logger.error("Automation exited immediately (see %s)", AUTOMATION_LOG)
                return False, "start_failed"
            return True, "started"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to start automation: %s", exc)
            self._process = None
            self._clear_pid_file()
            return False, "start_failed"

    def _terminate_pid(self, pid: int) -> None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
            return

        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return

        deadline = time.monotonic() + GRACEFUL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.2)

        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stop(self) -> Tuple[bool, str]:
        self.last_stop_was_user = True
        automation_running = self.is_running()
        blackbird_running = _is_blackbird_running()
        logger.info(
            "stop(): automation_running=%s blackbird_running=%s pids=%s",
            automation_running,
            blackbird_running,
            _find_blackbird_pids(),
        )

        if not automation_running and not blackbird_running:
            self._process = None
            self._clear_pid_file()
            return False, "not_running"

        automation_stopped = True
        if automation_running:
            pid: Optional[int] = None
            if self._process is not None and self._process.poll() is None:
                pid = self._process.pid
            else:
                pid = self._read_pid_file()

            if pid is None:
                self._clear_pid_file()
                automation_stopped = False
            else:
                logger.info("Stopping automation pid=%s", pid)
                self._terminate_pid(pid)

                if self._process is not None:
                    try:
                        self._process.wait(timeout=GRACEFUL_TIMEOUT_SEC)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                    self._process = None

                self._clear_pid_file()
                if self.is_running():
                    logger.error("Automation still running after stop")
                    automation_stopped = False

        # Always attempt BlackBird quit (modal may block until dismissed)
        blackbird_stopped = _quit_blackbird()
        if not blackbird_stopped and _is_blackbird_running():
            logger.error("BlackBird still running after quit attempts")

        if automation_stopped and blackbird_stopped:
            return True, "stopped"
        if not automation_running and blackbird_stopped:
            return True, "stopped"
        if automation_stopped and not _is_blackbird_running():
            return True, "stopped"
        return False, "stop_failed"
