"""Login session and credential manager for Imposter5 and parent apps."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

@dataclass
class ActiveLoginSession:
    """A live backend-owned login session."""
    user_id: str
    url: str
    domain: str
    browser: Any | None  # None for persistent context
    context: Any
    page: Any
    started_at: str

# Global registry of active login sessions
_ACTIVE_SESSIONS: dict[str, ActiveLoginSession] = {}

def get_domain_clean(url: str) -> str:
    """Extract and sanitize domain name from a URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url if "://" in url else f"https://{url}")
    domain = parsed.netloc.lower() or "generic"
    if "linkedin.com" in domain:
        return "linkedin"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", domain)

def load_site_cookies(user_id: str, url: str) -> list[dict]:
    """Load cookies for a specific user and website URL."""
    domain = get_domain_clean(url)
    if domain == "linkedin":
        from imposter5.loaders.linkedin_browser import load_cookies
        return load_cookies(user_id)
    
    local_path = Path(__file__).parent.parent.parent / "cookies" / domain / f"{user_id}.json"
    if local_path.is_file():
        try:
            with open(local_path, "r") as f:
                data = json.load(f)
                logger.info("[login_manager] loaded %d cookies from local file for user %s on %s", len(data), user_id, domain)
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning("[login_manager] failed to read local cookies for %s: %s", domain, e)
            
    # Try S3 as fallback
    try:
        from imposter5.loaders.linkedin_browser import _COOKIE_BUCKET, _s3
        key = f"tokyo/user-data/prod/{domain}/cookies/{user_id}.json"
        resp = _s3().get_object(Bucket=_COOKIE_BUCKET, Key=key)
        data = json.loads(resp["Body"].read())
        logger.info("[login_manager] loaded %d cookies from S3 for user %s on %s", len(data), user_id, domain)
        
        # Cache locally
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with open(local_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.info("[login_manager] no S3 cookies for %s: %s", domain, exc)
        return []

def save_site_cookies(user_id: str, url: str, cookies: list[dict]) -> None:
    """Save cookies for a specific user and website URL."""
    domain = get_domain_clean(url)
    if domain == "linkedin":
        from imposter5.loaders.linkedin_browser import save_cookies
        save_cookies(user_id, cookies)
        return
        
    local_path = Path(__file__).parent.parent.parent / "cookies" / domain / f"{user_id}.json"
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "w") as f:
            json.dump(cookies, f, indent=2)
        logger.info("[login_manager] saved %d cookies locally for user %s on %s", len(cookies), user_id, domain)
    except Exception as exc:
        logger.error("[login_manager] failed to save cookies locally for %s: %s", domain, exc)
        
    # S3 saving
    try:
        from imposter5.loaders.linkedin_browser import _COOKIE_BUCKET, _s3
        key = f"tokyo/user-data/prod/{domain}/cookies/{user_id}.json"
        _s3().put_object(
            Bucket=_COOKIE_BUCKET,
            Key=key,
            Body=json.dumps(cookies, ensure_ascii=False).encode(),
            ContentType="application/json",
        )
        logger.info("[login_manager] saved %d cookies to S3 for user %s on %s", len(cookies), user_id, domain)
    except Exception as exc:
        logger.warning("[login_manager] S3 save bypassed or failed for %s: %s", domain, exc)

def delete_site_cookies(user_id: str, url: str) -> None:
    """Delete saved cookies for a specific user and website URL."""
    domain = get_domain_clean(url)
    local_path = Path(__file__).parent.parent.parent / "cookies" / domain / f"{user_id}.json"
    if local_path.is_file():
        try:
            local_path.unlink()
            logger.info("[login_manager] deleted local cookies for user %s on %s", user_id, domain)
        except Exception as e:
            logger.error("[login_manager] failed to delete local cookies for %s: %s", domain, e)
            
    # Try S3 deletion
    try:
        from imposter5.loaders.linkedin_browser import _COOKIE_BUCKET, _s3
        key = f"tokyo/user-data/prod/{domain}/cookies/{user_id}.json"
        _s3().delete_object(Bucket=_COOKIE_BUCKET, Key=key)
        logger.info("[login_manager] deleted S3 cookies for user %s on %s", user_id, domain)
    except Exception as exc:
        logger.warning("[login_manager] S3 delete bypassed or failed for %s: %s", domain, exc)

def is_logged_in_generic(page: Any, domain: str) -> bool:
    """Check if the page indicates a logged-in state for a generic website."""
    if domain == "linkedin":
        from imposter5.loaders.linkedin_browser import is_logged_in
        return is_logged_in(page)
        
    url = (page.url or "").lower()
    # If still on login/signin pages, we are not logged in
    if any(k in url for k in ["login", "signin", "signup", "register", "auth", "uas/login"]):
        return False
        
    # If we have some non-empty cookies, we are likely logged in
    try:
        cookies = page.context.cookies()
        return len(cookies) > 0
    except Exception:
        return False

def start_login_session(user_id: str, url: str, mode: str = "interactive") -> dict[str, Any]:
    """Start a login session for a specific user and website URL."""
    domain = get_domain_clean(url)
    key = f"{domain}:{user_id}"
    
    if key in _ACTIVE_SESSIONS:
        return {
            "success": True,
            "reused": True,
            "message": "Login browser is already open. Finish login there, then click Verify.",
            "started_at": _ACTIVE_SESSIONS[key].started_at,
        }
        
    headless = mode == "headless"
    res = {}
    
    def run():
        try:
            from imposter5.loaders.cloak_runtime import (
                launch_automation_browser,
                launch_automation_persistent_context,
                automation_connector_stealth_context_kwargs,
                apply_anti_fingerprint_init_script,
            )
            from imposter5.loaders.linkedin_browser import linkedin_profile_dir
            
            ctx_kwargs = automation_connector_stealth_context_kwargs()
            
            if domain == "linkedin":
                context = launch_automation_persistent_context(
                    linkedin_profile_dir(user_id),
                    headless=headless,
                    **ctx_kwargs,
                )
                browser = None
            else:
                from imposter5.automation_connector.browser_runner import get_browser_runner
                runner = get_browser_runner()
                browser = runner.launch_browser(headless=headless)
                context = browser.new_context(**ctx_kwargs)
                
            try:
                apply_anti_fingerprint_init_script(context)
            except Exception:
                pass
                
            context.set_default_timeout(30_000)
            
            # Load existing cookies if any
            cookies = load_site_cookies(user_id, url)
            if cookies:
                try:
                    context.add_cookies(cookies)
                except Exception as e:
                    logger.warning("[login_manager] failed to restore cookies: %s", e)
                    
            pages = getattr(context, "pages", None) or []
            page = pages[0] if pages else context.new_page()
            
            # Navigate to the target URL
            page.goto(url, wait_until="domcontentloaded")
            
            _ACTIVE_SESSIONS[key] = ActiveLoginSession(
                user_id=user_id,
                url=url,
                domain=domain,
                browser=browser,
                context=context,
                page=page,
                started_at=datetime.now().isoformat(),
            )
            res["success"] = True
            res["started_at"] = _ACTIVE_SESSIONS[key].started_at
        except Exception as e:
            res["success"] = False
            res["error"] = str(e)
            
    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    
    return res

def verify_login_session(user_id: str, url: str) -> dict[str, Any]:
    """Verify a login session and save cookies if successful."""
    domain = get_domain_clean(url)
    key = f"{domain}:{user_id}"
    
    session = _ACTIVE_SESSIONS.get(key)
    res = {}
    
    def run():
        nonlocal session
        try:
            if session is not None:
                # Active session is open: check if logged in
                verified = is_logged_in_generic(session.page, domain)
                if verified:
                    cookies = session.context.cookies()
                    if cookies:
                        save_site_cookies(user_id, url, cookies)
                    # Close and clean up
                    try:
                        session.page.close()
                    except Exception:
                        pass
                    try:
                        session.context.close()
                    except Exception:
                        pass
                    if session.browser is not None:
                        try:
                            session.browser.close()
                        except Exception:
                            pass
                    _ACTIVE_SESSIONS.pop(key, None)
                    res["success"] = True
                    res["verified"] = True
                    res["message"] = "Login verified and credentials saved successfully."
                else:
                    res["success"] = True
                    res["verified"] = False
                    res["message"] = "Session is not authenticated yet. Please complete login in the browser window."
            else:
                # No active session: do a headless check using stored cookies
                from imposter5.loaders.cloak_runtime import (
                    launch_automation_browser,
                    launch_automation_persistent_context,
                    automation_connector_stealth_context_kwargs,
                    apply_anti_fingerprint_init_script,
                )
                from imposter5.loaders.linkedin_browser import linkedin_profile_dir
                
                ctx_kwargs = automation_connector_stealth_context_kwargs()
                
                if domain == "linkedin":
                    context = launch_automation_persistent_context(
                        linkedin_profile_dir(user_id),
                        headless=True,
                        **ctx_kwargs,
                    )
                    browser = None
                else:
                    from imposter5.automation_connector.browser_runner import get_browser_runner
                    runner = get_browser_runner()
                    browser = runner.launch_browser(headless=True)
                    context = browser.new_context(**ctx_kwargs)
                    
                try:
                    apply_anti_fingerprint_init_script(context)
                except Exception:
                    pass
                    
                context.set_default_timeout(30_000)
                
                # Load existing cookies
                cookies = load_site_cookies(user_id, url)
                if cookies:
                    try:
                        context.add_cookies(cookies)
                    except Exception as e:
                        logger.warning("[login_manager] failed to restore cookies: %s", e)
                        
                pages = getattr(context, "pages", None) or []
                page = pages[0] if pages else context.new_page()
                
                page.goto(url, wait_until="domcontentloaded")
                verified = is_logged_in_generic(page, domain)
                
                # Save cookies if we verified successfully (might have updated/refreshed)
                if verified:
                    new_cookies = context.cookies()
                    if new_cookies:
                        save_site_cookies(user_id, url, new_cookies)
                        
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
                        
                res["success"] = True
                res["verified"] = verified
                res["message"] = "Credentials are valid and active." if verified else "Credentials are not valid or expired."
        except Exception as e:
            res["success"] = False
            res["error"] = str(e)
            
    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    
    return res
