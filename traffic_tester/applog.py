"""App log: in memory (last 500 lines, for the UI and Claude) and in <data_dir>/app.log (rotating)."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import os
from typing import List


class AppLog(logging.Handler):
    def __init__(self, data_dir: str):
        super().__init__(logging.INFO)
        self.lines = collections.deque(maxlen=500)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        self.logger = logging.getLogger("tt")
        self.logger.setLevel(logging.INFO)
        for h in list(self.logger.handlers):
            if isinstance(h, AppLog):
                self.logger.removeHandler(h)
        self.logger.addHandler(self)
        try:
            os.makedirs(data_dir, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(os.path.join(data_dir, "app.log"), maxBytes=1_000_000, backupCount=3)
            fh.setFormatter(self.formatter)
            self.logger.addHandler(fh)
        except OSError:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def tail(self, n: int = 80) -> List[str]:
        return list(self.lines)[-max(1, min(n, 500)):]
