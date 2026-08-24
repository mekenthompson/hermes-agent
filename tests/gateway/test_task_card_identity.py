"""Public task-card titles derive safely from explicit profile metadata."""

from __future__ import annotations

import random
import string
import unicodedata

import pytest
import yaml

from gateway.task_card_identity import (
    GENERIC_TASK_CARD_TITLE,
    resolve_task_card_title,
)


def _write_profile(home, display_name):
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").write_text(
        yaml.safe_dump({"display_name": display_name}, allow_unicode=True),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "display_name, expected",
    [
        ("Marko", "Marko is working"),
        ("  Marko   Agent  ", "Marko Agent is working"),
        ("小助手", "小助手 is working"),
        ("Jose\u0301", "José is working"),
        ("A\n\tB", "A B is working"),
        ("A\x00B", "AB is working"),
        ("A\u202eB", "AB is working"),
        ("👩\u200d💻", "👩\u200d💻 is working"),
    ],
)
def test_resolve_task_card_title_sanitizes_valid_string_metadata(
    tmp_path, display_name, expected
):
    _write_profile(tmp_path, display_name)

    assert resolve_task_card_title(tmp_path) == expected


@pytest.mark.parametrize("display_name", [None, "", " \t\n ", [], {}, 0, False])
def test_resolve_task_card_title_uses_exact_fallback_for_unusable_metadata(
    tmp_path, display_name
):
    _write_profile(tmp_path, display_name)

    assert resolve_task_card_title(tmp_path) == GENERIC_TASK_CARD_TITLE


def test_resolve_task_card_title_uses_fallback_for_missing_profile(tmp_path):
    assert resolve_task_card_title(tmp_path) == GENERIC_TASK_CARD_TITLE


def test_resolve_task_card_title_uses_fallback_for_malformed_yaml(tmp_path):
    (tmp_path / "profile.yaml").write_text("display_name: [", encoding="utf-8")

    assert resolve_task_card_title(tmp_path) == GENERIC_TASK_CARD_TITLE


def test_resolve_task_card_title_is_bounded_without_dangling_combining_or_joiner(
    tmp_path,
):
    _write_profile(tmp_path, "a" * 200 + "e\u0301\u200d")

    title = resolve_task_card_title(tmp_path)
    name = title.removesuffix(" is working")
    assert len(name) <= 64
    assert name[-1] != "\u200d"
    assert not unicodedata.category(name[-1]).startswith("M")


def test_profile_paths_are_explicit_and_do_not_cross_contaminate(tmp_path):
    marko_home = tmp_path / "marko"
    other_home = tmp_path / "other"
    _write_profile(marko_home, "Marko")
    _write_profile(other_home, "Other Agent")

    assert resolve_task_card_title(marko_home) == "Marko is working"
    assert resolve_task_card_title(other_home) == "Other Agent is working"
    assert "Marko" not in resolve_task_card_title(other_home)


def test_randomized_titles_are_safe_and_deterministic(tmp_path):
    rng = random.Random(94731)
    alphabet = (
        string.ascii_letters
        + string.digits
        + " \t\n\r"
        + "小助手é👩\u200d💻"
        + "\x00\x1f\u202a\u202e\u2066\u2069"
    )

    for index in range(250):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 180)))
        home = tmp_path / str(index)
        _write_profile(home, raw)
        first = resolve_task_card_title(home)
        second = resolve_task_card_title(home)

        assert first == second
        assert first
        assert first == GENERIC_TASK_CARD_TITLE or first.endswith(" is working")
        assert "\n" not in first and "\r" not in first and "\t" not in first
        assert not any(
            char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
            for char in first
        )
        assert len(first) <= 64 + len(" is working")
