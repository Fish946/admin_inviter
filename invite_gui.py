# Telegram инвайтер через админку
# Обновлено: Добавлена поддержка множественных сессий и улучшена обработка ошибок
import os
import sys
import json
import asyncio
import sqlite3
import shutil
from datetime import datetime
import pandas as pd
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch
from telethon.errors import SessionPasswordNeededError
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QMessageBox,
    QTabWidget, QTextEdit, QProgressBar, QScrollArea, QCheckBox,
    QInputDialog, QFileDialog
)
from PySide6.QtCore import QThread, Signal

class TelegramWorker(QThread):
    update_log = Signal(str)
    update_progress = Signal(int)
    auth_code_required = Signal()
    password_required = Signal()
    finished_signal = Signal(tuple)
    
    def __init__(self, api_id, api_hash, phone, channel_link, users, users_per_batch=10, batch_delay=300):
        super().__init__()
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.channel_link = channel_link
        self.users = users
        self.users_per_batch = users_per_batch
        self.batch_delay = batch_delay
        self.stop_flag = False
        self.client = None
        self.channel_id = None
        self.auth_code = None
        self.password = None
        self.last_error_message = ""

    async def invite_user(self, client, chat_id, user):
        """Приглашение пользователя"""
        try:
            user_to_add = await client.get_entity(user)
            await client.edit_admin(
                chat_id,
                user_to_add,
                is_admin=True,
                title="Member"
            )
            return True
        except errors.RPCError as e:
            error_message = str(e)
            if "admin rights do not allow you to do this" in error_message:
                self.update_log.emit("❌ У вас недостаточно прав администратора для добавления пользователей")
                self.update_log.emit("🛑 Работа бота остановлена")
                self.stop_flag = True  # Останавливаем бота
                return False
            self.update_log.emit(f"❌ Ошибка при инвайте пользователя {user}: {error_message}")
            return False

    async def get_participant_usernames(self):
        """Получение списка текущих подписчиков канала"""
        try:
            self.update_log.emit("📋 Получение списка текущих подписчиков...")
            all_participants = []
            offset = 0
            limit = 100
            
            while True:
                participants = await self.client(GetParticipantsRequest(
                    self.channel_id,
                    ChannelParticipantsSearch(''),
                    offset=offset,
                    limit=limit,
                    hash=0
                ))
                
                if not participants.users:
                    break
                    
                all_participants.extend([
                    participant.username.lower() if participant.username else str(participant.id)
                    for participant in participants.users
                ])
                
                offset += len(participants.users)
                
                if len(participants.users) < limit:
                    break
            
            self.update_log.emit(f"✅ Найдено {len(all_participants)} подписчиков")
            return all_participants
            
        except Exception as e:
            self.update_log.emit(f"❌ Ошибка при получении списка подписчиков: {str(e)}")
            return []

    async def get_channel_id(self, channel_link):
        """Получение ID канала из ссылки"""
        try:
            # Очищаем ссылку от лишнего
            if channel_link.startswith('https://t.me/'):
                channel_link = channel_link[13:]
            elif channel_link.startswith('@'):
                channel_link = channel_link[1:]
            elif channel_link.startswith('t.me/'):
                channel_link = channel_link[5:]
            
            # Получаем информацию о канале
            channel = await self.client.get_entity(channel_link)
            return channel.id
        except Exception as e:
            self.update_log.emit(f"❌ Ошибка при получении ID канала: {str(e)}")
            return None

    async def connect_and_get_channel(self, channel_link):
        """Подключение к Telegram и получение ID канала"""
        if not await self.connect_client():
            return None
            
        channel_id = await self.get_channel_id(channel_link)
        if channel_id:
            # Преобразуем ID в формат, который требует Telegram
            if channel_id > 0:
                channel_id = int(f"-100{channel_id}")
            self.update_log.emit(f"✅ ID канала получен успешно: {channel_id}")
            return channel_id
        return None

    async def bulk_invite(self):
        # Сначала получаем ID канала
        self.channel_id = await self.connect_and_get_channel(self.channel_link)
        if not self.channel_id:
            self.update_log.emit("❌ Не удалось получить ID канала")
            return 0, 0

        # Получаем список текущих подписчиков
        existing_participants = await self.get_participant_usernames()
        
        successful = 0
        failed = 0
        skipped = 0
        total_users = len(self.users)

        for i, user in enumerate(self.users):
            if self.stop_flag:
                self.update_log.emit("🛑 Процесс остановлен пользователем")
                break

            try:
                # Проверяем, является ли пользователь уже подписчиком
                user_id = user.replace('@', '').lower() if isinstance(user, str) else str(user)
                if user_id in existing_participants:
                    self.update_log.emit(f"⏭️ Пользователь {user} уже подписан на канал")
                    skipped += 1
                    continue

                result = await self.invite_user(self.client, self.channel_id, user)
                if not result:
                    # Проверяем, была ли это ошибка недавней авторизации
                    if "недавно авторизован" in self.last_error_message:
                        self.update_log.emit("🛑 Остановка процесса из-за ограничения недавней авторизации")
                        break  # Прерываем цикл
                    failed += 1
                else:
                    successful += 1

                # Обновляем прогресс
                progress = int((i + 1) / total_users * 100)
                self.update_progress.emit(progress)

                # Пауза между пользователями
                await asyncio.sleep(4)

                # Пауза после каждой партии
                if (i + 1) % self.users_per_batch == 0 and i + 1 < total_users:
                    self.update_log.emit(f"⏳ Пауза на {self.batch_delay} секунд...")
                    await asyncio.sleep(self.batch_delay)

            except Exception as e:
                self.update_log.emit(f"❌ Ошибка: {str(e)}")
                failed += 1

        self.update_log.emit(f"📊 Статистика:\n"
                            f"✅ Успешно добавлено: {successful}\n"
                            f"⏭️ Пропущено (уже подписаны): {skipped}\n"
                            f"❌ Ошибок: {failed}")
        return successful, failed

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        success = 0
        failed = 0
        try:
            success, failed = self.loop.run_until_complete(self.bulk_invite())
            if self.client and self.client.is_connected():
                try:
                    self.loop.run_until_complete(self.client.disconnect())
                except:
                    pass
        except Exception as e:
            self.update_log.emit(f"❌ Ошибка: {str(e)}")
        finally:
            try:
                self.loop.stop()
                self.loop.close()
            except:
                pass
            self.finished_signal.emit((success, failed))

    async def connect_client(self):
        """Подключение к клиенту Telegram"""
        try:
            self.update_log.emit("🔄 Подключение к Telegram...")
            
            # Создаем клиента
            session_file = os.path.join('sessions', self.phone)
            self.client = TelegramClient(session_file, self.api_id, self.api_hash)
            
            # Подключаемся
            await self.client.connect()
            
            # Проверяем авторизацию
            if not await self.client.is_user_authorized():
                self.update_log.emit("❌ Сессия не авторизована")
                return False
                
            self.update_log.emit("✅ Успешное подключение к Telegram")
            return True
            
        except Exception as e:
            self.update_log.emit(f"❌ Ошибка при подключении: {str(e)}")
            return False

class UserDatabase:
    def __init__(self):
        self.db_path = os.path.join('data', 'users.db')
        self.create_database()
    
    def create_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                status TEXT,
                last_update TIMESTAMP,
                channel TEXT,
                notes TEXT
            )
        ''')
        conn.commit()
        conn.close()
    
    def add_user(self, username):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO users (username, status) VALUES (?, ?)',
                      (username, 'pending'))
        conn.commit()
        conn.close()
    
    def update_status(self, username, status, notes=''):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET status = ?, last_update = CURRENT_TIMESTAMP, notes = ?
            WHERE username = ?
        ''', (status, notes, username))
        conn.commit()
        conn.close()
    
    def get_pending_users(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT username FROM users WHERE status = "pending"')
        users = [row[0] for row in cursor.fetchall()]
        conn.close()
        return users

class CheckAccountsWorker(QThread):
    log_signal = Signal(str)
    show_dialog_signal = Signal(str, str)
    
    def __init__(self, accounts, parent=None):
        super().__init__(parent)
        self.accounts = accounts
        
    def run(self):
        import asyncio
        
        # Создаем новый event loop для этого потока
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        for account in self.accounts:
            self.log_signal.emit(f"\nПроверка аккаунта {account}...")
            
            try:
                session_file = os.path.join('sessions', account)
                config_file = os.path.join('configs', f"{account}.json")
                
                # Проверяем существование файлов
                if not os.path.exists(config_file):
                    self.log_signal.emit(f"❌ Не найден конфиг для {account}")
                    continue
                    
                # Загружаем API данные
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    if 'telegram_api' in config:
                        api_data = config['telegram_api']
                        api_id = api_data.get('api_id')
                        api_hash = api_data.get('api_hash')
                    else:
                        api_id = config.get('app_id')
                        api_hash = config.get('app_hash')
                
                self.log_signal.emit(f"📱 Найдены данные API - ID: {api_id}, Hash: {api_hash}")
                
                # Создаем и подключаем клиент
                client = TelegramClient(session_file, api_id, api_hash, loop=loop)
                
                # Запускаем проверку в event loop
                loop.run_until_complete(self._check_account(client, account))
                
            except Exception as e:
                self.log_signal.emit(f"❌ Ошибка при проверке: {str(e)}")
                
        self.log_signal.emit("\nПроверка завершена!")
        loop.close()
    
    async def _check_account(self, client, account):
        """Асинхронная проверка одного аккаунта"""
        try:
            await client.connect()
            
            if not await client.is_user_authorized():
                self.log_signal.emit("❌ Аккаунт не авторизован")
                self.show_dialog_signal.emit(account, "не авторизован")
            else:
                self.log_signal.emit("✅ Аккаунт авторизован")
                try:
                    # Проверяем статус через SpamBot
                    spam_bot = await client.get_entity('SpamBot')
                    await client.send_message(spam_bot, '/start')
                    await asyncio.sleep(2)  # Ждем ответ
                    messages = await client.get_messages(spam_bot, limit=1)
                    
                    if messages and messages[0]:
                        status = messages[0].message
                        self.log_signal.emit(f"📝 Статус от @SpamBot: {status}")
                except Exception as e:
                    self.log_signal.emit(f"⚠️ Не удалось получить статус: {str(e)}")
                    
        finally:
            await client.disconnect()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Telegram Инвайт через Админку")
        self.setMinimumSize(800, 600)
        self.worker = None
        self.sessions = []
        
        # Создаем директории если их нет
        os.makedirs('sessions', exist_ok=True)
        os.makedirs('configs', exist_ok=True)
        
        # Перемещаем файлы сессий в правильную папку
        for file in os.listdir():
            if file.endswith('.session') or file.endswith('.session-journal'):
                try:
                    src = file
                    dst = os.path.join('sessions', file)
                    if os.path.exists(src):
                        if os.path.exists(dst):
                            os.remove(src)  # Если файл уже есть в sessions, удаляем из корня
                        else:
                            shutil.move(src, dst)  # Иначе перемещаем
                except Exception as e:
                    print(f"Ошибка при перемещении файла {file}: {e}")
        
        # Создаем центральный виджет с вкладками
        self.tab_widget = QTabWidget()
        self.setCentralWidget(self.tab_widget)
        
        # Создаем вкладки
        self.invite_tab = QWidget()
        self.check_tab = QWidget()
        
        # Настраиваем вкладки (это создаст self.log_text)
        self.setup_invite_tab()
        self.setup_check_tab()
        
        # Добавляем вкладки
        self.tab_widget.addTab(self.invite_tab, "Инвайт")
        self.tab_widget.addTab(self.check_tab, "Проверка аккаунтов")
        
        # Теперь можно создавать директории, так как log_text уже существует
        self.create_directories()
        
        # Загружаем список сессий
        self.load_sessions()
        
        # Если есть сессии, загружаем настройки первой сессии
        if self.sessions:
            first_session = self.sessions[0]
            self.session_combo.setCurrentText(first_session)
            self.on_session_changed(first_session)

    def create_directories(self):
        """Создание необходимых директорий"""
        directories = [
            'sessions',
            'configs',
            'temp',
            'logs',
            'data'
        ]
        for directory in directories:
            os.makedirs(directory, exist_ok=True)
        
        # Очищаем корневую директорию от файлов, которые должны быть в других папках
        self.cleanup_root_directory()
        
        if hasattr(self, 'log_text'):
            self.log_message("✅ Структура папок проверена/создана")

    def cleanup_root_directory(self):
        """Перемещение файлов из корневой директории в соответствующие папки"""
        try:
            # Перемещаем JSON файлы в configs
            for file in os.listdir():
                if file.endswith('.json'):
                    src = file
                    dst = os.path.join('configs', file)
                    if os.path.exists(src):
                        # Если файл уже существует в папке configs, удаляем его из корневой директории
                        if os.path.exists(dst):
                            os.remove(src)
                        else:
                            os.rename(src, dst)
        except Exception as e:
            if hasattr(self, 'log_text'):
                self.log_message(f"❌ Ошибка при очистке корневой директории: {str(e)}")

    def move_session_files(self):
        """Перемещение всех файлов сессий в папку sessions"""
        try:
            # Ищем все файлы сессий в корневой директории
            session_files = [f for f in os.listdir() if f.endswith('.session') or f.endswith('.session-journal')]
            
            for file in session_files:
                try:
                    source = file
                    destination = os.path.join('sessions', file)
                    
                    # Если файл уже существует в папке sessions, удаляем старый
                    if os.path.exists(destination):
                        os.remove(source)
                    else:
                        # Иначе перемещаем файл
                        shutil.move(source, destination)
                        if hasattr(self, 'log_text'):
                            self.log_text.append(f"✅ Файл {file} перемещен в папку sessions")
                except Exception as e:
                    if hasattr(self, 'log_text'):
                        self.log_text.append(f"❌ Ошибка при перемещении файла {file}: {str(e)}")
        except Exception as e:
            if hasattr(self, 'log_text'):
                self.log_text.append(f"❌ Ошибка при поиске файлов сессий: {str(e)}")

    def create_client(self):
        """Создание клиента Telegram"""
        try:
            phone = self.session_combo.currentText()
            # Проверяем наличие файла сессии в обоих местах
            root_session = f"{phone}.session"
            sessions_dir_file = os.path.join('sessions', f"{phone}.session")
            
            self.log_text.append(f"🔍 Поиск файла сессии для {phone}")
            
            if os.path.exists(root_session):
                self.log_text.append(f"📁 Найден файл сессии в корневой папке: {root_session}")
                # Перемещаем файл в папку sessions
                if not os.path.exists('sessions'):
                    os.makedirs('sessions')
                shutil.move(root_session, sessions_dir_file)
                self.log_text.append("✅ Файл сессии перемещен в папку sessions")
            
            if os.path.exists(sessions_dir_file):
                self.log_text.append(f"📁 Найден файл сессии в папке sessions: {sessions_dir_file}")
            else:
                self.log_text.append("❌ Файл сессии не найден!")
                return False
            
            # Проверяем размер файла сессии
            session_size = os.path.getsize(sessions_dir_file)
            self.log_text.append(f"📊 Размер файла сессии: {session_size} байт")
            
            api_id = int(self.api_id_input.text().strip())
            api_hash = self.api_hash_input.text().strip()
            
            self.log_text.append(f"🔑 Создание клиента с API ID: {api_id}")
            
            # Создаем клиента с путем к файлу сессии в папке sessions
            session_file = os.path.join('sessions', phone)
            self.client = TelegramClient(session_file, api_id, api_hash)
            
            return True
        except Exception as e:
            self.log_text.append(f"❌ Ошибка при создании клиента: {str(e)}")
            return False

    def setup_invite_tab(self):
        """Настройка вкладки инвайта"""
        layout = QVBoxLayout(self.invite_tab)
        
        # Выбор сессии
        session_layout = QHBoxLayout()
        self.session_combo = QComboBox()
        self.session_combo.addItems(sorted(self.sessions))
        self.session_combo.setEditable(False)
        self.session_combo.setPlaceholderText("Выберите аккаунт")
        self.session_combo.currentTextChanged.connect(self.on_session_changed)
        
        delete_session_btn = QPushButton("Удалить сессию")
        delete_session_btn.clicked.connect(self.delete_session)
        
        refresh_sessions_btn = QPushButton("Обновить список")
        refresh_sessions_btn.clicked.connect(self.refresh_sessions)
        
        session_layout.addWidget(QLabel("Активный аккаунт:"))
        session_layout.addWidget(self.session_combo)
        session_layout.addWidget(delete_session_btn)
        session_layout.addWidget(refresh_sessions_btn)
        layout.addLayout(session_layout)

        # Настройки
        settings_layout = QVBoxLayout()
        
        api_layout = QHBoxLayout()
        self.api_id_input = QLineEdit()
        self.api_hash_input = QLineEdit()
        api_layout.addWidget(QLabel("API ID:"))
        api_layout.addWidget(self.api_id_input)
        api_layout.addWidget(QLabel("API Hash:"))
        api_layout.addWidget(self.api_hash_input)
        settings_layout.addLayout(api_layout)

        phone_layout = QHBoxLayout()
        self.phone_input = QLineEdit()
        phone_layout.addWidget(QLabel("Телефон:"))
        phone_layout.addWidget(self.phone_input)
        settings_layout.addLayout(phone_layout)

        channel_layout = QHBoxLayout()
        self.channel_input = QLineEdit()
        self.channel_input.setPlaceholderText("Например: https://t.me/channel или @channel")
        channel_layout.addWidget(QLabel("Ссылка на канал:"))
        channel_layout.addWidget(self.channel_input)
        settings_layout.addLayout(channel_layout)

        layout.addLayout(settings_layout)

        # Список пользователей
        users_header_layout = QHBoxLayout()
        users_header_layout.addWidget(QLabel("Список пользователей:"))
        
        load_db_button = QPushButton("Загрузить из БД")
        load_db_button.clicked.connect(self.load_users_from_db)
        users_header_layout.addWidget(load_db_button)
        
        layout.addLayout(users_header_layout)

        self.users_input = QTextEdit()
        self.users_input.setPlaceholderText("Введите список пользователей (по одному на строку)")
        layout.addWidget(self.users_input)

        # Прогресс
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        # Лог
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(QLabel("Лог:"))
        layout.addWidget(self.log_text)

        # Кнопки управления
        buttons_layout = QHBoxLayout()
        self.start_button = QPushButton("Начать")
        self.stop_button = QPushButton("Остановить")
        self.save_button = QPushButton("Сохранить настройки")
        self.stop_button.setEnabled(False)
        
        self.start_button.clicked.connect(self.start_invite)
        self.stop_button.clicked.connect(self.stop_invite)
        self.save_button.clicked.connect(self.save_config)
        
        buttons_layout.addWidget(self.start_button)
        buttons_layout.addWidget(self.stop_button)
        buttons_layout.addWidget(self.save_button)
        layout.addLayout(buttons_layout)

    def setup_check_tab(self):
        """Настройка вкладки проверки аккаунтов"""
        layout = QVBoxLayout(self.check_tab)
        
        # Создаем область прокрутки для списка аккаунтов
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        
        # Создаем чекбоксы для каждого аккаунта
        self.account_checkboxes = {}
        for session in self.sessions:
            checkbox = QCheckBox(session)
            self.account_checkboxes[session] = checkbox
            scroll_layout.addWidget(checkbox)
        
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        
        # Кнопки управления
        buttons_layout = QHBoxLayout()
        
        check_selected_btn = QPushButton("Проверить выбранные")
        check_all_btn = QPushButton("Проверить все")
        refresh_accounts_btn = QPushButton("Обновить список")
        
        check_selected_btn.clicked.connect(self.check_selected_accounts)
        check_all_btn.clicked.connect(self.check_all_accounts)
        refresh_accounts_btn.clicked.connect(self.refresh_account_checkboxes)
        
        buttons_layout.addWidget(check_selected_btn)
        buttons_layout.addWidget(check_all_btn)
        buttons_layout.addWidget(refresh_accounts_btn)
        layout.addLayout(buttons_layout)
        
        # Лог проверки
        self.check_log = QTextEdit()
        self.check_log.setReadOnly(True)
        layout.addWidget(QLabel("Результаты проверки:"))
        layout.addWidget(self.check_log)

    def refresh_account_checkboxes(self):
        """Обновление списка чекбоксов аккаунтов"""
        # Очищаем старые чекбоксы
        for checkbox in self.account_checkboxes.values():
            checkbox.deleteLater()
        self.account_checkboxes.clear()
        
        # Создаем новые чекбоксы
        scroll = self.check_tab.findChild(QScrollArea)
        scroll_content = scroll.widget()
        scroll_layout = scroll_content.layout()
        
        for session in self.sessions:
            checkbox = QCheckBox(session)
            self.account_checkboxes[session] = checkbox
            scroll_layout.addWidget(checkbox)

    def check_selected_accounts(self):
        """Проверка выбранных аккаунтов"""
        selected_accounts = [session for session, checkbox in self.account_checkboxes.items() 
                           if checkbox.isChecked()]
        if not selected_accounts:
            QMessageBox.warning(self, "Предупреждение", "Выберите хотя бы один аккаунт")
            return
        
        self.check_accounts(selected_accounts)

    def check_all_accounts(self):
        """Проверка всех аккаунтов"""
        self.check_accounts(self.sessions)

    def check_accounts(self, accounts=None):
        """Проверка аккаунтов"""
        if accounts is None:
            # Получаем выбранные аккаунты
            accounts = [account for account, checkbox in self.account_checkboxes.items() 
                       if checkbox.isChecked()]
            if not accounts:
                self.check_log.append("❌ Не выбрано ни одного аккаунта")
                return
            
        self.check_log.clear()
        self.check_log.append("Начинаем проверку аккаунтов...")
        
        # Создаем и запускаем worker
        self.check_worker = CheckAccountsWorker(accounts)
        self.check_worker.log_signal.connect(self.check_log.append)
        self.check_worker.show_dialog_signal.connect(self.show_restore_dialog)
        self.check_worker.start()

    def show_restore_dialog(self, account, message):
        """Показывает диалог восстановления сессии"""
        msg_box = QMessageBox()
        msg_box.setWindowTitle('Восстановление сессии')
        msg_box.setText(f'Аккаунт {account} {message}. Что вы хотите сделать?')
        
        restore_button = msg_box.addButton('Восстановить', QMessageBox.ActionRole)
        delete_button = msg_box.addButton('Удалить сессию', QMessageBox.ActionRole)
        skip_button = msg_box.addButton('Пропустить', QMessageBox.RejectRole)
        
        msg_box.exec_()
        
        if msg_box.clickedButton() == restore_button:
            self.check_log.append(f"🔄 Начинаем восстановление сессии {account}...")
            self.restore_session(account)
        elif msg_box.clickedButton() == delete_button:
            self.check_log.append(f"🗑️ Удаляем сессию {account}...")
            self.delete_session_files(account)

    def restore_session(self, account):
        """Восстановление сессии"""
        self.check_log.append(f"\nНачинаем восстановление сессии {account}...")
        
        try:
            session_file = os.path.join('sessions', account)
            config_file = os.path.join('configs', f"{account}.json")
            
            # Загружаем API данные
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                if 'telegram_api' in config:
                    api_data = config['telegram_api']
                    api_id = api_data.get('api_id')
                    api_hash = api_data.get('api_hash')
                else:
                    api_id = config.get('app_id')
                    api_hash = config.get('app_hash')
            
            # Форматируем номер телефона
            phone = account
            if not phone.startswith('+'):
                phone = '+' + phone
            
            self.check_log.append(f"📱 Используем номер: {phone}")
            
            # Создаем клиент в синхронном режиме
            client = TelegramClient(session_file, api_id, api_hash)
            
            try:
                client.connect()
                
                # Проверяем, не авторизован ли уже клиент
                if client.is_user_authorized():
                    self.check_log.append("✅ Клиент уже авторизован!")
                    return
                    
                # Отправляем запрос на код
                self.check_log.append("📤 Отправляем запрос на код в Telegram...")
                send_code_result = client.send_code_request(
                    phone,
                    force_sms=False  # Принудительно отключаем SMS
                )
                
                # Запрашиваем код у пользователя
                code, ok = QInputDialog.getText(
                    self,
                    'Введите код',
                    f'Введите код, отправленный в Telegram для {phone}:'
                )
                
                if ok and code:
                    try:
                        self.check_log.append("🔄 Пытаемся войти с полученным кодом...")
                        client.sign_in(
                            phone=phone,
                            code=code,
                            phone_code_hash=send_code_result.phone_code_hash
                        )
                        self.check_log.append("✅ Авторизация успешно восстановлена!")
                        
                    except SessionPasswordNeededError:
                        self.check_log.append("🔐 Требуется двухфакторная аутентификация...")
                        password, ok = QInputDialog.getText(
                            self,
                            'Двухфакторная аутентификация',
                            'Введите пароль двухфакторной аутентификации:',
                            QLineEdit.Password
                        )
                        
                        if ok and password:
                            client.sign_in(password=password)
                            self.check_log.append("✅ Авторизация успешно восстановлена!")
                        else:
                            self.check_log.append("❌ Отменено пользователем")
                    
                    except Exception as e:
                        self.check_log.append(f"❌ Ошибка при вводе кода: {str(e)}")
                else:
                    self.check_log.append("❌ Отменено пользователем")
                    
            finally:
                client.disconnect()
                
        except Exception as e:
            self.check_log.append(f"❌ Ошибка при восстановлении сессии: {str(e)}")
            # Выводим полный traceback для отладки
            import traceback
            self.check_log.append(f"Детали ошибки:\n{traceback.format_exc()}")

    def delete_session_files(self, account):
        """Удаление файлов сессии"""
        try:
            session_path = os.path.join('sessions', f"{account}.session")
            journal_path = os.path.join('sessions', f"{account}.session-journal")
            config_path = os.path.join('configs', f"{account}.json")
            
            if os.path.exists(session_path):
                os.remove(session_path)
            if os.path.exists(journal_path):
                os.remove(journal_path)
            if os.path.exists(config_path):
                os.remove(config_path)
            
            self.check_log.append(f"🗑️ Сессия {account} успешно удалена")
            
            # Обновляем список сессий
            self.refresh_sessions()
            self.refresh_account_checkboxes()
            
        except Exception as e:
            self.check_log.append(f"❌ Ошибка при удалении сессии: {str(e)}")

    def load_sessions(self):
        """Загрузка списка существующих сессий"""
        # Ищем все файлы .session в папке sessions
        session_files = [f for f in os.listdir('sessions') 
                        if f.endswith('.session') and not f.endswith('.session-journal')]
        self.sessions = []
        
        for session_file in session_files:
            # Извлекаем номер телефона из имени файла
            phone = session_file.replace('.session', '')
            self.sessions.append(phone)
        
        # Очищаем временные файлы
        self.cleanup_temp_files()
        
        # Обновляем комбобокс
        self.session_combo.clear()
        self.session_combo.addItems(sorted(self.sessions))
        
        self.log_message(f"📱 Найдено сессий: {len(self.sessions)}")

    def cleanup_temp_files(self):
        """Очистка временных файлов"""
        try:
            # Удаляем все файлы из папки temp
            for file in os.listdir('temp'):
                file_path = os.path.join('temp', file)
                try:
                    if os.path.isfile(file_path):
                        os.unlink(file_path)
                except Exception as e:
                    self.log_message(f"❌ Ошибка при удалении временного файла {file}: {str(e)}")
        except Exception as e:
            self.log_message(f"❌ Ошибка при очистке временных файлов: {str(e)}")

    def log_message(self, message):
        self.log_text.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def read_session_info(self, session_file):
        """Чтение информации из файла конфигурации сессии"""
        try:
            config_file = os.path.join('configs', session_file)
            with open(config_file, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
                api_data = config_data.get('telegram_api', {})
                return {
                    'api_id': api_data.get('api_id', ''),
                    'api_hash': api_data.get('api_hash', ''),
                    'phone': api_data.get('phone', '')
                }
        except Exception as e:
            self.log_message(f"❌ Ошибка при чтении файла конфигурации: {str(e)}")
            return None

    def on_session_changed(self, session):
        """Обработчик смены сессии"""
        if session:
            # Сначала перемещаем файлы сессии если они в корневой папке
            session_file = f"{session}.session"
            journal_file = f"{session}.session-journal"
            
            if os.path.exists(session_file):
                try:
                    shutil.move(session_file, os.path.join('sessions', session_file))
                    self.log_text.append(f"✅ Файл сессии {session_file} перемещен в папку sessions")
                except Exception as e:
                    self.log_text.append(f"❌ Ошибка при перемещении файла сессии: {str(e)}")
            
            if os.path.exists(journal_file):
                try:
                    shutil.move(journal_file, os.path.join('sessions', journal_file))
                except Exception as e:
                    self.log_text.append(f"❌ Ошибка при перемещении файла журнала: {str(e)}")
            
            # Загружаем конфигурацию
            config_file = os.path.join('configs', f"{session}.json")
            self.log_text.append(f"🔄 Загрузка настроек из файла {session}.json")
            
            try:
                if not os.path.exists(config_file):
                    self.log_text.append(f"❌ Файл {session}.json не существует")
                    return
                    
                with open(config_file, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    
                    # Пробуем сначала новый формат
                    api_data = config_data.get('telegram_api', {})
                    
                    if api_data:
                        api_id = str(api_data.get('api_id', ''))
                        api_hash = api_data.get('api_hash', '')
                    else:
                        # Если нет секции telegram_api, пробуем старый формат
                        api_id = str(config_data.get('app_id', ''))
                        api_hash = config_data.get('app_hash', '')
                    
                    if api_id and api_hash:
                        self.log_text.append(f"📱 Найдены данные API - ID: {api_id}, Hash: {api_hash}")
                        
                        self.api_id_input.setText(api_id)
                        self.api_hash_input.setText(api_hash)
                        self.phone_input.setText(session)
                        
                        self.log_text.append(f"✅ Успешно загружены настройки для сессии {session}")
                    else:
                        self.log_text.append("❌ Не найдены API ID и Hash в файле конфигурации")
                    
            except Exception as e:
                self.log_text.append(f"❌ Ошибка при загрузке настроек: {str(e)}")

    def delete_session(self):
        """Удаление выбранной сессии"""
        current_session = self.session_combo.currentText()
        if current_session:
            reply = QMessageBox.question(
                self, 
                'Подтверждение', 
                f'Вы уверены, что хотите удалить сессию {current_session}?',
                QMessageBox.Yes | QMessageBox.No, 
                QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                try:
                    # Удаляем файл конфигурации сессии
                    config_file = f"{current_session}.json"
                    if os.path.exists(config_file):
                        os.remove(config_file)
                    
                    self.log_message(f"✅ Сессия {current_session} удалена")
                    self.refresh_sessions()
                except Exception as e:
                    self.log_message(f"❌ Ошибка при удалении сессии: {str(e)}")

    def start_invite(self):
        try:
            api_id = int(self.api_id_input.text().strip())
            api_hash = self.api_hash_input.text().strip()
            phone = self.phone_input.text().strip()
            channel_link = self.channel_input.text().strip()
            users = [u.strip() for u in self.users_input.toPlainText().split('\n') if u.strip()]

            if not all([api_id, api_hash, phone, channel_link, users]):
                QMessageBox.warning(self, "Ошибка", "Пожалуйста, заполните все поля")
                return

            # Сохраняем настройки перед началом работы
            self.save_config()

            # Создаем и проверяем клиента перед созданием worker'а
            session_file = os.path.join('sessions', phone)
            try:
                client = TelegramClient(session_file, api_id, api_hash)
                # Пробуем подключиться для проверки
                client.connect()
                if not client.is_user_authorized():
                    QMessageBox.warning(self, "Ошибка", "Сессия не авторизована. Пожалуйста, проверьте файл сессии.")
                    client.disconnect()
                    return
                client.disconnect()
                self.log_text.append("✅ Проверка сессии успешна")
            except Exception as e:
                self.log_text.append(f"❌ Ошибка при проверке сессии: {str(e)}")
                QMessageBox.warning(self, "Ошибка", f"Не удалось создать клиент Telegram: {str(e)}")
                return

            # Если проверка прошла успешно, создаем worker
            self.worker = TelegramWorker(
                api_id=api_id,
                api_hash=api_hash,
                phone=phone,
                channel_link=channel_link,
                users=users,
                users_per_batch=10,
                batch_delay=300
            )
            
            self.worker.update_log.connect(self.log_message)
            self.worker.update_progress.connect(self.progress_bar.setValue)
            self.worker.auth_code_required.connect(self.request_auth_code)
            self.worker.password_required.connect(self.request_password)
            self.worker.finished_signal.connect(self.on_invite_finished)
            
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.progress_bar.setValue(0)
            self.worker.start()

        except ValueError as e:
            QMessageBox.warning(self, "Ошибка", "Проверьте правильность введенных данных")
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", f"Произошла ошибка: {str(e)}")

    def stop_invite(self):
        if self.worker:
            self.worker.stop()
            self.stop_button.setEnabled(False)
            # Ждем завершения потока
            self.worker.wait()

    def request_auth_code(self):
        code, ok = QInputDialog.getText(
            self,
            "Подтверждение",
            "Введите код подтверждения из Telegram:",
            QLineEdit.EchoMode.Normal
        )
        if ok and code:
            self.worker.set_auth_code(code)

    def request_password(self):
        password, ok = QInputDialog.getText(
            self,
            "Двухфакторная аутентификация",
            "Введите пароль двухфакторной аутентификации:",
            QLineEdit.EchoMode.Password
        )
        if ok and password:
            self.worker.set_password(password)

    def on_invite_finished(self, results):
        success, failed = results
        self.log_message(f"✨ Процесс завершен! Успешно: {success}, Неудачно: {failed}")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def closeEvent(self, event):
        """Обработчик закрытия окна"""
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()
        event.accept()

    def refresh_sessions(self):
        """Обновление списка сессий"""
        current = self.session_combo.currentText()
        
        # Обновляем список сессий
        self.load_sessions()
        
        # Обновляем комбобокс
        self.session_combo.clear()
        self.session_combo.addItems(sorted(self.sessions))
        
        # Восстанавливаем выбранную сессию, если она все еще существует
        if current in self.sessions:
            self.session_combo.setCurrentText(current)
        elif self.sessions:
            # Если предыдущей сессии нет, но есть другие - выбираем первую
            self.session_combo.setCurrentText(self.sessions[0])
            
        self.log_message(f"🔄 Список сессий обновлен. Найдено: {len(self.sessions)}")

    def load_users_from_file(self):
        try:
            with open('users.txt', 'r', encoding='utf-8') as f:
                users = f.read()
            self.users_input.setText(users)
            self.log_message("✅ Список пользователей успешно загружен из файла users.txt")
        except FileNotFoundError:
            self.log_message("❌ Файл users.txt не найден")
        except Exception as e:
            self.log_message(f"❌ Ошибка при загрузке списка пользователей: {str(e)}")

    def save_users_to_file(self):
        try:
            users = self.users_input.toPlainText()
            with open('users.txt', 'w', encoding='utf-8') as f:
                f.write(users)
            self.log_message("✅ Список пользователей сохранен в файл users.txt")
        except Exception as e:
            self.log_message(f"❌ Ошибка при сохранении списка пользователей: {str(e)}")

    def load_config(self):
        try:
            # Загружаем общие настройки
            with open('config.json', 'r', encoding='utf-8') as f:
                self.config = json.load(f)
            
            # Загружаем настройки для текущей сессии
            current_session = self.session_combo.currentText()
            if current_session:
                config_file = f"{current_session}.json"
                if os.path.exists(config_file):
                    try:
                        with open(config_file, 'r', encoding='utf-8') as f:
                            session_config = json.load(f)
                            
                            # Загружаем настройки API для этой сессии
                            api_settings = session_config.get('telegram_api', {})
                            self.api_id_input.setText(str(api_settings.get('api_id', '')))
                            self.api_hash_input.setText(api_settings.get('api_hash', ''))
                            self.phone_input.setText(current_session)  # Используем номер из имени файла
                            
                            # Загружаем настройки канала
                            channel_settings = session_config.get('channel_settings', {})
                            self.channel_input.setText(str(channel_settings.get('channel_link', '')))
                            
                            self.log_message(f"✅ Загружены настройки для сессии {current_session}")
                    except Exception as e:
                        self.log_message(f"❌ Ошибка при загрузке настроек сессии: {str(e)}")
                else:
                    self.log_message(f"❌ Файл настроек {config_file} не найден")
                    # Очищаем поля, так как настройки не найдены
                    self.api_id_input.clear()
                    self.api_hash_input.clear()
                    self.phone_input.setText(current_session)
                    self.channel_input.clear()
            
            # Пробуем загрузить пользователей из отдельного файла
            self.load_users_from_file()
                
        except FileNotFoundError:
            self.log_message("Файл конфигурации не найден. Будет создан новый при сохранении.")
            self.config = {}
        except Exception as e:
            self.log_message(f"Ошибка при загрузке конфигурации: {str(e)}")
            self.config = {}

    def save_config(self):
        try:
            # Сохраняем общие настройки
            config = {
                'invite_settings': {
                    'users_per_batch': 10,
                    'batch_delay': 300,
                    'user_delay': 4,
                    'max_retries': 3
                }
            }
            
            # Сохраняем основной конфиг сразу в папку configs
            config_path = os.path.join('configs', 'config.json')
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            
            # Сохраняем настройки для текущей сессии
            phone = self.phone_input.text()
            if phone:
                api_id = self.api_id_input.text().strip()
                api_hash = self.api_hash_input.text().strip()
                channel_link = self.channel_input.text().strip()
                
                self.log_message(f"💾 Сохранение настроек для сессии {phone}")
                
                session_config = {
                    'telegram_api': {
                        'api_id': api_id,
                        'api_hash': api_hash,
                        'phone': phone
                    },
                    'channel_settings': {
                        'channel_link': channel_link
                    }
                }
                
                # Сохраняем файл сессии сразу в папку configs
                session_config_file = os.path.join('configs', f"{phone}.json")
                with open(session_config_file, 'w', encoding='utf-8') as f:
                    json.dump(session_config, f, indent=4, ensure_ascii=False)
                
                self.log_message(f"✅ Настройки сохранены в файл {session_config_file}")
        
        except Exception as e:
            self.log_message(f"❌ Ошибка при сохранении настроек: {str(e)}")

    def import_from_excel(self, file_path):
        try:
            df = pd.read_excel(file_path)
            if 'username' not in df.columns:
                raise ValueError("Excel файл должен содержать колонку 'username'")
            
            for username in df['username']:
                self.db.add_user(str(username).strip())
            
            self.log_message(f"✅ Импортировано {len(df)} пользователей из Excel")
            return True
        except Exception as e:
            self.log_message(f"❌ Ошибка при импорте из Excel: {str(e)}")
            return False

    def export_to_excel(self):
        try:
            conn = sqlite3.connect(self.db.db_path)
            df = pd.read_sql_query('SELECT * FROM users', conn)
            conn.close()
            
            export_path = os.path.join('data', f'users_export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx')
            df.to_excel(export_path, index=False)
            self.log_message(f"✅ Данные экспортированы в {export_path}")
            return True
        except Exception as e:
            self.log_message(f"❌ Ошибка при экспорте в Excel: {str(e)}")
            return False

    def import_excel(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите Excel файл",
            "",
            "Excel Files (*.xlsx *.xls)"
        )
        if file_path:
            if self.import_from_excel(file_path):
                self.refresh_user_list()

    def refresh_user_list(self):
        users = self.db.get_pending_users()
        self.users_input.setText('\n'.join(users))

    def load_users_from_db(self):
        try:
            # Открываем диалог выбора файла базы данных
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Выберите файл базы данных",
                "",
                "База данных (*.db);;Все файлы (*.*)"
            )
            
            if not file_path:  # Если пользователь отменил выбор
                return
            
            conn = sqlite3.connect(file_path)
            cursor = conn.cursor()
            cursor.execute("SELECT username FROM users WHERE status = 'pending'")
            users = cursor.fetchall()
            
            # Очищаем текущий список
            self.users_input.clear()
            
            # Добавляем пользователей в список
            users_text = []
            for user in users:
                username = user[0].strip('@') if user[0].startswith('@') else user[0]
                users_text.append(username)
            
            # Устанавливаем текст в QTextEdit
            self.users_input.setText('\n'.join(users_text))
            
            self.log_text.append(f"Загружено {len(users)} пользователей из базы данных")
            
        except Exception as e:
            self.log_text.append(f"Ошибка при загрузке из БД: {str(e)}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить данные: {str(e)}")
        finally:
            if 'conn' in locals():
                conn.close()

    def delete_invalid_session(self, account):
        """Удаление неработающей сессии"""
        try:
            session_file = os.path.join('sessions', f"{account}.session")
            journal_file = os.path.join('sessions', f"{account}.session-journal")
            
            if os.path.exists(session_file):
                os.remove(session_file)
            if os.path.exists(journal_file):
                os.remove(journal_file)
            
            self.check_log.append(f"🗑️ Удалена неработающая сессия {account}")
            return True
        except Exception as e:
            self.check_log.append(f"❌ Ошибка при удалении сессии: {str(e)}")
            return False

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())