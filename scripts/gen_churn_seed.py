"""Generate datasets/knowledge/churn/seed.jsonl — many doc families for version churn."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "datasets" / "knowledge" / "churn" / "seed.jsonl"

# (prefix, acl, topic_vi) — expanded to many numbered families.
_FAMILIES: list[tuple[str, str, str]] = [
    ("fin-approve", "finance", "phê duyệt ngân sách thiết bị"),
    ("fin-travel", "finance", "công tác phí và khách sạn"),
    ("fin-vendor", "finance", "onboarding nhà cung cấp"),
    ("fin-invoice", "finance", "chu kỳ thanh toán hóa đơn"),
    ("fin-budget", "finance", "trần ngân sách quý"),
    ("fin-card", "finance", "hạn mức thẻ công ty"),
    ("fin-audit", "finance", "lưu trữ chứng từ kiểm toán"),
    ("fin-fx", "finance", "tỷ giá thanh toán ngoại tệ"),
    ("hr-leave", "hr", "nghỉ phép năm"),
    ("hr-sick", "hr", "nghỉ bệnh và giấy bác sĩ"),
    ("hr-pay", "hr", "ngày trả lương"),
    ("hr-bonus", "hr", "thưởng hiệu suất quý"),
    ("hr-onboard", "hr", "onboarding nhân viên mới"),
    ("hr-offboard", "hr", "quy trình nghỉ việc"),
    ("hr-ot", "hr", "làm thêm giờ"),
    ("hr-remote", "hr", "làm việc từ xa"),
    ("it-access", "it", "cấp quyền production"),
    ("it-mfa", "it", "bắt buộc MFA"),
    ("it-vpn", "it", "truy cập VPN"),
    ("it-laptop", "it", "cấp phát laptop"),
    ("it-incident", "it", "phân loại sự cố P1 P2 P3"),
    ("it-backup", "it", "sao lưu dữ liệu"),
    ("it-secret", "it", "xoay vòng secret"),
    ("it-patch", "it", "vá bảo mật máy chủ"),
    ("ops-oncall", "ops", "lịch on-call"),
    ("ops-sla", "ops", "SLA phản hồi Pager"),
    ("ops-change", "ops", "cửa sổ change production"),
    ("ops-runbook", "ops", "runbook khôi phục dịch vụ"),
    ("ops-capacity", "ops", "ngưỡng capacity alert"),
    ("ops-deploy", "ops", "tần suất deploy"),
    ("legal-nda", "legal", "thời hạn NDA"),
    ("legal-contract", "legal", "duyệt hợp đồng khung"),
    ("legal-privacy", "legal", "lưu trữ dữ liệu cá nhân"),
    ("legal-retain", "legal", "thời gian giữ hồ sơ"),
    ("sec-phish", "security", "báo cáo phishing"),
    ("sec-device", "security", "thiết bị mang về nhà"),
    ("sec-usb", "security", "cấm USB không mã hóa"),
    ("sec-class", "security", "phân loại tài liệu mật"),
    ("shared-glossary", "*", "thuật ngữ nội bộ ACL"),
    ("shared-desk", "*", "desk ticket approval"),
]


def main() -> None:
    versions = 5
    # 40 topic families × 5 copies = 200 docs × 5 versions = 1000 steps (~2800 cases).
    copies = 5
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for copy in range(1, copies + 1):
        for prefix, acl, topic in _FAMILIES:
            doc_id = f"{prefix}-{copy:02d}"
            rows.append(
                {
                    "document_id": doc_id,
                    "acl": [acl],
                    "topic": f"{topic} nhóm {copy}",
                    "versions": versions,
                    "unit": "triệu VND" if acl == "finance" else "ngày",
                    "base": 5 + copy,
                }
            )
    with OUT.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} families -> {OUT}")
    print(f"expanded steps ~= {sum(int(r['versions']) for r in rows)}")


if __name__ == "__main__":
    main()
