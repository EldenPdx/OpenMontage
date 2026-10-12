"""Only the external HTTP boundary is faulted; successful writes reach real Logto."""

import httpx


class LoseResponse(httpx.AsyncBaseTransport):
    def __init__(self, method, path):
        self.method, self.path = method, path
        self.deliveries = 0
        self.statuses = []

    async def handle_async_request(self, request):
        async with httpx.AsyncHTTPTransport() as transport:
            response = await transport.handle_async_request(request)
            await response.aread()
        if request.method == self.method and (self.path(request.url.path) if callable(self.path) else request.url.path == self.path):
            self.deliveries += 1
            self.statuses.append(response.status_code)
            if self.deliveries == 1 and 200 <= response.status_code < 300:
                raise httpx.ReadError("Injected loss after real identity response", request=request)
        return response


class AfterResponse(httpx.AsyncBaseTransport):
    """Apply a real external identity change between authority checks."""

    def __init__(self, method, path, action, *, after=1):
        self.method, self.path, self.action = method, path, action
        self.after = after
        self.deliveries = 0

    async def handle_async_request(self, request):
        async with httpx.AsyncHTTPTransport() as transport:
            response = await transport.handle_async_request(request)
            await response.aread()
        if request.method == self.method and request.url.path == self.path:
            self.deliveries += 1
            if self.deliveries == self.after:
                self.action()
        return response


class UnknownThrottle(httpx.AsyncBaseTransport):
    """A gateway rejection provides no proof of Logto's transactional mail guard."""

    async def handle_async_request(self, request):
        if request.method == "POST" and request.url.path == "/api/organization-invitations":
            return httpx.Response(429, json={"code": "request.rate_limited"}, request=request)
        async with httpx.AsyncHTTPTransport() as transport:
            response = await transport.handle_async_request(request)
            await response.aread()
        return response


class UnavailableRead(httpx.AsyncBaseTransport):
    """Identity reads fail while a genuine browser session remains established."""

    async def handle_async_request(self, request):
        if request.method == "GET" and request.url.path == "/api/organization-roles":
            raise httpx.ConnectError("Injected identity read outage", request=request)
        async with httpx.AsyncHTTPTransport() as transport:
            response = await transport.handle_async_request(request)
            await response.aread()
        return response


class ChangedTemplate(LoseResponse):
    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        if request.method == "GET" and request.url.path.startswith("/api/organization-roles/") and request.url.path.endswith("/scopes"):
            return httpx.Response(200, json=[{"id": "foreign-scope", "name": "platform:manage"}], request=request)
        return response
