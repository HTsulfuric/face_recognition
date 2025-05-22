import os
from dotenv import load_dotenv
import cv2
import numpy as np
import face_recognition
import websocket
import threading
import time
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
import queue
from datetime import datetime
# import requests
import logging
from logging import getLogger, config
from logging_handlers import TkinterHandler
import json

# 環境変数の読み込み
load_dotenv()

class AppConfig:
    """アプリケーションの設定を管理するクラス"""
    WS_URL = os.getenv("WS_URL", "ws://localhost:8080")
    # LINE_NOTIFY_TOKEN = os.getenv("LINE_NOTIFY_TOKEN", "") 
    FACES_DIR = os.getenv("FACES_DIR", "./resources/faces")
    FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", 0.5))
    DEFAULT_FPS_SETTING = "1"
    DEFAULT_RESOLUTION = "160x120"
    RESOLUTION_RESEND_INTERVAL_SEC = 5
    SAVE_UNKNOWN_FACES = os.getenv("SAVE_UNKNOWN_FACES", "True").lower() == "true" # 未知の顔を保存するかどうかの設定

class WebSocketClient:
    """WebSocket接続を管理するシングルトンクラス"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, url, on_message, on_error, on_close, on_open, logger):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(WebSocketClient, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, url, on_message, on_error, on_close, on_open, logger):
        if self._initialized:
            if self.url != url:
                logger.warning("WebSocketClientのURLが変更されましたが、シングルトンのため古いインスタンスを再利用します。")
            return

        self.url = url
        self.on_message_callback = on_message
        self.on_error_callback = on_error
        self.on_close_callback = on_close
        self.on_open_callback = on_open
        self.logger = logger
        self.ws = None
        self.thread = None
        self.stop_event = threading.Event()
        self.is_connected = False
        self._initialized = True

    def _on_message(self, ws, message):
        self.on_message_callback(ws, message)

    def _on_error(self, ws, error):
        self.is_connected = False
        self.logger.error(f"WebSocketエラー (WebSocketClient): {error}")
        # GUI更新はAppクラスに任せる
        self.on_error_callback(ws, error)

    def _on_close(self, ws, close_status_code, close_msg):
        self.is_connected = False
        self.logger.warning(f"WebSocket接続が閉じられました。コード: {close_status_code}, メッセージ: {close_msg}")
        # GUI更新はAppクラスに任せる
        self.on_close_callback(ws, close_status_code, close_msg)

    def _on_open(self, ws):
        self.is_connected = True
        self.logger.info("WebSocketに接続しました。 (WebSocketClient._on_open)")
        # GUI更新はAppクラスに任せる
        self.on_open_callback(ws)

    def connect(self):
        if self.ws and self.ws.keep_running:
            self.logger.info("既にWebSocket接続処理が実行中です。")
            if self.is_connected:
                return
            else:
                self.logger.info("以前の接続はあったが、現在未接続のため再接続を試みます。")
                if self.thread and self.thread.is_alive():
                    try:
                        self.stop_event.set() # スレッドに停止を通知
                        self.ws.close()
                        self.thread.join(timeout=2.0)
                        self.stop_event.clear() # 次の接続のためにクリア
                    except Exception as e:
                        self.logger.error(f"既存WebSocketスレッドの終了待機中にエラー: {e}")
        
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )
        self.thread = threading.Thread(target=self.ws.run_forever)
        self.thread.daemon = True
        self.thread.start()
        self.logger.info("WebSocketクライアント接続処理を開始しました。")

    def send(self, message):
        if self.is_connected and self.ws and self.ws.sock and self.ws.sock.connected:
            try:
                self.ws.send(message)
                self.logger.debug(f"WebSocketにメッセージを送信: {message}")
            except Exception as e:
                self.logger.error(f"WebSocket送信エラー: {e}")
                self.is_connected = False
                # GUI更新はAppクラスに任せる
        else:
            self.logger.warning(f"WebSocketが接続されていません。メッセージ '{message}' の送信をスキップします。")

    def close(self):
        self.stop_event.set()
        self.is_connected = False
        if self.ws:
            self.ws.close()
            self.logger.info("WebSocketクライアント接続を閉じました。")
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
            if self.thread.is_alive():
                self.logger.warning("WebSocketスレッドがタイムアウト後も終了していません。")
            else:
                self.logger.info("WebSocketスレッドを終了しました。")
        self.ws = None
        self.thread = None

    def run_manager(self):
        """WebSocketの自動再接続を管理するスレッドのターゲット関数"""
        while not self.stop_event.is_set():
            if not self.is_connected:
                if not (self.ws and self.ws.keep_running):
                    self.logger.warning("WebSocketが切断されました。再接続を試みます...")
                    self.connect()
            time.sleep(5)
        self.logger.info("WebSocketClient run_managerループが終了しました。")


class App:
    """アプリケーションのメインクラス"""
    def __init__(self, root):
        self.root = root
        self.logger = self._setup_logging()

        # Haar Cascadesの読み込み
        self.face_cascade = cv2.CascadeClassifier("resources/models/haarcascade_frontalface_default.xml")
        if self.face_cascade.empty():
            self.logger.critical("Haar Cascades ファイルが見つかりません。アプリケーションを終了します。")
            raise IOError("Haar Cascades ファイルが見つかりません。正しいパスを確認してください。")

        # 顔認証データ
        self.known_face_encodings = []
        self.known_face_names = []
        self._load_known_faces()

        # GUI要素
        self.image_label = None
        self.fps_label = None
        # self.websocket_status_label = None # WebSocketステータス表示を削除
        self.start_button = None
        self.stop_button = None
        self.log_text = None
        self.fps_var = tk.StringVar(value=AppConfig.DEFAULT_FPS_SETTING)
        self.resolution_var = tk.StringVar(value=AppConfig.DEFAULT_RESOLUTION)
        self.save_unknown_faces_var = tk.BooleanVar(value=AppConfig.SAVE_UNKNOWN_FACES) # 未知の顔保存トグルスイッチ

        # 状態変数
        self.is_running = False
        self.latest_frame = None
        self.frame_lock = threading.Lock()
        self.frame_count = 0
        self.start_time = time.time()
        self.current_fps = 0
        self.current_fps_setting = AppConfig.DEFAULT_FPS_SETTING
        self.current_resolution = AppConfig.DEFAULT_RESOLUTION
        self.send_stream_command_on_open = False
        self.last_resolution_resend_time = 0
        self.unknown_face_times = []
        self.detected_counts = {}
        # self.notified_names = set() # LINE Notifyをコメントアウト

        # キュー
        self.log_queue = queue.Queue()
        self.fps_queue = queue.Queue()

        # WebSocketクライアント
        self.websocket_client = WebSocketClient(
            AppConfig.WS_URL,
            self._on_websocket_message,
            self._on_websocket_error,
            self._on_websocket_close,
            self._on_websocket_open,
            self.logger
        )
        self.websocket_manager_thread = None

        self._setup_gui()
        self.root.after(100, self._process_queues)
        self._update_button_states()

    def _setup_logging(self):
        """ロギングを設定する"""
        with open('log_config.json', 'r', encoding='utf-8') as f:
            log_cfg = json.load(f)
        
        # TkinterHandlerはGUI要素が必要なので、ここでは設定しない
        temp_handlers = {}
        if 'consoleHandler' in log_cfg['handlers']:
            temp_handlers['consoleHandler'] = log_cfg['handlers']['consoleHandler']
        if 'fileHandler' in log_cfg['handlers']:
            temp_handlers['fileHandler'] = log_cfg['handlers']['fileHandler']
        
        partial_log_config = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": log_cfg.get('formatters', {}),
            "handlers": temp_handlers,
            "loggers": {
                "__main__": {
                    "level": log_cfg.get('loggers', {}).get('__main__', {}).get('level', 'DEBUG'),
                    "handlers": [h for h in temp_handlers.keys()],
                    "propagate": False
                }
            },
            "root": log_cfg.get('root', {"level": "INFO"})
        }
        logging.config.dictConfig(partial_log_config)
        return getLogger(__name__)

    def _setup_gui(self):
        """GUI要素をセットアップする"""
        self.root.title("ESP32-CAM 顔認証デモ")
        self.root.geometry("1200x800")
        self.root.resizable(True, True)

        try:
            self.root.eval('tk::PlaceWindow . center')
        except:
            pass

        self.root.bind("<Configure>", self._on_resize)

        main_frame = ttk.Frame(self.root, padding="10 10 10 10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        control_panel_frame = ttk.Frame(main_frame, width=300)
        control_panel_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        control_panel_frame.pack_propagate(False)

        video_display_frame = ttk.Frame(main_frame)
        video_display_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        status_frame = ttk.LabelFrame(control_panel_frame, text="ステータス", padding="10")
        status_frame.pack(fill=tk.X, pady=(0, 10))

        self.fps_label = ttk.Label(status_frame, text="現在のFPS: 0.00", font=("Helvetica", 12))
        self.fps_label.pack(anchor=tk.W, pady=2)

        # self.websocket_status_label = ttk.Label(status_frame, text="接続状態: 未接続", font=("Helvetica", 12), foreground="grey")
        # self.websocket_status_label.pack(anchor=tk.W, pady=2)

        action_buttons_frame = ttk.LabelFrame(control_panel_frame, text="操作", padding="10")
        action_buttons_frame.pack(fill=tk.X, pady=(0, 10))

        self.start_button = tk.Button(action_buttons_frame, text="開始", bg="green", fg="white", command=self.start_process, width=15, height=2)
        self.start_button.pack(fill=tk.X, pady=5)
        self.stop_button = tk.Button(action_buttons_frame, text="停止", bg="red", fg="white", command=self.stop_process, width=15, height=2)
        self.stop_button.pack(fill=tk.X, pady=5)
        exit_button = tk.Button(action_buttons_frame, text="終了", bg="grey", fg="white", command=self.safe_exit, width=15, height=2)
        exit_button.pack(fill=tk.X, pady=5)

        fps_setting_frame = ttk.LabelFrame(control_panel_frame, text="FPS設定", padding="10")
        fps_setting_frame.pack(fill=tk.X, pady=(0, 10))

        fps_options = [
            ("1 FPS", "1"),
            ("5 FPS", "5"),
            ("10 FPS", "10"),
            ("20 FPS", "20"),
            ("30 FPS", "30")
        ]
        for label, fps_val in fps_options:
            rb = ttk.Radiobutton(fps_setting_frame, text=label, variable=self.fps_var, value=fps_val, command=lambda f=fps_val: self._set_fps(f))
            rb.pack(anchor=tk.W, pady=2)

        resolution_setting_frame = ttk.LabelFrame(control_panel_frame, text="解像度設定", padding="10")
        resolution_setting_frame.pack(fill=tk.X, pady=(0, 10))

        resolution_options = [
            ("160x120 (QQVGA)", "160x120"),
            ("176x144 (QCIF)", "176x144"),
            ("240x176 (HQVGA)", "240x176"),
            ("240x240", "240x240"),
            ("320x240 (QVGA)", "320x240"),
        ]
        for label, res_val in resolution_options:
            rb = ttk.Radiobutton(resolution_setting_frame, text=label, variable=self.resolution_var, value=res_val, command=lambda r=res_val: self._set_resolution(r))
            rb.pack(anchor=tk.W, pady=2)

        # 未知の顔保存トグルスイッチ
        save_unknown_frame = ttk.LabelFrame(control_panel_frame, text="設定", padding="10")
        save_unknown_frame.pack(fill=tk.X, pady=(0, 10))
        self.save_unknown_faces_check = ttk.Checkbutton(save_unknown_frame, text="未知の顔を保存", variable=self.save_unknown_faces_var)
        self.save_unknown_faces_check.pack(anchor=tk.W, pady=2)


        log_frame = ttk.LabelFrame(control_panel_frame, text="ログ", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 0))

        self.log_text = tk.Text(log_frame, state='disabled', wrap='word', font=("Meiryo", 10))
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text['yscrollcommand'] = scrollbar.set

        self.image_label = ttk.Label(video_display_frame, text="No frame", background="black")
        self.image_label.pack(expand=True, fill=tk.BOTH)
        self.root.image_label = self.image_label # Tkinterのガベージコレクション対策

        # TkinterHandlerをここで設定
        log_handler = TkinterHandler(self.log_text, self.log_queue)
        log_handler.setFormatter(logging.Formatter('%(asctime)s %(name)s:%(lineno)s %(funcName)s [%(levelname)s]: %(message)s'))
        logging.getLogger().addHandler(log_handler) # ルートロガーに追加
        self.logger.info("GUIをセットアップしました。")

    def _on_resize(self, event):
        """ウィンドウリサイズ時の処理"""
        # ボタンやログのスペースを考慮してマージンを調整
        self.video_canvas_width = max(event.width - 350, 400)
        self.video_canvas_height = max(event.height - 300, 300)

    def _load_known_faces(self):
        """既知の顔データをロードする"""
        faces_dir = AppConfig.FACES_DIR
        if not os.path.exists(faces_dir):
            os.makedirs(faces_dir)
            self.logger.info(f"ディレクトリ {faces_dir} を作成しました。")

        for filename in os.listdir(faces_dir):
            if filename.lower().startswith("unknown_"):
                self.logger.info(f"既知の顔として 'Unknown_' で始まるファイル '{filename}' をスキップしました。")
                continue

            if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                name_part = filename.split('_')[0]
                name = name_part if name_part else "Unknown"

                filepath = os.path.join(faces_dir, filename)
                try:
                    img = face_recognition.load_image_file(filepath)
                    encodings = face_recognition.face_encodings(img)
                    if encodings:
                        self.known_face_encodings.append(encodings[0])
                        self.known_face_names.append(name)
                        self.logger.debug(f"ロード成功: {name} ({filename})")
                    else:
                        self.logger.debug(f"顔が検出されませんでした: {filename}")
                except Exception as e:
                    self.logger.error(f"ファイルの処理中にエラーが発生しました: {filename} - {e}")

        self.logger.info(f"Loaded {len(self.known_face_encodings)} known faces.")
        self.logger.debug(str(self.known_face_names))

    def _on_websocket_message(self, ws_app, message):
        """WebSocketメッセージ受信時の処理"""
        if isinstance(message, bytes):
            original_color_frame = cv2.imdecode(np.frombuffer(message, np.uint8), cv2.IMREAD_COLOR)
            if original_color_frame is not None:
                original_color_frame = cv2.rotate(original_color_frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
                
                face_results = self._process_faces_and_get_coords(original_color_frame.copy())

                frame_with_drawings = original_color_frame.copy()
                for (top, right, bottom, left), name in face_results:
                    color = (0, 0, 255) if name == "Unknown" else (0, 255, 0)
                    cv2.rectangle(frame_with_drawings, (left, top), (right, bottom), color, 1)
                    cv2.putText(frame_with_drawings, name, (left + 6, bottom + 12), cv2.FONT_HERSHEY_DUPLEX, 0.5, color, 1)

                with self.frame_lock:
                    self.latest_frame = frame_with_drawings
                
                self.frame_count += 1
                elapsed_time = time.time() - self.start_time
                if elapsed_time >= 1.0:
                    self.current_fps = self.frame_count / elapsed_time
                    self.frame_count = 0
                    self.start_time = time.time()
                    self.fps_queue.put(self.current_fps)

        elif isinstance(message, str):
            if message == "error:frame_capture_failed":
                self.logger.warning("ESP32からフレーム取得失敗通知を受信しました。")
                current_time_esp_err = time.time()
                if (current_time_esp_err - self.last_resolution_resend_time > AppConfig.RESOLUTION_RESEND_INTERVAL_SEC):
                    self.logger.info(f"ESP32でのフレーム取得失敗のため、現在の解像度 ({self.current_resolution}) を再送信します。")
                    self.send_command(f"SET_RESOLUTION:{self.current_resolution}")
                    self.last_resolution_resend_time = current_time_esp_err
                else:
                    self.logger.info("ESP32フレーム取得失敗通知を受信しましたが、短時間での解像度再送信はスキップします。")
            elif message.startswith("from_esp32:"):
                self.logger.info(message)
            elif message.startswith("current_fps:"):
                try:
                    fps_val = message.split(":")[1].strip()
                    self.root.after(0, lambda: self.fps_var.set(fps_val))
                    self.logger.info(f"ESP32からFPS設定を受信: {fps_val}")
                except IndexError:
                    self.logger.warning(f"不正なFPSメッセージ形式: {message}")
            elif message.startswith("current_resolution:"):
                try:
                    res_val = message.split(":")[1].strip()
                    self.root.after(0, lambda: self.resolution_var.set(res_val))
                    self.logger.info(f"ESP32から解像度設定を受信: {res_val}")
                except IndexError:
                    self.logger.warning(f"不正な解像度メッセージ形式: {message}")
        else:
            self.logger.warning(f"Unknown message type: {type(message)}")

    def _on_websocket_error(self, ws_app, error):
        """WebSocketエラー発生時の処理"""
        self.logger.error(f"App WebSocketエラー: {error}")
        # self.root.after(0, lambda: self.websocket_status_label.config(text="接続状態: エラー", foreground="red")) # WebSocketステータス表示を削除
        self.root.after(0, self._update_button_states)

    def _on_websocket_close(self, ws_app, close_status_code, close_msg):
        """WebSocket接続切断時の処理"""
        self.logger.warning(f"App WebSocket接続が切断されました。コード: {close_status_code}, メッセージ: {close_msg}")
        # self.root.after(0, lambda: self.websocket_status_label.config(text="接続状態: 切断", foreground="red")) # WebSocketステータス表示を削除
        self.root.after(0, self._update_button_states)

    def _on_websocket_open(self, ws_app):
        """WebSocket接続確立時の処理"""
        self.logger.info("App WebSocket接続が確立しました (on_app_open)。")
        # self.root.after(0, lambda: self.websocket_status_label.config(text="接続状態: 接続済み", foreground="green")) # WebSocketステータス表示を削除
        
        if self.send_stream_command_on_open:
            self.logger.info("接続確立のため、start_streamコマンドを送信します。")
            self.send_command("start_stream")
            self.send_stream_command_on_open = False
        self.root.after(0, self._update_button_states)

    def _start_websocket_manager(self):
        """WebSocketマネージャースレッドを開始する"""
        if self.websocket_manager_thread is None or not self.websocket_manager_thread.is_alive():
            if self.websocket_client.stop_event.is_set():
                self.websocket_client.stop_event.clear()
            self.websocket_manager_thread = threading.Thread(target=self.websocket_client.run_manager, daemon=True)
            self.websocket_manager_thread.start()
            self.logger.info("WebSocketClient.run_manager() を別スレッドで開始しました（自動再接続用）。")

    def send_command(self, command):
        """ESP32-CAMにコマンドを送信する"""
        if self.websocket_client:
            self.websocket_client.send(command)
        else:
            self.logger.error("WebSocketクライアントが初期化されていません。")

    def start_process(self):
        """ストリーミングプロセスを開始する"""
        if not self.is_running:
            self.is_running = True
            self.logger.info("プロセスを開始します。")
            self._update_button_states()
        
            if self.websocket_client and self.websocket_client.is_connected:
                self.logger.info("WebSocketクライアントが接続済みです。")
                self.send_command("start_stream")
                self.send_stream_command_on_open = False
                # self.root.after(0, lambda: self.websocket_status_label.config(text="接続状態: 接続済み", foreground="green")) # WebSocketステータス表示を削除
            else:
                self.send_stream_command_on_open = True
                self.websocket_client.connect()
                self._start_websocket_manager() # WebSocket接続が開始されるようにマネージャースレッドも開始

            self._update_image()
        else:
            self.logger.info("既にプロセスが実行中です。")

    def stop_process(self):
        """ストリーミングプロセスを停止する"""
        if self.is_running:
            self.is_running = False
            self.logger.info("プロセスを停止します。")
            self._update_button_states()
            self.send_stream_command_on_open = False
            self.send_command("stop_stream")
            
            # self.root.after(0, lambda: self.websocket_status_label.config(text="接続状態: ストリーム停止中", foreground="blue")) # WebSocketステータス表示を削除
        else:
            self.logger.info("プロセスは既に停止しています。")

    def _update_button_states(self):
        """ボタンの状態を更新する"""
        if self.is_running:
            self.start_button.config(state='disabled')
            self.stop_button.config(state='normal')
        else:
            self.start_button.config(state='normal')
            self.stop_button.config(state='disabled')

    def _process_queues(self):
        """キューからログとFPSを処理し、GUIを更新する"""
        while not self.log_queue.empty():
            log_entry = self.log_queue.get()
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, log_entry + "\n")
            self.log_text.yview(tk.END)
            self.log_text.config(state=tk.DISABLED)
        while not self.fps_queue.empty():
            fps = self.fps_queue.get()
            self.fps_label.config(text=f"現在のFPS: {fps:.2f}")

        self.root.after(100, self._process_queues)

    def _update_image(self):
        """受信したフレームをGUIに表示する"""
        if not self.is_running:
            return
        
        frame_to_display = None
        with self.frame_lock:
            if self.latest_frame is not None:
                frame_to_display = self.latest_frame.copy()

        if frame_to_display is not None and self.image_label:
            try:
                display_width = self.image_label.winfo_width()
                display_height = self.image_label.winfo_height()

                # 初期値や最小値を設定
                if display_width <= 1: display_width = 800 # Fallback to default canvas width
                if display_height <= 1: display_height = 600 # Fallback to default canvas height

                h, w = frame_to_display.shape[:2]
                
                scale_w = display_width / w
                scale_h = display_height / h
                scale = min(scale_w, scale_h)

                new_w = int(w * scale)
                new_h = int(h * scale)
                
                if new_w > 0 and new_h > 0:
                    resized_frame = cv2.resize(frame_to_display, (new_w, new_h))
                    rgb_frame_for_pil = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2RGB)
                    imgtk = ImageTk.PhotoImage(image=Image.fromarray(rgb_frame_for_pil))

                    self.image_label.imgtk = imgtk
                    self.image_label.configure(image=imgtk)
                else:
                    self.logger.debug("リサイズ後の画像サイズが無効です。")

            except Exception as e:
                self.logger.debug(f"画像更新中にエラー発生: {e}")

        self.root.after(10, self._update_image)

    def _set_fps(self, fps):
        """ESP32-CAMのFPSを設定する"""
        self.current_fps_setting = fps
        self.logger.info(f"FPSを{fps}に設定しました。")
        self.send_command(f"SET_FPS:{fps}")

    def _set_resolution(self, resolution):
        """ESP32-CAMの解像度を設定する"""
        self.current_resolution = resolution
        self.logger.info(f"解像度を{resolution}に設定しました。")
        self.send_command(f"SET_RESOLUTION:{resolution}")

    def _convert_to_grayscale(self, frame):
        """画像をグレースケールに変換する"""
        if len(frame.shape) == 2 or frame.shape[2] == 1:
            return frame
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return gray

    def _remove_noise(self, frame):
        """画像からノイズを除去する"""
        denoised = cv2.GaussianBlur(frame, (5, 5), 0)
        return denoised

    def _histogram_equalization(self, frame):
        """ヒストグラム平坦化を適用する"""
        equalized = cv2.equalizeHist(frame)
        return equalized

    def _adjust_gamma(self, image, gamma=1.0):
        """ガンマ補正を適用する"""
        invGamma = 1.0 / gamma
        table = np.array([
            ((i / 255.0) ** invGamma) * 255
            for i in np.arange(0, 256)
        ]).astype("uint8")
        return cv2.LUT(image, table)

    def _preprocess_frame(self, frame):
        """顔認識のための前処理を行う"""
        gray = self._convert_to_grayscale(frame)
        denoised = self._remove_noise(gray)
        equalized = self._histogram_equalization(denoised)
        gamma_corrected = self._adjust_gamma(equalized, gamma=1.5)
        return gamma_corrected

    def _process_faces_and_get_coords(self, frame_for_processing):
        """フレーム内の顔を検出し、認識する"""
        gray_frame = cv2.cvtColor(frame_for_processing, cv2.COLOR_BGR2GRAY)
        processed_frame = self._preprocess_frame(gray_frame)

        current_detected_names = set()
        face_detection_results = []

        color_for_dlib = cv2.cvtColor(processed_frame, cv2.COLOR_GRAY2BGR)

        face_locations = face_recognition.face_locations(color_for_dlib, model="hog")
        face_encodings = face_recognition.face_encodings(color_for_dlib, face_locations)

        for (top, right, bottom, left), face_encoding in zip(face_locations, face_encodings):
            matches = face_recognition.compare_faces(self.known_face_encodings, face_encoding, tolerance=AppConfig.FACE_MATCH_THRESHOLD)
            name = "Unknown"

            if self.known_face_encodings:
                face_distances = face_recognition.face_distance(self.known_face_encodings, face_encoding)
                best_match_index = np.argmin(face_distances)
                if matches[best_match_index]:
                    name = self.known_face_names[best_match_index]
                    current_detected_names.add(name)

            current_time = time.time()
            if name == "Unknown":
                if self.save_unknown_faces_var.get(): # トグルスイッチの状態を確認
                    # 過去1分間の未知の顔の記録をクリーンアップ
                    self.unknown_face_times = [t for t in self.unknown_face_times if t > current_time - 60]
                    if len(self.unknown_face_times) < 10: # max_unknown_faces_per_minute
                        self._save_unknown_face(gray_frame, (top, right, bottom, left))
                        self.unknown_face_times.append(current_time)
                else:
                    self.logger.debug("未知の顔の保存は無効になっています。")
            else:
                self.logger.info(f"顔を検出しました: {name}")

                if name in self.detected_counts:
                    self.detected_counts[name] += 1
                else:
                    self.detected_counts[name] = 1

            face_detection_results.append(((top, right, bottom, left), name))

        # 検出されなくなった顔のカウントをリセット
        for name_key in list(self.detected_counts.keys()):
            if name_key not in current_detected_names:
                self.detected_counts[name_key] = 0
        
        return face_detection_results

    def _save_unknown_face(self, frame, face_coords):
        """未知の顔を画像として保存する"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"Unknown_{timestamp}.jpg"
        path = os.path.join(AppConfig.FACES_DIR, filename)
        cv2.imwrite(path, frame)
        self.logger.info(f"未知の顔を保存しました: {filename}")

    def safe_exit(self):
        """アプリケーションを安全に終了する"""
        self.logger.info("終了処理を開始します。")
        self.stop_process() # ストリームを停止

        if self.websocket_client:
            self.logger.info("WebSocketクライアントを明示的に閉じます。")
            self.websocket_client.close()
            # self.websocket_client = None # シングルトンなのでNoneにしない

        if self.websocket_manager_thread and self.websocket_manager_thread.is_alive():
            self.logger.info("WebSocketマネージャスレッドの終了を試みます。")
            self.websocket_manager_thread.join(timeout=2.0)
            if self.websocket_manager_thread.is_alive():
                self.logger.warning("WebSocketマネージャスレッドがタイムアウト後も終了していません。")
            else:
                self.logger.info("WebSocketマネージャスレッドを終了しました。")

        self.logger.info("Tkinter GUIを閉じます。")
        self.root.destroy()
        self.logger.info("アプリケーションを終了しました。")


def main():
    """アプリケーションのエントリーポイント"""
    root = tk.Tk()
    app = App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
