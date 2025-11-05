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
        self.cache_ttl_days = 14
    
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
        
        if cache_path.exists():
            try:
                with open(cache_path, 'r') as f:
                    cache_data = json.load(f)
            except:
                cache_data = {"cached_at": datetime.now().isoformat(), "milestones": {}}
        else:
            cache_data = {"cached_at": datetime.now().isoformat(), "milestones": {}}
        
        if "milestones" not in cache_data:
            cache_data["milestones"] = {}
        
        cache_data["milestones"][milestone] = {
            "cached_at": datetime.now().isoformat(),
            "script": script,
            "operations": operations or []
        }
        
        try:
            with open(cache_path, 'w') as f:
                json.dump(cache_data, f, indent=2)
            print(f"💾 Cached milestone '{milestone}' for {carrier}:{booking_id}")
        except Exception as e:
            print(f"⚠️  Failed to save cache: {e}")
    
    def save_final_results(self, carrier, booking_id, voyage_number, arrival_date, verification_scripts=None):
        """Save final results (voyage number and arrival date) to cache."""
        cache_path = self._get_cache_path(carrier, booking_id)
        
        if cache_path.exists():
            try:
                with open(cache_path, 'r') as f:
                    cache_data = json.load(f)
            except:
                cache_data = {"cached_at": datetime.now().isoformat(), "milestones": {}}
        else:
            cache_data = {"cached_at": datetime.now().isoformat(), "milestones": {}}
        

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
            "Found booking ID input field, entered booking ID, and submitted tracking query",
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
    
    def set_goal(self, booking_id, carrier="hmm"):
        self.goal = f"Extract voyage number and arrival date for booking {booking_id}"
        self.booking_id = booking_id
        self.carrier = carrier.lower()
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
        if milestone and self.remaining_milestones:
            milestone_lower = milestone.lower()
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
        recent_step_data = {}
        if self.history:
            most_recent = self.history[-1]
            recent_step_data = {
                "last_step_milestone": most_recent.get("milestone"),
                "last_step_url": most_recent.get("current_url"),
                "last_step_success": most_recent.get("success"),
                "last_step_errors": most_recent.get("errors", [])
            }
        
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
        
        try:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(500)
            print("  ✅ Tried Escape key")
        except Exception:
            pass
        
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
                
                for popup_idx, popup in enumerate(popup_info["popups"]):
                    print(f"  📋 Popup {popup_idx + 1} (z-index {popup['zIndex']}) has {len(popup['closeButtons'])} close button candidate(s)")
                    
                    target_z_index = popup["zIndex"]
                    
                    for btn_idx, close_btn in enumerate(popup["closeButtons"]):
                        try:
                            x, y = close_btn["x"], close_btn["y"]
                            btn_text = close_btn["text"]
                            print(f"  🖱️  Attempting to click button {btn_idx + 1} at ({x}, {y}) - text: '{btn_text}'")
                            
                            self.page.mouse.click(x, y)
                            self.page.wait_for_timeout(2000)
                            
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
                            
                            if target_z_index not in remaining_popups_info:
                                print(f"  ✅ Successfully closed popup (z-index {target_z_index} disappeared)!")
                                return True
                            else:
                                print(f"  ⚠️  Popup still visible, trying next button...")
                                
                        except Exception as e:
                            print(f"  ⚠️  Error clicking button: {e}")
                            continue
                    
                    if popup_idx == 0:
                        print(f"  ⚠️  Could not close highest z-index popup, trying others...")
                        continue
                    else:
                        break
        except Exception as e:
            print(f"  ⚠️  Error in popup detection: {e}")
        
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
                if not screenshots:
                    raise
        
        try:
            self.page.evaluate("window.scrollTo(0, 0)")
            self.page.wait_for_timeout(250)
        except:
            pass
        
        if screenshots:
            return screenshots
        else:
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
            import pyautogui
            
            if scroll_into_view:
                viewport = self.page.viewport_size or {"width": 1280, "height": 720}
                current_scroll = self.page.evaluate("window.pageYOffset")
                
                if y < current_scroll or y > current_scroll + viewport["height"]:
                    scroll_to_y = max(0, y - viewport["height"] // 2)
                    self.page.evaluate(f"window.scrollTo(0, {scroll_to_y})")
                    self.page.wait_for_timeout(500)
                    
                    new_scroll = self.page.evaluate("window.pageYOffset")
                    y = y - new_scroll
            
            screen_coords = self.viewport_to_screen(x, y)
            
            print(f"🖱️  Clicking at viewport ({x}, {y}) → screen ({screen_coords['screen_x']:.0f}, {screen_coords['screen_y']:.0f})")
            
            try:
                print(f"🔍 Attempting coordinate click with new tab detection...")
                with self.page.context.expect_page(timeout=3000) as new_page_info:
                    pyautogui.click(screen_coords["screen_x"], screen_coords["screen_y"])
                
                new_page = new_page_info.value
                if new_page:
                    print(f"✅ New tab opened via coordinate click! Switching to: {new_page.url}")
                    try:
                        new_page.wait_for_load_state('domcontentloaded', timeout=5000)
                    except:
                        pass
                    
                    old_url = self.page.url if not self.page.is_closed() else "unknown"
                    self.page = new_page
                    new_page.bring_to_front()
                    
                    print(f"🔄 Switched from {old_url} → {new_page.url}")
                    
                    popup_closed = False
                    try:
                        popup_closed = self.close_popup()
                        self.popup_closed_by_click = popup_closed
                    except Exception as popup_error:
                        print(f"⚠️  Popup closing failed: {popup_error}")
                    
                    return {"success": True, "new_page": new_page, "switched": True, "popup_closed": popup_closed}
            except Exception as expect_error:
                print(f"ℹ️  No new tab detected (timeout or same-tab navigation): {type(expect_error).__name__}")
            
            print(f"🖱️  Performing normal coordinate click (same-tab navigation)...")
            pyautogui.click(screen_coords["screen_x"], screen_coords["screen_y"])
            
            try:
                self.page.wait_for_load_state('domcontentloaded', timeout=5000)
            except:
                pass
            
            return {"success": True, "new_page": None, "switched": False, "popup_closed": False}
            
        except Exception as e:
            print(f"⚠️  Coordinate click failed: {e}")
            return {"success": False, "new_page": None, "switched": False, "popup_closed": False}
    
    def click_and_type_at_coordinates(self, x, y, text, scroll_into_view=True):
        """
        Click at viewport coordinates using Playwright's mouse click, then type text.
        Uses pure Playwright methods (no pyautogui) for better focus handling.
        
        Args:
            x: X coordinate in viewport (from vision model)
            y: Y coordinate in viewport (from vision model)
            text: Text to type into the input field
            scroll_into_view: Whether to scroll the coordinates into view first
        
        Returns:
            dict with {"success": bool, "new_page": Page or None, "switched": bool}
        """
        try:
            if scroll_into_view:
                viewport = self.page.viewport_size or {"width": 1280, "height": 720}
                viewport_height = viewport["height"]
                
                current_scroll = self.page.evaluate("window.pageYOffset")
                absolute_y = current_scroll + y
                target_scroll = max(0, absolute_y - viewport_height // 2)
                
                if abs(current_scroll - target_scroll) > 10:
                    self.page.evaluate(f"window.scrollTo(0, {target_scroll})")
                    self.page.wait_for_timeout(500)
                    
                    new_scroll = self.page.evaluate("window.pageYOffset")
                    y = absolute_y - new_scroll
            
            new_page = None
            switched = False
            
            print(f"🖱️  Clicking at viewport coordinates ({x}, {y}) using Playwright mouse")
            
            try:
                with self.page.context.expect_page(timeout=3000) as new_page_info:
                    self.page.mouse.click(x, y)
                
                new_page = new_page_info.value
                if new_page:
                    print(f"✅ New tab opened! Switching to: {new_page.url}")
                    try:
                        new_page.wait_for_load_state('domcontentloaded', timeout=5000)
                    except:
                        pass
                    
                    old_url = self.page.url if not self.page.is_closed() else "unknown"
                    self.page = new_page
                    new_page.bring_to_front()
                    switched = True
                    
                    print(f"🔄 Switched from {old_url} → {new_page.url}")
                    
                    popup_closed = False
                    try:
                        popup_closed = self.close_popup()
                        self.popup_closed_by_click = popup_closed
                    except Exception as popup_error:
                        print(f"⚠️  Popup closing failed: {popup_error}")
                    
                    page_to_use = new_page
                else:
                    page_to_use = self.page
                    
            except Exception as expect_error:
                print(f"ℹ️  No new tab detected (same-tab navigation): {type(expect_error).__name__}")
                page_to_use = self.page
            
            page_to_use.wait_for_timeout(300)
            
            try:
                page_to_use.keyboard.press('Control+a')
                page_to_use.wait_for_timeout(50)
                page_to_use.keyboard.press('Delete')
                page_to_use.wait_for_timeout(50)
            except:
                try:
                    page_to_use.keyboard.press('Meta+a')
                    page_to_use.wait_for_timeout(50)
                    page_to_use.keyboard.press('Delete')
                    page_to_use.wait_for_timeout(50)
                except:
                    pass
            
            print(f"⌨️  Typing text: '{text}'")
            page_to_use.keyboard.type(text, delay=50)
            
            print(f"⏎  Pressing Enter to submit")
            page_to_use.keyboard.press('Enter')
            
            print(f"✅ Successfully entered text and submitted (Enter key)")
            
            return {
                "success": True,
                "new_page": new_page,
                "switched": switched,
                "popup_closed": self.popup_closed_by_click if switched else False
            }
            
        except Exception as e:
            print(f"⚠️  Click and type at coordinates failed: {e}")
            return {"success": False, "new_page": None, "switched": False, "popup_closed": False}
    
    def take_window_screenshot(self, path, timeout=30000):
        """Takes a screenshot of the entire browser window including chrome/tabs"""
        try:
            if not self.page or self.page.is_closed():
                return None
            
            cdp = self.page.context.new_cdp_session(self.page)
            screenshot_data = cdp.send("Page.captureScreenshot", {"captureBeyondViewport": False})
            
            import base64
            with open(path, "wb") as f:
                f.write(base64.b64decode(screenshot_data["data"]))
            
            return str(path)
        except Exception as e:
            print(f"⚠️  Window screenshot failed (trying fallback): {e}")
            try:
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
        - Example: vision_helpers.click_at_coordinates(x, y)
        - More reliable than selectors for elements in overlays, modals, or complex DOMs

        URL NAVIGATION:
        - NEVER EVER hardcode/guess carrier URLs
        - NEVER use page.goto() with any carrier domain URLs
        - To reach carrier site: MUST find and click the carrier link, never navigate directly via page.goto()
        - All navigation to carrier sites must happen by clicking visible links/elements

        HANDLING NEW TABS:
        - Carrier links often open in new tabs/pages
        - IMPORTANT: page.context.pages is a PROPERTY (not a method) - use page.context.pages NOT page.context.pages()
        
        - FOR SELECTOR-BASED CLICKS that might open new tabs (CARRIER LINKS ON AGGREGATOR SITES):
          # CRITICAL: Close popups/overlays first, then scroll element into view, verify it's visible, then click
          # This prevents clicking on ad overlays that might be covering the link
          try:
              page.keyboard.press("Escape")
              page.wait_for_timeout(300)
          except:
              pass
          old_page = page
          link_locator = page.locator('text=EXACT_TEXT_FROM_INSTRUCTION')
          link_locator.scroll_into_view_if_needed()
          page.wait_for_timeout(500)
          link_locator.wait_for(state='visible', timeout=5000)
          try:
              with page.context.expect_page(timeout=10000) as new_page_info:
                  link_locator.click()
          except Exception as e:
              try:
                  with page.context.expect_page(timeout=10000) as new_page_info:
                      link_locator.click(force=True)
              except Exception:
                  raise e
          new_page = new_page_info.value
          if new_page:
              page = new_page
              page.wait_for_load_state('domcontentloaded')
              old_page.close()
          else:
              page.wait_for_load_state('domcontentloaded')
        
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
          vision_helpers.click_and_type_at_coordinates(x, y, booking_id)
          # This method handles clicking, waiting for input to be ready, and typing directly
          # No need to call page.fill() separately - it's all handled internally
          # Example: vision_helpers.click_and_type_at_coordinates(689, 130, booking_id)
        
        - FOR SUBMITTING INPUT FIELDS:
          # DEFAULT METHOD (preferred): click_and_type_at_coordinates() automatically presses Enter after typing
          vision_helpers.click_and_type_at_coordinates(x, y, booking_id)
          # Note: Enter key is automatically pressed inside click_and_type_at_coordinates(), no need to call it separately
          page.wait_for_load_state('load')
          
          # FALLBACK METHOD (only if Enter doesn't work): Click Search button after typing
          # Only use this if Enter key submission failed (detected via post-execution vision)
          vision_helpers.click_and_type_at_coordinates(x, y, booking_id)
          # Skip Enter key (already tried) and click button instead
          vision_helpers.click_at_coordinates(button_x, button_y)
          page.wait_for_load_state('load')
          
          # Always wait for page load after submission to ensure results are loaded
        
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
            
            def get_domain(url):
                from urllib.parse import urlparse
                try:
                    return urlparse(url).netloc.lower()
                except:
                    return url.lower()
            
            new_domain = get_domain(new_url)
            prev_domain = get_domain(prev_url)
            
            print(f"🔍 Validating new tab: {new_domain}")
            
            ad_indicators = ["ads", "advert", "marketing", "promo", "offer", "survey", "feedback", "redirect", "click", "track", "doubleclick", "googleads", "googlesyndication", "mapsplatform"]
            is_ad_page = any(indicator in new_url.lower() for indicator in ad_indicators)
            
            if is_ad_page:
                print(f"❌ Ad page detected: {new_domain}")
                return False
            
            url_valid = False
            
            if new_domain == prev_domain:
                url_valid = True
                print(f"✅ Same domain as previous page")
            
            elif carrier and carrier.lower() in new_url.lower():
                url_valid = True
                print(f"✅ URL contains carrier name: {carrier}")
            
            if url_valid:
                return True
            
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
                return True
                
        except Exception as e:
            print(f"⚠️  Tab validation error: {e}, allowing tab by default")
            return True
    
    def execute(self, script, vision_helpers=None, context=None, milestone=None):
        if not self.page:
            return {"success": False, "error": "No page object available"}
        try:
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
            if context and hasattr(context, 'booking_id') and context.booking_id:
                local_vars["booking_id"] = context.booking_id
            exec(script, {}, local_vars)
            result = local_vars.get("result")
            if not isinstance(result, dict):
                result = {}
            
            new_page = local_vars.get("page")
            script_switched = new_page and new_page != self.page
            
            if vision_helpers and vision_helpers.page != self.page:
                if not script_switched:
                    new_page = vision_helpers.page
                    script_switched = True
                elif new_page != vision_helpers.page:
                    new_page = vision_helpers.page
                    script_switched = True
            
            if "click_at_coordinates" in script:
                wait_time = 2.5
            else:
                wait_time = 1.5
            
            time.sleep(wait_time)
            
            tabs_after = len(self.page.context.pages)
            auto_switched = False
            script_tab_valid = True
            
            if tabs_after > tabs_before:
                print(f"🆕 New tab detected ({tabs_before} → {tabs_after})")
                
                all_pages = self.page.context.pages
                new_tabs = all_pages[tabs_before:]
                
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
                        script_tab_valid = True
                
                if not script_switched or not script_tab_valid:
                    for new_tab in new_tabs:
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
                            
                            if vision_helpers:
                                vision_helpers.page = new_tab
                                if not vision_helpers.popup_closed_by_click:
                                    try:
                                        vision_helpers.close_popup()
                                    except Exception as popup_error:
                                        print(f"⚠️  Popup closing failed: {popup_error}")
                                else:
                                    print(f"ℹ️  Popups already closed by click_at_coordinates(), skipping duplicate close")
                            
                            break
                        else:
                            print(f"❌ Closing invalid tab: {new_tab.url}")
                            try:
                                new_tab.close()
                            except:
                                pass
                elif script_switched and script_tab_valid:
                    print(f"✅ Script switched to valid tab: {new_page.url}")
                    
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
            
            elif script_switched:
                if context and milestone:
                    try:
                        script_tab_valid = self.validate_new_tab(new_page, context, milestone)
                        if not script_tab_valid:
                            print(f"⚠️  Script switched to tab, but validation failed: {new_page.url}")
                            current_url = new_page.url if not new_page.is_closed() else "unknown"
                            print(f"⚠️  Current tab URL: {current_url}")
                    except Exception as e:
                        print(f"⚠️  Error validating script's tab: {e}")
                        script_tab_valid = True
                else:
                    script_tab_valid = True
                
                if script_tab_valid:
                    auto_switched = True
            
            try:
                url_after = self.page.url if not self.page.is_closed() else "unknown"
                if url_after != "unknown" and url_after != url_before and tabs_after == tabs_before and not script_switched:
                    print(f"🔄 Same-tab navigation detected: {url_before} → {url_after}")
            except:
                pass
            
            switched_to_new_page = script_switched or auto_switched
            
            return_dict = {
                "success": True, 
                "result": result,
                "switched_to_new_page": switched_to_new_page
            }
            
            popup_already_closed_by_click = False
            if "click_at_coordinates" in script and switched_to_new_page:
                popup_already_closed_by_click = True
            
            return_dict["popup_already_closed"] = popup_already_closed_by_click
            
            if switched_to_new_page:
                return_dict["_new_page_ref"] = new_page
            
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
    carrier = context.get("carrier", "unknown")
    current_url = context.get("current_url", "unknown")
    last_milestone = context.get("last_achieved_milestone", "None")
    next_milestone = context.get("next_milestone", "Unknown")
    remaining_count = len(context.get("remaining_milestones", []))
    
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
    - Example instruction: "Vision found input at (x, y). Use vision_helpers.click_at_coordinates(x, y)"

    HANDLING "Found booking ID input field, entered booking ID, and submitted tracking query" MILESTONE:
    - **CRITICAL**: This is a COMBINED milestone that requires ALL THREE actions to complete:
      1. FIND the booking ID input field (locate it using vision)
      2. ENTER the booking ID (type the booking ID into the input field)
      3. SUBMIT the tracking query (press Enter key OR click Search/Submit button)
    - **What this milestone means**:
      * Vision must locate the actual text input field (rectangular box for typing) - NOT buttons, NOT links, ONLY input fields
      * Code must execute to click the input field, type the booking ID value, and submit (either via Enter key or button click)
      * The submission must be completed - the page should navigate to results OR show tracking results on the same page
      * Success indicators: URL changed to results page, OR tracking results visible on page, OR booking ID appears in results/tabs
    - When setting vision_objective: "Locate ACTUAL TEXT INPUT FIELDS (rectangular boxes for typing) - NOT buttons, NOT links, ONLY input fields. Also identify the submission method (Enter key or Search/Submit button)."
    - When setting language_instruction: "Click the input field at coordinates (x, y), type the booking_id, then submit using [Enter key OR button click at coordinates]. Ensure all three actions complete: find, enter, submit."
    - This milestone is NOT complete until ALL three parts are done: finding, entering, AND submitting

    HANDLING MULTIPLE INPUT FIELDS (when next_milestone is about entering booking ID):
    - When setting vision_objective, be EXPLICIT: "Locate ACTUAL TEXT INPUT FIELDS (rectangular boxes for typing) - NOT buttons, NOT links, ONLY input fields"
    - When vision returns "input_groups" (array of input fields with submission context):
      1. Select the input with HIGHEST relevance_score (0.0-1.0)
      2. If multiple inputs have similar relevance, prioritize:
         * Input with label containing "Booking", "B/L", "BL", "Container" > generic "Search"
         * Input in top navigation bar > input in body forms
         * Input with clear submission method > ambiguous submission
      3. Use the submission.method from vision analysis:
         * "button_click": Instruct to click input, type booking ID, then click the button
         * "enter_key": Instruct to click input, type booking ID, then press Enter key
      4. Include button coordinates if method is "button_click"
      5. Example instruction format:
         - Button click: "Click input at (x, y), type booking_id, then click Search button at (x, y)"
         - Enter key: "Click input at (x, y), type booking_id, then press Enter key"
    - If vision doesn't provide input_groups but has elements, use fallback logic:
      * Look for elements with labels containing "input", "search", "booking"
      * If Search button found nearby (within 300px), use button_click method
      * Otherwise, default to enter_key method

    CRITICAL URL RESTRICTIONS - NEVER VIOLATE THESE:
    - NEVER EVER instruct direct navigation to carrier sites
    - NEVER suggest page.goto() with any carrier domain URLs in your language_instruction
    - To reach carrier site: ALWAYS instruct to "find and click the {carrier} carrier link on the aggregator site"
    - Only allow page.goto() for known aggregator sites (e.g., "navigate to seacargotracking.net" is OK)

    HANDLING "Data extracted" MILESTONE:
    - When next_milestone is "Data extracted", your job is DIFFERENT:
      * DO NOT generate code to extract data using DOM selectors
      * INSTEAD: Instruct vision agent to read voyage number and arrival date directly from the screenshots
      * Set vision_objective: "Extract the voyage number and arrival date (or ETA) from the visible tracking results on this page. Look for text that contains voyage/vessel information and arrival/ETA dates. Return the exact values as they appear on screen."
      * Set language_instruction: "Extract voyage number and arrival date from vision analysis results - no code execution needed, just read from screenshots"
      * The vision agent will return the data in its response, and the system will parse it automatically
      * This is more reliable than DOM selectors which often fail due to complex page structures

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
    
    start = result.find("{")
    end = result.rfind("}") + 1
    if start != -1 and end > start:
        result = result[start:end]
    
    return json.loads(result)


def vision_agent(client, screenshot_path, objective):
    with open(screenshot_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    
    is_input_field_analysis = any(keyword in objective.lower() for keyword in [
        "input field", "text input", "booking id input", "b/l input", "container input",
        "enter booking", "enter the id", "input for booking"
    ])
    
    if is_input_field_analysis:
        system_prompt = f"""You are a vision analysis agent specialized in analyzing input fields and their submission mechanisms.

    CURRENT OBJECTIVE:
    {objective}

    CRITICAL REQUIREMENTS - READ CAREFULLY:
    - You MUST identify ACTUAL TEXT INPUT FIELDS (rectangular boxes where users type text)
    - DO NOT return buttons (Login, Search, Submit, etc.) - these are NOT input fields
    - DO NOT return links, dropdowns, or clickable elements - ONLY text input boxes
    - Input fields are typically: white/gray rectangular boxes with borders, often with placeholder text or labels
    - Buttons are typically: colored/shaded elements with text labels, often rounded or styled differently
    
    YOUR JOB:
    - Find ALL ACTUAL TEXT INPUT FIELDS (not buttons, not links, not dropdowns) that could potentially accept booking IDs, B/L numbers, container numbers, or tracking references
    - Visually identify rectangular input boxes where text can be typed
    - For EACH input field found, analyze its SUBMISSION CONTEXT:
      * Check if there's a Search/Submit/Go/Find button nearby (within 300 pixels horizontally)
      * Determine if the input is part of a form (multiple fields) or standalone
      * Assess visual layout: is button to the right, below, or integrated?
      * Note if input appears in top navigation bar (often accepts Enter key)
      * Note if input is in a dedicated search section (may have button)
    
    EXCLUSION LIST - DO NOT RETURN THESE:
    - Login buttons, Search buttons, Submit buttons, Navigation buttons
    - Links (text or image links)
    - Dropdown menus or select elements
    - Icons, logos, or images
    - Menu items or navigation elements
    - Any element that is NOT a text input field (rectangular box for typing)
    
    SPATIAL ANALYSIS:
    - Calculate distance between input field and any nearby buttons
    - Buttons within 300px horizontally are considered "nearby"
    - IMPORTANT: Even if a button is nearby, Enter key is ALWAYS tried first (it works on most forms)
    - Button click is only used as a fallback if Enter key doesn't work
    - Most booking/tracking input fields accept Enter key for submission, even when a button is visible
    
    RELEVANCE SCORING:
    - Inputs with labels/placeholders containing "Booking", "B/L", "BL", "Container", "CNTR", "Tracking", "Reference" → higher relevance
    - Inputs in top navigation/search bars → medium-high relevance
    - Inputs in schedule/search forms → medium relevance
    - Inputs in unrelated sections (contact forms, etc.) → lower relevance
    - ONLY score actual input fields - exclude buttons and other elements from scoring

    OUTPUT FORMAT (use this EXACT structure):
    {{
    "found": boolean,
    "input_groups": [
        {{
            "input": {{
                "label": "descriptive label of input field (MUST be an actual input box, NOT a button)",
                "x": x_coordinate,
                "y": y_coordinate,
                "confidence": 0.0-1.0
            }},
            "submission": {{
                "method": "enter_key" | "button_click",
                "button": {{
                    "label": "button text/label",
                    "x": x_coordinate,
                    "y": y_coordinate,
                    "confidence": 0.0-1.0
                }} | null,
                "button_distance": distance_in_pixels | null,
                "reasoning": "explanation of why this submission method was chosen. NOTE: Always prefer 'enter_key' as the default - it works on most forms. Only use 'button_click' if Enter key is explicitly known to not work."
            }},
            "relevance_score": 0.0-1.0,
            "relevance_reasoning": "why this input is relevant for booking ID entry"
        }}
    ],
    "elements": [{{"label": "name", "x": coord, "y": coord, "confidence": 0.0-1.0}}],  // Keep for backward compatibility
    "notes": "overall observations about input fields and submission mechanisms found"
    }}
    
    VALIDATION CHECKLIST BEFORE RETURNING:
    - For each input_group: Verify the "input" element is actually a rectangular text input box (not a button)
    - Double-check coordinates point to an input field (white/gray box with border), not a button (colored element)
    - If unsure whether something is an input or button, do NOT include it - only return elements you're CERTAIN are input fields
    - If you find 0 input fields, return {{"found": false, "input_groups": [], "notes": "No text input fields found for booking ID entry"}}"""
    else:
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

    vision_model = "gpt-4o" if is_input_field_analysis else "gpt-4.1-mini"

    response = client.chat.completions.create(
        model=vision_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}}
            ]}
        ],
        temperature=0.0  # Lower temperature for more deterministic results
    )
    
    result = response.choices[0].message.content.strip()
    
    start = result.find("{")
    end = result.rfind("}") + 1
    if start != -1 and end > start:
        result = result[start:end]
    
    vision_result = json.loads(result)
    
    if is_input_field_analysis and vision_result.get("input_groups"):
        vision_result = validate_input_fields_from_vision(vision_result, screenshot_path)
    
    return vision_result


def adjust_vision_coordinates_for_scroll(vision_result, scroll_top):
    """
    Adjust vision result coordinates by adding scroll_top offset to y coordinates.
    Vision model returns coordinates relative to the screenshot image, which may start
    at scroll_top > 0. We need to add scroll_top to get absolute page coordinates.
    
    Args:
        vision_result: Vision result dict with elements/input_groups
        scroll_top: Scroll offset where screenshot was taken
        
    Returns:
        Vision result with adjusted coordinates
    """
    if not scroll_top or scroll_top == 0:
        return vision_result
    
    if vision_result.get("elements"):
        for elem in vision_result["elements"]:
            if "y" in elem:
                elem["y"] = elem["y"] + scroll_top
    
    if vision_result.get("input_groups"):
        for group in vision_result["input_groups"]:
            if group.get("input") and "y" in group["input"]:
                group["input"]["y"] = group["input"]["y"] + scroll_top
            
            if group.get("submission") and group["submission"].get("button") and "y" in group["submission"]["button"]:
                group["submission"]["button"]["y"] = group["submission"]["button"]["y"] + scroll_top
    
    if vision_result.get("_original_input_groups"):
        for group in vision_result["_original_input_groups"]:
            if group.get("input") and "y" in group["input"]:
                group["input"]["y"] = group["input"]["y"] + scroll_top
            if group.get("submission") and group["submission"].get("button") and "y" in group["submission"]["button"]:
                group["submission"]["button"]["y"] = group["submission"]["button"]["y"] + scroll_top
    
    return vision_result


def validate_input_fields_from_vision(vision_result, screenshot_path=None):
    """
    Validate that vision-identified elements are actually input fields by checking DOM.
    Filters out hallucinations (buttons, links, etc.) and ensures we only return real input fields.
    """
    if not vision_result.get("input_groups"):
        return vision_result
    
    validated_groups = []
    
    for group in vision_result.get("input_groups", []):
        input_info = group.get("input", {})
        label = input_info.get("label", "").lower()
        
        exclude_keywords = [
            "button", "login", "submit", "search button", "link", "icon", 
            "logo", "menu", "dropdown", "select", "navigation"
        ]
        
        if any(keyword in label for keyword in exclude_keywords):
            continue
        
        include_keywords = [
            "input", "field", "text", "search", "booking", "b/l", "bl", 
            "container", "cntr", "tracking", "reference"
        ]
        
        has_include_keyword = any(keyword in label for keyword in include_keywords)
        is_top_nav = input_info.get("y", 9999) < 100
        
        if has_include_keyword or is_top_nav:
            group["_needs_dom_validation"] = True
            validated_groups.append(group)
    
    original_count = len(vision_result.get("input_groups", []))
    vision_result["input_groups"] = validated_groups
    vision_result["found"] = len(validated_groups) > 0
    
    if len(validated_groups) < original_count:
        vision_result["notes"] = (vision_result.get("notes", "") + 
            f" [Filtered: Removed {original_count - len(validated_groups)} non-input elements]")
    
    return vision_result


def validate_input_fields_against_dom(page, vision_result):
    """
    Use Playwright to verify that coordinates from vision actually point to input fields.
    This is called after vision returns results, when we have page access.
    Improved to account for scroll position and provide fallback for high-confidence vision results.
    """
    if not vision_result.get("input_groups"):
        return vision_result
    
    validated_groups = []
    
    for group in vision_result.get("input_groups", []):
        input_info = group.get("input", {})
        x, y = input_info.get("x"), input_info.get("y")
        confidence = input_info.get("confidence", 0.0)
        label = input_info.get("label", "").lower()
        relevance_score = group.get("relevance_score", 0.0)
        
        try:
            scroll_info = page.evaluate("""() => ({
                scrollX: window.pageXOffset || window.scrollX || 0,
                scrollY: window.pageYOffset || window.scrollY || 0,
                viewportWidth: window.innerWidth,
                viewportHeight: window.innerHeight
            })""")
            
            element_info = page.evaluate(f"""
                () => {{
                    // Check primary coordinate and nearby offsets (handles slight coordinate inaccuracies)
                    // Wider search area for better matching
                    const offsets = [
                        [0, 0], [5, 0], [-5, 0], [0, 5], [0, -5], 
                        [5, 5], [-5, -5], [10, 0], [-10, 0], [0, 10], [0, -10],
                        [10, 5], [-10, 5], [5, 10], [-5, 10]
                    ];
                    let bestMatch = null;
                    
                    for (const [dx, dy] of offsets) {{
                        const el = document.elementFromPoint({x} + dx, {y} + dy);
                        if (!el) continue;
                        
                        const tagName = el.tagName.toLowerCase();
                        const isInput = tagName === 'input' && 
                            (el.type === 'text' || el.type === 'search' || el.type === '' || !el.type || el.type === 'tel' || el.type === 'url');
                        const isTextarea = tagName === 'textarea';
                        const hasContentEditable = el.contentEditable === 'true';
                        
                        if (isInput || isTextarea || hasContentEditable) {{
                            bestMatch = {{
                                isInput: true,
                                tagName: tagName,
                                type: el.type || null,
                                id: el.id || null,
                                name: el.name || null,
                                placeholder: el.placeholder || null,
                                offsetX: dx,
                                offsetY: dy
                            }};
                            break;
                        }}
                    }}
                    
                    // If no input found at primary or nearby coordinates, return what's at primary coordinate
                    if (!bestMatch) {{
                        const el = document.elementFromPoint({x}, {y});
                        return {{
                            isInput: false,
                            tagName: el ? el.tagName.toLowerCase() : null,
                            type: el ? (el.type || null) : null
                        }};
                    }}
                    
                    return bestMatch;
                }}
            """)
            
            if element_info.get("isInput"):
                group["_dom_validated"] = True
                group["_dom_info"] = element_info
                if element_info.get("offsetX") or element_info.get("offsetY"):
                    input_info["x"] = x + element_info.get("offsetX", 0)
                    input_info["y"] = y + element_info.get("offsetY", 0)
                    print(f"✅ Adjusted coordinates by ({element_info.get('offsetX', 0)}, {element_info.get('offsetY', 0)}) for better input field alignment")
                validated_groups.append(group)
            else:
                input_keywords = ["input", "field", "booking", "b/l", "bl", "container", "cntr", "search", "text"]
                has_input_keyword = any(keyword in label for keyword in input_keywords)
                
                if confidence >= 0.9 and relevance_score >= 0.8 and has_input_keyword:
                    print(f"⚠️  DOM validation failed for ({x}, {y}) but keeping due to high vision confidence ({confidence:.2f}) and relevance ({relevance_score:.2f})")
                    group["_dom_validated"] = False
                    group["_dom_validation_failed"] = True
                    group["_fallback_used"] = True
                    validated_groups.append(group)
                else:
                    print(f"⚠️  Vision coordinate ({x}, {y}) points to {element_info.get('tagName')}, not an input field - filtering out")
                    continue
                
        except Exception as e:
            print(f"⚠️  DOM validation failed for ({x}, {y}): {e}")
            group["_dom_validated"] = False
            group["_dom_validation_error"] = str(e)
            if confidence >= 0.9:
                group["_fallback_used"] = True
                print(f"✅ Keeping input despite validation error due to high vision confidence ({confidence:.2f})")
            validated_groups.append(group)
    
    original_count = len(vision_result.get("input_groups", []))
    vision_result["input_groups"] = validated_groups
    vision_result["found"] = len(validated_groups) > 0
    
    if len(validated_groups) < original_count:
        print(f"✅ DOM validation: Kept {len(validated_groups)} valid input fields, filtered out {original_count - len(validated_groups)} non-input elements")
    
    return vision_result


def language_agent(client, context, vision_info, reasoning_instruction=None):
    next_milestone = context.get("next_milestone", "Unknown")
    current_url = context.get("current_url", "unknown")
    carrier = context.get("carrier", "unknown")
    
    is_carrier_site = current_url and "seacargotracking" not in current_url and "google.com" not in current_url
    site_type = "CARRIER SITE" if is_carrier_site else "AGGREGATOR/OTHER"
    
    carrier_link_text = None
    if vision_info and not is_carrier_site:
        for vision_result in vision_info:
            if isinstance(vision_result, dict):
                elements = vision_result.get("elements", [])
                for element in elements:
                    label = element.get("label", "")
                    label_lower = label.lower()
                    carrier_lower = carrier.lower()
                    if carrier_lower in label_lower:
                        if "merchant" in label_lower or "marine" in label_lower or carrier.upper() in label:
                            carrier_link_text = label
                            break
                        elif len(label) > 3 and carrier_lower in label_lower:
                            carrier_link_text = label
                            break
                if carrier_link_text:
                    break
    
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
    - **If milestone is "Data extracted" → YOU MUST set needs_code=false** - data extraction uses vision to read from screenshots, NO code execution needed
    - **If reasoning agent says "navigate to X" → YOU MUST set needs_code=true and generate navigation instruction**
    - **If reasoning agent says "click Y" → YOU MUST set needs_code=true and generate click instruction**
    - **If reasoning agent says "enter booking ID" or "type booking ID" or milestone is "Found booking ID input field, entered booking ID, and submitted tracking query" → YOU MUST set needs_code=true and generate input instruction, EVEN IF vision validation failed or input_groups is empty**
    - **If milestone is "Found booking ID input field, entered booking ID, and submitted tracking query" → YOU MUST set needs_code=true**
      * This milestone requires ALL THREE actions: find input field, enter booking ID, AND submit query
      * Code must execute to: click input field, type booking_id, and submit (Enter key OR button click)
      * The instruction must ensure all three parts complete - do NOT generate code that only finds or only enters without submitting
    - DO NOT say "no code needed" when reasoning agent explicitly requests an action, especially booking ID entry
    - If vision found input_groups but DOM validation filtered them out, STILL generate code if reasoning agent says to enter booking ID (use the coordinates from vision anyway)
    - Only say "no code needed" if goal is achieved or milestone is "Data extracted" or reasoning agent explicitly asks for more analysis first

    CARRIER LINK CLICKING (AGGREGATOR SITES):
    - **CRITICAL**: On aggregator sites (seacargotracking.net), when vision finds a carrier link with visible text:
      * Check vision_info for elements with labels containing the carrier name
      * Extract the EXACT text label from vision results (the label field from the element)
      * ALWAYS: 1) Close popups/overlays first (try Escape key), 2) Use locator with scroll_into_view_if_needed() to ensure element is visible, 3) Wait 500ms, 4) Click with expect_page()
      * This prevents clicking on ad overlays that might cover the link
      * Use: Try Escape key, then page.locator('text=EXACT_TEXT').scroll_into_view_if_needed(), wait 500ms, then click with expect_page()
      * Replace EXACT_TEXT with the actual text label found in vision results
      * Example instruction: "Close any popups (try Escape key), then use page.locator('text=EXACT_CARRIER_LINK_TEXT').scroll_into_view_if_needed(), wait 500ms, then click with expect_page() to handle new tab"
      * This is MORE RELIABLE than coordinates because text is deterministic and scrolling ensures visibility
      * Text selectors work even if page layout changes slightly
      * Only fall back to coordinates if text selector fails after retries
    - When vision provides both text label AND coordinates → ALWAYS prefer text selector with popup closing and scrolling on aggregator sites
    
    COORDINATE-BASED CLICKING (PRIORITIZE ON CARRIER SITES):
    - On carrier sites (URLs NOT containing 'seacargotracking'): ALWAYS use coordinate clicking when vision provides coordinates
    - On aggregator sites: Use text selectors first (when text is available), then coordinates as fallback
    - If reasoning agent provides coordinates (x, y) → instruct: "Use vision_helpers.click_at_coordinates(x, y)"
    - Vision coordinates with pyautogui are MORE RELIABLE than selectors on carrier sites
    - Avoids issues with overlays, modals, strict mode violations, and complex DOMs

    HANDLING INPUT FIELDS WITH SUBMISSION METHODS:
    - **DEFAULT BEHAVIOR: Enter key is ALWAYS tried first** - click_and_type_at_coordinates() automatically presses Enter after typing
    - When vision provides "input_groups" (structured input field data):
      1. Use the input coordinates from vision analysis (not hardcoded)
      2. **PREFER Enter key method** - click_and_type_at_coordinates() already includes Enter key press
      3. Generate instruction: "Use vision_helpers.click_and_type_at_coordinates(x, y, booking_id)" - this will automatically click, type, and press Enter
      4. Only use button_click as a fallback if Enter key doesn't work (detected via post-execution vision analysis)
      5. Use booking_id from context (DO NOT hardcode the ID value)
      6. Example (default/preferred): "vision_helpers.click_and_type_at_coordinates(x, y, booking_id)" - Enter is automatic
      7. Example (fallback only): "vision_helpers.click_and_type_at_coordinates(x, y, booking_id); vision_helpers.click_at_coordinates(button_x, button_y)" - only if Enter failed
    - **FALLBACK WHEN DOM VALIDATION FAILED**: If vision found input_groups but they were filtered out by DOM validation, AND reasoning agent says to enter booking ID:
      * Check if vision_results contain "_original_input_groups" field (this means DOM validation filtered them out but original vision data exists)
      * If "_original_input_groups" exists, use those coordinates: Extract input coordinates from _original_input_groups and use "vision_helpers.click_and_type_at_coordinates(x, y, booking_id)"
      * Also check if vision_results contain any elements with coordinates (even if input_groups is empty)
      * If found, use those coordinates with booking_id: "vision_helpers.click_and_type_at_coordinates(x, y, booking_id)"
      * If no coordinates available, instruct to use vision again to locate input field
      * IMPORTANT: When using _original_input_groups, still generate code - DOM validation failure doesn't mean the coordinates are wrong, just that we couldn't verify them
    - When reasoning agent mentions submission method explicitly, use those coordinates and method
    - NEVER hardcode coordinates or booking IDs - always use values from vision/context

    RESTRICTIONS:
    - NEVER EVER instruct to hardcode/guess carrier URLs
    - NEVER instruct page.goto() with carrier domains - only allow for known aggregator sites
    - To reach carrier site: MUST instruct to find and CLICK the {carrier} carrier link, never navigate directly
    - If navigation needed and no link visible → instruct to navigate to known aggregator site (e.g., seacargotracking.net)
    - Request SYNCHRONOUS Playwright code only

    REMINDER FOR THIS STEP:
    - Focus ONLY on: "{next_milestone}"
    - On {site_type}: {"PRIORITIZE coordinate clicking" if is_carrier_site else "Use selectors first, coordinates as fallback"}

    HANDLING "Data extracted" MILESTONE:
    - When milestone is "Data extracted":
      * Set needs_code=false (no code execution - we read from screenshots)
      * Set needs_vision=true (vision will read the data from screenshots)
      * Set instruction: "Extract voyage number and arrival date from vision analysis results"
      * Set data_to_extract: ["voyage_number", "arrival_date"]
      * The system will automatically extract these values from vision results after vision analysis completes

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
    - **ALWAYS set needs_vision=true for "Data extracted" milestone** (to read data from screenshots)
    - Set needs_vision=false if you already have enough information (e.g., navigation to known URL)"""
    
    vision_summary_str = json.dumps(vision_info, indent=2)
    if carrier_link_text and not is_carrier_site:
        vision_summary_str += f"\n\nEXTRACTED CARRIER LINK TEXT FROM VISION: '{carrier_link_text}'"
        vision_summary_str += f"\nINSTRUCTION: Use this exact text in a Playwright text selector. Generate instruction like: 'Use page.click(\\'text={carrier_link_text}\\')' or 'Use page.locator(\\'text={carrier_link_text}\\').click()'"
    
    prompt = f"""Context:
            {json.dumps(context, indent=2)}

            Vision Analysis:
            {vision_summary_str}

            Based on the reasoning agent's instruction above, decide if code is needed and provide instruction for playwright manager.
            {"IMPORTANT: Vision found carrier link with text: '" + carrier_link_text + "'. Generate instruction that: 1) Closes popups first (try Escape key), 2) Uses this exact text in a Playwright locator with scroll_into_view_if_needed() (e.g., 'Close popups (try Escape), then use page.locator(\\'text=" + carrier_link_text + "\\').scroll_into_view_if_needed(), wait 500ms, then click with expect_page()') instead of coordinates on aggregator sites. This ensures the link is visible and not covered by ads." if carrier_link_text and not is_carrier_site else ""}"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    )
    
    result = response.choices[0].message.content.strip()
    
    start = result.find("{")
    end = result.rfind("}") + 1
    if start != -1 and end > start:
        result = result[start:end]
    
    return json.loads(result)


def extract_data_from_vision_results(client, vision_results):
    """
    Extract voyage number and arrival date from vision analysis results using GPT.
    This analyzes the vision notes and elements to find the exact values.
    """
    if not vision_results or len(vision_results) == 0:
        return {"voyage_number": "", "arrival_date": ""}
    
    # Combine all vision data into a single text for analysis
    vision_summary = []
    for vision_result in vision_results:
        notes = vision_result.get("notes", "")
        elements = vision_result.get("elements", [])
        
        vision_summary.append(f"Notes: {notes}")
        
        if elements:
            element_labels = [elem.get("label", "") for elem in elements[:10]]  # Limit to first 10 elements
            vision_summary.append(f"Elements: {', '.join(element_labels)}")
    
    combined_text = "\n".join(vision_summary)
    
    system_prompt = """You are a data extraction specialist. Your job is to extract voyage number and arrival date from vision analysis results of shipping tracking pages.

CRITICAL REQUIREMENTS:
1. VOYAGE NUMBER: Extract the exact voyage number/vessel name as it appears
   - Look for patterns like: "Voyage Number: X", "Voyage: X", "Vessel: X", "Vessel Name: X"
   - Voyage numbers typically contain letters and numbers
   - Return the EXACT value as shown, including spaces and formatting

2. ARRIVAL DATE: Extract the exact arrival date or ETA as it appears at the FINAL DESTINATION
   - Look for patterns like: "Arrival Date: X", "Arrival: X", "ETA: X", "Estimated Arrival: X"
   - Dates can be in various formats: "2025-02-28", "28/02/2025", "Feb 28, 2025", etc.
   - Return the EXACT value as shown (don't normalize the format)

3. If you cannot find a value, return empty string "" for that field
4. Be precise - only extract values that are clearly visible in the vision analysis

Return ONLY valid JSON with this exact structure:
{
    "voyage_number": "exact value or empty string",
    "arrival_date": "exact value or empty string"
}"""

    prompt = f"""Analyze the following vision analysis results from a shipping tracking page and extract the voyage number and arrival date.

Vision Analysis Results:
{combined_text}

Extract:
1. Voyage number/vessel name (if visible)
2. Arrival date or ETA (if visible)

Return JSON with voyage_number and arrival_date fields."""

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
        
        result = response.choices[0].message.content.strip()
        
        start = result.find("{")
        end = result.rfind("}") + 1
        if start != -1 and end > start:
            result = result[start:end]
        
        extracted = json.loads(result)
        
        # Ensure we have the required fields
        return {
            "voyage_number": extracted.get("voyage_number", "").strip(),
            "arrival_date": extracted.get("arrival_date", "").strip()
        }
    except Exception as e:
        print(f"⚠️  Error extracting data from vision results: {e}")
        return {"voyage_number": "", "arrival_date": ""}


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
    if "Found booking ID input field, entered booking ID, and submitted tracking query" in milestone_goal:
        booking_id = (context.booking_id or '').upper() if hasattr(context, 'booking_id') else ''
        execution_success = context.last_response_data.get("success", False) if context.last_response_data else False
        
        if not language_result.get("needs_code"):
            return False, "Combined milestone requires code execution - needs_code was false. All three actions (find, enter, submit) require code execution."
        
        if not execution_success:
            return False, "Code execution failed - combined milestone requires successful execution of all three actions (find input field, enter booking ID, submit query)"
        
        if post_execution_vision_results and len(post_execution_vision_results) > 0:
            vision_data = post_execution_vision_results[0]
            vision_notes = vision_data.get('notes', '').lower()
            vision_elements = vision_data.get('elements', [])
            
            has_booking_id_in_notes = booking_id.lower() in vision_notes if booking_id else False
            has_booking_id_in_elements = any(
                booking_id.lower() in str(elem.get('label', '')).lower() 
                for elem in vision_elements
            ) if booking_id else False
            
            has_tracking_results = any(
                keyword in vision_notes 
                for keyword in ['tracking result', 'track & trace', 'b/l no.', 'booking no.', 'route', 'voyage', 'arrival', 'vessel', 'eta', 'discharge']
            )
            url_is_tracking_page = any(
                keyword in current_url.lower() 
                for keyword in ['track', 'trace', 'result', 'search', 'query']
            )
            url_changed = context.current_url != (context.last_response_data.get('url_before', '') if context.last_response_data else '')
            
            if has_booking_id_in_notes or has_booking_id_in_elements or has_tracking_results or url_is_tracking_page or url_changed:
                evidence_parts = []
                if has_booking_id_in_notes or has_booking_id_in_elements:
                    evidence_parts.append("booking ID found in page")
                if has_tracking_results:
                    evidence_parts.append("tracking results visible")
                if url_is_tracking_page or url_changed:
                    evidence_parts.append("URL indicates submission")
                
                return True, f"Combined milestone SUCCESS: All three actions completed - {'; '.join(evidence_parts)}"
            else:
                return False, "Combined milestone INCOMPLETE: Code executed but no evidence of submission. Missing indicators: booking ID in page, tracking results, or URL change to results page."
        else:
            if execution_success:
                url_changed = context.current_url != (context.last_response_data.get('url_before', '') if context.last_response_data else '')
                if url_changed or any(keyword in current_url.lower() for keyword in ['track', 'trace', 'result']):
                    return True, "Combined milestone SUCCESS: Code executed and URL changed to results page"
                else:
                    return False, "Combined milestone INCOMPLETE: Code executed but no URL change or vision data to verify submission"
            else:
                return False, "Combined milestone FAILED: Code execution failed"
    
    if not language_result.get("needs_code"):
        if any(action in milestone_goal.lower() for action in ["enter", "type", "submit", "click", "navigate"]):
            return False, f"Milestone '{milestone_goal}' requires code execution but needs_code was false"
        return True, "No code execution needed"
    
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
    - **If milestone says "Found booking ID input field, entered booking ID, and submitted tracking query" → Check STRINGENTLY - This is a COMBINED milestone requiring ALL THREE actions:**
      **CRITICAL**: This milestone requires ALL THREE parts to complete:
      1. FIND: Input field was located (vision found it OR code executed successfully)
      2. ENTER: Booking ID was typed into the input field (code executed + evidence of entry)
      3. SUBMIT: Query was submitted (Enter key pressed OR button clicked, AND page changed OR results visible)
      
      **SUCCESS CRITERIA** (at least ONE must be true):
      a) Code was executed successfully (execution_success = true)
      b) AND one of these indicators:
         * Booking ID appears in page (in input field, results, tabs, or B/L displays)
         * Tracking results visible on page (voyage, vessel, arrival, ETA, route, etc.)
         * URL changed to results/tracking page (navigation after submission)
         * URL contains tracking-related keywords (track, trace, result, search, query)
      
      **FAILURE CRITERIA**:
      - If no code was executed → FAILURE (all three actions require code)
      - If code executed but NO evidence of submission (no results, no URL change, no booking ID in page) → FAILURE
      - If code executed but only input field found (no entry or submission) → FAILURE
      - If code executed but only entry happened (no submission) → FAILURE
      
      **SUCCESS = All three parts completed**: Finding input field, entering booking ID, AND submitting query
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
    
    print("⏳ Waiting for browser to be ready...")
    time.sleep(10)
    
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
        print("✅ Connected to existing browser session")
        
        contexts = browser.contexts
        page = None
        
        if contexts and contexts[0].pages:
            page = contexts[0].pages[0]
            print(f"📄 Using existing page: {page.url}")
        else:
            try:
                if contexts:
                    page = contexts[0].new_page()
                else:
                    new_context = browser.new_context(
                        viewport={"width": 1280, "height": 720},
                        ignore_https_errors=True
                    )
                    page = new_context.new_page()
                print(f"📄 Created new page (no existing pages found)")
            except Exception as page_create_error:
                print(f"⚠️  Failed to create new page: {page_create_error}")
                if contexts:
                    for ctx in contexts:
                        if ctx.pages:
                            page = ctx.pages[0]
                            print(f"📄 Using fallback page from context: {page.url}")
                            break
                
                if not page:
                    raise Exception(f"Could not create or find a page: {page_create_error}")

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
                        cached_script = cached_milestone.get("script")
                        logger.log_operation("cache_execution_start", {"script": cached_script})
                        print(f"⚡ Executing cached script for '{next_milestone}'")
                        
                        exec_result = playwright_mgr.execute(cached_script, vision_helpers, context, next_milestone)
                        
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
                        
                        try:
                            time.sleep(0.5)
                            current_url = page.url if not page.is_closed() else context.current_url or "unknown"
                            context.update_url(current_url)
                            logger.log_operation("url_updated_after_cache", {"url": current_url})
                        except Exception as e:
                            logger.log_operation("url_update_error_after_cache", {"error": str(e)}, success=False)
                        
                        if exec_result.get("success"):
                            print(f"🔍 Validating cached milestone completion: '{next_milestone}'")
                            
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
                        page.goto(recovery_url, timeout=30000, wait_until='load')
                        time.sleep(1)
                        
                        current_url = page.url
                        context.update_url(current_url)
                        
                        if reset_milestone:
                            all_milestones = DOMAIN_KNOWLEDGE["shipping_tracking_workflow"]["milestones"]
                            formatted_milestones = [m.format(carrier=context.carrier.upper()) for m in all_milestones]
                            
                            try:
                                reset_idx = formatted_milestones.index(reset_milestone)
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
            else:
                reasoning_result = {}
            
            if reasoning_result.get("goal_achieved"):
                logger.log_operation("goal_achieved", context.extracted_data)
                logger.end_step(
                    milestone=reasoning_result.get("next_milestone"),
                    current_url=context.current_url,
                    success=True
                )
                break
            
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
                        
                        scroll_top = screenshot.get("scroll_top", 0)
                        if scroll_top > 0:
                            vision_result = adjust_vision_coordinates_for_scroll(vision_result, scroll_top)
                            print(f"📍 Adjusted vision coordinates by scroll_top={scroll_top}px")
                        
                        logger.log_operation("vision_response", vision_result)
                        
                        original_vision_result = vision_result.copy() if vision_result.get("input_groups") else None
                        
                        if vision_result.get("input_groups") and page:
                            try:
                                vision_result = validate_input_fields_against_dom(page, vision_result)
                                if len(vision_result.get("input_groups", [])) == 0 and original_vision_result and len(original_vision_result.get("input_groups", [])) > 0:
                                    vision_result["_original_input_groups"] = original_vision_result.get("input_groups")
                                    vision_result["_dom_validation_filtered_all"] = True
                                    print(f"⚠️  DOM validation filtered out all inputs, preserving original vision data as fallback")
                                logger.log_operation("vision_dom_validation", {
                                    "validated_count": len(vision_result.get("input_groups", [])),
                                    "original_count": len(original_vision_result.get("input_groups", [])) if original_vision_result else 0,
                                    "validation_success": True
                                })
                            except Exception as dom_error:
                                logger.log_operation("vision_dom_validation_error", {"error": str(dom_error)}, success=False)
                                print(f"⚠️  DOM validation error: {dom_error}")
                                if original_vision_result:
                                    vision_result["_original_input_groups"] = original_vision_result.get("input_groups")
                        
                        vision_results.append(vision_result)
                        if vision_result.get("found") or vision_result.get("_dom_validation_filtered_all"):
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
                            logger.log_operation("tab_switched", {
                                "old_url": old_url, 
                                "new_url": new_url,
                                "old_tab_closed": True,
                                "step": step
                            })
                            print(f"🔄 Switched to new tab: {old_url} → {new_url}")
                            
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
                         
                        scroll_top = screenshot.get("scroll_top", 0)
                        if scroll_top > 0:
                            vision_result = adjust_vision_coordinates_for_scroll(vision_result, scroll_top)
                         
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
                
                if "voyage_number" in data_to_extract or "arrival_date" in data_to_extract:
                    if post_execution_vision_results and len(post_execution_vision_results) > 0:
                        extracted_from_vision = extract_data_from_vision_results(client, post_execution_vision_results)
                        if extracted_from_vision:
                            context.extracted_data.update(extracted_from_vision)
                            logger.log_operation("data_extracted_from_vision", extracted_from_vision)
                            print(f"📊 Extracted from vision: Voyage={extracted_from_vision.get('voyage_number', '')}, Arrival={extracted_from_vision.get('arrival_date', '')}")
                
                if context.last_response_data and isinstance(context.last_response_data, dict):
                    result = context.last_response_data.get("result")
                    if result:
                        context.extracted_data.update(result)
                        
            
            try:
                step_url = page.url if not page.is_closed() else context.current_url or "unknown"
            except Exception:
                step_url = context.current_url or "unknown"
            
            pipeline_entry = logger.end_step(
                milestone=reasoning_result.get("next_milestone"),
                current_url=step_url,
                success=step_succeeded
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
            pass
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