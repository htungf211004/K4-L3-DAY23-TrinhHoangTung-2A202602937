# Blameless Postmortem - DR Drill Lab 23

**Ngay drill:** 2026-10-09

**Pham vi:** Region A bi network partition gia lap; Region B duoc restore, warm-up va nhan traffic.

**Ket qua:** DR hoat dong, Region B phuc hoi traffic voi RTO `27.1s`; RPO `0.0s / 0 docs`.

## 1. Impact

- Co 12 request loi trong cua so sau outage, tu request dau tien o `reports/drill-2-withdr.jsonl:11` den request loi cuoi o `reports/drill-2-withdr.jsonl:22`.
- Request dau tien phuc hoi duoc serve boi Region B tai `reports/drill-2-withdr.jsonl:23`.
- Khong mat document trong lan drill nay; restore log ghi `docs_lost:0` tai `reports/failover-events.jsonl:2`.
- Golden signals sau cutover: 10 request, 0 loi, p95 `156.6ms` tai `reports/runbook-run.jsonl:6`.

## 2. Timeline UTC

| ISO time | Su kien | Evidence |
|---|---|---|
| 2026-10-09T06:55:50.960Z | Outage bat dau; Region A bi netblock | `chaos/chaos-events.jsonl:7` |
| 2026-10-09T06:55:52.986Z | User thay loi dau tien sau outage | `reports/drill-2-withdr.jsonl:11` |
| 2026-10-09T06:56:10.588Z | Runbook xac nhan 3 probe Region A fail lien tiep | `reports/runbook-run.jsonl:1` |
| 2026-10-09T06:56:10.602Z | Incident duoc ghi nhan, do tre thong bao `19.642s` | `reports/runbook-run.jsonl:2` |
| 2026-10-09T06:56:10.862Z | Snapshot restore hoan tat; RPO `0.0s / 0 docs` | `reports/failover-events.jsonl:2` |
| 2026-10-09T06:56:11.017Z | Health checker doc lap chuyen Region A sang `UNHEALTHY` | `reports/health-events.jsonl:2` |
| 2026-10-09T06:56:17.122Z | Region B hoan tat warm-up va ready | `reports/failover-events.jsonl:4` |
| 2026-10-09T06:56:17.137Z | DNS/LB pointer chuyen sang Region B | `reports/failover-events.jsonl:5` |
| 2026-10-09T06:56:18.089Z | Request dau tien thanh cong tu Region B; incident recovered | `reports/drill-2-withdr.jsonl:23` |
| 2026-10-09T06:56:18.644Z | Golden signals dat 10/10 request, 0 loi | `reports/runbook-run.jsonl:6` |

Runbook va health checker la hai detector doc lap. Vi vay restore bat dau khoang 0.2 giay truoc event `UNHEALTHY` cua health checker; DNS cutover van dien ra sau event nay va ket qua do khong co warning.

## 3. RTO/RPO va gap analysis

| Chi so | Muc tieu | Do duoc | Gap/headroom | Verdict | Evidence |
|---|---:|---:|---:|---|---|
| RTO | `300s` | `27.1s` | `272.9s` duoi muc tieu | PASS | `chaos/chaos-events.jsonl:7`, `reports/drill-2-withdr.jsonl:23` |
| RPO | `300s` | `0.0s / 0 docs` | `300.0s` duoi muc tieu | PASS | `reports/failover-events.jsonl:2`, `reports/replication.jsonl:2` |

Buoc ton nhieu thoi gian nhat la phat hien va xac nhan outage: khoang `19.7s`, tuong duong 72.7% RTO. Trong do detect floor cau hinh la `5.0s x 3 = 15.0s`; thoi gian con lai den tu timeout probe, scheduling va xac nhan ban tu dong. GPU warm-up ton them `6.3s`; edge propagation mat khoang `0.9s`.

RPO bang 0 la ket qua thuan loi cua timing: snapshot tai `reports/replication.jsonl:2` hoan tat ngay truoc restore. Voi lich replication 30 giay, mot lan drill khac co the co RPO lon hon 0.

## 4. Root cause - 5 whys

1. **Tai sao user nhan loi?** Edge van tro toan bo traffic vao Region A trong khi upstream khong phan hoi.
2. **Tai sao Region A khong phan hoi?** Serving process bi mat kha nang xu ly ket noi, nen request cho den network timeout thay vi fail-fast.
3. **Tai sao traffic khong chuyen ngay sang Region B?** Kien truc active-passive yeu cau xac nhan nhieu lan va phe duyet truoc cutover de chong transient failure va flapping.
4. **Tai sao Region B chua the nhan traffic ngay?** Region B ban dau thieu vector state/model weights va GPU pool chi o trang thai `warm`; no phai restore snapshot roi warm-up du 6 giay.
5. **Tai sao recovery van mat 27.1 giay du automation hoat dong?** Detection/confirmation, restore, GPU warm-up va edge propagation nam noi tiep hoac chong lan trong critical path; chua co mot nguon health event duy nhat dieu phoi toan bo qua trinh.

Root cause he thong la thiet ke active-passive voi cold state/compute va hai co che xac nhan doc lap, khong phai ca nhan hay thao tac chay chaos.

## 5. Action items

| # | Action item | Owner | Deadline | Tac dong du kien |
|---|---|---|---|---|
| 1 | Dung event `UNHEALTHY` da dat threshold lam input cho runbook, bo vong xac nhan trung nhung van giu nut phe duyet cua Incident commander | SRE/DR owner | 2026-10-16 | Loai bo toi da khoang `4.7s` overhead ngoai detect floor va loai bo race giua hai detector |
| 2 | Duy tri Region B voi model/vector snapshot gan nhat va mot GPU worker pre-warmed | Serving platform | 2026-10-23 | Giam warm-up tu `6.3s` xuong muc tieu `<=1.0s`, tiet kiem khoang `5.3s` |
| 3 | Danh gia giam Edge TTL tu 5 giay xuong 1 giay, kem circuit breaker va rollback monitor | Edge owner | 2026-10-23 | Giam toi da `0.9s` propagation da quan sat trong drill nay |
| 4 | Canh bao khi replication lag vuot 30 giay hoac manifest/model version khong khop | Data platform | 2026-10-16 | Khong giam RPO cua lan nay (`0.0s`), nhung giu RPO van hanh trong gioi han lich snapshot 30 giay |

## 6. Ba cau hoi bat buoc

1. **Detect floor la bao nhieu va chiem bao nhieu RTO?** `5.0s x 3 = 15.0s`, chiem khoang **55.4%** cua RTO `27.1s`. Evidence cau hinh: `reports/health-events.jsonl:2`.
2. **Neu interval giam xuong 1 giay?** Floor ly thuyet giam tu 15 giay xuong 3 giay, tuc toi da giam 12 giay. Doi lai he thong probe nhieu gap 5 lan, nhay hon voi network jitter va co nguy co false positive/flapping; threshold va circuit breaker van phai duoc giu.
3. **Neu outage keo dai 6 gio va primary mat du lieu vinh vien?** `docs_lost:0` chi chung minh snapshot lan drill chua moi document co tai thoi diem restore. Trong incident 6 gio, moi write sau snapshot cuoi ma chua replicate co the mat hoac phai replay; tac dong khach hang la ticket/query moi khong xuat hien o Region B. Phai dung replication lag va so document thuc te, khong duoc mac dinh RPO luon bang 0.
