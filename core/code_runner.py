"""Parent side of the code-strategy worker: starts it, sends candles, enforces a time limit."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

WORKER = Path(__file__).with_name("code_worker.py")
COLUMNS = ("open", "high", "low", "close", "volume")


class CodeRunner:
    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout
        self._proc = None
        self._lock = threading.Lock()

    def _start(self):
        self._proc = subprocess.Popen(
            [sys.executable, "-I", str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def stop(self):
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            p.kill()
            p.wait(timeout=2)

    def run(self, code: str, df, timeout: float = None):
        """Returns (result_dict, None) or (None, error_text)."""
        rows = {c: [float(x) for x in df[c].tolist()] for c in COLUMNS if c in df.columns}
        if "volume" not in rows:
            rows["volume"] = [0.0] * len(df)
        payload = json.dumps({"code": code, "rows": rows}) + "\n"
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            box = {}

            def talk():
                try:
                    self._proc.stdin.write(payload)
                    self._proc.stdin.flush()
                    box["line"] = self._proc.stdout.readline()
                except Exception as e:
                    box["err"] = str(e)

            t = threading.Thread(target=talk, daemon=True)
            t.start()
            t.join(timeout or self.timeout)
            timed_out = t.is_alive()
            if timed_out or "line" not in box or not box["line"]:
                self.stop()
                if timed_out:
                    return None, f"Your code took longer than {timeout or self.timeout:g} seconds and was stopped."
                return None, "Your code crashed the strategy process (for example it used too much memory)."
            try:
                ans = json.loads(box["line"])
            except ValueError:
                self.stop()
                return None, "The strategy process returned something unreadable."
        return (ans["result"], None) if ans.get("ok") else (None, ans.get("error", "unknown error"))


RUNNER = CodeRunner()


def synthetic_candles(n: int = 300):
    """Deterministic fake price data, used to test-run code before it is saved."""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(7)
    close = 1.10 + np.cumsum(rng.normal(0, 0.0008, n))
    high = close + rng.uniform(0.0002, 0.001, n)
    low = close - rng.uniform(0.0002, 0.001, n)
    return pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": high, "low": low,
                         "close": close, "volume": rng.integers(100, 1000, n).astype(float)})
