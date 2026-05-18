"""Services for GASUM Biogas Logistics System."""

from .data_loader import DataLoader

__all__ = [
    "DataLoader",
]

# Lazy imports for services not yet implemented
def __getattr__(name):
    if name == "VRPSolver":
        from .vrp_solver import VRPSolver
        return VRPSolver
    elif name == "RiskCalculator":
        from .risk_calculator import RiskCalculator
        return RiskCalculator
    elif name == "RecommendationService":
        from .recommendation import RecommendationService
        return RecommendationService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
