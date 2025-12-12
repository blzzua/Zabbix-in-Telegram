import asyncio
import aiohttp
import logging
import json
import os
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime
from aiohttp import web
from pyzabbix import ZabbixAPI, ZabbixAPIException
import zbxtg_settings

# Конфігурація
class Config:
    BOT_API_KEY = zbxtg_settings.tg_key
    try:
        POLLING_TIMEOUT = int(zbxtg_settings.zbx_tg_polling_timeout)
    except:
        POLLING_TIMEOUT = 15
    STATE_FILE = os.getenv('STATE_FILE', zbxtg_settings.zbx_tg_tmp_dir + '/telegram_bot_state.json')
    HTTP_PORT = int(os.getenv('HTTP_PORT', '8089'))
    HTTP_HOST = os.getenv('HTTP_HOST', '127.0.0.1')
    ALLOWED_USER_IDS = zbxtg_settings.zbx_tg_daemon_enabled_ids

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('TelegramBotDaemon')


class StateManager:
    """Менеджер для збереження та завантаження стану бота"""

    def __init__(self, state_file: str):
        self.state_file = Path(state_file)
        self.last_update_id: Optional[int] = None
        self.load_state()

    def load_state(self):
        """Завантаження стану з файлу"""
        try:
            if self.state_file.exists():
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.last_update_id = data.get('last_update_id')
                    logger.info(f"Стан завантажено: last_update_id={self.last_update_id}")
            else:
                logger.info("Файл стану не знайдено, починаємо з початку")
        except Exception as e:
            logger.error(f"Помилка завантаження стану: {e}")
            self.last_update_id = None

    def save_state(self):
        """Збереження стану у файл"""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump({
                    'last_update_id': self.last_update_id,
                    'updated_at': datetime.now().isoformat()
                }, f, indent=2)
            logger.debug(f"Стан збережено: last_update_id={self.last_update_id}")
        except Exception as e:
            logger.error(f"Помилка збереження стану: {e}")

    def update_id(self, update_id: int):
        """Оновлення last_update_id"""
        if self.last_update_id is None or update_id > self.last_update_id:
            self.last_update_id = update_id
            self.save_state()


class ZabbixEventController:
    def __init__(self, server, user, password):
        self.api = ZabbixAPI(server)
        self.user = user
        self.password = password

    def login(self):
        self.api.login(self.user, self.password)

    def process_callback(self, callback_data: str, from_user: dict) -> int:
        """
        Обробляє дані зворотного виклику (формат PREFIX:PARAM2:EVENTID),
        автентифікується в Zabbix і підтверджує подію.

        :param callback_data: Рядок даних зворотного виклику (наприклад, 'AKN:empty_param:123456').
        :param from_user: Інформація про користувача.
        :param api: Екземпляр Zabbix API клієнта.
        :param zbx_api_user: Ім'я користувача Zabbix API.
        :param zbx_api_pass: Пароль Zabbix API.
        :return: 0 - успіх, -1 - подія не знайдена, -2 - вже підтверджено, -3 - невідомий префікс, -4 - невірний формат/помилка конвертації ID.
        """

        # 1. Розбиття даних зворотного виклику по одинарному символу ':'
        parts = callback_data.split(':')

        # Очікуємо мінімум 3 частини: [0]PREFIX, [1]PARAM2, [2]EVENTID
        if len(parts) < 3:
            logger.error( f"Помилка: Невірний формат callback_data. Очікується 'PREFIX:PARAM2:EVENTID'. Отримано: {callback_data}")
            return -4
        prefix = parts[0]

        if prefix == 'AKN':
            # 1.1 Обробка префікса AKN (Acknowledge)
            # Нам потрібні частини з індексами 0 (префікс) і 2 (eventid)
            event_id_str = parts[2]
            
            try:
                eventid = int(event_id_str)
            except ValueError:
                logger.error(f"Помилка: Не вдалося конвертувати '{event_id_str}' в число для eventid.")
                return -4  # Помилка конвертації ID

            logger.info(f"-> Спроба підтвердити eventid: {eventid} (Callback: {callback_data})")

            # todo: refactor, зробити так щоб була перевірка і якщо необхідно аутентифікація. якщо неможливо - аварійна зупинка роботи.

            # 3. Отримання події
            logger.debug(f"DEBUG:  pre step3")
            events = self.api.event.get(eventids=[eventid])
            logger.debug(f"DEBUG:  step3")
            if len(events) == 0:
                # Подія не знайдена
                logger.error(f"Помилка: Подію з ID {eventid} не знайдено.")
                return -1
            current_event = events[0]

            # 4. Перевірка статусу підтвердження
            if current_event.get('acknowledged') == '1':
                # Вже підтверджено
                logger.info(f"Подія з ID {eventid} вже підтверджена.")
                return -2

            # 5. Підтвердження події (Acknowledge)
            try:
                # message може містити інформацію про користувача, який натиснув кнопку
                first_name = from_user.get("first_name", "")
                last_name = from_user.get("last_name", "")
                username = from_user.get("username", "")
                ack_message = f"Acknowledged by {first_name} {last_name}"
                ack_message = (ack_message + " " + f"(@{username})") if len(username) > 0 else ack_message
                self.api.event.acknowledge(
                    eventids=[current_event["eventid"]],
                    action=4+2,
                    message=ack_message
                )
                logger.info(f"Успіх: Подія з ID {eventid} успішно підтверджена.")
                return 0
            except Exception as e:
                logger.error(f"Помилка під час підтвердження події {eventid}: {e}")
                return -6  # Помилка під час ACK

        elif prefix == 'MUT':
            # Очікувані частини: [0]MUT, [1]ЧАС_ГОД, [2]EVENTID
            if len(parts) < 3:
                logger.error(f"Помилка: Невірний формат MUT callback_data. Очікується 'MUT:time:EVENTID'. Отримано: {callback_data}")
                return -4

            time_hours_str = parts[1]
            event_id_str = parts[2]

            try:
                time_hours = int(time_hours_str)
                eventid = int(event_id_str)
            except ValueError:
                logger.error(f"Помилка: Не вдалося конвертувати час '{time_hours_str}' або eventid '{event_id_str}' в число.")
                return -4  # Помилка конвертації ID

            logger.info(f"-> Спроба приглушити eventid: {eventid} на {time_hours} год (Callback: {callback_data})")

            suppress_until_dt = datetime.now() + timedelta(hours=time_hours)
            suppress_until_timestamp = int(suppress_until_dt.timestamp())

            try:
                events = self.api.event.get(eventids=[eventid])
                if len(events) == 0:
                    logger.error(f"Помилка: Подію з ID {eventid} не знайдено.")
                    return -1
            except Exception as e:
                logger.error(f"Помилка отримання події {eventid} з Zabbix: {e}")
                return -5 # Помилка API

            # call zabbix api to suppress
            try:
                first_name = from_user.get("first_name", "")
                last_name = from_user.get("last_name", "")
                username = from_user.get("username", "")
                ack_message = f"Suppressed by {first_name} {last_name} for {time_hours} hours."
                ack_message = (ack_message + " " + f"(@{username})") if len(username) > 0 else ack_message

                # event.acknowledge з дією Suppress (значення 8)
                self.api.event.acknowledge(
                    eventids=[eventid],
                    action=32 + 4,  # 32 = Suppress  + 2 add message.
                    message=ack_message,
                    suppress_until=suppress_until_timestamp
                )
                logger.info(f"Успіх: Подія з ID {eventid} успішно приглушена до {suppress_until_dt.strftime('%Y-%m-%d %H:%M:%S')}.")
                return 0
            except Exception as e:
                logger.error(f"Помилка під час приглушення події {eventid}: {e}")
                return -7  # Помилка під час Suppress
                
        # 6. Місце для майбутнього розширення
        elif prefix == 'IGN':
            logger.info(f"Обробка: Ігнорування події за запитом користувача: {from_user}")
            return 0

        else:
            # Невідомий префікс
            logger.error(f"Помилка: Невідомий префікс callback_data: {prefix}")
            return -3

class TelegramBotDaemon:
    """main class Telegram-bot"""

    def __init__(self, config: Config, zbx: ZabbixEventController):
        self.config = config
        self.state_manager = StateManager(config.STATE_FILE)
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False
        self.api_base_url = f"https://api.telegram.org/bot{config.BOT_API_KEY}"
        self.zbx = zbx

        # Статистика
        self.stats = {
            'started_at': datetime.now(),
            'processed_callbacks': 0,
            'errors': 0,
            'last_poll_at': None
        }

    async def start(self):
        """daemon start"""
        logger.info("Запуск Telegram Bot Daemon...")
        self.running = True
        self.session = aiohttp.ClientSession()

        try:
            await self.polling_loop()
        finally:
            await self.stop()

    async def stop(self):
        """daemon stop"""
        logger.info("Зупинка Telegram Bot Daemon...")
        self.running = False
        if self.session:
            await self.session.close()

    async def polling_loop(self):
        """Основний цикл Long Polling"""
        logger.info("Початок Long Polling циклу")

        while self.running:
            try:
                updates = await self.get_updates()

                if updates:
                    await self.process_updates(updates)

                self.stats['last_poll_at'] = datetime.now()

            except asyncio.CancelledError:
                logger.info("Polling loop скасовано")
                break
            except Exception as e:
                logger.error(f"Помилка в polling loop: {e}")
                self.stats['errors'] += 1
                await asyncio.sleep(5)  # Затримка перед повторною спробою

    async def get_updates(self) -> List[Dict[Any, Any]]:
        """Отримання оновлень через Long Polling"""
        params = {
            'timeout': self.config.POLLING_TIMEOUT,
            'allowed_updates': json.dumps(['callback_query'])
        }

        # Встановлюємо offset якщо є last_update_id
        if self.state_manager.last_update_id is not None:
            params['offset'] = self.state_manager.last_update_id + 1

        try:
            url = f"{self.api_base_url}/getUpdates"
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=self.config.POLLING_TIMEOUT + 10)) as response:
                data = await response.json()

                if data.get('ok'):
                    return data.get('result', [])
                else:
                    logger.error(f"Помилка API: {data}")
                    return []

        except asyncio.TimeoutError:
            logger.debug("Timeout при очікуванні оновлень (це нормально)")
            return []
        except Exception as e:
            logger.error(f"Помилка отримання оновлень: {e}")
            return []

    async def process_updates(self, updates: List[Dict[Any, Any]]):
        """Обробка списку оновлень"""
        for update in updates:
            update_id = update.get('update_id')

            # Оновлюємо update_id незалежно від типу
            if update_id:
                self.state_manager.update_id(update_id)

            # Обробляємо тільки callback_query
            if 'callback_query' in update:
                await self.process_callback_query(update['callback_query'])

    async def process_callback_query(self, callback_query: Dict[Any, Any]):
        """Обробка callback_query"""
        try:
            callback_data = callback_query.get('data')
            from_user = callback_query.get('from')
            user_id = from_user.get('id')

            # Перевірка дозволених користувачів
            if user_id not in self.config.ALLOWED_USER_IDS:
                logger.warning(f"Користувач {user_id} не в списку дозволених. Ігноруємо.")
                return

            logger.info(f"Обробка callback від користувача {user_id}: {callback_data}")

            # Виклик функції обробки
            await self.process_callback(callback_data, from_user)

            self.stats['processed_callbacks'] += 1

        except Exception as e:
            logger.error(f"Помилка обробки callback_query: {e}")
            self.stats['errors'] += 1

    async def process_callback(self, callback_data: str, from_user: dict):
        """
        Функція обробки callback даних
        Тут повинна бути ваша бізнес-логіка
        """
        logger.info(f"process_callback викликано:")
        logger.info(f"  callback_data: {callback_data}")
        logger.info(f"  from_user: {from_user.get('first_name')} {from_user.get('last_name')} (@{from_user.get('username')})")

        # Розбір формату даних (приклад)
        if callback_data.startswith('MUT:'):
            parts = callback_data.split(':')
            if len(parts) == 3:
                action, hours, event_id = parts
                logger.info(f"  приклад. Mute на {hours} годин для події {event_id}")
        elif callback_data.startswith('AKN::'):
            parts = callback_data.split('::')
            if len(parts) == 2:
                action, event_id = parts
                logger.info(f"  приклад. Acknowledge для події {event_id}")

        # Тут додайте вашу логіку обробки
        process_callback_result = self.zbx.process_callback(callback_data, from_user)
        logger.info(f"DEBUG {process_callback_result=}")



class HTTPServer:
    """HTTP-сервер для моніторингу та управління. заплановоно для доробок, для webhook"""

    def __init__(self, daemon: TelegramBotDaemon, config: Config):
        self.daemon = daemon
        self.config = config
        self.app = web.Application()
        self.setup_routes()

    def setup_routes(self):
        """Налаштування маршрутів"""
        self.app.router.add_get('/health', self.health_check)
        self.app.router.add_get('/status/last_update_id', self.get_last_update_id)
        self.app.router.add_get('/stats', self.get_stats)
        self.app.router.add_post('/send_message', self.send_message)

    async def health_check(self, request):
        """Перевірка стану демона"""
        return web.json_response({
            'status': 'ok' if self.daemon.running else 'stopped',
            'uptime_seconds': (datetime.now() - self.daemon.stats['started_at']).total_seconds()
        })

    async def get_last_update_id(self, request):
        """Отримання останнього update_id"""
        return web.json_response({
            'last_update_id': self.daemon.state_manager.last_update_id
        })

    async def get_stats(self, request):
        """Отримання статистики"""
        stats = self.daemon.stats.copy()
        stats['started_at'] = stats['started_at'].isoformat()
        if stats['last_poll_at']:
            stats['last_poll_at'] = stats['last_poll_at'].isoformat()
        stats['last_update_id'] = self.daemon.state_manager.last_update_id

        return web.json_response(stats)

    async def send_message(self, request):
        """Відправка повідомлення через бота"""
        try:
            data = await request.json()
            chat_id = data.get('chat_id')
            text = data.get('text')

            if not chat_id or not text:
                return web.json_response(
                    {'error': 'chat_id та text обов\'язкові'},
                    status=400
                )

            url = f"{self.daemon.api_base_url}/sendMessage"
            async with self.daemon.session.post(url, json={'chat_id': chat_id, 'text': text}) as response:
                result = await response.json()
                return web.json_response(result)

        except Exception as e:
            logger.error(f"Помилка відправки повідомлення: {e}")
            return web.json_response({'error': str(e)}, status=500)

    async def start(self):
        """Запуск HTTP-сервера"""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.HTTP_HOST, self.config.HTTP_PORT)
        await site.start()
        logger.info(f"HTTP-сервер запущено на {self.config.HTTP_HOST}:{self.config.HTTP_PORT}")


async def main():
    """Головна функція"""
    config = Config()

    logger.info("=" * 60)
    logger.info("Telegram Bot Daemon - Long Polling")
    logger.info("=" * 60)
    logger.info(f"Bot API: {config.BOT_API_KEY[:10]}...")
    logger.info(f"Polling timeout: {config.POLLING_TIMEOUT}s")
    logger.info(f"State file: {config.STATE_FILE}")
    logger.info(f"HTTP server: {config.HTTP_HOST}:{config.HTTP_PORT}")
    logger.info(f"Allowed users: {config.ALLOWED_USER_IDS}")
    logger.info("=" * 60)

    zbx = ZabbixEventController(zbxtg_settings.zbx_server, zbxtg_settings.zbx_api_user, zbxtg_settings.zbx_api_pass)
    zbx.login()
    daemon = TelegramBotDaemon(config, zbx=zbx)
    http_server = HTTPServer(daemon, config)

    # Запуск HTTP-сервера
    await http_server.start()

    # Запуск демона
    try:
        await daemon.start()
    except KeyboardInterrupt:
        logger.info("Отримано сигнал зупинки (Ctrl+C)")
    finally:
        await daemon.stop()


if __name__ == '__main__':
    asyncio.run(main())
