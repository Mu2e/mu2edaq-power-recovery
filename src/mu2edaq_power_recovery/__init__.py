"""Mu2e DAQ power-outage recovery tooling.

Four phases, driven from a workstation outside the DAQ networks:

1. assess   -- read-only survey of every node's reachability, login, disk,
               network and power state.  Takes no corrective action.
2. poweron  -- bring nodes up in the dependency order given by
               config/power-sequence.yaml, verifying each stage before
               starting the next.
3. netcheck -- inter-node connectivity mesh across the lab/data/ipmi
               segments.
4. report   -- consolidated narrative of everything the run did, optionally
               posted to the electronic logbook.

Each phase writes its own page into the static report site; re-running a
phase refreshes that page in place.
"""

__version__ = "0.1.0"
__author__ = "A. Norman"
__all__ = ["__version__"]
