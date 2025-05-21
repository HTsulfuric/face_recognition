// face_detection.ino
#include <esp_camera.h>
#include <WiFi.h>

#define CAMERA_MODEL_XIAO_ESP32S3  // Has PSRAM
#include "camera_pins.h"

void startCameraServer();
// init_camera関数を外部（app_httpd.cpp）から呼び出せるようにプロトタイプ宣言
bool init_camera();

const char SSID[] = "Your_SSID";  // WiFiのSSID
const char password[] = "Your_Password";  // WiFiのパスワード

// 最大再試行回数
#define MAX_WIFI_RETRIES 20
#define WIFI_RETRY_DELAY 500  // ミリ秒単位

// カメラ初期化の再試行回数
#define MAX_CAMERA_INIT_RETRIES 5
#define CAMERA_INIT_DELAY 2000  // ミリ秒単位

// シリアル通信の初期化設定
#define BAUD_RATE 115200


// WiFi接続関数
bool connect_wifi() {
  int retry_count = 0;
  WiFi.begin(SSID, password);
  Serial.print("WiFi接続中");
  while (WiFi.status() != WL_CONNECTED && retry_count < MAX_WIFI_RETRIES) {
    delay(WIFI_RETRY_DELAY);
    Serial.print(".");
    retry_count++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFiに接続しました");
    Serial.print("IPアドレス: ");
    Serial.println(WiFi.localIP());
    delay(100);
    return true;
  } else {
    Serial.println("\nWiFi接続に失敗しました");
    return false;
  }
}

// カメラ初期化関数（再試行付き）
bool initialize_camera() {
  int retry_count = 0;
  while (retry_count < MAX_CAMERA_INIT_RETRIES) {
    if (init_camera()) {
      Serial.println("カメラが正常に初期化されました");
      return true;
    } else {
      Serial.println("カメラ初期化に失敗。再試行します...");
      retry_count++;
      delay(CAMERA_INIT_DELAY);
    }
  }
  Serial.println("カメラの初期化に複数回失敗しました。システムをリセットします。");
  ESP.restart();  // システムリセット
  return false;  // この行は通常実行されません
}

// カメラの初期化関数
bool init_camera() {
    camera_config_t config;
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer = LEDC_TIMER_0;
    config.pin_d0 = Y2_GPIO_NUM;
    config.pin_d1 = Y3_GPIO_NUM;
    config.pin_d2 = Y4_GPIO_NUM;
    config.pin_d3 = Y5_GPIO_NUM;
    config.pin_d4 = Y6_GPIO_NUM;
    config.pin_d5 = Y7_GPIO_NUM;
    config.pin_d6 = Y8_GPIO_NUM;
    config.pin_d7 = Y9_GPIO_NUM;
    config.pin_xclk = XCLK_GPIO_NUM;
    config.pin_pclk = PCLK_GPIO_NUM;
    config.pin_vsync = VSYNC_GPIO_NUM;
    config.pin_href = HREF_GPIO_NUM;
    config.pin_sscb_sda = SIOD_GPIO_NUM;
    config.pin_sscb_scl = SIOC_GPIO_NUM;
    config.pin_pwdn = PWDN_GPIO_NUM;
    config.pin_reset = RESET_GPIO_NUM;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;
    config.fb_location = CAMERA_FB_IN_PSRAM;

    // フレームサイズやバッファ数はPSRAMの有無で調整
    if(psramFound()){
        Serial.println("PSRAM is found");
        config.frame_size = FRAMESIZE_HQVGA;
        config.jpeg_quality = 10; // JPEG品質を再設定
        config.fb_count = 3; // PSRAM使用時はフレームバッファを3つにする これはPSRAMの容量による
        config.grab_mode = CAMERA_GRAB_LATEST;
    } else {
        Serial.println("PSRAM is not found");
        config.frame_size = FRAMESIZE_HQVGA;
        config.jpeg_quality = 12; // JPEG品質を再設定
        config.fb_count = 1;
        config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    }

    // カメラ初期化
    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("カメラの初期化に失敗しました。エラーコード: 0x%x (%s)\n", err, esp_err_to_name(err)); // エラーコードと名前を表示
        return false;
    }

    return true;
}

void setup() {
  // シリアル通信の初期化
    Serial.begin(115200);
  Serial.setDebugOutput(true);
  Serial.println();
  delay(1000);
  Serial.println("ESP32-CAM フレームレート管理システム起動中...");

  // WiFi接続の初期化
  if (!connect_wifi()) {
    Serial.println("WiFi接続に失敗しました。システムをリセットします。");
    ESP.restart();  // システムリセット
  }

  // カメラの初期化をsetupで実行しないように戻す
  // if (!initialize_camera()) {
  //   // initialize_camera()内でリセットされるため、ここには到達しません
  // }

  // カメラサーバーの開始
  startCameraServer();
}

void loop() {
  // メインループでは特に処理を行いません
  delay(10000);
}
