"""Reasoning strategies and plan-quality evaluation (v5.5).

This package evaluates plans and answers; it never executes a plan, schedules
work or calls tools. The planner and workflow engine remain the existing
orchestration (and the parallel DAG workstream). A plan that passes
evaluation is still only a proposal: "pass" means "well-formed and
consistent", never "completed".
"""
