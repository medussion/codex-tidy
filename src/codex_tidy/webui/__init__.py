"""Local browser UI.

Served from 127.0.0.1 by the standard library only. See server.serve().
"""

from .server import serve

__all__ = ["serve"]
