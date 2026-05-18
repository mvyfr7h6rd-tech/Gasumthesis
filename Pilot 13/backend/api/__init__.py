"""API layer for GASUM Biogas Logistics System."""

__all__ = []

# Lazy imports for API modules not yet implemented
def __getattr__(name):
    if name == "router":
        from .routes import router
        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
