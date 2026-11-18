"""Bounded, allowlisted Linux event exercises; never an arbitrary command API."""
from datetime import datetime, timezone
import errno
import hashlib
import hmac
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Literal
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DATA = Path(os.getenv("EVENT_DATA_DIR", "/data/events"))
SCRATCH = Path(os.getenv("EVENT_SCRATCH_DIR", "/scratch"))
KEY_FILE = Path(os.getenv("SERVICE_KEY_FILE", "/secrets/service-key"))
execution_lock = threading.Lock()
app = FastAPI(title="llooggeerr bounded event worker")


def now():
    return datetime.now(timezone.utc).isoformat()


def authorize(x_service_key: str = Header(default="")):
    if not KEY_FILE.is_file() or not hmac.compare_digest(x_service_key.encode(), KEY_FILE.read_bytes().strip()):
        raise HTTPException(401, "invalid service key")


class Job(BaseModel):
    kind: Literal["file", "process", "permission", "network", "cpu", "memory", "disk"]
    operation_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1,
                              max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    seconds: float = Field(default=0.25, ge=0.05, le=3)
    memory_mib: int = Field(default=8, ge=1, le=32)


def context():
    def read(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None
    namespaces = {}
    for name in ("pid", "mnt", "net", "user", "cgroup"):
        try:
            namespaces[name] = os.readlink(f"/proc/self/ns/{name}")
        except OSError:
            namespaces[name] = None
    return {"pid": os.getpid(), "uid": os.getuid(), "gid": os.getgid(),
            "hostname": socket.gethostname(), "cgroup": read("/proc/self/cgroup"),
            "boot_id": read("/proc/sys/kernel/random/boot_id"), "namespaces": namespaces}


class Recorder:
    def __init__(self):
        self.rows = []

    def call(self, operation, target, function, expected_errno=None):
        row = {"operation": operation, "target": str(target), "attempted": True,
               "started_at": now(), "pid": os.getpid(), "source": "independent_workload"}
        try:
            value = function()
            row.update(success=True, return_code=value if isinstance(value, int) else 0, errno=None)
        except OSError as exc:
            row.update(success=False, return_code=-1, errno=exc.errno, error=str(exc))
        row["finished_at"] = now()
        row["expected_errno"] = expected_errno
        row["expectation_met"] = row["errno"] == expected_errno if expected_errno else row["success"]
        self.rows.append(row)
        return row


def file_job(rec, root):
    target = root / "file.txt"
    renamed = root / "renamed.txt"
    rec.call("create_write", target, lambda: target.write_bytes(b"llooggeerr synthetic bank event\n"))
    rec.call("open_read", target, target.read_bytes)
    def append():
        with target.open("ab") as handle:
            return handle.write(b"updated\n")
    rec.call("append_write", target, append)
    rec.call("rename", f"{target} -> {renamed}", lambda: target.rename(renamed))
    rec.call("unlink", renamed, renamed.unlink)
    rec.call("missing_file_open", target, target.read_bytes, errno.ENOENT)


def permission_job(rec, root):
    target = root / "restricted.txt"
    target.write_text("synthetic restricted resource\n")
    try:
        rec.call("chmod", target, lambda: target.chmod(0))
        rec.call("denied_read", target, target.read_bytes, errno.EACCES)
        rec.call("chown_root_attempt", target, lambda: os.chown(target, 0, 0), errno.EPERM)
    finally:
        target.chmod(0o600)
        target.unlink()


def process_job(rec, root):
    for code in (0, 3):
        started = now()
        child = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"],
                                 cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        actual = child.wait(timeout=5)
        rec.rows.append({"operation": "spawn_exec_exit", "target": sys.executable,
                         "attempted": True, "started_at": started, "finished_at": now(),
                         "pid": child.pid, "ppid": os.getpid(), "success": actual == 0,
                         "return_code": actual, "expected_return_code": code,
                         "expectation_met": actual == code, "source": "independent_workload"})
    started = now()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], cwd=root,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        child.terminate()
        actual = child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    rec.rows.append({"operation": "spawn_signal_exit", "target": sys.executable, "pid": child.pid,
                     "ppid": os.getpid(), "attempted": True, "started_at": started,
                     "finished_at": now(), "success": False, "return_code": actual,
                     "signal": signal.SIGTERM, "expectation_met": actual == -signal.SIGTERM,
                     "source": "independent_workload"})


def network_job(rec):
    with socket.socket() as server:
        server.settimeout(2)
        rec.call("bind", "127.0.0.1:ephemeral", lambda: server.bind(("127.0.0.1", 0)))
        address = server.getsockname()
        rec.call("listen", address, lambda: server.listen(1))
        with socket.socket() as client:
            client.settimeout(2)
            rec.call("connect", address, lambda: client.connect(address))
            accepted, peer = server.accept()
            with accepted:
                rec.rows.append({"operation": "accept", "target": str(peer), "attempted": True,
                                 "started_at": now(), "finished_at": now(), "success": True,
                                 "return_code": accepted.fileno(), "errno": None,
                                 "expectation_met": True, "source": "independent_workload"})
                rec.call("send", address, lambda: client.send(b"synthetic"))
                rec.call("recv", peer, lambda: accepted.recv(32))
    # Reserving a non-listening loopback socket avoids an arbitrary external target.
    with socket.socket() as reserved, socket.socket() as client:
        reserved.bind(("127.0.0.1", 0))
        client.settimeout(2)
        rec.call("connect_refused", reserved.getsockname(),
                 lambda: client.connect(reserved.getsockname()), errno.ECONNREFUSED)


def disk_job(rec):
    # Refuse to fill a host volume or an unbounded filesystem, even if misconfigured.
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    tmpfs = any(line.split()[4] == str(SCRATCH) and " - tmpfs " in line for line in mounts)
    size = os.statvfs(SCRATCH)
    if not tmpfs or not 0 < size.f_blocks * size.f_frsize <= 16 * 1024 * 1024:
        raise HTTPException(409, "disk exercise requires a dedicated tmpfs of at most 16 MiB")
    target = SCRATCH / f"fill-{uuid.uuid4().hex}.bin"
    written = 0
    def fill():
        nonlocal written
        with target.open("xb", buffering=0) as handle:
            for _ in range(20):
                written += handle.write(b"x" * 1024 * 1024)
            os.fsync(handle.fileno())
    try:
        row = rec.call("write_until_enospc", target, fill, errno.ENOSPC)
        row["bytes_written"] = written
        row["filesystem_capacity_bytes"] = size.f_blocks * size.f_frsize
    finally:
        target.unlink(missing_ok=True)


@app.get("/health")
def health():
    return {"status": "ok", "role": "bounded_event_worker", "pid": os.getpid()}


@app.post("/run", dependencies=[Depends(authorize)])
def run(body: Job):
    if os.getuid() == 0:
        raise HTTPException(409, "event worker must run as a non-root user")
    if not execution_lock.acquire(blocking=False):
        raise HTTPException(429, "one bounded event job is already running")
    started = now()
    root = DATA / (body.operation_id + "-" + uuid.uuid4().hex[:8])
    rec = Recorder()
    try:
        root.mkdir(parents=True, exist_ok=False)
        if body.kind == "file":
            file_job(rec, root)
        elif body.kind == "permission":
            permission_job(rec, root)
        elif body.kind == "process":
            process_job(rec, root)
        elif body.kind == "network":
            network_job(rec)
        elif body.kind == "disk":
            disk_job(rec)
        elif body.kind == "cpu":
            def cpu():
                deadline, iterations = time.monotonic() + body.seconds, 0
                while time.monotonic() < deadline:
                    hashlib.sha256(b"llooggeerr bounded CPU" * 256).digest()
                    iterations += 1
                return iterations
            rec.call("bounded_cpu", f"{body.seconds}s", cpu)
        elif body.kind == "memory":
            def memory():
                block = bytearray(body.memory_mib * 1024 * 1024)
                for offset in range(0, len(block), 4096):
                    block[offset] = 1
                time.sleep(body.seconds)
                return len(block)
            rec.call("bounded_memory", f"{body.memory_mib}MiB/{body.seconds}s", memory)
        return {"operation_id": body.operation_id, "kind": body.kind,
                "started_at": started, "finished_at": now(), "subevents": rec.rows,
                "success": all(row["expectation_met"] for row in rec.rows), "context": context(),
                "source": "independent_workload", "is_kernel_capture": False}
    finally:
        try:
            root.rmdir()
        except OSError:
            pass  # Unexpected leftovers are evidence; never recursively erase them.
        execution_lock.release()
