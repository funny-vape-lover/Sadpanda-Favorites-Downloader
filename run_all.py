import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform.startswith("win"):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def main() -> int:
    worker_cmd = [sys.executable, "worker.py"]
    app_cmd = [sys.executable, "-m", "streamlit", "run", "app.py"]

    worker_proc = subprocess.Popen(worker_cmd, cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    try:
        app_rc = subprocess.call(app_cmd, cwd=ROOT)
    except KeyboardInterrupt:
        app_rc = 0
    finally:
        _terminate(worker_proc)

    return app_rc


if __name__ == "__main__":
    raise SystemExit(main())
