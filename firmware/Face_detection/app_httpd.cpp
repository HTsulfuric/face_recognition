#include <ESPAsyncWebServer.h>
#include <esp_camera.h>
#include <esp_log.h>

// Uncomment the appropriate camera model
#define CAMERA_MODEL_XIAO_ESP32S3 // Has PSRAM

#include "camera_pins.h" // Ensure this header defines the camera pin configuration

// init_camera関数を外部から呼び出せるように宣言
extern bool init_camera();
extern bool initialize_camera(); // initialize_cameraも外部から呼び出せるように宣言

AsyncWebServer server(80);
AsyncWebSocket ws("/stream");

AsyncWebSocketClient *currentClient = nullptr;

// FPS設定（デフォルト1FPS）
int current_fps = 1;
unsigned long frame_interval_ms = 1000 / current_fps;

bool isStreaming = false;

// 解像度設定（デフォルトは160*120）
String current_resolution = "160x120"; // 変更後

// 解像度を設定する関数
void set_resolution(const String &res) {
  current_resolution = res;
  Serial.printf("解像度が %s に設定されました。\n", current_resolution.c_str());

  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    if (current_resolution == "160x120") {
      s->set_framesize(s, FRAMESIZE_QQVGA); // 160x120
      Serial.println("解像度を 160x120 に設定しました。");
    } else if (current_resolution == "176x144") {
      s->set_framesize(s, FRAMESIZE_QCIF); // 176x144
      Serial.println("解像度を 176x144 に設定しました。");
    } else if (current_resolution == "240x176") {
      s->set_framesize(s, FRAMESIZE_HQVGA); // 240x160
      Serial.println("解像度を 240x160 に設定しました。");
    } else if (current_resolution == "240x240") {
      s->set_framesize(s, FRAMESIZE_240X240); // 240x240
      Serial.println("解像度を 240x240 に設定しました。");
    } else if (current_resolution == "320x240") { // QVGAを追加
      s->set_framesize(s, FRAMESIZE_QVGA); // 320x240
      Serial.println("解像度を 320x240 に設定しました。");
    } else {
      Serial.println("未対応の解像度が指定されました。");
    }
  }
}

// JPEG画質を設定する関数
void set_jpeg_quality(int quality) {
  if (quality < 10)
    quality = 10;
  if (quality > 100)
    quality = 100;
  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    s->set_quality(s, quality);
    Serial.printf("JPEG画質が %d に設定されました。\n", quality);
  }
}

void set_fps(int fps) {
  if (fps > 0 && fps <= 30) {
    current_fps = fps;
    frame_interval_ms = 1000 / current_fps;
    Serial.printf("FPSが %d に設定されました。\n", current_fps);
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
    const int MAX_DEINIT_RETRIES = 5; // 再試行回数を増やす
    const int DEINIT_RETRY_DELAY_MS = 50; // 再試行間の遅延

    // カメラが初期化されている場合のみデアセンブルを試みる
    // esp_camera_sensor_get() が NULL でないことを確認
    if (esp_camera_sensor_get() != NULL) {
        while (retry_count < MAX_DEINIT_RETRIES) {
            deinit_err = esp_camera_deinit();
            if (deinit_err == ESP_OK) {
                Serial.println("カメラを正常に停止しました。");
                break;
            } else {
                Serial.printf("カメラの停止に失敗しました。エラーコード: 0x%x (%s) - 再試行 %d/%d\n", deinit_err, esp_err_to_name(deinit_err), retry_count + 1, MAX_DEINIT_RETRIES);
                vTaskDelay(DEINIT_RETRY_DELAY_MS / portTICK_PERIOD_MS);
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
      client->text("current_resolution:" + current_resolution);
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
        int fps = msg.substring(8).toInt();
        set_fps(fps);
      } else if (msg.startsWith("SET_JPEG_QUALITY:")) {
        int quality = msg.substring(17).toInt();
        set_jpeg_quality(quality);
      } else if (msg.startsWith("SET_RESOLUTION:")) {
        String res = msg.substring(15);
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
            vTaskDelay(100 / portTICK_PERIOD_MS); // エラー時の待機
          } else {
            vTaskDelay(100 / portTICK_PERIOD_MS); // カメラが初期化されていない場合の短い待機
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
      vTaskDelay(100 / portTICK_PERIOD_MS); // 長めの待機
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
  xTaskCreatePinnedToCore(streamTask, "WebSocketStream", 16384, NULL, 1, NULL,
                          1);
}
