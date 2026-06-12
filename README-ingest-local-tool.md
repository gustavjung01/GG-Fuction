# Ingest text từ tool local

Repo đã có endpoint Cloud Run:

```text
POST /ingest-text
```

Endpoint gốc nhận chuẩn:

```json
{
  "url": "link bài hoặc nguồn",
  "author": "người đăng",
  "content": "nội dung bài",
  "min_score": 55
}
```

Nếu tool local của bạn đang xuất field khác như `text`, `post_text`, `message`, `post_url`, `authorName`, dùng adapter có sẵn:

```bash
export SERVICE_URL="https://fb-lead-scanner-638713993935.asia-southeast1.run.app"
export REVIEW_TOKEN="2f43138500176366bdaa6750984df9f4"

cat local_payload.json | python local_ingest_adapter.py
```

Ví dụ `local_payload.json`:

```json
{
  "post_url": "https://www.facebook.com/groups/1105863179934185/posts/test",
  "authorName": "Nguyen Van A",
  "text": "Mình đang cần vay gấp 30 triệu, ai hỗ trợ được không ạ?",
  "min_score": 55
}
```

Adapter sẽ tự đổi thành `content`, `url`, `author` rồi bắn lên Cloud Run.

Không có cookies Facebook, không login, không auto comment, không auto message.
