import os
import time
import subprocess
import yaml
from datetime import datetime
from pathlib import Path

# === IMPROVED: Dynamic BASE_PATH ===
# This automatically sets the path relative to scheduler.py itself
BASE_PATH = str(Path(__file__).parent.resolve())
PYTHON_BIN = f"{BASE_PATH}/venv/bin/python3"

print(f"BASE_PATH resolved to: {BASE_PATH}")  # helpful debug line


def load_jobs():
    jobs_yml = os.getenv("JOBS_YML", "jobs.yml")
    path = jobs_yml if os.path.isabs(jobs_yml) else f"{BASE_PATH}/{jobs_yml}"
    with open(path, "r") as f:
        return yaml.safe_load(f)["jobs"]


def run_job(job):
    """
    Run a job in a non-blocking subprocess with flock.

    Supports:
      script: foo.py
      script: scripts/run_stack_job.sh
      args: [wnba, sports411_betting.py]
    """
    log_path = f"{BASE_PATH}/logs/{job['name']}.log"
    script_path = job["script"]
    if not os.path.isabs(script_path):
        script_path = f"{BASE_PATH}/{script_path}"
    lock_file = f"/tmp/{job['name']}.lock"
    args = list(job.get("args") or [])

    # Remove stale lock if older than 1 hour
    if os.path.exists(lock_file):
        if time.time() - os.path.getmtime(lock_file) > 3600:
            os.remove(lock_file)

    with open(log_path, "a") as log:
        timestamp = datetime.now()
        log.write(f"\n[{timestamp}] START {job['name']}\n")

        if script_path.endswith(".sh"):
            cmd = ["flock", lock_file, "bash", script_path, *args]
        else:
            cmd = ["flock", lock_file, PYTHON_BIN, script_path, *args]

        process = subprocess.Popen(cmd, stdout=log, stderr=log, cwd=BASE_PATH)

    return process


def _initial_last_run(job, now):
    """Phase-offset first run so Chrome jobs do not all start at once."""
    interval = job.get("interval_seconds", 60)
    offset = job.get("start_offset_seconds", 0)
    return now - interval + offset


def main():
    jobs = load_jobs()
    boot = time.time()
    last_run = {job["name"]: _initial_last_run(job, boot) for job in jobs}
    running_processes = {}

    while True:
        now = time.time()

        for job in jobs:
            interval = job.get("interval_seconds", 60)
            job_name = job["name"]

            # Check if job is already running
            process = running_processes.get(job_name)
            if process and process.poll() is None:
                # Job still running, skip this interval
                continue

            # Check if interval has passed (offset preserved via last_run schedule)
            if now - last_run[job_name] >= interval:
                running_processes[job_name] = run_job(job)
                last_run[job_name] = now

        time.sleep(1)


if __name__ == "__main__":
    main()
