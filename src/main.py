import os
from dotenv import load_dotenv
import cv2
import numpy as np
import face_recognition  # 顔認証用ライブラリ
import websocket
import threading
import time
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
import queue  # キューのインポート
from datetime import datetime
import requests
import logging
from logging import getLogger, config
from logging_handlers import TkinterHandler #
import json


# -----------------------------------------------------------------------------
# Haar Cascades（顔検出用）を読み込み
face_cascade = cv2.CascadeClassifier("resources/models/haarcascade_frontalface_default.xml") #
if face_cascade.empty():
    raise IOError("Haar Cascades ファイルが見つかりません。正しいパスを確認してください。") #

# 環境変数の読み込み
load_dotenv() #

class Config:
    WS_URL = os.getenv("WS_URL", "ws://localhost:8080") #
    LINE_NOTIFY_TOKEN = os.getenv("LINE_NOTIFY_TOKEN", "") #
    FACES_DIR = os.getenv("FACES_DIR", "./resources/faces") #
    FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", 0.5)) #


# WebSocketのURL（ESP32-CAMのIPアドレスに合わせて変更）
ws_url = Config.WS_URL #
latest_frame = None #
frame_lock = threading.Lock() #

frame_count = 0 #
start_time = time.time() #
current_fps = 0 #

#LINE Notifyのトークン

detected_counts = {} #
notified_names = set() #

# --- Global logger instance ---
logger = None #

def send_line_notify(message):
    url = "https://notify-api.line.me/api/notify" #
    headers = {
        "Authorization": f"Bearer {Config.LINE_NOTIFY_TOKEN}", #
        "Content-Type": "application/x-www-form-urlencoded" #
    }
    payload = {
        "message": message #
    }
    try:
        response = requests.post(url, headers=headers, data=payload) #
        if response.status_code != 200:
            logger.error(f"LINE Notifyの送信に失敗しました: {response.status_code}") #
        else:
            logger.info("LINE Notify sent successfully.") #
    except Exception as e:
        logger.error(f"LINE Notifyの送信中にエラーが発生しました: {e}") #


# -----------------------------------------------------------------------------
# 1. 顔認証用の既知人物データを準備
# 例として "qaq.jpg" 等を用いて事前にエンコードを取得

known_face_encodings = [] #
known_face_names = [] #

# メッセージキューの作成
log_queue = queue.Queue() #
fps_queue = queue.Queue()  # 追加: FPS用キュー #

# -----------------------------------------------------------------------------
# 2. GUIの設定とボタンの追加

root = None  # 後で初期化 #
start_button = None #
stop_button = None #
log_text = None #
fps_label = None #
image_label = None # image_label をグローバルスコープで初期化
websocket_status_label = None # WebSocket接続状態表示用ラベルを追加

is_running = False  # プロセスの状態管理をFalseで初期化 #
current_fps_setting = "1"  # デフォルトFPSをESP32側の初期値に合わせる #
current_resolution = "160x120"  # デフォルト解像度をESP32側の初期値に合わせる #

# ウィンドウサイズを記憶する変数
video_canvas_width = 800 #
video_canvas_height = 600 #

# リストで顔写真の保存時間を管理
unknown_face_times = [] #
max_unknown_faces_per_minute = 10 #

# 前回の画像送信の時刻を記録
last_send_time = 0 #

send_stream_command_on_open = False # 接続確立後にストリーム開始コマンドを送るためのフラグ

# 解像度再送信のための変数
last_resolution_resend_time = 0
RESOLUTION_RESEND_INTERVAL = 5 # 秒

def on_resize(event):
    global video_canvas_width, video_canvas_height
    # ウィンドウのサイズに合わせて、動画表示領域の幅・高さを更新
    # ボタンやログのスペースを考慮してマージンを調整
    video_canvas_width = max(event.width - 350, 400)   # ボタンやログの幅を考慮 #
    video_canvas_height = max(event.height - 300, 300)  # ボタンやログの高さを考慮 #

# -----------------------------------------------------------------------------
# WebSocket接続管理クラス
class WebSocketClient:
    _instance = None

    def __new__(cls, url, on_message, on_error, on_close, on_open):
        if cls._instance is None:
            cls._instance = super(WebSocketClient, cls).__new__(cls) #
        return cls._instance

    def __init__(self, url, on_message, on_error, on_close, on_open):
        if hasattr(self, 'initialized') and self.initialized:
            if self.url != url:
                 logger.warning("WebSocketClientのURLが変更されましたが、シングルトンのため古いインスタンスを再利用します。")
            # return # 既存の return をコメントアウトして、再接続時などに on_open などが再設定されるようにする
        
        self.url = url #
        self.on_message_callback = on_message
        self.on_error_callback = on_error
        self.on_close_callback = on_close
        self.on_open_callback = on_open
        self.ws = None #
        self.thread = None #
        self.stop_event = threading.Event() #
        self.is_connected = False
        self.initialized = True

    def _on_message(self, ws, message):
        self.on_message_callback(ws, message)

    def _on_error(self, ws, error):
        global logger
        self.is_connected = False
        if logger: # logger が None でないことを確認
            logger.error(f"WebSocketエラー (WebSocketClient): {error}")
        if websocket_status_label and root: # root が None でないことを確認
            root.after(0, lambda: websocket_status_label.config(text="接続状態: エラー", foreground="red"))
        self.on_error_callback(ws, error)

    def _on_close(self, ws, close_status_code, close_msg):
        global logger
        self.is_connected = False
        if logger: # logger が None でないことを確認
            logger.warning(f"WebSocket接続が閉じられました。コード: {close_status_code}, メッセージ: {close_msg}")
        if websocket_status_label and root: # root が None でないことを確認
            root.after(0, lambda: websocket_status_label.config(text="接続状態: 切断", foreground="red"))
        self.on_close_callback(ws, close_status_code, close_msg)


    def _on_open(self, ws):
        global logger
        self.is_connected = True
        if logger: # logger が None でないことを確認
            logger.info("WebSocketに接続しました。 (WebSocketClient._on_open)")
        if websocket_status_label and root: # root が None でないことを確認
            root.after(0, lambda: websocket_status_label.config(text="接続状態: 接続済み", foreground="green"))
        self.on_open_callback(ws)

    def connect(self):
        global logger
        if self.ws and self.ws.keep_running:
            if logger:
                logger.info("既にWebSocket接続処理が実行中です。")
            if self.is_connected:
                return
            else:
                if logger:
                    logger.info("以前の接続はあったが、現在未接続のため再接続を試みます。")
                if self.thread and self.thread.is_alive():
                    try:
                        self.stop_event.set() # スレッドに停止を通知
                        self.ws.close()
                        self.thread.join(timeout=2.0)
                        self.stop_event.clear() # 次の接続のためにクリア
                    except Exception as e:
                        if logger:
                            logger.error(f"既存WebSocketスレッドの終了待機中にエラー: {e}")
        
        if websocket_status_label and root:
            root.after(0, lambda: websocket_status_label.config(text="接続状態: 接続試行中...", foreground="orange"))

        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )
        self.thread = threading.Thread(target=self.ws.run_forever) #
        self.thread.daemon = True #
        self.thread.start() #
        if logger:
            logger.info("WebSocketクライアント接続処理を開始しました。")

    def send(self, message):
        global logger
        if self.is_connected and self.ws and self.ws.sock and self.ws.sock.connected:
            try:
                self.ws.send(message) #
                if logger:
                    logger.debug(f"WebSocketにメッセージを送信: {message}") #
            except Exception as e:
                if logger:
                    logger.error(f"WebSocket送信エラー: {e}") #
                self.is_connected = False
                if websocket_status_label and root:
                    root.after(0, lambda: websocket_status_label.config(text="接続状態: 送信エラー", foreground="red"))
        else:
            if logger:
                logger.warning(f"WebSocketが接続されていません。メッセージ '{message}' の送信をスキップします。")

    def close(self):
        global logger
        self.stop_event.set() # スレッドに停止を通知
        self.is_connected = False # 接続状態をFalseに設定
        if self.ws:
            self.ws.close() #
            if logger:
                logger.info("WebSocketクライアント接続を閉じました。")
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
            if self.thread.is_alive():
                if logger:
                    logger.warning("WebSocketスレッドがタイムアウト後も終了していません。")
            else:
                if logger:
                    logger.info("WebSocketスレッドを終了しました。")
        self.ws = None
        self.thread = None


    def run(self):
        global logger
        while not self.stop_event.is_set():
            if not self.is_connected:
                if not (self.ws and self.ws.keep_running):
                    if logger:
                        logger.warning("WebSocketが切断されました。再接続を試みます...")
                    self.connect()
            time.sleep(5) #
        if logger:
            logger.info("WebSocketClient runループが終了しました。")

# -----------------------------------------------------------------------------
# GUIの設定とボタンの追加
def setup_gui():
    global root, start_button, stop_button, log_text, fps_label, image_label, websocket_status_label, logger
    root = tk.Tk() #
    root.title("ESP32-CAM 顔認証デモ") #
    root.geometry("1200x800") #
    root.resizable(True, True) #

    try:
        root.eval('tk::PlaceWindow . center') #
    except:
        pass

    root.bind("<Configure>", on_resize) #

    main_frame = ttk.Frame(root, padding="10 10 10 10") #
    main_frame.pack(fill=tk.BOTH, expand=True) #

    control_panel_frame = ttk.Frame(main_frame, width=300) #
    control_panel_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10)) #
    control_panel_frame.pack_propagate(False) #

    video_display_frame = ttk.Frame(main_frame) #
    video_display_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True) #

    status_frame = ttk.LabelFrame(control_panel_frame, text="ステータス", padding="10") #
    status_frame.pack(fill=tk.X, pady=(0, 10)) #

    fps_label = ttk.Label(status_frame, text="現在のFPS: 0.00", font=("Helvetica", 12)) #
    fps_label.pack(anchor=tk.W, pady=2) #

    websocket_status_label = ttk.Label(status_frame, text="接続状態: 未接続", font=("Helvetica", 12), foreground="grey") #
    websocket_status_label.pack(anchor=tk.W, pady=2) #

    action_buttons_frame = ttk.LabelFrame(control_panel_frame, text="操作", padding="10") #
    action_buttons_frame.pack(fill=tk.X, pady=(0, 10)) #

    start_button = tk.Button(action_buttons_frame, text="開始", bg="green", fg="white", command=start_process, width=15, height=2) #
    start_button.pack(fill=tk.X, pady=5) #
    stop_button = tk.Button(action_buttons_frame, text="停止", bg="red", fg="white", command=stop_process, width=15, height=2) #
    stop_button.pack(fill=tk.X, pady=5) #
    exit_button = tk.Button(action_buttons_frame, text="終了", bg="grey", fg="white", command=safe_exit, width=15, height=2) #
    exit_button.pack(fill=tk.X, pady=5) #

    fps_setting_frame = ttk.LabelFrame(control_panel_frame, text="FPS設定", padding="10") #
    fps_setting_frame.pack(fill=tk.X, pady=(0, 10)) #

    global fps_var
    fps_var = tk.StringVar(value=current_fps_setting) #
    fps_options = [
        ("1 FPS", "1"), #
        ("5 FPS", "5"), #
        ("10 FPS", "10"), #
        ("20 FPS", "20"), #
        ("30 FPS", "30") #
    ]
    for label, fps_val in fps_options:
        rb = ttk.Radiobutton(fps_setting_frame, text=label, variable=fps_var, value=fps_val, command=lambda f=fps_val: set_fps(f)) #
        rb.pack(anchor=tk.W, pady=2) #

    resolution_setting_frame = ttk.LabelFrame(control_panel_frame, text="解像度設定", padding="10") #
    resolution_setting_frame.pack(fill=tk.X, pady=(0, 10)) #

    global resolution_var
    resolution_var = tk.StringVar(value=current_resolution) #
    resolution_options = [
        ("160x120 (QQVGA)", "160x120"), #
        ("176x144 (QCIF)", "176x144"), #
        ("240x176 (HQVGA)", "240x176"), #
        ("240x240", "240x240"), #
        ("320x240 (QVGA)", "320x240"), #
    ]
    for label, res_val in resolution_options:
        rb = ttk.Radiobutton(resolution_setting_frame, text=label, variable=resolution_var, value=res_val, command=lambda r=res_val: set_resolution(r)) #
        rb.pack(anchor=tk.W, pady=2) #

    log_frame = ttk.LabelFrame(control_panel_frame, text="ログ", padding="10") #
    log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 0)) #

    log_text = tk.Text(log_frame, state='disabled', wrap='word', font=("Meiryo", 10)) #
    log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True) #
    scrollbar = ttk.Scrollbar(log_frame, command=log_text.yview) #
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y) #
    log_text['yscrollcommand'] = scrollbar.set #

    image_label = ttk.Label(video_display_frame, text="No frame", background="black") #
    image_label.pack(expand=True, fill=tk.BOTH) #
    root.image_label = image_label

    with open('log_config.json', 'r', encoding='utf-8') as f: #
        log_config = json.load(f) #

    log_config['handlers']['tkinterHandler']['text_widget'] = log_text #
    log_config['handlers']['tkinterHandler']['log_queue'] = log_queue #
    logging.config.dictConfig(log_config) #

    logger = getLogger(__name__) #
    logger.info("GUIをセットアップしました。") #

    root.after(100, process_queues) #
    update_button_states() #


# -----------------------------------------------------------------------------
# WebSocketのイベントハンドラ (アプリケーションレベル)
def on_app_message(ws_app, message): # 名前を変更して WebSocketClient と区別
    global latest_frame, frame_count, start_time, current_fps, logger, root, fps_var, resolution_var, last_resolution_resend_time

    if isinstance(message, bytes):
        original_color_frame = cv2.imdecode(np.frombuffer(message, np.uint8), cv2.IMREAD_COLOR) #
        if original_color_frame is not None:
            original_color_frame = cv2.rotate(original_color_frame, cv2.ROTATE_90_COUNTERCLOCKWISE) #
            
            face_results = process_faces_and_get_coords(original_color_frame.copy()) #

            frame_with_drawings = original_color_frame.copy() #
            for (top, right, bottom, left), name in face_results:
                color = (0, 0, 255) if name == "Unknown" else (0, 255, 0) #
                cv2.rectangle(frame_with_drawings, (left, top), (right, bottom), color, 1) #
                cv2.putText(frame_with_drawings, name, (left + 6, bottom + 12), cv2.FONT_HERSHEY_DUPLEX, 0.5, color, 1) #

            with frame_lock:
                latest_frame = frame_with_drawings #
            
            frame_count += 1 #
            elapsed_time = time.time() - start_time #
            if elapsed_time >= 1.0:
                current_fps = frame_count / elapsed_time #
                frame_count = 0 #
                start_time = time.time() #
                fps_queue.put(current_fps) #

    elif isinstance(message, str):
        if message == "error:frame_capture_failed": # ESP32からのフレーム取得失敗通知
            if logger:
                logger.warning("ESP32からフレーム取得失敗通知を受信しました。")
            current_time_esp_err = time.time()
            if (current_time_esp_err - last_resolution_resend_time > RESOLUTION_RESEND_INTERVAL):
                if logger:
                    logger.info(f"ESP32でのフレーム取得失敗のため、現在の解像度 ({current_resolution}) を再送信します。")
                send_command(f"SET_RESOLUTION:{current_resolution}")
                last_resolution_resend_time = current_time_esp_err
            else:
                if logger:
                    logger.info("ESP32フレーム取得失敗通知を受信しましたが、短時間での解像度再送信はスキップします。")
        elif message.startswith("from_esp32:"): #
            if logger: logger.info(message) #
        elif message.startswith("current_fps:"): #
            try:
                fps_val = message.split(":")[1].strip() #
                if root: root.after(0, lambda: fps_var.set(fps_val)) #
                if logger: logger.info(f"ESP32からFPS設定を受信: {fps_val}") #
            except IndexError:
                if logger: logger.warning(f"不正なFPSメッセージ形式: {message}") #
        elif message.startswith("current_resolution:"): #
            try:
                res_val = message.split(":")[1].strip() #
                if root: root.after(0, lambda: resolution_var.set(res_val)) #
                if logger: logger.info(f"ESP32から解像度設定を受信: {res_val}") #
            except IndexError:
                if logger: logger.warning(f"不正な解像度メッセージ形式: {message}") #
    else:
        if logger: logger.warning(f"Unknown message type: {type(message)}") #

def on_app_error(ws_app, error): # 名前を変更
    global logger, root
    if logger: logger.error(f"App WebSocketエラー: {error}") #
    # GUIラベル更新はWebSocketClient._on_errorで行う
    if root: root.after(0, update_button_states) #

def on_app_close(ws_app, close_status_code, close_msg): # 名前を変更
    global logger, root
    if logger: logger.warning(f"App WebSocket接続が切断されました。コード: {close_status_code}, メッセージ: {close_msg}") #
    # GUIラベル更新はWebSocketClient._on_closeで行う
    if root: root.after(0, update_button_states) #

def on_app_open(ws_app): # 名前変更 (original_on_open -> on_app_open)
    global send_stream_command_on_open, logger
    if logger: logger.info("App WebSocket接続が確立しました (on_app_open)。") #
    # GUIラベル更新はWebSocketClient._on_openで行う
    
    if send_stream_command_on_open:
        if logger: logger.info("接続確立のため、start_streamコマンドを送信します。")
        send_command("start_stream") #
        send_stream_command_on_open = False
    if root: root.after(0, update_button_states) #


# -----------------------------------------------------------------------------
# WebSocketの設定とスレッド管理
websocket_client = None #
websocket_lock = threading.Lock() #
websocket_manager_thread = None

def start_websocket():
    global websocket_client, websocket_manager_thread, logger
    with websocket_lock:
        if websocket_client is None:
            websocket_client = WebSocketClient(
                ws_url,
                on_app_message, # 修正
                on_app_error,   # 修正
                on_app_close,   # 修正
                on_app_open     # 修正
            )
            if logger: logger.info("WebSocketClientインスタンスを新規作成しました。")
        
        if not websocket_client.is_connected:
            if websocket_client.ws and websocket_client.ws.keep_running:
                 if logger: logger.info("WebSocketは実行中ですが未接続です。closeを試みてから再接続します。")
                 websocket_client.close()
            
            if websocket_status_label and root:
                root.after(0, lambda: websocket_status_label.config(text="接続状態: 接続試行中...", foreground="orange"))
            websocket_client.connect()
            
            if websocket_manager_thread is None or not websocket_manager_thread.is_alive():
                if websocket_client.stop_event.is_set(): # runが一度終了していたらstop_eventをクリア
                    websocket_client.stop_event.clear()
                websocket_manager_thread = threading.Thread(target=websocket_client.run, daemon=True)
                websocket_manager_thread.start()
                if logger: logger.info("WebSocketClient.run() を別スレッドで開始しました（自動再接続用）。")
            if logger: logger.info("WebSocketクライアント接続処理を開始/再開しました。")
        else:
            if logger: logger.info("WebSocketクライアントは既に接続済みです。")
            # 既に接続済みの場合は、GUIのステータスも「接続済み」に更新
            if websocket_status_label and root:
                root.after(0, lambda: websocket_status_label.config(text="接続状態: 接続済み", foreground="green"))


# -----------------------------------------------------------------------------
# コマンド送信関数
def send_command(command):
    global logger
    if websocket_client:
        websocket_client.send(command) #
    else:
        if logger: logger.error("WebSocketクライアントが初期化されていません。") #

# -----------------------------------------------------------------------------
# プロセス開始/停止関数
def start_process():
    global is_running, send_stream_command_on_open, logger
    if not is_running:
        is_running = True
        if logger: logger.info("プロセスを開始します。") #
        if root: update_button_states() #
    
        if websocket_client and websocket_client.is_connected:
            if logger: logger.info("WebSocketクライアントが接続済みです。")
            send_command("start_stream")
            send_stream_command_on_open = False
            # 既に接続済みの場合もステータスを明示的に更新
            if websocket_status_label and root:
                root.after(0, lambda: websocket_status_label.config(text="接続状態: 接続済み", foreground="green"))
        else:
            send_stream_command_on_open = True
            start_websocket() 
        
        if root: update_image() #
    else:
        if logger: logger.info("既にプロセスが実行中です。") #

def stop_process():
    global is_running, send_stream_command_on_open, logger
    if is_running:
        is_running = False
        if logger: logger.info("プロセスを停止します。") #
        if root: update_button_states() #
        send_stream_command_on_open = False
        send_command("stop_stream") #
        
        # WebSocket接続は閉じないため、ステータスは「停止処理中」ではなく「接続済み」のままか、
        # あるいはストリーム停止中を示す状態にする
        if websocket_status_label and root:
            # ストリーム停止中だが接続は維持されている状態を示す
            root.after(0, lambda: websocket_status_label.config(text="接続状態: ストリーム停止中", foreground="blue"))
    else:
        if logger: logger.info("プロセスは既に停止しています。") #

# -----------------------------------------------------------------------------
# ボタンの状態を更新する関数
def update_button_states():
    if is_running:
        if start_button: start_button.config(state='disabled') #
        if stop_button: stop_button.config(state='normal') #
    else:
        if start_button: start_button.config(state='normal') #
        if stop_button: stop_button.config(state='disabled') #

# -----------------------------------------------------------------------------
# キューの管理(メインスレッドでやらないとSIGSEVするやつら)
def process_queues():
    global log_text, fps_label, root # root をglobalに追加
    while not log_queue.empty():
        log_entry = log_queue.get() #
        if log_text:
            log_text.config(state=tk.NORMAL) #
            log_text.insert(tk.END, log_entry + "\n") #
            log_text.yview(tk.END) #
            log_text.config(state=tk.DISABLED) #
    while not fps_queue.empty():
        fps = fps_queue.get() #
        if fps_label:
            fps_label.config(text=f"現在のFPS: {fps:.2f}") #

    if root: # rootがNoneでないことを確認
        root.after(100, process_queues) #

# -----------------------------------------------------------------------------
# 顔認証用の既知顔データのロード
def load_known_faces():
    global logger
    faces_dir = Config.FACES_DIR #
    if not os.path.exists(faces_dir): #
        os.makedirs(faces_dir) #
        if logger: logger.info(f"ディレクトリ {faces_dir} を作成しました。") #

    for filename in os.listdir(faces_dir): #
        if filename.lower().startswith("unknown_"): #
            if logger: logger.info(f"既知の顔として 'Unknown_' で始まるファイル '{filename}' をスキップしました。") #
            continue

        if filename.lower().endswith(('.jpg', '.jpeg', '.png')): #
            name_part = filename.split('_')[0] #
            name = name_part if name_part else "Unknown" #

            filepath = os.path.join(faces_dir, filename) #
            try:
                img = face_recognition.load_image_file(filepath) #
                encodings = face_recognition.face_encodings(img) #
                if encodings:
                    known_face_encodings.append(encodings[0]) #
                    known_face_names.append(name) #
                    if logger: logger.debug(f"ロード成功: {name} ({filename})") #
                else:
                    if logger: logger.debug(f"顔が検出されませんでした: {filename}") #
            except Exception as e:
                if logger: logger.error(f"ファイルの処理中にエラーが発生しました: {filename} - {e}") #

    if logger:
        logger.info(f"Loaded {len(known_face_encodings)} known faces.") #
        logger.debug(str(known_face_names)) #

# -----------------------------------------------------------------------------
# 画像更新関数
def update_image():
    global logger, root
    if not is_running: #
        return
    
    frame_to_display = None
    with frame_lock:
        if latest_frame is not None:
            frame_to_display = latest_frame.copy() #

    if frame_to_display is not None and root and root.image_label: # rootとimage_labelの存在を確認
        try:
            display_width = root.image_label.winfo_width() #
            display_height = root.image_label.winfo_height() #

            if display_width <= 1: display_width = video_canvas_width # 初期値や最小値を設定
            if display_height <= 1: display_height = video_canvas_height

            h, w = frame_to_display.shape[:2] #
            
            scale_w = display_width / w #
            scale_h = display_height / h #
            scale = min(scale_w, scale_h) #

            new_w = int(w * scale) #
            new_h = int(h * scale) #
            
            if new_w > 0 and new_h > 0:
                 resized_frame = cv2.resize(frame_to_display, (new_w, new_h)) #
                 rgb_frame_for_pil = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2RGB) #
                 imgtk = ImageTk.PhotoImage(image=Image.fromarray(rgb_frame_for_pil)) #

                 root.image_label.imgtk = imgtk #
                 root.image_label.configure(image=imgtk) #
            else:
                if logger: logger.debug("リサイズ後の画像サイズが無効です。")


        except Exception as e:
            if logger: logger.debug(f"画像更新中にエラー発生: {e}") #

    if root: # rootがNoneでないことを確認
        root.after(10, update_image) #

# -----------------------------------------------------------------------------
# FPS設定関数
def set_fps(fps):
    global current_fps_setting, logger
    current_fps_setting = fps #
    if logger: logger.info(f"FPSを{fps}に設定しました。") #
    send_command(f"SET_FPS:{fps}") #

# -----------------------------------------------------------------------------
# 解像度設定関数
def set_resolution(resolution):
    global current_resolution, logger
    current_resolution = resolution #
    if logger: logger.info(f"解像度を{resolution}に設定しました。") #
    send_command(f"SET_RESOLUTION:{resolution}") #

# -----------------------------------------------------------------------------
# 顔認識と未知顔保存機能の実装
# フィルタリング関数の定義
def convert_to_grayscale(frame):
    if len(frame.shape) == 2 or frame.shape[2] == 1: #
        return frame
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) #
    return gray

def remove_noise(frame):
    denoised = cv2.GaussianBlur(frame, (5, 5), 0) #
    return denoised

def histogram_equalization(frame):
    equalized = cv2.equalizeHist(frame) #
    return equalized

def adjust_gamma(image, gamma=1.0):
    invGamma = 1.0 / gamma #
    table = np.array([ #
        ((i / 255.0) ** invGamma) * 255 #
        for i in np.arange(0, 256) #
    ]).astype("uint8") #
    return cv2.LUT(image, table) #

def preprocess_frame(frame):
    gray = convert_to_grayscale(frame) #
    denoised = remove_noise(gray) #
    equalized = histogram_equalization(denoised) #
    gamma_corrected = adjust_gamma(equalized, gamma=1.5) #
    return gamma_corrected


def process_faces_and_get_coords(frame_for_processing):
    global unknown_face_times, logger, detected_counts, notified_names # logger, detected_counts, notified_namesをglobalに追加
    
    gray_frame = cv2.cvtColor(frame_for_processing, cv2.COLOR_BGR2GRAY) #
    processed_frame = preprocess_frame(gray_frame) #

    current_detected_names = set() #
    face_detection_results = [] #

    color_for_dlib = cv2.cvtColor(processed_frame, cv2.COLOR_GRAY2BGR) #

    face_locations = face_recognition.face_locations(color_for_dlib, model="hog") #
    face_encodings = face_recognition.face_encodings(color_for_dlib, face_locations) #

    for (top, right, bottom, left), face_encoding in zip(face_locations, face_encodings):
        matches = face_recognition.compare_faces(known_face_encodings, face_encoding, tolerance=Config.FACE_MATCH_THRESHOLD) #
        name = "Unknown" #

        if known_face_encodings:
            face_distances = face_recognition.face_distance(known_face_encodings, face_encoding) #
            best_match_index = np.argmin(face_distances) #
            if matches[best_match_index]:
                name = known_face_names[best_match_index] #
                current_detected_names.add(name) #

        current_time = time.time() #
        if name == "Unknown": #
            unknown_face_times = [t for t in unknown_face_times if t > current_time - 60] #
            if len(unknown_face_times) < max_unknown_faces_per_minute: #
                save_unknown_face(gray_frame, (top, right, bottom, left)) #
                unknown_face_times.append(current_time) #
        else:
            if logger: logger.info(f"顔を検出しました: {name}") #

            if name in detected_counts: #
                detected_counts[name] += 1 #
            else:
                detected_counts[name] = 1 #

        face_detection_results.append(((top, right, bottom, left), name)) #

    for name_key in list(detected_counts.keys()): # .keys() のコピーに対してイテレート
        if name_key not in current_detected_names:
            detected_counts[name_key] = 0 #
    
    return face_detection_results

def save_unknown_face(frame, face_coords):
    global logger
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S") #
    filename = f"Unknown_{timestamp}.jpg" #
    path = os.path.join(Config.FACES_DIR, filename) #
    cv2.imwrite(path, frame) #
    if logger: logger.info(f"未知の顔を保存しました: {filename}") #


def safe_exit():
    global logger, root, websocket_client, websocket_manager_thread
    if logger: logger.info("終了処理を開始します。") #
    stop_process()

    if websocket_client:
        if logger: logger.info("WebSocketクライアントを明示的に閉じます。")
        websocket_client.close()
        websocket_client = None
    
    if websocket_manager_thread and websocket_manager_thread.is_alive():
        if logger: logger.info("WebSocketマネージャスレッドの終了を試みます。")
        # websocket_client は既に None の可能性があるので、stop_event を直接操作できない。
        # WebSocketClient.close() の中で stop_event.set() が呼ばれることを期待する。
        # ここでは、websocket_client.close() に任せる。
        websocket_manager_thread.join(timeout=2.0)
        if websocket_manager_thread.is_alive():
            if logger: logger.warning("WebSocketマネージャスレッドがタイムアウト後も終了していません。")

    if logger: logger.info("Tkinter GUIを閉じます。")
    if root: # root が None でないことを確認
        root.destroy() #
    if logger: logger.info("アプリケーションを終了しました。")


# -----------------------------------------------------------------------------
# メイン処理
def start():
    global logger # main処理の最初でloggerを確実に取得
    
    # ログ設定の初期化 (setup_guiより前に移動して、早期にロガーを使えるようにする)
    # ただし、tkinterHandlerはlog_textウィジェットが必要なので、setup_gui後でないと完全には設定できない。
    # ここでは基本的なロガーを設定し、tkinterHandlerはsetup_gui内で追加する形にする。

    # loggerの初期化をここで行い、setup_gui内でtkinterHandlerを追加する
    with open('log_config.json', 'r', encoding='utf-8') as f:
        log_cfg = json.load(f)
    
    # tkinterHandler以外のハンドラを先に設定
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
    logger = getLogger(__name__) # これでコンソールとファイルへのログは開始される

    logger.info("アプリケーション起動シーケンス開始。")

    setup_gui()  # この中でtkinterHandlerが設定される
    logger.info("GUIセットアップ完了。") # このログはtkinterにも表示されるはず
    
    load_known_faces() #
    if root: # root が None でないことを確認
        root.mainloop() #
    else:
        logger.error("Tkinterのrootウィンドウが初期化されていません。")


if __name__ == "__main__":
    start() #
