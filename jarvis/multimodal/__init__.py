"""Provider-neutral multimodal input foundation (v5.4).

Validates and describes non-text inputs (images, speech transcripts, browser
visual observations), checks that a selected model actually supports them,
and keeps direct observations (measured by code) separate from model
interpretations. No file paths are accepted, and media are never sent to a
remote provider without explicit per-request authorization. Only mock
multimodal adapters exist in this version; no real image understanding is
claimed.
"""
