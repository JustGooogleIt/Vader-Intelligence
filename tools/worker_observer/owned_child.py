"""POSIX group anchor, launched only by boundary.run in a fresh session.

Stay alive through TERM and after the command exits, so the parent can signal
the entire owned group before reaping its leader. No worker/server PID inputs.
"""

import os
import signal
import subprocess
import sys


def main():
    # Caught handlers (unlike SIG_IGN) reset to default when the command execs.
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    status_fd = int(sys.argv[1])
    os.set_inheritable(status_fd, False)
    try:
        command = subprocess.Popen(sys.argv[2:], close_fds=True)
    except OSError:
        result = b"launch_error\n"
    else:
        result = f"exit:{command.wait()}\n".encode("ascii")
    os.write(status_fd, result)  # One bounded message, smaller than PIPE_BUF.
    os.close(status_fd)
    os.close(0)
    os.close(1)
    while True:
        signal.pause()  # Parent's final SIGKILL, then parent wait/reap.


if __name__ == "__main__":
    main()
