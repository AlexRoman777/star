import uuid

from star.services.highlight_ranking import RankedMatch, fit_regularized_plackett_luce


def test_repeated_winner_ranks_above_repeated_loser() -> None:
    first, second, third, fourth = (uuid.uuid4() for _ in range(4))
    match = RankedMatch((first, second, third, fourth), (first, second, third, fourth), 0.9)
    estimates = fit_regularized_plackett_luce({first, second, third, fourth}, [match] * 6)
    assert estimates[first].conservative_score > estimates[fourth].conservative_score
    assert estimates[first].sigma < 8.333
    assert estimates[first].confidence_low < estimates[first].mu < estimates[first].confidence_high
    assert estimates[first].confidence_low < estimates[first].mu < estimates[first].confidence_high


def test_position_rotation_does_not_erase_visible_preference() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    matches = [
        RankedMatch((first, second), (first, second)),
        RankedMatch((second, first), (first, second)),
    ] * 4
    estimates = fit_regularized_plackett_luce({first, second}, matches)
    assert estimates[first].mu > estimates[second].mu
    assert abs(estimates[first].position_bias) < 1.0


def test_human_anchor_prior_does_not_override_match_evidence() -> None:
    anchor, candidate = uuid.uuid4(), uuid.uuid4()
    match = RankedMatch((anchor, candidate), (candidate, anchor), 0.9)
    estimates = fit_regularized_plackett_luce(
        {anchor, candidate},
        [match] * 4,
        anchor_priors={anchor: (30.0, 6.0)},
    )
    assert estimates[anchor].sigma < estimates[candidate].sigma
    assert estimates[candidate].mu > estimates[anchor].mu


def test_human_anchor_rating_value_does_not_change_visible_order() -> None:
    anchor, candidate = uuid.uuid4(), uuid.uuid4()
    match = RankedMatch((anchor, candidate), (candidate, anchor), 0.9)
    estimates = fit_regularized_plackett_luce(
        {anchor, candidate},
        [match] * 4,
        anchor_priors={anchor: (45.0, 6.0)},
    )
    assert estimates[anchor].sigma < estimates[candidate].sigma
    assert estimates[candidate].mu > estimates[anchor].mu
