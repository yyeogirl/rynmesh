"""rynmesh.services — early Ryn-network service experiments.

Wedge category: heavy AI compute on bounded I/O (the strongest fit per
sim/FINDINGS.md F5). All follow the same signal50_service.py pattern —
poll the mailbox, run, and publish a signed result. The old LLM work-order path
is disabled because prompts must use the private encrypted peer protocol in
``rynmesh.llm_package``; deterministic helpers are tests only.
"""
from ._base import ServiceWorker  # noqa: F401
