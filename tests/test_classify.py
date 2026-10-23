import numpy as np

from dronedet.classify import decide
from dronedet.classify.voting import UNKNOWN

C = ["drone", "wifi", "noise"]


def p(*v):
    return np.array(v, dtype=np.float32)


def test_three_of_five_wins():
    votes = [p(.8, .1, .1), p(.7, .2, .1), p(.2, .7, .1), p(.9, .05, .05), p(.3, .6, .1)]
    d = decide(votes, C)
    assert d.label == "drone" and d.votes_for == 3


def test_split_vote_is_unknown():
    votes = [p(.8, .1, .1), p(.1, .8, .1), p(.1, .1, .8), p(.7, .2, .1), p(.2, .7, .1)]
    assert decide(votes, C).label == UNKNOWN


def test_low_confidence_is_unknown():
    votes = [p(.4, .3, .3)] * 5
    assert decide(votes, C).label == UNKNOWN


def test_too_few_votes_is_pending():
    assert decide([p(.9, .05, .05)], C).label is None
