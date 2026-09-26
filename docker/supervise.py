"""Run Radicale and the MCP API, stopping the container if either exits."""
import signal
import subprocess
import sys
import time

children = []


def stop_children():
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 10
    for child in children:
        if child.poll() is None:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def stop_on_signal(signum, frame):
    stop_children()
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop_on_signal)
signal.signal(signal.SIGINT, stop_on_signal)
try:
    children.append(subprocess.Popen(["radicale", "--config", "/etc/radicale/config"]))
    children.append(subprocess.Popen([sys.executable, "-m", "radicale_mcp.server"]))
    while True:
        for child in children:
            status = child.poll()
            if status is not None:
                stop_children()
                sys.exit(status)
        time.sleep(0.2)
except BaseException:
    stop_children()
    raise
