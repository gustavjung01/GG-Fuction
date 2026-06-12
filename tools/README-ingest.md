# Local tool ingest

Cloud Run nhận text qua `POST /ingest-text`. Tool local không cần cookies Facebook, không cần login, không auto comment/message. Nó chỉ gửi text đã cào được lên Cloud Run để phân loại và lưu lead.

Endpoint hiện nhận các field linh hoạt:

- Nội dung: `content`, `text`, `message`, `post_text`, `postText`, `body`, `caption`, `raw_text`
- Link nguồn: `url`, `source_url`, `sourceUrl`, `post_url`, `postUrl`, `link`, `href`
- Tác giả: `author`, `user`, `username`, `author_name`, `authorName`, `name`, `poster`

Ví dụ:

```bash
SERVICE_URL="https://fb-lead-scanner-638713993935.asia-southeast1.run.app"
TOKEN="2f43138500176366bdaa6750984df9f4"

curl -s -X POST "$SERVICE_URL/ingest-text?token=$TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "post_url": "https://www.facebook.com/groups/1105863179934185/posts/test",
    "authorName": "Nguyen Van A",
    "text": "Mình đang cần vay gấp 30 triệu, ai hỗ trợ được không ạ?",
    "min_score": 55
  }'
```

Hoặc dùng helper Python:

```bash
python ingest_client.py \
  --token "$TOKEN" \
  --url "https://www.facebook.com/groups/1105863179934185/posts/test" \
  --author "Nguyen Van A" \
  --text "Mình đang cần vay gấp 30 triệu, ai hỗ trợ được không ạ?"
```
