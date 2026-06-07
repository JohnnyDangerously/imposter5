"""Story Mode: a goal/intent layer that drives long, human-RANDOM, goal-oriented
sessions on top of imposter5's analog motor + semi-Markov micro-behavior engine.

A small ``TaskIntent`` prompt seeds a plan. The pipeline is:

    TaskIntent -> SiteMapper (DOM -> affordance map; heuristic v1, cached)
               -> StoryCompiler (intent + map -> sampled StoryPlan + curiosity tangents)
               -> Journey Executor (scenes -> live element resolution -> analog motor)
               -> Goal checker (terminate on goal_predicate +/- jitter)

The defining behavior is intent-level *curiosity/wander*: at eligible scenes the
agent may branch onto an off-goal tangent (open a friend's profile, research a
different query, refresh) and ALWAYS return via a resume stack to finish the main
goal. No two attempts from one prompt are alike, yet each accomplishes the goal.

This package is the language/goal brain; it never reimplements motion. All physical
interaction is delegated to the existing ``automation_connector`` primitives and the
``loaders.markov_simulator`` semi-Markov engine.
"""
from __future__ import annotations

from imposter5.story.task_intent import (
    Curiosity,
    GoalPredicate,
    Objective,
    TaskIntent,
    Variance,
    load_task_intent,
    parse_task_intent,
)
from imposter5.story.goal import GoalChecker
from imposter5.story.compiler import Scene, StoryCompiler, StoryPlan, compile_story
from imposter5.story.site_mapper import AffordanceMap, SiteMapper
from imposter5.story.executor import StoryExecutor, run_story

__all__ = [
    "Curiosity",
    "GoalPredicate",
    "Objective",
    "TaskIntent",
    "Variance",
    "load_task_intent",
    "parse_task_intent",
    "GoalChecker",
    "Scene",
    "StoryCompiler",
    "StoryPlan",
    "compile_story",
    "AffordanceMap",
    "SiteMapper",
    "StoryExecutor",
    "run_story",
]
