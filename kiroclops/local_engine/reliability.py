"""Local runtime preparation and bounded, unevaluated capture readiness.

No function starts a browser, Godot, a model or an AWS operation. Warm-up
captures are supplied by the caller and retain their own timing thresholds.
Godot ignores the parent's TMP: check the actual per-operation paths instead.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import time

from kiroclops.visual.schema import VisualError, file_sha256, write_json


# V6 plan WP4.4: the short runtime root is a machine setting, not a repository constant.
DEFAULT_RUNTIME_ROOT = Path(os.environ.get("KIROCLOPS_RUNTIME_ROOT", "C:/kc3d"))
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
COMMON_CAPTURE_LOCK = WORKSPACE_ROOT / "artifacts/local-engine/capture.lock"
MAX_TMP_CHARS = 240
MAX_WARMUP_ATTEMPTS = 6
REQUIRED_CLEAN_WARMUPS = 2
ENGINE_DIRECTORY = "engine.portable-heart-runtime-v1"


def _not_ready(message, **details):
    error = VisualError("environment_not_ready", message)
    error.details = details
    if "receipt_path" in details:
        error.receipt_path = details["receipt_path"]
    return error


def _json_record(value):
    """Freeze a finite JSON value before it can enter a durable receipt."""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def check_path_budget(paths, *, maximum_chars=MAX_TMP_CHARS):
    """Check concrete, resolved Godot TMP paths without creating anything.

    Windows counts UTF-16 code units, including both halves of an emoji. The
    240-unit ceiling cannot be relaxed through this readiness API. Call again
    with actual operation paths if a new recipe or directory layout is added.
    """
    if type(maximum_chars) is not int or not 1 <= maximum_chars <= MAX_TMP_CHARS:
        raise ValueError("Godot TMP budget must be an integer from 1 to 240")
    if isinstance(paths, (str, bytes, Path)):
        raise ValueError("Provide a collection of concrete Godot TMP paths")
    rows = []
    for value in paths:
        resolved = Path(value).resolve()
        text = str(resolved)
        if "\x00" in text:
            raise _not_ready("Godot TMP path contains a null character")
        chars = len(text.encode("utf-16-le")) // 2
        rows.append({"path": text, "characters": chars, "within_budget": chars <= maximum_chars})
    if not rows:
        raise ValueError("At least one concrete Godot TMP path is required")
    receipt = {"status": "pass" if all(row["within_budget"] for row in rows) else "fail",
               "maximum_characters": maximum_chars, "counting": "resolved_utf16_code_units",
               "deepest_characters": max(row["characters"] for row in rows), "paths": rows}
    if receipt["status"] != "pass":
        raise _not_ready("A planned Godot TMP path exceeds the 240-character safety budget", path_budget=receipt)
    return receipt


def planned_tmp_paths(session_directory, *, job_id="0" * 32, scenario_name="reverse24-x24-r3"):
    """Plan current import/capture/preservation paths for a fresh game session.

    The placeholder job ID has the production UUID-hex length. Callers may
    substitute their actual job/scenario names before execution. No TMP shell
    override or changes to the qualified Godot transport are involved.
    """
    for value, label in ((job_id, "job_id"), (scenario_name, "scenario_name")):
        if not isinstance(value, str) or not value or value in {".", ".."} or any(c in value for c in "/\\\x00:"):
            raise ValueError(f"{label} must be one nonempty path component")
    base = Path(session_directory).resolve() / "measurements" / job_id
    outputs = []
    for role in ("before", "candidate", "jump/before", "jump/candidate", "boundary"):
        outputs.extend((base / role / "import", base / role / "import/clean-retry",
                        base / role / "raw" / scenario_name))
    for suite in ("preservation/baseline", "preservation/candidate",
                  "preservation/wall-coyote/baseline", "preservation/wall-coyote/candidate"):
        outputs.extend((base / suite / "import", base / suite / "import/clean-retry",
                        base / suite / "capture"))
    return [output / ENGINE_DIRECTORY / "environment/tmp" for output in outputs]


def _packaged_node(path=None):
    if path is None:
        spec = importlib.util.find_spec("playwright")
        if spec is None or not spec.origin:
            raise _not_ready("The installed Playwright package is unavailable")
        path = Path(spec.origin).resolve().parent / "driver/node.exe"
    path = Path(path).resolve()
    if not path.is_file():
        raise _not_ready("The installed Playwright driver node.exe is missing", driver_source=str(path))
    return path


def runtime_doctor(root=DEFAULT_RUNTIME_ROOT, *, packaged_node=None):
    """Read-only path/driver inventory; no timer request or native capture."""
    root = Path(root).resolve()
    budget = check_path_budget(planned_tmp_paths(root / "s/probe"))
    source = _packaged_node(packaged_node)
    source_sha = file_sha256(source)
    destination = root / "runtime/driver" / source_sha / "node.exe"
    copied = destination.is_file() and not destination.is_symlink()
    verified = copied and file_sha256(destination) == source_sha
    return {"kind": "LocalRuntimeReadiness", "schema_version": 1,
            "status": "ready" if verified else "not_prepared", "root": str(root),
            "session_root": str(root / "s"), "runtime_root": str(root / "runtime"),
            "capture_lock": str(COMMON_CAPTURE_LOCK.resolve()), "path_budget": budget,
            "driver": {"source": str(source), "path": str(destination), "sha256": source_sha,
                       "copied": copied, "verified": verified},
            "model_started": False, "cloud_used": False, "native_capture_started": False}


def prepare_runtime(root=DEFAULT_RUNTIME_ROOT, *, packaged_node=None, browser_path=None):
    """Provision a byte-identical local Node copy and return child-process env.

    Does not mutate os.environ or system configuration. Driver versions live
    in separate content-addressed directories; an existing mismatched copy is
    rejected, never overwritten. The caller owns any subsequent captures.
    """
    receipt = runtime_doctor(root, packaged_node=packaged_node)
    source = Path(receipt["driver"]["source"])
    destination = Path(receipt["driver"]["path"])
    source_sha = receipt["driver"]["sha256"]
    selected_browser = None
    if browser_path is not None:
        selected_browser = Path(browser_path).resolve()
        if not selected_browser.is_dir():
            raise _not_ready("Selected Playwright browser store does not exist", browser_path=str(selected_browser))
    root = Path(receipt["root"])
    # Resolve before writing so a pre-existing junction cannot route the copy
    # outside the explicitly selected runtime root.
    if not destination.resolve().is_relative_to(root):
        raise _not_ready("Playwright driver destination escapes the runtime root")
    if not (root / "s").resolve().is_relative_to(root):
        raise _not_ready("Session directory escapes the runtime root")
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or file_sha256(destination) != source_sha:
            raise _not_ready("Existing local Playwright driver differs from its installed source", driver=receipt["driver"])
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with source.open("rb") as original, destination.open("xb") as copied:
                shutil.copyfileobj(original, copied)
        except FileExistsError:
            # Another preparation may have created the same version. Verify it
            # below instead of replacing it or exposing unverified bytes.
            pass
    if (destination.is_symlink() or file_sha256(destination) != source_sha
            or file_sha256(source) != source_sha):
        raise _not_ready("Playwright driver bytes changed during local preparation", driver=receipt["driver"])
    (root / "s").mkdir(parents=True, exist_ok=True)
    driver_receipt = {"kind": "LocalPlaywrightDriver", "schema_version": 1,
                      "source": str(source), "path": str(destination), "sha256": source_sha,
                      "source_sha256": source_sha, "size_bytes": destination.stat().st_size,
                      "byte_identical": True, "model_started": False, "cloud_used": False}
    receipt_path = destination.parent / "receipt.json"
    if receipt_path.exists():
        try:
            previous = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise _not_ready("Existing driver provenance cannot be verified") from error
        if previous != driver_receipt:
            raise _not_ready("Existing driver provenance differs; choose a fresh runtime root")
    else:
        write_json(receipt_path, driver_receipt)
    environment = {"PLAYWRIGHT_NODEJS_PATH": str(destination)}
    if selected_browser is not None:
        environment["PLAYWRIGHT_BROWSERS_PATH"] = str(selected_browser)
    receipt.update(status="ready", environment=environment,
                   driver={**driver_receipt, "verified": True, "receipt_path": str(receipt_path)})
    return _json_record(receipt)


def qualify_warmups(capture, output, max_attempts=MAX_WARMUP_ATTEMPTS):
    """Require two consecutive clean timing results, preserving every attempt.

    ``capture(fresh_path)`` must return the real capture result with status
    ``ok``/``pass`` and ``timing.status == pass``. The callable owns creation of
    that path, which is initially absent. At most six unevaluated attempts run.
    This utility adds no threads, retries of active frames, or timing changes.
    """
    if not callable(capture):
        raise TypeError("capture must be callable")
    if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_WARMUP_ATTEMPTS:
        raise ValueError("Warm-up attempts must be an integer from 1 to 6")
    output = Path(output).resolve()
    if output.exists():
        raise VisualError("output_exists", "Warm-up evidence requires a fresh directory")
    output.mkdir(parents=True)
    receipt_path = output / "result.json"
    receipt = {"kind": "LocalWarmupReadiness", "schema_version": 1, "status": "running",
               "evaluated": False, "model_started": False, "cloud_used": False,
               "policy": {"maximum_attempts": max_attempts, "required_consecutive_clean": REQUIRED_CLEAN_WARMUPS,
                          "capture_timing_thresholds_changed": False},
               "attempts": [], "consecutive_clean": 0, "receipt_path": str(receipt_path)}
    write_json(receipt_path, receipt)
    for number in range(1, max_attempts + 1):
        directory = output / f"attempt-{number:02}"
        started = time.monotonic()
        attempt = {"attempt": number, "directory": str(directory), "status": "fail"}
        cancelled = None
        try:
            result = _json_record(capture(directory))
            if not isinstance(result, dict):
                raise ValueError("Capture result must be a finite JSON object")
            attempt["capture_result"] = result
            timing = result.get("timing")
            clean = (result.get("status") in {"ok", "pass"} and isinstance(timing, dict)
                     and timing.get("status") == "pass" and timing.get("errors", []) == [])
            attempt["status"] = "pass" if clean else "fail"
        except BaseException as error:
            attempt["error"] = {"code": getattr(error, "code", type(error).__name__), "message": str(error)}
            if not isinstance(error, Exception) or getattr(error, "code", None) == "cancelled":
                cancelled = error
        attempt["elapsed_seconds"] = time.monotonic() - started
        receipt["attempts"].append(attempt)
        receipt["consecutive_clean"] = receipt["consecutive_clean"] + 1 if attempt["status"] == "pass" else 0
        write_json(output / f"attempt-{number:02}-result.json", attempt)
        if cancelled is not None:
            receipt["status"] = "cancelled"
            write_json(receipt_path, receipt)
            raise cancelled
        if receipt["consecutive_clean"] >= REQUIRED_CLEAN_WARMUPS:
            receipt["status"] = "pass"
            write_json(receipt_path, receipt)
            return _json_record(receipt)
        write_json(receipt_path, receipt)
    receipt["status"] = "fail"
    write_json(receipt_path, receipt)
    raise _not_ready("Two consecutive clean warm-up captures were not obtained", receipt_path=str(receipt_path), receipt=receipt)
