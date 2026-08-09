# -*- coding: utf-8 -*-
"""
aiohttp_stubs
----------------------------------

Minimal stand-ins for the small part of :mod:`aiohttp` the plugins use:
``async with aiohttp.ClientSession() as s: async with s.get(...) as resp:``.

Using these instead of :class:`~unittest.mock.MagicMock` keeps the async
context manager protocol honest and lets tests assert on the requests that
were made.
"""


class FakeResponse:
    """A stub for ``aiohttp.ClientResponse``."""

    def __init__(self, status=200, json_data=None, text=""):
        self.status = status
        self._json_data = json_data
        self._text = text

    async def json(self, content_type="application/json"):
        return self._json_data

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """A stub for ``aiohttp.ClientSession``.

    Hands out the queued ``responses`` in order. A queued
    :class:`Exception` is raised instead of returned, which simulates a
    connection error or timeout.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def _request(self, method, url, kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected {} request to {}".format(method, url))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self._request("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, kwargs)


def fake_session(*responses):
    """Return a ``(factory, session)`` pair.

    ``factory`` is a drop-in replacement for ``aiohttp.ClientSession`` that
    always yields ``session``, so the test can inspect
    :attr:`FakeSession.requests` afterwards.
    """
    session = FakeSession(responses)

    def factory(*args, **kwargs):
        return session

    return factory, session
