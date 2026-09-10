"""Record linkage over open corporate-registry and sanctions data.

Resolves beneficial-ownership records from UK Companies House and OpenSanctions
into canonical entities, then loads them into a graph database so ownership and
control can be traversed.

Pipeline stages (see :mod:`ownership_er.cli`):

    acquire -> normalize -> block -> match -> cluster -> load -> analyse -> evaluate
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
