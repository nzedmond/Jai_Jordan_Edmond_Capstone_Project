# issue 1: one TCP connection for all cameras

> - get_frame.py accepts one TCP connection, handling one multiplexed stream. That stream can carry frames from any number of cameras.
> - The socket accept only one connnection, there's no loop to accept additional senders.
> - Sync mode is hardcoded to 2 cameras
> - One failure kills all cameras

## solution:

> One TCP connection per camera. 
  - _receive_loop calls stop.set() when any connection closes, which kills all other threads too. (This is fine)


# Issue 2: Not adre3ssing clock differences

> Every timestamp in the pipeline comes from `datetime.now(tz=timezone.utc)` called on whichever machine is running `transport.py`. The `sync.py` logic treats `ts_ms` values from different cameras as if they were measured on the same clock. `sync_error_ms = max(ts_vals) - min(ts_vals)`. If camera A is on machine 1 and camera B is on machine 2, and machine 2's clock is 200 ms ahead, `sync_error_ms` will show about 200 ms of error even if both cameras captures their frames at exactly the same instant. 

## solution:

> - 1. `Rely on NTP`: ensure both machines sync to the same NTP server. Typical accuracy on this method is 10-50 ms on a LAN and requires nothing in code.
> - 2. `Application layer clock offset estimation`: before thesession starts, the receiver sends a ping to each sender, measures RTT, and estimate the clock offset (similar to how NTP works but between the two machines directly). This can get to 1-5 ms of accuracy. 
> - 3. `PTP (IEEE 1588)`: harware-assisted, sub-millisecond accuracy, but requires OS/hardware support.

> we're going with the second approach. We add a new script, `clock_sync.py`, the NTP-style handshake module. 
 - The receiver sends N pings (each carrying `T1`, its own clock). 
 - The sender echoes back `(T1, T2, T3)`, where `T2/T3` bracket the sender's prcessing time.
 - The receiver records `T4` on arrival. The standard NTP formula gives `offset = ((T2-T1) + (T3-T4)) / 2`, which is the sender's clock lead over the receiver's.
 - Run 8 rounds, take the median. Then the receiver subtracts that offset from every incmoing `ts_ms` before pushing to `SyncBuffer`, correcting all sender timestamps into receiver-local time. 
 - `transport.py`: calls `server_clock_sync(sock)` after each `sock.connect()`, before frame threads start.
 - `get_frame.py`: calls `measure_offset(conn)` after each `server.accept()`, pass the per-connection offset into `_receive_loop`, apply correction there. 

 # Issue 3: Using TCP over UDP

 > - If we decide to use UDP instead, the whole Socket API connection model would disappear. This means the receiver wouldn't know a sender exists until a datagram arrives, and has no way to know when a sender leaves. Additionally, UDP means we either get the whole packet or we don't. 
 > - Another problem is that Ethernet MTU is 1500 bytes while a typical JPEG frame at quality 90 is 30-150 KB. A single `sendto()` call with a 100KB payload either gets fragmented by the IP layer into about 70 fragments, any one of which being dropped silently drops the whole frame with no retransmission, or gets rejected with the max-size depending on the OS. 
 > - We would need to implement application-layer fragmentation to split each JPEG into chunks, add a sequence number and fragment index to each chunk's header, reassemble on the receiver, which, I think, is a reimplementation of what TCP already does. 
 > - Our system already handles out-of order delivery by the `_StreamBuffer` min-heap in `sync.py` by sorting frames on `ts_ms`

 > So, the only thing UPD buys us is the absence of head-of-line blocking, which we already solved by giving each camera its own TCP connection. The fragmentation problem alone makes UDP a poor fit for JPEG payloads, unless we have to write a reassembly layer, at which point we'd have rebuilt most of what TCP gives us for free. 

