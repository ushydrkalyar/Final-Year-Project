"""One-time eviction of pages a browser cached before they were sent with no-cache.

Such a page is reused for days without asking the server, so no redirect, renamed route or
header on the page can reach it. Every one of those pages that needs a session does call
/auth/me on load, so that response tells the browser to drop its HTTP cache, once.
Cookies, localStorage and IndexedDB (the saved projects) are not touched.
"""
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection

PROBE_PATH = "/auth/me"
MARKER = "cs_fresh_pages"
MAX_AGE = 365 * 24 * 3600


class EvictStalePages:
    def __init__(self, app, secure: bool = False):
        self.app = app
        self.cookie = f"{MARKER}=1; Max-Age={MAX_AGE}; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != PROBE_PATH or MARKER in HTTPConnection(scope).cookies:
            return await self.app(scope, receive, send)

        async def send_evicting(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Clear-Site-Data"] = '"cache"'
                headers.append("Set-Cookie", self.cookie)
            await send(message)

        await self.app(scope, receive, send_evicting)
