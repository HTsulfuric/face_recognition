#include <ESPAsyncWebServer.h>
#include <esp_camera.h>
#include <esp_log.h>

// Uncomment the appropriate camera model
#define CAMERA_MODEL_XIAO_ESP32S3 // Has PSRAM

#include "camera_pins.h" // Ensure this header defines the camera pin configuration

// init_camera関数を外部から呼び出せるように宣言
extern bool init_camera();
extern bool initialize_camera(); // initialize_cameraも外部から呼び出せるように宣言

// 定数定義
const int DEFAULT_FPS = 1;
const int MIN_FPS = 1;
const int MAX_FPS = 30;
const int MIN_JPEG_QUALITY = 10;
const int MAX_JPEG_QUALITY = 100;
const int STREAM_TASK_STACK_SIZE = 16384; // ストリームタスクのスタックサイズ
const int WS_RETRY_DELAY_MS = 100; // WebSocketエラー時の待機時間
const int CAMERA_DEINIT_MAX_RETRIES = 5; // カメラデアセンブルの最大再試行回数
const int CAMERA_DEINIT_RETRY_DELAY_MS = 50; // カメラデアセンブルの再試行間隔

// 解像度文字列とFRAMESIZEのマップ
struct ResolutionMap {
    const char* name;
    framesize_t size;
};

const ResolutionMap RESOLUTION_MAP[] = {
    {"160x120", FRAMESIZE_QQVGA},
    {"176x144", FRAMESIZE_QCIF},
    {"240x176", FRAMESIZE_HQVGA},
    {"240x240", FRAMESIZE_240X240},
    {"320x240", FRAMESIZE_QVGA}
};
const size_t RESOLUTION_MAP_SIZE = sizeof(RESOLUTION_MAP) / sizeof(RESOLUTION_MAP[0]);


AsyncWebServer server(80);
AsyncWebSocket ws("/stream");

AsyncWebSocketClient *currentClient = nullptr;

// FPS設定
int current_fps = DEFAULT_FPS;
unsigned long frame_interval_ms = 1000 / current_fps;

bool isStreaming = false;

// 解像度設定
String current_resolution_str = "160x120"; // 変更後

// 解像度を設定する関数
void set_resolution(const String &res_str) {
  current_resolution_str = res_str;
  Serial.printf("解像度が %s に設定されました。\n", current_resolution_str.c_str());

  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    framesize_t target_framesize = FRAMESIZE_INVALID;
    for (size_t i = 0; i < RESOLUTION_MAP_SIZE; ++i) {
        if (res_str == RESOLUTION_MAP[i].name) {
            target_framesize = RESOLUTION_MAP[i].size;
            break;
        }
    }

    if (target_framesize != FRAMESIZE_INVALID) {
        s->set_framesize(s, target_framesize);
        Serial.printf("解像度を %s に設定しました。\n", res_str.c_str());
    } else {
        Serial.printf("未対応の解像度が指定されました: %s\n", res_str.c_str());
    }
  }
}

// JPEG画質を設定する関数
void set_jpeg_quality(int quality) {
  if (quality < MIN_JPEG_QUALITY)
    quality = MIN_JPEG_QUALITY;
  if (quality > MAX_JPEG_QUALITY)
    quality = MAX_JPEG_QUALITY;
  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    s->set_quality(s, quality);
    Serial.printf("JPEG画質が %d に設定されました。\n", quality);
  }
}

void set_fps(int fps) {
  if (fps >= MIN_FPS && fps <= MAX_FPS) {
    current_fps = fps;
    frame_interval_ms = 1000 / current_fps;
    Serial.printf("FPSが %d に設定されました。\n", current_fps);
  } else {
    Serial.printf("無効なFPS値が指定されました: %d (許容範囲: %d-%d)\n", fps, MIN_FPS, MAX_FPS);
  }
}

// カメラを停止し、フレームバッファを解放するヘルパー関数
void stop_camera_and_free_fb() {
    Serial.println("カメラ停止処理を開始します。");
    // 保持中のフレームバッファを全て解放する
    camera_fb_t *fb = NULL;
    int freed_count = 0;
    // esp_camera_fb_get() は新しいフレームを取得しようとするため、
    // キューに残っているバッファを消費し、解放する。
    // タイムアウトを設定し、無限ループにならないようにする
    unsigned long start_free_time = millis();
    const unsigned long MAX_FREE_WAIT_MS = 500; // 最大500ms待機

    while (millis() - start_free_time < MAX_FREE_WAIT_MS) {
        fb = esp_camera_fb_get();
        if (fb) {
            esp_camera_fb_return(fb);
            freed_count++;
            // Serial.printf("保持中のフレームバッファを解放しました。解放数: %d\n", freed_count);
        } else {
            // フレームバッファがもうない場合
            break;
        }
        vTaskDelay(10 / portTICK_PERIOD_MS); // 短い遅延を入れて、他のタスクにCPUを譲る
    }
    Serial.printf("合計 %d 個のフレームバッファを解放しました。\n", freed_count);


    // カメラのデアセンブルを試行（失敗したら再試行）
    esp_err_t deinit_err = ESP_OK;
    int retry_count = 0;
    
    // カメラが初期化されている場合のみデアセンブルを試みる
    // esp_camera_sensor_get() が NULL でないことを確認
    if (esp_camera_sensor_get() != NULL) {
        while (retry_count < CAMERA_DEINIT_MAX_RETRIES) {
            deinit_err = esp_camera_deinit();
            if (deinit_err == ESP_OK) {
                Serial.println("カメラを正常に停止しました。");
                break;
            } else {
                Serial.printf("カメラの停止に失敗しました。エラーコード: 0x%x (%s) - 再試行 %d/%d\n", deinit_err, esp_err_to_name(deinit_err), retry_count + 1, CAMERA_DEINIT_MAX_RETRIES);
                vTaskDelay(CAMERA_DEINIT_RETRY_DELAY_MS / portTICK_PERIOD_MS);
                retry_count++;
            }
        }
        if (deinit_err != ESP_OK) {
            Serial.println("カメラの停止に複数回失敗しました。強制終了します。");
        }
    } else {
        Serial.println("カメラは既に停止しているか、初期化されていません。デアセンブルは不要です。");
    }
}


// WebSocketイベントハンドラ
void onWsEvent(AsyncWebSocket *server, AsyncWebSocketClient *client,
               AwsEventType type, void *arg, uint8_t *data, size_t len) {
  switch (type) {
  case WS_EVT_CONNECT:
    Serial.printf("WebSocket client #%u connected from %s\n", client->id(),
                  client->remoteIP().toString().c_str());

    if (currentClient != nullptr && currentClient != client) {
      Serial.printf("既存のクラインアント #%u を切断します。\n",
                    currentClient->id());
      currentClient->close();
    }
    currentClient = client;

    // 接続時にストリーミングは開始しない。start_streamコマンドを待つ
    // ここで接続通知を送信
    if (client->canSend()) {
      client->text("from_esp32: client connected");
      // 現在のFPSと解像度を通知
      client->text("current_fps:" + String(current_fps));
      client->text("current_resolution:" + current_resolution_str);
    }
    break;

  case WS_EVT_DISCONNECT:
    Serial.printf("WebSocket client #%u disconnected\n", client->id());
    if (currentClient == client) {
      currentClient = nullptr;
      isStreaming = false;
      // クライアント切断時にカメラを停止
      stop_camera_and_free_fb(); // ヘルパー関数を呼び出す
    }
    break;

  case WS_EVT_DATA: {
    AwsFrameInfo *info = (AwsFrameInfo *)arg; // 変数宣言
    if (info->opcode == WS_TEXT) {
      String msg = "";
      for (size_t i = 0; i < info->len; i++) {
        msg += (char)data[i];
      }
      Serial.printf("Received message: %s\n", msg.c_str());

      // メッセージのパースと処理
      if (msg.startsWith("SET_FPS:")) {
        int fps = msg.substring(strlen("SET_FPS:")).toInt();
        set_fps(fps);
      } else if (msg.startsWith("SET_JPEG_QUALITY:")) {
        int quality = msg.substring(strlen("SET_JPEG_QUALITY:")).toInt();
        set_jpeg_quality(quality);
      } else if (msg.startsWith("SET_RESOLUTION:")) {
        String res = msg.substring(strlen("SET_RESOLUTION:"));
        // 解像度設定の処理
        set_resolution(res);
      } else if (msg == "start_stream") {
        if (!isStreaming) {
          Serial.println("ストリーミング開始コマンドを受信しました。");
          if (esp_camera_sensor_get() == NULL) {
            Serial.println("カメラが停止しているため再初期化を試みます。");
            if (!initialize_camera()) { // initialize_camera() を呼び出す
              Serial.println("カメラの再初期化に失敗しました。");
              // エラー処理
              if (client->canSend()) {
                client->text("from_esp32: camera_reinit_failed");
              }
              return;
            }
          } else {
            Serial.println("カメラは既に初期化されています。");
          }
          isStreaming = true;
          Serial.println("ストリーミングを開始します");
        } else {
          Serial.println("既にストリーミング中です。");
        }
      } else if (msg == "stop_stream") {
        if (isStreaming) {
          isStreaming = false;
          Serial.println("ストリーミングを停止します");
        }
        // ストリーム停止時もカメラを停止する
        stop_camera_and_free_fb(); // ヘルパー関数を呼び出す
      }
    }
    break;
  }

  case WS_EVT_PONG:
    Serial.println("Received PONG");
    break;

  case WS_EVT_ERROR:
    Serial.println("WebSocket ERROR");
    break;

  default:
    break;
  }
}

// ストリーム送信タスクの作成
void streamTask(void *parameter) {
  static bool last_fb_get_failed = false; // 前回のフレーム取得が失敗したかどうかのフラグ

  while (1) {
    if (isStreaming && currentClient != nullptr && currentClient->canSend()) {
      unsigned long current_time = millis();
      static unsigned long last_frame_time = 0;
      // FPS制御
      if (current_time - last_frame_time >= frame_interval_ms) {
        last_frame_time = current_time;
        camera_fb_t *fb = esp_camera_fb_get();
        if (!fb) {
          if (!last_fb_get_failed) { // 連続エラーの場合は初回のみログ出力
            Serial.println("カメラフレームの取得に失敗しました (esp_camera_fb_get)");
            last_fb_get_failed = true;
          }
          if (currentClient != nullptr && currentClient->canSend()) {
            currentClient->text("error:frame_capture_failed"); // エラーメッセージを送信
            Serial.println("クライアントに'error:frame_capture_failed'を送信しました。");
          }
          // カメラが停止している場合は、エラーメッセージを繰り返さないようにする
          if (esp_camera_sensor_get() != NULL) { // カメラが初期化されている場合のみ待機
            vTaskDelay(WS_RETRY_DELAY_MS / portTICK_PERIOD_MS); // エラー時の待機
          } else {
            vTaskDelay(WS_RETRY_DELAY_MS / portTICK_PERIOD_MS); // カメラが初期化されていない場合の短い待機
          }
          continue;
        }
        last_fb_get_failed = false; // 成功したらフラグをリセット

        // JPEG形式の場合のみ送信
        if (fb->format == PIXFORMAT_JPEG) {
          currentClient->binary(fb->buf, fb->len);
        }

        esp_camera_fb_return(fb);
      }
    }
    // ストリーミング中でない場合やクライアントがいない場合は、CPUを解放するために待機
    else {
      vTaskDelay(WS_RETRY_DELAY_MS / portTICK_PERIOD_MS); // 長めの待機
    }
    // 他のタスクが稼働できるように軽い遅延
    vTaskDelay(10 / portTICK_PERIOD_MS);
  }
}

// カメラサーバーの開始関数
void startCameraServer() {
  // WebSocketのイベント登録
  ws.onEvent(onWsEvent);
  server.addHandler(&ws);

  // HTTPサーバーのルート設定
  server.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->send_P(200, "text/html",
                    "<!DOCTYPE html><html><head><title>ESP32-CAM</title></head>"
                    "<body><h1>ESP32-CAM WebSocket Stream</h1>"
                    "<img src=\"/stream\"/></body></html>");
  });

  // ストリームルートの設定
  server.on("/stream", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->send(200, "text/plain", "WebSocket stream");
  });

  // HTTPサーバーの開始
  server.begin();
  Serial.println("HTTPサーバーを開始しました");

  // ストリームタスクの作成
  xTaskCreatePinnedToCore(streamTask, "WebSocketStream", STREAM_TASK_STACK_SIZE, NULL, 1, NULL,
                          1);
}
