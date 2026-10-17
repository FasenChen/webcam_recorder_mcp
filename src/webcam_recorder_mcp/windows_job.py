"""Release the capture device even when the MCP process is forcibly terminated."""

import win32api
import win32con
import win32job


class CaptureJob:
    def __init__(self, pid: int) -> None:
        self.handle = win32job.CreateJobObject(None, "")
        try:
            limits = win32job.QueryInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation,
            )
            limits["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation, limits,
            )
            process = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid,
            )
            try:
                win32job.AssignProcessToJobObject(self.handle, process)
            finally:
                process.Close()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.handle.Close()
