import asyncio
import os
from datetime import date, datetime, timezone, timedelta
from typing import Any, Awaitable, Callable, Dict

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from dotenv import load_dotenv

from route import (
    start_route,
    stop_route,
    get_active_user_ids,
    ADDRESS_OWNERS,
    get_address_owner,
    cancel_address,
    skip_if_current_cancelled,
)
from gps import (
    process_driver_location,
    reset_tracking,
    seconds_since_last_update,
)


# ============================================================
# 0. Часовой пояс проекта (Казахстан, UTC+5)
# ============================================================
# Серверы (например Amvera) работают по системному времени UTC+0.
# Задаём часовой пояс явно, чтобы дедлайны и проверки времени
# всегда совпадали с реальным временем водителей и диспетчеров.

KZ_TIMEZONE = timezone(timedelta(hours=5))


# ============================================================
# 1. Загрузка переменных окружения
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")


# ============================================================
# 1.0 Список разрешённых пользователей (whitelist)
# ============================================================

_raw_allowed_ids = os.getenv("ALLOWED_USER_IDS", "")

# ALLOWED_USER_IDS — список ВОДИТЕЛЕЙ (могут запускать маршрут).
ALLOWED_USER_IDS: set[int] = {
    int(user_id.strip())
    for user_id in _raw_allowed_ids.split(",")
    if user_id.strip().isdigit()
}


# ============================================================
# 1.0.1 Владельцы адресов
# ============================================================

ADDRESS_OWNER_IDS: set[int] = {
    owner_id
    for owner_id in ADDRESS_OWNERS.values()
    if owner_id is not None
}


# ============================================================
# 1.1 Настройки таймаута GPS
# ============================================================

LOCATION_TIMEOUT_SECONDS = 150  # 2.5 минуты

driver_names: dict[int, str] = {}
warned_users: set[int] = set()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в файле .env")


# ============================================================
# 2.1 Отправка текстового статуса в группу диспетчеров
# ============================================================

async def notify_group(text: str):
    if not GROUP_CHAT_ID:
        return

    try:
        await bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=text,
        )
    except Exception as error:
        print(f"⚠️ Не удалось отправить статус в группу: {error}")


# ============================================================
# 1.2 Middleware проверки доступа (whitelist)
# ============================================================

class AccessMiddleware(BaseMiddleware):

    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: Dict[str, Any],
    ) -> Any:

        if not ALLOWED_USER_IDS:
            return await handler(event, data)

        combined_allowed_ids = ALLOWED_USER_IDS | ADDRESS_OWNER_IDS

        user_id = None

        if event.message and event.message.from_user:
            user_id = event.message.from_user.id

        elif event.edited_message and event.edited_message.from_user:
            user_id = event.edited_message.from_user.id

        elif event.callback_query and event.callback_query.from_user:
            user_id = event.callback_query.from_user.id

        if user_id is None:
            return await handler(event, data)

        if user_id in combined_allowed_ids:
            return await handler(event, data)

        print(
            f"🚫 Доступ запрещён: user_id={user_id} "
            f"попытался воспользоваться ботом."
        )

        if event.message:
            await event.message.answer(
                "⛔️ У вас нет доступа к этому боту.\n"
                "Обратитесь к администратору."
            )

        elif event.callback_query:
            await event.callback_query.answer(
                "⛔️ Доступ запрещён.",
                show_alert=True,
            )

        return None


# ============================================================
# 2. Создание Telegram-бота и диспетчера
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.update.outer_middleware(AccessMiddleware())


# ============================================================
# 3. Клавиатуры
# ============================================================

def route_start_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="▶️ Начать маршрут"
                )
            ]
        ],
        resize_keyboard=True,
    )


def stop_route_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⛔️ Завершить маршрут",
                    callback_data="stop_route",
                )
            ]
        ]
    )


# ============================================================
# 5. Запуск маршрута
# ============================================================

async def send_route_start(message: Message):

    user_id = message.from_user.id
    driver_name = message.from_user.full_name

    driver_names[user_id] = driver_name

    # Получаем сегодняшнюю дату по часовому поясу Казахстана
    today = datetime.now(KZ_TIMEZONE).date()
    current_address = start_route(user_id, today=today)

    reset_tracking(user_id)
    warned_users.discard(user_id)

    if current_address is None:
        await message.answer(
            "ℹ️ На сегодня все адреса маршрута отменены. "
            "Ехать никуда не нужно.",
            reply_markup=route_start_keyboard(),
        )

        await notify_group(
            f"ℹ️ Водитель {driver_name}: на сегодня все "
            f"адреса маршрута отменены, выезд не требуется."
        )
        return

    await message.answer(
        "🚚 Маршрут начат!",
        reply_markup=ReplyKeyboardRemove(),
    )

    await message.answer(
        f"📍 {current_address['name']}, готовься.\n\n"
        f"Включи трансляцию геопозиции, чтобы бот "
        f"автоматически определял прибытие на адреса:\n"
        f"📎 (скрепка) → Геопозиция → "
        f"Транслировать геопозицию (выбери 8 часов).",
        reply_markup=stop_route_keyboard(),
    )

    await notify_group(
        f"🚚 Водитель {driver_name} начал маршрут.\n"
        f"📍 Едет на {current_address['name']}."
    )


# ============================================================
# 6. Хэндлеры команд и кнопок
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "Привет! Нажимай кнопку ниже, когда будешь готов начать маршрут.",
        reply_markup=route_start_keyboard(),
    )


@dp.message(F.text == "▶️ Начать маршрут")
async def start_button_handler(message: Message):
    user_id = message.from_user.id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await message.answer(
            "⛔️ У вас нет прав запускать маршрут."
        )
        return

    await send_route_start(message)


@dp.callback_query(F.data == "stop_route")
async def stop_callback_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    driver_name = callback.from_user.full_name

    print(
        f"\n⛔️ РУЧНАЯ ОСТАНОВКА МАРШРУТА\n"
        f"Водитель: {driver_name} (id {user_id})\n"
    )

    stop_route(user_id)
    reset_tracking(user_id)

    await callback.message.answer(
        "✅ Маршрут завершён.\n\n"
        "Не забудь отключить трансляцию геопозиции "
        "(смахни сообщение с геопозицией → Остановить).",
        reply_markup=route_start_keyboard(),
    )

    await notify_group(
        f"⛔️ Водитель {driver_name} вручную завершил маршрут."
    )

    await callback.answer()


# ============================================================
# 8.1 Настройка дедлайна отмены "на сегодня"
# ============================================================

CANCEL_TODAY_DEADLINE_HOUR = 10


# ============================================================
# 8.2 Команда /отменить
# ============================================================

@dp.message(F.text.startswith("/отменить"))
async def cancel_address_handler(message: Message):

    if not GROUP_CHAT_ID or str(message.chat.id) != str(GROUP_CHAT_ID):
        return

    user_id = message.from_user.id
    sender_name = message.from_user.full_name

    now_kz = datetime.now(KZ_TIMEZONE)
    today_kz = now_kz.date()

    raw_text = message.text.strip()
    content = raw_text[len("/отменить"):].strip()

    if not content:
        await message.reply(
            "⚠️ Формат команды:\n"
            "/отменить сегодня [Название адреса]\n"
            "/отменить [Название адреса] [ДД.ММ.ГГГГ]"
        )
        return

    address_name = ""
    target_date = None

    if content.lower().startswith("сегодня"):
        address_name = content[len("сегодня"):].strip(".,!?:; ")
        if not address_name:
            await message.reply(
                "⚠️ Формат команды:\n"
                "/отменить сегодня [Название адреса]\n"
                "/отменить [Название адреса] [ДД.ММ.ГГГГ]"
            )
            return
        target_date = today_kz
    else:
        parts = content.split(maxsplit=1)
        address_name = parts[0].strip(".,!?:; ")
        date_text = parts[1].strip() if len(parts) > 1 else None

        if not address_name:
            await message.reply(
                "⚠️ Формат команды:\n"
                "/отменить сегодня [Название адреса]\n"
                "/отменить [Название адреса] [ДД.ММ.ГГГГ]"
            )
            return

        if date_text is None:
            target_date = today_kz
        else:
            try:
                target_date = datetime.strptime(
                    date_text, "%d.%m.%Y"
                ).date()
            except ValueError:
                await message.reply(
                    "⚠️ Неверный формат даты. "
                    "Используйте ДД.ММ.ГГГГ, например 31.08.2026."
                )
                return

            if target_date < today_kz:
                await message.reply(
                    "⚠️ Нельзя отменить прошедшую дату."
                )
                return

    # ПЕРВЫМ ДЕЛОМ: Проверка прав на адрес
    owner_id = get_address_owner(address_name)

    if owner_id is None or owner_id != user_id:
        await message.reply(
            "⛔️ У вас нет прав отменять этот адрес."
        )
        return

    # ЗАТЕМ: Проверка дедлайна 10:00, если отмена на сегодня
    if target_date == today_kz and now_kz.hour >= CANCEL_TODAY_DEADLINE_HOUR:
        await message.reply(
            f"⚠️ Отмена на сегодня возможна только до "
            f"{CANCEL_TODAY_DEADLINE_HOUR}:00.\n"
            f"Укажите дату, если хотите отменить на "
            f"другой день:\n"
            f"/отменить {address_name} ДД.ММ.ГГГГ"
        )
        return

    # Выполняем отмену
    cancel_address(address_name, target_date, user_id)
    date_str = target_date.strftime("%d.%m.%Y")

    print(
        f"\n❌ ОТМЕНА АДРЕСА\n"
        f"Адрес: {address_name}\n"
        f"Дата: {date_str}\n"
        f"Отменил: {sender_name} (id {user_id})\n"
    )

    await notify_group(
        f"❌ Адрес '{address_name}' отменён на {date_str} "
        f"пользователем {sender_name}."
    )

    # Если отменили на сегодня — мгновенно реагируем на активных водителей
    if target_date != today_kz:
        return

    for driver_id in get_active_user_ids():
        skip_result = skip_if_current_cancelled(driver_id, today=today_kz)

        if skip_result is None:
            continue

        old_name, new_address = skip_result
        reset_tracking(driver_id)

        driver_name = driver_names.get(driver_id, f"id {driver_id}")

        if new_address:
            await notify_group(
                f"➡️ Едем на следующий адрес: "
                f"{new_address['name']}."
            )

            try:
                await bot.send_message(
                    driver_id,
                    f"📍 {new_address['name']}, готовься.",
                    reply_markup=stop_route_keyboard(),
                )
            except Exception as error:
                print(f"⚠️ Не удалось уведомить водителя {driver_id}: {error}")
        else:
            await notify_group(
                f"🏁 Водитель {driver_name} завершил маршрут "
                f"(все оставшиеся адреса отменены)."
            )

            try:
                await bot.send_message(
                    driver_id,
                    "🏁 Маршрут завершён — все оставшиеся "
                    "адреса на сегодня отменены.",
                    reply_markup=route_start_keyboard(),
                )
            except Exception as error:
                print(f"⚠️ Не удалось уведомить водителя {driver_id}: {error}")


# ============================================================
# 9. Обработка геопозиции
# ============================================================

@dp.message(F.location)
async def location_handler(message: Message):
    await handle_driver_location(message)


@dp.edited_message(F.location)
async def live_location_handler(message: Message):
    await handle_driver_location(message)


async def handle_driver_location(message: Message):
    user_id = message.from_user.id
    latitude = message.location.latitude
    longitude = message.location.longitude

    driver_names[user_id] = message.from_user.full_name
    warned_users.discard(user_id)

    result = process_driver_location(
        user_id,
        latitude,
        longitude,
    )

    if result is None or result == "waiting" or result is False:
        return

    if result == "finished":
        driver_name = message.from_user.full_name

        print(
            f"\n🏁 МАРШРУТ ЗАВЕРШЁН ПОЛНОСТЬЮ\n"
            f"Водитель: {driver_name} (id {user_id})\n"
        )

        await message.answer(
            "🏁 Маршрут завершён. Все адреса подтверждены!",
            reply_markup=route_start_keyboard(),
        )

        await notify_group(
            f"🏁 Водитель {driver_name} завершил маршрут. "
            f"Все адреса подтверждены!"
        )
        return

    next_address = result

    await message.answer(
        f"✅ Адрес подтверждён.\n\n"
        f"📍 {next_address['name']}, готовься.",
        reply_markup=stop_route_keyboard(),
    )

    driver_name = message.from_user.full_name

    await notify_group(
        f"✅ Водитель {driver_name}: адрес подтверждён.\n"
        f"📍 Едет на {next_address['name']}."
    )


# ============================================================
# 12. Фоновая проверка таймаута GPS
# ============================================================

async def location_timeout_watcher():
    while True:
        await asyncio.sleep(30)

        for user_id in get_active_user_ids():
            elapsed = seconds_since_last_update(user_id)

            if elapsed is None or elapsed <= LOCATION_TIMEOUT_SECONDS:
                continue

            if user_id in warned_users:
                continue

            warned_users.add(user_id)
            driver_name = driver_names.get(user_id, f"id {user_id}")
            minutes = int(elapsed // 60)

            print(
                f"\n⚠️ ПОТЕРЯН СИГНАЛ GPS\n"
                f"Водитель: {driver_name} (id {user_id})\n"
                f"Нет обновлений координат уже {minutes} мин.\n"
            )


# ============================================================
# 13. Точка входа
# ============================================================

async def main():
    print("Бот запущен...")
    asyncio.create_task(location_timeout_watcher())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())