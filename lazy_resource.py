"""Resolve read-only resource handles on first use, never during app import."""
import threading

_UNSET = object()


class LazyResource:
    def __init__(self, factory):
        self._factory = factory
        self._value = _UNSET
        self._lock = threading.RLock()

    def _resolve(self):
        if self._value is _UNSET:
            with self._lock:
                if self._value is _UNSET:
                    # Publish only a fully initialized handle. Failed lookups
                    # remain unresolved so a subsequent request can retry.
                    value = self._factory()
                    self._value = value
        return self._value

    def __getattr__(self, name):
        return getattr(self._resolve(), name)
