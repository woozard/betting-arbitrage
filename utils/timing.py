import time
from functools import wraps
from utils.logger import Logger

logger = Logger.get_logger("latency")

def time_it(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        end = time.perf_counter()
        latency_ms = (end - start) * 1000
        logger.info(f"TIMING | {func.__name__} took {latency_ms:.2f} ms")
        return result
    return wrapper
