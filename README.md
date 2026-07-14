# QGIS Plugin - Geopackage Merger

Geopackage Merger is a QGIS plugin for comparing and validating one main GeoPackage against one or more source GeoPackages before combining them.

The plugin reports critical issues, warnings and information before any data is copied. Critical issues prevent the merge, while warnings allow the user to review possible concerns.

Geopackage Merger is designed for compatibility with modern QGIS versions and includes forward-compatible support for Qt6 and upcoming QGIS 4.x environments.

<img src="geopackage_merger/icons/geopackage_merger_icon.png" width="100" height="100">

## Features

* Compare one main GeoPackage with multiple numbered sources
* Detect missing layers, tables and fields
* Add compatible missing fields and use `NULL` where values are unavailable
* Validate and safely convert compatible field data types
* Detect null, empty and invalid geometries
* Detect configured duplicate identifiers between GeoPackages
* Detect exact cross-GeoPackage feature duplicates using attributes and geometry
* Report critical issues, warnings and information using clear source labels
* Create missing layers and attribute tables when enabled
* Preserve styles in existing main layers
* Copy available source styles to newly created layers
* Record each copied feature's origin in `geopackage_source`
* Create an optional timestamped backup before merging
* Show progress and status feedback during checks and merging
* Include configurable settings and integrated help documentation
* Support light and dark QGIS themes
* Provide Settings and Help through the QGIS Plugins menu
* Forward-compatible design for:

  * QGIS 3.x
  * QGIS 4.x
  * Qt5
  * Qt6

## Compatibility

Geopackage Merger is actively developed and tested for:

* QGIS 3.28+
* PyQt5 / Qt5
* Forward compatibility with Qt6 and QGIS 4.x APIs where possible

The plugin avoids deprecated Qt and QGIS API usage where practical to support long-term maintainability.

## Basic workflow

1. Select the main GeoPackage that will receive the data.
2. Add one or more source GeoPackages.
3. Run the mandatory checks and review the report.
4. Resolve any critical issues.
5. Validate and merge the compatible data.

Detailed user guidance is available from the plugin's Help button or **Plugins > Geopackage Merger > Help**.

## License

This project is licensed under the GNU General Public License v2. See the `LICENSE` file for details.
