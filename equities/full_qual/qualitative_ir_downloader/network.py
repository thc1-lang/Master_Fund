"""One bounded HTTP transport with an established browser-session fallback.

An HTTP denial is evidence about this transport, never a browser-host blacklist.
"""
import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
import httpx
from .models import AccessEvent, CollectionError
from .urls import public_url

log = logging.getLogger(__name__)
DENIED = {401, 403, 429}

@dataclass
class ResourceResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    transport: str

class ResourceClient:
    def __init__(self, browser, context):
        self.browser, self.context = browser, context

    def record(self, url, transport, status=None, error=None):
        event = AccessEvent(url, transport, status, status in DENIED, error)
        self.browser.access_events.append(event)
        log.debug("Access transport=%s status=%s url=%s error=%s", transport, status, url, error or "")
        return event

    async def http(self, url: str, referer: str = "", max_bytes: int | None = None) -> ResourceResponse:
        limit = max_bytes or self.browser.config.max_pdf_bytes
        cookies = httpx.Cookies()
        for cookie in await self.context.cookies():
            cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
        headers = {}  # Use the HTTP library's honest default identity; do not impersonate Chromium.
        if referer:
            headers["Referer"] = referer
        timeout = httpx.Timeout(
            float(getattr(self.browser.config, "http_total_timeout", self.browser.config.navigation_timeout_ms / 1000)),
            connect=float(getattr(self.browser.config, "http_connect_timeout", 10.0)),
            read=float(getattr(self.browser.config, "http_read_timeout", self.browser.config.navigation_timeout_ms / 1000)),
        )
        async with httpx.AsyncClient(timeout=timeout, cookies=cookies, headers=headers, follow_redirects=False) as client:
            current, seen = url, set()
            max_redirects = int(getattr(self.browser.config, "max_redirects", 8))
            for _ in range(max_redirects):
                if not public_url(current) or current in seen:
                    raise CollectionError("INVALID_URL", f"Invalid URL or redirect cycle: {current}")
                seen.add(current)
                tracker = getattr(self.browser, "acquisition_tracker", None)
                if tracker:
                    tracker.check()
                for attempt in range(self.browser.config.retries):
                    try:
                        await asyncio.sleep(self.browser.config.request_delay)
                        if tracker:
                            tracker.start(tracker.current_substage or "HTTP", item=current,
                                          network_requests=tracker.counters.get("network_requests", 0) + 1)
                        async with asyncio.timeout(float(getattr(self.browser.config, "http_total_timeout", 45.0))):
                            stream_context = client.stream("GET", current)
                            async with stream_context as response:
                                self.record(current, "http", response.status_code)
                                if response.is_redirect:
                                    if "location" not in response.headers:
                                        raise CollectionError("HTTP_FAILED", "Redirect without Location")
                                    current = urljoin(current, response.headers["location"])
                                    break
                                if response.status_code in {408, 429} or response.status_code >= 500:
                                    if response.status_code in {401, 403}:
                                        raise CollectionError("SITE_BLOCKED", f"HTTP {response.status_code}: {current}")
                                    if attempt + 1 < self.browser.config.retries:
                                        if tracker:
                                            tracker.progress(tracker.current_substage or "HTTP", item=current,
                                                            network_requests=tracker.counters.get("network_requests", 0),
                                                            retry_attempt=attempt + 1, retry_max=self.browser.config.retries,
                                                            retry_reason=f"HTTP_{response.status_code}",
                                                            retry_next_delay_seconds=min(2 ** attempt, 8))
                                        await asyncio.sleep(min(2 ** attempt, 8))
                                        continue
                                    raise CollectionError("HTTP_RETRY_EXHAUSTED", f"HTTP {response.status_code} after {self.browser.config.retries} attempts")
                                data = bytearray()
                                async for chunk in response.aiter_bytes():
                                    data.extend(chunk)
                                    if len(data) > limit:
                                        raise CollectionError("SAFETY_LIMIT_REACHED", "Response byte limit exceeded")
                                result = ResourceResponse(current, response.status_code, dict(response.headers), bytes(data), "http")
                                if tracker:
                                    tracker.progress(tracker.current_substage or "HTTP", item=current,
                                                    network_requests=tracker.counters.get("network_requests", 0),
                                                    processed=tracker.counters.get("processed", 0) + 1)
                                return result
                    except (httpx.HTTPError, OSError, TimeoutError) as exc:
                        self.record(current, "http", error=str(exc))
                        terminal_transport = isinstance(exc, OSError) or any(
                            marker in str(exc).casefold()
                            for marker in ("getaddrinfo failed", "name_not_resolved", "connecterror", "connection refused")
                        )
                        if terminal_transport:
                            raise
                        if attempt + 1 == self.browser.config.retries:
                            raise
                        if tracker:
                            tracker.progress(tracker.current_substage or "HTTP", item=current,
                                            retry_attempt=attempt + 1, retry_max=self.browser.config.retries,
                                            retry_reason=type(exc).__name__,
                                            retry_next_delay_seconds=min(2 ** attempt, 8))
                        await asyncio.sleep(min(2 ** attempt, 8))
            raise CollectionError("HTTP_REDIRECT_LIMIT", "Too many redirects")

    async def session_request(self, url: str, referer: str) -> ResourceResponse:
        current, seen = url, set()
        max_redirects = int(getattr(self.browser.config, "max_redirects", 8))
        for _ in range(max_redirects):
            if not public_url(current) or current in seen:
                raise CollectionError("INVALID_URL", f"Invalid session-request redirect: {current}")
            seen.add(current)
            tracker = getattr(self.browser, "acquisition_tracker", None)
            if tracker:
                tracker.check()
                tracker.start(tracker.current_substage or "HTTP", item=current,
                              network_requests=tracker.counters.get("network_requests", 0) + 1)
            await asyncio.sleep(self.browser.config.request_delay)
            response = await self.context.request.get(current, headers={"Referer": referer}, timeout=self.browser.config.navigation_timeout_ms, max_redirects=0, fail_on_status_code=False)
            try:
                self.record(current, "browser_context", response.status)
                if 300 <= response.status < 400 and "location" in response.headers:
                    current = urljoin(current, response.headers["location"])
                    continue
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > self.browser.config.max_pdf_bytes:
                    raise CollectionError("SAFETY_LIMIT_REACHED", "Session response byte limit exceeded")
                body = await response.body()
                if len(body) > self.browser.config.max_pdf_bytes:
                    raise CollectionError("SAFETY_LIMIT_REACHED", "Session response byte limit exceeded")
                return ResourceResponse(current, response.status, dict(response.headers), body, "browser_context")
            finally:
                await response.dispose()
        raise CollectionError("HTTP_REDIRECT_LIMIT", "Too many session-request redirects")

    async def get(self, url: str, referer: str) -> ResourceResponse:
        response, failure = None, None
        try:
            response = await self.http(url, referer)
        except CollectionError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            failure = exc
        if response and 200 <= response.status < 300:
            return response
        # Use only the session that legitimately loaded the referring page.
        if urlsplit(referer).hostname in self.browser.accessible_hosts:
            log.info("Basic HTTP failed; using established browser context for linked resource %s", url)
            response = await self.session_request(url, referer)
        if response:
            code = "SITE_BLOCKED" if response.status in DENIED else "HTTP_FAILED"
            if not 200 <= response.status < 300:
                raise CollectionError(code, f"{response.transport} HTTP {response.status}: {response.url}")
            return response
        raise CollectionError("HTTP_FAILED", str(failure))
