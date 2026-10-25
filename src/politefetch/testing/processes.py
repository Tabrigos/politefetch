"""Child processes for tests, started behind the network barrier.

The barrier patches the `socket` module of one process and does not reach its
children. `spawn` starts a child with the "spawn" method (a fresh interpreter,
nothing inherited by accident), enables a `NetworkBarrier` in it before
running the target, and hands back the target's result or exception.

Tests start processes only through `spawn` (D-012); a guard in the test suite
fails on any other way.
"""

import multiprocessing
import traceback
from collections.abc import Callable
from multiprocessing.connection import Connection, wait
from typing import Any

from politefetch.testing.barrier import NetworkBarrier

__all__ = ["Child", "ChildFailed", "spawn"]

_CONTEXT = multiprocessing.get_context("spawn")


class ChildFailed(RuntimeError):
    """The child ended without handing back a result: it died, was killed, or
    raised an exception that could not be sent back."""

    def __init__(self, exitcode: int | None, detail: str = "") -> None:
        super().__init__(exitcode, detail)
        self.exitcode = exitcode
        self.detail = detail

    def __str__(self) -> str:
        text = f"child process ended with exit code {self.exitcode}"
        return f"{text}: {self.detail}" if self.detail else text


def _child_main(
    conn: Connection, target: Callable[..., Any], args: tuple[Any, ...]
) -> None:
    with NetworkBarrier():
        try:
            result = target(*args)
        except BaseException as exc:
            try:
                conn.send(("error", exc))
            except Exception:
                conn.send(("unpicklable", traceback.format_exc()))
        else:
            conn.send(("result", result))
    conn.close()


class Child[T]:
    """A running child process started by `spawn`."""

    def __init__(self, target: Callable[..., T], args: tuple[Any, ...]) -> None:
        self._receiver, sender = _CONTEXT.Pipe(duplex=False)
        self._process = _CONTEXT.Process(
            target=_child_main, args=(sender, target, args), daemon=True
        )
        self._process.start()
        sender.close()

    @property
    def exitcode(self) -> int | None:
        return self._process.exitcode

    def kill(self) -> None:
        """Kill the child with SIGKILL (TerminateProcess on Windows), as a
        crash would."""
        self._process.kill()

    def join(self, timeout: float) -> T:
        """Wait for the child and return its result, or raise its exception.

        If the child is still running after `timeout` seconds it is killed and
        `TimeoutError` is raised. A child that ends without a result raises
        `ChildFailed`.
        """
        try:
            # Wait for the process too: on Windows a child killed before it
            # takes over its end of the pipe leaves that end open, and the
            # pipe never reports EOF.
            ready = wait([self._receiver, self._process.sentinel], timeout)
            if not ready:
                self._process.kill()
                self._process.join()
                raise TimeoutError(f"child process still running after {timeout}s")
            message: tuple[str, Any] | None
            try:
                # Anything the child sent is in the pipe before it exits.
                message = self._receiver.recv() if self._receiver.poll(0) else None
            except (EOFError, BrokenPipeError):  # died without sending anything
                message = None
        finally:
            self._receiver.close()
        # The child has sent its result or died; it ends right after.
        self._process.join(timeout)
        if self._process.is_alive():
            self._process.kill()
            self._process.join()
        exitcode = self._process.exitcode
        self._process.close()
        if message is None:
            raise ChildFailed(exitcode)
        kind, payload = message
        if kind == "result":
            return payload  # type: ignore[no-any-return]
        if kind == "error":
            raise payload
        raise ChildFailed(exitcode, payload)


def spawn[T](target: Callable[..., T], *args: Any) -> Child[T]:
    """Start `target(*args)` in a child process behind the network barrier.

    `target` and `args` must be picklable: `target` is a module-level function
    the child can import by name.
    """
    return Child(target, args)
