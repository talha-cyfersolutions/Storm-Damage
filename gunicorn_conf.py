"""Production Gunicorn configuration for Storm Damage Imagery API."""

import multiprocessing
import os

from config import Config

# Server Socket
bind = f"{os.getenv('HOST', Config.HOST)}:{os.getenv('PORT', str(Config.PORT))}"
backlog = 2048

# Worker Processes
# Defaults to 2 * CPUs + 1, or WEB_CONCURRENCY environment variable
default_workers = max(2, multiprocessing.cpu_count() * 2 + 1)
workers = int(os.getenv("WEB_CONCURRENCY", default_workers))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000

# Timeouts
# COG window fetching and analysis may require up to 60-90s under high network latency
timeout = int(os.getenv("WORKER_TIMEOUT", "120"))
keepalive = 5
graceful_timeout = 30

# Worker recycling to guard against GDAL / C-extension memory leaks
max_requests = 1000
max_requests_jitter = 100

# Logging
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", Config.LOG_LEVEL).lower()
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" (%(D)s µs)'

# Process Naming
proc_name = "storm-damage-api"

