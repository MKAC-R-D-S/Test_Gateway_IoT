"""Khung log cuộn, giới hạn số dòng — không lag khi chạy nhiều ngày.

`LogViewer` dùng `setMaximumBlockCount` nên Qt tự bỏ dòng cũ; ngoài ra có
`append_tagged()` để tô màu theo mức (INFO/WARN/ERROR) và tự cuộn xuống cuối
(trừ khi người dùng đang kéo lên xem lại — giữ trải nghiệm đọc log).
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import QPlainTextEdit

from . import theme

LEVEL_FORMATS = {
    "INFO": theme.COLOR_TEXT,
    "DEBUG": theme.COLOR_DIM,
    "WARN": theme.COLOR_WARN,
    "WARNING": theme.COLOR_WARN,
    "ERROR": theme.COLOR_CRIT,
    "CRITICAL": theme.COLOR_CRIT,
    "OK": theme.COLOR_OK,
    "EVENT": theme.COLOR_INFO,
}


class LogViewer(QPlainTextEdit):
    """Log viewer read-only, giới hạn dòng, có tô màu theo mức."""

    def __init__(self, max_lines: int = 5000, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(max(100, int(max_lines)))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(theme.mono_font(9))
        self.setPlaceholderText("Nhật ký hoạt động của tool sẽ hiển thị ở đây…")
        self._stick_to_bottom = True
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # ------------------------------------------------------------------ API
    def append_line(self, text: str, level: str = "INFO") -> None:
        """Thêm 1 dòng, tô màu theo mức, tự cuộn nếu đang ở cuối."""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(LEVEL_FORMATS.get((level or "INFO").upper(), theme.COLOR_TEXT)))
        cursor.insertText(text if text.endswith("\n") else text + "\n", fmt)
        if self._stick_to_bottom:
            self._scroll_to_bottom()

    def append_block(self, lines, level: str = "INFO") -> None:
        for line in lines:
            self.append_line(line, level)

    def clear_log(self) -> None:
        self.clear()

    # --------------------------------------------------------------- nội bộ
    def _on_scroll(self, value: int) -> None:
        bar = self.verticalScrollBar()
        self._stick_to_bottom = value >= bar.maximum() - 4

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._stick_to_bottom = True
