import json

import bridge
from bridge import R900Resolver, is_bcd_misparse, map_message, map_r900


# Real-world shapes, anonymized IDs. The expected dicts are field-for-field
# what rtl_433 publishes for the same meters (minus receiver-side fields
# like rssi/freq/snr that a different receiver cannot reproduce).

R900_BINARY = {"ID": 1582600001, "Unkn1": 163, "NoUse": 3, "BackFlow": 0,
               "Consumption": 228633, "Unkn3": 0, "Leak": 0, "LeakNow": 0}
# Same transmission decoded by the BCD parser: a nibble > 9 -> 0
R900_BINARY_AS_BCD = dict(R900_BINARY, Consumption=0)

# A BCD meter: true reading 16000, binary misparse = 0x16000 = 90112
R900_BCD = {"ID": 1485200001, "Unkn1": 161, "NoUse": 0, "BackFlow": 0,
            "Consumption": 16000, "Unkn3": 0, "Leak": 15, "LeakNow": 3}
R900_BCD_AS_BINARY = dict(R900_BCD, Consumption=90112)

SCM = {"ID": 61014001, "Type": 4, "TamperPhy": 0, "TamperEnc": 0,
       "Consumption": 7649}

SCMPLUS = {"FrameSync": 5795, "ProtocolID": 30, "EndpointType": 7,
           "EndpointID": 1554100001, "Consumption": 1450537,
           "Tamper": 513, "PacketCRC": 32713}


def test_bcd_misparse_identity():
    assert is_bcd_misparse(90112, 16000)          # 0x16000 == 90112
    assert is_bcd_misparse(1065216, 104100)       # 0x104100 == 1065216
    assert is_bcd_misparse(1610645, 189395)       # 0x189395 == 1610645
    assert not is_bcd_misparse(228633, 0)
    assert not is_bcd_misparse(228633, 228633)
    assert not is_bcd_misparse(None, 16000)


def test_map_r900_matches_rtl433_fields():
    event = map_r900(R900_BINARY)
    assert event["model"] == "Neptune-R900"
    assert event["id"] == 1582600001
    assert event["consumption"] == 228633
    for field in ("unkn1", "nouse", "backflow", "unkn3", "leak", "leaknow"):
        assert field in event


def test_map_scm_matches_rtl433_fields():
    event = map_message("SCM", SCM)
    assert event == {"model": "ERT-SCM", "id": 61014001,
                     "physical_tamper": 0, "ert_type": 4,
                     "encoder_tamper": 0, "consumption_data": 7649}


def test_map_scmplus_matches_rtl433_fields():
    event = map_message("SCM+", SCMPLUS)
    assert event["model"] == "SCMplus"
    assert event["id"] == 1554100001
    assert event["ProtocolID"] == "0x1E"
    assert event["EndpointType"] == "0x07"
    assert event["Consumption"] == 1450537
    assert event["Tamper"] == "0x0201"
    assert event["PacketCRC"] == "0x7FC9"
    assert event["MeterType"] == "Electric"


def _resolver(**kwargs):
    return R900Resolver("auto", pairing=True, window=0.0, **kwargs)


def test_resolver_picks_binary_when_bcd_parse_is_zero():
    resolver = _resolver()
    assert resolver.feed("R900", R900_BINARY, "t1") == []
    events = resolver.feed("R900BCD", R900_BINARY_AS_BCD, "t1")
    assert len(events) == 1
    event, _ = events[0]
    assert event["consumption"] == 228633
    assert event["r900_encoding"] == "binary"
    # decision is sticky: later packets publish immediately, wrong kind dropped
    assert resolver.feed("R900", R900_BINARY, "t2")[0][0]["consumption"] == 228633
    assert resolver.feed("R900BCD", R900_BINARY_AS_BCD, "t2") == []


def test_resolver_detects_bcd_meter():
    resolver = _resolver()
    assert resolver.feed("R900BCD", R900_BCD, "t1") == []
    events = resolver.feed("R900", R900_BCD_AS_BINARY, "t1")
    assert len(events) == 1
    event, _ = events[0]
    assert event["consumption"] == 16000          # corrected, not 90112
    assert event["r900_encoding"] == "bcd"
    assert resolver.feed("R900", R900_BCD_AS_BINARY, "t2") == []
    assert resolver.feed("R900BCD", R900_BCD, "t2")[0][0]["consumption"] == 16000


def test_resolver_id_overrides():
    resolver = R900Resolver("auto", pairing=True,
                            binary_ids=["1485200001"], window=0.0)
    # forced binary: the (actually-BCD) meter publishes the binary parse
    events = resolver.feed("R900", R900_BCD_AS_BINARY, "t1")
    assert events[0][0]["consumption"] == 90112
    assert resolver.feed("R900BCD", R900_BCD, "t1") == []


def test_resolver_flush_publishes_unpaired():
    resolver = _resolver()
    assert resolver.feed("R900", R900_BINARY, "t1") == []
    events = resolver.flush()   # window=0 -> immediately stale
    assert len(events) == 1
    assert events[0][0]["consumption"] == 228633


def test_resolver_forced_mode_skips_pairing():
    resolver = R900Resolver("bcd", pairing=False)
    assert resolver.feed("R900", R900_BCD_AS_BINARY, "t1") == []
    events = resolver.feed("R900BCD", R900_BCD, "t1")
    assert events[0][0]["consumption"] == 16000


def test_rtl433_time_formats_local():
    formatted = bridge.rtl433_time("2026-06-12T19:37:36.027Z")
    assert len(formatted) == 19 and formatted[4] == "-" and formatted[13] == ":"
    # garbage falls back to "now" without raising
    assert len(bridge.rtl433_time(None)) == 19


def test_event_json_is_rtl433_shaped():
    event = map_r900(R900_BINARY, encoding="binary")
    data = {"time": "2026-06-12 19:37:36"}
    data.update({k: v for k, v in event.items() if v is not None})
    parsed = json.loads(json.dumps(data))
    assert list(parsed)[0] == "time"
    assert parsed["model"] == "Neptune-R900"
