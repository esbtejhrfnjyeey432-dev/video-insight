"""Durable storage for payment state and proof images.

Production uses PostgreSQL via ``DATABASE_URL``. Local development keeps the
existing atomic JSON/file implementation so contributors do not need a DB.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class PaymentStore:
    def __init__(self, database_url: str, state_path: Path, proof_dir: Path):
        self.database_url = (database_url or "").strip()
        self.state_path = Path(state_path)
        self.proof_dir = Path(proof_dir)
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self.state_loaded_durable = False
        self.last_error = ""

    @property
    def configured_durable(self) -> bool:
        return bool(self.database_url)

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - only possible in a broken deploy
            raise RuntimeError("psycopg 未安装，无法使用付款持久化数据库") from exc
        return psycopg.connect(self.database_url, connect_timeout=8)

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS video_insight_payment_state (
                            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                            payload JSONB NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS video_insight_payment_proofs (
                            order_id TEXT PRIMARY KEY,
                            filename TEXT NOT NULL,
                            content_type TEXT NOT NULL,
                            sha256 TEXT NOT NULL,
                            payload BYTEA NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS payment_proofs_sha256_idx
                        ON video_insight_payment_proofs (sha256)
                    """)
            self._schema_ready = True

    def durable_available(self) -> bool:
        if not self.configured_durable:
            self.last_error = "DATABASE_URL 未配置"
            return False
        try:
            self._ensure_schema()
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            self.last_error = ""
            return True
        except Exception as exc:
            self._schema_ready = False
            self.last_error = type(exc).__name__
            return False

    def _load_local(self, default: dict) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("clients"), dict):
                return data
        except (OSError, ValueError, TypeError):
            pass
        return dict(default)

    def load_state(self, default: dict) -> dict:
        if not self.configured_durable:
            return self._load_local(default)
        try:
            self._ensure_schema()
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT payload FROM video_insight_payment_state WHERE singleton = TRUE")
                    row = cur.fetchone()
                    if row:
                        data = row[0]
                        if isinstance(data, str):
                            data = json.loads(data)
                        if isinstance(data, dict):
                            self.state_loaded_durable = True
                            self.last_error = ""
                            return data
            # One-time migration: seed PostgreSQL from the old local JSON if present.
            data = self._load_local(default)
            self.state_loaded_durable = True
            self.save_state(data)
            return data
        except Exception as exc:
            self._schema_ready = False
            self.state_loaded_durable = False
            self.last_error = type(exc).__name__
            return self._load_local(default)

    def save_state(self, state: dict) -> None:
        if self.configured_durable:
            if not self.state_loaded_durable:
                raise RuntimeError("付款状态尚未从持久化数据库载入，拒绝覆盖数据库")
            self._ensure_schema()
            from psycopg.types.json import Jsonb
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO video_insight_payment_state (singleton, payload, updated_at)
                        VALUES (TRUE, %s, NOW())
                        ON CONFLICT (singleton) DO UPDATE
                        SET payload = EXCLUDED.payload, updated_at = NOW()
                    """, (Jsonb(state),))
            self.last_error = ""
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def save_proof(self, order_id: str, filename: str, content_type: str,
                   sha256: str, raw: bytes) -> None:
        if self.configured_durable:
            self._ensure_schema()
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO video_insight_payment_proofs
                            (order_id, filename, content_type, sha256, payload, created_at)
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (order_id) DO UPDATE SET
                            filename = EXCLUDED.filename,
                            content_type = EXCLUDED.content_type,
                            sha256 = EXCLUDED.sha256,
                            payload = EXCLUDED.payload,
                            created_at = NOW()
                    """, (order_id, filename, content_type, sha256, raw))
            self.last_error = ""
            return
        self.proof_dir.mkdir(parents=True, exist_ok=True)
        (self.proof_dir / filename).write_bytes(raw)

    def get_proof(self, order_id: str) -> tuple[bytes, str, str] | None:
        if self.configured_durable:
            self._ensure_schema()
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT payload, content_type, filename
                        FROM video_insight_payment_proofs WHERE order_id = %s
                    """, (order_id,))
                    row = cur.fetchone()
            if not row:
                return None
            return bytes(row[0]), str(row[1]), str(row[2])
        matches = sorted(self.proof_dir.glob(f"{order_id}.*")) if self.proof_dir.is_dir() else []
        if not matches:
            return None
        path = matches[0]
        media = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(
            path.suffix.lstrip("."), "application/octet-stream")
        return path.read_bytes(), media, path.name
