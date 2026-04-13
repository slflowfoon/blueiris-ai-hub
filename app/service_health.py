import json
import os
import threading
import time


HEALTH_DIR = os.getenv("HEALTH_DIR", "/app/data/health")
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "10"))
HEARTBEAT_STALE_AFTER = int(os.getenv("HEARTBEAT_STALE_AFTER", "45"))


def ensure_health_dir():
    os.makedirs(HEALTH_DIR, exist_ok=True)


def heartbeat_path(service_name):
    return os.path.join(HEALTH_DIR, f"{service_name}.json")


def write_heartbeat(service_name, extra=None):
    ensure_health_dir()
    payload = {
        "service": service_name,
        "pid": os.getpid(),
        "timestamp": time.time(),
    }
    if extra:
        payload.update(extra)
    with open(heartbeat_path(service_name), "w", encoding="utf-8") as f:
        json.dump(payload, f)


def heartbeat_status(service_name, stale_after=HEARTBEAT_STALE_AFTER):
    path = heartbeat_path(service_name)
    if not os.path.exists(path):
        return {"service": service_name, "status": "missing", "age_seconds": None}

    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {"service": service_name, "status": "invalid", "age_seconds": None}

    ts = float(payload.get("timestamp", 0) or 0)
    age = max(0.0, time.time() - ts)
    return {
        "service": service_name,
        "status": "ok" if age <= stale_after else "stale",
        "age_seconds": round(age, 1),
        "pid": payload.get("pid"),
    }


def start_heartbeat_thread(service_name, interval=HEARTBEAT_INTERVAL, extra_fn=None):
    def _run():
        while True:
            extra = extra_fn() if extra_fn else None
            write_heartbeat(service_name, extra=extra)
            time.sleep(interval)

    thread = threading.Thread(target=_run, name=f"{service_name}-heartbeat", daemon=True)
    thread.start()
    return thread
