"""Pairwise matchers: interpretable rules, probabilistic Splink, LLM adjudication."""

from ownership_er.match.base import Decision, Matcher, ScoredPair, decide
from ownership_er.match.rules import RuleMatcher

__all__ = ["Decision", "Matcher", "RuleMatcher", "ScoredPair", "decide"]
