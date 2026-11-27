"""Background endpoint health checks; in-memory status map used by routing."""
import asyncio
import time

from sqlmodel import select

from .. import accounts, config, discovery
from ..db import session
from ..models import Endpoint, Model
from .scheduler import scheduler

status: dict[int, str] = {}  # endpoint_id -> online | offline | unknown


async def _account_ok(ep_id: int):
    from ..accounts import tokens
    try:
        await tokens.fresh_access(ep_id)
        return True, None
    except accounts.AccountError as e:
        return False, str(e)[:200]
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}"


async def check_endpoint(ep_id: int) -> None:
    with session() as s:
        ep = s.get(Endpoint, ep_id)
        if not ep:
            return
        url, key, provider = ep.base_url, ep.api_key, ep.provider
    if accounts.is_oauth(provider):   # a subscription account: healthy while it can sign requests (no call to the vendor, no quota used)
        ok, err = await _account_ok(ep_id)
    else:
        ok, err = await discovery.ping(url, key)
    with session() as s:
        ep = s.get(Endpoint, ep_id)
        if not ep:
            return
        ep.status = "online" if ok else "offline"
        ep.last_error = err
        ep.last_checked = time.time()
        s.add(ep)
        s.commit()
    status[ep_id] = "online" if ok else "offline"


async def loop() -> None:
    while True:
        try:
            with session() as s:
                ids = [e.id for e in s.exec(select(Endpoint)).all()]
            await asyncio.gather(*(check_endpoint(i) for i in ids), return_exceptions=True)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(config.HEALTH_INTERVAL)


async def slot_sync_loop() -> None:
    """Poll upstream slot usage so traffic from other clients is taken into account."""
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=2.0)) as client:
        while True:
            try:
                with session() as s:
                    eps = [(e.id, e.base_url, e.api_key, e.slot_source) for e in s.exec(select(Endpoint)).all() if not accounts.is_oauth(e.provider)]
                    models = {}
                    for m in s.exec(select(Model)).all():
                        models.setdefault(m.endpoint_id, []).append(m.id)
                for ep_id, base, key, src in eps:
                    if src is None:  # first time: detect once
                        src = await discovery.detect_slot_source(base, key)
                        with session() as s:
                            ep = s.get(Endpoint, ep_id)
                            if ep:
                                ep.slot_source = src
                                s.add(ep)
                                s.commit()
                    ids = models.get(ep_id, [])
                    if src in (None, "none") or len(ids) != 1:
                        for mid in ids:
                            scheduler.stats.external_busy.pop(mid, None)
                        continue
                    busy = await discovery.read_busy_slots(client, base, key, src)
                    if busy is None:
                        scheduler.stats.external_busy.pop(ids[0], None)
                        continue
                    ours = scheduler.stats.inflight(ids[0])
                    scheduler.stats.external_busy[ids[0]] = max(0, busy - ours)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(2.0)
