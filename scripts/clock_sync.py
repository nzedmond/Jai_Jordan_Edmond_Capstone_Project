"""Application-layer clock offset estimation (NTP-style).

Protocol (runs once per connection before frame streaming begins):

    Receiver                        Sender
       |                               |
       |  --- ping: [T1_ms] ---------> |
       |                        T2 = sender.now()
       |                        T3 = sender.now()
       |  <-- pong: [T1,T2,T3] ------- |
    T4 = receiver.now()
       |
    offset = ((T2 - T1) + (T3 - T4)) / 2
    # offset > 0  =>  sender clock is ahead of receiver clock
    # corrected_ts = raw_sender_ts - offset

Run NUM_ROUNDS exchanges and return the median offset to reduce noise from
transient network spikes.
"""

import socket
import struct
import time

NUM_ROUNDS = 8

# Ping: receiver -> sender   [T1_ms : int64]
PING_FMT = ">q"
PING_SIZE = struct.calcsize(PING_FMT)   # 8 bytes

# Pong: sender -> receiver   [T1_ms : int64][T2_ms : int64][T3_ms : int64]
PONG_FMT = ">qqq"
PONG_SIZE = struct.calcsize(PONG_FMT)  # 24 bytes


def _now_ms() -> int:
    return int(time.time() * 1_000)


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed during clock sync handshake")
        buf += chunk
    return buf


def measure_offset(conn: socket.socket, num_rounds: int = NUM_ROUNDS) -> int:
    """Receiver side: exchange pings with the sender and return the median
    estimated clock offset in milliseconds.

    offset_ms = sender_clock - receiver_clock
    Apply to incoming sender timestamps as: corrected_ts = raw_ts - offset_ms
    """
    offsets = []
    for _ in range(num_rounds):
        T1 = _now_ms()
        conn.sendall(struct.pack(PING_FMT, T1))
        pong = _recv_exact(conn, PONG_SIZE)
        T4 = _now_ms()
        T1_echo, T2, T3 = struct.unpack(PONG_FMT, pong)
        offset = ((T2 - T1_echo) + (T3 - T4)) // 2
        offsets.append(offset)

    offsets.sort()
    return offsets[len(offsets) // 2]


def serve_clock_sync(conn: socket.socket, num_rounds: int = NUM_ROUNDS) -> None:
    """Sender side: respond to pings from the receiver.

    Must be called immediately after connect() and before any frame data is sent.
    """
    for _ in range(num_rounds):
        ping = _recv_exact(conn, PING_SIZE)
        T2 = _now_ms()
        (T1,) = struct.unpack(PING_FMT, ping)
        T3 = _now_ms()
        conn.sendall(struct.pack(PONG_FMT, T1, T2, T3))
