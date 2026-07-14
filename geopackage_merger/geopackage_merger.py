# -*- coding: utf-8 -*-
"""
Geopackage Merger

Implementation for comparing one or more source GeoPackages against
one main GeoPackage, reporting critical issues, and only copying data once the
pre-copy checks have passed.
"""

import hashlib
import math
import os
import shutil
import sqlite3
import webbrowser
from collections import defaultdict
from datetime import datetime

from qgis.PyQt.QtCore import QCoreApplication, QLocale, Qt, QTranslator, QVariant
from qgis.PyQt.QtGui import QIcon, QPalette
from qgis.PyQt.QtWidgets import QAction, QApplication, QFileDialog, QListWidgetItem

from qgis.core import (
    NULL,
    QgsFeature,
    QgsField,
    QgsProject,
    QgsSettings,
    QgsVectorFileWriter,
    QgsVectorLayer
)

from .geopackage_merger_dialog import (
    GeopackageMergerDialog,
    GeopackageMergerSettingsDialog,
)

class GeopackageMerger:
    """QGIS Plugin Implementation."""

    SUPPORTED_GPKG_EXT = ".gpkg"
    GEOPACKAGE_SOURCE_FIELD = "geopackage_source"
    SOURCE_PATH_ROLE = Qt.ItemDataRole.UserRole if hasattr(Qt, "ItemDataRole") else Qt.UserRole

    # Common PCA duplicate checks based on the supplied example site-plan GeoPackage.
    # Duplicate identifiers that must be resolved before merging.
    CRITICAL_DUPLICATE_KEY_FIELDS = {
        "DRS_Context_Database": ["Context"],
        "DRS_Trench_Database": ["Trench_Number"],
        "Drawing_Points": ["drawing_no"],
        "Environmental": ["sample_no"],
        "Interventions": ["context_no"],
        "Sections": ["section_no"],
        "Small_Finds": ["sf_no"],
    }
    
    # Layers and identifying fields used to detect exact feature duplicates.
    # A feature is critical only when its layer, identifying values and exact
    # geometry match a feature in another GeoPackage.
    EXACT_GEOMETRY_DUPLICATE_FIELDS = {
        "Archaeological_Features": ["context_no"],
        "Archaeological_Features_LN": ["context_no"],
        "Burials": ["context_no"],
        "Drawing_Points": ["drawing_no"],
        "Environmental": ["sample_no"],
        "Features_for_PostEx": ["source_layer", "source_fid"],
        "Furrows_and_Ridges": ["context_no"],
        "Interventions": ["context_no"],
        "Layers": ["context_no"],
        "Levels": ["context_no"],
        "LOE": ["loe_no"],
        "Masonry": ["context_no"],
        "Modern": ["context_no"],
        "Natural": ["context_no"],
        "Previous": ["context_no"],
        "Previous_lines": ["context_no"],
        "Sections": ["section_no"],
        "Small_Finds": ["sf_no"],
        "Stations": ["stn_no"],
        "Targets": ["targ_name"],
        "TBM": ["tbm_name"],
    }

    INTERNAL_TABLES = {
        "gpkg_contents",
        "gpkg_extensions",
        "gpkg_geometry_columns",
        "gpkg_ogr_contents",
        "gpkg_spatial_ref_sys",
        "gpkg_tile_matrix",
        "gpkg_tile_matrix_set",
        "rtree_",
        "sqlite_",
    }

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = self.tr("&Geopackage Merger")
        self.first_start = None
        self.dlg = None
        self.last_report = ""
        self.last_preflight_ok = False

        locale = QgsSettings().value("locale/userLocale", QLocale().name())[0:2]
        locale_path = os.path.join(self.plugin_dir, "i18n", "{}.qm".format(locale))
        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

    def tr(self, message):
        return QCoreApplication.translate("GeopackageMerger", message)
    
    def _is_dark_mode(self):
        """Return True when QGIS is using a dark application theme."""
        palette = QApplication.palette()

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

    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None,
    ):
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip:
            action.setStatusTip(status_tip)
        if whats_this:
            action.setWhatsThis(whats_this)
        if add_to_toolbar:
            self.iface.addToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)
        return action

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icons", "geopackage_merger_icon.png")
        self.add_action(
            icon_path,
            text=self.tr("Geopackage Merger"),
            callback=self.run,
            parent=self.iface.mainWindow(),
        )

        settings_icon_path = os.path.join(
            self.plugin_dir,
            "icons",
            "settings_darkmode.png"
            if self._is_dark_mode()
            else "settings.png",
        )
        self.add_action(
            settings_icon_path,
            text=self.tr("Settings"),
            callback=self.open_settings,
            add_to_toolbar=False,
            parent=self.iface.mainWindow(),
        )

        help_icon_path = os.path.join(
            self.plugin_dir,
            "icons",
            "question_mark_darkmode.png"
            if self._is_dark_mode()
            else "question_mark.png",
        )
        self.add_action(
            help_icon_path,
            text=self.tr("Help"),
            callback=self.open_help,
            add_to_toolbar=False,
            parent=self.iface.mainWindow(),
        )

        self.first_start = True

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)

    def run(self):
        if self.first_start or self.dlg is None:
            self.first_start = False
            self.dlg = GeopackageMergerDialog(self.iface.mainWindow())
            self._wire_dialog()

        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
    
    def open_settings(self):
        if self.dlg is None:
            self.dlg = GeopackageMergerDialog(self.iface.mainWindow())
            self._wire_dialog()

        self.dlg.open_settings()

    def open_help(self):
        if self.dlg is None:
            self.dlg = GeopackageMergerDialog(self.iface.mainWindow())
            self._wire_dialog()

        self.dlg.open_help()

    def _setting_checked(self, object_name, default=False):
        if hasattr(self.dlg, "setting_checked"):
            return self.dlg.setting_checked(object_name, default)
        return default
    
    def _reset_plugin_state(self):
        """Reset cached validation state when the dialog closes."""

        self.last_preflight_ok = False
        self.validation_result = None

    # ------------------------------------------------------------------
    # UI wiring
    # ------------------------------------------------------------------

    def _wire_dialog(self):
        self.dlg.targetBrowseButton.clicked.connect(self._browse_target)
        self.dlg.addSourcesButton.clicked.connect(self._add_sources)
        self.dlg.removeSourceButton.clicked.connect(self._remove_selected_source)
        self.dlg.clearSourcesButton.clicked.connect(self._clear_sources)
        self.dlg.validateButton.clicked.connect(self._run_checks_only)
        self.dlg.mergeButton.clicked.connect(self._validate_and_merge)
        self.dlg.saveReportButton.clicked.connect(self._save_report)
        self.dlg.clearReportButton.clicked.connect(self._clear_report)

        self.dlg.targetLineEdit.textChanged.connect(self._target_changed)
        self.dlg.sourceListWidget.model().rowsInserted.connect(self._update_action_state)
        self.dlg.sourceListWidget.model().rowsRemoved.connect(self._update_action_state)
        self.dlg.finished.connect(self._reset_plugin_state)
        
    def _browse_target(self):
        path, _ = QFileDialog.getOpenFileName(
            self.dlg,
            "Select main GeoPackage",
            "",
            "GeoPackage (*.gpkg)",
        )
        if path:
            self.dlg.targetLineEdit.setText(path)
            self.last_preflight_ok = False
            self.dlg.mergeButton.setEnabled(False)
            
            self._set_status(
                "Selections changed. Run checks before merging.",
                "neutral"
            )

    def _add_sources(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self.dlg,
            "Select source GeoPackages",
            "",
            "GeoPackage (*.gpkg)",
        )

        if not paths:
            return

        target_path = self.dlg.targetLineEdit.text().strip()
        target_key = self._normalised_path_key(target_path) if target_path else ""

        existing = {
            self._normalised_path_key(path)
            for path in self._source_paths()
        }

        added_count = 0
        skipped_duplicates = []
        skipped_target = []

        for path in paths:
            if not path:
                continue

            path_key = self._normalised_path_key(path)

            if target_key and path_key == target_key:
                skipped_target.append(path)
                continue

            if path_key in existing:
                skipped_duplicates.append(path)
                continue

            item = QListWidgetItem()
            item.setData(self.SOURCE_PATH_ROLE, path)
            self.dlg.sourceListWidget.addItem(item)
            existing.add(path_key)
            added_count += 1

        self._renumber_sources()

        self.last_preflight_ok = False
        self.dlg.mergeButton.setEnabled(False)

        if skipped_target:
            self._set_status(
                "Source not added because it is already selected as the main GeoPackage.",
                "warning"
            )
        elif skipped_duplicates:
            self._set_status(
                "Duplicate source GeoPackage ignored. Run checks before merging.",
                "warning"
            )
        elif added_count:
            self._set_status(
                "Sources changed. Run checks before merging.",
                "neutral"
            )

        self._update_action_state()

    def _remove_selected_source(self):
        for item in self.dlg.sourceListWidget.selectedItems():
            row = self.dlg.sourceListWidget.row(item)
            self.dlg.sourceListWidget.takeItem(row)

        self._renumber_sources()
        self.last_preflight_ok = False
        self.dlg.mergeButton.setEnabled(False)
        self._set_status("Sources changed. Run checks before merging.", "neutral")
        self._update_action_state()

    def _clear_sources(self):
        self.dlg.sourceListWidget.clear()
        self.last_preflight_ok = False
        self.dlg.mergeButton.setEnabled(False)
        
        self._set_status(
            "Sources cleared. Add source GeoPackages and run checks before merging.",
            "neutral"
        )

    def _renumber_sources(self):
        """Display sequential source numbers while preserving the original paths."""
        for index in range(self.dlg.sourceListWidget.count()):
            item = self.dlg.sourceListWidget.item(index)
            path = item.data(self.SOURCE_PATH_ROLE)

            if not path:
                path = item.text()
                prefix, separator, remainder = path.partition(". ")
                if separator and prefix.isdigit():
                    path = remainder
                item.setData(self.SOURCE_PATH_ROLE, path)

            item.setText(f"{index + 1}. {path}")
            item.setToolTip(path)

    def _source_paths(self):
        paths = []
        for index in range(self.dlg.sourceListWidget.count()):
            item = self.dlg.sourceListWidget.item(index)
            path = item.data(self.SOURCE_PATH_ROLE)
            if not path:
                text = item.text()
                prefix, separator, remainder = text.partition(". ")
                path = remainder if separator and prefix.isdigit() else text
            paths.append(path)
        return paths

    def _geopackage_label(self, path):
        """Return Main or the current numbered source label for a GeoPackage."""
        path_key = self._normalised_path_key(path)
        target_path = self.dlg.targetLineEdit.text().strip()

        if target_path and path_key == self._normalised_path_key(target_path):
            return "Main"

        for index, source_path in enumerate(self._source_paths(), start=1):
            if path_key == self._normalised_path_key(source_path):
                return f"Source {index}"

        return os.path.basename(path)
    
    def _normalised_path_key(self, path):
        """Return a stable key for comparing selected file paths."""
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))
    
    def _target_changed(self, *args):
        """Invalidate validation when the main GeoPackage selection changes."""
        self.last_preflight_ok = False
        self.dlg.mergeButton.setEnabled(False)

        target_path = self.dlg.targetLineEdit.text().strip()
        target_key = self._normalised_path_key(target_path) if target_path else ""

        source_keys = {
            self._normalised_path_key(path)
            for path in self._source_paths()
        }

        if target_key and target_key in source_keys:
            self._set_status(
                "The main GeoPackage is also selected as a source. Remove it from the source list before merging.",
                "warning"
            )
        elif target_path:
            self._set_status(
                "Main GeoPackage changed. Run checks before merging.",
                "neutral"
            )
        else:
            self._set_status("", "neutral")

        self._update_action_state()

    def _update_action_state(self, *args):
        ready = bool(self.dlg.targetLineEdit.text().strip()) and bool(self._source_paths())
        self.dlg.validateButton.setEnabled(ready)
        self.dlg.mergeButton.setEnabled(ready and self.last_preflight_ok)

    # ------------------------------------------------------------------
    # Checks and reporting
    # ------------------------------------------------------------------

    def _run_checks_only(self):
        self._set_status("Running GeoPackage checks...", "neutral")
        self._set_busy(True)
        try:
            try:
                ok, report, _plan = self._preflight()
            except Exception as exc:
                ok = False
                report = f"GEOPACKAGE MERGER REPORT\n========================\n\nVALIDATION FAILED\n-----------------\n{exc}"

            self.last_report = report
            self.last_preflight_ok = ok
            self.dlg.reportTextEdit.setPlainText(report)
            self.dlg.saveReportButton.setEnabled(True)
            self.dlg.mergeButton.setEnabled(ok)
            self.dlg.clearReportButton.setEnabled(True)

            if ok:
                self._set_status("GeoPackage checks passed. You can now merge.", "success")
            else:
                self._set_status("GeoPackage checks found critical issues. Fix them and retry.", "warning")
        finally:
            self._set_busy(False)


    def _validate_and_merge(self):
        self._set_status("Validating GeoPackages before merging...", "neutral")
        self._set_busy(True)
        try:
            try:
                ok, report, plan = self._preflight()
            except Exception as exc:
                ok = False
                report = f"GEOPACKAGE MERGER REPORT\n========================\n\nVALIDATION FAILED\n-----------------\n{exc}"
                plan = []

            self.last_report = report
            self.last_preflight_ok = ok
            self.dlg.reportTextEdit.setPlainText(report)
            self.dlg.saveReportButton.setEnabled(True)
            self.dlg.clearReportButton.setEnabled(True)

            if not ok:
                self.dlg.mergeButton.setEnabled(False)
                self._set_status("Merge cancelled. Critical issues must be fixed first.", "warning")
                return

            target_path = self.dlg.targetLineEdit.text().strip()
            try:
                self._set_status("Checks passed. Copying data into the main GeoPackage...", "neutral")

                self._set_progress(2, "Preparing to merge GeoPackages...")
                backup_path = None

                if self._setting_checked("backupCheckBox", True):
                    self._set_progress(5, "Creating a backup of Main...")
                    backup_path = self._create_backup(target_path)

                self._set_progress(10, "Copying layers and tables...")
                copied_count, created_layers = self._execute_merge(target_path, plan)
                self._set_progress(92, "Copying styles for newly created layers...")
                self._copy_styles_for_created_layers(target_path, created_layers)

                final_lines = [report, "", "MERGE COMPLETE", "--------------", f"Features copied: {copied_count}"]
                if backup_path:
                    final_lines.append(f"Backup created: {backup_path}")

                self.last_report = "\n".join(final_lines)
                self.dlg.reportTextEdit.setPlainText(self.last_report)
                self._set_progress(98, "Refreshing layers in QGIS...")
                QgsProject.instance().reloadAllLayers()
                self._set_progress(100, "Merge complete.")
                self._set_status(f"Merge complete. {copied_count} features copied.", "success")

            except Exception as exc:
                self._set_status(f"Merge failed: {exc}", "error")
                self.dlg.reportTextEdit.appendPlainText(f"\nMERGE FAILED\n------------\n{exc}")
        finally:
            self._set_busy(False)
        
    def _clear_report(self):
        """Clear the report panel without changing the current GeoPackage selections."""
        self.last_report = ""
        self.dlg.reportTextEdit.clear()
        self.dlg.saveReportButton.setEnabled(False)
        self.dlg.clearReportButton.setEnabled(False)
        self._set_status("", "neutral")

    def _save_report(self):
        if not self.last_report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self.dlg,
            "Save Geopackage Merger report",
            f"geopackage_merger_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text file (*.txt)",
        )
        if not path:
            return
        if not path.lower().endswith(".txt"):
            path += ".txt"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.last_report)
        self._set_status("Report saved.", "success")

    def _preflight(self):
        target_path = self.dlg.targetLineEdit.text().strip()
        source_paths = self._source_paths()
        critical = []
        warnings = []
        info = []
        plan = []
        exact_duplicate_values = defaultdict(list)
        incoming_duplicate_values = defaultdict(list)

        self._set_progress(2, "Checking selected GeoPackage paths...")
        self._validate_selected_paths(target_path, source_paths, critical)
        if critical:
            self._set_progress(100, "Validation could not start.")
            return False, self._format_report(critical, warnings, info, plan), plan

        self._set_progress(5, "Reading the Main schema...")
        target_schema = self._read_gpkg_schema(target_path)
        target_layers = target_schema["layers"]
        target_tables = target_schema["tables"]
        source_schemas = {}

        source_total = max(len(source_paths), 1)
        for index, source_path in enumerate(source_paths, start=1):
            label = self._geopackage_label(source_path)
            self._set_progress(5 + int(5 * index / source_total), f"Reading the schema for {label}...")
            source_schemas[source_path] = self._read_gpkg_schema(source_path)

        main_duplicate_layers = [
            (layer_name, key_fields)
            for layer_name, key_fields in self.EXACT_GEOMETRY_DUPLICATE_FIELDS.items()
            if layer_name in target_layers
        ]

        total_items = len(main_duplicate_layers) + 2
        for source_schema in source_schemas.values():
            total_items += len(source_schema["layers"])
            total_items += len([name for name in source_schema["tables"] if name != "layer_styles"])

        completed_items = 0

        def advance_progress(message):
            nonlocal completed_items
            completed_items += 1
            percentage = 10 + int(85 * completed_items / max(total_items, 1))
            self._set_progress(percentage, message)

        for layer_name, key_fields in main_duplicate_layers:
            self._set_status(f"Checking Main layer '{layer_name}' for exact duplicates...", "neutral")
            self._collect_exact_geometry_duplicates(target_path, layer_name, key_fields, exact_duplicate_values)
            advance_progress(f"Checked Main layer '{layer_name}'.")

        if target_schema["rasters"]:
            warnings.append("Main contains raster tile tables. Raster layers are not copied by this version.")

        for source_path in source_paths:
            source_schema = source_schemas[source_path]
            source_label = self._geopackage_label(source_path)

            if source_schema["rasters"]:
                warnings.append(f"{source_label} contains raster tile tables. Raster layers are not copied by this version.")

            for layer_name, source_layer_meta in source_schema["layers"].items():
                self._set_status(f"Checking {source_label}, layer '{layer_name}'...", "neutral")
                source_count = self._feature_count(source_path, layer_name)

                if source_count == 0 and self._setting_checked("ignoreEmptySourceLayersCheckBox", True):
                    advance_progress(f"Skipped empty layer '{layer_name}' in {source_label}.")
                    continue

                self._check_layer_geometries(source_path, layer_name, critical)
                key_fields = self.EXACT_GEOMETRY_DUPLICATE_FIELDS.get(layer_name)
                if key_fields:
                    self._collect_exact_geometry_duplicates(source_path, layer_name, key_fields, exact_duplicate_values)

                target_meta = target_layers.get(layer_name)
                if target_meta is None:
                    if self._setting_checked("createMissingLayersCheckBox", False):
                        plan.append(("create", source_path, layer_name))
                        info.append(f"Will create missing layer '{layer_name}' from {source_label}.")
                        target_layers[layer_name] = {
                            **source_layer_meta,
                            "fields": {name: dict(meta) for name, meta in source_layer_meta["fields"].items()},
                        }
                    else:
                        critical.append(f"Missing layer in Main: '{layer_name}' from {source_label}.")

                    advance_progress(f"Checked {source_label}, layer '{layer_name}'.")
                    continue

                missing_fields = self._check_layer_compatibility(source_path, layer_name, source_layer_meta, target_meta, critical, info)
                for field_name in missing_fields:
                    target_meta["fields"][field_name] = dict(source_layer_meta["fields"][field_name])

                self._collect_duplicate_values(source_path, layer_name, incoming_duplicate_values)
                self._check_duplicates_against_target(source_path, target_path, layer_name, critical)
                plan.append(("append", source_path, layer_name))
                advance_progress(f"Checked {source_label}, layer '{layer_name}'.")

            for table_name, source_table_meta in source_schema["tables"].items():
                if table_name == "layer_styles":
                    continue

                self._set_status(f"Checking {source_label}, table '{table_name}'...", "neutral")
                source_count = self._table_count(source_path, table_name)

                if source_count == 0 and self._setting_checked("ignoreEmptySourceLayersCheckBox", True):
                    advance_progress(f"Skipped empty table '{table_name}' in {source_label}.")
                    continue

                target_meta = target_tables.get(table_name)
                if target_meta is None:
                    if self._setting_checked("createMissingLayersCheckBox", False):
                        plan.append(("copy_table", source_path, table_name))
                        info.append(f"Will create missing attribute table '{table_name}' from {source_label}.")
                        target_tables[table_name] = {
                            **source_table_meta,
                            "fields": {name: dict(meta) for name, meta in source_table_meta["fields"].items()},
                        }
                    else:
                        critical.append(f"Missing attribute table in Main: '{table_name}' from {source_label}.")

                    advance_progress(f"Checked {source_label}, table '{table_name}'.")
                    continue

                missing_fields = self._check_table_fields(source_path, table_name, source_table_meta["fields"], target_meta["fields"], critical, info)
                for field_name in missing_fields:
                    target_meta["fields"][field_name] = dict(source_table_meta["fields"][field_name])

                self._collect_duplicate_values(source_path, table_name, incoming_duplicate_values)
                self._check_duplicates_against_target(source_path, target_path, table_name, critical)
                plan.append(("append_table", source_path, table_name))
                advance_progress(f"Checked {source_label}, table '{table_name}'.")

        self._set_status("Comparing duplicate values between source GeoPackages...", "neutral")
        self._check_duplicates_between_sources(incoming_duplicate_values, critical)
        advance_progress("Compared duplicate values between sources.")

        self._set_status("Comparing exact feature duplicates...", "neutral")
        self._check_exact_geometry_duplicates(exact_duplicate_values, critical)
        advance_progress("Compared exact feature duplicates.")

        self._set_progress(100, "Preparing validation report...")
        return not critical, self._format_report(critical, warnings, info, plan), plan
        
    def _validate_selected_paths(self, target_path, source_paths, critical):
        target_key = self._normalised_path_key(target_path) if target_path else ""

        if not target_path:
            critical.append("No main GeoPackage selected.")
        elif not os.path.isfile(target_path):
            critical.append(f"Main GeoPackage does not exist: {target_path}")
        elif not target_path.lower().endswith(self.SUPPORTED_GPKG_EXT):
            critical.append("Main file is not a .gpkg file.")

        if not source_paths:
            critical.append("No source GeoPackages selected.")
            return

        seen_sources = {}

        for source_path in source_paths:
            source_key = self._normalised_path_key(source_path)

            if source_key in seen_sources:
                critical.append(
                    "The same source GeoPackage has been selected more than once: "
                    f"{source_path}"
                )
                continue

            seen_sources[source_key] = source_path

            if not os.path.isfile(source_path):
                critical.append(f"Source GeoPackage does not exist: {source_path}")
            elif not source_path.lower().endswith(self.SUPPORTED_GPKG_EXT):
                critical.append(f"Source file is not a .gpkg file: {source_path}")
            elif target_key and source_key == target_key:
                critical.append(
                    "A source GeoPackage cannot also be the main GeoPackage: "
                    f"{source_path}"
                )

    def _read_gpkg_schema(self, gpkg_path):
        with sqlite3.connect(gpkg_path) as conn:
            rows = conn.execute(
                "SELECT table_name, data_type, srs_id FROM gpkg_contents ORDER BY table_name"
            ).fetchall()
            geom_rows = {
                row[0]: row
                for row in conn.execute(
                    "SELECT table_name, column_name, geometry_type_name, srs_id FROM gpkg_geometry_columns"
                ).fetchall()
            }

            schema = {"layers": {}, "tables": {}, "rasters": {}}
            for table_name, data_type, srs_id in rows:
                fields = self._table_fields(conn, table_name)
                if data_type == "features":
                    geom = geom_rows.get(table_name)
                    schema["layers"][table_name] = {
                        "data_type": data_type,
                        "srs_id": srs_id,
                        "geom_column": geom[1] if geom else "geom",
                        "geometry_type": geom[2] if geom else "",
                        "fields": fields,
                    }
                elif data_type == "attributes":
                    schema["tables"][table_name] = {"data_type": data_type, "fields": fields}
                elif data_type == "tiles":
                    schema["rasters"][table_name] = {"data_type": data_type, "srs_id": srs_id, "fields": fields}
            return schema

    def _table_fields(self, conn, table_name):
        rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
        return {
            row[1]: {
                "type": (row[2] or "").upper(),
                "notnull": bool(row[3]),
                "default": row[4],
                "pk": bool(row[5]),
            }
            for row in rows
        }

    def _check_layer_compatibility(self, source_path, layer_name, source_meta, target_meta, critical, info):
        if int(source_meta.get("srs_id") or 0) != int(target_meta.get("srs_id") or 0):
            critical.append(f"CRS mismatch in '{layer_name}': source EPSG/SRS {source_meta.get('srs_id')} vs main {target_meta.get('srs_id')}.")

        source_geom = (source_meta.get("geometry_type") or "").upper()
        target_geom = (target_meta.get("geometry_type") or "").upper()
        if source_geom and target_geom and source_geom != target_geom:
            critical.append(f"Geometry type mismatch in '{layer_name}': source {source_geom} vs main {target_geom}.")

        source_fields = {name: meta for name, meta in source_meta["fields"].items() if name != source_meta.get("geom_column")}
        target_fields = {name: meta for name, meta in target_meta["fields"].items() if name != target_meta.get("geom_column")}
        return self._check_table_fields(source_path, layer_name, source_fields, target_fields, critical, info)

    def _check_table_fields(self, source_path, table_name, source_fields, target_fields, critical, info):
        source_names = {name for name, meta in source_fields.items() if not meta["pk"]}
        target_names = {name for name, meta in target_fields.items() if not meta["pk"]}

        missing_in_target = sorted(source_names - target_names)
        if missing_in_target:
            info.append(f"Fields will be added to '{table_name}' in the main GeoPackage: {', '.join(missing_in_target)}. Existing records will contain NULL in these fields.")

        missing_in_source = sorted(target_names - source_names)
        if missing_in_source:
            info.append(f"Source records for '{table_name}' do not contain: {', '.join(missing_in_source)}. NULL or the main field default will be used.")
            for field_name in missing_in_source:
                meta = target_fields[field_name]
                if meta["notnull"] and meta.get("default") is None:
                    critical.append(f"Source is missing required field '{table_name}.{field_name}', which is NOT NULL and has no default in the main GeoPackage.")

        for field_name in sorted(source_names & target_names):
            source_type = self._normalise_field_type(source_fields[field_name]["type"])
            target_type = self._normalise_field_type(target_fields[field_name]["type"])
            if source_type and target_type and source_type != target_type:
                self._check_field_conversion(source_path, table_name, field_name, source_type, target_type, source_fields, target_fields[field_name], critical, info)

        return missing_in_target

    def _normalise_field_type(self, field_type):
        ft = (field_type or "").upper()
        if "INT" in ft:
            return "INTEGER"
        if any(token in ft for token in ("REAL", "DOUBLE", "FLOAT", "NUMERIC")):
            return "REAL"
        if any(token in ft for token in ("TEXT", "CHAR", "CLOB", "DATE", "TIME", "BOOLEAN")):
            return "TEXT"
        return ft
    
    def _convert_field_value(self, value, target_type, target_meta=None):
        is_null = value is None or value == NULL
        if is_null or (isinstance(value, str) and not value.strip() and target_type in ("INTEGER", "REAL")):
            if target_meta and target_meta.get("notnull"):
                raise ValueError("NULL is not allowed")
            return None

        if target_type == "TEXT":
            if isinstance(value, (bytes, bytearray, memoryview)):
                raise ValueError("binary data cannot be converted safely to text")
            return str(value)

        if target_type == "INTEGER":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                if not math.isfinite(value) or not value.is_integer():
                    raise ValueError("value is not a whole number")
                return int(value)
            text = str(value).strip()
            number = float(text)
            if not math.isfinite(number) or not number.is_integer():
                raise ValueError("value is not a whole number")
            return int(number)

        if target_type == "REAL":
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("value is not a finite number")
            return number

        if target_type == "BLOB":
            if isinstance(value, bytes):
                return value
            if isinstance(value, (bytearray, memoryview)):
                return bytes(value)
            raise ValueError("value is not binary data")

        raise ValueError(f"unsupported target type {target_type or 'unknown'}")

    def _check_field_conversion(self, source_path, table_name, field_name, source_type, target_type, source_fields, target_meta, critical, info):
        primary_key = next((name for name, meta in source_fields.items() if meta["pk"]), None)
        identifier_sql = self._quote_identifier(primary_key) if primary_key else "rowid"
        sql = f"SELECT {identifier_sql}, {self._quote_identifier(field_name)} FROM {self._quote_identifier(table_name)}"
        failures = []
        failure_count = 0

        with sqlite3.connect(source_path) as conn:
            for fid, value in conn.execute(sql):
                try:
                    self._convert_field_value(value, target_type, target_meta)
                except (TypeError, ValueError, OverflowError) as exc:
                    failure_count += 1
                    if len(failures) < 20:
                        failures.append(f"FID {fid}: {value!r} ({exc})")

        source_name = self._geopackage_label(source_path)
        if failure_count:
            details = "; ".join(failures)
            if failure_count > len(failures):
                details += f"; and {failure_count - len(failures)} additional incompatible value(s)"
            critical.append(f"Field conversion failed in GeoPackage '{source_name}', field '{table_name}.{field_name}', from {source_type} to {target_type}: {details}.")
        else:
            info.append(f"Field '{table_name}.{field_name}' in {source_name} will be converted from {source_type} to {target_type}.")

    def _check_layer_geometries(self, gpkg_path, layer_name, critical):
        """Add critical issues for source features with unusable geometries."""

        layer = self._open_layer(gpkg_path, layer_name)
        geopackage_name = self._geopackage_label(gpkg_path)

        if not layer.isValid():
            critical.append(
                f"Could not open layer '{layer_name}' in "
                f"'{geopackage_name}'."
            )
            return

        for feature in layer.getFeatures():
            fid = feature.id()
            geometry = feature.geometry()

            if (
                not feature.hasGeometry()
                or geometry is None
                or geometry.isNull()
            ):
                critical.append(
                    f"Null geometry: {geopackage_name}, "
                    f"layer '{layer_name}', FID {fid}."
                )
                continue

            if geometry.isEmpty():
                critical.append(
                    f"Empty geometry: GeoPackage '{geopackage_name}', "
                    f"layer '{layer_name}', FID {fid}."
                )
                continue

            if not geometry.isGeosValid():
                critical.append(
                    f"Invalid geometry: GeoPackage '{geopackage_name}', "
                    f"layer '{layer_name}', FID {fid}."
                )
    
    def _collect_exact_geometry_duplicates(
        self,
        gpkg_path,
        layer_name,
        key_fields,
        store,
    ):
        """Collect attribute and geometry fingerprints for duplicate comparison."""

        layer = self._open_layer(gpkg_path, layer_name)
        if not layer.isValid():
            return

        available_fields = {
            field.name()
            for field in layer.fields()
        }

        if not all(field_name in available_fields for field_name in key_fields):
            return

        path_key = self._normalised_path_key(gpkg_path)

        for feature in layer.getFeatures():
            geometry = feature.geometry()

            # Geometry problems are reported separately as critical issues.
            if (
                not feature.hasGeometry()
                or geometry is None
                or geometry.isNull()
                or geometry.isEmpty()
            ):
                continue

            key_values = tuple(
                None
                if feature[field_name] is None
                else str(feature[field_name])
                for field_name in key_fields
            )

            geometry_hash = hashlib.sha256(
                bytes(geometry.asWkb())
            ).hexdigest()

            fingerprint = (
                layer_name,
                tuple(key_fields),
                key_values,
                geometry_hash,
            )

            store[fingerprint].append(
                {
                    "path": gpkg_path,
                    "path_key": path_key,
                    "fid": feature.id(),
                }
            )
    
    def _check_exact_geometry_duplicates(self, store, critical):
        """Report exact duplicates found in different GeoPackages."""

        for fingerprint, locations in store.items():
            layer_name, key_fields, key_values, _geometry_hash = fingerprint

            geopackage_keys = {
                location["path_key"]
                for location in locations
            }

            # Do not flag duplicates that exist only within one GeoPackage.
            if len(geopackage_keys) < 2:
                continue

            field_values = ", ".join(
                f"{field_name}={value!r}"
                for field_name, value in zip(key_fields, key_values)
            )

            feature_locations = "; ".join(
                f"{self._geopackage_label(location['path'])}, layer '{layer_name}', FID {location['fid']}"
                for location in locations
            )

            critical.append(
                f"Exact duplicate feature: {field_values}. "
                f"Matching geometry found in {feature_locations}."
            )

    def _collect_duplicate_values(self, gpkg_path, table_name, store):
        """Collect configured values for comparison between GeoPackages."""
        key_fields = self.CRITICAL_DUPLICATE_KEY_FIELDS.get(table_name)
        if not key_fields:
            return

        values = self._read_key_values(gpkg_path, table_name, key_fields)
        for value, fid in values:
            if value in (None, ""):
                continue
            store[(table_name, tuple(key_fields), value)].append((gpkg_path, fid))
    
    def _check_duplicates_against_target(self, source_path, target_path, table_name, critical):
        """Report configured values already present in the main GeoPackage."""
        key_fields = self.CRITICAL_DUPLICATE_KEY_FIELDS.get(table_name)
        if not key_fields:
            return

        source_values = self._read_key_values(source_path, table_name, key_fields)
        target_values = self._read_key_values(target_path, table_name, key_fields)
        target_locations = defaultdict(list)

        for value, fid in target_values:
            if value not in (None, ""):
                target_locations[value].append(fid)

        source_name = self._geopackage_label(source_path)
        target_name = self._geopackage_label(target_path)

        for value, source_fid in source_values:
            if value in (None, "") or value not in target_locations:
                continue

            target_fids = ", ".join(str(fid) for fid in target_locations[value])
            critical.append(
                f"Duplicate value in '{table_name}' for {key_fields} = {value!r}: "
                f"{source_name}, FID {source_fid}; {target_name}, FID(s) {target_fids}."
            )

    def _check_duplicates_between_sources(self, incoming_duplicate_values, critical):
        """Report configured values found in different source GeoPackages."""
        for (table_name, key_fields, value), locations in incoming_duplicate_values.items():
            distinct_paths = {
                self._normalised_path_key(path)
                for path, _fid in locations
            }

            if len(distinct_paths) < 2:
                continue

            feature_locations = "; ".join(
                f"GeoPackage '{os.path.basename(path)}', FID {fid}"
                for path, fid in locations
            )
            critical.append(
                f"Duplicate incoming value in '{table_name}' for "
                f"{list(key_fields)} = {value}: {feature_locations}."
            )

    def _read_key_values(self, gpkg_path, table_name, key_fields):
        schema = self._read_gpkg_schema(gpkg_path)
        fields = {}
        if table_name in schema["layers"]:
            fields = schema["layers"][table_name]["fields"]
        elif table_name in schema["tables"]:
            fields = schema["tables"][table_name]["fields"]
        if not all(field in fields for field in key_fields):
            return []

        fid_field = next((name for name, meta in fields.items() if meta["pk"]), "fid")
        quoted_keys = ", ".join([self._quote_identifier(f) for f in key_fields])
        expression = f"SELECT {self._quote_identifier(fid_field)}, {quoted_keys} FROM {self._quote_identifier(table_name)}"
        with sqlite3.connect(gpkg_path) as conn:
            rows = conn.execute(expression).fetchall()
        values = []
        for row in rows:
            fid = row[0]
            key_parts = row[1:]
            value = key_parts[0] if len(key_parts) == 1 else tuple(key_parts)
            values.append((value, fid))
        return values

    # ------------------------------------------------------------------
    # Merge execution
    # ------------------------------------------------------------------

    def _execute_merge(self, target_path, plan):
        copied_count = 0
        created_layers = set()
        created_layer_sources = []
        created_tables = set()
        total_actions = max(len(plan), 1)

        for index, (action, source_path, name) in enumerate(plan, start=1):
            source_label = self._geopackage_label(source_path)
            self._set_progress(10 + int(80 * (index - 1) / total_actions), f"Merging '{name}' from {source_label}...")

            if action == "create":
                if name not in created_layers:
                    self._create_missing_vector_layer(source_path, target_path, name)
                    created_layers.add(name)
                    created_layer_sources.append((source_path, name))
                    copied_count += self._feature_count(source_path, name)
                else:
                    copied_count += self._append_vector_layer(source_path, target_path, name)
            elif action == "append":
                copied_count += self._append_vector_layer(source_path, target_path, name)
            elif action == "copy_table":
                if name not in created_tables:
                    self._copy_missing_attribute_table(source_path, target_path, name)
                    created_tables.add(name)
                copied_count += self._append_attribute_table(source_path, target_path, name)
            elif action == "append_table":
                copied_count += self._append_attribute_table(source_path, target_path, name)

            self._set_progress(10 + int(80 * index / total_actions), f"Merged '{name}' from {source_label}.")

        return copied_count, created_layer_sources
    
    def _ensure_geopackage_source_field(self, layer):
        """Ensure the merged layer has a field recording the source GeoPackage path."""
        field_name = self.GEOPACKAGE_SOURCE_FIELD

        existing_index = layer.fields().indexFromName(field_name)
        if existing_index != -1:
            return existing_index

        provider = layer.dataProvider()
        if not provider.addAttributes([QgsField(field_name, QVariant.String)]):
            raise RuntimeError(f"Could not add '{field_name}' field to '{layer.name()}'.")

        layer.updateFields()

        field_index = layer.fields().indexFromName(field_name)
        if field_index == -1:
            raise RuntimeError(f"'{field_name}' field was not created on '{layer.name()}'.")

        return field_index
    
    def _ensure_missing_vector_fields(self, source_layer, target_layer):
        target_names = {field.name() for field in target_layer.fields()}
        missing_fields = []

        for field in source_layer.fields():
            if field.name() in target_names or field.name() in ("fid", "ogc_fid", self.GEOPACKAGE_SOURCE_FIELD):
                continue

            missing_fields.append(QgsField(
                field.name(),
                field.type(),
                field.typeName(),
                field.length(),
                field.precision(),
                field.comment()
            ))

        if missing_fields and not target_layer.dataProvider().addAttributes(missing_fields):
            names = ", ".join(field.name() for field in missing_fields)
            raise RuntimeError(f"Could not add fields to '{target_layer.name()}': {names}.")

        if missing_fields:
            target_layer.updateFields()

    def _append_vector_layer(self, source_path, target_path, layer_name):
        source_layer = self._open_layer(source_path, layer_name)
        target_layer = self._open_layer(target_path, layer_name)
        if not source_layer.isValid() or not target_layer.isValid():
            raise RuntimeError(f"Could not open '{layer_name}' for copying.")

        self._ensure_missing_vector_fields(source_layer, target_layer)
        self._ensure_geopackage_source_field(target_layer)

        target_fields = target_layer.fields()
        source_fields = source_layer.fields()
        target_field_names = [field.name() for field in target_fields]
        source_index_by_name = {field.name(): idx for idx, field in enumerate(source_fields)}
        target_field_by_name = {field.name(): field for field in target_fields}
        

        target_layer.startEditing()
        try:
            new_features = []
            for source_feature in source_layer.getFeatures():
                new_feature = QgsFeature(target_fields)
                new_feature.setGeometry(source_feature.geometry())
                attrs = []
                for field_name in target_field_names:
                    if field_name in ("fid", "ogc_fid"):
                        attrs.append(None)
                    elif field_name == self.GEOPACKAGE_SOURCE_FIELD:
                        attrs.append(os.path.abspath(source_path))
                    elif field_name in source_index_by_name:
                        source_index = source_index_by_name[field_name]
                        value = source_feature[source_index]
                        source_type = self._normalise_field_type(source_fields[source_index].typeName())
                        target_type = self._normalise_field_type(target_field_by_name[field_name].typeName())
                        if source_type != target_type:
                            value = self._convert_field_value(value, target_type)
                        attrs.append(value)
                    else:
                        attrs.append(None)
                new_feature.setAttributes(attrs)
                new_features.append(new_feature)

            if new_features:
                ok = target_layer.addFeatures(new_features)
                if not ok:
                    raise RuntimeError(f"QGIS could not add features to '{layer_name}'.")
            if not target_layer.commitChanges():
                raise RuntimeError("; ".join(target_layer.commitErrors()))
            return len(new_features)
        except Exception:
            target_layer.rollBack()
            raise

    def _append_attribute_table(self, source_path, target_path, table_name, skip_existing=False):
        if skip_existing:
            return 0
        source_schema = self._read_gpkg_schema(source_path)
        target_schema = self._read_gpkg_schema(target_path)
        source_fields = source_schema["tables"][table_name]["fields"]
        target_fields = target_schema["tables"][table_name]["fields"]
        missing_fields = [name for name, meta in source_fields.items() if not meta["pk"] and name not in target_fields]

        if missing_fields:
            with sqlite3.connect(target_path) as target_conn:
                for field_name in missing_fields:
                    field_type = self._normalise_field_type(source_fields[field_name]["type"])
                    if field_type not in ("INTEGER", "REAL", "TEXT", "BLOB"):
                        field_type = "TEXT"

                    target_conn.execute(
                        f"ALTER TABLE {self._quote_identifier(table_name)} "
                        f"ADD COLUMN {self._quote_identifier(field_name)} {field_type}"
                    )

                target_conn.commit()

            target_schema = self._read_gpkg_schema(target_path)
            target_fields = target_schema["tables"][table_name]["fields"]

        target_non_pk = [name for name, meta in target_fields.items() if not meta["pk"]]
        source_non_pk = [name for name, meta in source_fields.items() if not meta["pk"]]
        common = [name for name in target_non_pk if name in source_non_pk]
        if not common:
            return 0

        with sqlite3.connect(source_path) as source_conn, sqlite3.connect(target_path) as target_conn:
            source_sql = f"SELECT {', '.join(self._quote_identifier(f) for f in common)} FROM {self._quote_identifier(table_name)}"
            rows = source_conn.execute(source_sql).fetchall()
            if not rows:
                return 0

            converted_rows = []
            for row in rows:
                converted_row = []
                for field_name, value in zip(common, row):
                    source_type = self._normalise_field_type(source_fields[field_name]["type"])
                    target_type = self._normalise_field_type(target_fields[field_name]["type"])
                    if source_type != target_type:
                        value = self._convert_field_value(value, target_type, target_fields[field_name])
                    converted_row.append(value)
                converted_rows.append(tuple(converted_row))

            placeholders = ", ".join(["?"] * len(common))
            insert_sql = (
                f"INSERT INTO {self._quote_identifier(table_name)} "
                f"({', '.join(self._quote_identifier(f) for f in common)}) VALUES ({placeholders})"
            )
            target_conn.executemany(insert_sql, converted_rows)
            target_conn.commit()
            return len(converted_rows)

    def _create_missing_vector_layer(self, source_path, target_path, layer_name):
        source_layer = self._open_layer(source_path, layer_name)
        if not source_layer.isValid():
            raise RuntimeError(f"Could not open source layer '{layer_name}' to create it in the main GeoPackage.")

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = layer_name
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        options.fileEncoding = "UTF-8"
        options.onlySelectedFeatures = False

        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            source_layer,
            target_path,
            QgsProject.instance().transformContext(),
            options,
        )
        if result[0] != QgsVectorFileWriter.NoError:
            raise RuntimeError(f"Could not create missing layer '{layer_name}': {result[1]}")
        
        created_layer = self._open_layer(target_path, layer_name)
        if not created_layer.isValid():
            raise RuntimeError(f"Could not reopen created layer '{layer_name}' to add source tracking.")

        source_field_index = self._ensure_geopackage_source_field(created_layer)

        created_layer.startEditing()
        try:
            for feature in created_layer.getFeatures():
                created_layer.changeAttributeValue(
                    feature.id(),
                    source_field_index,
                    os.path.abspath(source_path)
                )

            if not created_layer.commitChanges():
                raise RuntimeError("; ".join(created_layer.commitErrors()))
        except Exception:
            created_layer.rollBack()
            raise

    def _copy_missing_attribute_table(self, source_path, target_path, table_name):
        source_schema = self._read_gpkg_schema(source_path)
        fields = source_schema["tables"][table_name]["fields"]
        with sqlite3.connect(source_path) as source_conn, sqlite3.connect(target_path) as target_conn:
            create_row = source_conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
            ).fetchone()
            if not create_row or not create_row[0]:
                raise RuntimeError(f"Could not read CREATE statement for '{table_name}'.")
            target_conn.execute(create_row[0])
            target_conn.execute(
                "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, last_change, srs_id) "
                "VALUES (?, 'attributes', ?, '', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 0)",
                (table_name, table_name),
            )
            target_conn.commit()

    def _copy_styles_for_created_layers(self, target_path, created_layers):
        """Copy source styles only for layers newly created in the main GeoPackage."""

        for source_path, layer_name in created_layers:
            if not self._table_exists(source_path, "layer_styles"):
                continue

            with sqlite3.connect(source_path) as source_conn:
                source_columns = [
                    row[1]
                    for row in source_conn.execute(
                        "PRAGMA table_info('layer_styles')"
                    ).fetchall()
                ]

                if "f_table_name" not in source_columns:
                    continue

                create_row = source_conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='layer_styles'"
                ).fetchone()

                with sqlite3.connect(target_path) as target_conn:
                    if not self._table_exists(target_path, "layer_styles"):
                        if not create_row or not create_row[0]:
                            continue

                        target_conn.execute(create_row[0])

                    target_columns = {
                        row[1]
                        for row in target_conn.execute(
                            "PRAGMA table_info('layer_styles')"
                        ).fetchall()
                    }

                    insert_columns = [
                        column
                        for column in source_columns
                        if column != "id" and column in target_columns
                    ]

                    if "f_table_name" not in insert_columns:
                        continue

                    quoted_columns = ", ".join(
                        self._quote_identifier(column)
                        for column in insert_columns
                    )

                    rows = source_conn.execute(
                        f"SELECT {quoted_columns} FROM layer_styles "
                        "WHERE f_table_name = ?",
                        (layer_name,),
                    ).fetchall()

                    if not rows:
                        continue

                    placeholders = ", ".join(
                        "?" for _ in insert_columns
                    )

                    target_conn.executemany(
                        f"INSERT INTO layer_styles ({quoted_columns}) "
                        f"VALUES ({placeholders})",
                        rows,
                    )
                    target_conn.commit()
    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _open_layer(self, gpkg_path, layer_name):
        return QgsVectorLayer(f"{gpkg_path}|layername={layer_name}", layer_name, "ogr")

    def _feature_count(self, gpkg_path, layer_name):
        layer = self._open_layer(gpkg_path, layer_name)
        return layer.featureCount() if layer.isValid() else 0

    def _table_count(self, gpkg_path, table_name):
        with sqlite3.connect(gpkg_path) as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM {self._quote_identifier(table_name)}").fetchone()[0])

    def _table_exists(self, gpkg_path, table_name):
        with sqlite3.connect(gpkg_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
            ).fetchone()
        return row is not None

    def _create_backup(self, target_path):
        base, ext = os.path.splitext(target_path)
        backup_path = f"{base}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        shutil.copy2(target_path, backup_path)
        return backup_path

    def _quote_identifier(self, value):
        return '"' + str(value).replace('"', '""') + '"'

    def _format_report(self, critical, warnings, info, plan):
        lines = [
            "GEOPACKAGE MERGER REPORT",
            "========================",
            f"Created: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
            "",
            "SUMMARY",
            "-------",
            f"Critical issues: {len(critical)}",
            f"Warnings: {len(warnings)}",
            "",
        ]
        
        target_path = self.dlg.targetLineEdit.text().strip()
        source_paths = self._source_paths()
        lines.extend(["FILES", "-----"])

        if target_path:
            lines.append(f"Main: {target_path}")

        for index, source_path in enumerate(source_paths, start=1):
            lines.append(f"Source {index}: {source_path}")

        lines.append("")

        if critical:
            lines.extend(["CRITICAL ISSUES - FIX THESE BEFORE MERGING", "-----------------------------------------"])
            lines.extend(f"- {item}" for item in critical)
            lines.append("")
        else:
            lines.extend(["CRITICAL ISSUES", "---------------", "None found.", ""])

        if warnings:
            lines.extend(["WARNINGS", "--------"])
            lines.extend(f"- {item}" for item in warnings)
            lines.append("")

        if info:
            lines.extend(["INFORMATION", "-----------"])
            lines.extend(f"- {item}" for item in info)
            lines.append("")

        if critical:
            lines.append("No data has been copied. Fix the listed issues and run the checks again.")
        else:
            lines.append("Checks passed. No data has been copied yet unless you clicked 'Validate and merge'.")
        return "\n".join(lines)

    def _set_busy(self, is_busy):
        """Show or hide the determinate progress indicator."""
        progress_bar = getattr(self.dlg, "progressBar", None)
        if progress_bar is not None:
            progress_bar.setRange(0, 100)
            progress_bar.setTextVisible(True)
            progress_bar.setVisible(is_busy)
            if is_busy:
                progress_bar.setValue(0)
            else:
                progress_bar.setValue(0)
        QApplication.processEvents()

    def _set_progress(self, value, status_text=None):
        """Update progress and allow QGIS to repaint during synchronous work."""
        progress_bar = getattr(self.dlg, "progressBar", None)
        if progress_bar is not None:
            progress_bar.setRange(0, 100)
            progress_bar.setValue(max(0, min(100, int(value))))
        if status_text:
            self._set_status(status_text, "neutral")
        else:
            QApplication.processEvents()

    def _set_status(self, text, status_type="neutral"):
        """Show inline status text using accessible colours instead of QGIS message bars."""
        label = getattr(self.dlg, "statusLabel", None)
        if label is None:
            return

        colours = {
            "success": "#2e7d32",
            "warning": "#c46a00",
            "error": "#b3261e",
            "neutral": "",
        }
        colour = colours.get(status_type, "")
        label.setText(text)
        label.setStyleSheet(f"font-weight: bold; color: {colour};" if colour else "")
        QApplication.processEvents()
