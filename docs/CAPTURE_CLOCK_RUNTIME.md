# Real-time Windows capture clock

The local Windows adapter uses an owned Proactor loop whose monotonic clock
reads Python's actual `perf_counter` (QueryPerformanceCounter on the pinned
Python3.10 runtime). It does not install a global event-loop policy, modify
Python's clocks, supply virtual browser time or shift recorded timestamps.
Python documents the loop's time API as its internal monotonic clock.
[Python3.10 event-loop documentation](https://docs.python.org/3.10/library/asyncio-eventloop.html#asyncio.loop.time).

In the actual host probe, changing only the loop clock left a1ms requested
sleep at15.58ms median. An additional scoped `timeBeginPeriod(1)` request
reduced it to2.01ms; requested16.67ms sleeps changed from31.53ms to17.55ms.
The adapter therefore requests1ms only during its owned capture, and pairs
every successful request with `timeEndPeriod(1)` in `finally`. Startup errors,
operation errors, cancellation and loop cleanup errors all release the
request. It requires Windows10 build19041 or newer: Microsoft documents
per-process behavior from Windows10 version2004 onward. This is a request,
not a timing guarantee; the existing measured capture gates still apply.
[Microsoft timer API contract](https://learn.microsoft.com/en-us/windows/win32/api/timeapi/nf-timeapi-timebeginperiod).

Host image brackets read actual UTC using `GetSystemTimePreciseAsFileTime`,
converting native FILETIME units to Unix epoch seconds. Microsoft describes
this API as high-precision UTC and recommends the performance counter for
elapsed intervals. Its documented precision is not a claim of measured UTC
accuracy. Browser timestamps and host brackets remain independently observed.
[Microsoft precise UTC API](https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-getsystemtimepreciseasfiletime).

Private `clock-runtime.json` records the actual implementations, scheduler
resolution, documented UTC precision bound, timer begin/end results, release
status and loop cleanup status. `capability_check()` performs a read-only
doctor check and never calls either timer-period function. Unsupported APIs
or timer requests produce `capture_environment_failure`; they cannot produce
a valid recording.

The frame coordinator gives one pending response-boundary frame precedence
over a queued duplicate pump image. At drag start it performs one current
visible hit lookup followed by one real frame, then releases the frame lock
before dispatching trusted pointerdown. Boundaries use BeginFrame without a
screenshot request and retain a completed-frame command receipt. They do not
create an image observation or supply a substitute timestamp. Actual pixels
still come from browser consumer packets and routine BeginFrame PNGs. This
keeps PNG encoding off the input-critical boundary; missing real pre-input
observations still fail the unchanged coverage gate. The pump continues while native input
ACKs are awaited. Wrong or moved targets are still rejected by actual input
receipts and the private application checks. Lock wait, hit lookup and draw
durations are recorded separately; input schedules and thresholds are unchanged.

Preserved diagnostic probes:

- `artifacts/phase3a/refs05-input-latency-diagnosis-01`
- `artifacts/phase3a/refs05-qpc-timer-probe-01`
- `artifacts/phase3a/refs05-qpc-period-timer-probe-01`

These are timer/engineering probes, not native model or transfer acceptance.
Fresh panel/SVG captures must validate the final recorder separately.

## Version4: browser observation time and PNG delivery order

The recorder pins Chromium153.0.8010.12 for its declared transport policy.
That implementation creates browser-consumer metadata for a captured frame,
then dispatches separate PNG encoding jobs. Encoding completion can change
event delivery order while each image remains bound to its original metadata.
[Exact Chromium153 PageHandler source](https://chromium.googlesource.com/chromium/src/+/refs/tags/153.0.8010.12/content/browser/devtools/protocol/page_handler.cc).

Before frame merging, a fixed validator requires finite unique original
metadata timestamps, increasing local receive ordinals and receive times,
actual segment bounds, no future observation, the existing one-second delivery
bound, and no inversion greater than120ms. It permutes whole image/metadata
packets only. Equal timestamps always fail; image equality cannot remove a
packet. Host-bracket order remains strict. The unchanged merger still rejects
unvalidated source inversion and overlapping clock intervals.

Raw received PNGs, arrival order and original timestamps are preserved before
normalization. `private/transport-order.json` binds that raw receipt hash,
every original browser packet's PNG hash, and the complete raw-index
permutation. It records no removed PNG or changed timestamp. Local receive
ordinals are explicitly not browser frame counters. A different Chromium
version requires verification of its transport semantics before acceptance.

Observation production ends at the actual screencast-stop response. Its PNG
encoding replies can arrive later, so the separate delivery interval ends
only after the owned browser closes and its queued receipt tasks settle.
`private/transport-context.json` preserves both actual intervals even if
transport validation fails. Extending the delivery interval never shifts
observation timestamps or admits an observation produced after stream stop.

The legacy `cdp_compositor` source identifier remains readable, but new clock
labels describe **browser-consumer observation metadata time**. A zero-width
source interval identifies the provided timestamp; it does not establish the
time at which those pixels were presented. Package metadata and timing
receipts explicitly declare pixel-presentation uncertainty unknown (`null`),
capture-to-metadata latency unmeasured, and no presentation-accuracy claim.
Actual BeginFrame images retain their separate host request/response brackets.
All existing timing, input-ambiguity and pixel-comparison gates remain in force.
