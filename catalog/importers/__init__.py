"""One importer per supplier file layout. config/catalog.json -> businesses.<ID>.importer picks one."""

from catalog.importers.hcc import import_hcc
from catalog.importers.kcd import import_kcd
from catalog.importers.pelican import import_pelican
from catalog.importers.zsolt import import_zsolt

IMPORTERS = {"hcc": import_hcc, "kcd": import_kcd, "pelican": import_pelican, "zsolt": import_zsolt}
