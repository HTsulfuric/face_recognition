import logging
import tkinter as tk

class TkinterHandler(logging.Handler):
    """
    A logging handler that sends log messages to a Tkinter Text widget.
    """
    def __init__(self, text_widget, log_queue):
        super().__init__()
        self.text_widget = text_widget
        self.log_queue = log_queue

    def emit(self, record):
        log_entry = self.format(record)
        self.log_queue.put(log_entry)
