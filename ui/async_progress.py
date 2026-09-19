"""Bounded, coalesced worker progress; only dispatched callbacks touch widgets."""
import threading
import time


class CoalescedProgress:
    def __init__(self, dispatch, publish, *, interval=0.25, clock=None, timer_factory=None):
        self._dispatch = dispatch
        self._publish = publish
        self._interval = interval
        self._clock = clock or time.monotonic
        self._timer_factory = timer_factory or threading.Timer
        self._lock = threading.Lock()
        self._values = {}
        self._pending = False
        self._closed = False
        self._last_published = None
        self._timer = None

    def update(self, key, value):
        with self._lock:
            if self._closed:
                return
            self._values[key] = value
            if self._pending:
                return
            self._pending = True
        self._post_flush()

    def _post_flush(self):
        with self._lock:
            self._timer = None
            if self._closed:
                self._pending = False
                return
        try:
            self._dispatch(self._flush)
        except Exception:
            with self._lock:
                self._pending = False

    def _flush(self, *, force=False):
        with self._lock:
            if self._closed:
                self._pending = False
                return
            now = self._clock()
            remaining = self._interval - (now - self._last_published) if self._last_published is not None else 0
            if force or remaining <= 0:
                self._pending = False
                self._last_published = now
                snapshot = dict(self._values)
            else:
                snapshot = None
        if snapshot is None:
            try:
                # One short-lived timer supplies a trailing refresh even when
                # every remaining probe is slow. It never touches Tk; closing
                # from any thread can safely cancel it without after_cancel.
                timer = self._timer_factory(remaining, self._post_flush)
                timer.daemon = True
                with self._lock:
                    if self._closed:
                        self._pending = False
                        return
                    self._timer = timer
                timer.start()
            except Exception:
                with self._lock:
                    self._timer = None
                # Resource pressure must not leave a pending update stuck.
                self._flush(force=True)
            return
        self._publish(snapshot)

    def close(self):
        """Invalidate queued partial updates before the caller posts its final result."""
        with self._lock:
            self._closed = True
            self._values.clear()
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
