import uuid
from pathlib import Path

import boto3
from botocore.client import Config

from app.config import get_settings

settings = get_settings()


class StorageService:
    """S3 when configured (exp-tech); local disk fallback."""

    def __init__(self) -> None:
        self.upload_dir = Path(settings.upload_dir)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._s3 = None
        self._bytes_cache: dict[str, bytes] = {}
        if settings.use_s3:
            kwargs = {
                "aws_access_key_id": settings.aws_access_key_id,
                "aws_secret_access_key": settings.aws_secret_access_key,
                "region_name": settings.aws_region,
                "endpoint_url": settings.s3_endpoint_url
                or f"https://s3.{settings.aws_region}.amazonaws.com",
                "config": Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
            }
            self._s3 = boto3.client("s3", **kwargs)

    def make_key(self, task_id: str, filename: str) -> str:
        # Match existing Prisma app prefix
        ext = Path(filename).suffix or ".jpg"
        return f"gig-management/executor/{uuid.uuid4().hex}{ext}"

    def make_brand_key(self, brand_id: str | None, filename: str) -> str:
        ext = Path(filename).suffix.lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg"
        prefix = brand_id or "new"
        return f"gig-management/brands/{prefix}/{uuid.uuid4().hex}{ext}"

    def public_url(self, key: str) -> str:
        if settings.use_s3:
            return f"https://{settings.s3_bucket}.s3.{settings.aws_region}.amazonaws.com/{key}"
        return f"{settings.public_base_url}/uploads/{key}"

    def presign_put(self, key: str, content_type: str) -> tuple[str, bool]:
        base = settings.public_base_url.rstrip("/")
        proxy_url = f"{base}/api/uploads/local"
        if self._s3 and settings.s3_browser_upload:
            url = self._s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": settings.s3_bucket,
                    "Key": key,
                    "ContentType": content_type,
                },
                ExpiresIn=3600,
            )
            return url, True
        return proxy_url, False

    def upload_bytes(self, key: str, data: bytes, content_type: str = "image/jpeg") -> str:
        if not self._s3:
            return self.save_local(key, data)
        self._s3.put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return self.public_url(key)

    def save_local(self, key: str, data: bytes) -> str:
        path = self.upload_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.public_url(key)

    def resolve_local_path(self, key: str) -> Path | None:
        path = self.upload_dir / key
        return path if path.exists() else None

    def get_bytes(self, key: str | None, url: str) -> bytes | None:
        cache_key = key or self.key_from_url(url or "")
        if cache_key and cache_key in self._bytes_cache:
            return self._bytes_cache[cache_key]

        data = None
        if key:
            local = self.resolve_local_path(key)
            if local:
                data = local.read_bytes()
            elif self._s3:
                try:
                    obj = self._s3.get_object(Bucket=settings.s3_bucket, Key=key)
                    data = obj["Body"].read()
                except Exception:
                    data = None
        if data is None:
            derived = self.key_from_url(url or "")
            if derived and self._s3:
                try:
                    obj = self._s3.get_object(Bucket=settings.s3_bucket, Key=derived)
                    data = obj["Body"].read()
                except Exception:
                    data = None

        if data is not None and cache_key:
            if len(self._bytes_cache) > 64:
                # drop an arbitrary old entry
                self._bytes_cache.pop(next(iter(self._bytes_cache)))
            self._bytes_cache[cache_key] = data
        return data

    def key_from_url(self, url: str) -> str | None:
        if not url:
            return None
        markers = [
            f"{settings.s3_bucket}.s3.{settings.aws_region}.amazonaws.com/",
            f"{settings.s3_bucket}.s3.amazonaws.com/",
            ".amazonaws.com/",
        ]
        for m in markers:
            if m in url:
                return url.split(m, 1)[1].split("?", 1)[0]
        if url.startswith("gig-management/"):
            return url
        return None

    def presign_get(self, key_or_url: str, expires_in: int = 3600) -> str:
        """Return a time-limited GET URL for a private S3 object."""
        if not self._s3:
            return key_or_url
        key = key_or_url
        if key_or_url.startswith("http"):
            derived = self.key_from_url(key_or_url)
            if not derived:
                return key_or_url
            key = derived
        try:
            return self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.s3_bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception:
            return key_or_url

    def viewable_url(self, url: str | None, key: str | None = None) -> str:
        """Browser-safe relative URL proxied through our API (private bucket)."""
        derived = key or self.key_from_url(url or "")
        if derived and settings.use_s3:
            from urllib.parse import quote

            return f"/api/uploads/view?key={quote(derived, safe='')}"
        return url or ""


    def ensure_bucket_cors(self) -> None:
        """Merge app CORS origins into the S3 bucket so browser uploads work when enabled."""
        if not self._s3 or not settings.s3_bucket:
            return

        required = set(settings.cors_origin_list)
        required.update(
            {
                "http://localhost:5173",
                "http://127.0.0.1:5173",
                "http://localhost:3000",
                "http://127.0.0.1:3000",
            }
        )

        try:
            resp = self._s3.get_bucket_cors(Bucket=settings.s3_bucket)
            rules = list(resp.get("CORSRules", []))
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code not in ("NoSuchCORSConfiguration", "NoSuchBucket"):
                print(f"[experientia] S3 CORS read failed: {exc}")
                return
            rules = []

        changed = False
        if not rules:
            rules = [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["PUT", "GET", "HEAD", "POST"],
                    "AllowedOrigins": sorted(required),
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3000,
                }
            ]
            changed = True
        else:
            for rule in rules:
                origins = set(rule.get("AllowedOrigins", []))
                if "*" in origins:
                    continue
                missing = required - origins
                if missing:
                    rule["AllowedOrigins"] = sorted(origins | missing)
                    changed = True

        if not changed:
            return

        try:
            self._s3.put_bucket_cors(
                Bucket=settings.s3_bucket,
                CORSConfiguration={"CORSRules": rules},
            )
            print(f"[experientia] S3 bucket CORS updated ({settings.s3_bucket})")
        except Exception as exc:  # noqa: BLE001
            print(f"[experientia] S3 CORS update failed: {exc}")


storage = StorageService()
