from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

from assist_live_client.protocol import ProtocolError


class ServerAuth:
    """Keep the rejected token out of new requests until the entry reloads."""

    def __init__(self, reauth: Callable[[], None] | None = None) -> None:
        self._reauth = reauth
        self.rejected = False

    def _check(self) -> None:
        if self.rejected:
            raise ProtocolError("server token rejected", kind="unauthorized")

    def _refused(self, error: ProtocolError) -> None:
        if error.kind == "unauthorized" and not self.rejected:
            self.rejected = True
            if self._reauth is not None:
                self._reauth()

    async def request[T, **P](
        self, request: Callable[P, Awaitable[T]], /, *args: P.args, **kwargs: P.kwargs
    ) -> T:
        self._check()
        try:
            return await request(*args, **kwargs)
        except ProtocolError as error:
            self._refused(error)
            raise

    async def stream[T, **P](
        self,
        request: Callable[P, AsyncIterator[T]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> AsyncGenerator[T]:
        self._check()
        try:
            stream = request(*args, **kwargs)
            try:
                async for item in stream:
                    yield item
            finally:
                if isinstance(stream, AsyncGenerator):
                    await stream.aclose()
        except ProtocolError as error:
            self._refused(error)
            raise
