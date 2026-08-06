from midicrt import proto


def test_encode_roundtrip_and_framing():
    data = proto.encode({"a": 1}) + proto.encode({"b": 2})
    dec = proto.LineDecoder()
    assert dec.feed(data) == [{"a": 1}, {"b": 2}]


def test_decoder_handles_partial_lines():
    dec = proto.LineDecoder()
    raw = proto.encode({"x": "hello"})
    assert dec.feed(raw[:5]) == []
    assert dec.feed(raw[5:]) == [{"x": "hello"}]


def test_message_builders():
    assert proto.response(3, {"ok": 1}) == {"id": 3, "ok": True, "data": {"ok": 1}}
    assert proto.error_response(4, "nope") == {"id": 4, "ok": False, "error": "nope"}
    snap = proto.snapshot("page.eventlog", 7, {"lines": []})
    assert snap == {"kind": "snapshot", "topic": "page.eventlog", "seq": 7, "data": {"lines": []}}
    ev = proto.event("learn_bound", {"action": "x"})
    assert ev == {"kind": "event", "name": "learn_bound", "data": {"action": "x"}}
    assert proto.hello()["kind"] == "hello"
    assert proto.major("1.4.2") == 1
