import customtkinter as ctk
import tkinter as tk
from PIL import Image, ImageTk
import os
from datetime import datetime
import json
import logging
from logging import getLogger, config
from logging_handlers import TkinterHandler
import queue

# かわいいテーマカラー
COLORS = {
    "pink": "#FFB6C1",
    "light_pink": "#FFC0CB",
    "purple": "#DDA0DD",
    "light_purple": "#E6E6FA",
    "white": "#FFFFFF",
    "black": "#000000"
}

class CuteGUI:
    def __init__(self):
        # メインウィンドウの設定
        self.root = ctk.CTk()
        self.root.title("✨ かわいい顔検出システム ✨")
        self.root.geometry("1200x800")
        
        # カスタムテーマの設定
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        
        # メインフレーム
        self.main_frame = ctk.CTkFrame(self.root, fg_color=COLORS["light_pink"])
        self.main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # 上部フレーム（コントロール用）
        self.top_frame = ctk.CTkFrame(self.main_frame, fg_color=COLORS["light_purple"])
        self.top_frame.pack(fill="x", padx=10, pady=5)
        
        # タイトルラベル
        self.title_label = ctk.CTkLabel(
            self.top_frame,
            text="✨ かわいい顔検出システム ✨",
            font=("Helvetica", 24, "bold"),
            text_color=COLORS["purple"]
        )
        self.title_label.pack(pady=10)
        
        # ボタンフレーム
        self.button_frame = ctk.CTkFrame(self.top_frame, fg_color=COLORS["light_purple"])
        self.button_frame.pack(pady=5)
        
        # かわいいボタンたち
        self.start_button = ctk.CTkButton(
            self.button_frame,
            text="🎀 開始",
            command=self.start_detection,
            fg_color=COLORS["pink"],
            hover_color=COLORS["purple"],
            width=120
        )
        self.start_button.pack(side="left", padx=5)
        
        self.stop_button = ctk.CTkButton(
            self.button_frame,
            text="💝 停止",
            command=self.stop_detection,
            fg_color=COLORS["pink"],
            hover_color=COLORS["purple"],
            width=120
        )
        self.stop_button.pack(side="left", padx=5)
        
        # FPS設定フレーム
        self.fps_frame = ctk.CTkFrame(self.top_frame, fg_color=COLORS["light_purple"])
        self.fps_frame.pack(pady=5)
        
        self.fps_label = ctk.CTkLabel(
            self.fps_frame,
            text="⚡ FPS設定",
            font=("Helvetica", 14),
            text_color=COLORS["purple"]
        )
        self.fps_label.pack(side="left", padx=5)
        
        self.fps_options = ["1", "5", "10", "20", "30"]
        self.fps_var = ctk.StringVar(value="10")
        
        for fps in self.fps_options:
            btn = ctk.CTkButton(
                self.fps_frame,
                text=f"{fps} FPS",
                command=lambda f=fps: self.set_fps(f),
                fg_color=COLORS["pink"],
                hover_color=COLORS["purple"],
                width=60
            )
            btn.pack(side="left", padx=2)
        
        # 解像度設定フレーム
        self.res_frame = ctk.CTkFrame(self.top_frame, fg_color=COLORS["light_purple"])
        self.res_frame.pack(pady=5)
        
        self.res_label = ctk.CTkLabel(
            self.res_frame,
            text="📐 解像度設定",
            font=("Helvetica", 14),
            text_color=COLORS["purple"]
        )
        self.res_label.pack(side="left", padx=5)
        
        self.res_options = ["160x120", "176x144", "240x176", "240x240", "320x240"]
        self.res_var = ctk.StringVar(value="240x176")
        
        for res in self.res_options:
            btn = ctk.CTkButton(
                self.res_frame,
                text=res,
                command=lambda r=res: self.set_resolution(r),
                fg_color=COLORS["pink"],
                hover_color=COLORS["purple"],
                width=80
            )
            btn.pack(side="left", padx=2)
        
        # 映像表示フレーム
        self.video_frame = ctk.CTkFrame(self.main_frame, fg_color=COLORS["white"])
        self.video_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.video_label = ctk.CTkLabel(
            self.video_frame,
            text="📸 映像を待機中...",
            font=("Helvetica", 16),
            text_color=COLORS["purple"]
        )
        self.video_label.pack(expand=True)
        
        # ログ表示フレーム
        self.log_frame = ctk.CTkFrame(self.main_frame, fg_color=COLORS["light_purple"])
        self.log_frame.pack(fill="x", padx=10, pady=5)
        
        self.log_text = ctk.CTkTextbox(
            self.log_frame,
            height=150,
            font=("Helvetica", 12),
            text_color=COLORS["black"],
            fg_color=COLORS["white"]
        )
        self.log_text.pack(fill="x", padx=5, pady=5)
        
        # ステータスバー
        self.status_frame = ctk.CTkFrame(self.main_frame, fg_color=COLORS["light_pink"])
        self.status_frame.pack(fill="x", padx=10, pady=5)
        
        self.status_label = ctk.CTkLabel(
            self.status_frame,
            text="✨ 準備完了！",
            font=("Helvetica", 12),
            text_color=COLORS["purple"]
        )
        self.status_label.pack(side="left", padx=10)
        
        self.fps_display = ctk.CTkLabel(
            self.status_frame,
            text="FPS: 0",
            font=("Helvetica", 12),
            text_color=COLORS["purple"]
        )
        self.fps_display.pack(side="right", padx=10)
        
        # ログ設定
        self.setup_logging()
        
    def setup_logging(self):
        with open('log_config.json', 'r') as f:
            log_config = json.load(f)
        
        log_config['handlers']['tkinterHandler']['text_widget'] = self.log_text
        log_config['handlers']['tkinterHandler']['log_queue'] = queue.Queue()
        
        logging.config.dictConfig(log_config)
        self.logger = getLogger(__name__)
    
    def start_detection(self):
        self.logger.info("🎀 顔検出を開始するよ〜")
        self.status_label.configure(text="✨ 検出中...")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
    
    def stop_detection(self):
        self.logger.info("💝 顔検出を停止するよ〜")
        self.status_label.configure(text="✨ 停止中...")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
    
    def set_fps(self, fps):
        self.fps_var.set(fps)
        self.logger.info(f"⚡ FPSを{fps}に設定したよ〜")
    
    def set_resolution(self, resolution):
        self.res_var.set(resolution)
        self.logger.info(f"📐 解像度を{resolution}に設定したよ〜")
    
    def update_video(self, frame):
        # ここで映像を更新する処理を実装
        pass
    
    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = CuteGUI()
    app.run() 