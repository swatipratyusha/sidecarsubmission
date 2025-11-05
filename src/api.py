from flask import Flask, request, jsonify
import time
import random
import math
import json
import base64
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright
from openai import OpenAI

app = Flask(__name__)

requested_bookings = set()

# ============================================================================
# CACHE MANAGEMENT
# ============================================================================

class MilestoneCache:
    """
    Caches successful milestone scripts and final results.
    
    Cache structure:
    {
        "carrier:booking_id": {
            "cached_at": "timestamp",
            "final_results": {
                "voyage_number": "...",
                "arrival_date": "...",
                "verification_scripts": [...]  # Scripts to re-verify arrival date
            },
            "milestones": {
                "Reached hub site": {
                    "cached_at": "timestamp",
                    "script": "exact script that worked",
                    "operations": ["script_execution_start", "script_execution_result", ...]
                },
                ...
            }
        }
    }
    """
    
    def __init__(self, cache_dir="cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_days = 14  # Only use cache newer than 14 days
    
    def _get_cache_path(self, carrier, booking_id):
        """Get cache file path for a specific carrier:booking_id combination."""
        cache_key = f"{carrier}_{booking_id}"
        return self.cache_dir / f"{cache_key}.json"
    
    def load_cache(self, carrier, booking_id):
        """Load cache for a specific carrier:booking_id."""
        cache_path = self._get_cache_path(carrier, booking_id)
        
        if not cache_path.exists():
            return None
        
        try:
            with open(cache_path, 'r') as f:
                cache_data = json.load(f)
            
            # Check if cache is fresh (< 14 days old)
            cached_at = datetime.fromisoformat(cache_data.get("cached_at", "2000-01-01"))
            age = datetime.now() - cached_at
            
            if age.days >= self.cache_ttl_days:
                print(f"⏰ Cache expired for {carrier}:{booking_id} (age: {age.days} days)")
                return None
            
            print(f"✨ Found cache for {carrier}:{booking_id} (age: {age.days} days)")
            return cache_data
            
        except Exception as e:
            print(f"⚠️  Failed to load cache: {e}")
            return None
    
    def get_milestone_cache(self, carrier, booking_id, milestone):
        """Get cached script for a specific milestone."""
        cache = self.load_cache(carrier, booking_id)
        
        if not cache:
            return None
        
        milestones = cache.get("milestones", {})
        return milestones.get(milestone)
    
    def save_milestone(self, carrier, booking_id, milestone, script, operations=None):
        """Save a successful milestone script to cache."""
        cache_path = self._get_cache_path(carrier, booking_id)
        
        # Load existing cache or create new
        if cache_path.exists():
            try:
                with open(cache_path, 'r') as f:
                    cache_data = json.load(f)
            except:
                cache_data = {"cached_at": datetime.now().isoformat(), "milestones": {}}
        else:
            cache_data = {"cached_at": datetime.now().isoformat(), "milestones": {}}
        
        # Update only this specific milestone (partial update)
        if "milestones" not in cache_data:
            cache_data["milestones"] = {}
        
        cache_data["milestones"][milestone] = {
            "cached_at": datetime.now().isoformat(),
            "script": script,
            "operations": operations or []
        }
        
        # Save to file
        try:
            with open(cache_path, 'w') as f:
                json.dump(cache_data, f, indent=2)
            print(f"💾 Cached milestone '{milestone}' for {carrier}:{booking_id}")
        except Exception as e:
            print(f"⚠️  Failed to save cache: {e}")
    
    def save_final_results(self, carrier, booking_id, voyage_number, arrival_date, verification_scripts=None):
        """Save final results (voyage number and arrival date) to cache."""
        cache_path = self._get_cache_path(carrier, booking_id)
        
        # Load existing cache or create new
        if cache_path.exists():
            try:
                with open(cache_path, 'r') as f:
                    cache_data = json.load(f)
            except:
                cache_data = {"cached_at": datetime.now().isoformat(), "milestones": {}}
        else:
            cache_data = {"cached_at": datetime.now().isoformat(), "milestones": {}}
        
        # Add final results
        cache_data["final_results"] = {
            "voyage_number": voyage_number,
            "arrival_date": arrival_date,
            "verification_scripts": verification_scripts or [],
            "cached_at": datetime.now().isoformat()
        }
        
        # Save to file
        try:
            with open(cache_path, 'w') as f:
                json.dump(cache_data, f, indent=2)
            print(f"💾 Cached final results for {carrier}:{booking_id}")
        except Exception as e:
            print(f"⚠️  Failed to save final results: {e}")
    
    def get_final_results(self, carrier, booking_id):
        """Get cached final results if available."""
        cache = self.load_cache(carrier, booking_id)
        
        if not cache:
            return None
        
        return cache.get("final_results")

# Global cache instance
milestone_cache = MilestoneCache()

# ============================================================================
# KNOWLEDGE BASE
# ============================================================================

AUTOMATION_GUIDELINES = {
    "playwright_best_practices": {
        "selectors": {
            "domcontentloaded": "Use page.wait_for_load_state('domcontentloaded') before interaction",
            "priority_order": ["data-testid", "aria-label", "id", "name", "class", "text"],
            "avoid": ["xpath with indexes", "nth-child without context"],
            "tips": "Use semantic selectors that are less likely to break"
        },
        "waits": {
            "prefer": "page.wait_for_load_state('load') - Waits for the load event (DOM + all resources loaded). This is recommended for most cases as it's practical for real-world sites with ads/analytics.",
            "element_waits": "Use page.wait_for_selector() before interaction",
            "avoid": "Hard-coded time.sleep() unless necessary. Also avoid 'networkidle' as it often times out on sites with continuous background requests."
        },
        "actions": {
            "clicking": "Always scroll element into view first if needed",
            "typing": "Use page.fill() for inputs, page.type() for char-by-char",
            "navigation": "Wait for navigation events after clicks",
            "tab_handling": "Carrier links often open new tabs. After clicking a carrier link, check if a new tab opened and switch to the new one if you feel it is the carrier site."
        },
        "error_handling": {
            "timeouts": "Use reasonable timeouts (5-10 seconds for most actions)",
            "try_catch": "Wrap risky operations in try-except blocks",
            "logging": "Log failures with context for debugging"
        }
    },
    "code_generation_rules": {
        "no_imports": True,
        "assume_page_variable": True,
        "return_data_via_result": "Store extracted data in 'result' variable",
        "comments": "Add brief comments for complex logic"
    }
}

DOMAIN_KNOWLEDGE = {
    "shipping_tracking_workflow": {
        "typical_flow": [
            "Navigate to aggregator/hub site (e.g., http://seacargotracking.net)",
            "Find and click on specific carrier link",
            "Wait for carrier site to load (often opens in new tab/window)",
            "IMPORTANT: Carrier links often open in new tabs/windows. After clicking, check all open tabs/pages to see if a new one opened with the carrier site",
            "If new tab opened, switch to it to continue automation on the carrier site",
            "IMPORTANT: Look for and click navigation items like 'E-Services', 'e-Service', 'eService', 'Online Services', or similar menu items FIRST to access the tracking interface. Avoid generic 'tracking' term related links",
            "Visually locate B/L or Booking ID input field typically around a vivid Search or Continue button using vision model. The input field might have a name that is different, so, use vision-based click and type instead of path selectors",
            "Enter booking/container number",
            "Submit search",
            "Extract voyage number and arrival date from results"
        ],
        "milestones": [
            "Reached hub site",
            "Reached {carrier} website",
            "Accessed services section (if needed)",
            "Found text input for B/L or Booking ID and entered the ID",
            "Submitted tracking query",
            "Results displayed",
            "Data extracted"
        ]
    },
    "common_patterns": {
        "carrier_links": {
            "identifiers": ["carrier name in text", "logo images", "links with carrier domain"],
            "locations": ["grid layout", "list of links", "dropdown menu"]
        },
        "navigation_items": {
            "tracking_section_links": ["E-Services", "e-Service", "eService", "Online Services", "Services", "Cargo Services"],
            "locations": ["top navigation menu", "main menu bar", "side navigation", "body", "footer links"],
            "note": "Carrier landing pages often require clicking these navigation items before the tracking input field becomes visible. PRIORITIZE 'E-Services' or 'e-Service' links as they typically lead directly to tracking tools. Avoid ambiguous 'Track & Trace' or 'Tracking' text that may match multiple elements including overlays."
        },
        "search_interfaces": {
            "input_fields": ["booking number", "container number", "BL number"],
            "labels": ["Booking No", "Container No", "B/L No", "Reference"],
            "buttons": ["Search", "Track", "Submit", "Go", "Find"]
        },
        "results_display": {
            "formats": ["table", "card layout", "list view"],
            "data_locations": ["table cells", "labeled divs", "definition lists"],
            "key_fields": ["voyage", "vessel", "arrival", "ETA", "discharge"]
        }
    },
    "carrier_specific": {
            "typical_flow": "some option like tracking, trace, eservices, etc. -> booking input -> search -> results",
            "data_format": "Table with voyage and arrival information"
    }
}

RECOVERY_STRATEGIES = {
    "site_not_loading_or_ssl_error": {
        "strategies": [
            "Switch from https to http or vice versa",
            "Take anti-blocking measures"
        ],
        "max_retries": 3
    },
    "element_not_found": {
        "strategies": [
            "Take screenshot and analyze with vision model",
            "Try alternative selectors (text-based, partial match)",
            "Scroll page to ensure element is in viewport",
            "Wait longer (element might be loading)",
            "Check if page structure changed (website update)",
            "Use vision-guided coordinate-based clicking (if element is visible but selector fails due to strict mode violations, overlays, or complex DOM structure)"
        ],
        "max_retries": 3
    },
    "script_execution_failure": {
        "strategies": [
            "Regenerate script with error context",
            "Try simpler approach (less complex selectors)",
            "Use vision-guided coordinate-based clicking",
            "Check if page is ready (wait for load state)"
        ]
    },
    "wrong_page": {
        "detection": "URL doesn't match expected pattern",
        "strategies": [
            "Go back and try different link",
            "Navigate directly if URL is known",
            "Look for breadcrumbs or navigation menu"
        ]
    },
    "timeout": {
        "strategies": [
            "Increase timeout for slow-loading pages",
            "Check network connectivity",
            "Try refreshing page",
            "Use alternative carrier if available"
        ]
    },
    "no_results_found": {
        "strategies": [
            "Verify booking ID is correct",
            "Try different input format (with/without spaces, dashes)",
            "Check if carrier selection was correct",
            "Look for error messages or suggestions on page"
        ]
    }
}

# ============================================================================
# CURRENT CONTEXT CLASS
# ============================================================================

class CurrentContext:
    def __init__(self):
        self.goal = None
        self.booking_id = None
        self.carrier = "hmm"
        self.last_tried_script = None
        self.last_intent = None
        self.last_response_data = None
        self.last_response_status = None
        self.current_url = None
        self.last_achieved_milestone = None
        self.remaining_milestones = []  # Track remaining milestones as a task list
        self.current_window_screenshot = None
        self.tab_screenshots = []
        self.history = []
        self.extracted_data = {}
        self.vision_analysis_after_action = None
        self.tab_analysis = None  # Store tab information from window screenshots
    
    def set_goal(self, booking_id, carrier="hmm"):
        self.goal = f"Extract voyage number and arrival date for booking {booking_id}"
        self.booking_id = booking_id
        self.carrier = carrier.lower()
        # Initialize remaining milestones from the workflow and substitute carrier name
        self.remaining_milestones = [
            milestone.format(carrier=self.carrier.upper())
            for milestone in DOMAIN_KNOWLEDGE["shipping_tracking_workflow"]["milestones"]
        ]
    
    def update_script(self, script, intent):
        self.last_tried_script = script
        self.last_intent = intent
    
    def update_response(self, data, status):
        self.last_response_data = data
        self.last_response_status = status
    
    def clear_stale_context(self):
        """Clear context fields that may be stale from previous step's async operations"""
        self.last_tried_script = None
        self.last_intent = None
        self.last_response_data = None
        self.last_response_status = None
    
    def update_url(self, url):
        self.current_url = url
    
    def update_milestone(self, milestone):
        self.last_achieved_milestone = milestone
        # Remove completed milestone from remaining list (use fuzzy matching)
        if milestone and self.remaining_milestones:
            milestone_lower = milestone.lower()
            # Try to find and remove matching milestone
            for i, remaining in enumerate(self.remaining_milestones):
                if remaining.lower() in milestone_lower or milestone_lower in remaining.lower():
                    self.remaining_milestones.pop(i)
                    break
    
    def update_screenshots(self, window_screenshot, tab_screenshots=None):
        self.current_window_screenshot = window_screenshot
        if tab_screenshots:
            self.tab_screenshots = tab_screenshots
    
    def add_to_history(self, log_json):
        self.history.append(log_json)
        if len(self.history) > 6:
            self.history.pop(0)
    
    def to_dict(self):
        # Get most recent step's data from history if available
        recent_step_data = {}
        if self.history:
            # History is ordered, get the most recent (last entry)
            most_recent = self.history[-1]
            recent_step_data = {
                "last_step_milestone": most_recent.get("milestone"),
                "last_step_url": most_recent.get("current_url"),
                "last_step_success": most_recent.get("success"),
                "last_step_errors": most_recent.get("errors", [])
            }
        
        # Get next milestone (first in remaining list)
        next_milestone = self.remaining_milestones[0] if self.remaining_milestones else "Goal completion"
        
        return {
            "goal": self.goal,
            "booking_id": self.booking_id,
            "carrier": self.carrier,
            "last_tried_script": self.last_tried_script,
            "last_intent": self.last_intent,
            "last_response_data": self.last_response_data,
            "last_response_status": self.last_response_status,
            "current_url": self.current_url,
            "last_achieved_milestone": self.last_achieved_milestone,
            "next_milestone": next_milestone,
            "remaining_milestones": self.remaining_milestones,
            "current_window_screenshot": self.current_window_screenshot,
            "tab_screenshots": self.tab_screenshots,
            "tab_analysis": self.tab_analysis,
            "history": self.history,
            "extracted_data": self.extracted_data,
            "vision_analysis_after_action": self.vision_analysis_after_action,
            "recent_step_data": recent_step_data,
            "workflow": DOMAIN_KNOWLEDGE["shipping_tracking_workflow"],
            "common_patterns": DOMAIN_KNOWLEDGE["common_patterns"],
            "automation_guidelines": AUTOMATION_GUIDELINES,
            "recovery_strategies": RECOVERY_STRATEGIES
        }
    
    def reset(self):
        self.__init__()


# ============================================================================
# VISION HELPERS CLASS
# ============================================================================

class VisionHelpers:
    def __init__(self, page):
        self.page = page
        self.popup_closed_by_click = False  # Track if popups were closed by click_at_coordinates
    
    def close_popup(self):
        """
        Attempts to close any popup/modal/overlay using metadata-driven detection.
        Identifies popups by their behavior characteristics (z-index, positioning, visibility)
        rather than hardcoded class names or selectors.
        """
        print("\n🚪 Attempting to close popup/modal...")
        
        try:
            self.page.wait_for_timeout(500)
        except:
            pass
        
        # Strategy 1: Try pressing Escape key
        try:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(500)
            print("  ✅ Tried Escape key")
        except Exception:
            pass
        
        # Strategy 2: Use JavaScript to find popup-like elements by metadata/characteristics
        try:
            popup_info = self.page.evaluate("""
                () => {
                    const allElements = document.querySelectorAll('*');
                    const popups = [];
                    
                    for (const el of allElements) {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        
                        // Check if element has popup-like characteristics
                        const zIndex = parseInt(style.zIndex) || 0;
                        const position = style.position;
                        const display = style.display;
                        const visibility = style.visibility;
                        const opacity = parseFloat(style.opacity);
                        
                        // Popup indicators:
                        // 1. High z-index (usually > 1000 for popups)
                        // 2. Fixed or absolute positioning
                        // 3. Visible and displayed
                        // 4. Has significant size (not tiny)
                        const isPopupLike = (
                            (zIndex > 1000 || zIndex > 100) &&
                            (position === 'fixed' || position === 'absolute') &&
                            display !== 'none' &&
                            visibility !== 'hidden' &&
                            opacity > 0.5 &&
                            rect.width > 100 &&
                            rect.height > 50
                        );
                        
                        if (isPopupLike) {
                            // Find buttons within this popup element
                            const buttons = el.querySelectorAll('button');
                            const closeButtonCandidates = [];
                            
                            for (const btn of buttons) {
                                const btnRect = btn.getBoundingClientRect();
                                const btnText = btn.textContent.trim();
                                
                                // Close button indicators:
                                // 1. Usually small (not a main action button)
                                // 2. Often positioned in top-right corner of popup
                                // 3. May have specific text or be icon-only
                                const isSmallButton = btnRect.width < 100 && btnRect.height < 100;
                                const isInTopRight = (
                                    btnRect.right > rect.right - 50 &&  // Near right edge
                                    btnRect.top < rect.top + 100        // Near top
                                );
                                
                                if (isSmallButton || isInTopRight || btnText.length < 10) {
                                    closeButtonCandidates.push({
                                        text: btnText,
                                        x: btnRect.x + btnRect.width / 2,
                                        y: btnRect.y + btnRect.height / 2,
                                        zIndex: zIndex
                                    });
                                }
                            }
                            
                            if (closeButtonCandidates.length > 0) {
                                popups.push({
                                    zIndex: zIndex,
                                    rect: {
                                        x: rect.x,
                                        y: rect.y,
                                        width: rect.width,
                                        height: rect.height
                                    },
                                    closeButtons: closeButtonCandidates
                                });
                            }
                        }
                    }
                    
                    // Sort by z-index (highest first - most likely to be the visible popup)
                    popups.sort((a, b) => b.zIndex - a.zIndex);
                    
                    return popups.length > 0 ? {
                        found: true,
                        popups: popups.map(p => ({
                            zIndex: p.zIndex,
                            rect: p.rect,
                            closeButtons: p.closeButtons.map(btn => ({
                                text: btn.text,
                                x: Math.round(btn.x),
                                y: Math.round(btn.y)
                            }))
                        }))
                    } : { found: false };
                }
            """)
            
            if popup_info.get("found") and len(popup_info.get("popups", [])) > 0:
                print(f"  🔍 Found {len(popup_info['popups'])} popup-like element(s) using metadata detection")
                
                # Try the highest z-index popup first (most likely the visible one)
                for popup_idx, popup in enumerate(popup_info["popups"]):
                    print(f"  📋 Popup {popup_idx + 1} (z-index {popup['zIndex']}) has {len(popup['closeButtons'])} close button candidate(s)")
                    
                    # Store the popup's z-index to check if it disappeared
                    target_z_index = popup["zIndex"]
                    
                    for btn_idx, close_btn in enumerate(popup["closeButtons"]):
                        try:
                            x, y = close_btn["x"], close_btn["y"]
                            btn_text = close_btn["text"]
                            print(f"  🖱️  Attempting to click button {btn_idx + 1} at ({x}, {y}) - text: '{btn_text}'")
                            
                            self.page.mouse.click(x, y)
                            # Wait longer for popup closing animation to complete (was 800ms, now 2000ms)
                            self.page.wait_for_timeout(2000)
                            
                            # Check if popup with this z-index disappeared
                            remaining_popups_info = self.page.evaluate("""
                                () => {
                                    const allElements = document.querySelectorAll('*');
                                    const visiblePopups = [];
                                    for (const el of allElements) {
                                        const style = window.getComputedStyle(el);
                                        const zIndex = parseInt(style.zIndex) || 0;
                                        const rect = el.getBoundingClientRect();
                                        if (zIndex > 1000 && 
                                            style.display !== 'none' && 
                                            style.visibility !== 'hidden' &&
                                            rect.width > 100 &&
                                            rect.height > 50) {
                                            visiblePopups.push(zIndex);
                                        }
                                    }
                                    return visiblePopups;
                                }
                            """)
                            
                            # Check if the target popup disappeared
                            if target_z_index not in remaining_popups_info:
                                print(f"  ✅ Successfully closed popup (z-index {target_z_index} disappeared)!")
                                return True
                            else:
                                print(f"  ⚠️  Popup still visible, trying next button...")
                                
                        except Exception as e:
                            print(f"  ⚠️  Error clicking button: {e}")
                            continue
                    
                    # If highest z-index popup couldn't be closed, try others
                    if popup_idx == 0:
                        print(f"  ⚠️  Could not close highest z-index popup, trying others...")
                        continue
                    else:
                        break
        except Exception as e:
            print(f"  ⚠️  Error in popup detection: {e}")
        
        # Strategy 3: Fallback - position-based detection
        print("  🔍 Trying position-based close button detection...")
        try:
            viewport = self.page.viewport_size or {"width": 1280, "height": 720}
            close_positions = [
                (viewport["width"] - 50, 50),
                (viewport["width"] - 100, 50),
                (viewport["width"] - 50, 100),
            ]
            
            for x, y in close_positions:
                try:
                    element_at_pos = self.page.evaluate(f"""
                        () => {{
                            const el = document.elementFromPoint({x}, {y});
                            if (el && el.tagName === 'BUTTON') {{
                                return {{
                                    text: el.textContent.trim(),
                                    x: el.getBoundingClientRect().x + el.getBoundingClientRect().width / 2,
                                    y: el.getBoundingClientRect().y + el.getBoundingClientRect().height / 2
                                }};
                            }}
                            return null;
                        }}
                    """)
                    
                    if element_at_pos:
                        btn_x, btn_y = int(element_at_pos["x"]), int(element_at_pos["y"])
                        print(f"  🖱️  Found button at ({x}, {y}), clicking at ({btn_x}, {btn_y})")
                        self.page.mouse.click(btn_x, btn_y)
                        self.page.wait_for_timeout(500)
                        print(f"  ✅ Clicked button at position")
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        
        print("  ⚠️  Could not find or close popup using metadata detection")
        print("  ℹ️  Popup may have closed, or it may require manual intervention")
        return False
    
    def take_screenshot(self, path, timeout=30000):
        try:
            self.page.screenshot(
                path=path, 
                full_page=False, 
                timeout=timeout, 
                animations="disabled",
                caret="hide"  # Hide blinking caret to prevent blocking
            )
            return path
        except Exception as e:
            print(f"⚠️  Screenshot error: {e}")
            raise
    
    def take_multifold_screenshots(self, prefix, screenshot_dir, timeout=30000):
        # Check if page is still valid
        if self.page.is_closed():
            raise Exception("Page is closed, cannot take screenshot")
        
        viewport = self.page.viewport_size or {"width": 1280, "height": 720}
        v_height = viewport["height"]
        
        try:
            total_height = self.page.evaluate(
                """() => Math.max(
                    document.body.scrollHeight,
                    document.documentElement.scrollHeight
                )"""
            )
        except Exception as e:
            print(f"⚠️  Could not evaluate page height: {e}")
            # Fallback to single screenshot
            screenshot_path = self.take_screenshot(Path(screenshot_dir) / f"{prefix}_fold_01.png", timeout)
            return [{"path": str(screenshot_path), "scroll_top": 0}]
        
        num_folds = max(1, math.ceil(total_height / v_height))
        screenshots = []
        
        for idx in range(num_folds):
            try:
                scroll_top = min(idx * v_height, max(total_height - v_height, 0))
                self.page.evaluate(f"window.scrollTo(0, {scroll_top})")
                self.page.wait_for_timeout(450)
                filename = f"{prefix}_fold_{idx+1:02d}.png"
                path = Path(screenshot_dir) / filename
                self.page.screenshot(path=path, full_page=False, timeout=timeout, animations="disabled", caret="hide")
                screenshots.append({"path": str(path), "scroll_top": scroll_top})
            except Exception as e:
                print(f"⚠️  Failed to take screenshot fold {idx+1}: {e}")
                # Continue with screenshots taken so far
                if not screenshots:
                    # If first screenshot failed, raise to trigger fallback
                    raise
        
        try:
            self.page.evaluate("window.scrollTo(0, 0)")
            self.page.wait_for_timeout(250)
        except:
            pass  # Ignore errors on scroll back
        
        if screenshots:
            return screenshots
        else:
            # Fallback if all screenshots failed
            screenshot_path = self.take_screenshot(Path(screenshot_dir) / f"{prefix}_fold_01.png", timeout)
            return [{"path": str(screenshot_path), "scroll_top": 0}]
    
    def get_element_coordinates(self, selector):
        element = self.page.locator(selector).first
        box = element.bounding_box()
        if box:
            return {"x": box["x"], "y": box["y"]}
        return None
    
    def get_element_size(self, selector):
        element = self.page.locator(selector).first
        box = element.bounding_box()
        if box:
            return {"width": box["width"], "height": box["height"]}
        return None
    
    def get_viewport_size(self):
        return self.page.viewport_size
    
    def get_page_size(self):
        dimensions = self.page.evaluate(
            """() => ({
                width: Math.max(
                    document.body.scrollWidth,
                    document.documentElement.scrollWidth
                ),
                height: Math.max(
                    document.body.scrollHeight,
                    document.documentElement.scrollHeight
                )
            })"""
        )
        return dimensions
    
    def move_mouse(self, x, y, duration=0.35):
        steps = max(int(duration * 30), 12)
        self.page.mouse.move(x, y, steps=steps)
        self.page.mouse.click(x, y)
        return True
    
    def get_window_metrics(self):
        return self.page.evaluate(
            """() => ({
                screenX: window.screenX,
                screenY: window.screenY,
                outerWidth: window.outerWidth,
                outerHeight: window.outerHeight,
                innerWidth: window.innerWidth,
                innerHeight: window.innerHeight
            })"""
        )
    
    def scroll_to(self, x, y):
        self.page.evaluate(f"window.scrollTo({x}, {y})")
        self.page.wait_for_timeout(250)
    
    def viewport_to_screen(self, x, y):
        metrics = self.get_window_metrics()
        border_x = (metrics["outerWidth"] - metrics["innerWidth"]) / 2
        border_y = metrics["outerHeight"] - metrics["innerHeight"] - border_x
        screen_x = metrics["screenX"] + border_x + x
        screen_y = metrics["screenY"] + max(border_y, 0.0) + y
        return {"screen_x": screen_x, "screen_y": screen_y}
    
    def click_at_coordinates(self, x, y, scroll_into_view=True):
        """
        Click at viewport coordinates (x, y) using pyautogui.
        Uses two-try approach:
        1. First try: Coordinate click WITH expect_page() to catch new tabs via events
        2. Second try: Normal coordinate click if no new tab opened (same-tab navigation)
        
        Args:
            x: X coordinate in viewport (from vision model)
            y: Y coordinate in viewport (from vision model)
            scroll_into_view: Whether to scroll the coordinates into view first
        
        Returns:
            dict with {"success": bool, "new_page": Page or None, "switched": bool}
        """
        try:
            # Lazy import to avoid X11 connection issues at startup
            import pyautogui
            
            # Scroll to bring coordinates into view if needed
            if scroll_into_view:
                viewport = self.page.viewport_size or {"width": 1280, "height": 720}
                current_scroll = self.page.evaluate("window.pageYOffset")
                
                # If y coordinate is outside viewport, scroll to it
                if y < current_scroll or y > current_scroll + viewport["height"]:
                    scroll_to_y = max(0, y - viewport["height"] // 2)
                    self.page.evaluate(f"window.scrollTo(0, {scroll_to_y})")
                    self.page.wait_for_timeout(500)  # Wait for scroll animation
                    
                    # Adjust y coordinate after scrolling
                    new_scroll = self.page.evaluate("window.pageYOffset")
                    y = y - new_scroll
            
            # Convert viewport coordinates to screen coordinates
            screen_coords = self.viewport_to_screen(x, y)
            
            print(f"🖱️  Clicking at viewport ({x}, {y}) → screen ({screen_coords['screen_x']:.0f}, {screen_coords['screen_y']:.0f})")
            
            # ============================================================
            # FIRST TRY: Coordinate click WITH expect_page() to catch new tabs
            # ============================================================
            try:
                print(f"🔍 Attempting coordinate click with new tab detection...")
                with self.page.context.expect_page(timeout=3000) as new_page_info:
                    # Perform the coordinate click
                    pyautogui.click(screen_coords["screen_x"], screen_coords["screen_y"])
                
                new_page = new_page_info.value
                if new_page:
                    print(f"✅ New tab opened via coordinate click! Switching to: {new_page.url}")
                    try:
                        new_page.wait_for_load_state('domcontentloaded', timeout=5000)
                    except:
                        pass
                    
                    # Update self.page to the new page
                    old_url = self.page.url if not self.page.is_closed() else "unknown"
                    self.page = new_page
                    new_page.bring_to_front()
                    
                    print(f"🔄 Switched from {old_url} → {new_page.url}")
                    
                    # Close any popups on the new tab
                    popup_closed = False
                    try:
                        popup_closed = self.close_popup()
                        # Track that we closed popups so execute() doesn't close them again
                        self.popup_closed_by_click = popup_closed
                    except Exception as popup_error:
                        print(f"⚠️  Popup closing failed: {popup_error}")
                    
                    return {"success": True, "new_page": new_page, "switched": True, "popup_closed": popup_closed}
            except Exception as expect_error:
                # TimeoutError or other exception - no new tab opened
                # This is expected if link navigates in same tab
                print(f"ℹ️  No new tab detected (timeout or same-tab navigation): {type(expect_error).__name__}")
            
            # ============================================================
            # SECOND TRY: Normal coordinate click (same-tab navigation)
            # ============================================================
            print(f"🖱️  Performing normal coordinate click (same-tab navigation)...")
            pyautogui.click(screen_coords["screen_x"], screen_coords["screen_y"])
            
            # Wait for page to load (navigation might happen in same tab)
            try:
                self.page.wait_for_load_state('domcontentloaded', timeout=5000)
            except:
                pass  # Page might already be loaded or navigation failed
            
            return {"success": True, "new_page": None, "switched": False, "popup_closed": False}
            
        except Exception as e:
            print(f"⚠️  Coordinate click failed: {e}")
            return {"success": False, "new_page": None, "switched": False, "popup_closed": False}
    
    def click_and_type_at_coordinates(self, x, y, text, scroll_into_view=True):
        """
        Click at viewport coordinates and type text directly using keyboard.
        This is more reliable than clicking and then using page.fill() with selectors.
        
        Args:
            x: X coordinate in viewport (from vision model)
            y: Y coordinate in viewport (from vision model)
            text: Text to type into the input field
            scroll_into_view: Whether to scroll the coordinates into view first
        
        Returns:
            dict with {"success": bool, "new_page": Page or None, "switched": bool}
        """
        try:
            # First, click at coordinates (using existing method)
            click_result = self.click_at_coordinates(x, y, scroll_into_view)
            
            if not click_result.get("success"):
                return click_result
            
            # If a new tab opened, we need to type on the new page
            if click_result.get("switched") and click_result.get("new_page"):
                page_to_use = click_result["new_page"]
            else:
                page_to_use = self.page
            
            # Wait a brief moment for the input field to be ready
            page_to_use.wait_for_timeout(300)
            
            # Type the text directly using keyboard (more reliable after coordinate click)
            print(f"⌨️  Typing text at clicked coordinates: '{text}'")
            page_to_use.keyboard.type(text, delay=50)
            
            print(f"✅ Successfully typed text at coordinates")
            return click_result
            
        except Exception as e:
            print(f"⚠️  Click and type at coordinates failed: {e}")
            return {"success": False, "new_page": None, "switched": False, "popup_closed": False}
    
    def take_window_screenshot(self, path, timeout=30000):
        """Takes a screenshot of the entire browser window including chrome/tabs"""
        try:
            if not self.page or self.page.is_closed():
                return None
            
            # Use CDP to capture the entire window including browser UI
            cdp = self.page.context.new_cdp_session(self.page)
            screenshot_data = cdp.send("Page.captureScreenshot", {"captureBeyondViewport": False})
            
            # Save the screenshot
            import base64
            with open(path, "wb") as f:
                f.write(base64.b64decode(screenshot_data["data"]))
            
            return str(path)
        except Exception as e:
            print(f"⚠️  Window screenshot failed (trying fallback): {e}")
            try:
                # Fallback to regular screenshot
                self.page.screenshot(path=path, full_page=False, timeout=timeout, animations="disabled", caret="hide")
                return str(path)
            except Exception as e2:
                print(f"⚠️  Fallback screenshot also failed: {e2}")
                return None


# ============================================================================
# PLAYWRIGHT MANAGER CLASS
# ============================================================================

class PlaywrightManager:
    def __init__(self, client, page=None):
        self.client = client
        self.page = page
    
    def set_page(self, page):
        self.page = page
    
    def generate_script(self, instruction, context=None):
        prompt = f"""Generate synchronous Playwright Python code based on this instruction.

        Instruction: {instruction}

        Requirements:
        - Use SYNCHRONOUS Playwright API only (no async/await)
        - Assume 'page' and 'vision_helpers' variables exist
        - No imports needed
        - Keep code simple
        
        COORDINATE-BASED CLICKING (PRIORITIZE ON CARRIER SITES):
        - IMPORTANT: On carrier sites, coordinate clicking with pyautogui is MORE RELIABLE than selectors
        - If instruction provides coordinates (x, y), use vision_helpers.click_at_coordinates(x, y)
        - This uses pyautogui for OS-level clicks, avoiding Playwright selector issues
        - Automatically handles scrolling and coordinate conversion
        - Example: vision_helpers.click_at_coordinates(645, 62)
        - More reliable than selectors for elements in overlays, modals, or complex DOMs

        URL NAVIGATION:
        - NEVER EVER hardcode/guess carrier URLs
        - NEVER use page.goto() with any carrier domain URLs
        - To reach carrier site: MUST find and click the carrier link, never navigate directly via page.goto()
        - All navigation to carrier sites must happen by clicking visible links/elements

        HANDLING NEW TABS:
        - Carrier links often open in new tabs/pages
        - IMPORTANT: page.context.pages is a PROPERTY (not a method) - use page.context.pages NOT page.context.pages()
        
        - FOR SELECTOR-BASED CLICKS that might open new tabs:
          old_page = page
          with page.context.expect_page() as new_page_info:
              link.click()
          new_page = new_page_info.value
          if new_page:
              page = new_page  # Switch to the new page
              old_page.close()  # Close the aggregator tab
        
        - FOR COORDINATE-BASED CLICKS that might open new tabs (e-Service links, carrier links, etc.):
          vision_helpers.click_at_coordinates(x, y)
          # NOTE: click_at_coordinates() automatically handles new tab detection internally
          # It uses expect_page() to catch new tabs via events, then falls back to normal click
          # If a new tab opens, it will automatically switch. No manual tab checking needed.
          # After calling click_at_coordinates(), just wait for page load:
          page.wait_for_load_state('load')
        
        - FOR CLICKING AND TYPING INTO INPUT FIELDS (when coordinates are known):
          # IMPORTANT: Use click_and_type_at_coordinates() instead of click_at_coordinates() + page.fill()
          # This avoids timeout issues with page.fill() after coordinate clicks
          vision_helpers.click_and_type_at_coordinates(x, y, 'text_to_enter')
          # This method handles clicking, waiting for input to be ready, and typing directly
          # No need to call page.fill() separately - it's all handled internally
          # Example: vision_helpers.click_and_type_at_coordinates(689, 130, 'SINI25432400')
        
        - IMPORTANT: You MUST reassign the 'page' variable (page = pages[-1] or page = new_page) for the system to detect the tab switch
        - The 'page' variable reassignment is what triggers automatic page reference updates in the automation system

        HANDLING OVERLAYS, MODALS, AND STRICT MODE VIOLATIONS:
        - If you encounter "strict mode violation" (multiple elements found) or elements in overlays/modals:
          - If that fails and vision model provides coordinates, use coordinate-based clicking:
            page.mouse.click(x_coord, y_coord)  # Click at specific coordinates
        - Coordinate clicking is useful when selectors fail due to complex DOM, overlays, or multiple matching elements
        
        CARRIER SITE NAVIGATION (after reaching carrier homepage):
        - ALWAYS look for 'E-Services', 'e-Service', or 'eService' links in the top navigation
        - AVOID generic 'Tracking' text that appears in multiple places

        Generate code:"""

        response = self.client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "You are a Playwright code generator. Output only synchronous Python code (NO async/await). No explanations or markdown."},
                {"role": "user", "content": prompt}
            ]
        )
        
        script = response.choices[0].message.content.strip()
        
        # Remove markdown code blocks if present
        if script.startswith("```python"):
            script = script[9:]
        if script.startswith("```"):
            script = script[3:]
        if script.endswith("```"):
            script = script[:-3]
        
        return script.strip()
    
    def compile_code(self, code, instruction=None, context=None, max_retries=3):
        for attempt in range(max_retries):
            try:
                compile(code, '<string>', 'exec')
                return {"success": True, "code": code, "attempts": attempt + 1}
            except SyntaxError as e:
                error_msg = f"SyntaxError at line {e.lineno}: {str(e)}"
                if attempt < max_retries - 1 and instruction:
                    code = self.generate_script(instruction, context)
                else:
                    return {"success": False, "error": error_msg, "attempts": attempt + 1}
            except Exception as e:
                error_msg = str(e)
                if attempt < max_retries - 1 and instruction:
                    code = self.generate_script(instruction, context)
                else:
                    return {"success": False, "error": error_msg, "attempts": attempt + 1}
        
        return {"success": False, "error": "Max retries reached", "attempts": max_retries}
    
    def validate_new_tab(self, new_page, context, milestone):
        """
        Validate if a newly opened tab is legitimate using context + vision.
        Returns True if tab should be switched to, False if it should be closed.
        """
        try:
            new_url = new_page.url
            prev_url = context.current_url
            carrier = context.carrier
            
            # Extract domains for comparison
            def get_domain(url):
                from urllib.parse import urlparse
                try:
                    return urlparse(url).netloc.lower()
                except:
                    return url.lower()
            
            new_domain = get_domain(new_url)
            prev_domain = get_domain(prev_url)
            
            print(f"🔍 Validating new tab: {new_domain}")
            
            # Quick URL-based checks
            url_valid = False
            
            # Case 1: Same domain as previous (legitimate navigation within same site)
            if new_domain == prev_domain:
                url_valid = True
                print(f"✅ Same domain as previous page")
            
            # Case 2: Contains carrier name
            elif carrier and carrier.lower() in new_url.lower():
                url_valid = True
                print(f"✅ URL contains carrier name: {carrier}")
            
            # If URL alone suggests it's valid, skip expensive vision check
            if url_valid:
                return True
            
            # Otherwise, use vision to make final decision
            print(f"⚠️  URL check inconclusive, using vision validation...")
            
            try:
                screenshot = new_page.screenshot(timeout=30000, animations="disabled", caret="hide")
                screenshot_path = f"temp_tab_validation_{int(time.time())}.png"
                new_page.screenshot(path=screenshot_path, timeout=30000, animations="disabled", caret="hide")
                
                vision_prompt = f"""Analyze this page and determine if it's related to shipping/cargo tracking.
                
                Context:
                - Carrier: {carrier.upper() if carrier else 'Unknown'}
                - Current milestone: {milestone}
                - Expected: Should show carrier branding, shipping services, or tracking interface
                
                Look for:
                1. Carrier logo or branding (e.g., HMM, Hyundai Merchant Marine)
                2. Shipping/cargo tracking interface
                3. Navigation menus for e-Services, tracking, cargo services
                4. Shipping-related content (containers, vessels, schedules)
                
                This is INVALID if it shows:
                - Generic advertisements
                - Unrelated commercial content
                - Survey/feedback forms
                - Promotional offers unrelated to shipping
                
                Answer with ONLY 'yes' or 'no':
                - yes = This page is related to the carrier or shipping tracking
                - no = This is an ad/unrelated page
                """
                
                response = self.client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": vision_prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{base64.b64encode(screenshot).decode()}"
                                    }
                                }
                            ]
                        }
                    ],
                    max_tokens=10
                )
                
                # Clean up temp file
                import os
                if os.path.exists(screenshot_path):
                    os.remove(screenshot_path)
                
                answer = response.choices[0].message.content.strip().lower()
                is_valid = "yes" in answer
                
                if is_valid:
                    print(f"✅ Vision confirmed: Tab is legitimate")
                else:
                    print(f"❌ Vision confirmed: Tab is ad/unrelated")
                
                return is_valid
                
            except Exception as vision_error:
                print(f"⚠️  Vision validation failed: {vision_error}, allowing tab by default")
                # If vision fails, be conservative and allow the tab
                return True
                
        except Exception as e:
            print(f"⚠️  Tab validation error: {e}, allowing tab by default")
            return True
    
    def execute(self, script, vision_helpers=None, context=None, milestone=None):
        if not self.page:
            return {"success": False, "error": "No page object available"}
        try:
            # Capture state BEFORE execution
            tabs_before = len(self.page.context.pages)
            try:
                url_before = self.page.url if not self.page.is_closed() else "unknown"
            except:
                url_before = "unknown"
            
            if vision_helpers:
                vision_helpers.popup_closed_by_click = False
            
            import time
            local_vars = {"page": self.page, "result": {}, "time": time}
            if vision_helpers:
                local_vars["vision_helpers"] = vision_helpers
            exec(script, {}, local_vars)
            result = local_vars.get("result")
            if not isinstance(result, dict):
                result = {}
            
            # Check if script explicitly switched to a new page (script set page variable)
            new_page = local_vars.get("page")
            script_switched = new_page and new_page != self.page
            
            if vision_helpers and vision_helpers.page != self.page:
                if not script_switched:
                    # click_at_coordinates() switched but script didn't update page variable
                    # Note: click_at_coordinates() already logged the switch, so we just detect it here
                    new_page = vision_helpers.page
                    script_switched = True
                    # We'll check this later to avoid duplicate popup closing
                elif new_page != vision_helpers.page:
                    # Both switched but to different pages - use vision_helpers.page (from click_at_coordinates)
                    new_page = vision_helpers.page
                    script_switched = True
            
            # Determine wait time based on script type
            if "click_at_coordinates" in script:
                wait_time = 2.5  # Longer wait for coordinate clicks
            else:
                wait_time = 1.5  # Standard wait for selector clicks
            
            # Wait for any new tabs to be registered in context
            time.sleep(wait_time)
            
            tabs_after = len(self.page.context.pages)
            auto_switched = False
            script_tab_valid = True  # Assume script's tab is valid if it switched
            
            if tabs_after > tabs_before:
                print(f"🆕 New tab detected ({tabs_before} → {tabs_after})")
                
                all_pages = self.page.context.pages
                new_tabs = all_pages[tabs_before:]  # Get newly opened tabs
                
                # If script switched, validate its choice first
                if script_switched and new_page in new_tabs:
                    try:
                        new_page.wait_for_load_state('domcontentloaded', timeout=5000)
                    except:
                        pass
                    
                    if context and milestone:
                        script_tab_valid = self.validate_new_tab(new_page, context, milestone)
                        if not script_tab_valid:
                            print(f"⚠️  Script switched to tab, but validation failed: {new_page.url}")
                    else:
                        script_tab_valid = True  # No context, assume valid
                
                # If script's tab is invalid or script didn't switch, find best valid tab
                if not script_switched or not script_tab_valid:
                    for new_tab in new_tabs:
                        # Skip script's tab if it's invalid
                        if script_switched and new_tab == new_page and not script_tab_valid:
                            continue
                        
                        try:
                            new_tab.wait_for_load_state('domcontentloaded', timeout=5000)
                        except:
                            pass
                        
                        if context and milestone:
                            is_valid = self.validate_new_tab(new_tab, context, milestone)
                        else:
                            print(f"⚠️  No context for validation, accepting tab")
                            is_valid = True
                        
                        if is_valid:
                            print(f"✅ Switching to validated new tab: {new_tab.url}")
                            new_tab.bring_to_front()
                            new_page = new_tab
                            auto_switched = True
                            
                            # Close any popups on the new tab (only if not already closed by click_at_coordinates)
                            if vision_helpers:
                                vision_helpers.page = new_tab
                                if not vision_helpers.popup_closed_by_click:
                                    try:
                                        vision_helpers.close_popup()
                                    except Exception as popup_error:
                                        print(f"⚠️  Popup closing failed: {popup_error}")
                                else:
                                    print(f"ℹ️  Popups already closed by click_at_coordinates(), skipping duplicate close")
                            
                            break  # Use first valid tab
                        else:
                            print(f"❌ Closing invalid tab: {new_tab.url}")
                            try:
                                new_tab.close()
                            except:
                                pass
                elif script_switched and script_tab_valid:
                    # Script's tab is valid, use it
                    print(f"✅ Script switched to valid tab: {new_page.url}")
                    
                    # Close any popups on the new tab (only if not already closed by click_at_coordinates)
                    if vision_helpers:
                        vision_helpers.page = new_page
                        if not vision_helpers.popup_closed_by_click:
                            try:
                                vision_helpers.close_popup()
                            except Exception as popup_error:
                                print(f"⚠️  Popup closing failed: {popup_error}")
                        else:
                            print(f"ℹ️  Popups already closed by click_at_coordinates(), skipping duplicate close")
                    
                    auto_switched = True
            
            # Scenario B: Script switched but no new tabs (might be same-tab navigation)
            # Validate the script's chosen tab if it switched
            elif script_switched:
                if context and milestone:
                    try:
                        script_tab_valid = self.validate_new_tab(new_page, context, milestone)
                        if not script_tab_valid:
                            print(f"⚠️  Script switched to tab, but validation failed: {new_page.url}")
                            # Script switched but tab is invalid - check current page
                            current_url = new_page.url if not new_page.is_closed() else "unknown"
                            print(f"⚠️  Current tab URL: {current_url}")
                    except Exception as e:
                        print(f"⚠️  Error validating script's tab: {e}")
                        script_tab_valid = True  # Default to valid on error
                else:
                    script_tab_valid = True
                
                if script_tab_valid:
                    auto_switched = True
            
            # Scenario C: Check for same-tab navigation (URL changed but no new tab)
            try:
                url_after = self.page.url if not self.page.is_closed() else "unknown"
                if url_after != "unknown" and url_after != url_before and tabs_after == tabs_before and not script_switched:
                    print(f"🔄 Same-tab navigation detected: {url_before} → {url_after}")
                    # This is valid - navigation happened in same tab
            except:
                pass
            
            # Determine if we switched tabs (either script did or auto-detection did)
            switched_to_new_page = script_switched or auto_switched
            
            # Don't include Page object in return dict (not JSON serializable)
            # We'll get the new page reference from local_vars in the calling code
            return_dict = {
                "success": True, 
                "result": result,
                "switched_to_new_page": switched_to_new_page
            }
            
            # Check if click_at_coordinates already closed popups (from script execution)
            # If the script used click_at_coordinates and it switched, popups may already be closed
            popup_already_closed_by_click = False
            if "click_at_coordinates" in script and switched_to_new_page:
                # click_at_coordinates() handles popup closing internally when it switches tabs
                # We need to check if it was actually called and succeeded
                # This is a heuristic - if click_at_coordinates was in the script and we switched, assume it handled popups
                popup_already_closed_by_click = True
            
            return_dict["popup_already_closed"] = popup_already_closed_by_click
            
            # Store the new page reference in a way we can retrieve it (attach to return dict as attribute)
            # This is a workaround since we can't serialize Page objects
            if switched_to_new_page:
                return_dict["_new_page_ref"] = new_page  # Internal reference, not logged
            
            return return_dict
        except Exception as e:
            error_str = str(e) if e else repr(e)
            return {"success": False, "error": error_str, "error_type": type(e).__name__}


# ============================================================================
# LOGGER CLASS
# ============================================================================

class Logger:
    def __init__(self, log_dir, run_id=None):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.complete_log_path = self.log_dir / "complete.jsonl"
        self.pipeline_log_path = self.log_dir / "pipeline.jsonl"
        self.current_step = 0
        self.step_start_time = None
        self.step_operations = []
        self.step_errors = []
    
    def log_operation(self, operation_type, data=None, success=True, duration_ms=None):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "run_id": self.run_id,
            "step": self.current_step,
            "operation_type": operation_type,
            "data": data or {},
            "success": success
        }
        if duration_ms:
            entry["duration_ms"] = duration_ms
        
        with open(self.complete_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        
        self.step_operations.append(operation_type)
        if not success:
            self.step_errors.append({"operation": operation_type, "data": data})
    
    def start_step(self):
        self.current_step += 1
        self.step_start_time = time.time()
        self.step_operations = []
        self.step_errors = []
    
    def end_step(self, milestone=None, current_url=None, success=True):
        duration_ms = int((time.time() - self.step_start_time) * 1000) if self.step_start_time else 0
        
        pipeline_entry = {
            "run_id": self.run_id,
            "step": self.current_step,
            "timestamp": datetime.now().isoformat(),
            "operations": self.step_operations,
            "milestone": milestone,
            "current_url": current_url,
            "success": success,
            "errors": self.step_errors,
            "duration_ms": duration_ms
        }
        
        with open(self.pipeline_log_path, "a") as f:
            f.write(json.dumps(pipeline_entry) + "\n")
        
        return pipeline_entry
    
    def get_recent_pipeline_logs(self, count=6):
        if not self.pipeline_log_path.exists():
            return []
        
        with open(self.pipeline_log_path, "r") as f:
            lines = f.readlines()
        
        recent_lines = lines[-count:] if len(lines) > count else lines
        return [json.loads(line) for line in recent_lines]


# ============================================================================
# AGENT FUNCTIONS
# ============================================================================

def reasoning_agent(client, context):
    # Extract key variables
    carrier = context.get("carrier", "unknown")
    current_url = context.get("current_url", "unknown")
    last_milestone = context.get("last_achieved_milestone", "None")
    next_milestone = context.get("next_milestone", "Unknown")
    remaining_count = len(context.get("remaining_milestones", []))
    
    # Build complete system prompt with variable interpolation
    system_prompt = f"""You are the COMMANDER. You analyze the automation state and decide what needs to happen next.

    CURRENT STATE:
    - Target Carrier: {carrier.upper()}
    - Current URL: {current_url}
    - Last Achieved Milestone: {last_milestone}
    - FOCUS → Next Milestone: {next_milestone}
    - Remaining Milestones: {remaining_count}

    YOUR JOB:
    1. Check if goal is achieved (voyage number and arrival date extracted)
    2. Look at context.next_milestone - this is your ONLY focus for this step
    3. Tell vision agent what to look for (specific to the next_milestone)
    4. Tell language agent what action to take (specific to the next_milestone)

    MILESTONE-BASED WORKFLOW:
    - The context provides:
    * last_achieved_milestone: What was just completed
    * next_milestone: What you need to focus on NOW (from remaining_milestones)
    * remaining_milestones: Full task list of what's left to do
    - Focus ONLY on completing next_milestone - do NOT try to do multiple milestones at once
    - Your instructions should accomplish ONLY the next_milestone, nothing more
    - Once next_milestone is achieved, the system will automatically move to the following milestone

    KEY RULES:
    - Use context.carrier to identify which carrier to look for (currently: {carrier})
    - ALWAYS use context.current_url (the deterministic URL from the browser) - DO NOT infer or guess URLs from screenshots or other context
    - If context.current_url doesn't match expected page → navigation is needed
    - Look for "E-Services", "e-Service", "eService", or "Online Services" navigation items on carrier sites BEFORE looking for input fields
    - If same action failed 2+ times → change strategy
    - If failure_alert exists in context → immediately try different approach

    AD PAGE DETECTION AND RECOVERY:
    CRITICAL: Websites often redirect to ad pages, hijacking the tracking flow. You MUST detect and recover.

    DETECTION SIGNS:
    - URL contains: ads, advert, marketing, promo, offer, survey, feedback, redirect, click, track (as subdomain)
    - URL domain completely unrelated to shipping/carrier/tracking (e.g., random commercial sites, surveys)
    - Vision shows: pop-ups, "Click here", "Special offer", "Survey", completely unrelated content
    - History shows: we were on legitimate tracking site, then suddenly on unrelated domain

    RECOVERY PROCEDURE (when ad page detected):
    1. Look at context.history to find the LAST LEGITIMATE tracking-related URL:
    - Hub site URL
    - Carrier site URL
    - Any URL that was part of successful milestone completion
    2. Instruct: "Navigate back to [last_legitimate_url] using page.goto()"
    3. Identify which milestone we should be at based on that URL:
    - If last legitimate URL was hub site → reset to "Reached hub site" milestone
    - If last legitimate URL was carrier site → reset to appropriate carrier milestone
    4. In your response, note: "Ad page detected, recovering to [last_legitimate_url]"

    EXAMPLE:
    Current URL: "https://random-ads-site.com/offer"
    History shows: Last URL was "http://seacargotracking.net/"
    Action: "Ad page detected. Navigate back to http://seacargotracking.net/ and continue from 'Reached hub site' milestone"

    FAILURE RECOVERY WITH VISION:
    - Check history for failed steps (timeout, element not found)
    - If vision_analysis_after_action shows elements with coordinates → instruct coordinate-based clicking
    - NEVER assume success if execution failed - retry with vision-guided approach

    COORDINATE CLICKING PRIORITY:
    - On CARRIER SITES (not aggregator): Prioritize coordinate clicking over selectors when vision provides coordinates
    - On AGGREGATOR SITES: Use selectors first, coordinates as fallback
    - Coordinate clicking with pyautogui is MORE RELIABLE on carrier sites (avoids overlays, modals, strict mode)
    - Example instruction: "Vision found input at (645, 62). Use vision_helpers.click_at_coordinates(645, 62)"

    CRITICAL URL RESTRICTIONS - NEVER VIOLATE THESE:
    - NEVER EVER instruct direct navigation to carrier sites
    - NEVER suggest page.goto() with any carrier domain URLs in your language_instruction
    - To reach carrier site: ALWAYS instruct to "find and click the {carrier} carrier link on the aggregator site"
    - Only allow page.goto() for known aggregator sites (e.g., "navigate to seacargotracking.net" is OK)

    CRITICAL REMINDER:
    Your ONLY job this step is: "{next_milestone}"
    Do NOT try to accomplish multiple milestones at once.

    OUTPUT FORMAT:
    {{
    "goal_achieved": boolean,
    "next_milestone": "copy from context.next_milestone - what you're focusing on completing in this step",
    "vision_objective": "what vision should look for (specific to next_milestone only)",
    "language_instruction": "what action to take (specific to next_milestone only - be explicit and focused)",
    "reasoning": "why this step completes next_milestone",
    "failure_analysis": "if failures are detected in history, explain how to avoid repetition",
    "ad_recovery": {{
        "detected": boolean (true if ad page detected),
        "recovery_url": "URL to navigate back to (from history)",
        "reset_to_milestone": "milestone name to reset to based on recovery URL"
    }} (only include if ad page detected, otherwise omit this field)
    }}"""
    
    prompt = f"""Current Context:
            {json.dumps(context, indent=2)}
            Analyze the context and decide the next step."""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    )
    
    result = response.choices[0].message.content.strip()
    
    # Extract JSON
    start = result.find("{")
    end = result.rfind("}") + 1
    if start != -1 and end > start:
        result = result[start:end]
    
    return json.loads(result)


def vision_agent(client, screenshot_path, objective):
    with open(screenshot_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    
    # Build complete system prompt
    system_prompt = f"""You are a vision analysis agent. You analyze screenshots to understand page state and locate elements.

    CURRENT OBJECTIVE:
    {objective}

    YOUR JOB:
    - Analyze screenshots based on the objective above
    - Look for the SPECIFIC element/pattern described in the objective ONLY
    - DO NOT return random/unrelated elements (cookie popups, navigation links, etc.) unless explicitly asked for them
    - If objective asks for a carrier link → return ONLY carrier links matching that name, ignore everything else
    - If objective asks for "Tracking navigation item" → return ONLY tracking-related navigation items, ignore everything else
    - Match the exact carrier name when looking for carrier links (don't pick the first one)
    - When analyzing post-execution screenshots, identify what changed, if navigation occurred, errors, etc.
    - When objective asks to analyze page state → you can list key elements, but prioritize what's relevant to the workflow

    CRITICAL FILTERING:
    - Only return elements that DIRECTLY match the objective
    - Ignore cookie consent popups, random navigation menus, footer links unless specifically requested
    - If objective is vague ("analyze page state"), still filter to workflow-relevant elements (carrier links, tracking inputs, search buttons, etc.)
    - DO NOT list every button/link on the page - only what's relevant to the current task

    YOUR ROLE IN THE WORKFLOW:
    - You analyze BEFORE the language agent decides actions (you inform what's available on the page)
    - You analyze AFTER script execution (you inform what changed and current state)
    - Your analysis guides both the language agent (what actions are possible) and the reasoning agent (what happened)

    OUTPUT:
    {{
    "found": boolean,
    "elements": [{{"label": "name", "x": coord, "y": coord, "confidence": 0.0-1.0}}],
    "notes": "observations about the page state, what changed, what's visible, any errors"
    }}"""
    
    prompt = f"""Objective: {objective}
            Analyze this screenshot and locate the elements."""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}}
            ]}
        ]
    )
    
    result = response.choices[0].message.content.strip()
    
    # Extract JSON
    start = result.find("{")
    end = result.rfind("}") + 1
    if start != -1 and end > start:
        result = result[start:end]
    
    return json.loads(result)


def analyze_window_for_tabs(client, window_screenshot_path):
    """Analyze a window screenshot to extract tab information"""
    with open(window_screenshot_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    
    prompt = """Analyze this browser window screenshot. Extract:
    1. Total number of open tabs (count all visible browser tabs)
    2. Tab names/titles if visible (list each tab's title from left to right)
    3. Which tab is currently active (usually highlighted or has different styling)
    4. Any visual indicators of tab state (loading, favicon, etc.)
    
    Return JSON format:
    {
        "tab_count": <number>,
        "tabs": [
            {"index": 0, "title": "Tab title", "active": true/false},
            {"index": 1, "title": "Tab title", "active": true/false}
        ],
        "notes": "Any additional observations about tabs"
    }"""
    
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "You are analyzing browser window screenshots to extract tab information. Be precise about counting tabs and reading tab titles."},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}}
            ]}
        ]
    )
    
    result = response.choices[0].message.content.strip()
    
    # Extract JSON
    start = result.find("{")
    end = result.rfind("}") + 1
    if start != -1 and end > start:
        result = result[start:end]
    
    return json.loads(result)


def language_agent(client, context, vision_info, reasoning_instruction=None):
    next_milestone = context.get("next_milestone", "Unknown")
    current_url = context.get("current_url", "unknown")
    carrier = context.get("carrier", "unknown")
    
    is_carrier_site = current_url and "seacargotracking" not in current_url and "google.com" not in current_url
    site_type = "CARRIER SITE" if is_carrier_site else "AGGREGATOR/OTHER"
    
    system_prompt = f"""You translate the reasoning agent's instructions into actionable code instructions.

    CURRENT CONTEXT:
    - Milestone Goal: {next_milestone}
    - Current URL: {current_url}
    - Site Type: {site_type}
    - Target Carrier: {carrier.upper()}

    REASONING AGENT'S INSTRUCTION:
    {reasoning_instruction if reasoning_instruction else "Not provided"}

    YOUR JOB:
    The reasoning agent tells you what action to take. Your job is to:
    1. Decide if code is needed (navigation/clicking/typing/extraction = yes, goal achieved = no)
    2. Write clear instructions for code generation based on what reasoning agent said

    CRITICAL RULES:
    - **If reasoning agent says "navigate to X" → YOU MUST set needs_code=true and generate navigation instruction**
    - **If reasoning agent says "click Y" → YOU MUST set needs_code=true and generate click instruction**
    - **If reasoning agent says "enter booking ID" → YOU MUST set needs_code=true and generate input instruction**
    - DO NOT say "no code needed" when reasoning agent explicitly requests an action
    - Only say "no code needed" if goal is achieved or reasoning agent explicitly asks for more analysis first

    COORDINATE-BASED CLICKING (PRIORITIZE ON CARRIER SITES):
    - On carrier sites (URLs NOT containing 'seacargotracking'): ALWAYS use coordinate clicking when vision provides coordinates
    - On aggregator sites: Use selectors first, coordinates as fallback
    - If reasoning agent provides coordinates (x, y) → instruct: "Use vision_helpers.click_at_coordinates(x, y)"
    - Vision coordinates with pyautogui are MORE RELIABLE than selectors on carrier sites
    - Avoids issues with overlays, modals, strict mode violations, and complex DOMs

    RESTRICTIONS:
    - NEVER EVER instruct to hardcode/guess carrier URLs
    - NEVER instruct page.goto() with carrier domains - only allow for known aggregator sites
    - To reach carrier site: MUST instruct to find and CLICK the {carrier} carrier link, never navigate directly
    - If navigation needed and no link visible → instruct to navigate to known aggregator site (e.g., seacargotracking.net)
    - Request SYNCHRONOUS Playwright code only

    REMINDER FOR THIS STEP:
    - Focus ONLY on: "{next_milestone}"
    - On {site_type}: {"PRIORITIZE coordinate clicking" if is_carrier_site else "Use selectors first, coordinates as fallback"}

    OUTPUT:
    {{
    "needs_code": boolean,
    "needs_vision": boolean,
    "instruction": "specific action for playwright manager",
    "expected_outcome": "what should happen",
    "data_to_extract": ["fields if extraction step"]
    }}

    When to set needs_vision=true:
    - If you need to find elements on the page (carrier links, input fields, buttons, etc.)
    - If reasoning agent says "find" or "locate" something
    - If you need to analyze what's visible before generating code
    - Set needs_vision=false if you already have enough information (e.g., navigation to known URL)"""
    
    prompt = f"""Context:
            {json.dumps(context, indent=2)}

            Vision Analysis:
            {json.dumps(vision_info, indent=2)}

            Based on the reasoning agent's instruction above, decide if code is needed and provide instruction for playwright manager."""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    )
    
    result = response.choices[0].message.content.strip()
    
    # Extract JSON
    start = result.find("{")
    end = result.rfind("}") + 1
    if start != -1 and end > start:
        result = result[start:end]
    
    return json.loads(result)


def detect_repeated_failures(history, threshold=2):
    if len(history) < 2:
        return None
    
    recent_errors = []
    for entry in history[-3:]:
        if entry.get("errors") and len(entry["errors"]) > 0:
            recent_errors.append(entry)
    
    if len(recent_errors) >= threshold:
        return {
            "detected": True,
            "count": len(recent_errors),
            "pattern": "Multiple consecutive failures detected",
            "recommendation": "Switch to alternative approach immediately"
        }
    
    return None


def determine_step_success(client, milestone_goal, language_result, context, current_url, post_execution_vision_results):
    if not language_result.get("needs_code"):
        return True, "No code execution needed"
    
    # Prepare context for LLM decision
    decision_context = {
        "milestone_goal": milestone_goal,
        "current_url": current_url,
        "execution_success": context.last_response_data.get("success", False) if context.last_response_data else False,
        "execution_error": context.last_response_data.get("error", None) if context.last_response_data else None,
        "vision_analysis": post_execution_vision_results
    }
    
    prompt = f"""Let's determine if the automation milestone was achieved. Answer these questions step by step:

    QUESTION 1: What milestone were we trying to reach?
    Answer: "{milestone_goal}"

    QUESTION 2: What is the current state now?
    - Current URL: {current_url}
    - What the page shows (vision): {post_execution_vision_results[0].get('notes', 'No description') if post_execution_vision_results and len(post_execution_vision_results) > 0 else "No visual analysis available"}
    - Script note: {f"Encountered error: {decision_context['execution_error']}" if decision_context["execution_error"] else "Completed without errors"}

    QUESTION 3: Using common sense, has the milestone been achieved?

    Think through this logically:
    - If milestone says "Reached [site name]" → Check: Does URL contain that site's domain? Does vision confirm we're on that site?
    - If milestone says "Clicked [something] and reached [site]" → Check: Are we actually ON the target site now? (not just seeing it as a link)
    - If milestone says "Found [element]" → Check: Did vision see that element on the page?
    - If milestone says "Submitted [form]" → Check: Did the action complete and we see confirmation/results?
    - If milestone says "Results displayed" → Check VERY STRINGENTLY: 
    a) Does vision see actual tracking data (voyage number, vessel name, arrival date, ETA, container status)?
    b) Check for FAILURE indicators: error messages ("No results found", "Invalid booking ID", "Not found"), empty results, or just input forms/dashboards
    c) A generic "dashboard" or "control panel" WITHOUT specific shipping data → FAILURE
    d) Error messages or "no data" states → FAILURE
    e) Only if VALID tracking results with shipping data are visible → SUCCESS
    - If milestone says "Data extracted" → Check STRINGENTLY: Does vision confirm that BOTH voyage number AND arrival date (or ETA) are explicitly visible and readable? Both required fields must be present for SUCCESS.

    IMPORTANT EDGE CASES:
    - Partial script success counts: If milestone is "Reached X site" and URL shows X site AND vision confirms X site, SUCCESS even if script had errors doing extra things
    - Script tried multiple actions but only first one matters: If milestone is about navigation and navigation succeeded, ignore errors in subsequent actions
    - Timeouts don't always mean failure: If page loaded correctly (vision confirms) but script timed out waiting, SUCCESS
    - Fatal errors: If URL is chrome-error:// or about:blank → FAILURE always
    - Wrong site: If vision clearly says we're on a different site than milestone requires → FAILURE
    - General confusion: If vision does not show specific tracking results (voyage, vessel, arrival date), this is NOT "Results displayed" - FAILURE

    Now answer: Did we achieve the milestone "{milestone_goal}"?

    Respond with ONLY JSON: {{"success": true/false, "reasoning": "brief explanation of your common sense conclusion"}}"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        
        result = response.choices[0].message.content.strip()
        start = result.find("{")
        end = result.rfind("}") + 1
        if start != -1 and end > start:
            result = result[start:end]
        
        decision = json.loads(result)
        success = decision.get("success", False)
        reasoning = decision.get('reasoning', 'No reason provided')
        print(f"🤖 {'✅' if success else '❌'} {reasoning}")
        return success, reasoning
    
    except Exception as e:
        print(f"⚠️ LLM decision failed: {e}")
        fallback_success = not any(err in (current_url or "") for err in ["chrome-error://", "about:blank"])
        return fallback_success, f"LLM evaluation failed: {e}"

def real_tracking_process(booking_id, carrier="hmm", max_steps=20):
    client = OpenAI()
    log_dir = Path("logs") / booking_id
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    
    if log_dir.exists():
        try:
            shutil.rmtree(log_dir)
            print(f"🧹 Cleaned up existing logs for {booking_id}")
        except Exception as cleanup_error:
            print(f"⚠️  Could not fully clean up logs (files may be in use): {cleanup_error}")
    
    logger = Logger(log_dir, run_id)
    print(f"🔖 Run ID: {run_id}")
    context = CurrentContext()
    context.set_goal(booking_id, carrier)
    print(f"📦 Tracking booking {booking_id} for carrier: {carrier}")
    
    # ═══════════════════════════════════════════════════════════════
    # CHECK FOR CACHED FINAL RESULTS
    # ═══════════════════════════════════════════════════════════════
    cached_final_results = milestone_cache.get_final_results(carrier, booking_id)
    if cached_final_results:
        print(f"🎯 Found cached final results for {carrier}:{booking_id}")
        logger.log_operation("cached_final_results_found", cached_final_results)
        
        voyage_number = cached_final_results.get("voyage_number")
        
        print(f"🔄 Re-verifying arrival date using cached scripts...")

        arrival_date = cached_final_results.get("arrival_date")
        
        print(f"✅ Using cached results: Voyage={voyage_number}, Arrival={arrival_date}")
        return {
            "voyage_number": voyage_number,
            "arrival_date": arrival_date,
            "from_cache": True
        }
    
    print("⏳ Waiting for browser to be ready...")
    time.sleep(10)
    
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
        print("✅ Connected to existing browser session")
        
        contexts = browser.contexts
        if contexts and contexts[0].pages:
            page = contexts[0].pages[0]
            print(f"📄 Using existing page: {page.url}")
        else:
            if contexts:
                page = contexts[0].new_page()
            else:
                page = browser.new_page()
            print(f"📄 Created new page (no existing pages found)")

        vision_helpers = VisionHelpers(page)
        playwright_mgr = PlaywrightManager(client, page)
        
        context.update_url(page.url)
        context.update_milestone("Starting automation")
        
        for step in range(1, max_steps + 1):
            if "voyage_number" in context.extracted_data and "arrival_date" in context.extracted_data:
                logger.log_operation("goal_achieved", context.extracted_data)
                break
            
            logger.start_step()
            logger.log_operation("step_start", {"step": step, "booking_id": booking_id})
            
            context.clear_stale_context()
            
            # ═══════════════════════════════════════════════════════════════
            # CACHE CHECK - Try cached script for this milestone FIRST
            # ═══════════════════════════════════════════════════════════════
            next_milestone = context.remaining_milestones[0] if context.remaining_milestones else None
            cached_milestone = None
            cache_hit = False
            
            if next_milestone:
                cached_milestone = milestone_cache.get_milestone_cache(carrier, booking_id, next_milestone)
                
                if cached_milestone:
                    print(f"🔍 Cache hit for milestone: '{next_milestone}'")
                    logger.log_operation("cache_check", {
                        "milestone": next_milestone,
                        "cache_found": True,
                        "cached_at": cached_milestone.get("cached_at")
                    })
                    
                    try:
                        # Execute cached script
                        cached_script = cached_milestone.get("script")
                        logger.log_operation("cache_execution_start", {"script": cached_script})
                        print(f"⚡ Executing cached script for '{next_milestone}'")
                        
                        exec_result = playwright_mgr.execute(cached_script, vision_helpers, context, next_milestone)
                        
                        # Handle page switching from cached script
                        new_page_ref = exec_result.pop("_new_page_ref", None) if exec_result.get("success") else None
                        
                        logger.log_operation("cache_execution_result", exec_result)
                        context.update_response(exec_result, "cache_executed")
                        
                        if exec_result.get("switched_to_new_page") and new_page_ref:
                            try:
                                new_page = new_page_ref
                                old_url = page.url if not page.is_closed() else "unknown"
                                old_page = page
                                
                                page = new_page
                                vision_helpers.page = new_page
                                playwright_mgr.set_page(new_page)
                                
                                try:
                                    if not old_page.is_closed():
                                        old_page.close()
                                        print(f"🗑️  Closed old tab: {old_url}")
                                except Exception as close_error:
                                    print(f"⚠️  Could not close old tab: {close_error}")
                                
                                new_url = new_page.url if not new_page.is_closed() else "unknown"
                                context.update_url(new_url)
                                logger.log_operation("tab_switched_from_cache", {
                                    "old_url": old_url, 
                                    "new_url": new_url,
                                    "old_tab_closed": True,
                                    "step": step
                                })
                                print(f"🔄 Switched to new tab (from cache): {old_url} → {new_url}")
                                
                                # Close any popups on the new tab (only if not already closed by click_at_coordinates)
                                if not vision_helpers.popup_closed_by_click:
                                    try:
                                        vision_helpers.close_popup()
                                        logger.log_operation("popup_closed_after_cache_tab_switch", {"url": new_url})
                                    except Exception as popup_error:
                                        logger.log_operation("popup_close_error_from_cache", {"error": str(popup_error)}, success=False)
                                        print(f"⚠️  Popup closing failed: {popup_error}")
                                else:
                                    print(f"ℹ️  Popups already closed by click_at_coordinates(), skipping duplicate close")
                                    logger.log_operation("popup_already_closed_by_click_cache", {"url": new_url})
                            except Exception as e:
                                logger.log_operation("tab_switch_error_from_cache", {"error": str(e)}, success=False)
                                print(f"⚠️  Error switching to new tab (from cache): {e}")
                        
                        # Update URL
                        try:
                            time.sleep(0.5)
                            current_url = page.url if not page.is_closed() else context.current_url or "unknown"
                            context.update_url(current_url)
                            logger.log_operation("url_updated_after_cache", {"url": current_url})
                        except Exception as e:
                            logger.log_operation("url_update_error_after_cache", {"error": str(e)}, success=False)
                        
                        # ═══════════════════════════════════════════════════════════════
                        # VALIDATE CACHE EXECUTION - Don't blindly trust script success!
                        # ═══════════════════════════════════════════════════════════════
                        if exec_result.get("success"):
                            print(f"🔍 Validating cached milestone completion: '{next_milestone}'")
                            
                            # Use determine_step_success to validate milestone was ACTUALLY achieved
                            step_succeeded, step_reasoning = determine_step_success(
                                client=client,
                                milestone_goal=next_milestone,
                                language_result=exec_result,
                                context=context,
                                current_url=context.current_url,
                                post_execution_vision_results={}  # Cache execution, no vision yet
                            )
                            
                            if step_succeeded:
                                # Cache validation passed - mark milestone complete and skip LLM reasoning
                                cache_hit = True
                                context.update_milestone(next_milestone)
                                logger.log_operation("milestone_completed_from_cache", {
                                    "milestone": next_milestone,
                                    "remaining": context.remaining_milestones,
                                    "validation_reasoning": step_reasoning
                                })
                                print(f"✅ Cache validation passed: Milestone '{next_milestone}' completed")
                                
                                # End this step and continue to next iteration
                                pipeline_entry = logger.end_step(
                                    milestone=next_milestone,
                                    current_url=context.current_url,
                                    success=True
                                )
                                context.add_to_history(pipeline_entry)
                                continue  # Skip to next step
                            else:
                                # Cache validation failed - fall back to LLM reasoning
                                print(f"⚠️  Cache validation failed for '{next_milestone}': {step_reasoning}")
                                logger.log_operation("cache_validation_failed", {
                                    "milestone": next_milestone,
                                    "reasoning": step_reasoning
                                }, success=False)
                                # cache_hit remains False, will fall through to LLM reasoning
                    
                    except Exception as cache_error:
                        print(f"❌ Cache execution error: {cache_error}, falling back to LLM reasoning")
                        logger.log_operation("cache_execution_error", {"error": str(cache_error)}, success=False)
                else:
                    logger.log_operation("cache_check", {
                        "milestone": next_milestone,
                        "cache_found": False
                    })
            
            # ═══════════════════════════════════════════════════════════════
            # LLM REASONING - If cache miss or cache execution failed
            # ═══════════════════════════════════════════════════════════════
            if not cache_hit:
                failure_detection = detect_repeated_failures(context.history)
                if failure_detection:
                    logger.log_operation("failure_pattern_detected", failure_detection, success=False)
                    print(f"⚠️  Repeated failures detected: {failure_detection['pattern']}")
                
                context_dict = context.to_dict()
                if failure_detection:
                    context_dict["failure_alert"] = failure_detection
                
                logger.log_operation("reasoning_query", {"context": context_dict})
                reasoning_result = reasoning_agent(client, context_dict)
                logger.log_operation("reasoning_response", reasoning_result)
                
                # ═══════════════════════════════════════════════════════════════
                # AD PAGE RECOVERY - Check if LLM detected ad page hijacking
                # ═══════════════════════════════════════════════════════════════
                ad_recovery = reasoning_result.get("ad_recovery")
                if ad_recovery and ad_recovery.get("detected"):
                    recovery_url = ad_recovery.get("recovery_url")
                    reset_milestone = ad_recovery.get("reset_to_milestone")
                    
                    print(f"🚨 Ad page detected! Recovering to: {recovery_url}")
                    logger.log_operation("ad_page_detected", {
                        "current_url": context.current_url,
                        "recovery_url": recovery_url,
                        "reset_to_milestone": reset_milestone
                    })
                    
                    try:
                        # Navigate back to legitimate tracking URL
                        page.goto(recovery_url, timeout=30000, wait_until='load')
                        time.sleep(1)
                        
                        current_url = page.url
                        context.update_url(current_url)
                        
                        # Reset milestones to appropriate point
                        if reset_milestone:
                            # Find where reset_milestone is in the milestone list
                            all_milestones = DOMAIN_KNOWLEDGE["shipping_tracking_workflow"]["milestones"]
                            formatted_milestones = [m.format(carrier=context.carrier.upper()) for m in all_milestones]
                            
                            try:
                                reset_idx = formatted_milestones.index(reset_milestone)
                                # Set remaining milestones from reset point onwards
                                context.remaining_milestones = formatted_milestones[reset_idx:]
                                context.last_achieved_milestone = formatted_milestones[reset_idx - 1] if reset_idx > 0 else "Starting automation"
                                
                                print(f"♻️  Milestones reset to: {reset_milestone}")
                                logger.log_operation("milestones_reset", {
                                    "reset_to": reset_milestone,
                                    "remaining": context.remaining_milestones
                                })
                            except ValueError:
                                print(f"⚠️  Could not find milestone '{reset_milestone}' in list, keeping current milestones")
                        
                        logger.log_operation("ad_recovery_success", {
                            "recovered_to_url": current_url,
                            "current_milestone": context.remaining_milestones[0] if context.remaining_milestones else None
                        })
                        print(f"✅ Recovered! Now at: {current_url}")
                        
                        # End this step and continue to next iteration
                        pipeline_entry = logger.end_step(
                            milestone=f"Ad recovery to {reset_milestone}",
                            current_url=current_url,
                            success=True
                        )
                        context.add_to_history(pipeline_entry)
                        continue  # Skip to next step with recovered state
                        
                    except Exception as recovery_error:
                        print(f"❌ Ad recovery failed: {recovery_error}")
                        logger.log_operation("ad_recovery_failed", {"error": str(recovery_error)}, success=False)
                        # Continue with normal flow despite recovery failure
            else:
                # Cache hit, skip to end of loop (already continued above)
                reasoning_result = {}
            
            if reasoning_result.get("goal_achieved"):
                logger.log_operation("goal_achieved", context.extracted_data)
                logger.end_step(
                    milestone=reasoning_result.get("next_milestone"),
                    current_url=context.current_url,
                    success=True
                )
                break
            
            # Skip rest if cache hit (continue statement would have already skipped)
            if cache_hit:
                continue
            
            screenshot_dir = log_dir / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            
            screenshots = []
            try:
                screenshots = vision_helpers.take_multifold_screenshots(f"step_{step}", screenshot_dir)
                logger.log_operation("screenshot_multifold", {"count": len(screenshots), "paths": [s["path"] for s in screenshots]})
            except Exception as e:
                logger.log_operation("screenshot_error", {"error": str(e), "error_type": type(e).__name__}, success=False)
                print(f"⚠️  Screenshot failed: {e}")
                try:
                    single_screenshot = vision_helpers.take_screenshot(screenshot_dir / f"step_{step}_fallback.png")
                    screenshots = [{"path": str(single_screenshot), "scroll_top": 0}]
                    logger.log_operation("screenshot_fallback", {"path": str(single_screenshot)})
                except Exception as fallback_error:
                    logger.log_operation("screenshot_fallback_failed", {"error": str(fallback_error)}, success=False)
                    print(f"⚠️  Screenshot fallback also failed: {fallback_error}")
            
            context.update_screenshots(screenshots[0]["path"] if screenshots else None, screenshots)
            
            reasoning_instruction = reasoning_result.get("language_instruction", "")
            logger.log_operation("language_query", {"context": context.to_dict(), "reasoning_instruction": reasoning_instruction})
            language_result = language_agent(client, context.to_dict(), [], reasoning_instruction)
            logger.log_operation("language_response", language_result)
            
            vision_results = []
            if language_result.get("needs_vision"):
                vision_objective = reasoning_result.get("vision_objective", "Analyze the page")
                for screenshot in screenshots:
                    logger.log_operation("vision_query", {"screenshot": screenshot["path"], "objective": vision_objective})
                    try:
                        vision_result = vision_agent(client, screenshot["path"], vision_objective)
                        logger.log_operation("vision_response", vision_result)
                        vision_results.append(vision_result)
                        if vision_result.get("found"):
                            break
                    except Exception as e:
                        logger.log_operation("vision_error", {"error": str(e)}, success=False)
                
                if vision_results:
                    logger.log_operation("language_query_with_vision", {"vision_results": vision_results})
                    language_result = language_agent(client, context.to_dict(), vision_results, reasoning_instruction)
                    logger.log_operation("language_response_with_vision", language_result)
            
            if language_result.get("needs_code"):
                instruction = language_result.get("instruction")
                context.update_script(None, instruction)
                
                logger.log_operation("script_generation_request", {"instruction": instruction})
                script = playwright_mgr.generate_script(instruction, context.to_dict())
                logger.log_operation("script_generation_response", {"script": script})
                
                compile_result = playwright_mgr.compile_code(script, instruction, context.to_dict())
                logger.log_operation("script_compilation", compile_result)
                
                if compile_result["success"]:
                    logger.log_operation("script_execution_start", {"script": compile_result["code"]})
                    exec_result = playwright_mgr.execute(compile_result["code"], vision_helpers, context, next_milestone)
                    
                    context.update_script(compile_result["code"], instruction)
                    
                    new_page_ref = exec_result.pop("_new_page_ref", None) if exec_result.get("success") else None
                    
                    logger.log_operation("script_execution_result", exec_result)
                    context.update_response(exec_result, "executed")
                    
                    if exec_result.get("switched_to_new_page") and new_page_ref:
                        try:
                            new_page = new_page_ref
                            old_url = page.url if not page.is_closed() else "unknown"
                            old_page = page  # Store reference to old page before switching
                            
                            page = new_page
                            vision_helpers.page = new_page
                            playwright_mgr.set_page(new_page)
                            
                            # Close the old aggregator tab after successful switch
                            try:
                                if not old_page.is_closed():
                                    old_page.close()
                                    print(f"🗑️  Closed old tab: {old_url}")
                            except Exception as close_error:
                                print(f"⚠️  Could not close old tab: {close_error}")
                            
                            new_url = new_page.url if not new_page.is_closed() else "unknown"
                            context.update_url(new_url)
                            logger.log_operation("tab_switched", {
                                "old_url": old_url, 
                                "new_url": new_url,
                                "old_tab_closed": True,
                                "step": step
                            })
                            print(f"🔄 Switched to new tab: {old_url} → {new_url}")
                            
                            # Close any popups on the new tab (only if not already closed by click_at_coordinates)
                            popup_already_closed = exec_result.get("popup_already_closed", False)
                            if not popup_already_closed:
                                try:
                                    vision_helpers.page = new_page
                                    vision_helpers.close_popup()
                                    logger.log_operation("popup_closed_after_tab_switch", {"url": new_url})
                                except Exception as popup_error:
                                    logger.log_operation("popup_close_error", {"error": str(popup_error)}, success=False)
                                    print(f"⚠️  Popup closing failed: {popup_error}")
                            else:
                                print(f"ℹ️  Popups already closed by click_at_coordinates(), skipping duplicate close")
                                logger.log_operation("popup_already_closed_by_click", {"url": new_url})
                        except Exception as e:
                            logger.log_operation("tab_switch_error", {"error": str(e)}, success=False)
                            print(f"⚠️  Error switching to new tab: {e}")
                    
                    try:
                        time.sleep(0.5)
                        current_url = page.url if not page.is_closed() else context.current_url or "unknown"
                        context.update_url(current_url)
                        logger.log_operation("url_updated_after_script", {"url": current_url})
                    except Exception as e:
                        logger.log_operation("url_update_error_after_script", {"error": str(e)}, success=False)
                else:
                    logger.log_operation("script_compilation_failed", compile_result, success=False)
                    context.update_response(compile_result, "compilation_failed")
            
            post_execution_screenshots = []
            if language_result.get("needs_code"):
                try:
                    post_execution_screenshots = vision_helpers.take_multifold_screenshots(f"step_{step}_post", screenshot_dir)
                    logger.log_operation("screenshot_post_execution", {"count": len(post_execution_screenshots), "paths": [s["path"] for s in post_execution_screenshots]})
                except Exception as e:
                    logger.log_operation("screenshot_post_execution_error", {"error": str(e)}, success=False)
                    try:
                        single_screenshot = vision_helpers.take_screenshot(screenshot_dir / f"step_{step}_post_fallback.png")
                        post_execution_screenshots = [{"path": str(single_screenshot), "scroll_top": 0}]
                    except:
                        pass
            
            # Take window screenshot to analyze tabs
            window_screenshot_path = None
            tab_analysis = None
            if language_result.get("needs_code"):
                try:
                    window_screenshot_path = vision_helpers.take_window_screenshot(screenshot_dir / f"step_{step}_window_post.png")
                    if window_screenshot_path:
                        logger.log_operation("window_screenshot_post_execution", {"path": window_screenshot_path})
                        tab_analysis = analyze_window_for_tabs(client, window_screenshot_path)
                        logger.log_operation("tab_analysis_post_execution", tab_analysis)
                        # Update context with tab analysis
                        context.tab_analysis = tab_analysis
                except Exception as e:
                    logger.log_operation("window_screenshot_error", {"error": str(e)}, success=False)
            
            post_execution_vision_results = []
            if post_execution_screenshots:
                post_vision_objective = """Analyze the current page state after the action was executed:
                1. IDENTIFY THE SITE: What website is this? Look for domain names, large logo text, site branding
                2. VISUAL BRANDING: Are there prominent logos, company names, or branding elements visible?
                3. CHANGES: What changed after the action? Did navigation occur? New elements appeared?
                4. ERRORS: Are any error messages or warnings visible?
                5. PAGE STATE: What interactive elements are now visible on this page?"""
                
                for screenshot in post_execution_screenshots:
                    logger.log_operation("vision_query_post_execution", {"screenshot": screenshot["path"], "objective": post_vision_objective})
                    try:
                        vision_result = vision_agent(client, screenshot["path"], post_vision_objective)
                        logger.log_operation("vision_response_post_execution", vision_result)
                        post_execution_vision_results.append(vision_result)
                        if vision_result.get("found"):
                            break
                    except Exception as e:
                        logger.log_operation("vision_error_post_execution", {"error": str(e)}, success=False)
            
            if post_execution_vision_results:
                context.vision_analysis_after_action = post_execution_vision_results
            
            try:
                current_url = page.url if not page.is_closed() else context.current_url or "unknown"
                context.update_url(current_url)
            except Exception as e:
                logger.log_operation("url_update_error", {"error": str(e)}, success=False)
                pass
            
            # Determine if milestone was actually achieved using LLM
            step_succeeded, step_reasoning = determine_step_success(
                client=client,
                milestone_goal=reasoning_result.get("next_milestone"),
                language_result=language_result,
                context=context,
                current_url=context.current_url,
                post_execution_vision_results=post_execution_vision_results
            )
            
            if step_succeeded:
                completed_milestone = reasoning_result.get("next_milestone")
                context.update_milestone(completed_milestone)
                logger.log_operation("milestone_completed", {
                    "milestone": completed_milestone,
                    "remaining": context.remaining_milestones,
                    "reasoning": step_reasoning
                })
                print(f"✅ Milestone completed: {completed_milestone}")
                
                # ═══════════════════════════════════════════════════════════════
                # CACHE SAVE - Save successful script to cache
                # ═══════════════════════════════════════════════════════════════
                if completed_milestone and context.last_tried_script:
                    milestone_cache.save_milestone(
                        carrier=carrier,
                        booking_id=booking_id,
                        milestone=completed_milestone,
                        script=context.last_tried_script,
                        operations=logger.step_operations
                    )
                    print(f"💾 Milestone cached: '{completed_milestone}'")
            else:
                logger.log_operation("milestone_not_completed", {
                    "milestone": reasoning_result.get("next_milestone"),
                    "reason": step_reasoning
                }, success=False)
                print(f"⚠️  Milestone not completed: {reasoning_result.get('next_milestone')} - will retry")
            
            data_to_extract = language_result.get("data_to_extract", [])
            if data_to_extract:
                logger.log_operation("data_extraction_attempt", {"fields": data_to_extract})
                if context.last_response_data and isinstance(context.last_response_data, dict):
                    result = context.last_response_data.get("result")
                    if result:
                        context.extracted_data.update(result)
                        
                        # ═══════════════════════════════════════════════════════════════
                        # CACHE SAVE - Save final results if both fields are extracted
                        # ═══════════════════════════════════════════════════════════════
                        if "voyage_number" in context.extracted_data and "arrival_date" in context.extracted_data:
                            # Get the extraction scripts from recent operations for re-verification
                            extraction_scripts = []
                            if context.last_tried_script:
                                extraction_scripts.append(context.last_tried_script)
                            
                            milestone_cache.save_final_results(
                                carrier=carrier,
                                booking_id=booking_id,
                                voyage_number=context.extracted_data.get("voyage_number"),
                                arrival_date=context.extracted_data.get("arrival_date"),
                                verification_scripts=extraction_scripts
                            )
                            print(f"💾 Final results cached for {carrier}:{booking_id}")
            
            try:
                step_url = page.url if not page.is_closed() else context.current_url or "unknown"
            except Exception:
                step_url = context.current_url or "unknown"
            
            pipeline_entry = logger.end_step(
                milestone=reasoning_result.get("next_milestone"),
                current_url=step_url,
                success=step_succeeded  # Use actual success status
            )
            context.add_to_history(pipeline_entry)
        
        print("🎉 Automation completed!")
        return context.extracted_data
        
    except Exception as e:
        error_str = str(e)
        print("❌ Error during automation:", error_str)
        import traceback
        try:
            logger.log_operation("automation_error", {"error": error_str, "error_type": type(e).__name__, "traceback": traceback.format_exc()}, success=False)
        except:
            pass  # Logger might not be initialized yet
        try:
            return context.extracted_data
        except:
            return {}
    finally:
        playwright.stop()

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

@app.route('/track', methods=['POST'])
def track_booking():
    try:
        data = request.json
        booking_id = data.get('booking_id')
        carrier = data.get('carrier', 'hmm')
        force_fresh = data.get('force_fresh', False)
        
        if not booking_id:
            return jsonify({"error": "booking_id is required"}), 400
        
        start_time = time.time()
        
        extracted_data = real_tracking_process(booking_id, carrier)
        
        execution_time = time.time() - start_time
        
        response = {
            "success": True,
            "booking_id": booking_id,
            "voyage_number": extracted_data.get("voyage_number"),
            "arrival_date": extracted_data.get("arrival_date"),
            "execution_time": round(execution_time, 2),
            "timestamp": datetime.now().isoformat(),
            "extracted_data": extracted_data
        }
        
        return jsonify(response)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)