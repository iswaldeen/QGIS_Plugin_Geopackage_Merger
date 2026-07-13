# -*- coding: utf-8 -*-
"""
Geopackage Merger

Implementation for comparing one or more source GeoPackages against
one main GeoPackage, reporting blocking issues, and only copying data once the
pre-copy checks have passed.
"""

import os
import shutil
import sqlite3
import webbrowser
from collections import defaultdict
from datetime import datetime

from qgis.PyQt.QtCore import QCoreApplication, QLocale, QTranslator, QVariant
from qgis.PyQt.QtGui import QIcon, QPalette
from qgis.PyQt.QtWidgets import QAction, QApplication, QFileDialog, QListWidgetItem

from qgis.core import (
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

    # Common PCA duplicate checks based on the supplied example site-plan GeoPackage.
    DUPLICATE_KEY_FIELDS = {
        "Archaeological_Features": ["context_no"],
        "Archaeological_Features_LN": ["context_no"],
        "Burials": ["context_no"],
        "DRS_Context_Database": ["Context"],
        "DRS_Trench_Database": ["Trench_Number"],
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

            self.dlg.sourceListWidget.addItem(QListWidgetItem(path))
            existing.add(path_key)
            added_count += 1

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
        self.last_preflight_ok = False
        self.dlg.mergeButton.setEnabled(False)
        
        self._set_status(
            "Sources changed. Run checks before merging.",
            "neutral"
        )

    def _clear_sources(self):
        self.dlg.sourceListWidget.clear()
        self.last_preflight_ok = False
        self.dlg.mergeButton.setEnabled(False)
        
        self._set_status(
            "Sources cleared. Add source GeoPackages and run checks before merging.",
            "neutral"
        )

    def _source_paths(self):
        return [self.dlg.sourceListWidget.item(i).text() for i in range(self.dlg.sourceListWidget.count())]
    
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
                self._set_status("GeoPackage checks found blocking issues. Fix them and retry.", "warning")
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
                self._set_status("Merge cancelled. Blocking issues must be fixed first.", "warning")
                return

            target_path = self.dlg.targetLineEdit.text().strip()
            try:
                self._set_status("Checks passed. Copying data into the main GeoPackage...", "neutral")

                backup_path = None
                if self._setting_checked("backupCheckBox", True):
                    backup_path = self._create_backup(target_path)

                copied_count = self._execute_merge(target_path, plan)

                if self._setting_checked("copyStylesCheckBox", True):
                    self._copy_layer_styles(target_path, self._source_paths())

                final_lines = [report, "", "MERGE COMPLETE", "--------------", f"Features copied: {copied_count}"]
                if backup_path:
                    final_lines.append(f"Backup created: {backup_path}")

                self.last_report = "\n".join(final_lines)
                self.dlg.reportTextEdit.setPlainText(self.last_report)
                self._set_status(f"Merge complete. {copied_count} features copied.", "success")
                QgsProject.instance().reloadAllLayers()

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
        blocking = []
        warnings = []
        info = []
        plan = []

        self._validate_selected_paths(target_path, source_paths, blocking)
        if blocking:
            return False, self._format_report(blocking, warnings, info, plan), plan

        target_schema = self._read_gpkg_schema(target_path)
        target_layers = target_schema["layers"]
        target_tables = target_schema["tables"]

        if target_schema["rasters"]:
            warnings.append("Main GeoPackage contains raster tile tables. Raster copying is detected but not copied by this first version.")

        incoming_duplicate_values = defaultdict(list)

        for source_path in source_paths:
            source_schema = self._read_gpkg_schema(source_path)
            if source_schema["rasters"]:
                warnings.append(f"{os.path.basename(source_path)} contains raster tile tables. Raster layers are not copied by this first version.")
                
            self._check_postex_presence(source_path, source_schema, warnings, info)

            for layer_name, source_layer_meta in source_schema["layers"].items():
                source_count = self._feature_count(source_path, layer_name)
                if source_count == 0 and self._setting_checked("ignoreEmptySourceLayersCheckBox", True):
                    continue

                target_meta = target_layers.get(layer_name)
                if target_meta is None:
                    if self._setting_checked("createMissingLayersCheckBox", False):
                        plan.append(("create", source_path, layer_name))
                        info.append(f"Will create missing layer '{layer_name}' from {os.path.basename(source_path)}.")
                    else:
                        blocking.append(f"Missing layer in main GeoPackage: '{layer_name}' from {os.path.basename(source_path)}.")
                    continue

                if True:
                    self._check_layer_compatibility(layer_name, source_layer_meta, target_meta, blocking)

                if True:
                    self._check_layer_geometries(source_path, layer_name, blocking)

                if True:
                    self._collect_duplicate_values(source_path, layer_name, incoming_duplicate_values)
                    self._check_duplicates_against_target(source_path, target_path, layer_name, blocking)

                plan.append(("append", source_path, layer_name))

            # Attribute-only DRS tables.
            for table_name, source_table_meta in source_schema["tables"].items():
                if table_name == "layer_styles":
                    continue
                source_count = self._table_count(source_path, table_name)
                if source_count == 0 and self._setting_checked("ignoreEmptySourceLayersCheckBox", True):
                    continue

                target_meta = target_tables.get(table_name)
                if target_meta is None:
                    if self._setting_checked("createMissingLayersCheckBox", False):
                        plan.append(("copy_table", source_path, table_name))
                        info.append(f"Will create missing attribute table '{table_name}' from {os.path.basename(source_path)}.")
                    else:
                        blocking.append(f"Missing attribute table in main GeoPackage: '{table_name}' from {os.path.basename(source_path)}.")
                    continue

                if True:
                    self._check_table_fields(table_name, source_table_meta["fields"], target_meta["fields"], blocking)

                if True:
                    self._collect_duplicate_values(source_path, table_name, incoming_duplicate_values)
                    self._check_duplicates_against_target(source_path, target_path, table_name, blocking)

                plan.append(("append_table", source_path, table_name))

        if True:
            self._check_duplicates_between_sources(incoming_duplicate_values, blocking)

        if True:
            self._check_styles(source_paths, warnings, info)

        return not blocking, self._format_report(blocking, warnings, info, plan), plan

    def _validate_selected_paths(self, target_path, source_paths, blocking):
        target_key = self._normalised_path_key(target_path) if target_path else ""

        if not target_path:
            blocking.append("No main GeoPackage selected.")
        elif not os.path.isfile(target_path):
            blocking.append(f"Main GeoPackage does not exist: {target_path}")
        elif not target_path.lower().endswith(self.SUPPORTED_GPKG_EXT):
            blocking.append("Main file is not a .gpkg file.")

        if not source_paths:
            blocking.append("No source GeoPackages selected.")
            return

        seen_sources = {}

        for source_path in source_paths:
            source_key = self._normalised_path_key(source_path)

            if source_key in seen_sources:
                blocking.append(
                    "The same source GeoPackage has been selected more than once: "
                    f"{source_path}"
                )
                continue

            seen_sources[source_key] = source_path

            if not os.path.isfile(source_path):
                blocking.append(f"Source GeoPackage does not exist: {source_path}")
            elif not source_path.lower().endswith(self.SUPPORTED_GPKG_EXT):
                blocking.append(f"Source file is not a .gpkg file: {source_path}")
            elif target_key and source_key == target_key:
                blocking.append(
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
                "pk": bool(row[5]),
            }
            for row in rows
        }

    def _check_layer_compatibility(self, layer_name, source_meta, target_meta, blocking):
        if int(source_meta.get("srs_id") or 0) != int(target_meta.get("srs_id") or 0):
            blocking.append(
                f"CRS mismatch in '{layer_name}': source EPSG/SRS {source_meta.get('srs_id')} vs main {target_meta.get('srs_id')}.")

        source_geom = (source_meta.get("geometry_type") or "").upper()
        target_geom = (target_meta.get("geometry_type") or "").upper()
        if source_geom and target_geom and source_geom != target_geom:
            blocking.append(f"Geometry type mismatch in '{layer_name}': source {source_geom} vs main {target_geom}.")

        self._check_table_fields(layer_name, source_meta["fields"], target_meta["fields"], blocking)

    def _check_table_fields(self, table_name, source_fields, target_fields, blocking):
        source_names = {n for n, m in source_fields.items() if not m["pk"]}
        target_names = {n for n, m in target_fields.items() if not m["pk"]}

        missing_in_target = sorted(source_names - target_names)
        if missing_in_target:
            blocking.append(
                f"Field mismatch in '{table_name}': main GeoPackage is missing {', '.join(missing_in_target)}."
            )

        common = sorted(source_names & target_names)
        for field_name in common:
            src_type = self._normalise_field_type(source_fields[field_name]["type"])
            dst_type = self._normalise_field_type(target_fields[field_name]["type"])
            if src_type and dst_type and src_type != dst_type:
                blocking.append(
                    f"Field type mismatch in '{table_name}.{field_name}': source {source_fields[field_name]['type']} vs main {target_fields[field_name]['type']}."
                )

    def _normalise_field_type(self, field_type):
        ft = (field_type or "").upper()
        if "INT" in ft:
            return "INTEGER"
        if any(token in ft for token in ("REAL", "DOUBLE", "FLOAT", "NUMERIC")):
            return "REAL"
        if any(token in ft for token in ("TEXT", "CHAR", "CLOB", "DATE", "TIME", "BOOLEAN")):
            return "TEXT"
        return ft

    def _check_layer_geometries(self, gpkg_path, layer_name, blocking):
        layer = self._open_layer(gpkg_path, layer_name)
        if not layer.isValid():
            blocking.append(f"Could not open layer '{layer_name}' from {os.path.basename(gpkg_path)}.")
            return

        for feature in layer.getFeatures():
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                blocking.append(f"Empty geometry in {os.path.basename(gpkg_path)} / {layer_name}, fid {feature.id()}.")
                continue
            if not geom.isGeosValid():
                blocking.append(f"Invalid geometry in {os.path.basename(gpkg_path)} / {layer_name}, fid {feature.id()}.")

    def _collect_duplicate_values(self, gpkg_path, table_name, store):
        key_fields = self.DUPLICATE_KEY_FIELDS.get(table_name)
        if not key_fields:
            return
        values = self._read_key_values(gpkg_path, table_name, key_fields)
        seen_here = defaultdict(list)
        for value, fid in values:
            if value in (None, ""):
                continue
            seen_here[value].append(fid)
            store[(table_name, tuple(key_fields), value)].append((gpkg_path, fid))

        for value, fids in seen_here.items():
            if len(fids) > 1:
                store[(table_name, tuple(key_fields), value)].append((gpkg_path, f"duplicate in same source: {fids}"))

    def _check_duplicates_against_target(self, source_path, target_path, table_name, blocking):
        key_fields = self.DUPLICATE_KEY_FIELDS.get(table_name)
        if not key_fields:
            return
        source_values = {value for value, _ in self._read_key_values(source_path, table_name, key_fields) if value not in (None, "")}
        if not source_values:
            return
        target_values = {value for value, _ in self._read_key_values(target_path, table_name, key_fields) if value not in (None, "")}
        duplicates = sorted(source_values & target_values, key=lambda v: str(v))
        if duplicates:
            sample = ", ".join(str(v) for v in duplicates[:20])
            suffix = "..." if len(duplicates) > 20 else ""
            blocking.append(
                f"Duplicate {table_name} record values already exist in main GeoPackage for {key_fields}: {sample}{suffix}"
            )

    def _check_duplicates_between_sources(self, incoming_duplicate_values, blocking):
        for (table_name, key_fields, value), locations in incoming_duplicate_values.items():
            source_paths = {os.path.basename(path) for path, _fid in locations}
            if len(locations) > 1:
                blocking.append(
                    f"Duplicate incoming value in '{table_name}' for {list(key_fields)} = {value}: found in {', '.join(sorted(source_paths))}."
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

    def _check_postex_presence(self, source_path, source_schema, warnings, info):
        if "Features_for_PostEx" in source_schema["layers"]:
            count = self._feature_count(source_path, "Features_for_PostEx")
            if count:
                info.append(f"{os.path.basename(source_path)} contains {count} Features_for_PostEx records.")
            else:
                warnings.append(f"{os.path.basename(source_path)} contains Features_for_PostEx, but it is empty.")
        else:
            warnings.append(f"{os.path.basename(source_path)} does not contain a Features_for_PostEx layer.")

    def _check_styles(self, source_paths, warnings, info):
        for path in source_paths:
            try:
                count = self._table_count(path, "layer_styles")
                if count:
                    info.append(f"{os.path.basename(path)} contains {count} layer style records.")
            except Exception:
                warnings.append(f"{os.path.basename(path)} does not contain a readable layer_styles table.")

    # ------------------------------------------------------------------
    # Merge execution
    # ------------------------------------------------------------------

    def _execute_merge(self, target_path, plan):
        copied_count = 0
        created_layers = set()
        created_tables = set()

        for action, source_path, name in plan:
            if action == "create":
                if name not in created_layers:
                    self._create_missing_vector_layer(source_path, target_path, name)
                    created_layers.add(name)
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

        return copied_count
    
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

    def _append_vector_layer(self, source_path, target_path, layer_name):
        source_layer = self._open_layer(source_path, layer_name)
        target_layer = self._open_layer(target_path, layer_name)
        if not source_layer.isValid() or not target_layer.isValid():
            raise RuntimeError(f"Could not open '{layer_name}' for copying.")

        self._ensure_geopackage_source_field(target_layer)

        target_fields = target_layer.fields()
        source_fields = source_layer.fields()
        target_field_names = [field.name() for field in target_fields]
        source_index_by_name = {field.name(): idx for idx, field in enumerate(source_fields)}

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
                        attrs.append(source_feature[source_index_by_name[field_name]])
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
            placeholders = ", ".join(["?"] * len(common))
            insert_sql = (
                f"INSERT INTO {self._quote_identifier(table_name)} "
                f"({', '.join(self._quote_identifier(f) for f in common)}) VALUES ({placeholders})"
            )
            target_conn.executemany(insert_sql, rows)
            target_conn.commit()
            return len(rows)

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

    def _copy_layer_styles(self, target_path, source_paths):
        if not self._table_exists(target_path, "layer_styles"):
            return
        copied = 0
        with sqlite3.connect(target_path) as target_conn:
            target_style_keys = set(
                target_conn.execute(
                    "SELECT f_table_name, styleName FROM layer_styles"
                ).fetchall()
            )
            for source_path in source_paths:
                if not self._table_exists(source_path, "layer_styles"):
                    continue
                with sqlite3.connect(source_path) as source_conn:
                    columns = [row[1] for row in source_conn.execute("PRAGMA table_info('layer_styles')").fetchall()]
                    insert_columns = [c for c in columns if c != "id"]
                    rows = source_conn.execute(
                        f"SELECT {', '.join(self._quote_identifier(c) for c in insert_columns)} FROM layer_styles"
                    ).fetchall()
                    for row in rows:
                        row_dict = dict(zip(insert_columns, row))
                        key = (row_dict.get("f_table_name"), row_dict.get("styleName"))
                        if key in target_style_keys:
                            continue
                        placeholders = ", ".join(["?"] * len(insert_columns))
                        sql = (
                            f"INSERT INTO layer_styles ({', '.join(self._quote_identifier(c) for c in insert_columns)}) "
                            f"VALUES ({placeholders})"
                        )
                        target_conn.execute(sql, row)
                        target_style_keys.add(key)
                        copied += 1
            target_conn.commit()
        if copied:
            self.dlg.reportTextEdit.appendPlainText(f"\nLayer style records copied: {copied}")

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

    def _format_report(self, blocking, warnings, info, plan):
        lines = [
            "GEOPACKAGE MERGER REPORT",
            "========================",
            f"Created: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
            "",
            "SUMMARY",
            "-------",
            f"Blocking issues: {len(blocking)}",
            f"Warnings: {len(warnings)}",
            "",
        ]

        if blocking:
            lines.extend(["BLOCKING ISSUES - FIX THESE BEFORE MERGING", "-----------------------------------------"])
            lines.extend(f"- {item}" for item in blocking)
            lines.append("")
        else:
            lines.extend(["BLOCKING ISSUES", "---------------", "None found.", ""])

        if warnings:
            lines.extend(["WARNINGS", "--------"])
            lines.extend(f"- {item}" for item in warnings)
            lines.append("")

        if info:
            lines.extend(["INFORMATION", "-----------"])
            lines.extend(f"- {item}" for item in info)
            lines.append("")

        if blocking:
            lines.append("No data has been copied. Fix the listed issues and run the checks again.")
        else:
            lines.append("Checks passed. No data has been copied yet unless you clicked 'Validate and merge'.")
        return "\n".join(lines)

    def _set_busy(self, is_busy):
        """Show or hide the inline progress indicator while checks or merging are running."""
        progress_bar = getattr(self.dlg, "progressBar", None)
        if progress_bar is not None:
            # Use an indeterminate progress bar because checks run as one synchronous task.
            progress_bar.setRange(0, 0)
            progress_bar.setVisible(is_busy)
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
