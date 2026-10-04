"""Cross-cutting foundations: config, logging, enums, and domain models.

Nothing in `core` imports a broker client or any other layer, so it stays a
safe, dependency-free base that every other package can build on.
"""
