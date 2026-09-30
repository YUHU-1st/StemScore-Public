from __future__ import annotations

import sys

import pytest

from stemscore.audio import run_logged


def test_run_logged_includes_useful_child_error_detail() -> None:
    messages: list[str] = []
    with pytest.raises(RuntimeError, match="error: synthetic failure"):
        run_logged(
            [
                sys.executable,
                "-c",
                "print('error: synthetic failure'); print('cleanup line'); raise SystemExit(1)",
            ],
            messages.append,
        )

    assert "error: synthetic failure" in messages
