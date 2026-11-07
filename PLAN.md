# Plan — GenAI Platform

Repo: `/Users/tam/go/src/github.com/nguyenbatam/genai-lead`. Không đặt tên Nexus. `nexus-platform` cạnh đây là hệ nhắn tin E2EE, không liên quan.

Thanh đo vai trò: `JD.md` (playSTUDIOS GenAI Engineering Lead/Manager). Repo này chứng minh phần hands-on GenAI + kiến trúc nền tảng nội bộ; phần quản lý người và stakeholder nằm ở `INTERVIEW.md`, không nhét vào code.

Sản phẩm là một nền tảng nhiều team dùng chung, không phải một app hỏi đáp. Control plane quản tài nguyên. Data plane chạy request. Async plane nuốt việc dài. Hai app mẫu ngồi trên cùng API để chứng minh đó là nền tảng.

Modular monolith cộng worker. Tách service khi một số đo buộc phải tách, không tách trước.

## Vì sao không làm y bản 16 tuần

Bản đó đúng về hướng và sai về sức chứa của một người.

| Việc trong bản kia | Quyết định |
|---|---|
| Org, project, API key, quota, model registry, agent config | Làm. Đây là phần Lead |
| Gateway tự sở hữu routing, fallback, sổ giá | Làm. LiteLLM chỉ được phép là adapter gọi provider, không phải nơi giữ quota và chính sách |
| Agent runtime: workflow, single agent, supervisor | Làm đủ ba mode. Không compiler sinh graph tùy ý từ JSON |
| RAG là service dùng chung, có version và ACL | Làm. App hỏi đáp trở thành tenant đầu tiên, không bị xóa |
| Eval so sánh hai experiment | Làm. Đây là chỗ khác một demo chat |
| Trace, latency, token, giá, redact | Làm bằng OpenTelemetry và Prometheus |
| Bốn app: support, research, sales, eval UI | Không. Support agent đã có trong `ai_deployment_platform` (Go, approval, RAG, Keycloak). Không viết lại |
| Keycloak, Vault, Kubernetes, Terraform, admin web, billing, Grafana thành sản phẩm | Không trong repo này. JWT ký local. Secret từ env. Compose là bản chạy |
| MCP cho mọi server | Một MCP client nằm sau tool registry, allow-list. MCP không cấp quyền |
| Bảy agent Research hardcode | Không. Supervisor điều phối agent đã đăng ký. Số agent của một team là config, trần bước vẫn là code |
| Rust cho runtime | Không, cho đến khi một benchmark chỉ ra Python là nút thắt |
| 10.000 PDF làm cửa ải | Cùng failure mode trên tập nhỏ hơn: job trùng, crash giữa chừng, xóa version cũ |

`studio-kit` trả lời "khi nào không gọi model" cho game studio. `ai_deployment_platform` trả lời "một app support đã có approval và IdP". Repo này trả lời "nhiều project dùng chung gateway, runtime, RAG và eval".

## Bốn mặt phẳng

Chi tiết và sơ đồ: `ARCHITECTURE.md`. ADR tool plane: `docs/adr/005-tool-plane.md`.

```
Developer / app
      |
      v
Control plane (đồng bộ, Postgres)
  org, project, api key, quota, model, prompt, agent, knowledge base, audit
      |
      v
Data plane (API stateless)
  kiểm key -> quota -> concurrency theo project -> nạp config -> chạy mode đã chọn -> gateway -> validate
  ToolRuntime: allow-list, approval, idempotency (policy)
  state của một run nằm ở checkpoint, không nằm trong process
      |
      +---> Tool plane (apps/tools)
      |       execute create_record / note; không cấp quyền
      |
      v
Async plane (hàng đợi)
  ingest, embed, reindex, eval batch, gộp usage, sweep run
  claim fair theo project (không FIFO thuần)
```

API không embed trong request. Upload trả `job_id`.

## Ba mode chạy

| Mode | Khi nào | Ai quyết định bước tiếp |
|---|---|---|
| `workflow` | Các bước đã biết. Hỏi tài liệu là mode này | Code |
| `agent` | Cần chọn tool theo kết quả, có thể dừng | Một model, trong allow-list của agent đó |
| `supervisor` | Việc tách được thành vai đã đăng ký, và một agent không đủ | Supervisor chọn agent đã đăng ký, không chọn tên lạ |

Cùng một task được đo ở `agent` và `supervisor`: hoàn thành, latency, token, giá, tool đúng, lần gọi thừa. Không mặc định supervisor thắng. Số đo ghi trong eval, điền khi chạy, không điền trước.

Handoff là supervisor chuyển sang một agent khác rồi không quay lại vai cũ trong cùng bước. Parallel là nhiều tool hoặc nhiều agent không phụ thuộc nhau, fan-in bằng reducer. Trần bước, timeout, và ngân sách USD nằm trên run. Vòng supervisor không có đường thoát là test phải fail.

## Hai app mẫu

Cùng gateway, cùng runtime, cùng trace, khác project và khác key.

1. Knowledge. Mode `workflow`. Knowledge base riêng. Citation. Không tool ghi.
2. Desk. Mode `agent`. Một tool ghi bản ghi cục bộ, `requires_approval: true`, idempotency key. Retry không tạo hai bản ghi. Không có Gmail, không có CRM.

App thứ hai đủ để demo side effect. Support agent đầy đủ mở ở repo Go.

## API

Key gửi ở header. Key lưu dạng hash. Project lấy từ key, không lấy từ body.

| Method | Path | Mặt |
|---|---|---|
| `POST` | `/v1/orgs` `/v1/projects` | Control |
| `POST` | `/v1/api-keys` | Control. Bản rõ chỉ trả một lần |
| `POST` | `/v1/models` | Control. Giá theo version |
| `POST` | `/v1/prompts` | Control. Version bất biến |
| `POST` | `/v1/agents` | Control. Mode, model, prompt version, tool, trần |
| `POST` | `/v1/knowledge-bases` | Control |
| `POST` | `/v1/chat/completions` | Data. Gateway |
| `POST` | `/v1/agents/{id}/runs` | Data |
| `POST` | `/v1/runs/{id}/approvals` | Data. Resume sau interrupt |
| `POST` | `/v1/knowledge-bases/{id}/documents` | Async. `202` |
| `POST` | `/v1/evals` | Async. So sánh experiment |
| `GET` | `/v1/usage` | Control. Theo project |
| `GET` | `/v1/usage/summary` | Control. Gom theo model × tier; hint `tier_order` để tối ưu cost |
| `GET` | `/health` `/ready` `/metrics` | Vận hành. Attempts/cost gắn `model` + `tier` |

Lỗi RFC 9457. `/ready` chỉ xanh khi Postgres và Redis sống. Qdrant và hàng đợi vào điều kiện ready từ phase có chúng.

## Gateway

Mọi completion đi một hàm. Không module nào gọi provider trực tiếp.

- Explicit: request chỉ định model đã được project cho phép.
- Policy: task `cheap`, `reason`, hoặc `embed` map tới model trong registry.
- Fallback chỉ khi policy của task ghi chuỗi model thay thế cùng capability (`fallback_model_ids`, tối đa hai). Model chính chết không được tự rơi xuống model rẻ hơn nếu policy không cho. Credential pick: `tier_order` `free_paid_local` (mặc định) hoặc `free_local_paid`; route có thể override.
- Một dòng usage: `org_id`, `project_id`, provider, model, pricing version, token vào, token ra, latency, cost, status. Kể cả từng bước fallback trong chuỗi.
- Rate limit và ngân sách theo project, trên Redis, đối soát ở Postgres.
- Concurrency theo project (chat/run/provider in-flight) trước semaphore toàn process. Trần open run và hàng đợi ingest theo project. Worker claim fair theo `project_id` và tôn trọng run limiter. Create project nhận các trần này. Không lập lịch theo estimated done time.
- Provider port: model kịch bản cho CI, adapter OpenAI-compatible khi có `LLM_BASE_URL`.

Công thức giá: token vào nhân đơn giá vào, cộng token ra nhân đơn giá ra, chia một triệu. Đơn giá lấy từ pricing version đã gắn với request, không lấy giá "hiện tại" lúc đối soát.

## Runtime

Agent là hàng trong Postgres, không phải class Python cho từng usecase.

Trạng thái run: `created`, `queued`, `running`, `waiting_approval`, `completed`, `failed`, `cancelled`.

Checkpoint trên Postgres. Process chết giữa `running` thì run được nhận lại, không chạy lại tool đã commit. Tool side effect nhận idempotency key của run cộng tên tool cộng hash input. Tool không có trong allow-list của agent version đó là `denied` và dừng. Agent không nhận connection string, không nhận secret.

LangGraph biểu diễn ba mode. Graph không quản key, quota, hay giá. Những thứ đó xong trước khi vào graph.

## RAG

Knowledge base thuộc project. Upload vào object storage (MinIO). Worker parse, chunk parent/child, embed bằng `fastembed`, upsert Qdrant. Parent text và ACL ở Postgres của chính knowledge base này, không thêm Mongo trừ khi một đo đạc cho thấy Postgres không đủ.

Sửa tài liệu tạo version mới và xóa point cũ trước khi version mới được phục vụ. Xóa là tombstone cả child lẫn parent. Filter ACL tại search và lần nữa lúc lấy parent. Project khác không thấy collection này. Cache retrieval gắn project và `corpus_version`.

Tool `search_knowledge_base` là tool thường trong registry. Workflow knowledge dùng nó. Supervisor cũng chỉ gọi được nếu agent đó được cấp.

## Eval

Một dataset, hai experiment (đổi model hoặc đổi prompt version hoặc đổi mode). Báo cáo cạnh nhau: faithfulness hoặc task completion, tool chọn đúng, lần gọi thừa, unauthorized tool, latency, giá. Gate đỏ nếu leak hoặc unauthorized tool khác 0 trên bộ security. Điểm của model kịch bản không được gọi là chất lượng model.

Scorer tự viết để CI không cần mạng. Ragas chỉ là đối chiếu thêm, không phải cửa ải.

## Trace

Một run một trace: auth, nạp config, từng node, từng tool, từng lần gateway, validate. Span không chứa câu hỏi, tài liệu, câu trả lời, hay key. Metric: request, lỗi, latency p95 tách khỏi thời gian provider, token, cost, tool success, run completion.

Demo sự cố: provider chính trả lỗi, fallback đúng policy, trace có cả hai lần gọi, usage có hai dòng hoặc một dòng ghi rõ fallback, tổng giá khớp đơn giá của từng model.

## Code

| Path | Được phụ thuộc | Không được làm |
|---|---|---|
| `control/` | Postgres, Redis | Import agent graph, gọi LLM |
| `core/gateway/` | Provider port, usage | Biết một agent cụ thể |
| `core/runtime/` | Gateway port, tool port, checkpoint | Hash API key, tính hóa đơn |
| `core/tools/` | Registry, approval, idempotency | Tự mở database của platform; tự cấp quyền |
| `core/rag/` | Qdrant, object storage | Quyết định quota |
| `core/eval/` | Dataset, scorer | Gọi provider ngoài gateway |
| `apps/api/` | Ghép các port | Chứa luật nghiệp vụ |
| `apps/workers/` | Hàng đợi, rag, eval | Nhận request người dùng |
| `apps/tools/` | Execute tool body | Allow-list, approval, idempotency |
| `examples/knowledge/` `examples/desk/` `_template/` | Chỉ HTTP của platform | Import `core` / `control` |
| `docs/adr/` | Ghi quyết định của phase vừa xong | Viết trước khi có code của phase đó |

## Phase

Unit xanh mới sang phase sau. Integration của phase đó xanh trên Compose.

### 1. Control plane

Compose: Postgres, Redis. Org, project, thành viên, vai. Tạo key, xác thực key, thu hồi key. Audit. Hai project không đọc chéo config.

Học: tenant là dữ liệu, không phải biến global. Key là bí mật chỉ hiện một lần.

### 2. Gateway

Registry model và pricing version. Completion qua một hàm. Policy route. Fallback bị cấm khi chưa khai báo. Usage theo project. Giết provider chính trong test thì thấy fallback hoặc lỗi có chủ đích, không thấy model lạ.

Học: gateway là chính sách. Thư viện gọi HTTP là chi tiết.

### 3. Runtime một agent

Tạo agent bằng API. Run có trần bước, timeout, ngân sách. Checkpoint. Tool lạ bị `denied`. Tool ghi chờ approval. Retry sau approval không nhân đôi bản ghi. Crash process giữa chừng rồi resume.

Học: durable execution. Idempotency của side effect.

### 4. Supervisor

Một team hai vai đăng ký qua API. Parallel fan-in. Handoff. Vòng lặp bị cắt. Cùng task chạy mode `agent` và `supervisor`, eval ghi năm số. Chưa cần số đẹp. Cần số thật và câu nói "em không chọn mode trước khi đo".

### 5. RAG service

Knowledge base theo project. Ingest bất đồng bộ, idempotent, xóa version cũ, ACL hai lần, citation. App knowledge chỉ là client HTTP. Project kia không retrieve được.

### 6. Eval và trace

`POST /v1/evals` so hai experiment. Trace một run thất bại vì provider. Metric p95 không cộng thời gian model. Bộ security: injection trong tài liệu, tool không được cấp, PII không vào log.

### 7. Demo

`make demo`: tạo hai project, chạy knowledge, chạy desk tới approval, tắt provider, chỉ trace và usage. Sau đó viết `INTERVIEW.md` (map demo → từng mục trong `JD.md`) và năm ADR đã phát sinh: tenant, gateway không nhét vào LiteLLM, ba mode chứ không phải graph tự do, Postgres cộng Qdrant, durable run.

### Hardening — Fair admission

Đã có rate quota và spend. Bổ sung concurrency per project, trần open run / ingest queue, fair claim trên worker. Một project spam nhận `429`; project kia vẫn được phục vụ. ADR `002-fair-admission.md`.

### Hardening — Provider resilience

Credential tier `local` cùng `free` / `paid`. `tier_order` trên model (và override trên route): `free_paid_local` hoặc `free_local_paid`. Route `fallback_model_ids` tối đa hai model; capability phải cover; self-host là ModelCard `provider=selfhost` + credential local. Compose profile `local-llm` (Ollama) opt-in. Metric: `genai_provider_ms` chỉ wire time; thêm attempts / retries / fallbacks. ADR `004-model-fallback-chain.md`.

## Ngoài phạm vi

Admin web. IdP tự xây. Keycloak trong repo này. Vault. Kubernetes. Terraform. Billing thanh toán. Connector Gmail, CRM, Slack sống. Fine-tune. Vector database tự viết. Nhiều hơn hai app mẫu. Hơn ba mode chạy. MCP ngoài allow-list.

## Việc tiếp theo

Phase 1 khi được yêu cầu code. Không scaffold phase 2–7 trong cùng một lượt.
