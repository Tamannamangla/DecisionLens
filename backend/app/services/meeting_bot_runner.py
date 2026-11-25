import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Dict, Optional

from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import GoogleMeetBotSession
from app.services.meeting_bot import ingest_speech_chunk

logger = logging.getLogger("decisionlens.meeting_bot_runner")

_ACTIVE_BOT_TASKS: Dict[int, asyncio.Task] = {}
_ACTIVE_BOT_CONTROLLERS: Dict[int, "MeetBotController"] = {}


class MeetBotController:
    """Controls the headless Chromium bot joining and listening in Google Meet."""

    def __init__(self, session_id: int, decision_id: int, meet_url: str, bot_name: str = "DecisionLens AI Scribe"):
        self.session_id = session_id
        self.decision_id = decision_id
        self.meet_url = meet_url
        self.bot_name = bot_name
        self.is_running = True
        self.in_meeting = False
        self.browser = None
        self.context = None
        self.page = None

    async def stop(self):
        """Signal bot to leave call and clean up browser."""
        self.is_running = False
        if self.page:
            try:
                # Try clicking leave call button if present
                leave_btn = self.page.locator('button[aria-label*="Leave call" i], button[aria-label*="leave" i]')
                if await leave_btn.count() > 0:
                    await leave_btn.first.click()
                    await asyncio.sleep(1)
            except Exception:
                pass
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        logger.info("MeetBotController stopped for session=%s", self.session_id)


async def run_google_meet_bot(session_id: int, decision_id: int, meet_url: str, bot_name: str = "DecisionLens AI Scribe"):
    """
    Asynchronous worker that launches headless Chromium, enters Google Meet,
    handles the pre-join lobby, mutes audio/video, requests entry, turns on Live Captions,
    and streams live participant transcripts directly into DecisionLens.
    """
    controller = MeetBotController(session_id, decision_id, meet_url, bot_name)
    _ACTIVE_BOT_CONTROLLERS[session_id] = controller

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            import os
            profile_dir = os.path.expanduser("~/.decisionlens/bot_profile")
            os.makedirs(profile_dir, exist_ok=True)

            logger.info("Launching Chromium with persistent profile for Google Meet bot (session=%s, url=%s)", session_id, meet_url)

            # Chromium flags for WebRTC & Google Meet joining
            browser_args = [
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
            ]

            # Launch persistent context using installed official Google Chrome if available
            chrome_kwargs = {
                "user_data_dir": profile_dir,
                "headless": False,
                "args": browser_args,
                "ignore_default_args": ["--enable-automation"],
                "viewport": {"width": 1280, "height": 800},
                "permissions": ["microphone", "camera"],
            }
            try:
                controller.context = await p.chromium.launch_persistent_context(channel="chrome", **chrome_kwargs)
                logger.info("Launched persistent Google Chrome for session=%s", session_id)
            except Exception as chrome_err:
                logger.warning("Could not launch channel=chrome (%s), falling back to bundled chromium", chrome_err)
                controller.context = await p.chromium.launch_persistent_context(**chrome_kwargs)

            # Pre-grant permissions for Google Meet
            await controller.context.grant_permissions(["microphone", "camera"], origin="https://meet.google.com")

            controller.page = controller.context.pages[0] if controller.context.pages else await controller.context.new_page()

            # 1. Update session status to 'connecting'
            _update_session_status(session_id, "connecting")

            # 2. Navigate to Google Meet
            clean_url = meet_url.strip()
            if not clean_url.startswith("http"):
                clean_url = f"https://{clean_url}"

            logger.info("Opening Google Meet URL: %s", clean_url)
            await controller.page.goto(clean_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(4)

            # Check if Google Meet displays "You can't join this video call" (host management restricted)
            try:
                body_text = await controller.page.inner_text("body")
                if "You can't join this video call" in body_text or "Return to home screen" in body_text:
                    logger.warning("Google Meet blocked entry for session=%s: Google requires sign-in or call has ended.", session_id)
            except Exception:
                pass

            # Check for any dismiss/got-it popups and dismiss them
            try:
                dismiss = controller.page.locator('button[aria-label*="Dismiss" i], button:has-text("Dismiss"), button:has-text("Got it")')
                if await dismiss.count() > 0 and await dismiss.first.is_visible():
                    await dismiss.first.click()
                    await asyncio.sleep(1)
            except Exception:
                pass

            # 3. Handle pre-join lobby: Mute mic and camera
            try:
                await controller.page.keyboard.press("Control+d")
                await controller.page.keyboard.press("Control+e")
            except Exception as e:
                logger.debug("Shortcut mute attempt: %s", e)

            # 3b. Check for 'Continue as guest' screen
            try:
                guest_btn = controller.page.locator(
                    'button:has-text("Continue as guest"), span:has-text("Continue as guest"), [role="button"]:has-text("Continue as guest")'
                ).first
                if await guest_btn.count() > 0 and await guest_btn.is_visible():
                    await guest_btn.click()
                    logger.info("Clicked 'Continue as guest' for session=%s", session_id)
                    await asyncio.sleep(2)
            except Exception as e:
                logger.debug("Continue as guest attempt: %s", e)

            # 4. Fill in bot name if guest name input is present
            try:
                input_selector = 'input[aria-label*="name" i], input#i2, input[placeholder*="name" i], input#jd\\.anon_name'
                name_el = controller.page.locator(input_selector).first
                if await name_el.count() > 0:
                    try:
                        await name_el.fill(bot_name)
                    except Exception:
                        await controller.page.focus(input_selector)
                        await asyncio.sleep(0.2)
                        await controller.page.keyboard.type(bot_name, delay=30)
                    await asyncio.sleep(0.8)
                    logger.info("Typed guest bot name: %s", bot_name)
            except Exception as e:
                logger.debug("Name typing attempt: %s", e)

            # 5. Click "Ask to join" or "Join now"
            join_clicked = False
            for selector in [
                'button:has-text("Ask to join")',
                'button[jsname="Qx7uuf"]',
                'button:has-text("Join now")',
                'button:has-text("Join")',
                'span:has-text("Ask to join")',
                'span:has-text("Join now")',
            ]:
                try:
                    btn = controller.page.locator(selector)
                    if await btn.count() > 0:
                        is_dis = await btn.first.is_disabled()
                        if not is_dis:
                            await btn.first.click()
                            join_clicked = True
                            logger.info("Clicked guest join button (%s) for session=%s", selector, session_id)
                            break
                except Exception:
                    continue

            # Update status to waiting for admission
            _update_session_status(session_id, "waiting_for_admission")

            # 6. Wait for host to admit the bot into the call (up to 10 minutes)
            admitted = False
            max_wait_seconds = 600
            start_wait = asyncio.get_event_loop().time()

            while controller.is_running and (asyncio.get_event_loop().time() - start_wait < max_wait_seconds):
                # Check for in-call indicators (leave call button, meeting details, chat, participant list)
                in_call_locator = controller.page.locator(
                    'button[aria-label*="Leave call" i], button[aria-label*="leave" i], [aria-label*="Chat with everyone" i], [data-panel-id="2"]'
                )
                if await in_call_locator.count() > 0 and await in_call_locator.first.is_visible():
                    admitted = True
                    break

                # Check if denied admission
                denied_locator = controller.page.locator('text="You can\'t join this call", text="Entry denied"')
                if await denied_locator.count() > 0 and await denied_locator.first.is_visible():
                    logger.warning("Bot admission denied by host for session=%s", session_id)
                    _update_session_status(session_id, "failed_admission")
                    return

                await asyncio.sleep(2)

            if not admitted:
                logger.warning("Bot admission timed out for session=%s", session_id)
                _update_session_status(session_id, "admission_timeout")
                return

            # 7. Admitted into the call! Update status to active
            controller.in_meeting = True
            _update_session_status(session_id, "in_call_capturing")
            logger.info("Bot successfully admitted into Google Meet call (session=%s)!", session_id)

            # 8. Enable Live Closed Captions ('c' key and button fallback)
            try:
                await controller.page.keyboard.press("c")
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug("Failed pressing 'c' for captions: %s", e)

            for cap_sel in [
                'button[aria-label*="Turn on captions" i]',
                'button[aria-label*="captions" i]',
                'div[role="button"][aria-label*="Turn on captions" i]',
            ]:
                try:
                    cap_btn = controller.page.locator(cap_sel)
                    if await cap_btn.count() > 0 and await cap_btn.first.is_visible():
                        await cap_btn.first.click()
                        break
                except Exception:
                    continue

            # 9. Real-time Transcript Extraction Loop
            # Google Meet live captions reside in DOM elements (e.g. .iTTPOb, div[jsname="YSxPC"], div[aria-live="polite"])
            last_captured_text = ""

            while controller.is_running:
                # Check if call was ended or bot was removed
                leave_check = controller.page.locator('button[aria-label*="Leave call" i], [aria-label*="Chat with everyone" i]')
                if await leave_check.count() == 0:
                    # Might have been disconnected or removed
                    logger.info("Bot disconnected from Google Meet call (session=%s)", session_id)
                    break

                try:
                    # Extract any active captions from the DOM
                    captions_data = await controller.page.evaluate('''() => {
                        const results = [];
                        // Common Google Meet caption selectors
                        const containers = document.querySelectorAll('.iTTPOb, [jsname="YSxPC"], div[aria-live="polite"] > div');
                        containers.forEach(el => {
                            const text = (el.innerText || "").trim();
                            if (text) {
                                // Try finding speaker name
                                let speaker = "Participant";
                                const speakerEl = el.querySelector('.zs7Du, [class*="speaker" i], [class*="name" i]');
                                if (speakerEl) {
                                    speaker = (speakerEl.innerText || "").trim();
                                }
                                results.push({ speaker, text });
                            }
                        });
                        return results;
                    }''')

                    for item in captions_data:
                        spk = item.get("speaker", "Participant") or "Participant"
                        raw_txt = item.get("text", "").strip()
                        # Clean out speaker name if prefixed
                        if raw_txt.startswith(f"{spk}:"):
                            raw_txt = raw_txt[len(spk) + 1:].strip()

                        if raw_txt and raw_txt != last_captured_text and len(raw_txt) > 3:
                            last_captured_text = raw_txt
                            time_tag = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            _ingest_speech_to_db(session_id, raw_txt, speaker=spk, timestamp=time_tag)

                except Exception as exc:
                    logger.debug("Caption polling iteration error: %s", exc)

                await asyncio.sleep(1.5)

    except Exception as exc:
        logger.error("Error during autonomous Google Meet bot session=%s: %s", session_id, exc)
        _update_session_status(session_id, "failed")
    finally:
        await controller.stop()
        if session_id in _ACTIVE_BOT_CONTROLLERS:
            del _ACTIVE_BOT_CONTROLLERS[session_id]
        if session_id in _ACTIVE_BOT_TASKS:
            del _ACTIVE_BOT_TASKS[session_id]
        _update_session_status(session_id, "completed")


import threading


def _run_bot_thread(session_id: int, decision_id: int, meet_url: str, bot_name: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_google_meet_bot(session_id, decision_id, meet_url, bot_name))
    except Exception as exc:
        logger.error("Error in bot worker thread for session=%s: %s", session_id, exc)
    finally:
        try:
            loop.close()
        except Exception:
            pass


def start_meet_bot_background(session_id: int, decision_id: int, meet_url: str, bot_name: str = "DecisionLens AI Scribe"):
    """Spawn autonomous Playwright meeting bot in a dedicated background worker thread."""
    thread = threading.Thread(
        target=_run_bot_thread,
        args=(session_id, decision_id, meet_url, bot_name),
        daemon=True,
        name=f"MeetBotWorker-{session_id}",
    )
    thread.start()
    logger.info("Spawned background worker thread %s for session=%s", thread.name, session_id)
    return thread


async def stop_meet_bot(session_id: int):
    """Stop active meeting bot for session."""
    controller = _ACTIVE_BOT_CONTROLLERS.get(session_id)
    if controller:
        await controller.stop()
    task = _ACTIVE_BOT_TASKS.get(session_id)
    if task and not task.done():
        task.cancel()


def stop_meet_bot_sync(session_id: int):
    """Safely stop meeting bot whether called from async or sync thread."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(stop_meet_bot(session_id))
    except RuntimeError:
        try:
            asyncio.run(stop_meet_bot(session_id))
        except Exception as exc:
            logger.debug("stop_meet_bot_sync error: %s", exc)


def _update_session_status(session_id: int, new_status: str):
    """Thread-safe session status updater."""
    db: Session = SessionLocal()
    try:
        session = db.get(GoogleMeetBotSession, session_id)
        if session and session.status != "completed":
            session.status = new_status
            db.commit()
    except Exception as exc:
        logger.warning("Could not update session status=%s: %s", new_status, exc)
    finally:
        db.close()


def _ingest_speech_to_db(session_id: int, text: str, speaker: str, timestamp: str):
    """Thread-safe transcript chunk ingester."""
    db: Session = SessionLocal()
    try:
        session = db.get(GoogleMeetBotSession, session_id)
        if session:
            ingest_speech_chunk(db, session, text=text, speaker=speaker, timestamp=timestamp)
    except Exception as exc:
        logger.warning("Could not ingest transcript chunk for session=%s: %s", session_id, exc)
    finally:
        db.close()
