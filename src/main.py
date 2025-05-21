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
from logging_handlers import TkinterHandler
import json


# -----------------------------------------------------------------------------
# Haar Cascades（顔検出用）を読み込み
face_cascade = cv2.CascadeClassifier("resources/models/haarcascade_frontalface_default.xml")
if face_cascade.empty():
    raise IOError("Haar Cascades ファイルが見つかりません。正しいパスを確認してください。")

# 環境変数の読み込み
load_dotenv()

class Config:
    WS_URL = os.getenv("WS_URL", "ws://localhost:8080")
    LINE_NOTIFY_TOKEN = os.getenv("LINE_NOTIFY_TOKEN", "")
    FACES_DIR = os.getenv("FACES_DIR", "./resources/faces")
    FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", 0.5))


# WebSocketのURL（ESP32-CAMのIPアドレスに合わせて変更）
ws_url = Config.WS_URL
latest_frame = None
frame_lock = threading.Lock()

frame_count = 0
start_time = time.time()
current_fps = 0

#LINE Notifyのトークン

detected_counts = {}
notified_names = set()

def send_line_notify(message):
    url = "https://notify-api.line.me/api/notify"
    headers = {
        "Authorization": f"Bearer {Config.LINE_NOTIFY_TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    payload = {
        "message": message
    }
    try:
        response = requests.post(url, headers=headers, data=payload)
        if response.status_code != 200:
            logger.error(f"LINE Notifyの送信に失敗しました: {response.status_code}")
        else:
            logger.info("LINE Notify sent successfully.")
    except Exception as e:
        logger.error(f"LINE Notifyの送信中にエラーが発生しました: {e}")


# -----------------------------------------------------------------------------
# 1. 顔認証用の既知人物データを準備
# 例として "qaq.jpg" 等を用いて事前にエンコードを取得

known_face_encodings = []
known_face_names = []

# メッセージキューの作成
log_queue = queue.Queue()
fps_queue = queue.Queue()  # 追加: FPS用キュー

# -----------------------------------------------------------------------------
# 2. GUIの設定とボタンの追加

root = None  # 後で初期化
start_button = None
stop_button = None
log_text = None
fps_label = None
connection_status_label = None # 追加: 接続ステータスラベル

is_running = False  # プロセスの状態管理をFalseで初期化
current_fps_setting = "1"  # デフォルトFPSをESP32側の初期値に合わせる
current_resolution = "160x120"  # デフォルト解像度をESP32側の初期値に合わせる

# ウィンドウサイズを記憶する変数
video_canvas_width = 800
video_canvas_height = 600

# -----------------------------------------------------------------------------
# リストで顔写真の保存時間を管理
unknown_face_times = []
max_unknown_faces_per_minute = 10

# 前回の画像送信の時刻を記録
last_send_time = 0

def on_resize(event):
    global video_canvas_width, video_canvas_height
    # ウィンドウのサイズに合わせて、動画表示領域の幅・高さを更新
    # ボタンやログのスペースを考慮してマージンを調整
    video_canvas_width = max(event.width - 350, 400)   # ボタンやログの幅を考慮
    video_canvas_height = max(event.height - 300, 300)  # ボタンやログの高さを考慮

# -----------------------------------------------------------------------------
# WebSocket接続管理クラス

class WebSocketClient:
    _instance = None  # クラス変数としてインスタンスを保持

    def __new__(cls, url, on_message, on_error, on_close, on_open):
        if cls._instance is None:
            cls._instance = super(WebSocketClient, cls).__new__(cls)
        return cls._instance

    def __init__(self, url, on_message, on_error, on_close, on_open):
        if hasattr(self, 'initialized') and self.initialized:
            return  # 既に初期化済み
        self.url = url
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.on_open = on_open
        self.ws = None
        self.thread = None
        self.stop_event = threading.Event()
        self.initialized = True

    def connect(self):
        if self.ws and self.ws.keep_running:
            logger.info("既にWebSocketクライアントが接続されています。")
            return
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        self.thread = threading.Thread(target=self.ws.run_forever)
        self.thread.daemon = True
        self.thread.start()
        logger.info("WebSocketクライアントを接続しました。")

    def send(self, message):
        if self.ws and self.ws.sock and self.ws.sock.connected:
            try:
                self.ws.send(message)
                logger.debug(f"WebSocketにメッセージを送信: {message}")
            except Exception as e:
                logger.error(f"WebSocket送信エラー: {e}")
        else:
            logger.warning("WebSocketが接続されていません。再接続を試みます。")
            self.connect()
            time.sleep(1)  # 再接続の待機
            self.send(message)

    def close(self):
        if self.ws:
            self.ws.close()
            logger.info("WebSocketクライアントを閉じました。")
        self.stop_event.set()
        if self.thread:
            self.thread.join()
            logger.info("WebSocketスレッドを終了しました。")

    def run(self):
        while not self.stop_event.is_set():
            if not (self.ws and self.ws.sock and self.ws.sock.connected):
                logger.warning("WebSocketが切断されました。再接続を試みます...")
                self.connect()
            time.sleep(5)

# -----------------------------------------------------------------------------
# 2. GUIの設定とボタンの追加

def setup_gui():
    global root, start_button, stop_button, log_text, fps_label, image_label, connection_status_label
    root = tk.Tk()
    root.title("ESP32-CAM 顔認証デモ")
    # ウィンドウサイズと初期位置（スクリーン中央に配置）
    root.geometry("1200x800")
    root.resizable(True, True)

    # ウィンドウ中央配置（Tk 8.6以降で使用可能）
    try:
        root.eval('tk::PlaceWindow . center')
    except:
        pass

    # リサイズイベントのバインド
    root.bind("<Configure>", on_resize)

    # メインフレーム（全体を囲む）
    main_frame = ttk.Frame(root, padding="10 10 10 10")
    main_frame.pack(fill=tk.BOTH, expand=True)

    # 左側のコントロールパネルフレーム
    control_panel_frame = ttk.Frame(main_frame, width=300)
    control_panel_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
    control_panel_frame.pack_propagate(False) # フレームのサイズが内容によって変わらないようにする

    # 右側の映像表示フレーム
    video_display_frame = ttk.Frame(main_frame)
    video_display_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

    # --- コントロールパネル内の要素 ---

    # 状態表示フレーム
    status_frame = ttk.LabelFrame(control_panel_frame, text="ステータス", padding="10")
    status_frame.pack(fill=tk.X, pady=(0, 10))

    # FPS表示ラベル
    fps_label = ttk.Label(status_frame, text="現在のFPS: 0.00", font=("Helvetica", 12))
    fps_label.pack(anchor=tk.W, pady=2)

    # 接続ステータスラベル（仮）
    connection_status_label = ttk.Label(status_frame, text="接続状態: 未接続", font=("Helvetica", 12))
    connection_status_label.pack(anchor=tk.W, pady=2)


    # 操作ボタンフレーム
    action_buttons_frame = ttk.LabelFrame(control_panel_frame, text="操作", padding="10")
    action_buttons_frame.pack(fill=tk.X, pady=(0, 10))

    # プロセス開始ボタン
    start_button = tk.Button(action_buttons_frame, text="開始", bg="green", fg="white", command=start_process, width=15, height=2)
    start_button.pack(fill=tk.X, pady=5)
    # プロセス停止ボタン
    stop_button = tk.Button(action_buttons_frame, text="停止", bg="red", fg="white", command=stop_process, width=15, height=2)
    stop_button.pack(fill=tk.X, pady=5)
    # 終了ボタン
    exit_button = tk.Button(action_buttons_frame, text="終了", bg="grey", fg="white", command=safe_exit, width=15, height=2)
    exit_button.pack(fill=tk.X, pady=5)

    # FPS設定フレーム
    fps_setting_frame = ttk.LabelFrame(control_panel_frame, text="FPS設定", padding="10")
    fps_setting_frame.pack(fill=tk.X, pady=(0, 10))

    # FPS選択用のRadiobutton
    global fps_var
    fps_var = tk.StringVar(value=current_fps_setting) # 初期値を設定
    fps_options = [
        ("1 FPS", "1"),
        ("5 FPS", "5"),
        ("10 FPS", "10"),
        ("20 FPS", "20"),
        ("30 FPS", "30")
    ]
    for label, fps_val in fps_options:
        rb = ttk.Radiobutton(fps_setting_frame, text=label, variable=fps_var, value=fps_val, command=lambda f=fps_val: set_fps(f))
        rb.pack(anchor=tk.W, pady=2)

    # 解像度設定フレーム
    resolution_setting_frame = ttk.LabelFrame(control_panel_frame, text="解像度設定", padding="10")
    resolution_setting_frame.pack(fill=tk.X, pady=(0, 10))

    # 解像度選択用のRadiobutton
    global resolution_var
    resolution_var = tk.StringVar(value=current_resolution) # 初期値を設定
    resolution_options = [
        ("160x120 (QQVGA)", "160x120"),
        ("176x144 (QCIF)", "176x144"),
        ("240x176 (HQVGA)", "240x176"),
        ("240x240", "240x240"),
        ("320x240 (QVGA)", "320x240"), # 追加
    ]
    for label, res_val in resolution_options:
        rb = ttk.Radiobutton(resolution_setting_frame, text=label, variable=resolution_var, value=res_val, command=lambda r=res_val: set_resolution(r))
        rb.pack(anchor=tk.W, pady=2)

    # ログ表示フレーム
    log_frame = ttk.LabelFrame(control_panel_frame, text="ログ", padding="10")
    log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 0)) # ログエリアが残りのスペースを埋めるように

    log_text = tk.Text(log_frame, state='disabled', wrap='word', font=("Meiryo", 10)) # フォントサイズ調整
    log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar = ttk.Scrollbar(log_frame, command=log_text.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    log_text['yscrollcommand'] = scrollbar.set

    # --- 映像表示フレーム内の要素 ---
    image_label = ttk.Label(video_display_frame, text="No frame", background="black")
    image_label.pack(expand=True, fill=tk.BOTH)
    root.image_label = image_label  # 参照保持

    # ログ設定の初期化
    with open('log_config.json', 'r', encoding='utf-8') as f:
        log_config = json.load(f)

    log_config['handlers']['tkinterHandler']['text_widget'] = log_text
    log_config['handlers']['tkinterHandler']['log_queue'] = log_queue
    logging.config.dictConfig(log_config)

    global logger
    logger = getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.info("GUIをセットアップしました。")

    # FPS表示の更新
    root.after(100, process_queues)

    # 初期ボタン状態の設定
    update_button_states()


# -----------------------------------------------------------------------------
# 3. WebSocketのイベントハンドラ

def on_message(ws_app, message):
    global latest_frame, frame_count, start_time, current_fps

    if isinstance(message, bytes):
        # フレームをバイナリ (JPEG) で受信 → NumPy配列へ
        # JPEGとしてデコード
        original_color_frame = cv2.imdecode(np.frombuffer(message, np.uint8), cv2.IMREAD_COLOR)
        if original_color_frame is not None:
            original_color_frame = cv2.rotate(original_color_frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
            
            # 顔認識処理はグレースケールで行い、検出結果の座標と名前を取得
            face_results = process_faces_and_get_coords(original_color_frame.copy()) # コピーを渡す

            # 元のカラーフレームに検出結果を描画
            frame_with_drawings = original_color_frame.copy()
            for (top, right, bottom, left), name in face_results:
                if name == "Unknown":
                    color = (0, 0, 255) # 赤
                else:
                    color = (0, 255, 0) # 緑
                cv2.rectangle(frame_with_drawings, (left, top), (right, bottom), color, 1)
                cv2.putText(frame_with_drawings, name, (left + 6, bottom + 12), cv2.FONT_HERSHEY_DUPLEX, 0.5, color, 1)

            with frame_lock:
                latest_frame = frame_with_drawings # 描画済みのカラーフレームをlatest_frameに設定
            
            frame_count += 1
            elapsed_time = time.time() - start_time
            if elapsed_time >= 1.0:
                current_fps = frame_count / elapsed_time
                frame_count = 0
                start_time = time.time()
                fps_queue.put(current_fps)  # 追加: FPSをfps_queueに送信

    elif isinstance(message, str):
        if message.startswith("from_esp32:"):
            logger.info(message)
        elif message.startswith("current_fps:"):
            try:
                fps_val = message.split(":")[1].strip()
                # GUIスレッドで更新するためにroot.afterを使用
                root.after(0, lambda: fps_var.set(fps_val)) 
                logger.info(f"ESP32からFPS設定を受信: {fps_val}")
            except IndexError:
                logger.warning(f"不正なFPSメッセージ形式: {message}")
        elif message.startswith("current_resolution:"):
            try:
                res_val = message.split(":")[1].strip()
                # GUIスレッドで更新するためにroot.afterを使用
                root.after(0, lambda: resolution_var.set(res_val))
                logger.info(f"ESP32から解像度設定を受信: {res_val}")
            except IndexError:
                logger.warning(f"不正な解像度メッセージ形式: {message}")
    else:
        logger.warning(f"Unknown message type: {type(message)}")

def on_error(ws_app, error):
    logger.error(f"WebSocketエラー: {error}")
    # エラー発生時もボタンの状態を更新
    root.after(0, update_button_states)
    root.after(0, lambda: connection_status_label.config(text="接続状態: エラー", fg="red"))


def on_close(ws_app, close_status_code, close_msg):
    logger.warning("WebSocket接続が切断されました。再接続を試みます。")
    # 接続切断時もボタンの状態を更新
    root.after(0, update_button_states)
    root.after(0, lambda: connection_status_label.config(text="接続状態: 切断", fg="orange"))


def on_open(ws_app):
    logger.info("WebSocketに接続しました。")
    # 接続成功時もボタンの状態を更新
    root.after(0, update_button_states)
    root.after(0, lambda: connection_status_label.config(text="接続状態: 接続済み", fg="green"))


# -----------------------------------------------------------------------------
# 4. WebSocketの設定とスレッド管理

websocket_client = None
websocket_lock = threading.Lock()  # WebSocketClientのインスタンス化を制御するロック

def start_websocket():
    global websocket_client
    with websocket_lock:
        if websocket_client is None:
            websocket_client = WebSocketClient(
                ws_url,
                on_message,
                on_error,
                on_close,
                on_open
            )
            websocket_client.connect()
            threading.Thread(target=websocket_client.run, daemon=True).start()
            logger.info("WebSocketクライアントを開始しました。")
        else:
            logger.info("WebSocketクライアントは既に起動しています。")

# -----------------------------------------------------------------------------
# 5. コマンド送信関数

def send_command(command):
    if websocket_client:
        websocket_client.send(command)
    else:
        logger.error("WebSocketクライアントが初期化されていません。")

# -----------------------------------------------------------------------------
# 6. プロセス開始/停止関数

def start_process():
    global is_running
    if not is_running:
        is_running = True
        logger.info("プロセスを開始します。")
        update_button_states() # ボタンの状態をすぐに更新
        start_websocket()  # WebSocketClientを開始
        send_command("start_stream")  # ESP32にストリーミング開始コマンドを送信
        # 画像更新をスケジュール
        update_image()
    else:
        logger.info("既にプロセスが実行中です。")


def stop_process():
    global is_running
    global websocket_client
    if is_running:
        is_running = False
        logger.info("プロセスを停止します。")
        update_button_states() # ボタンの状態をすぐに更新
        send_command("stop_stream")  # ESP32にストリーミング停止コマンドを送信
    else:
        logger.info("プロセスは既に停止しています。")

# -----------------------------------------------------------------------------
# ボタンの状態を更新する関数
def update_button_states():
    if is_running:
        start_button.config(state='disabled')
        stop_button.config(state='normal')
    else:
        start_button.config(state='normal')
        stop_button.config(state='disabled')

# -----------------------------------------------------------------------------
# 7. キューの管理(メインスレッドでやらないとSIGSEVするやつら)

def process_queues():
    while not log_queue.empty():
        log_entry = log_queue.get()
        log_text.config(state=tk.NORMAL)
        log_text.insert(tk.END, log_entry + "\n")
        log_text.yview(tk.END)  # スクロールを最新の行に移動
        log_text.config(state=tk.DISABLED)
    while not fps_queue.empty():
        fps = fps_queue.get()
        if fps_label:
            fps_label.config(text=f"現在のFPS: {fps:.2f}")

    root.after(100, process_queues)  # 100ミリ秒ごとにチェック

# -----------------------------------------------------------------------------
# 8. 顔認証用の既知顔データのロード

def load_known_faces():
    faces_dir = Config.FACES_DIR
    if not os.path.exists(faces_dir):
        os.makedirs(faces_dir)
        logger.info(f"ディレクトリ {faces_dir} を作成しました。")

    for filename in os.listdir(faces_dir):
        # "Unknown_" で始まるファイルはスキップする
        if filename.lower().startswith("unknown_"):
            logger.info(f"既知の顔として 'Unknown_' で始まるファイル '{filename}' をスキップしました。")
            continue

        if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
            # ファイル名から名前を抽出（例: name_hogehoge.jpg -> name）
            name_part = filename.split('_')[0]
            name = name_part if name_part else "Unknown"

            filepath = os.path.join(faces_dir, filename)
            try:
                img = face_recognition.load_image_file(filepath)
                encodings = face_recognition.face_encodings(img)
                if encodings:
                    known_face_encodings.append(encodings[0])
                    known_face_names.append(name)
                    logger.debug(f"ロード成功: {name} ({filename})")
                else:
                    logger.debug(f"顔が検出されませんでした: {filename}")
            except Exception as e:
                logger.error(f"ファイルの処理中にエラーが発生しました: {filename} - {e}")

    logger.info(f"Loaded {len(known_face_encodings)} known faces.")
    logger.debug(str(known_face_names))

# -----------------------------------------------------------------------------
# 9. 画像更新関数

def update_image():
    if not is_running:
        return
    with frame_lock:
        frame = latest_frame.copy() if latest_frame is not None else None

    if frame is not None:
        try:
            # image_labelの現在の幅と高さを取得
            # ウィンドウがまだ描画されていない場合は0になるため、デフォルト値を使用
            display_width = root.image_label.winfo_width()
            display_height = root.image_label.winfo_height()

            if display_width == 0: # 初期表示時など、まだ幅が確定していない場合
                display_width = 800 # デフォルト値
            if display_height == 0: # 初期表示時など、まだ高さが確定していない場合
                display_height = 600 # デフォルト値

            h, w = frame.shape[:2]
            
            # アスペクト比を維持しつつ、表示領域に収まるようにリサイズ
            scale_w = display_width / w
            scale_h = display_height / h
            scale = min(scale_w, scale_h)

            new_w = int(w * scale)
            new_h = int(h * scale)
            
            resized_frame = cv2.resize(frame, (new_w, new_h))

            # OpenCVのBGR画像をPILのRGBに変換
            rgb_frame_for_pil = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2RGB)
            imgtk = ImageTk.PhotoImage(image=Image.fromarray(rgb_frame_for_pil))

            # 画像をラベルに設定
            root.image_label.imgtk = imgtk  # 保持するための参照
            root.image_label.configure(image=imgtk)
        except Exception as e:
            logger.debug(f"画像更新中にエラー発生: {e}")

    root.after(10, update_image)

# -----------------------------------------------------------------------------
# 10. FPS設定関数

def set_fps(fps):
    global current_fps_setting
    current_fps_setting = fps
    logger.info(f"FPSを{fps}に設定しました。")
    send_command(f"SET_FPS:{fps}")

# -----------------------------------------------------------------------------
# 11. 解像度設定関数

def set_resolution(resolution):
    global current_resolution
    current_resolution = resolution
    logger.info(f"解像度を{resolution}に設定しました。")
    send_command(f"SET_RESOLUTION:{resolution}")

# -----------------------------------------------------------------------------
# 顔認識と未知顔保存機能の実装

# -----------------------------------------------------------------------------
# フィルタリング関数の定義

def convert_to_grayscale(frame):
    """フレームをグレースケールに変換する"""
    # 入力が既にグレースケールの場合、変換は不要
    if len(frame.shape) == 2 or frame.shape[2] == 1:
        return frame
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return gray

def remove_noise(frame):
    """ガウシアンブラーを使用してノイズを除去する"""
    denoised = cv2.GaussianBlur(frame, (5, 5), 0)
    return denoised

def histogram_equalization(frame):
    """ヒストグラム均等化を適用してコントラストを向上させる"""
    equalized = cv2.equalizeHist(frame)
    return equalized

def adjust_gamma(image, gamma=1.0):
    """ガンマ補正を適用する"""
    invGamma = 1.0 / gamma
    table = np.array([
        ((i / 255.0) ** invGamma) * 255
        for i in np.arange(0, 256)
    ]).astype("uint8")
    return cv2.LUT(image, table)

def preprocess_frame(frame):
    """顔認識用にフレームを前処理する"""
    # 入力フレームが既にグレースケールなので、convert_to_grayscaleは不要
    # ただし、念のため関数は残し、内部でチェックするように修正
    gray = convert_to_grayscale(frame) # ここで既にグレースケールであることを想定
    denoised = remove_noise(gray)
    equalized = histogram_equalization(denoised)
    gamma_corrected = adjust_gamma(equalized, gamma=1.5)
    return gamma_corrected


def process_faces_and_get_coords(frame_for_processing):
    global unknown_face_times
    
    # 顔認識はグレースケールで行うため、フレームをグレースケールに変換
    gray_frame = cv2.cvtColor(frame_for_processing, cv2.COLOR_BGR2GRAY)
    
    # 前処理を適用
    processed_frame = preprocess_frame(gray_frame)

    current_detected_names = set()
    face_detection_results = [] # 検出された顔の座標と名前を格納するリスト

    # face_recognitionはカラー画像を期待するため、グレースケールをBGRに変換
    color_for_dlib = cv2.cvtColor(processed_frame, cv2.COLOR_GRAY2BGR)

    face_locations = face_recognition.face_locations(color_for_dlib, model="hog")
    face_encodings = face_recognition.face_encodings(color_for_dlib, face_locations)

    for (top, right, bottom, left), face_encoding in zip(face_locations, face_encodings):
        matches = face_recognition.compare_faces(known_face_encodings, face_encoding, tolerance=Config.FACE_MATCH_THRESHOLD)
        name = "Unknown"

        # 顔が一致するか確認
        if known_face_encodings:
            face_distances = face_recognition.face_distance(known_face_encodings, face_encoding)
            best_match_index = np.argmin(face_distances)
            if matches[best_match_index]:
                name = known_face_names[best_match_index]
                current_detected_names.add(name)

        current_time = time.time()
        # Unknownの場合、画像を保存
        if name == "Unknown":
            # 1分以内に保存された未知の顔画像の数をカウント
            unknown_face_times = [t for t in unknown_face_times if t > current_time - 60]
            if len(unknown_face_times) < max_unknown_faces_per_minute:
                # 保存は元のカラーフレームのグレースケール版で行う
                save_unknown_face(gray_frame, (top, right, bottom, left))
                unknown_face_times.append(current_time)
        else:
            logger.info(f"顔を検出しました: {name}")

            if name in detected_counts:
                detected_counts[name] += 1
            else:
                detected_counts[name] = 1

            # NOTIFICATION_THRESHOLD が定義されていないため、コメントアウト
            # if detected_counts[name] == NOTIFICATION_THRESHOLD and name not in notified_names:
            #     send_line_notify(f"{name} さんが {detected_counts[name]} 回連続で検出されました。")
            #     notified_names.add(name)

        face_detection_results.append(((top, right, bottom, left), name))

    # 未検出の名前をリセット
    for name in detected_counts.keys():
        if name not in current_detected_names:
            detected_counts[name] = 0
    
    return face_detection_results # 検出された顔の座標と名前のリストを返す


def save_unknown_face(frame, face_coords):
    # トリムを行わず、全体のフレームを保存
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"Unknown_{timestamp}.jpg"
    path = os.path.join(Config.FACES_DIR, filename)
    # グレースケール画像を保存する場合、cv2.imwriteはそのまま保存できる
    cv2.imwrite(path, frame)  # フレーム全体を保存
    logger.info(f"未知の顔を保存しました: {filename}")


def safe_exit():
    logger.info("終了します。")
    stop_process()  # プロセスを停止し、WebSocketを閉じる
    # 終了ボタンが押された場合は、明示的にWebSocketクライアントを閉じる
    global websocket_client
    if websocket_client:
        websocket_client.close()
        websocket_client = None
    root.destroy()  # Tkinter GUIを閉じる

# -----------------------------------------------------------------------------
# 12. メイン処理

def start():
    setup_gui()
    load_known_faces()
    # WebSocket接続はstart_process()内で開始
    # update_image() はstart_process()内で開始
    root.mainloop()

if __name__ == "__main__":
    start()
