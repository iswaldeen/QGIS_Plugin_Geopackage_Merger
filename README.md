# QGIS Plugin - Geopackage Merger

Geopackage Merger is a QGIS plugin for comparing, validating and combining multiple GeoPackages into a single output GeoPackage while identifying potential conflicts before data is merged.

The plugin performs a series of validation checks to help ensure that source GeoPackages are compatible before merging, reducing the risk of schema inconsistencies, duplicate records and missing data.

Geopackage Merger is designed for compatibility with modern QGIS versions and includes forward-compatible support for Qt6 and upcoming QGIS 4.x environments.

<img src="geopackage_merger/icons/geopackage_merger_icon.png" width="100" height="100">

## Features

* Compare multiple GeoPackages before merging
* Detect missing (disjoint) layers between GeoPackages
* Compare layer schemas including field names and field types
* Identify potential duplicate features
* Detect primary key and geometry conflicts
* Validate GeoPackages before performing a merge
* Generate a detailed validation report highlighting blocking issues
* Merge compatible GeoPackages into a single output file
* Preserve source information by recording the originating GeoPackage
* Integrated help documentation
* Configurable plugin settings
* Direct access to Settings and Help from the QGIS Plugins menu
* Theme-aware UI and menu icon support for both light and dark QGIS themes
* PyQt / Qt compatibility handling
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

## License

All content is licensed under the <a href="https://creativecommons.org/licenses/by-sa/3.0/">Creative Commons Attribution-ShareAlike 3.0 licence (CC BY-SA)</a>.