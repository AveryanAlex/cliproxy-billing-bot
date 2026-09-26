from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from cliproxy_billing.db import initialize_database, make_engine, make_sessions
from cliproxy_billing.keeper import KeeperClient
from cliproxy_billing.navigation import make_navigation_router
from cliproxy_billing.ui import ADMIN_BUTTON, KEYS_BUTTON, main_keyboard
from cliproxy_billing.user_bot import Linking, make_user_router


async def test_reply_button_escapes_key_input(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/navigation.db")
    await initialize_database(engine)
    sessions = make_sessions(engine)
    bot = Bot("123456:TEST")
    keeper = KeeperClient("http://localhost", "unused")
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(make_navigation_router(sessions, frozenset({99}), ZoneInfo("UTC")))
    dispatcher.include_router(make_user_router(sessions, keeper, frozenset({99}), ZoneInfo("UTC")))
    user_id = 42
    state = dispatcher.fsm.get_context(bot=bot, chat_id=user_id, user_id=user_id)
    await state.set_state(Linking.key)
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        text=KEYS_BUTTON,
    )
    with patch.object(bot, "send_message", new_callable=AsyncMock) as send_message:
        await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    assert await state.get_state() is None
    assert send_message.await_count == 1
    assert send_message.await_args is not None
    assert "Пока нет привязанных ключей" in send_message.await_args.args[1]
    await keeper.aclose()
    await bot.session.close()
    await engine.dispose()


def test_admin_keyboard_has_management_button() -> None:
    user_labels = [button.text for row in main_keyboard(is_admin=False).keyboard for button in row]
    admin_labels = [button.text for row in main_keyboard(is_admin=True).keyboard for button in row]
    assert ADMIN_BUTTON not in user_labels
    assert ADMIN_BUTTON in admin_labels
