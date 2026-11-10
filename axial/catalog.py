"""The product catalog ("mock ERP").

This module is the ONLY place a ``Sku`` can be created. Everything downstream
receives SKUs through :func:`to_sku`, which raises on anything not in the catalog.
Neither the regex rules nor the LLM ever emit a SKU string - they emit the
customer's *words*, and :mod:`axial.resolve.catalog_match` maps words to catalog
entries. A SKU that is not in the catalog is therefore impossible in the output
by construction, not by validation after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

Sku = NewType("Sku", str)


class UnknownSkuError(ValueError):
    """Raised when a string is not a catalog SKU."""


@dataclass(frozen=True, slots=True)
class CatalogItem:
    sku: Sku
    description: str
    #: What the customer calls this kind of product in general ("lona").
    family: str
    #: Normalized colloquial words (lowercase, no accents) that refer to this product.
    aliases: tuple[str, ...]
    #: Words that single this item out from its siblings in the same family ("mesh").
    variant_aliases: tuple[str, ...]
    #: When the customer says only the family word, this is the item they mean.
    default_for_family: bool
    unit: str
    width_m: float | None = None
    length_m: float | None = None
    thickness_mm: float | None = None


CATALOG: tuple[CatalogItem, ...] = (
    CatalogItem(
        sku=Sku("ALFBTC-013-320100"),
        description="Lona Frontlit BTE 13 3.20x100",
        family="lona",
        aliases=("lona", "lonas", "frontlit", "front lit"),
        variant_aliases=("frontlit", "front lit", "bte"),
        default_for_family=True,
        unit="rollo",
        width_m=3.20,
        length_m=100,
    ),
    CatalogItem(
        sku=Sku("ALMCLC-0909-320100"),
        description="Lona Mesh CLN 9x9 3.20x100",
        family="lona",
        aliases=("lona", "lonas", "mesh", "malla", "mallas"),
        variant_aliases=("mesh", "malla", "mallas", "cln"),
        default_for_family=False,
        unit="rollo",
        width_m=3.20,
        length_m=100,
    ),
    CatalogItem(
        sku=Sku("AVMICS-1A-137050"),
        description="Vinil Adhesivo Microperforado 1.37x50",
        family="vinil",
        aliases=(
            "microperforado",
            "microperforados",
            "microperforada",
            "micro perforado",
            "vinil",
            "viniles",
            "vinil adhesivo",
        ),
        variant_aliases=("microperforado", "microperforados", "microperforada", "micro perforado"),
        default_for_family=True,
        unit="rollo",
        width_m=1.37,
        length_m=50,
    ),
    CatalogItem(
        sku=Sku("ARACBL-03-122244"),
        description="Acrilico Blanco 3mm 1.22x2.44",
        family="acrilico",
        aliases=("acrilico", "acrilicos", "acrilica", "acrilicas", "acrylic"),
        variant_aliases=(),
        default_for_family=True,
        unit="hoja",
        width_m=1.22,
        length_m=2.44,
        thickness_mm=3,
    ),
)

_BY_SKU: dict[str, CatalogItem] = {item.sku: item for item in CATALOG}


def is_catalog_sku(value: str) -> bool:
    return value in _BY_SKU


def to_sku(value: str) -> Sku:
    """The single gate from an untrusted string to a trusted SKU."""
    if value not in _BY_SKU:
        raise UnknownSkuError(f"Unknown SKU: {value!r}")
    return Sku(value)


def get_item(sku: Sku) -> CatalogItem:
    return _BY_SKU[sku]
