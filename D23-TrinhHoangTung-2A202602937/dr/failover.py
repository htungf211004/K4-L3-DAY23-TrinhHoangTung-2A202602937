"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped failover event to the evidence log."""
    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(record, ensure_ascii=False))
    return record


def state_of(region: str) -> dict:
    """Return the serving state of a region or raise on an unusable response."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Execute the five failover steps in order, cutting over only when ready."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if wait < 0:
        raise ValueError("wait must not be negative")

    primary = "b" if target == "a" else "a"
    steps = []
    result = {
        "ok": False,
        "target": target,
        "steps": steps,
        "rpo_seconds": None,
        "docs_lost": None,
        "embed_model_version": None,
    }

    # 1. Verify that the target serving process exists and expose its current state.
    try:
        target_state = state_of(target)
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        emit(step="1_verify_target", target=target, ok=False,
             error=type(exc).__name__)
        return result
    emit(step="1_verify_target", target=target, ok=True, state=target_state)
    steps.append("1_verify_target")

    # 2. Restore state before asking the target to serve traffic, then measure RPO.
    restored = snapshot.get(target, backend)
    rpo = snapshot.rpo(
        pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
        pathlib.Path(f"state/region-{target}/vectors.sqlite"),
    )
    result.update(
        rpo_seconds=rpo["rpo_seconds"],
        docs_lost=rpo["docs_lost"],
        embed_model_version=restored.get("embed_model_version"),
    )
    emit(
        step="2_restore_snapshot",
        target=target,
        backend=backend,
        ok=True,
        rpo_seconds=result["rpo_seconds"],
        docs_lost=result["docs_lost"],
        embed_model_version=result["embed_model_version"],
    )
    steps.append("2_restore_snapshot")

    # 3. Scale the target pool. The serving process observes this transition and
    # starts its configured warm-up timer on the next readiness probe.
    pool_state = pathlib.Path(f"state/region-{target}/pool_state")
    pool_state.parent.mkdir(parents=True, exist_ok=True)
    pool_state.write_text("full", encoding="utf-8")
    emit(step="3_scale_pool", target=target, ok=True, pool_state="full")
    steps.append("3_scale_pool")

    # 4. Wait until the complete serving readiness contract passes. A timeout is
    # an explicit abort: edge/active_region must remain untouched.
    started = time.monotonic()
    deadline = started + wait
    ready = False
    reason = "readiness_timeout"
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            response = httpx.get(
                f"{URL[target]}/readyz",
                timeout=max(0.01, min(2.0, remaining)),
            )
            if response.status_code == 200:
                ready = True
                reason = "ready"
                break
            try:
                body = response.json()
                reasons = body.get("reasons")
                reason = "; ".join(str(item) for item in reasons) if reasons else (
                    body.get("error") or f"http_{response.status_code}"
                )
            except (ValueError, AttributeError):
                reason = f"http_{response.status_code}"
        except httpx.RequestError as exc:
            reason = type(exc).__name__

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))

    waited_s = round(time.monotonic() - started, 3)
    emit(step="4_wait_ready", target=target, ok=ready,
         waited_s=waited_s, reason=reason)
    steps.append("4_wait_ready")
    if not ready:
        result["error"] = reason
        return result

    # 5. Cut over only after readiness succeeds.
    active_region = pathlib.Path("edge/active_region")
    active_region.parent.mkdir(parents=True, exist_ok=True)
    active_region.write_text(target, encoding="utf-8")
    emit(step="5_dns_cutover", target=target, ok=True,
         active_region=target)
    steps.append("5_dns_cutover")
    result["ok"] = True
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
