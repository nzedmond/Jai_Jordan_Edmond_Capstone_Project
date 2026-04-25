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