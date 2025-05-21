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

is_running = True  # プロセスの状態管理
current_fps_setting = "10"  # デフォルトFPS
current_resolution = "240x176"  # デフォルト解像度

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
            # log_message("既にWebSocketクライアントが接続されています。")
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
        # log_message("WebSocketクライアントを接続しました。")
        logger.info("WebSocketクライアントを接続しました。")

    def send(self, message):
        if self.ws and self.ws.sock and self.ws.sock.connected:
            try:
                self.ws.send(message)
                # log_message(f"WebSocketにメッセージを送信: {message}")
                logger.debug(f"WebSocketにメッセージを送信: {message}")
            except Exception as e:
                # log_message(f"WebSocket送信エラー: {e}")
                logger.error(f"WebSocket送信エラー: {e}")
        else:
            # log_message("WebSocketが接続されていません。再接続を試みます。")
            logger.warning("WebSocketが接続されていません。再接続を試みます。")
            self.connect()
            time.sleep(1)  # 再接続の待機
            self.send(message)

    def close(self):
        if self.ws:
            self.ws.close()
            # log_message("WebSocketクライアントを閉じました。")
            logger.info("WebSocketクライアントを閉じました。")
        self.stop_event.set()
        if self.thread:
            self.thread.join()
            # log_message("WebSocketスレッドを終了しました。")
            logger.info("WebSocketスレッドを終了しました。")

    def run(self):
        while not self.stop_event.is_set():
            if not (self.ws and self.ws.sock and self.ws.sock.connected):
                # log_message("WebSocketが切断されました。再接続を試みます...")
                logger.warning("WebSocketが切断されました。再接続を試みます...")
                self.connect()
            time.sleep(5)

# -----------------------------------------------------------------------------
# 2. GUIの設定とボタンの追加

def setup_gui():
    global root, start_button, stop_button, log_text, fps_label, image_label
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

    # 上部フレーム（FPS表示とボタン類）
    top_frame = ttk.Frame(root)
    top_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)

    # FPS表示ラベル
    global fps_label
    fps_label = ttk.Label(top_frame, text="現在のFPS: 0")
    fps_label.pack(side=tk.LEFT, padx=10)

    # プロセス開始ボタン
    global start_button, stop_button
    start_button = tk.Button(top_frame, text="開始", bg="green", command=start_process, width=10, state='disabled')
    start_button.pack(side=tk.LEFT, padx=10)
    stop_button = tk.Button(top_frame, text="停止", bg="red", command=stop_process, width=10)
    stop_button.pack(side=tk.LEFT, padx=10)

    # Exitボタンの追加
    exit_button = tk.Button(top_frame, text="終了", bg="grey", command=safe_exit, width=10)
    exit_button.pack(side=tk.LEFT, padx=10)

    # FPS設定ボタンのフレーム
    fps_frame = ttk.LabelFrame(top_frame, text="FPS設定")
    fps_frame.pack(side=tk.LEFT, padx=20)

    fps_options = [
        ("1 FPS", "1"),
        ("5 FPS", "5"),
        ("10 FPS", "10"),
        ("20 FPS", "20"),
        ("30 FPS", "30")
    ]

    for label, fps in fps_options:
        btn = ttk.Button(fps_frame, text=label, command=lambda f=fps: set_fps(f))
        btn.pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)

    # 解像度設定フレーム
    resolution_change_frame = ttk.LabelFrame(top_frame, text="解像度設定")
    resolution_change_frame.pack(side=tk.LEFT, padx=20)

    resolution_options = [
        ("160x120", "160x120"),
        ("176x144", "176x144"),
        ("240x176", "240x176"),
        ("240x240", "240x240"),
        ("320x240", "320x240"),
    ]

    for label, res in resolution_options:
        btn = ttk.Button(resolution_change_frame, text=label, command=lambda r=res: set_resolution(r))
        btn.pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)

    # 映像表示フレーム（中央部分を広く取る）
    video_frame = ttk.Frame(root)
    video_frame.pack(side=tk.TOP, expand=True, fill=tk.BOTH, padx=10, pady=5)

    # 画像表示ラベル（中央フレームに配置）
    image_label = ttk.Label(video_frame, text="No frame", background="black")
    image_label.pack(expand=True, fill=tk.BOTH)
    root.image_label = image_label  # 参照保持

    # 下部フレーム（ログなど）
    bottom_frame = ttk.Frame(root)
    bottom_frame.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, padx=10, pady=5)

    # ログ表示（下部フレーム）
    global log_text
    log_text = tk.Text(bottom_frame, state='disabled', wrap='word')
    log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar = ttk.Scrollbar(bottom_frame, command=log_text.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    log_text['yscrollcommand'] = scrollbar.set

    with open('log_config.json', 'r', encoding='utf-8') as f:
        log_config = json.load(f)

    # カスタムハンドラに必要な引数を渡す
    log_config['handlers']['tkinterHandler']['text_widget'] = log_text
    log_config['handlers']['tkinterHandler']['log_queue'] = log_queue

    # ログ設定の適用
    logging.config.dictConfig(log_config)

    # ロガーの取得
    global logger
    logger = getLogger(__name__)

    logger.setLevel(logging.INFO)
    logger.info("プロセスを開始します。")

    # FPS表示の更新
    root.after(100, process_queues)  # 変更: process_queuesを定期的に呼び出す

# -----------------------------------------------------------------------------
# 3. WebSocketのイベントハンドラ

def on_message(ws_app, message):
    global latest_frame, frame_count, start_time, current_fps

    if isinstance(message, bytes):
        # フレームをバイナリ (JPEG) で受信 → NumPy配列へ
        # JPEGとしてデコード
        np_arr = np.frombuffer(message, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is not None:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
            
            # 顔認識処理と描画
            processed_frame_with_faces = recognize_faces_and_draw(frame.copy()) # 描画済みのカラーフレームを取得

            with frame_lock:
                latest_frame = processed_frame_with_faces # 描画済みのカラーフレームをlatest_frameに設定
            
            frame_count += 1
            elapsed_time = time.time() - start_time
            if elapsed_time >= 1.0:
                current_fps = frame_count / elapsed_time
                frame_count = 0
                start_time = time.time()
                fps_queue.put(current_fps)  # 追加: FPSをfps_queueに送信

    elif isinstance(message, str):
        if message.startswith("from_esp32:"):
            # log_message(message)
            logger.info(message)
    else:
        # log_message(f"Unknown message type: {type(message)}")
        logger.warning(f"Unknown message type: {type(message)}")

def on_error(ws_app, error):
    # log_message(f"WebSocketエラー: {error}")
    logger.error(f"WebSocketエラー: {error}")

def on_close(ws_app, close_status_code, close_msg):
    # log_message("WebSocket接続が切断されました。再接続を試みます。")
    logger.warning("WebSocket接続が切断されました。再接続を試みます。")

def on_open(ws_app):
    # log_message("WebSocketに接続しました。")
    logger.info("WebSocketに接続しました。")

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
            # log_message("WebSocketクライアントを開始しました。")
            logger.info("WebSocketクライアントを開始しました。")
        else:
            # log_message("WebSocketクライアントは既に起動しています。")
            logger.info("WebSocketクライアントは既に起動しています。")

# -----------------------------------------------------------------------------
# 5. コマンド送信関数

def send_command(command):
    if websocket_client:
        websocket_client.send(command)
    else:
        # log_message("WebSocketクライアントが初期化されていません。")
        logger.error("WebSocketクライアントが初期化されていません。")

# -----------------------------------------------------------------------------
# 6. プロセス開始/停止関数

def start_process():
    global is_running
    if not is_running:
        is_running = True
        # log_message("プロセスを開始します。")
        logger.info("プロセスを開始します。")
        start_websocket()  # WebSocketClientを開始
        send_command("start_stream")  # ESP32にストリーミング開始コマンドを送信
        # 画像更新をスケジュール
        update_image()
        # ボタンの状態を更新
        start_button.config(state='disabled')
        stop_button.config(state='normal')

def stop_process():
    global is_running
    global websocket_client
    if is_running:
        is_running = False
        # log_message("プロセスを停止します。")
        logger.info("プロセスを停止します。")
        send_command("stop_stream")  # ESP32にストリーミング停止コマンドを送信
        # WebSocketクライアントを閉じる
        if websocket_client:
            websocket_client.close()
            websocket_client = None
        # ボタンの状態を更新
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
        # log_message(f"ディレクトリ {faces_dir} を作成しました。")
        logger.info(f"ディレクトリ {faces_dir} を作成しました。")

    for filename in os.listdir(faces_dir):
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
                    # log_message(f"ロード成功: {name} ({filename})")
                    logger.debug(f"ロード成功: {name} ({filename})")
                else:
                    # log_message(f"顔が検出されませんでした: {filename}")
                    logger.debug(f"顔が検出されませんでした: {filename}")
            except Exception as e:
                # log_message(f"ファイルの処理中にエラーが発生しました: {filename} - {e}")
                logger.error(f"ファイルの処理中にエラーが発生しました: {filename} - {e}")

    # log_message(f"Loaded {len(known_face_encodings)} known faces.")
    logger.info(f"Loaded {len(known_face_encodings)} known faces.")
    # log_message(str(known_face_names))
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
            # ウィンドウ幅・高さに合わせてリサイズ（video_canvas_width / video_canvas_height）を利用
            h, w = frame.shape[:2]
            scale = min(video_canvas_width / float(w), video_canvas_height / float(h))
            new_w = int(w * scale)
            new_h = int(h * scale)
            resized_frame = cv2.resize(frame, (new_w, new_h))

            # latest_frameは既にカラー画像なので、そのままImage.fromarrayに渡す
            rgb_frame = Image.fromarray(resized_frame).convert('RGB')
            imgtk = ImageTk.PhotoImage(image=rgb_frame)

            # 画像をラベルに設定
            root.image_label.imgtk = imgtk  # 保持するための参照
            root.image_label.configure(image=imgtk)
        except Exception as e:
            # log_message(f"画像更新中にエラー発生: {e}")
            logger.debug(f"画像更新中にエラー発生: {e}")

    root.after(10, update_image)

# -----------------------------------------------------------------------------
# 10. FPS設定関数

def set_fps(fps):
    global current_fps_setting
    current_fps_setting = fps
    # log_message(f"FPSを{fps}に設定しました。")
    logger.info(f"FPSを{fps}に設定しました。")
    send_command(f"SET_FPS:{fps}")

# -----------------------------------------------------------------------------
# 11. 解像度設定関数

def set_resolution(resolution):
    global current_resolution
    current_resolution = resolution
    # log_message(f"解像度を{resolution}に設定しました。")
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


def recognize_faces_and_draw(frame_to_draw):
    global unknown_face_times

    # 顔認識はグレースケールで行うため、フレームをグレースケールに変換
    gray_frame = cv2.cvtColor(frame_to_draw, cv2.COLOR_BGR2GRAY)
    
    # 前処理を適用
    processed_frame = preprocess_frame(gray_frame)

    current_detected_names = set()

    # face_recognitionはカラー画像を期待するため、グレースケールをBGRに変換
    color_for_dlib = cv2.cvtColor(processed_frame, cv2.COLOR_GRAY2BGR)

    face_locations = face_recognition.face_locations(color_for_dlib, model="cnn")
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

            # 描画は元のカラーフレームに行う
            cv2.rectangle(frame_to_draw, (left, top), (right, bottom), (0, 0, 255), 1)  # 赤枠
            cv2.putText(frame_to_draw, name, (left + 6, bottom + 12), cv2.FONT_HERSHEY_DUPLEX, 0.5, (0, 0, 255), 1)  # 赤文字
        else:
            logger.info(f"顔を検出しました: {name}")
            # 描画は元のカラーフレームに行う
            cv2.rectangle(frame_to_draw, (left, top), (right, bottom), (0, 255, 0), 1) # 緑枠
            cv2.putText(frame_to_draw, name, (left + 6, bottom + 12), cv2.FONT_HERSHEY_DUPLEX, 0.5, (0, 255, 0), 1)  # 緑文字

            if name in detected_counts:
                detected_counts[name] += 1
            else:
                detected_counts[name] = 1

            # NOTIFICATION_THRESHOLD が定義されていないため、コメントアウト
            # if detected_counts[name] == NOTIFICATION_THRESHOLD and name not in notified_names:
            #     send_line_notify(f"{name} さんが {detected_counts[name]} 回連続で検出されました。")
            #     notified_names.add(name)

    # 未検出の名前をリセット
    for name in detected_counts.keys():
        if name not in current_detected_names:
            detected_counts[name] = 0
    
    return frame_to_draw # 描画済みのカラーフレームを返す


def save_unknown_face(frame, face_coords):
    # トリムを行わず、全体のフレームを保存
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"Unknown_{timestamp}.jpg"
    path = os.path.join(Config.FACES_DIR, filename)
    # グレースケール画像を保存する場合、cv2.imwriteはそのまま保存できる
    cv2.imwrite(path, frame)  # フレーム全体を保存
    # log_message(f"未知の顔を保存しました: {filename}")
    logger.info(f"未知の顔を保存しました: {filename}")


def safe_exit():
    # log_message("終了します。")
    logger.info("終了します。")
    stop_process()  # プロセスを停止し、WebSocketを閉じる
    root.destroy()  # Tkinter GUIを閉じる

# -----------------------------------------------------------------------------
# 12. メイン処理

def start():
    setup_gui()
    load_known_faces()
    start_websocket()      # WebSocket接続を開始
    update_image()         # 画像更新処理を開始
    root.mainloop()

if __name__ == "__main__":
    start()
