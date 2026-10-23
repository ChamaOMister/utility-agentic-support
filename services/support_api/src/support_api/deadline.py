import time
from collections.abc import Callable


class Deadline:
    """A monotonic deadline; every outbound timeout is capped by what remains of it."""

    def __init__(
        self, seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    def timeout(self, per_attempt: float) -> float:
        return max(0.0, min(per_attempt, self.remaining()))

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0
