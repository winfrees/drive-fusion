"""Catalog persistence (SQLite). One of two packages permitted to write.

Writes are confined to the application's own catalog database and its
backups — never to catalogued media. Schema and loader arrive at M1.
"""
