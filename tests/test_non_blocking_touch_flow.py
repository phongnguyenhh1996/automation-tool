import threading
import time

import pytest

from automation_tool.tradingview_touch_flow import _AbortLongOp, _run_worker_in_background_with_poll


def test_background_worker_success() -> None:
    def worker() -> tuple[str, str]:
        time.sleep(0.05)
        return ("ok", "rid_1")

    def poll_abort() -> None:
        return

    out, rid = _run_worker_in_background_with_poll(worker=worker, poll_abort=poll_abort, poll_interval_s=0.01)
    assert out == "ok"
    assert rid == "rid_1"


def test_background_worker_aborts_early_and_ignores_late_result() -> None:
    started = threading.Event()
    finished = threading.Event()

    def worker() -> tuple[str, str]:
        started.set()
        # Simulate slow OpenAI call; would eventually return but should be ignored.
        time.sleep(0.2)
        finished.set()
        return ("late", "rid_late")

    polls = {"n": 0}

    def poll_abort() -> None:
        polls["n"] += 1
        if polls["n"] >= 2:
            raise _AbortLongOp(reason="supersede", supersede=(1.0, "line", "plan_phu"))

    with pytest.raises(_AbortLongOp):
        _run_worker_in_background_with_poll(worker=worker, poll_abort=poll_abort, poll_interval_s=0.01)

    assert started.is_set()
    # Worker might or might not finish later, but we must have returned control before that.
    assert not finished.is_set()

