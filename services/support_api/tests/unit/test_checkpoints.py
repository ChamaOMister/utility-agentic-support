import pytest
from support_api.checkpoints import Checkpoints, SimulatedCrash


async def test_default_checkpoint_is_noop():
    assert await Checkpoints().hit("anything") is None


def test_simulated_crash_is_not_an_exception():
    assert issubclass(SimulatedCrash, BaseException)
    assert not issubclass(SimulatedCrash, Exception)
    with pytest.raises(SimulatedCrash):
        raise SimulatedCrash("boom")
