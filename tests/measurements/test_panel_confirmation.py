import copy

from field_recognition.measurements.panel_confirmation import PanelConfirmation


def region(text="0.0000", instrument="B", slot="0", box=None):
    return {
        "instrument_id": instrument,
        "panel_id": slot,
        "bbox": box or [0, 0, 100, 40],
        "local_ocr": {"lines": [{"text": text, "panel_id": slot}] if text else []},
    }


def observe(voter, n, regions, scope="session1", now=None):
    return voter.observe(regions, (n,), str(n), n * 0.5 if now is None else now, scope)


def test_two_of_three_tolerates_one_wrong_frame_and_requires_current_value():
    v = PanelConfirmation()
    assert observe(v, 0, [region()])[0]["temporal_confirmation"]["status"] == "pending"
    assert observe(v, 1, [region("8.0000")])[0]["text"] == "8.0000"
    r = region()
    lines = observe(v, 2, [r])
    assert lines[0]["text"] == "0.0000" and lines[0]["temporal_confirmation"]["votes"] == 2
    assert v.changed(r)
    v.mark_saved([r])
    assert not v.changed(r)
    # A varying current observation is preserved without substituting an old majority.
    latest = observe(v, 3, [region("8.8888")])[0]
    assert latest["text"] == "8.8888" and latest["temporal_confirmation"]["status"] == "pending"


def test_same_frame_never_counts_twice_and_expired_votes_are_discarded():
    v = PanelConfirmation()
    assert observe(v, 1, [region()], now=0)
    for t in (0.2, 0.4):
        assert not observe(v, 1, [region()], now=t)
    assert observe(v, 2, [region()], now=2)[0]["temporal_confirmation"]["votes"] == 1
    assert observe(v, 3, [region()], now=2.5)


def test_each_instrument_and_each_window_votes_independently():
    v = PanelConfirmation()
    first = [region("100", "A", "0"), region("200", "A", "1"), region("0.0000", "B", "0")]
    observe(v, 0, first)
    v.mark_saved(first)
    second = copy.deepcopy(first)
    second[0]["local_ocr"]["lines"][0]["text"] = "999"
    lines = observe(v, 1, second)
    assert [l["text"] for l in lines] == ["999", "200", "0.0000"]
    assert [l["temporal_confirmation"]["votes"] for l in lines] == [1, 2, 2]
    assert v.changed(second[0]) and not v.changed(second[1]) and not v.changed(second[2])


def test_new_binding_scope_absent_panel_or_large_motion_cannot_reuse_votes():
    for interrupt in ("scope", "absent", "motion"):
        v = PanelConfirmation()
        observe(v, 0, [region()])
        if interrupt == "scope":
            lines = observe(v, 1, [region()], scope="session2")
        elif interrupt == "absent":
            observe(v, 1, [])
            lines = observe(v, 2, [region()])
        else:
            lines = observe(v, 1, [region(box=[200, 200, 300, 240])])
        assert lines[0]["temporal_confirmation"]["votes"] == 1
        assert lines[0]["temporal_confirmation"]["status"] == "pending"


def test_periodic_observation_requires_fresh_frame_and_same_binding_without_consensus():
    voter = PanelConfirmation()
    first, second = region(), region()
    observe(voter, 0, [first])
    observe(voter, 1, [second])
    voter.mark_saved([second], 0.5)
    assert not voter.periodic_due(second, 30.4, 30)
    after_gap = region()
    observe(voter, 61, [after_gap], now=30.5)
    assert voter.periodic_due(after_gap, 30.5, 30)
    assert after_gap["temporal_confirmation"]["votes"] == 1
    duplicate = region()
    assert not observe(voter, 61, [duplicate], now=30.6)
    assert not voter.periodic_due(duplicate, 30.6, 30)
    fresh = region()
    observe(voter, 62, [fresh], now=31)
    assert voter.periodic_due(fresh, 31, 30)
    new_binding = region()
    observe(voter, 63, [new_binding], scope="another-binding", now=31.5)
    assert not voter.periodic_due(new_binding, 31.5, 30)


def test_invalid_or_rejected_ocr_is_not_recordable_even_when_repeated():
    for text, issue in [("not a number", None), ("1.2345", "display_precision_mismatch")]:
        voter = PanelConfirmation()
        for n in range(3):
            r = region(text)
            r["local_ocr"]["lines"][0]["quality_issues"] = [issue] if issue else []
            assert not observe(voter, n, [r])
            assert not voter.changed(r)
