class SimulatedCrash(BaseException):
    """Raised by tests at a checkpoint to emulate a process dying mid-turn.

    Derives from BaseException so no `except Exception` in the code under test can swallow it.
    """


class Checkpoints:
    """Named hook points inside the turn pipeline. The default does nothing; tests subclass it."""

    async def hit(self, name: str) -> None:
        return None
