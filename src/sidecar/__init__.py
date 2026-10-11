"""sidecar: the daemon that controls coding agents over a local HTTP plane.

Change sidecar-v0 (issue #31), design v4.  The daemon only knows four
actions - open, close, status, poke - and keeps all of its state in memory.
"""

__version__ = "0.1.0"
