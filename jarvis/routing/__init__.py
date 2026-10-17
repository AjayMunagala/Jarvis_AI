"""Provider-neutral model registry, routing and evaluation (v5.1).

The router wraps the existing ``IntelligenceProvider`` adapters
(``jarvis.intelligence``) instead of replacing them. It ranks only models that
pass hard eligibility rules (availability, modality, context, structured
output, privacy) and never grants permissions to a model.
"""
