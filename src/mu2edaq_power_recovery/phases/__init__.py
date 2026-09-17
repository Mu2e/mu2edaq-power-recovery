"""The four recovery phases.

Each module exposes ``run(orchestrator, ...) -> PhaseResult`` and nothing else
the CLI needs, so phases can be run individually, out of order, or repeatedly.
"""
from .base import PhaseResult, overall_status
from . import phase1_assess, phase2_poweron, phase3_network, phase4_report

#: Phase name -> module, for --phase and for the report's page mapping.
PHASES = {
    "assess": phase1_assess,
    "poweron": phase2_poweron,
    "network": phase3_network,
    "report": phase4_report,
}

__all__ = ["PhaseResult", "overall_status", "PHASES",
           "phase1_assess", "phase2_poweron", "phase3_network", "phase4_report"]
