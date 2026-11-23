from pathlib import Path

from courtside.recording import RecordingWriter, read_recording, replay_tops


def test_round_trip_and_replay(tmp_path: Path) -> None:
    w = RecordingWriter(tmp_path / "g.jsonl", {"event_ticker": "E"}, now_ms=1)
    w.write(10, {"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}})
    w.write(
        11,
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": "A",
                "yes_dollars_fp": [["0.4000", "1.00"]],
                "no_dollars_fp": [["0.5800", "1.00"]],
            },
        },
    )
    w.write(
        12,
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 2,
            "msg": {
                "market_ticker": "A",
                "side": "yes",
                "price_dollars": "0.4100",
                "delta_fp": "2.00",
            },
        },
    )
    path = w.close()
    assert path.name == "g.jsonl.gz" and not (tmp_path / "g.jsonl").exists()

    meta, msgs = read_recording(path)
    assert meta == {"event_ticker": "E"}
    assert [t for t, _ in msgs] == [10, 11, 12]

    tops = list(replay_tops(path))
    assert [(t, top.yes_bid, top.yes_ask, top.t_ms) for t, top in tops] == [
        ("A", 40, 42, 11),
        ("A", 41, 42, 12),
    ]


def test_torn_last_line_is_tolerated(tmp_path: Path) -> None:
    w = RecordingWriter(tmp_path / "g.jsonl", {}, now_ms=1)
    w.write(5, {"type": "x"})
    w.flush()
    with (tmp_path / "g.jsonl").open("a") as f:
        f.write('{"t": 6, "m": {"ty')  # simulated crash mid-write
    _, msgs = read_recording(tmp_path / "g.jsonl")
    assert [t for t, _ in msgs] == [5]
