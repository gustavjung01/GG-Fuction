import json
import os
from pathlib import Path
from typing import Dict, Iterable, List

from google.cloud import storage as gcs_storage


class LeadStore:
    def __init__(self) -> None:
        self.bucket_name = os.getenv("LEADS_BUCKET", "").strip()
        self.local_path = Path(os.getenv("LEADS_LOCAL_PATH", "data/leads.jsonl"))
        self.local_settings_path = Path(os.getenv("SCAN_SETTINGS_LOCAL_PATH", "data/scan_settings.json"))
        self.settings_blob_name = os.getenv("SCAN_SETTINGS_BLOB", "config/scan_settings.json")
        self.client = gcs_storage.Client() if self.bucket_name else None

    def _gcs_blob_name(self, created_at: str) -> str:
        date_part = (created_at or "")[:10]
        return f"leads/{date_part}.jsonl"

    def _append_local(self, records: Iterable[Dict[str, object]]) -> None:
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        with self.local_path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _load_local(self) -> List[Dict[str, object]]:
        if not self.local_path.exists():
            return []
        records: List[Dict[str, object]] = []
        with self.local_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def _append_gcs(self, records: Iterable[Dict[str, object]]) -> None:
        if not self.client or not self.bucket_name:
            return
        bucket = self.client.bucket(self.bucket_name)
        grouped: Dict[str, List[Dict[str, object]]] = {}
        for record in records:
            blob_name = self._gcs_blob_name(str(record.get("created_at", "")))
            grouped.setdefault(blob_name, []).append(record)

        for blob_name, batch in grouped.items():
            blob = bucket.blob(blob_name)
            existing = blob.download_as_text(encoding="utf-8") if blob.exists() else ""
            new_lines = existing.rstrip("\n")
            additions = "\n".join(json.dumps(record, ensure_ascii=False) for record in batch)
            content = "\n".join(part for part in [new_lines, additions] if part)
            blob.upload_from_string(content + "\n", content_type="text/plain; charset=utf-8")

    def append_leads(self, records: Iterable[Dict[str, object]]) -> None:
        records = list(records)
        if not records:
            return
        if self.bucket_name and self.client:
            self._append_gcs(records)
        else:
            self._append_local(records)

    def load_all_leads(self) -> List[Dict[str, object]]:
        if self.bucket_name and self.client:
            return self._load_all_gcs()
        return self._load_local()

    def _load_all_gcs(self) -> List[Dict[str, object]]:
        if not self.client or not self.bucket_name:
            return []
        bucket = self.client.bucket(self.bucket_name)
        records: List[Dict[str, object]] = []
        for blob in self.client.list_blobs(bucket, prefix="leads/"):
            text = blob.download_as_text(encoding="utf-8")
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def save_settings(self, settings: Dict[str, object]) -> None:
        if self.bucket_name and self.client:
            bucket = self.client.bucket(self.bucket_name)
            blob = bucket.blob(self.settings_blob_name)
            blob.upload_from_string(
                json.dumps(settings, ensure_ascii=False, indent=2),
                content_type="application/json; charset=utf-8",
            )
            return

        self.local_settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_settings(self) -> Dict[str, object]:
        if self.bucket_name and self.client:
            bucket = self.client.bucket(self.bucket_name)
            blob = bucket.blob(self.settings_blob_name)
            if not blob.exists():
                return {}
            try:
                return json.loads(blob.download_as_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}

        if not self.local_settings_path.exists():
            return {}
        try:
            return json.loads(self.local_settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
