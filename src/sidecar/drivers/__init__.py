"""Driver implementations (tasks 3.1/3.2).

One module per agent kind; both share `subproc` for the child environment and
the gentle stop.  `wiring.default_drivers` builds them from the sidecar
config's `agents` section.
"""
