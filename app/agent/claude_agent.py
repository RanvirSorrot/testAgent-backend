"""
Backward-compatible wrapper for the provider-agnostic AI agent module.
"""

from app.agent.ai_agent import (
    analyze_error,
    calculate_score,
    generate_summary,
    get_initial_test_plan,
    get_next_action,
)

__all__ = [
    "get_initial_test_plan",
    "get_next_action",
    "analyze_error",
    "generate_summary",
    "calculate_score",
]
