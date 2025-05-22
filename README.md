# ESP32-CAM 顔認証システム

このプロジェクトは、ESP32-CAMを使用したリアルタイム顔認証システムです。WebSocketを通じてESP32-CAMから映像を取得し、OpenCVとface_recognitionライブラリを使用して顔検出と認証を行います。

## 主な機能

- ESP32-CAMからのリアルタイム映像取得
- 顔検出と認証
- 未知の顔の検出と保存
- LINE Notifyによる通知機能
- カスタマイズ可能なFPS設定
- 解像度調整機能
- グラフィカルユーザーインターフェース（GUI）

## 必要条件

- Python 3.8以上
- ESP32-CAM
- Arduino IDE（ESP32-CAMのファームウェア開発用）
- インターネット接続
- LINE Notifyトークン（通知機能を使用する場合）

## インストール方法

### ESP32-CAMのセットアップ

1. Arduino IDEをインストール
2. ESP32ボードサポートを追加
3. 必要なライブラリをインストール:
   - ESP32 Camera
   - WebSockets
4. `firmware/Face_detection.ino`を開く
5. ESP32-CAMの設定を確認:
   - WiFiの設定
   - カメラの設定
6. スケッチをESP32-CAMにアップロード

### Pythonアプリケーションのセットアップ

1. リポジトリのクローン:
```bash
git clone [リポジトリURL]
cd face_detection
```

2. 仮想環境の作成と有効化:
```bash
python -m venv myenv
source myenv/bin/activate  # Linux/macOSの場合
# または
myenv\Scripts\activate  # Windowsの場合
```

3. 必要なパッケージのインストール:
```bash
pip install -r requirements.txt
pip install git+https://github.com/ageitgey/face_recognition_models
```

4. 環境変数の設定:
プロジェクトのルートディレクトリに`.env`ファイルを作成し、以下の内容を設定:
```
WS_URL=ws://[ESP32-CAMのIPアドレス]:8080
LINE_NOTIFY_TOKEN=your_token_here
FACES_DIR=./resources/faces
FACE_MATCH_THRESHOLD=0.5
```

## 使用方法

1. ESP32-CAMの起動:
   - ESP32-CAMに電源を接続
   - シリアルモニタでIPアドレスを確認

2. Pythonアプリケーションの起動:
```bash
python src/main.py
```

3. GUIの操作:
   - 「開始」ボタン: 顔認証処理を開始
   - 「停止」ボタン: 処理を一時停止
   - FPS設定: 1, 5, 10, 20, 30 FPSから選択可能
   - 解像度設定: 利用可能な解像度から選択

## 顔認証の設定

1. 既知の顔の登録:
   - `resources/faces/`ディレクトリに認識させたい人物の写真を配置
   - 写真のファイル名が人物の名前として使用されます

2. 未知の顔の処理:
   - 未知の顔が検出された場合、自動的に`resources/faces/`ディレクトリに保存
   - LINE Notifyが設定されている場合、通知が送信されます

## 注意事項

- 顔認証の精度は照明条件やカメラの角度に依存します
- 大量の未知の顔が検出された場合、LINE Notifyの通知が頻繁に送信される可能性があります
- 初回起動時は、必要なファイルのダウンロードに時間がかかる場合があります

## トラブルシューティング

1. WebSocket接続エラー:
   - ESP32-CAMのIPアドレスが正しいか確認
   - ネットワーク接続を確認
   - ESP32-CAMが正常に動作しているか確認

2. 顔検出が動作しない:
   - 照明条件を確認
   - カメラの角度を調整
   - 解像度設定を確認

## プロジェクト構造

```
face_detection/
├── src/                    # ソースコード
│   ├── main.py             # メインプログラム
│   └── logging_handlers.py # ログハンドラー
├── firmware/              # ESP32-CAMファームウェア
│   ├── Face_detection.ino # メインスケッチ
│   ├── app_httpd.cpp     # HTTPサーバー実装
│   ├── camera_pins.h     # カメラ設定
│   └── ci.json           # 設定ファイル
├── resources/             # リソースファイル
│   ├── faces/            # 顔画像保存ディレクトリ
│   └── models/           # モデルファイル
│       └── haarcascade_frontalface_default.xml
├── requirements.txt        # 依存パッケージ一覧
├── .env                    # 環境変数設定
├── log_config.json         # ログ設定
└── README.md              # プロジェクト説明
```

## ライセンス

このプロジェクトはMITライセンスの下で公開されています。
