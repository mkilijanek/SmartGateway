"""Request size limits as plain ASGI middleware: an oversized upload is refused with 413 before it is buffered in memory."""
import json
import os

MB = 1024 * 1024
GATEWAY_LIMIT = int(float(os.environ.get("SG_MAX_BODY_MB", "100")) * MB)   # chat bodies with base64 images/audio can be large
ADMIN_LIMIT = int(float(os.environ.get("SG_MAX_ADMIN_BODY_MB", "20")) * MB)
INFLIGHT_BUDGET = int(float(os.environ.get("SG_MAX_INFLIGHT_BODY_MB", "512")) * MB)   # request bytes being received/handled at the same moment, all requests together
_inflight = 0   # a plain int is safe here: the event loop is single-threaded
GATEWAY_PREFIXES = ("/gw/", "/v1/", "/unified/", "/mcp/", "/api/admin-gw/", "/api/admin-mcp/")


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global _inflight
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = GATEWAY_LIMIT if scope["path"].startswith(GATEWAY_PREFIXES) else ADMIN_LIMIT

        async def reject(status: int):
            if status == 503:
                msg, typ, extra = "The server is busy receiving other large requests, retry shortly", "overloaded_error", [(b"retry-after", b"5")]
            else:
                msg, typ, extra = f"Request body too large (limit {limit // MB} MB)", "invalid_request_error", []
            body = json.dumps({"error": {"message": msg, "type": typ, "code": status}}).encode()
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *extra]})
            await send({"type": "http.response.body", "body": body})

        for k, v in scope["headers"]:
            if k == b"content-length":
                try:
                    n = int(v)
                except ValueError:
                    return await reject(413)
                if n > limit:
                    return await reject(413)
                if _inflight + n > INFLIGHT_BUDGET:   # refuse before reading anything
                    return await reject(503)
        total, reserved, refused, done = 0, 0, 0, False

        async def counted():
            nonlocal total, reserved, refused
            global _inflight
            if refused:
                return {"type": "http.disconnect"}
            msg = await receive()
            if msg["type"] == "http.request":
                n = len(msg.get("body", b""))
                total += n
                if total > limit:   # stop feeding the app; whatever it answers is replaced by the error below
                    refused = 413
                    return {"type": "http.disconnect"}
                if _inflight + n > INFLIGHT_BUDGET:
                    refused = 503
                    return {"type": "http.disconnect"}
                _inflight += n
                reserved += n
            return msg

        async def watch(msg):
            nonlocal done
            if done:
                return
            if refused:
                done = True
                return await reject(refused)
            await send(msg)

        try:
            await self.app(scope, counted, watch)
            if refused and not done:   # the app finished without answering
                await reject(refused)
        finally:
            _inflight -= reserved   # the request is over: give its bytes back
