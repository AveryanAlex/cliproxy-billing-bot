from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from cliproxy_billing.admin_bot import NewBilling, make_admin_router
from cliproxy_billing.billing import BillingService
from cliproxy_billing.db import initialize_database, make_engine, make_sessions
from cliproxy_billing.keeper import KeeperClient
from cliproxy_billing.ledger import upsert_user
from cliproxy_billing.navigation import make_fallback_router, make_navigation_router
from cliproxy_billing.ui import (
    ADMIN_BUTTON,
    ADMIN_NEW_BUTTON,
    ADMIN_USERS_BUTTON,
    CANCEL_BUTTON,
    KEYS_BUTTON,
    PAY_BUTTON,
    PAY_CUSTOM_BUTTON,
    RUB_BUTTON,
    USD_BUTTON,
    main_keyboard,
)
from cliproxy_billing.user_bot import Linking, Paying, make_user_router


def incoming(user_id: int, text: str, update_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Test"),
            text=text,
        ),
    )


def incoming_callback(user_id: int, data: str, update_id: int) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=User(id=user_id, is_bot=False, first_name="Admin"),
            chat_instance="test",
            message=Message(
                message_id=update_id,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                text="Страница участников",
            ),
            data=data,
        ),
    )


def reply_labels(send_message: AsyncMock) -> list[str]:
    assert send_message.await_args is not None
    markup = send_message.await_args.kwargs["reply_markup"]
    assert isinstance(markup, ReplyKeyboardMarkup)
    return [button.text for row in markup.keyboard for button in row]


def request_message(request: AsyncMock) -> SendMessage:
    assert request.await_args is not None
    method = request.await_args.args[1]
    assert isinstance(method, SendMessage)
    return method


def request_labels(request: AsyncMock) -> list[str]:
    markup = request_message(request).reply_markup
    assert isinstance(markup, ReplyKeyboardMarkup)
    return [button.text for row in markup.keyboard for button in row]


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
    with patch.object(bot, "send_message", new_callable=AsyncMock) as send_message:
        await dispatcher.feed_update(bot, incoming(user_id, KEYS_BUTTON, 1))
    assert await state.get_state() is None
    assert send_message.await_count == 1
    assert send_message.await_args is not None
    assert "Пока нет привязанных ключей" in send_message.await_args.args[1]
    await keeper.aclose()
    await bot.session.close()
    await engine.dispose()


async def test_payment_keyboards_and_invalid_amount(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/payment-navigation.db")
    await initialize_database(engine)
    sessions = make_sessions(engine)
    keeper = KeeperClient("http://localhost", "unused")
    bot = Bot("123456:TEST")
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(make_navigation_router(sessions, frozenset(), ZoneInfo("UTC")))
    dispatcher.include_router(make_user_router(sessions, keeper, frozenset(), ZoneInfo("UTC")))
    dispatcher.include_router(make_fallback_router(frozenset()))
    user_id = 42
    state = dispatcher.fsm.get_context(bot=bot, chat_id=user_id, user_id=user_id)
    with patch.object(bot.session, "make_request", new_callable=AsyncMock) as request:
        await dispatcher.feed_update(bot, incoming(user_id, PAY_BUTTON, 1))
        assert await state.get_state() == Paying.currency.state
        assert request_labels(request) == [USD_BUTTON, RUB_BUTTON, CANCEL_BUTTON]

        await dispatcher.feed_update(bot, incoming(user_id, USD_BUTTON, 2))
        assert await state.get_state() == Paying.choice.state
        assert request_labels(request) == [PAY_CUSTOM_BUTTON, CANCEL_BUTTON]

        await dispatcher.feed_update(bot, incoming(user_id, PAY_CUSTOM_BUTTON, 3))
        assert await state.get_state() == Paying.amount.state
        assert request_labels(request) == [CANCEL_BUTTON]

        await dispatcher.feed_update(bot, incoming(user_id, "что-то не то", 4))
        assert await state.get_state() == Paying.amount.state
        assert "Введите сумму числом" in request_message(request).text

        await dispatcher.feed_update(bot, incoming(user_id, CANCEL_BUTTON, 5))
        assert await state.get_state() is None
        assert PAY_BUTTON in request_labels(request)

        await dispatcher.feed_update(bot, incoming(user_id, PAY_BUTTON, 6))
        await dispatcher.feed_update(bot, incoming(user_id, USD_BUTTON, 7))
        await dispatcher.feed_update(bot, incoming(user_id, "12,34", 8))
        assert await state.get_state() == Paying.screenshot.state
        assert (await state.get_data())["amount_minor"] == 1234
        assert "$12.34" in request_message(request).text
        assert request_labels(request) == [CANCEL_BUTTON]

        await dispatcher.feed_update(bot, incoming(user_id, CANCEL_BUTTON, 9))
        await dispatcher.feed_update(bot, incoming(user_id, PAY_BUTTON, 10))
        await dispatcher.feed_update(bot, incoming(user_id, USD_BUTTON, 11))
        await dispatcher.feed_update(bot, incoming(user_id, "12.345", 12))
        assert await state.get_state() == Paying.choice.state
        assert "не больше двух знаков" in request_message(request).text
        assert request_labels(request) == [PAY_CUSTOM_BUTTON, CANCEL_BUTTON]
    await keeper.aclose()
    await bot.session.close()
    await engine.dispose()


async def test_unknown_text_after_restart_restores_keyboard(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/fallback.db")
    await initialize_database(engine)
    sessions = make_sessions(engine)
    bot = Bot("123456:TEST")
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(make_navigation_router(sessions, frozenset(), ZoneInfo("UTC")))
    dispatcher.include_router(make_fallback_router(frozenset()))
    with patch.object(bot, "send_message", new_callable=AsyncMock) as send_message:
        await dispatcher.feed_update(bot, incoming(42, "12.34", 1))
        assert send_message.await_args is not None
        assert "Не понял сообщение" in send_message.await_args.args[1]
        assert PAY_BUTTON in reply_labels(send_message)
    await bot.session.close()
    await engine.dispose()


async def test_admin_number_step_has_only_cancel(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/admin-navigation.db")
    await initialize_database(engine)
    sessions = make_sessions(engine)
    keeper = KeeperClient("http://localhost", "unused")
    billing = BillingService(sessions, keeper, ZoneInfo("UTC"))
    bot = Bot("123456:TEST")
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(make_navigation_router(sessions, frozenset({42}), ZoneInfo("UTC")))
    dispatcher.include_router(
        make_admin_router(sessions, billing, frozenset({42}), ZoneInfo("UTC"))
    )
    dispatcher.include_router(make_fallback_router(frozenset({42})))
    state = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
    with patch.object(bot, "send_message", new_callable=AsyncMock) as send_message:
        await dispatcher.feed_update(bot, incoming(42, ADMIN_BUTTON, 1))
        assert ADMIN_NEW_BUTTON in reply_labels(send_message)

        await dispatcher.feed_update(bot, incoming(42, ADMIN_NEW_BUTTON, 2))
        assert await state.get_state() == NewBilling.initial_date.state
        assert reply_labels(send_message) == [CANCEL_BUTTON]

        await dispatcher.feed_update(bot, incoming(42, CANCEL_BUTTON, 3))
        assert await state.get_state() is None
        assert ADMIN_NEW_BUTTON in reply_labels(send_message)
    await keeper.aclose()
    await bot.session.close()
    await engine.dispose()


async def test_participant_pages_and_return_to_same_page(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/participants.db")
    await initialize_database(engine)
    sessions = make_sessions(engine)
    async with sessions() as session:
        async with session.begin():
            for user_id in range(1, 20):
                await upsert_user(session, user_id, f"Person {user_id}")
    keeper = KeeperClient("http://localhost", "unused")
    billing = BillingService(sessions, keeper, ZoneInfo("UTC"))
    bot = Bot("123456:TEST")
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(make_navigation_router(sessions, frozenset({42}), ZoneInfo("UTC")))
    dispatcher.include_router(
        make_admin_router(sessions, billing, frozenset({42}), ZoneInfo("UTC"))
    )
    with patch.object(bot.session, "make_request", new_callable=AsyncMock) as request:
        await dispatcher.feed_update(bot, incoming(42, ADMIN_USERS_BUTTON, 1))
        page_one = request_message(request)
        assert "страница 1/3" in page_one.text
        assert "Person 8" in page_one.text
        assert "Person 9" not in page_one.text
        assert isinstance(page_one.reply_markup, InlineKeyboardMarkup)
        assert page_one.reply_markup.inline_keyboard[-1][0].callback_data == "admin:users:page:1"
        assert all(
            button.text != "⬅️ Управление"
            for row in page_one.reply_markup.inline_keyboard
            for button in row
        )

        await dispatcher.feed_update(bot, incoming_callback(42, "admin:users:page:1", 2))
        assert request.await_args is not None
        page_two = request.await_args.args[1]
        assert isinstance(page_two, EditMessageText)
        assert page_two.text is not None
        assert "страница 2/3" in page_two.text
        assert "Person 9" in page_two.text
        assert "Person 8" not in page_two.text

        await dispatcher.feed_update(bot, incoming_callback(42, "admin:person:9:page:1", 3))
        person = request_message(request)
        assert isinstance(person.reply_markup, InlineKeyboardMarkup)
        assert person.reply_markup.inline_keyboard[0][0].callback_data == "admin:users:page:1"

        await dispatcher.feed_update(bot, incoming_callback(42, "admin:users:page:1", 4))
        assert request.await_args is not None
        return_page = request.await_args.args[1]
        assert isinstance(return_page, EditMessageText)
        assert return_page.text is not None
        assert "страница 2/3" in return_page.text
    await keeper.aclose()
    await bot.session.close()
    await engine.dispose()


def test_admin_keyboard_has_management_button() -> None:
    user_labels = [button.text for row in main_keyboard(is_admin=False).keyboard for button in row]
    admin_labels = [button.text for row in main_keyboard(is_admin=True).keyboard for button in row]
    assert ADMIN_BUTTON not in user_labels
    assert ADMIN_BUTTON in admin_labels
