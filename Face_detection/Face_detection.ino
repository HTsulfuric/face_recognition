// face_detection.ino
#include <esp_camera.h>
#include <WiFi.h>

//#define CAMERA_MODEL_AI_THINKER // Has PSRAM
#define CAMERA_MODEL_XIAO_ESP32S3  // Has PSRAM
#include "camera_pins.h"

void startCameraServer();

const char SSID[] = "SSID";
const char password[] = "password";

// 最大再試行回数
#define MAX_WIFI_RETRIES 20
#define WIFI_RETRY_DELAY 500  // ミリ秒単位

// カメラ初期化の再試行回数
#define MAX_CAMERA_INIT_RETRIES 5
#define CAMERA_INIT_DELAY 2000  // ミリ秒単位

// シリアル通信の初期化設定
#define BAUD_RATE 115200


// LED管理用定義
#if defined(LED_GPIO_NUM)
#define LED_LEDC_TIMER LEDC_TIMER_0
#define LED_MAX_INTENSITY 255

// LED管理用関数
void setup_led() {
  pinMode(LED_GPIO_NUM, OUTPUT);
  digitalWrite(LED_GPIO_NUM, LOW);  // 初期状態はLEDオフ

  // LEDピンの初期化
  ledcAttach(LED_GPIO_NUM, LED_LEDC_TIMER, 8); // 8ビットの分解能
  ledcWrite(LED_GPIO_NUM, 0); // 初期状態はオフ
}

// LED点灯関数（エラー時）
void blink_error_led(int blink_count, int delay_ms) {
  for(int i = 0; i < blink_count; i++) {
    digitalWrite(LED_GPIO_NUM, HIGH);
    delay(delay_ms);
    digitalWrite(LED_GPIO_NUM, LOW);
    delay(delay_ms);
  }
}

// LED点灯関数（正常時）
void solid_led() {
  digitalWrite(LED_GPIO_NUM, HIGH);
}

// LED管理用関数（LEDを消灯）
void turn_off_led() {
  digitalWrite(LED_GPIO_NUM, LOW);
}
#else
// LED_GPIO_NUMが定義されていない場合、空の関数を定義
void setup_led() {}
void blink_error_led(int blink_count, int delay_ms) {}
void solid_led() {}
void turn_off_led() {}
#endif

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
    #if defined(LED_GPIO_NUM)
    solid_led();  // 接続成功時にLEDを点灯
    delay(100);
    turn_off_led();  // 0.1秒後にLEDを消灯
    #endif
    return true;
  } else {
    Serial.println("\nWiFi接続に失敗しました");
    #if defined(LED_GPIO_NUM)
    blink_error_led(5, 500);  // エラー時にLEDを点滅
    #endif
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
      blink_error_led(2, 300);  // 再試行時にLEDを点滅
      delay(CAMERA_INIT_DELAY);
    }
  }
  Serial.println("カメラの初期化に複数回失敗しました。システムをリセットします。");
  blink_error_led(10, 100);  // 致命的なエラー時にLEDを長く点滅
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
        config.jpeg_quality = 10; // 値は低いほど圧縮率が高い
        config.fb_count = 3; // PSRAM使用時はフレームバッファを3つにする これはPSRAMの容量による
        config.grab_mode = CAMERA_GRAB_LATEST;
    } else {
        Serial.println("PSRAM is not found");
        config.frame_size = FRAMESIZE_HQVGA;
        config.jpeg_quality = 12;
        config.fb_count = 1;
        config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    }

    // カメラ初期化
    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("カメラの初期化に失敗しました。エラーコード: 0x%x\n", err);
        return false;
    }

    return true;
}

void setup() {
  // LEDのセットアップ
  setup_led();

  // シリアル通信の初期化
  Serial.begin(BAUD_RATE);
  delay(1000);
  Serial.println("ESP32-CAM フレームレート管理システム起動中...");

  // WiFi接続の初期化
  if (!connect_wifi()) {
    Serial.println("WiFi接続に失敗しました。システムをリセットします。");
    blink_error_led(10, 100);  // 致命的なエラー時にLEDを長く点滅
    ESP.restart();  // システムリセット
  }

  // カメラの初期化
  if (!initialize_camera()) {
    // initialize_camera()内でリセットされるため、ここには到達しません
  }

  // カメラサーバーの開始
  startCameraServer();
}

void loop() {
  // メインループでは特に処理を行いません
  delay(10000);
}