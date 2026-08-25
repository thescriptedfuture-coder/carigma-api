"""Score-band ladder.

The V2 ladder comes from the LOCKED design system (Brief 2), not from V1. These
tests pin it, because the band name ships in the API payload and a silent drift
would desynchronise the API from the design.

Conflict resolved here: V1 used four bands (buried / underselling / solid /
strong). V2 uses five. The design brief wins on what V2 looks like.
"""

from __future__ import annotations

import pytest

from carigma_api.services.constants import SCORE_BANDS, score_band


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, "FAINT"),
        (39, "FAINT"),
        (40, "EMERGING"),
        (59, "EMERGING"),
        (60, "CLEAR"),
        (74, "CLEAR"),
        (75, "STRONG"),
        (89, "STRONG"),
        (90, "COMMANDING"),
        (100, "COMMANDING"),
    ],
)
def test_band_boundaries(score: int, expected: str) -> None:
    assert score_band(score) == expected


def test_ladder_is_five_bands_and_covers_0_to_100_without_gaps() -> None:
    assert len(SCORE_BANDS) == 5
    assert SCORE_BANDS[0][0] == 0
    assert SCORE_BANDS[-1][1] == 100
    for (_, prev_high, _), (next_low, _, _) in zip(SCORE_BANDS, SCORE_BANDS[1:], strict=False):
        assert next_low == prev_high + 1, "gap or overlap in the band ladder"


def test_v1_band_names_are_gone() -> None:
    """Guard against someone restoring the V1 ladder from the Bible."""
    names = {name for _, _, name in SCORE_BANDS}
    assert not names & {"buried", "underselling", "solid", "strong"}


def test_band_carries_no_colour() -> None:
    """The band is a neutral ladder; colour happens only on MOVEMENT.

    If a colour ever appears in this constant, the API has started encoding a
    judgement the design deliberately withholds.
    """
    for entry in SCORE_BANDS:
        assert len(entry) == 3, "a band gained an extra field — is it a colour?"
        assert isinstance(entry[2], str)
        assert not entry[2].startswith("#")


def test_out_of_range_clamps_rather_than_inventing_a_band() -> None:
    assert score_band(-5) == "FAINT"
    assert score_band(140) == "COMMANDING"


def test_there_is_no_combined_score_helper() -> None:
    """Two instruments, two ladders. Nothing may average them."""
    from carigma_api.services import constants

    for forbidden in ("combined_score", "overall_score", "career_score", "average_score"):
        assert not hasattr(constants, forbidden)
