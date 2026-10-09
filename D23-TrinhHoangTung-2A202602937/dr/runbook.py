"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append one timestamped runbook step to the incident timeline."""
    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(record, ensure_ascii=False))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Return immediately in auto mode; otherwise require an explicit yes."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}


def _latest_outage(region: str) -> float | None:
    """Read the latest real kill timestamp for a region from the chaos log."""
    events = pathlib.Path("chaos/chaos-events.jsonl")
    if not events.exists():
        return None

    latest = None
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("action") == "kill" and event.get("region") == region:
            latest = event.get("ts")
    return latest


def _target_state(target: str, failover_result: dict) -> dict:
    """Use state returned by failover when available, otherwise read it once."""
    state = failover_result.get("target_state") or failover_result.get("state")
    if isinstance(state, dict):
        return state
    try:
        return fo.state_of(target)
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError):
        return {}


def _golden_signals(target: str, requests: int = 10) -> dict:
    """Send real inference requests directly to the target and summarize them."""
    latencies = []
    failures = 0
    for i in range(requests):
        started = time.perf_counter()
        try:
            response = httpx.get(
                f"{URL[target]}/v1/infer",
                params={"q": f"runbook golden signal {i}"},
                timeout=3.0,
            )
            if response.status_code != 200:
                failures += 1
        except httpx.RequestError:
            failures += 1
        latencies.append(round((time.perf_counter() - started) * 1000, 1))

    ordered = sorted(latencies)
    rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "requests": requests,
        "failures": failures,
        "error_rate": round(failures / requests, 3),
        "p95_latency_ms": ordered[rank],
    }


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the seven incident-response steps in order."""
    if primary not in URL or target not in URL:
        raise ValueError("primary and target must be known regions")
    if primary == target:
        raise ValueError("primary and target must be different regions")

    run_started = time.monotonic()

    # 1. Require three consecutive primary failures. Probe the target alongside
    # every primary probe so the operator sees both sides of the incident.
    primary_failures = 0
    probes = []
    for attempt in range(1, 4):
        primary_ready, primary_reason = hc.probe(primary, timeout=2.0)
        target_ready, target_reason = hc.probe(target, timeout=2.0)
        primary_failures = 0 if primary_ready else primary_failures + 1
        probes.append({
            "attempt": attempt,
            "primary_ready": primary_ready,
            "primary_reason": primary_reason,
            "target_ready": target_ready,
            "target_reason": target_reason,
        })
        if attempt < 3:
            time.sleep(5.0)

    outage_confirmed = primary_failures >= 3
    step(1, "xac_nhan_outage", primary=primary, target=target,
         ok=outage_confirmed, consecutive_fails=primary_failures, probes=probes)
    if not outage_confirmed:
        elapsed = round(time.monotonic() - run_started, 3)
        step(7, "post_incident", ok=False, reason="outage_not_confirmed",
             elapsed_s=elapsed,
             measure_command=("python tools/measure_rto.py --loadgen "
                              "reports/drill-2-withdr.jsonl --target-rto 300"))
        return {"ok": False, "reason": "outage_not_confirmed", "elapsed_s": elapsed}

    # 2. Record when the operator learned about the incident, separately from
    # the actual outage timestamp written by the chaos tool.
    outage_ts = _latest_outage(primary)
    notified_ts = time.time()
    notification_delay = None if outage_ts is None else round(notified_ts - outage_ts, 3)
    step(2, "thong_bao_incident", ok=True, primary=primary, target=target,
         t_outage=outage_ts, t_notified=notified_ts,
         notification_delay_s=notification_delay)

    if not confirm(auto, f"Fail over region-{primary} to region-{target}?"):
        elapsed = round(time.monotonic() - run_started, 3)
        step(7, "post_incident", ok=False, reason="operator_cancelled",
             elapsed_s=elapsed,
             measure_command=("python tools/measure_rto.py --loadgen "
                              "reports/drill-2-withdr.jsonl --target-rto 300"))
        return {"ok": False, "reason": "operator_cancelled", "elapsed_s": elapsed}

    # 3. This is the only invocation of failover(). It performs restore, scale,
    # readiness wait, and cutover as one ordered operation.
    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=bool(failover_result.get("ok")),
         target=target, failover_steps=failover_result.get("steps", []),
         rpo_seconds=failover_result.get("rpo_seconds"),
         docs_lost=failover_result.get("docs_lost"))

    # 4. Read and record the result; never invoke failover a second time.
    target_state = _target_state(target, failover_result)
    replica_ok = bool(
        failover_result.get("ok")
        and target_state.get("weights")
        and target_state.get("count", 0) > 0
    )
    step(4, "verify_state_replica", ok=replica_ok, target=target,
         vector_count=target_state.get("count"),
         weights=target_state.get("weights"),
         embed_model_version=failover_result.get("embed_model_version"),
         rpo_seconds=failover_result.get("rpo_seconds"),
         docs_lost=failover_result.get("docs_lost"))

    # 5. Verify the cutover outcome from the single failover result and pointer.
    active_file = pathlib.Path("edge/active_region")
    active_region = active_file.read_text(encoding="utf-8").strip() \
        if active_file.exists() else None
    cutover_ok = bool(
        failover_result.get("ok")
        and "5_dns_cutover" in failover_result.get("steps", [])
        and active_region == target
    )
    step(5, "dns_cutover", ok=cutover_ok, target=target,
         active_region=active_region)

    # 6. Golden signals are meaningful only after a successful cutover.
    if cutover_ok:
        golden = _golden_signals(target, requests=10)
        golden_ok = golden["failures"] == 0
        step(6, "verify_golden_signals", ok=golden_ok, target=target, **golden)
    else:
        golden = {
            "requests": 0,
            "failures": 0,
            "error_rate": None,
            "p95_latency_ms": None,
        }
        golden_ok = False
        step(6, "verify_golden_signals", ok=False, target=target,
             skipped=True, reason="cutover_not_completed", **golden)

    # 7. Finish the timeline with an exact command that derives RTO from logs.
    elapsed = round(time.monotonic() - run_started, 3)
    ok = bool(failover_result.get("ok") and replica_ok and cutover_ok and golden_ok)
    measure_command = (
        "python tools/measure_rto.py --loadgen "
        "reports/drill-2-withdr.jsonl --target-rto 300"
    )
    step(7, "post_incident", ok=ok, primary=primary, target=target,
         elapsed_s=elapsed, measure_command=measure_command)

    return {
        "ok": ok,
        "primary": primary,
        "target": target,
        "elapsed_s": elapsed,
        "failover": failover_result,
        "target_state": target_state,
        "golden_signals": golden,
        "measure_command": measure_command,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
