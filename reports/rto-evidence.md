# RTO/RPO Evidence - Lab 23

Moi so lieu duoi day duoc tinh tu timestamp trong log cua hai drill ngay 2026-10-09.

## 1. Drill 1 - khong co DR (baseline)

| Chi so | Gia tri | Cach do | Evidence |
|---|---:|---|---|
| t_outage | `2026-10-09T06:38:29Z` | Chaos kill Region A | `chaos/chaos-events.jsonl:5` |
| Request fail dau tien sau t_outage | `+2.0s` | Dong dau tien co `ts >= t_outage` va `ok:false` | `reports/drill-1-nodr.jsonl:15` |
| Request thanh cong sau do | Khong co | Tu lan fail dau tien den request cuoi deu `ok:false` | `reports/drill-1-nodr.jsonl:15`, `reports/drill-1-nodr.jsonl:29` |
| RTO | `NO_RECOVERY` | Khong co request phuc hoi trong cua so loadgen | `chaos/chaos-events.jsonl:5`, `reports/drill-1-nodr.jsonl:29` |

Ket luan: khong co health checker va failover nen Region A khong tu phuc hoi trong Drill 1.

## 2. Drill 2 - co DR

| Moc | +giay tu t_outage | Cach do | Evidence |
|---|---:|---|---|
| t_outage | `0.0s` | `action:kill`, Region A | `chaos/chaos-events.jsonl:7` |
| User thay loi dau tien | `+2.0s` | Request dau tien sau t_outage co `ok:false` | `reports/drill-2-withdr.jsonl:11` |
| Runbook xac nhan outage | `+19.6s` | Ba probe Region A fail lien tiep | `reports/runbook-run.jsonl:1`, `reports/runbook-run.jsonl:2` |
| Snapshot restore xong | `+19.9s` | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Health checker phat hien | `+20.1s` | `region:a`, `to:UNHEALTHY`, ba fail lien tiep | `reports/health-events.jsonl:2` |
| Region B ready | `+26.2s` | `step:4_wait_ready`, `waited_s:6.251` | `reports/failover-events.jsonl:4` |
| DNS cutover | `+26.2s` | `step:5_dns_cutover`, active region `b` | `reports/failover-events.jsonl:5` |
| **RTO do duoc** | **`+27.1s`** | Request thanh cong dau tien sau loi, served by `b` | `reports/drill-2-withdr.jsonl:23` |

| Chi so | Do duoc | Muc tieu | Verdict | Evidence |
|---|---:|---:|---|---|
| RTO - Inference API | `27.1s` | `300s` | **PASS** | `chaos/chaos-events.jsonl:7`, `reports/drill-2-withdr.jsonl:23` |
| RPO - Vector DB | `0.0s` / `0 docs` | `300s` | **PASS** | `reports/failover-events.jsonl:2`, `reports/replication.jsonl:2` |

RPO bang 0 trong lan chay nay vi replication cycle thu hai hoan tat ngay truoc restore. Day la so do cua lan drill, khong phai cam ket rang moi lan chay deu co RPO bang 0.

## 3. RTO gom nhung gi

Phan ra duoi day dung cac khoang thoi gian khong chong lan de tong bang RTO do duoc.

| Thanh phan | Giay | Nguon do | Cach giam |
|---|---:|---|---|
| Phat hien va xac nhan ban tu dong | `19.7s` | t_outage -> `1_verify_target`; detect floor cau hinh rieng la `5.0s x 3 = 15.0s` | Dung health event lam trigger duy nhat; can nhac interval nho hon cung circuit breaker |
| Verify target + snapshot restore + scale | `0.2s` | `1_verify_target` -> `3_scale_pool` | Snapshot incremental va giu manifest/model version dong bo |
| GPU pool warm-up | `6.3s` | `3_scale_pool` -> `4_wait_ready`; log ghi `waited_s:6.251` | Giu warm capacity hoac pre-warm pool phu |
| DNS/LB propagation | `0.9s` | t_recovered `27.1s` - t_cutover `26.2s` | Giam TTL co kiem soat hoac dung health-aware global LB |
| **Tong** | **`27.1s`** | Request dau tien phuc hoi o Region B | - |

Health checker doc lap phat hien o `+20.1s` (`reports/health-events.jsonl:2`), cham hon restore khoang 0.2 giay vi runbook cung thuc hien ba probe xac nhan song song. Cutover van dien ra sau health detection nen ket qua khong co warning.
