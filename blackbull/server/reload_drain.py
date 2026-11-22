"""Notify the selected protocol that its worker is retiring."""

from collections.abc import Callable


class ReloadDrain:
    __slots__ = ('requested', '_listeners')

    def __init__(self) -> None:
        self.requested = False
        self._listeners: list[Callable[[], None]] = []

    def register(self, notify: Callable[[], None]) -> None:
        self._listeners.append(notify)
        if self.requested:
            notify()

    def unregister(self, notify: Callable[[], None]) -> None:
        self._listeners.remove(notify)

    def begin(self) -> None:
        self.requested = True
        for notify in self._listeners:
            notify()
