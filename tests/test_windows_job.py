import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows process lifetime contract")
def test_owner_kill_also_terminates_capture_process() -> None:
    import win32api
    import win32con
    import win32event

    owner = subprocess.Popen([
        sys.executable, "-u", "-c",
        "import subprocess,sys,time; "
        "from webcam_recorder_mcp.windows_job import CaptureJob; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "job=CaptureJob(child.pid); print(child.pid,flush=True); time.sleep(60)",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child = None
    try:
        assert owner.stdout is not None
        pid = int(owner.stdout.readline().strip())
        child = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        owner.kill()
        owner.wait(timeout=5)
        assert win32event.WaitForSingleObject(child, 5000) == win32event.WAIT_OBJECT_0
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if child is not None:
            child.Close()
