from __future__ import annotations

import io

from makepatch.term import paint


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_paint(monkeypatch):
    for name in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    assert paint("x", "red", stream=io.StringIO()) == "x"
    assert paint("x", "red", "bold", stream=FakeTTY()) == "\033[31;1mx\033[0m"
    assert paint("x", stream=FakeTTY()) == "x"

    monkeypatch.setenv("FORCE_COLOR", "1")
    assert paint("x", "green", stream=io.StringIO()) == "\033[32mx\033[0m"
    monkeypatch.setenv("NO_COLOR", "1")
    assert paint("x", "green", stream=FakeTTY()) == "x"

    monkeypatch.delenv("NO_COLOR")
    monkeypatch.delenv("FORCE_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert paint("x", "green", stream=FakeTTY()) == "x"
