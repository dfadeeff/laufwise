"""Laufwise — runbook-as-code harness for business-process agents.

The harness is a loop, not a fan-out. Every step runs the same enforced sequence:
precondition -> tool allowlist -> approval -> execute -> postcondition -> checkpoint+trace.
The order is the product guarantee. See ARCHITECTURE.md and CLAUDE.md.
"""

__version__ = "0.1.0"
