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


# --- supervisor recovery (rtl_tcp recycling on USB re-enumeration) ----------

class _FakeProc:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_recycle_terminates_proc_and_flags_requested():
    sup = bridge.Supervisor()
    proc = _FakeProc()
    sup.rtl_tcp_proc = proc
    sup.recycle_rtl_tcp("test")
    assert proc.terminated
    assert sup.rtl_tcp_recycling is True


def test_recycle_respects_cooldown():
    sup = bridge.Supervisor()
    first = _FakeProc()
    sup.rtl_tcp_proc = first
    sup.recycle_rtl_tcp("first")
    assert first.terminated
    # a second recycle within the cooldown must not kill the fresh rtl_tcp
    second = _FakeProc()
    sup.rtl_tcp_proc = second
    sup.rtl_tcp_recycling = False
    sup.recycle_rtl_tcp("too soon")
    assert second.terminated is False
    assert sup.rtl_tcp_recycling is False


def test_recycle_without_proc_is_noop():
    sup = bridge.Supervisor()
    sup.rtl_tcp_proc = None
    sup.recycle_rtl_tcp("nothing running")  # must not raise


def test_mark_data_resets_age_and_writes_heartbeat(tmp_path, monkeypatch):
    heartbeat = tmp_path / "hb"
    monkeypatch.setattr(bridge, "HEARTBEAT_FILE", str(heartbeat))
    sup = bridge.Supervisor()
    sup.mark_data()
    assert heartbeat.exists()
    assert sup.data_age() < 1.0


# rtlamr's default symbol length (72 -> 2.36 MS/s) outruns the decoder on many
# hosts, which shows up as a pegged core plus a flood of drop errors.

def test_symbollength_default_is_applied(monkeypatch):
    monkeypatch.setattr(bridge, "RTLAMR_SYMBOLLENGTH", "32")
    monkeypatch.setattr(bridge, "RTLAMR_ARGS", "")
    assert "-symbollength=32" in bridge.Supervisor().rtlamr_cmd()


def test_explicit_symbollength_arg_wins(monkeypatch):
    monkeypatch.setattr(bridge, "RTLAMR_SYMBOLLENGTH", "32")
    monkeypatch.setattr(bridge, "RTLAMR_ARGS", "-symbollength=72")
    cmd = bridge.Supervisor().rtlamr_cmd()
    assert "-symbollength=72" in cmd
    assert "-symbollength=32" not in cmd


def test_empty_symbollength_leaves_rtlamr_default(monkeypatch):
    monkeypatch.setattr(bridge, "RTLAMR_SYMBOLLENGTH", "")
    monkeypatch.setattr(bridge, "RTLAMR_ARGS", "")
    assert not any(a.startswith("-symbollength")
                   for a in bridge.Supervisor().rtlamr_cmd())


def test_drain_stderr_collapses_drop_flood(monkeypatch, capsys, caplog):
    # One summary for the whole flood, not one line per dropped block.
    monkeypatch.setattr(bridge, "RTLAMR_DROP_SUMMARY_INTERVAL", 3600.0)
    lines = ['level=ERROR msg="not keeping up with rtl_tcp" rate=2351104\n'] * 5000
    with caplog.at_level("WARNING"):
        bridge.Supervisor().drain_stderr(iter(lines))
    assert capsys.readouterr().err == ""
    summaries = [r for r in caplog.records if "fell behind" in r.getMessage()]
    assert len(summaries) == 1
    assert "5000 dropped block(s)" in summaries[0].getMessage()


def test_drain_stderr_forwards_other_lines(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "RTLAMR_DROP_SUMMARY_INTERVAL", 3600.0)
    lines = ['level=ERROR msg="not keeping up with rtl_tcp"\n',
             'level=INFO msg="something worth reading"\n']
    bridge.Supervisor().drain_stderr(iter(lines))
    err = capsys.readouterr().err
    assert "something worth reading" in err
    assert "not keeping up" not in err
