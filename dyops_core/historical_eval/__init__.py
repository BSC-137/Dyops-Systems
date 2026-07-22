"""Vendor-neutral historical detector evaluation for Dyops."""

from .catalog import load_catalog
from .data import load_dataset, validate_dataset
from .runner import evaluate

__all__ = ["evaluate", "load_catalog", "load_dataset", "validate_dataset"]
