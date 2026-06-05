"""Wikipedia Advanced Simulation.

Implements highly realistic human interactions specifically modeled for Wikipedia,
including TOC navigation, text highlighting, reading guides, explanatory notes,
and bidirectional exploration.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

from imposter5.automation_connector.interaction_primitives import (
    click_element,
    hover_element,
    move_pointer,
    scroll_page,
    update_status_ticker,
    wait_human,
)
from imposter5.automation_connector.session_recorder import SessionRecorder

logger = logging.getLogger(__name__)


def run_wikipedia_simulation(
    page: Any,
    behavior_plan: dict[str, Any] | None = None,
    *,
    recorder: SessionRecorder | None = None,
) -> dict[str, Any]:
    """Run a highly humanized Wikipedia reading simulation.

    Executes the following advanced human behaviors:
    1. Expand side Table of Contents and click random section headings to skip around.
    2. Smoothly scroll randomly to top/bottom.
    3. Use mouse cursor as a reading guide across a few sentences.
    4. Highlight a subselection of text (sentence/phrase) with realistic drag-and-drop.
    5. Engage with explanatory notes / reference hover cards.
    6. Scroll/hover random paragraphs, go back, and scroll past.
    7. Click internal Wikipedia links to jump to another page.
    """
    plan = behavior_plan or {}
    recorder = recorder or SessionRecorder(plan)
    
    update_status_ticker(page, "🧭 INITIALIZING", "Starting Wikipedia Simulation...")
    wait_human(page, plan, 0, 1200, recorder=recorder)

    # 1. Expand Side Table of Contents (TOC) and skip around
    try:
        # Check if TOC is visible or needs expanding
        # Wikipedia uses different TOC selectors depending on skin (Vector 2022 uses .vector-toc)
        toc_button = page.locator("#vector-toc-collapsed-button, .vector-dropdown-label:has-text('Contents')").first
        if toc_button and toc_button.is_visible():
            update_status_ticker(page, "🧭 TABLE OF CONTENTS", "Expanding Table of Contents...")
            click_element(page, toc_button, plan, recorder=recorder)
            wait_human(page, plan, 0, 800, recorder=recorder)
            
        # Click a random heading in the TOC to jump
        toc_links = page.locator(".vector-toc-link, .toc a").all()
        if toc_links:
            # Pick a random heading (not the first one/intro)
            target_link = random.choice(toc_links[1:min(len(toc_links), 8)])
            link_text = target_link.inner_text() or "Section"
            update_status_ticker(page, "🧭 TOC NAV", f"Jumping to section: {link_text.strip()}")
            click_element(page, target_link, plan, recorder=recorder)
            wait_human(page, plan, 0, 1500, recorder=recorder)
    except Exception as e:
        logger.warning("[wikipedia_simulator] TOC step bypassed: %s", e)

    # 2. Reading Guide: Smooth mouse movement across a few sentences
    try:
        # Find a readable paragraph in the current viewport
        paragraphs = page.locator("p").all()
        visible_p = None
        for p in paragraphs:
            if p.is_visible():
                box = p.bounding_box()
                if box and box["height"] > 40 and box["y"] > 100 and box["y"] < 500:
                    visible_p = p
                    break
        
        if visible_p:
            box = visible_p.bounding_box()
            if box:
                update_status_ticker(page, "📖 READING GUIDE", "Using cursor as reading guide...")
                # Start at the left of the paragraph
                start_x = box["x"] + 20
                start_y = box["y"] + 15
                move_pointer(page, start_x, start_y, plan, recorder=recorder)
                wait_human(page, plan, 0, 300, recorder=recorder)
                
                # Move smoothly across 3 lines of text
                for line in range(3):
                    line_y = start_y + (line * 20)
                    # Sweep right
                    update_status_ticker(page, "📖 READING GUIDE", f"Reading line {line + 1}...")
                    move_pointer(page, start_x + box["width"] * random.uniform(0.6, 0.8), line_y, plan, recorder=recorder)
                    wait_human(page, plan, 0, random.randint(300, 600), recorder=recorder)
                    # Sweep back to start of next line
                    if line < 2:
                        move_pointer(page, start_x + random.uniform(-10, 10), line_y + 20, plan, recorder=recorder)
                        wait_human(page, plan, 0, 200, recorder=recorder)
    except Exception as e:
        logger.warning("[wikipedia_simulator] Reading guide bypassed: %s", e)

    # 3. Highlight a subselection of text (sentence/phrase)
    try:
        paragraphs = page.locator("p").all()
        visible_p = None
        for p in paragraphs:
            if p.is_visible():
                box = p.bounding_box()
                if box and box["height"] > 40 and box["y"] > 150 and box["y"] < 600:
                    visible_p = p
                    break
                    
        if visible_p:
            box = visible_p.bounding_box()
            if box and box["width"] > 100:
                update_status_ticker(page, "🖍️ HIGHLIGHT TEXT", "Highlighting subselection of text...")
                # Pick a starting spot in the paragraph
                start_x = box["x"] + box["width"] * random.uniform(0.15, 0.3)
                start_y = box["y"] + box["height"] * random.uniform(0.2, 0.4)
                
                # Move to start
                move_pointer(page, start_x, start_y, plan, recorder=recorder)
                wait_human(page, plan, 0, 250, recorder=recorder)
                
                # Drag to select
                page.mouse.down()
                wait_human(page, plan, 0, 100, recorder=recorder)
                
                end_x = start_x + box["width"] * random.uniform(0.25, 0.45)
                end_y = start_y + random.uniform(-5, 5)
                
                # Move smoothly to end of selection
                move_pointer(page, end_x, end_y, plan, recorder=recorder)
                wait_human(page, plan, 0, 200, recorder=recorder)
                page.mouse.up()
                
                update_status_ticker(page, "🖍️ HIGHLIGHT TEXT", "Text highlighted successfully.")
                wait_human(page, plan, 0, 1200, recorder=recorder)
                
                # Click off to clear highlight
                move_pointer(page, end_x + 50, end_y + 50, plan, recorder=recorder)
                page.mouse.click(end_x + 50, end_y + 50)
                wait_human(page, plan, 0, 500, recorder=recorder)
    except Exception as e:
        logger.warning("[wikipedia_simulator] Highlight step bypassed: %s", e)

    # 4. Engage with explanatory notes / reference hover cards
    try:
        # Wikipedia reference links are usually sup.reference a
        refs = page.locator("sup.reference a").all()
        if refs:
            target_ref = random.choice(refs[:min(len(refs), 12)])
            if target_ref.is_visible():
                update_status_ticker(page, "ℹ️ EXPLANATORY NOTE", "Engaging with reference / explanatory note...")
                hover_element(page, target_ref, plan, recorder=recorder)
                # Dwell on the reference popup card
                wait_human(page, plan, 0, 1800, recorder=recorder)
    except Exception as e:
        logger.warning("[wikipedia_simulator] Reference step bypassed: %s", e)

    # 5. Scroll and hover random things, then go back and pick up/scroll past
    try:
        update_status_ticker(page, "📜 BIDIRECTIONAL SCROLL", "Scrolling down to scan content...")
        # Scroll down
        scroll_page(page, plan, 1, 600, recorder=recorder)
        wait_human(page, plan, 1, 1000, recorder=recorder)
        
        # Hover some random element
        headers = page.locator("h2, h3, p").all()
        visible_h = None
        for h in headers:
            if h.is_visible():
                box = h.bounding_box()
                if box and box["y"] > 200 and box["y"] < 600:
                    visible_h = h
                    break
        if visible_h:
            update_status_ticker(page, "👁️ HOVER SCAN", "Scanning section content...")
            hover_element(page, visible_h, plan, recorder=recorder)
            wait_human(page, plan, 0, 800, recorder=recorder)
            
        # Scroll back up to "re-read" or check something
        update_status_ticker(page, "📜 BIDIRECTIONAL SCROLL", "Scrolling back up to re-read...")
        scroll_page(page, plan, 2, -400, recorder=recorder)
        wait_human(page, plan, 2, 1200, recorder=recorder)
        
        # Scroll past where we were
        update_status_ticker(page, "📜 BIDIRECTIONAL SCROLL", "Scrolling past previous position...")
        scroll_page(page, plan, 3, 900, recorder=recorder)
        wait_human(page, plan, 3, 1000, recorder=recorder)
    except Exception as e:
        logger.warning("[wikipedia_simulator] Bidirectional scroll bypassed: %s", e)

    # 6. Scroll randomly all the way to top/bottom
    try:
        if random.random() < 0.5:
            update_status_ticker(page, "📜 DEEP SCROLL", "Scrolling deep towards the bottom...")
            scroll_page(page, plan, 4, 1500, recorder=recorder)
            wait_human(page, plan, 4, 1500, recorder=recorder)
        else:
            update_status_ticker(page, "📜 DEEP SCROLL", "Scrolling back towards the top...")
            scroll_page(page, plan, 4, -1500, recorder=recorder)
            wait_human(page, plan, 4, 1500, recorder=recorder)
    except Exception as e:
        logger.warning("[wikipedia_simulator] Deep scroll bypassed: %s", e)

    # 7. Click into another Wikipedia page
    try:
        # Find internal wiki links inside the main body content
        # Usually inside #mw-content-text and starts with /wiki/ (excluding special namespaces)
        links = page.locator("#mw-content-text p a[href^='/wiki/']").all()
        valid_wiki_links = []
        for link in links:
            try:
                href = str(link.get_attribute("href") or "")
                # Exclude special/meta pages
                if not any(k in href for k in (":", "Main_Page", "File:", "Help:", "Special:")):
                    if link.is_visible():
                        box = link.bounding_box()
                        if box and box["width"] > 10 and box["height"] > 10:
                            valid_wiki_links.append(link)
            except Exception:
                pass
                
        if valid_wiki_links:
            target_link = random.choice(valid_wiki_links[:min(len(valid_wiki_links), 15)])
            link_text = target_link.inner_text() or "Wiki Link"
            update_status_ticker(page, "🧭 JUMP PAGE", f"Navigating to: {link_text.strip()}...")
            click_element(page, target_link, plan, recorder=recorder)
            wait_human(page, plan, 0, 2000, recorder=recorder)
    except Exception as e:
        logger.warning("[wikipedia_simulator] Jump page step bypassed: %s", e)

    update_status_ticker(page, "✅ SIMULATION COMPLETE", "Wikipedia simulation finished successfully.")
    wait_human(page, plan, 0, 1000, recorder=recorder)
    
    return {"success": True, "provider": "wikipedia"}
