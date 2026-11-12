"""Cancellation drains and stops a synthetic subprocess before returning."""

import asyncio
from unittest.mock import patch

import pytest

from astrbot_plugin_parser.core import utils


class FakeProcess:
    returncode = None

    def __init__(self):
        self.communicating = asyncio.Event()
        self.terminated = False
        self.drained = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def communicate(self):
        if not self.terminated:
            self.communicating.set()
            await asyncio.Future()
        self.drained = True
        return b"", b""


@pytest.mark.parametrize("during_spawn", [False, True])
def test_cancel_awaits_process_start_and_termination(during_spawn):
    async def run():
        process = FakeProcess()
        starting, release = asyncio.Event(), asyncio.Event()

        async def spawn(*_args, **_kwargs):
            starting.set()
            if during_spawn:
                await release.wait()
            return process

        with patch.object(utils.asyncio, "create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(utils.exec_ffmpeg_cmd(["ffmpeg", "synthetic"]))
            if during_spawn:
                await starting.wait()
            else:
                await process.communicating.wait()
            task.cancel()
            await asyncio.sleep(0)
            if during_spawn:
                assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert process.terminated
        assert process.drained

    asyncio.run(run())


def test_cancel_kills_process_if_termination_does_not_finish():
    async def run():
        process = FakeProcess()
        process.killed = False

        def kill():
            process.killed = True
            process.returncode = -9

        process.kill = kill

        async def spawn(*_args, **_kwargs):
            return process

        async def timeout(awaitable, timeout):
            assert timeout == 5
            awaitable.close()
            raise TimeoutError

        with patch.object(utils.asyncio, "create_subprocess_exec", side_effect=spawn), patch.object(utils.asyncio, "wait_for", side_effect=timeout):
            task = asyncio.create_task(utils.exec_ffmpeg_cmd(["ffmpeg", "synthetic"]))
            await process.communicating.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert process.terminated
        assert process.killed
        assert process.drained

    asyncio.run(run())
