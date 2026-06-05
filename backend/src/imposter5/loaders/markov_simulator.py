"""Markov Chain Pathing Simulator for Imposter5.

Generates dynamic, non-linear, probabilistic browsing sessions based on a 
transition probability matrix. Bypasses rigid script execution in favor of 
natural human state transitions.
"""

from __future__ import annotations

import logging
import random
from typing import Any

from imposter5.automation_connector.interaction_primitives import (
    click_element,
    hover_element,
    move_pointer,
    scroll_page,
    type_text,
    update_status_ticker,
    wait_human,
)
from imposter5.automation_connector.session_recorder import SessionRecorder

logger = logging.getLogger(__name__)

# Default human transition matrix trained on real browsing sessions
DEFAULT_HUMAN_MATRIX = {
    "idle": {
        "idle": 0.15,
        "mousemove": 0.35,
        "scroll_down": 0.25,
        "scroll_up": 0.05,
        "hover": 0.12,
        "click": 0.03,
        "typing": 0.05
    },
    "mousemove": {
        "idle": 0.25,
        "mousemove": 0.20,
        "scroll_down": 0.15,
        "scroll_up": 0.05,
        "hover": 0.25,
        "click": 0.08,
        "typing": 0.02
    },
    "scroll_down": {
        "idle": 0.40,
        "mousemove": 0.20,
        "scroll_down": 0.20,
        "scroll_up": 0.05,
        "hover": 0.10,
        "click": 0.04,
        "typing": 0.01
    },
    "scroll_up": {
        "idle": 0.35,
        "mousemove": 0.25,
        "scroll_down": 0.10,
        "scroll_up": 0.15,
        "hover": 0.10,
        "click": 0.03,
        "typing": 0.02
    },
    "hover": {
        "idle": 0.30,
        "mousemove": 0.20,
        "scroll_down": 0.10,
        "scroll_up": 0.02,
        "hover": 0.15,
        "click": 0.20,
        "typing": 0.03
    },
    "click": {
        "idle": 0.50,
        "mousemove": 0.20,
        "scroll_down": 0.15,
        "scroll_up": 0.02,
        "hover": 0.10,
        "click": 0.01,
        "typing": 0.02
    },
    "typing": {
        "idle": 0.20,
        "mousemove": 0.15,
        "scroll_down": 0.05,
        "scroll_up": 0.01,
        "hover": 0.05,
        "click": 0.50,
        "typing": 0.04
    }
}


def run_markov_simulation(
    page: Any,
    behavior_plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
    max_steps: int = 25,
) -> dict[str, Any]:
    """Execute a dynamic, Markov-chain-driven browsing session."""
    plan = behavior_plan or {}
    recorder = recorder or SessionRecorder(plan)
    
    # Load transition matrix from plan if provided (e.g., custom user upload), otherwise use default
    matrix = plan.get("markov_matrix", DEFAULT_HUMAN_MATRIX)
    
    update_status_ticker(page, "🎲 MARKOV INITIALIZED", "Generating probabilistic pathing...")
    wait_human(page, plan, 0, 1000, recorder=recorder)

    current_state = "idle"
    steps_executed = 0
    
    # Track state history for summary
    history = [current_state]

    while steps_executed < max_steps:
        steps_executed += 1
        
        # 1. Choose next state based on transition probabilities of current state
        probs = matrix.get(current_state, DEFAULT_HUMAN_MATRIX["idle"])
        
        # Normalize probabilities to ensure they sum to exactly 1.0
        states = list(probs.keys())
        weights = list(probs.values())
        total_w = sum(weights)
        if total_w > 0:
            weights = [w / total_w for w in weights]
        else:
            weights = [1.0 / len(states)] * len(states)
            
        next_state = random.choices(states, weights=weights, k=1)[0]
        
        logger.info("[markov_simulator] Step %d: %s -> %s", steps_executed, current_state, next_state)
        history.append(next_state)

        # 2. Execute interaction primitive corresponding to next state
        try:
            if next_state == "idle":
                # Pause and read
                dwell = random.randint(800, 3000)
                update_status_ticker(page, "👁️ READING", f"Pausing to read content ({dwell}ms)...")
                wait_human(page, plan, 0, dwell, recorder=recorder)
                
            elif next_state == "mousemove":
                # Move pointer to a random visual content area
                cx = random.randint(200, 1000)
                cy = random.randint(150, 700)
                update_status_ticker(page, "🖱️ MOVING", f"Moving mouse to ({cx}, {cy})...")
                move_pointer(page, cx, cy, plan, recorder=recorder)
                
            elif next_state == "scroll_down":
                # Scroll down
                delta = random.randint(300, 800)
                update_status_ticker(page, "📜 SCROLLING", f"Scrolling down {delta}px...")
                scroll_page(page, plan, pass_index=steps_executed, fallback_delta_y=delta, recorder=recorder)
                
            elif next_state == "scroll_up":
                # Scroll up (re-reading)
                delta = -random.randint(150, 500)
                update_status_ticker(page, "📜 SCROLLING", f"Scrolling up {-delta}px (re-reading)...")
                scroll_page(page, plan, pass_index=steps_executed, fallback_delta_y=delta, recorder=recorder)
                
            elif next_state == "hover":
                # Hover over a random link or element
                update_status_ticker(page, "👁️ HOVERING", "Looking for hover target...")
                hover_element(page, "a[href], button, input", plan, recorder=recorder)
                
            elif next_state == "click":
                # Click a link or button
                update_status_ticker(page, "🖱️ CLICKING", "Choosing element to click...")
                click_element(page, "a[href], button", plan, recorder=recorder)
                # Settle after click
                wait_human(page, plan, 0, 1200, recorder=recorder)
                
            elif next_state == "typing":
                # Type into a text input if visible
                inputs = page.locator("input[type=text], textarea, input[type=search]").all()
                visible_inputs = [i for i in inputs if i.is_visible()]
                if visible_inputs:
                    target_input = random.choice(visible_inputs)
                    text = random.choice(["hello", "markov chains", "last human line", "cybersecurity", "evasion"])
                    update_status_ticker(page, "⌨️ TYPING", f"Typing query: '{text}'")
                    type_text(page, target_input, text, plan, recorder=recorder)
                else:
                    # Fallback to mouse move if no inputs are visible
                    cx = random.randint(200, 1000)
                    cy = random.randint(150, 700)
                    move_pointer(page, cx, cy, plan, recorder=recorder)
                    
        except Exception as e:
            logger.warning("[markov_simulator] Error during state execution (%s): %s", next_state, e)
            
        current_state = next_state
        wait_human(page, plan, 0, random.randint(200, 600), recorder=recorder)

    update_status_ticker(page, "🏁 COMPLETED", "Markov simulation finished.")
    return {
        "steps_executed": steps_executed,
        "state_history": history,
        "final_state": current_state
    }
