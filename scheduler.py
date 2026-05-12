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
    with open(f"{BASE_PATH}/jobs.yml", "r") as f:
        return yaml.safe_load(f)["jobs"]

def run_job(job):
    """
    Run a job in a non-blocking subprocess with flock.
    """
    log_path = f"{BASE_PATH}/logs/{job['name']}.log"
    script_path = f"{BASE_PATH}/{job['script']}"
    lock_file = f"/tmp/{job['name']}.lock"

    # Remove stale lock if older than 1 hour
    if os.path.exists(lock_file):
        if time.time() - os.path.getmtime(lock_file) > 3600:
            os.remove(lock_file)

    with open(log_path, "a") as log:
        timestamp = datetime.now()
        log.write(f"\n[{timestamp}] START {job['name']}\n")

        # Run job in a separate subprocess (non-blocking)
        # flock will prevent concurrent runs of the same job
        process = subprocess.Popen([
            "flock", lock_file,
            PYTHON_BIN, script_path
        ], stdout=log, stderr=log)

    return process  # return process handle if needed

def main():
    jobs = load_jobs()
    last_run = {job["name"]: 0 for job in jobs}
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

            # Check if interval has passed
            if now - last_run[job_name] >= interval:
                running_processes[job_name] = run_job(job)
                last_run[job_name] = time.time()

        time.sleep(1)

if __name__ == "__main__":
    main()
    