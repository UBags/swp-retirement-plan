# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.

#!/usr/bin/env python3
"""
SWP Financial Planner – Launch Script

Changes vs original:
  - Prompts for user name at startup (via a Qt dialog, before the main window opens).
  - Creates  <project_root>/<user_name>/  and passes it to MainWindow as output_dir.
  - All file outputs (CSVs, project JSON, fund metrics, allocation results) are
    written into that directory automatically.

Requirements:
  pip install PySide6

Usage:
  python3 run.py
"""
import sys
import os
from pathlib import Path

# ── Make sure the app directory is on sys.path ────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QDialogButtonBox, QMessageBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont


# ── Project root is the directory that contains run.py ───────────────────────
PROJECT_ROOT = Path(os.path.dirname(os.path.abspath(__file__)))


class UserNameDialog(QDialog):
    """
    Modal startup dialog that asks for a user name.
    The name is used to create (or reuse) a per-user output directory under
    the project root.  Empty or whitespace-only names are rejected.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SWP Financial Planner – Welcome")
        self.setFixedWidth(420)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 20, 24, 16)

        title = QLabel("SWP Financial Planner")
        title.setFont(QFont("", 15, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        sub = QLabel(
            "All outputs (fund metrics, allocation results, project files, CSVs)\n"
            "are stored in a folder named after you under the application directory."
        )
        sub.setWordWrap(True)
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(sub)

        row = QHBoxLayout()
        row.addWidget(QLabel("Your name:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g.  Uddi")
        self.name_edit.returnPressed.connect(self._accept)
        row.addWidget(self.name_edit)
        layout.addLayout(row)

        self.dir_label = QLabel("")
        self.dir_label.setStyleSheet("color:#27ae60; font-size:10px;")
        self.dir_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.dir_label)
        self.name_edit.textChanged.connect(self._update_dir_label)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _update_dir_label(self, text: str):
        name = text.strip()
        if name:
            path = PROJECT_ROOT / name
            self.dir_label.setText(f"Output folder: {path}")
        else:
            self.dir_label.setText("")

    def _accept(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required",
                                "Please enter your name to continue.")
            return
        # Sanitise: replace characters that are illegal in directory names
        illegal = r'\/:*?"<>|'
        for ch in illegal:
            name = name.replace(ch, "_")
        self.name_edit.setText(name)   # show sanitised version
        self.accept()

    def user_name(self) -> str:
        return self.name_edit.text().strip()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # ── Step 1: ask for user name ─────────────────────────────────────────────
    dlg = UserNameDialog()
    if dlg.exec() != QDialog.DialogCode.Accepted:
        sys.exit(0)   # user clicked Cancel – quit cleanly

    user_name = dlg.user_name()

    # ── Step 2: create (or reuse) the per-user output directory ──────────────
    output_dir = PROJECT_ROOT / user_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 3: open the main window, passing output_dir ─────────────────────
    from main import MainWindow
    win = MainWindow(output_dir=output_dir, user_name=user_name)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
