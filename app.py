#!/usr/bin/env python3
"""Pi Backup - a small web dashboard for a Raspberry Pi that:

  * unlocks (cryptsetup, BitLocker) and mounts a USB disk,
  * copies files from an SFTP/FTP server onto it on a schedule (rclone),
  * shows live progress and whether each copy succeeded.

Runs as root (cryptsetup and mount need it). Settings come from environment
variables, normally set in /etc/pi-backup.env:

  PIB_PASSWORD   dashboard password (any user name); empty = no login
  PIB_ALLOWED_IPS  IPs / networks allowed to open the dashboard, comma separated
                 (e.g. "192.168.100.21, 192.168.100.22, 10.0.5.0/24"); empty = any.
                 The Pi itself (127.0.0.1, used by SSH tunnels) is always allowed.
  PIB_PORT       web port (default 8080)
  PIB_MOUNT      where the disk is mounted (default /mnt/backup)
  PIB_DATA       where settings, history and logs live (default /var/lib/pi-backup)
  PIB_TRANSFERS  parallel file transfers (default 4)
  PIB_RCLONE     rclone binary (default "rclone")
"""
import collections
import datetime as dt
import hmac
import ipaddress
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid

from flask import Flask, Response, jsonify, render_template, request

DATA_DIR = os.environ.get("PIB_DATA", "/var/lib/pi-backup")
MOUNT_POINT = os.environ.get("PIB_MOUNT", "/mnt/backup")
PASSWORD = os.environ.get("PIB_PASSWORD", "")
PORT = int(os.environ.get("PIB_PORT", "8080"))
TRANSFERS = os.environ.get("PIB_TRANSFERS", "4")
RCLONE = os.environ.get("PIB_RCLONE", "rclone")


def parse_allowed(text):
    nets = []
    for item in re.split(r"[,\s;]+", text.strip()):
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            # Refuse to start rather than silently letting everyone in.
            raise SystemExit(f"PIB_ALLOWED_IPS: '{item}' is not an IP address or network (like 192.168.100.0/24).")
    return nets


ALLOWED_NETS = parse_allowed(os.environ.get("PIB_ALLOWED_IPS", ""))
MAPPER_NAME = "pibackup"
MAPPER_DEV = "/dev/mapper/" + MAPPER_NAME
KEEP_RUNS = 300  # history entries (and their logs) to keep

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
RCLONE_CONF = os.path.join(DATA_DIR, "rclone.conf")
LOG_DIR = os.path.join(DATA_DIR, "logs")

DEFAULT_CONFIG = {
    "connection": {"type": "sftp", "host": "", "port": "", "user": "", "pass": "",
                   "key_file": "", "explicit_tls": False},
    "jobs": [],
}

RCLONE_FLAGS = [
    "--use-json-log", "--stats", "5s", "--stats-log-level", "NOTICE",
    "--retries", "5", "--low-level-retries", "20",
    "--contimeout", "60s", "--timeout", "5m", "--transfers", TRANSFERS,
]

app = Flask(__name__)
lock = threading.RLock()
work_queue: "queue.Queue[str]" = queue.Queue()
# What the worker is doing right now. Live progress stays in memory so the SD
# card is not rewritten every few seconds; history.json changes only on state changes.
current = {"run_id": None, "proc": None, "cancel": False, "phase": "", "progress": {}}


# ---------------------------------------------------------------- storage

def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(default))


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def get_config():
    with lock:
        return load_json(CONFIG_FILE, DEFAULT_CONFIG)


def get_history():
    with lock:
        return load_json(HISTORY_FILE, [])


def log_path(run_id):
    return os.path.join(LOG_DIR, run_id + ".log")


def find_run(run_id):
    return next((r for r in get_history() if r["id"] == run_id), None)


def update_run(run_id, **fields):
    with lock:
        hist = get_history()
        for r in hist:
            if r["id"] == run_id:
                r.update(fields)
                break
        save_json(HISTORY_FILE, hist)


def add_run(run):
    with lock:
        hist = get_history()
        hist.insert(0, run)
        for old in hist[KEEP_RUNS:]:
            try:
                os.remove(log_path(old["id"]))
            except OSError:
                pass
        save_json(HISTORY_FILE, hist[:KEEP_RUNS])


def write_log(run_id, text):
    with open(log_path(run_id), "a", encoding="utf-8") as f:
        f.write(f"{now_iso()} {text}\n")


# ---------------------------------------------------------------- jobs & runs

def new_run(job, trigger, status="queued", dest=None, message=""):
    return {
        "id": uuid.uuid4().hex[:12], "job_id": job["id"], "job_name": job["name"],
        "remote_path": job["remote_path"], "dest_template": job.get("dest_template") or "{date}",
        "verify": bool(job.get("verify")), "trigger": trigger, "status": status,
        "queued_at": now_iso(), "started_at": None,
        "finished_at": now_iso() if status not in ("queued", "running") else None,
        "dest": dest, "bytes": 0, "files": 0, "message": message,
    }


def enqueue(job, trigger, dest=None):
    run = new_run(job, trigger, dest=dest)
    add_run(run)
    work_queue.put(run["id"])
    return run


def job_busy(job_id):
    return any(r["job_id"] == job_id and r["status"] in ("queued", "running") for r in get_history())


def render_dest(template, job_name):
    """Turn a folder template like '{date}' into an absolute path on the disk."""
    now = dt.datetime.now()
    safe_job = re.sub(r"[^\w.-]+", "_", job_name).strip("_") or "job"
    rel = (template.replace("{date}", now.strftime("%Y-%m-%d"))
                   .replace("{time}", now.strftime("%H-%M"))
                   .replace("{job}", safe_job))
    root = os.path.realpath(MOUNT_POINT)
    dest = os.path.realpath(os.path.join(root, rel.strip("/\\")))
    if dest != root and not dest.startswith(root + os.sep):
        raise ValueError("The destination folder must stay inside the disk.")
    return dest


def next_run(job):
    if not job.get("enabled") or not job.get("days"):
        return None
    now = dt.datetime.now().replace(second=0, microsecond=0)
    hour, minute = map(int, job["time"].split(":"))
    for i in range(8):
        cand = (now + dt.timedelta(days=i)).replace(hour=hour, minute=minute)
        if cand.weekday() in job["days"] and cand > now:
            return cand.isoformat(timespec="minutes")
    return None


# ---------------------------------------------------------------- rclone

def write_rclone_conf(conn):
    """Write the server as remote [src] into the app's own rclone config file (root only).
    Debug by hand with:  sudo rclone --config /var/lib/pi-backup/rclone.conf lsf src:"""
    def clean(value):
        return str(value).replace("\r", "").replace("\n", "").strip()
    lines = ["[src]", "type = " + (conn.get("type") or "sftp")]
    for key in ("host", "port", "user", "pass"):
        if conn.get(key):
            lines.append(f"{key} = {clean(conn[key])}")
    if conn.get("type") == "sftp" and conn.get("key_file"):
        lines.append("key_file = " + clean(conn["key_file"]))
    if conn.get("type") == "ftp" and conn.get("explicit_tls"):
        lines.append("explicit_tls = true")
    with lock:
        tmp = RCLONE_CONF + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, RCLONE_CONF)
    return RCLONE_CONF


def run_rclone(args, conf, log):
    """Run rclone, feed its JSON stats into current["progress"], write a readable log.
    Returns (exit code, last error line)."""
    cmd = [RCLONE, "--config", conf, *args, *RCLONE_FLAGS]
    log.write(f"{now_iso()} $ {' '.join(cmd)}\n")
    log.flush()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    current["proc"] = proc
    last_error, last_stats_log = "", 0.0
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            log.write(line + "\n")
            continue
        stats = entry.get("stats")
        if isinstance(stats, dict):
            current["progress"] = stats
            if time.monotonic() - last_stats_log >= 60:  # one progress line a minute is plenty
                last_stats_log = time.monotonic()
                log.write(f"{now_iso()} PROGRESS {human(stats.get('bytes'))} of "
                          f"{human(stats.get('totalBytes'))}, {stats.get('transfers', 0)} files, "
                          f"{stats.get('errors', 0)} errors\n")
                log.flush()
            continue
        level = str(entry.get("level", "info")).upper()
        obj = entry.get("object")
        msg = str(entry.get("msg", "")).strip()
        text = f"{str(entry.get('time', ''))[:19]} {level}: {obj + ': ' if obj else ''}{msg}"
        log.write(text + "\n")
        log.flush()
        if level in ("ERROR", "CRITICAL", "FATAL"):
            last_error = f"{obj + ': ' if obj else ''}{msg}"
    rc = proc.wait()
    current["proc"] = None
    return rc, last_error


def finish(run_id, status, message, **fields):
    update_run(run_id, status=status, message=message, finished_at=now_iso(), **fields)
    write_log(run_id, f"RESULT {status.upper()}: {message}")


def execute(run):
    run_id = run["id"]
    conn = get_config()["connection"]
    if shutil.which(RCLONE) is None:
        return finish(run_id, "failed", "rclone is not installed on the Pi.")
    if not conn.get("host"):
        return finish(run_id, "failed", "No server set up yet (Server connection section).")
    if not os.path.ismount(MOUNT_POINT):
        return finish(run_id, "failed", "The disk is not mounted. Unlock and mount it, then press Retry.")
    try:
        dest = run["dest"] or render_dest(run["dest_template"], run["job_name"])
    except ValueError as e:
        return finish(run_id, "failed", str(e))
    os.makedirs(dest, exist_ok=True)
    update_run(run_id, status="running", started_at=now_iso(), dest=dest)
    current.update(run_id=run_id, cancel=False, phase="copying", progress={})

    conf = write_rclone_conf(conn)
    src = "src:" + run["remote_path"]
    with open(log_path(run_id), "a", encoding="utf-8") as log:
        rc, err = run_rclone(["copy", src, dest], conf, log)
        stats = current["progress"]
        result = {"bytes": stats.get("bytes", 0), "files": stats.get("transfers", 0)}
        if current["cancel"]:
            return finish(run_id, "cancelled", "Cancelled. Press Retry to continue where it stopped.", **result)
        if rc != 0:
            return finish(run_id, "failed", f"Copy failed (rclone exit code {rc}). {err}".strip(), **result)
        if run["verify"]:
            current.update(phase="verifying", progress={})
            rc, err = run_rclone(["check", src, dest, "--one-way", "--size-only"], conf, log)
            if current["cancel"]:
                return finish(run_id, "cancelled", "Cancelled during verification. Files were copied.", **result)
            if rc != 0:
                return finish(run_id, "failed",
                              f"Verification failed: some files are missing or differ. {err}".strip(), **result)
    finish(run_id, "success",
           f"Copied {result['files']} files ({human(result['bytes'])})"
           + (", every file verified." if run["verify"] else "."), **result)


def worker():
    """Runs queued copies one at a time."""
    while True:
        run_id = work_queue.get()
        run = find_run(run_id)
        if run is None or run["status"] != "queued":
            continue  # cancelled while waiting
        try:
            execute(run)
        except Exception as e:  # keep the worker alive whatever happens
            finish(run_id, "failed", f"Internal error: {e}")
        finally:
            current.update(run_id=None, proc=None, cancel=False, phase="", progress={})


def scheduler():
    fired = {}
    while True:
        try:
            now = dt.datetime.now()
            minute = now.strftime("%Y-%m-%dT%H:%M")
            for job in get_config()["jobs"]:
                if (not job.get("enabled") or now.weekday() not in job.get("days", [])
                        or job.get("time") != now.strftime("%H:%M") or fired.get(job["id"]) == minute):
                    continue
                fired[job["id"]] = minute
                hist = get_history()
                if any(r["job_id"] == job["id"] and r["trigger"] == "schedule"
                       and r["queued_at"][:16] == minute for r in hist):
                    continue  # already fired this minute (service restarted)
                if job_busy(job["id"]):
                    add_run(new_run(job, "schedule", status="skipped",
                                    message="Skipped: the previous run of this job was still going."))
                else:
                    enqueue(job, "schedule")
        except Exception as e:
            print(f"scheduler error: {e}", flush=True)
        time.sleep(15)


def recover():
    """Runs that were going when the Pi/service stopped can't be resumed silently."""
    with lock:
        hist = get_history()
        changed = False
        for r in hist:
            if r["status"] in ("queued", "running"):
                r.update(status="interrupted", finished_at=now_iso(),
                         message="The Pi or the service restarted before this finished. "
                                 "Press Retry to continue (files already copied are skipped).")
                changed = True
        if changed:
            save_json(HISTORY_FILE, hist)


# ---------------------------------------------------------------- disk

def run_cmd(cmd, input_text=None, timeout=120, env=None):
    try:
        return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} is not installed")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]} timed out")


def list_disks():
    """USB disks/partitions that can be mounted (the Pi's own system disk is hidden)."""
    r = run_cmd(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,FSTYPE,LABEL,MODEL,TYPE,MOUNTPOINT"], timeout=15)
    if r.returncode != 0:
        return [], f"Could not list disks: {r.stderr.strip()}"
    disks = []
    for disk in json.loads(r.stdout).get("blockdevices", []):
        if disk.get("type") != "disk" or disk["name"].startswith(("mmcblk", "zram", "ram", "loop")):
            continue
        parts = [c for c in disk.get("children") or [] if c.get("type") == "part"]
        if any(p.get("mountpoint") in ("/", "/boot", "/boot/firmware") for p in parts):
            continue  # Pi booting from this USB disk
        for p in parts or [disk]:
            disks.append({"path": p["path"], "size": int(p.get("size") or 0),
                          "fstype": p.get("fstype") or "", "label": p.get("label") or "",
                          "model": (disk.get("model") or "").strip()})
    return disks, ""


def disk_status():
    info = {"mounted": os.path.ismount(MOUNT_POINT), "mount_point": MOUNT_POINT}
    if info["mounted"]:
        usage = shutil.disk_usage(MOUNT_POINT)
        r = run_cmd(["findmnt", "-n", "-o", "SOURCE,FSTYPE", MOUNT_POINT], timeout=10)
        info.update(total=usage.total, used=usage.used, free=usage.free,
                    source=r.stdout.strip() if r.returncode == 0 else "",
                    encrypted=os.path.exists(MAPPER_DEV))
    return info


# ---------------------------------------------------------------- web

def err(msg, code=400):
    return jsonify(ok=False, error=msg), code


def client_allowed(addr):
    if not ALLOWED_NETS:
        return True
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or any(ip in net for net in ALLOWED_NETS)


@app.before_request
def check_login():
    if not client_allowed(request.remote_addr):
        return Response(f"Access denied: this computer ({request.remote_addr}) is not on the "
                        "dashboard's allowed list (PIB_ALLOWED_IPS).", 403, mimetype="text/plain")
    if not PASSWORD:
        return None
    auth = request.authorization
    if auth and hmac.compare_digest((auth.password or "").encode(), PASSWORD.encode()):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Pi Backup"'})


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    cfg, hist = get_config(), get_history()
    conn = dict(cfg["connection"])
    conn["has_pass"] = bool(conn.pop("pass", ""))
    cur = None
    if current["run_id"]:
        p = current["progress"] or {}
        cur = {
            "run": next((r for r in hist if r["id"] == current["run_id"]), None),
            "phase": current["phase"],
            "progress": {k: p.get(k) for k in ("bytes", "totalBytes", "speed", "eta", "transfers",
                                                "totalTransfers", "checks", "totalChecks",
                                                "errors", "elapsedTime")},
            "transferring": [{"name": t.get("name"), "percentage": t.get("percentage"),
                              "size": t.get("size")} for t in (p.get("transferring") or [])[:5]],
        }
    return jsonify(
        time=now_iso(), disk=disk_status(), current=cur, connection=conn,
        queued=[r for r in reversed(hist) if r["status"] == "queued"],
        jobs=[dict(j, next_run=next_run(j)) for j in cfg["jobs"]],
        history=hist[:60],
    )


@app.get("/api/disks")
def api_disks():
    disks, error = list_disks()
    return jsonify(disks=disks, error=error)


@app.post("/api/disk/mount")
def api_mount():
    body = request.get_json(force=True) or {}
    device, password = body.get("device", ""), body.get("password", "")
    disks, error = list_disks()
    disk = next((d for d in disks if d["path"] == device), None)
    if disk is None:
        return err(error or "Pick a disk from the list.")
    if os.path.ismount(MOUNT_POINT):
        return err("A disk is already mounted.")
    os.makedirs(MOUNT_POINT, exist_ok=True)

    target = device
    if disk["fstype"].lower() == "bitlocker":
        if not password:
            return err("Enter the BitLocker password (or the 48-digit recovery key).")
        if os.path.exists(MAPPER_DEV):  # left over from an earlier attempt
            run_cmd(["cryptsetup", "close", MAPPER_NAME])
        # The password goes in on stdin only: never on the command line, never saved.
        r = run_cmd(["cryptsetup", "open", "--type", "bitlk", device, MAPPER_NAME], input_text=password + "\n")
        if r.returncode != 0:
            return err("Unlock failed (wrong password?): " + (r.stderr.strip() or f"code {r.returncode}"))
        target = MAPPER_DEV

    fstype = run_cmd(["blkid", "-p", "-o", "value", "-s", "TYPE", target], timeout=30).stdout.strip().lower()
    if fstype == "ntfs":  # kernel driver first (faster), ntfs-3g as a fallback
        attempts = [["mount", "-t", "ntfs3", "-o", "noatime", target, MOUNT_POINT],
                    ["mount", "-t", "ntfs-3g", "-o", "noatime", target, MOUNT_POINT]]
    else:
        attempts = [["mount", "-o", "noatime", target, MOUNT_POINT]]
    errors = []
    for cmd in attempts:
        r = run_cmd(cmd)
        if r.returncode == 0:
            return jsonify(ok=True, message=f"Mounted {device} ({fstype or 'unknown'}) at {MOUNT_POINT}.")
        errors.append(r.stderr.strip())
    if target == MAPPER_DEV:
        run_cmd(["cryptsetup", "close", MAPPER_NAME])
    hint = ""
    if any("dirty" in e.lower() or "hibernat" in e.lower() for e in errors):
        hint = " Windows left the disk in use: plug it into a PC, use 'Safely remove', and turn off Fast Startup."
    return err("Unlocked, but mounting failed: " + " | ".join(e for e in errors if e) + hint)


@app.post("/api/disk/unmount")
def api_unmount():
    if current["run_id"]:
        return err("A copy is running. Cancel it first.", 409)
    if os.path.ismount(MOUNT_POINT):
        run_cmd(["sync"], timeout=600)
        r = run_cmd(["umount", MOUNT_POINT])
        if r.returncode != 0:
            return err("Unmount failed: " + r.stderr.strip())
    if os.path.exists(MAPPER_DEV):
        r = run_cmd(["cryptsetup", "close", MAPPER_NAME])
        if r.returncode != 0:
            return err("Unmounted, but locking the disk failed: " + r.stderr.strip())
    return jsonify(ok=True, message="Disk unmounted and locked. You can unplug it.")


@app.post("/api/connection")
def api_connection():
    body = request.get_json(force=True) or {}
    kind = body.get("type")
    port = str(body.get("port", "")).strip()
    if kind not in ("sftp", "ftp"):
        return err("Protocol must be SFTP or FTP.")
    if port and not port.isdigit():
        return err("Port must be a number.")
    new_pass = None
    if body.get("pass"):
        r = run_cmd([RCLONE, "obscure", "-"], input_text=body["pass"], timeout=30)
        if r.returncode != 0:
            return err("Could not store the password: " + r.stderr.strip())
        new_pass = r.stdout.strip()
    with lock:
        cfg = get_config()
        conn = cfg["connection"]
        conn.update(type=kind, host=str(body.get("host", "")).strip(), port=port,
                    user=str(body.get("user", "")).strip(),
                    key_file=str(body.get("key_file", "")).strip(),
                    explicit_tls=bool(body.get("explicit_tls")))
        if new_pass is not None:
            conn["pass"] = new_pass
        elif body.get("clear_pass"):
            conn["pass"] = ""
        save_json(CONFIG_FILE, cfg)
    return jsonify(ok=True, message="Connection saved.")


@app.post("/api/test")
def api_test():
    path = str((request.get_json(force=True) or {}).get("path", "")).strip()
    conn = get_config()["connection"]
    if not conn.get("host"):
        return err("Save the server connection first.")
    r = run_cmd([RCLONE, "--config", write_rclone_conf(conn), "lsf", "src:" + path, "--max-depth", "1",
                 "--contimeout", "15s", "--timeout", "30s", "--retries", "1", "--low-level-retries", "1"],
                timeout=90)
    if r.returncode != 0:
        lines = [l for l in r.stderr.strip().splitlines() if l.strip() and "NOTICE" not in l]
        return err("Connection test failed: " + (" ".join(lines[-2:]) or f"code {r.returncode}"))
    entries = r.stdout.splitlines()
    return jsonify(ok=True, message=f"Connected. {len(entries)} items in '{path or '(home folder)'}'.",
                   preview=entries[:15])


@app.post("/api/jobs")
def api_job_save():
    body = request.get_json(force=True) or {}
    name = str(body.get("name", "")).strip()
    time_str = str(body.get("time", "")).strip()
    template = str(body.get("dest_template") or "{date}").strip()
    if not name:
        return err("Give the job a name.")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_str):
        return err("Time must look like 07:30.")
    try:
        render_dest(template, name)
    except ValueError as e:
        return err(str(e))
    days = sorted({int(d) for d in body.get("days", []) if str(d).isdigit() and 0 <= int(d) <= 6})
    job = {"id": body.get("id") or uuid.uuid4().hex[:8], "name": name,
           "remote_path": str(body.get("remote_path", "")).strip(), "dest_template": template,
           "days": days, "time": time_str, "verify": bool(body.get("verify")),
           "enabled": bool(body.get("enabled"))}
    with lock:
        cfg = get_config()
        idx = next((i for i, j in enumerate(cfg["jobs"]) if j["id"] == job["id"]), None)
        if idx is None:
            cfg["jobs"].append(job)
        else:
            cfg["jobs"][idx] = job
        save_json(CONFIG_FILE, cfg)
    return jsonify(ok=True, message="Job saved.")


@app.delete("/api/jobs/<job_id>")
def api_job_delete(job_id):
    with lock:
        cfg = get_config()
        cfg["jobs"] = [j for j in cfg["jobs"] if j["id"] != job_id]
        save_json(CONFIG_FILE, cfg)
    return jsonify(ok=True, message="Job deleted.")


@app.post("/api/jobs/<job_id>/run")
def api_job_run(job_id):
    job = next((j for j in get_config()["jobs"] if j["id"] == job_id), None)
    if job is None:
        return err("Job not found.", 404)
    if job_busy(job_id):
        return err("This job is already queued or running.", 409)
    enqueue(job, "manual")
    return jsonify(ok=True, message=f"'{job['name']}' started.")


@app.post("/api/runs/<run_id>/retry")
def api_run_retry(run_id):
    run = find_run(run_id)
    if run is None:
        return err("Run not found.", 404)
    if run["status"] not in ("failed", "cancelled", "interrupted"):
        return err("Only failed, cancelled or interrupted runs can be retried.")
    if job_busy(run["job_id"]):
        return err("This job is already queued or running.", 409)
    job = {"id": run["job_id"], "name": run["job_name"], "remote_path": run["remote_path"],
           "dest_template": run["dest_template"], "verify": run["verify"]}
    # Same folder as before, so rclone skips everything that is already there.
    enqueue(job, "retry", dest=run["dest"])
    return jsonify(ok=True, message="Retry queued" + (f" into {run['dest']}." if run["dest"] else "."))


@app.post("/api/runs/<run_id>/cancel")
def api_run_cancel(run_id):
    run = find_run(run_id)
    if run is None:
        return err("Run not found.", 404)
    if run["status"] == "queued":
        update_run(run_id, status="cancelled", finished_at=now_iso(), message="Cancelled before it started.")
    elif run["status"] == "running" and current["run_id"] == run_id:
        current["cancel"] = True
        proc = current["proc"]
        if proc is not None:
            proc.terminate()
    else:
        return err("This run is not queued or running.")
    return jsonify(ok=True, message="Cancelling.")


@app.get("/api/runs/<run_id>/log")
def api_run_log(run_id):
    if not re.fullmatch(r"[0-9a-f]{12}", run_id):
        return err("Bad run id.")
    try:
        with open(log_path(run_id), encoding="utf-8", errors="replace") as f:
            tail = collections.deque(f, maxlen=500)
    except FileNotFoundError:
        return Response("No log for this run.", mimetype="text/plain")
    return Response("".join(tail), mimetype="text/plain")


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    recover()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    if not PASSWORD:
        print("WARNING: PIB_PASSWORD is not set - the dashboard has no login.", flush=True)
    print("Allowed computers: " + (", ".join(str(n) for n in ALLOWED_NETS) + " (+ this Pi)"
                                    if ALLOWED_NETS else "any"), flush=True)
    print(f"Pi Backup dashboard on port {PORT}", flush=True)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
