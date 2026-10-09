# Runbook 1 trang — Region chính down

Phạm vi: bare-mode local, Region A là primary và Region B là target. Từ PowerShell, vào đúng môi trường WSL trước khi chạy để `SIGSTOP/SIGCONT` hoạt động:

```powershell
wsl
cd /mnt/c/VinAI/K4-L3-DAY23-TrinhHoangTung-2A202602937
source .venv-wsl/bin/activate
```

Lệnh failover chính, mặc định có bước xác nhận `y/N`:

```bash
python dr/runbook.py --primary a --target b --backend fs
```

Không dùng `--auto` khi xử lý incident thật. Không chạy riêng lại `failover.py` trong khi runbook đang chạy.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | Region A `alive:false` qua 3 probe liên tiếp; Region B còn `alive:true` | On-call SRE |
| 2 | Mở incident và xác nhận failover | `python dr/runbook.py --primary a --target b --backend fs` | Có dòng `thong_bao_incident` trong `reports/runbook-run.jsonl` và operator nhập `y` | Incident commander |
| 3 | Restore state ở Region B | `tail -n 5 reports/failover-events.jsonl` | Có `2_restore_snapshot`, `ok:true`, cùng `rpo_seconds`, `docs_lost`, `embed_model_version` | Recovery operator |
| 4 | Scale và chờ Region B ready | `curl -fsS http://127.0.0.1:8002/readyz` | HTTP 200, `ready:true`, pool `full`, weights và vector DB hợp lệ | Serving on-call |
| 5 | Xác minh DNS/LB cutover | `curl -fsS http://127.0.0.1:8080/edge/state` | `active_region` là `b` và failover log có `5_dns_cutover` | Incident commander |
| 6 | Verify golden signals | `for i in $(seq 1 10); do curl -fsS 'http://127.0.0.1:8002/v1/infer?q=golden'; done` | 10/10 HTTP 200; error rate `<1%`; p95 `<500ms` | On-call SRE |
| 7 | Đo RTO/RPO và mở postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `warnings:[]`, `rto_verdict:PASS`; số liệu được chép vào reports | Incident lead |

## Abort và rollback

- **Abort trước cutover:** nếu Region B không trả `/readyz` HTTP 200 trong 60 giây, không sửa `edge/active_region`; tiếp tục phục vụ incident từ Region A nếu có thể và escalate tới Serving on-call.
- **Điều kiện rollback về A:** Region A phải `/readyz` HTTP 200 qua 3 lần kiểm tra cách nhau 5 giây, state đã đồng bộ, không còn ingest lag, và golden signals đạt error rate `<1%`, p95 `<500ms`.
- **Quyền quyết định:** chỉ Incident commander được phê duyệt rollback. On-call không tự động đổi hai chiều để tránh flapping.
- **Lệnh rollback sau phê duyệt:** `python -c "from pathlib import Path; Path('edge/active_region').write_text('a')"`.
- Sau rollback, chạy 10 golden requests qua Edge và theo dõi tối thiểu 10 phút. Nếu lỗi vượt 1% hoặc A mất readiness, lập tức trả pointer về `b` và giữ incident mở.
