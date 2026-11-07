# Quy trình sự cố bảo mật

## Phân loại
- P1: rò rỉ dữ liệu khách hoặc downtime production trên 15 phút.
- P2: dịch vụ nội bộ gián đoạn, chưa lộ dữ liệu.
- P3: cảnh báo bất thường chưa ảnh hưởng người dùng.

## Ứng cứu
P1 phải gọi on-call trong 10 phút và mở war room. Không xóa log gốc. Legal được báo nếu có dữ liệu cá nhân.

## Báo cáo
Sau P1 phải có postmortem trong 5 ngày làm việc, nêu root cause và action item chủ sở hữu.
