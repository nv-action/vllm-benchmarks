from .batches import batch_requests


def test_keeps_final_partial_batch():
    assert batch_requests(["a", "b", "c"], 2) == [["a", "b"], ["c"]]


def test_empty_input():
    assert batch_requests([], 2) == []
