from support_api.deadline import Deadline


def test_deadline_caps_timeout(fake_clock):
    d = Deadline(45, clock=fake_clock)
    fake_clock.advance(40)
    assert d.timeout(20) == 5 and not d.expired
    fake_clock.advance(6)
    assert d.timeout(20) == 0 and d.expired


def test_deadline_remaining_never_negative_and_per_attempt_wins_when_smaller(
    fake_clock,
):
    d = Deadline(45, clock=fake_clock)
    assert d.remaining() == 45
    assert d.timeout(3) == 3
    fake_clock.advance(100)
    assert d.remaining() == 0
