# -*- coding: utf-8 -*-

def classFactory(iface):
    from .geopackage_merger import GeopackageMerger
    return GeopackageMerger(iface)
