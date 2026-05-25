"""
Detecta si el catalizador corresponde a una estrategia del playbook de Oscar.
Gate 4 — informativo, no bloquea.
"""
import logging

logger = logging.getLogger(__name__)

# Keywords en resumen_cataliz → estrategias que pueden aplicar
CATALYST_STRATEGY_MAP = {
    "gobierno":     ["patron_a"],
    "chips act":    ["patron_a"],
    "award":        ["patron_a"],
    "contract":     ["patron_a"],
    "dod":          ["patron_a"],
    "grant":        ["patron_a"],
    "fda":          ["patron_a"],
    "partnership":  ["patron_a"],
    "upgrade":      ["patron_a"],
    "price target": ["patron_a"],
    "earnings beat": ["ped", "e1", "e2"],
    "beat":         ["ped", "e1"],
    "guidance raised": ["e1", "e2"],
    "earnings miss": ["e3", "resaca"],
    "miss":         ["resaca"],
}


def find_matching_strategies(catalyst_summary: str) -> list:
    """Retorna lista de estrategias del playbook que aplican al catalizador."""
    summary_lower = catalyst_summary.lower()
    matches = []
    for keyword, strategies in CATALYST_STRATEGY_MAP.items():
        if keyword in summary_lower:
            matches.extend(strategies)
    return list(set(matches))
