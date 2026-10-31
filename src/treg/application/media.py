"""Host a reference file so a vendor can fetch it: the one use case behind `treg host`.

AIGC endpoints (reAPI, PiAPI, OpenRouter video) take reference images, voice clips and videos as
public URLs their fetcher downloads. An agent on a laptop has no host, and every paste host we tried
failed a vendor probe at least once (catbox unreachable from fal, tmpfiles answering HTML, uguu
timing out). This keeps the bytes in treg and serves them from an opaque token.

Deliberately not metered: hosting is a courtesy like polling, so money's five entries stay untouched.
Bounded instead: a size cap per file, a daily byte quota per org, a TTL, and a media-only type list.

Bytes go to the archive's bucket under a random `hosted-media/v1/<hex>` key when `media_body_write`
is "r2", else into the row. No database connection is held while the bucket is read or written
(AGENTS.md rule 3): each database step opens and closes its own session around the storage I/O.
"""
from collections.abc import AsyncIterator
from datetime import timedelta
import logging
import secrets

from sqlalchemy import delete, func, select

from ..config import get_settings
from ..infra import object_store
from ..infra.db import session_maker
from ..models import Media, _now

log = logging.getLogger("treg.media")

MAX_BYTES = 30 * 1024 * 1024          # matches the vendors' per-reference limits
DAILY_ORG_BYTES = 300 * 1024 * 1024   # ten maximal files a day; a runaway agent stops there
TTL = timedelta(days=7)               # reAPI's own output URLs live about this long
ALLOWED_PREFIXES = ("image/", "audio/", "video/")
# SVG is an image type that carries script. Served inline on the treg.to origin it would run with a
# logged-in viewer's session, and no vendor takes an SVG reference anyway.
DENIED_TYPES = frozenset({"image/svg+xml", "image/svg"})


KEY_PREFIX = "hosted-media/v1/"

_store_override: object_store.StreamedObjectStore | None = None
_store_cache: tuple[tuple, object_store.StreamedObjectStore | None] | None = None


class MediaRejected(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class MediaUnavailable(Exception):
    """The object store could not serve a body that should be there."""


def use_store(store: object_store.StreamedObjectStore | None) -> None:
    """Tests and local tools: serve every call from `store` (None restores the configured one)."""
    global _store_override
    _store_override = store


def store() -> object_store.StreamedObjectStore | None:
    """The bucket bodies are read from and written to, or None while it is not configured. Opened
    whenever it is configured, whatever `media_body_write` says, so rows written before a switch
    back to "db" are still served."""
    global _store_cache
    if _store_override is not None:
        return _store_override
    s = get_settings()
    sig = (s.archive_object_store_endpoint, s.archive_object_store_bucket,
           s.archive_object_store_access_key_id, s.media_r2_timeout_s)
    if _store_cache is None or _store_cache[0] != sig:
        opened = (object_store.build_r2(s, max_body_bytes=MAX_BYTES, timeout_s=s.media_r2_timeout_s)
                  if object_store.configured(s) else None)
        _store_cache = (sig, opened)
    return _store_cache[1]


def validate_configuration() -> None:
    """Startup check: writing bodies to a bucket that is not configured fails loud, not per upload."""
    if get_settings().media_body_write == "r2" and store() is None:
        raise RuntimeError("media_body_write is r2 but the archive object store is not configured")


async def put(*, org_id: int, body: bytes, content_type: str) -> Media:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if not ctype.startswith(ALLOWED_PREFIXES) or ctype in DENIED_TYPES:
        raise MediaRejected(415, f"only image/*, audio/* and video/* (not SVG) can be hosted, not {ctype or 'an untyped body'!r}")
    if not body:
        raise MediaRejected(400, "empty body")
    if len(body) > MAX_BYTES:
        raise MediaRejected(413, f"file is {len(body)} bytes; the cap is {MAX_BYTES}")
    to_store = get_settings().media_body_write == "r2"
    st = store() if to_store else None
    if to_store and st is None:
        raise MediaRejected(503, "media storage is not configured")
    now = _now()
    async with session_maker() as db:
        # ponytail: expiry is swept inline on every upload rather than by a cron; a dedicated
        # sweep only matters once uploads are rare and the table is not.
        swept = (await db.execute(delete(Media).where(Media.expires_at < now)
                                  .returning(Media.object_key))).scalars().all()
        # ponytail: read-then-insert, so two concurrent uploads can overshoot the quota by one file;
        # a row lock per org is the upgrade if the courtesy ever gets abused.
        used = (await db.execute(select(func.coalesce(func.sum(Media.size), 0)).where(
            Media.org_id == org_id, Media.created_at >= now - timedelta(days=1)))).scalar_one()
        await db.commit()
    await _delete_objects([key for key in swept if key])
    if used + len(body) > DAILY_ORG_BYTES:
        raise MediaRejected(429, f"this team has hosted {used} bytes in the last 24 h; the daily quota is {DAILY_ORG_BYTES}")
    key = None
    if st is not None:
        key = KEY_PREFIX + secrets.token_hex(32)
        try:
            await st.put_named(key, body)
        except object_store.ObjectStoreError as exc:
            log.warning("media upload failed reason=%s", exc.reason)
            raise MediaRejected(503, "media storage is unavailable; try again") from None
    row = Media(token=secrets.token_urlsafe(24), org_id=org_id, content_type=ctype, size=len(body),
                body=None if key else body, object_key=key, created_at=now, expires_at=now + TTL)
    try:
        async with session_maker() as db:
            db.add(row)
            await db.commit()
    except Exception:
        if key:
            await _delete_objects([key])
        raise
    return row  # every field the router answers with was set above; no re-read of the body


async def _delete_objects(keys: list[str]) -> None:
    """Best effort: an object left behind is removed by the bucket's lifecycle rule on KEY_PREFIX."""
    st = store()
    if not keys or st is None:
        return
    for key in keys:
        try:
            await st.delete_named(key)
        except object_store.ObjectStoreError as exc:
            log.warning("media object delete failed reason=%s", exc.reason)


async def get(token: str) -> Media | None:
    async with session_maker() as db:
        row = (await db.execute(select(Media).where(Media.token == token))).scalar_one_or_none()
        if row is None or row.expires_at < _now():
            return None
        return row


async def open_body(row: Media) -> AsyncIterator[bytes] | None:
    """The stored bytes of a row whose body lives in the object store, as a stream, or None when
    the object is gone. Call with no session open."""
    st = store()
    if st is None:
        raise MediaUnavailable("media storage is not configured")
    try:
        opened = await st.open_named(row.object_key)
    except object_store.ObjectStoreError as exc:
        log.warning("media read failed reason=%s", exc.reason)
        raise MediaUnavailable(exc.reason) from None
    if opened is None:
        return None
    size, chunks = opened
    if size != row.size:
        await chunks.aclose()
        log.warning("media read failed reason=size_mismatch")
        raise MediaUnavailable("size_mismatch")
    return chunks
