# Judge access to the live AWS endpoint

KiroClops runs its OpenCV 5 measurement on an AWS Graviton4 worker that boots from the AWS Marketplace "Cloud Optimized OpenCV for AWS Graviton4" image (COOL, OpenCV 5.1.0-dev). The service is an authenticated HTTP API (`https://2oq1i28pd2.execute-api.us-east-1.amazonaws.com`, us-east-1). Two judge accounts exist. Their usernames and passwords are delivered through the Devpost submission form's private notes, never in this repository.

The worker stops after five idle minutes and wakes on the first job (about one minute from stopped to first result, measured 45.9 s). The service stays up until judging ends on 9 November 2026.

## 1. Prerequisites (any OS, about five minutes)

```bash
git clone <repository url> kiroclops && cd kiroclops
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                                      # opencv-python 5.0.0.93, numpy 2.2.6 (needed only for --compare-local)
```

Python 3.10 or newer. The cloud client itself uses the standard library only.

## 2. Log in

Copy the bootstrap configuration and log in; the password prompt is interactive and nothing is echoed or stored in plain text (Windows DPAPI or a mode-0600 file).

```bash
cp submission/judge-service.json service.json
python -m kiroclops.cloud --config service.json login --username <judge username from the private notes>
python -m kiroclops.cloud --config service.json usage
```

`usage` shows the account's quota: one active job, 1,800 compute-seconds per day, 2 GiB storage.

## 3. Measure a bundled recording on the COOL worker

```bash
python scripts/judge_check.py --config service.json --sample svg-three-to-four-w960-r1 --output judge-check-01
python scripts/judge_check.py --config service.json --sample game-reference-h03 --output judge-check-02 --compare-local
```

The script uploads the sample package (frames with hashed timestamps plus the selection and contract used in the Phase 3B calibration corpus), waits for the job, and prints the worker's signed delivery receipt: OpenCV version and architecture (`5.1.0-dev`, `aarch64`), the mapped `libopencv_*.so.5.1.0` libraries that identify the COOL build, the worker wall time, and the measurement summary (objects, valid-frame ratios, events). With `--compare-local` it measures the same recording with the local OpenCV 5.0.0 wheel and runs the service's cross-engine equivalence policy; the expected outcome is `equivalent_excluding_build_record_and_phase_shifts: true`: the only differences are the engine build record that V6 writes into every result and the last digits of a report-only phase-correlation camera diagnostic, which differ between x86 and Arm FFT implementations (see the technical report, §5.2). Every tracked position, validity flag and event is compared exactly.

Everything the script wrote is in the output folder: `judge-check.json` (summary), `client/jobs/<job>/delivery-v2.json` (receipt and full measurement), `client/jobs/<job>/result.json`.

## 4. Log out

```bash
python -m kiroclops.cloud --config service.json logout
```

This revokes the session capability on the service and deletes the local credential files.

## 5. If something fails

* `cloud_login_required` / `Service capability expired`: log in again (capabilities last one hour).
* The first job stays `queued` for up to about 90 seconds while the worker boots; the script waits up to seven minutes.
* `quota` errors: the account already has an active job or exceeded its daily compute seconds; wait or use the other account.
* Any other error: the contact in the Devpost submission; a live screen-share can be arranged.

## 6. What this proves and what it does not

It proves that the deployed worker runs the COOL build of OpenCV 5 on Graviton4, measures a real KiroClops recording, and returns the same numbers as the local OpenCV 5.0.0 engine within float32 rounding. It does not run a Kiro agent session: the agent loop needs a local Chromium capture environment and a Kiro CLI subscription, and is demonstrated in the video and the recorded session traces (`submission/AGENTIC_VISION.md`).
