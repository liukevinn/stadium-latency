import struct

from courtside.clock import _NTP_EPOCH_DELTA, _ntp_ts


def test_ntp_timestamp_parse() -> None:
    data = bytes(32) + struct.pack("!II", _NTP_EPOCH_DELTA + 1_000, 2**31) + bytes(8)
    assert _ntp_ts(data, 32) == 1_000.5
