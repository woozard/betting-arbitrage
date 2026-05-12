import logging
from logging.handlers import TimedRotatingFileHandler
import os
from datetime import datetime
from utils.config import LOG_DIR

class Logger:
    @staticmethod
    def get_logger(class_name):
        # Create a logger for the class
        logger = logging.getLogger(class_name)
        logger.setLevel(logging.DEBUG)

        # Create a directory for the logs if it doesn't exist
        if not os.path.exists('logs'):
            os.makedirs('logs')

        # Get today's date for the filename postfix
        today_date = datetime.now().strftime('%Y-%m-%d')
        filename = f'{LOG_DIR}/{class_name}-{today_date}.log'

        # Create a timed rotating file handler that creates a new log file daily
        file_handler = TimedRotatingFileHandler(
            filename=filename,
            when='midnight',
            interval=1,
            backupCount=7
        )
        file_handler.setLevel(logging.DEBUG)

        # Create a console handler to display logs on the screen
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)

        # Create a formatter and set it for the handlers
        formatter = logging.Formatter('[%(asctime)s] %(name)s.%(levelname)s: %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        # Add the handlers to the logger
        if not logger.hasHandlers():
            logger.addHandler(file_handler)
            logger.addHandler(console_handler)

        return logger