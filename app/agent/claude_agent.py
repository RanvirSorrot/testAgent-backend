"""
Backward-compatible wrapper for the provider-agnostic AI agent module.
"""

from app.agent.ai_agent import (
    analyze_error,
    calculate_score,
    generate_summary,
 
  
)

__all__ = [
  
    "analyze_error",
    "generate_summary",
    "calculate_score",
]
