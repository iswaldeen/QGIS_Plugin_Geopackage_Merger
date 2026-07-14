# -*- coding: utf-8 -*-
"""Dialog classes for Geopackage Merger."""

import os

from qgis.PyQt import QtCore, QtGui, QtWidgets, uic
from qgis.PyQt.QtGui import QPalette
from qgis.PyQt.QtCore import QUrl
from qgis.core import QgsSettings
from qgis.PyQt.QtGui import QDesktopServices

PLUGIN_DIR = os.path.dirname(__file__)

FORM_CLASS, _ = uic.loadUiType(
    os.path.join(PLUGIN_DIR, "geopackage_merger_dialog_base.ui")
)

SETTINGS_FORM_CLASS, _ = uic.loadUiType(
    os.path.join(PLUGIN_DIR, "geopackage_merger_settings_dialog_base.ui")
)

class GeopackageMergerSettingsDialog(QtWidgets.QDialog, SETTINGS_FORM_CLASS):
    """Settings dialog with persistent options saved between QGIS sessions."""

    SETTINGS_GROUP = "GeopackageMerger/settings"

    CHECKBOX_DEFAULTS = {
        "backupCheckBox": True,
        "createMissingLayersCheckBox": False,
        "ignoreEmptySourceLayersCheckBox": True,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)

        self._load_settings()
        self._connect_setting_widgets()
        self._setup_close_button()

    def _setup_close_button(self):
        button_box = getattr(self, "button_box", None)
        if button_box is not None:
            button_box.rejected.connect(self.reject)
            button_box.accepted.connect(self.accept)

    def _connect_setting_widgets(self):
        """Save checkbox changes immediately so settings persist between sessions."""
        for object_name in self.CHECKBOX_DEFAULTS:
            checkbox = getattr(self, object_name, None)
            if checkbox is not None:
                checkbox.toggled.connect(self._save_settings)

    def _load_settings(self):
        """Load saved settings, falling back to sensible defaults on first use."""
        settings = QgsSettings()

        for object_name, default in self.CHECKBOX_DEFAULTS.items():
            checkbox = getattr(self, object_name, None)
            if checkbox is None:
                continue

            value = settings.value(
                f"{self.SETTINGS_GROUP}/{object_name}",
                default,
                type=bool
            )
            checkbox.setChecked(value)

    def _save_settings(self):
        """Persist the current settings to the user's QGIS profile."""
        settings = QgsSettings()

        for object_name in self.CHECKBOX_DEFAULTS:
            checkbox = getattr(self, object_name, None)
            if checkbox is not None:
                settings.setValue(
                    f"{self.SETTINGS_GROUP}/{object_name}",
                    checkbox.isChecked()
                )


class GeopackageMergerDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.settings_dialog = GeopackageMergerSettingsDialog(self)
        self._setup_progress_status_widgets()
        self._setup_settings_button()
        self._setup_help_button()
        self._setup_close_button()
    
    def _setup_progress_status_widgets(self):
        """Initialise the inline progress bar and status text used during checks and merging."""
        progress_bar = getattr(self, "progressBar", None)
        if progress_bar is not None:
            progress_bar.setRange(0, 100)
            progress_bar.setValue(0)
            progress_bar.setTextVisible(True)
            progress_bar.setVisible(False)

        status_label = getattr(self, "statusLabel", None)
        if status_label is not None:
            status_label.clear()
    
    def _is_dark_mode(self):
        """Return True when QGIS is using a dark application theme."""
        palette = QtWidgets.QApplication.palette()

        try:
            window_role = QPalette.ColorRole.Window
            text_role = QPalette.ColorRole.WindowText
        except AttributeError:
            window_role = QPalette.Window
            text_role = QPalette.WindowText

        return (
            palette.color(window_role).lightness()
            < palette.color(text_role).lightness()
        )

    def _setup_settings_button(self):
        button = getattr(self, "settingsButton", None)
        if button is None:
            return

        icon_name = (
            "settings_darkmode.png"
            if self._is_dark_mode()
            else "settings.png"
        )

        icon_path = os.path.join(
            PLUGIN_DIR,
            "icons",
            icon_name,
        )
        if os.path.exists(icon_path):
            button.setIcon(QtGui.QIcon(icon_path))
            button.setIconSize(QtCore.QSize(22, 22))
        button.setToolTip("Settings")
        button.setText("")
        button.clicked.connect(self.open_settings)

    def _setup_close_button(self):
        button_box = getattr(self, "button_box", None)
        if button_box is not None:
            button_box.rejected.connect(self.reject)
    
    def _setup_help_button(self):
        """Set up the help button and open the bundled help document."""
        button = getattr(self, "helpButton", None)
        if button is None:
            return

        icon_name = (
            "question_mark_darkmode.png"
            if self._is_dark_mode()
            else "question_mark.png"
        )

        icon_path = os.path.join(
            PLUGIN_DIR,
            "icons",
            icon_name,
        )
        if os.path.exists(icon_path):
            button.setIcon(QtGui.QIcon(icon_path))
            button.setIconSize(QtCore.QSize(22, 22))

        button.setToolTip("Help")
        button.setText("")
        button.clicked.connect(self.open_help)
        
    def open_settings(self):
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()
    
    def open_help(self):
        """Open the local help document in the user's default browser."""
        help_path = os.path.join(PLUGIN_DIR, "help", "help.html")

        if os.path.exists(help_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(help_path))

    def setting_checked(self, object_name, default=False):
        widget = getattr(self.settings_dialog, object_name, None)
        if widget is None:
            return default
        return widget.isChecked()
