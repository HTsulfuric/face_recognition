#include <ESPAsyncWebServer.h>
#include <esp_camera.h>
#include <esp_log.h>

// Uncomment the appropriate camera model
#define CAMERA_MODEL_XIAO_ESP32S3 // Has PSRAM

#include "camera_pins.h" // Ensure this header defines the camera pin configuration

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

    isStreaming = true;
    // ここで接続通知を送信
    if (client->canSend()) {
      client->text("from_esp32: client connected");
    }
    break;

  case WS_EVT_DISCONNECT:
    Serial.printf("WebSocket client #%u disconnected\n", client->id());
    if (currentClient == client) {
      currentClient = nullptr;
      isStreaming = false;
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
          isStreaming = true;
          Serial.println("ストリーミングを開始します");
        }
      } else if (msg == "stop_stream") {
        if (isStreaming) {
          isStreaming = false;
          Serial.println("ストリーミングを停止します");
        }
        // 追加: 現在保持している映像を全て開放する
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) {
          esp_camera_fb_return(fb);
          Serial.println("保持中のフレームバッファを解放しました。");
        }
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

// ストリーム送信タスクの作成（スタックサイズを32768に設定）
void streamTask(void *parameter) {
  while (1) {
    if (isStreaming && currentClient != nullptr && currentClient->canSend()) {
      unsigned long current_time = millis();
      static unsigned long last_frame_time = 0;
      // FPS制御
      if (current_time - last_frame_time >= frame_interval_ms) {
        last_frame_time = current_time;
        camera_fb_t *fb = esp_camera_fb_get();
        if (!fb) {
          Serial.println("カメラフレームの取得に失敗しました");
          vTaskDelay(1000 / portTICK_PERIOD_MS);
          continue;
        }

        // JPEG形式の場合のみ送信
        if (fb->format == PIXFORMAT_JPEG) {
          currentClient->binary(fb->buf, fb->len);
        }

        esp_camera_fb_return(fb);
      }
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
