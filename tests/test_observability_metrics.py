from hacienda_gpt.observability.metrics import MetricsCollector


def test_metrics_collector_computes_rates_and_errors() -> None:
    m = MetricsCollector()

    with m.timed_stage("interpreter"):
        pass

    try:
        with m.timed_stage("rules_engine"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    m.track_turn(uncertainty_count=1, human_handoff=True)
    m.track_turn(uncertainty_count=0, human_handoff=False)

    snap = m.snapshot()
    assert snap["total_turns"] == 2
    assert snap["uncertainty_rate"] == 0.5
    assert snap["human_handoff_rate"] == 0.5
    assert snap["errors_by_module"]["rules_engine"] == 1
    assert len(snap["latency_by_stage_ms"]) == 2
