"""One-command demo: python scripts/demo.py

Standard library only, so it runs with plain Python (no venv, no packages).
Starts the server and its own Postgres in Docker, runs the simulator twice
inside the server container with the same seed, and prints the dashboard URL.
The second run must report 0 new devices: that is the "counted exactly once"
proof.
"""

import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8000
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
DASHBOARD_URL = f"http://127.0.0.1:{PORT}/dashboard"
HEALTH_TIMEOUT_S = 120
SEED = "1"

# Fixed project name: every container, network and volume of the demo is named
# activation-demo_*. The dev database lives in the default project (named after
# the folder) with volume <folder>_pgdata, so `down -v` here can never touch it.
PROJECT = "activation-demo"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", "docker-compose.demo.yml"]


def step(text: str) -> None:
    print(f"\n==> {text}", flush=True)


def fail(text: str) -> None:
    print(f"\nERROR: {text}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str]) -> int:
    print("    $ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT).returncode


def port_is_free() -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", PORT)) != 0


def healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def run_simulator() -> tuple[int, int | None, int | None]:
    """Run the simulator in the container, echoing its output.

    Returns (exit code, unique devices, new devices); the last two are None
    if the summary could not be read.
    """
    cmd = [*COMPOSE, "exec", "-T", "server", "python", "-m", "scripts.simulate", "--seed", SEED]
    print("    $ " + " ".join(cmd), flush=True)
    lines: list[str] = []
    with subprocess.Popen(
        cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    ) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            print("    | " + line.rstrip(), flush=True)
            lines.append(line)
    text = "".join(lines)
    unique = re.search(r"for (\d+) unique devices", text)
    new = re.search(r"new \(201\):\s+(\d+)", text)
    return proc.returncode, int(unique[1]) if unique else None, int(new[1]) if new else None


def main() -> int:
    env_file = ROOT / ".env"
    if not env_file.exists():
        shutil.copyfile(ROOT / ".env.example", env_file)
        print("No .env found: copied .env.example to .env (placeholder values, fine for a local demo).")

    step("Checking that Docker is available")
    if shutil.which("docker") is None or subprocess.run(
        ["docker", "compose", "version"], capture_output=True
    ).returncode != 0:
        fail("Docker (with the compose plugin) was not found. Install or start Docker Desktop.")

    step(f"Checking that port {PORT} is free")
    if not port_is_free():
        fail(f"port {PORT} is already in use. Stop the dev server (python -m app) first, "
             f"or the previous demo: {' '.join(COMPOSE)} down -v")

    step("Removing any previous demo (only the activation-demo project and its volume)")
    if run([*COMPOSE, "down", "-v"]) != 0:
        fail("docker compose down failed.")

    step("Building the server image and starting the demo database and server")
    if run([*COMPOSE, "up", "-d", "--build"]) != 0:
        fail("docker compose up failed.")

    step(f"Waiting for the server to become healthy (up to {HEALTH_TIMEOUT_S} s)")
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while not healthy():
        if time.monotonic() > deadline:
            print("\nLast server log lines:", file=sys.stderr)
            subprocess.run([*COMPOSE, "logs", "--tail", "40", "server"], cwd=ROOT)
            fail(f"the server did not become healthy within {HEALTH_TIMEOUT_S} s.")
        time.sleep(2)
    print("    server is healthy")

    step("Simulator run 1: every device should be NEW (201)")
    code, unique, new = run_simulator()
    if code != 0:
        fail(f"the simulator failed (exit code {code}).")
    if unique is None or new != unique:
        fail(f"expected all devices new on the first run, got new={new}, unique={unique}.")

    step("Simulator run 2, same seed: no device should be new (each is counted once)")
    code, _, new2 = run_simulator()
    if code != 0:
        fail(f"the simulator failed (exit code {code}).")
    if new2 != 0:
        fail(f"expected 0 new devices on the second run, got {new2}.")

    print(
        f"\nDemo complete: {unique} devices activated on run 1, 0 new on run 2 "
        "(repeat requests were not counted)."
        f"\n\nDashboard: {DASHBOARD_URL}"
        "\nLogin: DASHBOARD_USER and DASHBOARD_PASSWORD from your .env file."
        f"\n\nStop the demo and delete its data with:\n  {' '.join(COMPOSE)} down -v"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
