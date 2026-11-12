import asyncio
import pytest

from app.telegram_gateway import GatewayError, TelethonGateway, listening_event


class _HungZombieClient:
    def __init__(self, *, disconnect_hangs: bool = True):
        self.disconnect_hangs = disconnect_hangs
        self.disconnect_called = 0
        self.send_called = 0

    def is_connected(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.disconnect_called += 1
        if self.disconnect_hangs:
            await asyncio.sleep(60)

    def iter_dialogs(self):
        async def _gen():
            await asyncio.sleep(60)
            yield None  # pragma: no cover

        return _gen()

    async def send_message(self, *args, **kwargs):
        self.send_called += 1
        await asyncio.sleep(60)

    async def is_user_authorized(self) -> bool:
        await asyncio.sleep(60)
        return True

    async def catch_up(self) -> None:
        await asyncio.sleep(60)

    def add_event_handler(self, *args, **kwargs) -> None:
        pass

    def remove_event_handler(self, *args, **kwargs) -> None:
        pass

    async def run_until_disconnected(self) -> None:
        await asyncio.sleep(60)


class _HealthyFreshClient:
    def __init__(self, dialogs: list):
        self._dialogs = dialogs
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def iter_dialogs(self):
        dialogs = self._dialogs

        async def _gen():
            for d in dialogs:
                yield d

        return _gen()


def test_load_inbox_evicts_hung_post_hibernate_client_and_reconnects_preserving_session():
    async def _run():
        from telethon.tl.types import User

        gw = TelethonGateway()
        gw._rpc_timeout = 0.05
        gw._disconnect_timeout = 0.02

        sess_path = "data/sessions/acc_30.session"
        saved_ss = "1BVtsOHwBu5_fake_valid_session_marker"
        zombie = _HungZombieClient(disconnect_hangs=True)
        gw._clients[sess_path] = zombie
        gw._session_strings[sess_path] = saved_ss
        gw._warmed_dialogs.add(sess_path)

        fake_user = User(id=777001, first_name="Alice", username="alice_lead")
        fake_dialog = type(
            "FakeDialog",
            (),
            {
                "is_user": True,
                "is_group": False,
                "is_channel": False,
                "entity": fake_user,
                "name": "Alice",
                "unread_count": 1,
                "message": None,
                "dialog": type("D", (), {"read_outbox_max_id": 12})(),
            },
        )()
        fresh = _HealthyFreshClient([fake_dialog])

        orig_open = gw._open

        async def _patched_open(sp: str):
            if sp in gw._clients:
                return await orig_open(sp)
            gw._clients[sp] = fresh
            return fresh, False

        gw._open = _patched_open  # type: ignore[method-assign]

        rows = await asyncio.wait_for(gw.load_inbox(sess_path, limit=10), timeout=0.4)
        assert len(rows) == 1
        assert rows[0]["telegram_user_id"] == 777001
        assert zombie.disconnect_called >= 1
        assert gw._clients.get(sess_path) is fresh
        assert gw._session_strings.get(sess_path) == saved_ss
        assert not gw._session_lock(sess_path).locked()

    asyncio.run(_run())


def test_send_message_timeout_does_not_retry_write_and_evicts_zombie_preserving_session():
    async def _run():
        gw = TelethonGateway()
        gw._rpc_timeout = 0.05
        gw._disconnect_timeout = 0.02

        sess_path = "data/sessions/acc_31.session"
        saved_ss = "1BVtsOHwBu5_keep_me"
        zombie = _HungZombieClient(disconnect_hangs=True)
        gw._clients[sess_path] = zombie
        gw._session_strings[sess_path] = saved_ss

        with pytest.raises(GatewayError):
            await asyncio.wait_for(
                gw.send_message(sess_path, 999111, "Hello once"), timeout=0.4
            )

        # Must NEVER auto-retry non-idempotent send_message on RPC timeout
        assert zombie.send_called == 1
        assert sess_path not in gw._clients
        assert gw._session_strings.get(sess_path) == saved_ss
        assert not gw._session_lock(sess_path).locked()

    asyncio.run(_run())


def test_watch_private_catch_up_timeout_releases_session_lock():
    async def _run():
        gw = TelethonGateway()
        gw._rpc_timeout = 0.05
        gw._disconnect_timeout = 0.02

        sess_path = "data/sessions/acc_33.session"
        zombie = _HungZombieClient(disconnect_hangs=True)
        gw._clients[sess_path] = zombie

        async def _noop_msg(_payload):
            return None

        task = asyncio.create_task(gw.watch_private(sess_path, _noop_msg))
        try:
            await asyncio.wait_for(listening_event(gw, sess_path).wait(), timeout=0.4)
            assert not gw._session_lock(sess_path).locked()
        finally:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=0.4)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    asyncio.run(_run())
