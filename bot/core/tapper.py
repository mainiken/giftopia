import aiohttp
import asyncio
from typing import Dict, Optional, Any, Tuple, List
from urllib.parse import urlencode, unquote
from aiocfscrape import CloudflareScraper
from aiohttp_proxy import ProxyConnector
from better_proxy import Proxy
from random import uniform, randint
from time import time
from datetime import datetime, timezone
import json
import os
import traceback
import random

from bot.utils.universal_telegram_client import UniversalTelegramClient
from bot.utils.proxy_utils import check_proxy, get_working_proxy
from bot.utils.first_run import check_is_first_run, append_recurring_session
from bot.config import settings
from bot.utils import logger, config_utils, CONFIG_PATH
from bot.exceptions import InvalidSession
from bot.core.headers import HEADERS
from bot.core.agents import generate_random_user_agent


class TapperBot:
    BASE_URL = "https://giftopia.games"
    EMOJI = {
        'debug': '🔍',
        'success': '✅',
        'info': 'ℹ️',
        'warning': '⚠️',
        'error': '❌',
        'balance': '💎',
        'giveaway': '⭐',
        'login': '🔑',
        'mission': '🎯',
        'sleep': '😴',
        'proxy': '🌐'
    }

    def __init__(self, tg_client: UniversalTelegramClient):
        self.tg_client = tg_client
        if hasattr(self.tg_client, 'client'):
            self.tg_client.client.no_updates = True
        self.session_name = tg_client.session_name
        self._http_client: Optional[CloudflareScraper] = None
        self._current_proxy: Optional[str] = None
        self._access_token: Optional[str] = None
        self._is_first_run: Optional[bool] = None
        self._init_data: Optional[str] = None
        self._current_ref_id: Optional[str] = None
        self._user_agent: Optional[str] = None
        self._auth_token: Optional[str] = None
        session_config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
        if not all(key in session_config for key in ('api', 'user_agent')):
            logger.critical(f"CHECK accounts_config.json as it might be corrupted")
            exit(-1)
        self._user_agent = session_config.get('user_agent')
        self.proxy = session_config.get('proxy')
        if self.proxy:
            proxy = Proxy.from_str(self.proxy)
            self.tg_client.set_proxy(proxy)
            self._current_proxy = self.proxy

    def _log(self, level: str, message: str, emoji_key: Optional[str] = None) -> None:
        if level == 'debug' and not settings.DEBUG_LOGGING:
            return
        emoji = self.EMOJI.get(emoji_key, '') if emoji_key else ''
        formatted_message = f"{emoji} {message}" if emoji else message
        session_prefix = f"{self.session_name} | "
        full_message = session_prefix + formatted_message
        if level == 'debug':
            logger.debug(full_message)
        elif level == 'info':
            logger.info(full_message)
        elif level == 'warning':
            logger.warning(full_message)
        elif level == 'error':
            logger.error(full_message)
        elif level == 'success':
            logger.success(full_message)
        else:
            logger.info(full_message)

    def get_ref_id(self) -> str:
        if self._current_ref_id is None:
            session_hash = sum(ord(c) for c in self.session_name)
            remainder = session_hash % 10
            if remainder < 6:
                self._current_ref_id = settings.REF_ID
            elif remainder < 8:
                self._current_ref_id = '252453226'
        return self._current_ref_id

    async def get_tg_web_data(self, app_name: str, path: str) -> str:
        try:
            webview_url = await self.tg_client.get_app_webview_url(
                app_name,
                path,
                settings.REF_ID
            )
            if not webview_url:
                raise InvalidSession("Failed to get webview URL")
            tg_web_data = unquote(
                string=webview_url.split('tgWebAppData=')[1].split('&tgWebAppVersion')[0]
            )
            self._init_data = tg_web_data
            self._log('debug', f'Получены TG Web Data для {app_name}: {tg_web_data}', 'info')
            return tg_web_data
        except InvalidSession as e:
            self._log('error', f'Сессия невалидна: {str(e)}', 'error')
            raise
        except aiohttp.ClientError as e:
            self._log('error', f"Сетевая ошибка при получении TG Web Data в TapperBot: {str(e)}", 'error')
            self._log('debug', traceback.format_exc(), 'debug')
            raise InvalidSession("Ошибка сети при получении TG Web Data в TapperBot")
        except Exception as e:
            if 'User is unauthorized' in str(e):
                self._log('error', f'Сессия невалидна: {str(e)}', 'error')
                raise InvalidSession(f'User is unauthorized: {str(e)}')
            self._log('error', f"Неизвестная ошибка при получении TG Web Data в TapperBot: {str(e)}", 'error')
            self._log('debug', traceback.format_exc(), 'debug')
            raise InvalidSession("Критическая ошибка при получении TG Web Data в TapperBot")

    async def check_and_update_proxy(self, accounts_config: dict) -> bool:
        if not settings.USE_PROXY:
            return True
        if not self._current_proxy or not await check_proxy(self._current_proxy):
            new_proxy = await get_working_proxy(accounts_config, self._current_proxy)
            if not new_proxy:
                return False
            self._current_proxy = new_proxy
            if self._http_client and not self._http_client.closed:
                await self._http_client.close()
            proxy_conn = {'connector': ProxyConnector.from_url(new_proxy)}
            self._http_client = CloudflareScraper(timeout=aiohttp.ClientTimeout(60), **proxy_conn)
            self._log('info', f'Переключен на новый прокси: {new_proxy}', 'proxy')
        return True

    async def initialize_session(self) -> bool:
        try:
            self._is_first_run = await check_is_first_run(self.session_name)
            if self._is_first_run:
                self._log('info', f'Первый запуск сессии {self.session_name}', 'info')
                await append_recurring_session(self.session_name)
            return True
        except Exception as e:
            self._log('error', f'Ошибка инициализации сессии: {str(e)}', 'error')
            return False

    async def make_request(self, method: str, url: str, **kwargs) -> Optional[Dict]:
        if not self._http_client:
            raise InvalidSession("HTTP client not initialized")
        try:
            async with getattr(self._http_client, method.lower())(url, **kwargs) as response:
                if response.status == 200:
                    result = await response.json()
                    await asyncio.sleep(random.uniform(1, 3))
                    return result
                elif response.status == 201:
                    result = await response.json()
                    await asyncio.sleep(random.uniform(1, 3))
                    return result
                self._log('error', f'Запрос {method} {url} завершился со статусом {response.status}', 'error')
                self._log('debug', f'Ответ: {await response.text()}', 'debug')
                await asyncio.sleep(random.uniform(1, 3))
                return None
        except Exception as e:
            self._log('error', f'Ошибка запроса {method} {url}: {str(e)}', 'error')
            self._log('debug', traceback.format_exc(), 'debug')
            return None

    async def run(self) -> None:
        if not await self.initialize_session():
            raise InvalidSession("Failed to initialize session")
        random_delay = uniform(1, settings.SESSION_START_DELAY)
        self._log('info', f'Бот запустится через ⌚<g> {int(random_delay)}s </g>' , 'sleep')
        await asyncio.sleep(random_delay)
        proxy_conn = {'connector': ProxyConnector.from_url(self._current_proxy)} if self._current_proxy else {}
        async with CloudflareScraper(timeout=aiohttp.ClientTimeout(60), **proxy_conn) as http_client:
            self._http_client = http_client
            while True:
                try:
                    session_config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
                    if not await self.check_and_update_proxy(session_config):
                        self._log('warning', 'Не удалось найти рабочий прокси. Сон 5 минут.', 'proxy')
                        await asyncio.sleep(300)
                        continue
                    await self.process_bot_logic()
                except InvalidSession as error:
                    self._log('error', f'Сессия невалидна, завершение работы: {error}', 'error')
                    return
                except Exception as error:
                    sleep_duration = uniform(60, 120)
                    self._log('error', f'Неизвестная ошибка: {error}. Сон на {int(sleep_duration)}s', 'error')
                    self._log('debug', traceback.format_exc(), 'debug')
                    await asyncio.sleep(sleep_duration)

    def _get_headers(self, user_agent: str = None, extra: dict = None) -> dict:
        headers = HEADERS.copy()
        headers['user-agent'] = user_agent or self._user_agent or generate_random_user_agent()
        if extra:
            headers.update(extra)
        return headers

    def _get_cookies(self) -> dict:
        return {
            "auth_token": self._auth_token or "",
            "i18next": "ru"
        }

    async def login_giftopia(self) -> bool:
        if not self._init_data:
            await self.get_tg_web_data(app_name="giftopia_gamebot", path="start")
        if not self._init_data:
            self._log('info', 'Не удалось получить init_data для логина.', 'warning')
            return False
        try:
            self._log('debug', f'Init data для логина: {self._init_data}', 'debug')
            headers = self._get_headers()
            data = {"telegramData": self._init_data}
            async with self._http_client.post(
                f"{self.BASE_URL}/api/auth/authenticate",
                headers=headers,
                json=data,
                cookies=self._get_cookies()
            ) as response:
                resp_json = await response.json()
                await asyncio.sleep(random.uniform(1, 3))
                if response.status not in (200, 201):
                    self._log('error', f'Ошибка логина: {response.status} {await response.text()}', 'error')
                    return False
                cookies = response.cookies
                if 'auth_token' in cookies:
                    self._auth_token = cookies['auth_token'].value
                    self._log('debug', f'auth_token получен из Set-Cookie: {self._auth_token}', 'success')
                elif resp_json.get("data", {}).get("auth_token"):
                    self._auth_token = resp_json["data"]["auth_token"]
                    self._log('debug', f'auth_token получен из JSON: {self._auth_token}', 'success')
                else:
                    self._log('warning', 'auth_token не получен после логина, но логин успешен.', 'warning')
                if resp_json.get("status") is True and resp_json.get("data", {}).get("user"):
                    self._log('debug', 'Успешный логин, пользователь получен.', 'success')
                    return True
                self._log('error', f'Логин неуспешен: {resp_json}', 'error')
                return False
        except Exception as exc:
            self._log('error', f'Ошибка логина: {exc}', 'error')
            return False

    async def _request_giftopia(self, method: str, url: str, **kwargs) -> dict:
        headers = self._get_headers()
        cookies = self._get_cookies()
        async with self._http_client.request(method, url, headers=headers, cookies=cookies, **kwargs) as response:
            if response.status != 200:
                self._log('error', f'Ошибка запроса {url}: {response.status} {await response.text()}', 'error')
                await asyncio.sleep(random.uniform(1, 3))
                return {}
            result = await response.json()
            await asyncio.sleep(random.uniform(1, 3))
            return result

    async def get_mission_status(self) -> dict:
        url = f"{self.BASE_URL}/api/missions/user"
        return await self._request_giftopia("GET", url)

    async def check_mission(self, completed: bool = True) -> dict:
        url = f"{self.BASE_URL}/api/missions/check"
        data = {"completed": completed}
        return await self._request_giftopia("POST", url, json=data)

    async def get_translation(self, lang: str = "ru") -> dict:
        url = f"{self.BASE_URL}/locales/{lang}/translation.json"
        return await self._request_giftopia("GET", url)

    async def process_bot_logic(self) -> None:
        self._log('debug', 'Запуск логики бота-тапера.', 'info')
        if not await self.login_giftopia():
            self._log('error', 'Не удалось выполнить логин. Пропускаю выполнение.', 'error')
            await asyncio.sleep(60)
            return
        await self._get_user_data()

        # Попытка подтвердить миссию сразу
        self._log('info', 'Попытка подтвердить текущую миссию...', 'mission')
        completed_attempt_response = await self.complete_mission(completed=True)

        mission_data_after_attempt = None
        if completed_attempt_response and completed_attempt_response.get('status') is True and completed_attempt_response.get('data'):
            mission_data_after_attempt = completed_attempt_response['data'].get('mission')
            if mission_data_after_attempt:
                status_after_attempt = mission_data_after_attempt.get('status')
                self._log('info', f'Статус миссии после попытки подтверждения: {status_after_attempt}', 'mission')

                # Если миссия завершена после первой попытки
                if status_after_attempt == 'COMPLETED':
                    self._log('success', 'Миссия успешно подтверждена.', 'success')
                # Если миссия активна и требует подписки (sequence 1 или наличие ссылок)
                elif status_after_attempt == 'ACTIVE' and (mission_data_after_attempt.get('sequence') == 1 or mission_data_after_attempt.get('channel_url') or mission_data_after_attempt.get('link') or mission_data_after_attempt.get('url')):
                    self._log('info', 'Миссия активна и требует подписки. Выполняю подписку...', 'mission')
                    await self._process_subscription_mission(mission_data_after_attempt)
                    # После подписки, попытка подтвердить миссию снова
                    self._log('info', 'Попытка подтвердить миссию после подписки...', 'mission')
                    completed_after_sub_response = await self.complete_mission(completed=True)
                    if completed_after_sub_response and completed_after_sub_response.get('status') is True and completed_after_sub_response.get('data'):
                         mission_data_after_sub = completed_after_sub_response['data'].get('mission')
                         if mission_data_after_sub and mission_data_after_sub.get('status') == 'COMPLETED':
                             self._log('success', 'Миссия успешно подтверждена после подписки.', 'success')
                         else:
                             self._log('error', 'Не удалось подтвердить миссию после подписки.', 'error')
                    else:
                         self._log('error', 'Не удалось получить ответ после попытки подтверждения миссии после подписки.', 'error')
                # Обработка миссии sequence 2, если она еще активна
                elif mission_data_after_attempt.get('missionType') == 2 and status_after_attempt != 'COMPLETED':
                     self._log('info', f'Миссия missionType 2, статус: {status_after_attempt}. Пытаюсь завершить...', 'mission')
                     # Мы уже пытались завершить ее первой попыткой complete_mission(completed=True),
                     # так что если она еще не COMPLETED, возможно, есть проблема или нужно подождать.
                     # Добавляем небольшую паузу перед проверкой статуса.
                     self._log('info', 'Ожидание перед повторной проверкой статуса миссии missionType 2.', 'sleep')
                     await asyncio.sleep(random.uniform(10, 30))

            else:
                self._log('warning', 'В ответе после попытки подтверждения нет данных о миссии.', 'warning')
        else:
            self._log('error', 'Ошибка или некорректный ответ при попытке подтверждения миссии.', 'error')

        # Проверяем статус миссии для определения времени следующей
        # Используем _check_mission_status, который сам делает запрос и парсит время
        sleep_duration = await self._check_mission_status()

        if sleep_duration is not None and sleep_duration > 60:
            extra_delay = random.randint(settings.SLEEP_MIN, settings.SLEEP_MAX) # Используем стандартные настройки задержки
            total_sleep = sleep_duration + extra_delay
            hours, remainder = divmod(total_sleep, 3600)
            minutes, seconds = divmod(remainder, 60)
            self._log('info', f'Сессия засыпает на ⌚<g> {int(hours)}ч {int(minutes)}м {int(seconds)}с </g> до следующей миссии.', 'sleep')
            await asyncio.sleep(total_sleep)
            self._log('info', 'Сессия проснулась.', 'sleep')
        else:
            # Стандартная пауза, если время следующей миссии не определено или очень мало
            self._log('debug', 'Стандартная пауза перед следующим циклом.', 'sleep')
            await asyncio.sleep(uniform(settings.SLEEP_MIN, settings.SLEEP_MAX))

    def _get_sleep_duration_from_expires(self, expires_at_str: str) -> Optional[int]:
        try:
            now = datetime.now(timezone.utc)
            expires_at = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
            time_difference = (expires_at - now).total_seconds()
            if time_difference > 0:
                return int(time_difference)
        except Exception:
            pass
        return None

    async def complete_mission(self, completed: bool = True) -> Optional[Dict]:
        if self._http_client is None or self._http_client.closed:
            self._log('warning', 'HTTP client is not initialized or closed.', 'warning')
            return None
        try:
            headers = self._get_headers()
            data = {"completed": completed}
            self._log('info', f'Attempting to complete mission with data: {data}', 'mission')
            response = await self.make_request(
                'POST',
                f"{self.BASE_URL}/api/missions/check",
                headers=headers,
                cookies=self._get_cookies(),
                json=data
            )
            if response and response.get('status') is True:
                self._log('success', f'Mission completion request successful: {response.get("data")}', 'success')
            else:
                self._log('error', f'Mission completion request failed: {response}', 'error')
            return response
        except Exception as e:
            self._log('error', f'Error completing mission: {str(e)}', 'error')
            self._log('debug', traceback.format_exc(), 'debug')
            return None

    async def _check_mission_status(self) -> Optional[int]:
        if not self._http_client or self._http_client.closed or not self._auth_token:
            self._log('warning', 'HTTP client не инициализирован, закрыт или отсутствует access_token.', 'warning')
            return None
        sleep_duration_seconds = None
        try:
            headers = self._get_headers()
            self._log('info', 'Проверка статуса миссии...', 'mission')
            response = await self.make_request(
                'GET',
                f"{self.BASE_URL}/api/missions/user",
                headers=headers,
                cookies=self._get_cookies()
            )
            mission_data = None
            if response and response.get('status') is True and response.get('data'):
                mission_data = response['data'].get('mission')
                if mission_data:
                    mission_status = mission_data.get('status')
                    streak = mission_data.get('streak')
                    start_at_str = mission_data.get('startAt')
                    expires_at_str = mission_data.get('expiresAt')
                    self._log('info', f'Статус миссии: {mission_status}, Стрик: {streak}', 'mission')
                    now = datetime.now(timezone.utc)
                    if mission_status == "COMPLETED" and expires_at_str:
                        try:
                            expires_at = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
                            time_difference = (expires_at - now).total_seconds()
                            if time_difference > 0:
                                hours, remainder = divmod(time_difference, 3600)
                                minutes, seconds = divmod(remainder, 60)
                                self._log('info', f'Следующая миссия будет доступна через '
                                            f'{int(hours)}ч {int(minutes)}м {int(seconds)}с.', 'mission')
                                sleep_duration_seconds = int(time_difference)
                            else:
                                self._log('info', 'Время следующей миссии уже прошло или некорректно.', 'info')
                        except ValueError as e:
                            self._log('error', f'Ошибка парсинга времени expiresAt: {e}', 'error')
                    elif mission_status != "COMPLETED" and start_at_str:
                        try:
                            start_at = datetime.fromisoformat(start_at_str.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
                            time_difference = (start_at - now).total_seconds()
                            if time_difference > 0:
                                hours, remainder = divmod(time_difference, 3600)
                                minutes, seconds = divmod(remainder, 60)
                                self._log('info', f'Миссия станет доступна через '
                                            f'{int(hours)}ч {int(minutes)}м {int(seconds)}с.', 'mission')
                                sleep_duration_seconds = int(time_difference)
                            else:
                                self._log('info', 'Время старта миссии уже прошло или некорректно.', 'info')
                        except ValueError as e:
                            self._log('error', f'Ошибка парсинга времени startAt: {e}', 'error')
                    else:
                        self._log('info', f'Миссия не выполнена или имеет другой статус: {mission_status}', 'mission')
                else:
                    self._log('warning', f'В ответе нет данных о миссии: {response}', 'warning')
            else:
                self._log('error', f'Ошибка при проверке статуса миссии: {response}', 'error')
        except Exception as e:
            self._log('error', f'Исключение при проверке статуса миссии: {str(e)}', 'error')
            self._log('debug', traceback.format_exc(), 'debug')
        return sleep_duration_seconds

    async def _get_user_data(self) -> None:
        if not self._init_data:
             self._log('warning', 'Отсутствует init_data для получения данных пользователя.', 'warning')
             await self.get_tg_web_data(app_name="giftopia_gamebot", path="start")
             if not self._init_data:
                 self._log('error', 'Не удалось получить init_data для данных пользователя.', 'error')
                 return
        if not self._http_client or self._http_client.closed:
            self._log('warning', 'HTTP client не инициализирован или закрыт.', 'warning')
            return
        try:
            headers = self._get_headers()
            data = {"telegramData": self._init_data}
            self._log('debug', 'Запрос данных пользователя...', 'info')
            response = await self.make_request(
                'POST',
                f"{self.BASE_URL}/api/auth/authenticate",
                headers=headers,
                json=data
            )
            if response and response.get('status') is True and response.get('data'):
                user_data = response['data'].get('user')
                if user_data:
                    username = user_data.get('username')
                    first_name = user_data.get('firstName')
                    balance = user_data.get('balance')
                    display_name = username if username else first_name
                    self._log('success', f'Сессия: {display_name}, Баланс: {balance}', 'balance')
                else:
                    self._log('warning', f'В ответе нет данных пользователя: {response}', 'warning')
            else:
                self._log('error', f'Ошибка при получении данных пользователя: {response}', 'error')
        except Exception as e:
            self._log('error', f'Исключение при получении данных пользователя: {str(e)}', 'error')
            self._log('debug', traceback.format_exc(), 'debug')

    async def _sleep_until_next_mission(self, duration: int) -> None:
        if duration <= 0:
            self._log('info', 'Длительность сна некорректна или равна нулю.', 'info')
            return
        hours, remainder = divmod(duration, 3600)
        minutes, seconds = divmod(remainder, 60)
        self._log('info', f'Сессия засыпает на {int(hours)}ч {int(minutes)}м {int(seconds)}с до следующей миссии.', 'sleep')
        await asyncio.sleep(duration)
        self._log('info', 'Сессия проснулась.', 'sleep')

    async def _process_subscription_mission(self, mission_data: dict) -> None:
        sequence = mission_data.get("sequence")
        status = mission_data.get("status")
        reward = mission_data.get("reward")
        mission_title = mission_data.get("title")
        channel_url = (
            mission_data.get("channel_url")
            or mission_data.get("link")
            or mission_data.get("url")
        )
        if not channel_url and sequence == 1:
            channel_url = "https://t.me/GifTopiaChat"
        if not channel_url and sequence == 0:
            channel_url = "https://t.me/GifTopiaGame"
        if not channel_url and sequence == 12:
            channel_url = "https://t.me/giftopia_giftbot"
        self._log(
            "debug",
            f"Данные миссии: {json.dumps(mission_data, ensure_ascii=False)}",
            "debug"
        )
        if not channel_url:
            self._log(
                "warning",
                f"Не удалось определить ссылку для миссии: {mission_title} (sequence={sequence})",
                "warning"
            )
            return
        if status == "COMPLETED":
            self._log(
                "info",
                f"Миссия уже выполнена: {mission_title}",
                "success"
            )
            return
        self._log(
            "info",
            f"Выполняется переход/подписка по ссылке: {channel_url}",
            "mission"
        )
        try:
            await self.tg_client.join_and_mute_tg_channel(channel_url)
            self._log(
                "success",
                f"Успешно выполнено действие по ссылке: {channel_url}",
                "success"
            )
        except Exception as exc:
            self._log(
                "error",
                f"Ошибка при выполнении действия по ссылке: {channel_url} | {exc}",
                "error"
            )
            return
        completed = await self.complete_mission(completed=True)
        if completed:
            self._log(
                "success",
                f"Миссия выполнена. Награда: {reward}",
                "success"
            )
        else:
            self._log(
                "error",
                f"Не удалось подтвердить выполнение миссии.",
                "error"
            )

async def run_tapper(tg_client: UniversalTelegramClient) -> None:
    bot = TapperBot(tg_client=tg_client)
    await bot.run()
