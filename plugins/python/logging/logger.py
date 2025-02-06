from abc import ABC, abstractmethod
import logging


class Logger(ABC):
    """Abstract interface for logging."""

    @abstractmethod
    def error(self, message: str, *args):
        pass

    @abstractmethod
    def warn(self, message: str, *args):
        pass

    @abstractmethod
    def info(self, message: str, *args):
        pass

    @abstractmethod
    def debug(self, message: str, *args):
        pass


class PythonLogger(Logger):
    """Logger implementation using Python logging."""

    def __init__(self):
        logging.basicConfig(level=logging.DEBUG)

    def error(self, message, *args):
        logging.error(message % args if args else message)

    def warn(self, message, *args):
        logging.warning(message % args if args else message)

    def info(self, message, *args):
        logging.info(message % args if args else message)

    def debug(self, message, *args):
        logging.debug(message % args if args else message)
