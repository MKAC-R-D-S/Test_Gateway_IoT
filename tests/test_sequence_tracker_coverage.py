from analytics.sequence_tracker import SeqState


def test_observe_coverage_accepts_legacy_and_current_call_shapes():
    state = SeqState(key="node-1", node_uid="node-1")

    state.observe_coverage(["gw1", "gw2"], "gw2", -45.1, 8.2)

    assert state.gateways == {"GW1", "GW2"}
    assert state.gateways_heard == 2
    assert state.gateways_heard_max == 2
