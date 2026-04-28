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

 # Issue 4: Same fixed delay for every stream
 **Old version:** One `send_frames()` loop applied one `--base-delay-ms/--jitter-ms` to all cameras from the same transport.py instance. No way to give streams different conditions.

 **new_version:** Because get_frame.py now accepts N independent connections, you can run two separate transport.py instances with different delay parameters:

 `python transport.py --sources 0 --host 127.0.0.1 --port 9000 --base-delay-ms 20  --jitter-ms 5  --cam-id-start 0`

`python transport.py --sources 1 --host 127.0.0.1 --port 9000 --base-delay-ms 80  --jitter-ms 30 --cam-id-start 1`

# Issue 5: Varying inter-packet transmission time, not end-to-end latency.
`time.sleep(max(0.0, delay_s))`
`sock.sendall(make_packet(jpeg, ts_ms, cam_id))` in `trasport.py`

The sleep happens before the send. This delays when the packet leaves the sender, not when it arrives at the receiver. The difference matters: in a real network, the packet departs immediately and the delay is imposed in transit. With a sleep-before-send approach you can never produce out-of-order arrival — packet 2 always departs after packet 1 finishes sleeping. A real 80ms delay on packet 1 and a 20ms delay on packet 2 would cause packet 2 to arrive first (reordering), which is a real phenomenon the jitter buffer's min-heap is actually designed to handle.

  > The proper fix is to move the delay to the receiver side: send packets immediately and sleep in _receive_loop before pushing to the sync buffer. That simulates the packet spending time in the network rather than sitting at the sender.

# Issue 6: Uniform Random jitter vs. bursty jitter

`delay_s = (base_delay_ms + random.uniform(-jitter_ms, jitter_ms)) / 1000.0` in `transport.py`.

Real network jitter is temporally correlated — when a router becomes congested, the next 10–20 packets all experience elevated delay together, then conditions recover. Independent uniform noise per packet produces a smooth distribution that understates how badly a real jitter burst can affect a streaming buffer, and understates how important `buffer_delay_ms` depth really is.






