import logging
import os
from datetime import datetime

from utils.config import LOG_DIR


class DailyFileHandler(logging.Handler):
    """Write to {class_name}-{YYYY-MM-DD}.log and switch files at midnight."""

    def __init__(self, log_dir: str, class_name: str):
        super().__init__()
        self.log_dir = log_dir
        self.class_name = class_name
        self._current_date = None
        self._stream = None
        self._open_for_today()

    def _path_for(self, date_str: str) -> str:
        return os.path.join(self.log_dir, f"{self.class_name}-{date_str}.log")

    def _open_for_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._current_date and self._stream:
            return
        if self._stream:
            self._stream.close()
        self._current_date = today
        self._stream = open(self._path_for(today), "a", encoding="utf-8")

    def emit(self, record):
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if today != self._current_date:
                self._open_for_today()
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        if self._stream:
            self._stream.close()
            self._stream = None
        super().close()


class Logger:
    @staticmethod
    def get_logger(class_name):
        logger = logging.getLogger(class_name)
        if logger.handlers:
            return logger

        logger.setLevel(logging.DEBUG)

        log_dir = LOG_DIR or "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        file_handler = DailyFileHandler(log_dir, class_name)
        file_handler.setLevel(logging.DEBUG)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)

        formatter = logging.Formatter("[%(asctime)s] %(name)s.%(levelname)s: %(message)s")
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        return logger