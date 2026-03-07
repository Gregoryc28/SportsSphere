import asyncio
import logging
import time
import base64
import json
import hmac
import hashlib
import os
from typing import Dict, List, Optional
from urllib.parse import quote, unquote, urljoin, urlparse

import requests
from quart import Quart, jsonify, send_from_directory, url_for, request, Response
from quart_cors import cors
from playwright.async_api import async_playwright

# --- NEW: Impersonation Library for the Proxy ---
from curl_cffi import requests as cffi_requests

# --- Configuration ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

app = Quart(__name__)
app = cors(app, allow_origin="*")

API_BASE = "https://streamed.pk/api"
STREAMED_HOST = "https://streamed.pk"
CACHE_TIMEOUT = 300
STREAM_CACHE_DURATION = 900
SECRET_KEY = os.environ.get("PROXY_SECRET_KEY", "change-me-to-a-real-secret")
MAX_CONCURRENT_RESOLVERS = int(os.environ.get("MAX_CONCURRENT_RESOLVERS", "2"))
STREAM_RESOLUTION_TIMEOUT = float(os.environ.get("STREAM_RESOLUTION_TIMEOUT", "45"))
PER_EMBED_TIMEOUT = float(os.environ.get("PER_EMBED_TIMEOUT", "25"))
PLAYWRIGHT_INTERACTION_ATTEMPTS = int(
    os.environ.get("PLAYWRIGHT_INTERACTION_ATTEMPTS", "7")
)
RESOLVER_DEBUG = os.environ.get("RESOLVER_DEBUG", "").lower() in {"1", "true", "yes"}
RESOLVER_DEBUG_SOURCES = {
    source.strip().lower()
    for source in os.environ.get("RESOLVER_DEBUG_SOURCES", "").split(",")
    if source.strip()
}
PLAYWRIGHT_LOCALE = os.environ.get("PLAYWRIGHT_LOCALE", "en-US")
PLAYWRIGHT_TIMEZONE = os.environ.get("PLAYWRIGHT_TIMEZONE", "America/Chicago")

# Global Caches
catalog_cache = {}
stream_cache = {}

# --- Helper Functions ---


def sign_url(url: str) -> str:
    """Generate an HMAC-SHA256 signature for a proxy URL."""
    return hmac.new(SECRET_KEY.encode(), url.encode(), hashlib.sha256).hexdigest()


def should_debug_resolver(source: str) -> bool:
    if not RESOLVER_DEBUG:
        return False

    if not RESOLVER_DEBUG_SOURCES:
        return True

    return source.lower() in RESOLVER_DEBUG_SOURCES


async def get_all_matches() -> List[Dict]:
    global catalog_cache
    current_time = time.time()

    if catalog_cache and (
        current_time - catalog_cache.get("last_updated", 0) < CACHE_TIMEOUT
    ):
        return catalog_cache["data"]

    try:
        resp = requests.get(f"{API_BASE}/matches/all-today", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        data.sort(key=lambda x: x.get("date", 0))
        catalog_cache = {"last_updated": current_time, "data": data}
        return data
    except Exception as e:
        logging.error(f"Failed to fetch matches: {e}")
        return catalog_cache.get("data", [])


def get_poster_url(match: Dict) -> str:
    poster_val = match.get("poster")
    if not poster_val:
        return None
    if poster_val.startswith("http"):
        return poster_val
    if poster_val.startswith("/"):
        return f"{STREAMED_HOST}{poster_val}.webp"
    return f"{API_BASE}/images/proxy/{poster_val}.webp"


async def get_all_stream_embeds(match_id: str) -> List[Dict]:
    matches = await get_all_matches()
    match = next((m for m in matches if m["id"] == match_id), None)

    if not match or not match.get("sources"):
        return []

    found_embeds = []

    for src in match["sources"]:
        source_name = src["source"]
        source_id = src["id"]
        try:
            api_url = f"{API_BASE}/stream/{source_name}/{source_id}"
            resp = requests.get(api_url, timeout=5)
            if resp.status_code == 200:
                streams_data = resp.json()
                for stream_obj in streams_data:
                    embed_url = stream_obj.get("embedUrl")
                    if not embed_url:
                        continue

                    quality = "HD" if stream_obj.get("hd") else "SD"
                    stream_no = stream_obj.get("streamNo", 1)
                    label = f"{source_name.title()} - Stream {stream_no} ({quality})"

                    found_embeds.append(
                        {
                            "label": label,
                            "source": source_name,
                            "embed_url": embed_url,
                            "stream_no": stream_no,
                        }
                    )
        except Exception:
            continue

    # --- Priority Sorting: fastest/most reliable sources first ---
    def _source_priority(embed_or_source):
        source = embed_or_source
        if isinstance(embed_or_source, dict):
            source = embed_or_source.get("source", "")

        source = source.lower()
        if "golf" in source:
            return 0
        if "admin" in source:
            return 1
        if "delta" in source:
            return 2
        return 3

    found_embeds.sort(key=lambda embed: (_source_priority(embed), embed["stream_no"]))

    source_buckets = {}
    for embed in found_embeds:
        source_key = embed.get("source", "").lower()
        source_buckets.setdefault(source_key, []).append(embed)

    ordered_sources = sorted(source_buckets, key=_source_priority)
    interleaved_embeds = []

    while True:
        progressed = False
        for source_key in ordered_sources:
            bucket = source_buckets.get(source_key, [])
            if bucket:
                interleaved_embeds.append(bucket.pop(0))
                progressed = True

        if not progressed:
            break

    return interleaved_embeds


async def resolve_with_playwright(
    embed_url: str, browser, source: str = "", label: str = ""
) -> Optional[Dict]:
    """Resolve an embed URL using a shared browser instance."""
    logging.info(f"Resolving Embed: {embed_url}")
    stream_info = {}
    debug_enabled = should_debug_resolver(source)
    debug_state = {
        "attempts": [],
        "console": [],
        "page_errors": [],
        "request_failures": [],
        "responses": [],
    }

    def append_debug(bucket: str, value, limit: int = 8):
        if not debug_enabled:
            return

        if len(debug_state[bucket]) < limit:
            debug_state[bucket].append(value)

    def log_debug_summary(reason: str):
        if not debug_enabled:
            return

        frame_urls = [frame.url for frame in page.frames if frame.url][:8]
        logging.info(
            "Resolver debug: source=%s label=%s reason=%s embed=%s page_url=%s frames=%s attempts=%s console=%s page_errors=%s request_failures=%s responses=%s",
            source,
            label,
            reason,
            embed_url,
            page.url,
            frame_urls,
            debug_state["attempts"],
            debug_state["console"],
            debug_state["page_errors"],
            debug_state["request_failures"],
            debug_state["responses"],
        )

    parsed_uri = urlparse(embed_url)
    clean_root = "{uri.scheme}://{uri.netloc}/".format(uri=parsed_uri)
    clean_origin = "{uri.scheme}://{uri.netloc}".format(uri=parsed_uri)

    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    context = await browser.new_context(
        user_agent=user_agent,
        viewport={"width": 1366, "height": 768},
        ignore_https_errors=True,
        locale=PLAYWRIGHT_LOCALE,
        timezone_id=PLAYWRIGHT_TIMEZONE,
    )
    await context.set_extra_http_headers(
        {
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    await context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });

        Object.defineProperty(navigator, 'platform', {
            get: () => 'Win32',
        });

        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
        });

        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => 8,
        });

        Object.defineProperty(navigator, 'deviceMemory', {
            get: () => 8,
        });

        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
        });

        window.chrome = window.chrome || {
            runtime: {},
            loadTimes: () => {},
            csi: () => {},
        };

        const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (originalQuery) {
            window.navigator.permissions.query = (parameters) => (
                parameters && parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters)
            );
        }
        """
    )
    page = await context.new_page()

    # --- Resource Blocking: Only load HTML/JS, skip everything else ---
    BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}

    async def block_unnecessary_resources(route):
        try:
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            pass

    await page.route("**/*", block_unnecessary_resources)

    async def handle_popup(popup):
        if popup != page:
            try:
                await popup.close()
            except:
                pass

    def handle_console(message):
        if message.type in {"warning", "error"}:
            append_debug(
                "console",
                {
                    "type": message.type,
                    "text": message.text,
                },
            )

    def handle_page_error(error):
        append_debug("page_errors", str(error))

    def handle_request_failed(failed_request):
        append_debug(
            "request_failures",
            {
                "resource_type": failed_request.resource_type,
                "url": failed_request.url,
                "failure": str(failed_request.failure),
            },
        )

    def handle_response(response):
        if response.request.resource_type not in {"document", "fetch", "xhr", "media"}:
            return

        response_url = response.url
        if not debug_enabled and ".m3u" not in response_url:
            return

        append_debug(
            "responses",
            {
                "resource_type": response.request.resource_type,
                "status": response.status,
                "url": response_url,
            },
        )

    context.on("page", handle_popup)
    page.on("console", handle_console)
    page.on("pageerror", handle_page_error)
    page.on("requestfailed", handle_request_failed)
    page.on("response", handle_response)

    async def handle_request(request):
        # Atomic Check: If we already have the URL, stop processing
        if "url" in stream_info:
            return

        if ".m3u" in request.url and "http" in request.url:
            if "narakathegame" in request.url:
                return

            logging.info(f"SUCCESS! Found Stream Candidate: {request.url}")
            try:
                # 1. CRITICAL: Grab headers BEFORE setting stream_info['url']
                # This prevents the browser from closing until we have the data.
                headers = await request.all_headers()
                cookies = headers.get("cookie", headers.get("Cookie", ""))

                actual_referer = headers.get("referer", embed_url)
                final_headers = {
                    "User-Agent": user_agent,
                    "Cookie": cookies,
                    "Referer": actual_referer,
                    "Origin": clean_origin,
                }

                if "strmd" in request.url or "delta" in request.url:
                    final_headers["Referer"] = clean_root
                    if "Origin" in final_headers:
                        del final_headers["Origin"]

                # 2. SAVE DATA
                stream_info["headers"] = final_headers
                stream_info["clean_root"] = clean_root

                # 3. SET URL LAST (This signals the main loop that we are done)
                stream_info["url"] = request.url

            except Exception as e:
                logging.warning(f"Could not grab headers (Retrying...): {e}")

    page.on("request", handle_request)

    try:
        await page.goto(embed_url, wait_until="domcontentloaded", timeout=25000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        for attempt in range(PLAYWRIGHT_INTERACTION_ATTEMPTS):
            # Check if we found it (headers are guaranteed to be there if URL is set)
            if "url" in stream_info:
                break

            try:
                await page.mouse.click(683, 384)
            except:
                pass
            await asyncio.sleep(1.0)

            target = page
            for frame in page.frames:
                if (
                    "embed" in frame.url
                    or "poo" in frame.url
                    or "exposestrat" in frame.url
                    or "maestro" in frame.url
                    or "player" in frame.url
                    or "watch" in frame.url
                    or "stream" in frame.url
                ):
                    target = frame

            buttons = [
                "button.vjs-big-play-button",
                ".play-button",
                "div.play",
                "svg",
                "video",
                "#player",
                ".jw-icon-playback",
                ".jwplayer",
                ".plyr__control--overlaid",
                "button[aria-label='Play']",
                "button",
            ]
            visible_buttons = []
            for btn in buttons:
                try:
                    locator = target.locator(btn)
                    if await locator.count() > 0 and await locator.first.is_visible():
                        visible_buttons.append(btn)
                        await locator.first.click(timeout=1000, force=True)
                except:
                    pass

            append_debug(
                "attempts",
                {
                    "attempt": attempt + 1,
                    "target_url": getattr(target, "url", page.url),
                    "visible_buttons": visible_buttons[:6],
                },
            )

            try:
                await target.evaluate(
                    """() => {
                        const candidates = document.querySelectorAll(
                            'video, button, .play-button, .jw-icon-playback, #player, .jwplayer, .plyr__control--overlaid'
                        );
                        for (const element of candidates) {
                            try {
                                element.dispatchEvent(new MouseEvent('click', {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window,
                                }));
                            } catch (e) {}
                        }

                        for (const video of document.querySelectorAll('video')) {
                            try {
                                video.muted = true;
                                const playPromise = video.play();
                                if (playPromise && typeof playPromise.catch === 'function') {
                                    playPromise.catch(() => {});
                                }
                            } catch (e) {}
                        }
                    }"""
                )
            except:
                pass

            await asyncio.sleep(1.5)

        if "url" not in stream_info:
            log_debug_summary("no_stream")

    except Exception as e:
        log_debug_summary(f"exception:{e}")
        logging.error(f"Playwright error: {e}")
    finally:
        await context.close()

    return stream_info


# --- THE PROXY ENGINE ---


@app.route("/proxy")
async def proxy_stream():
    target_url = request.args.get("url")
    headers_b64 = request.args.get("headers")
    sig = request.args.get("sig")

    if not target_url or not headers_b64:
        return "Missing params", 400

    # --- HMAC Signature Verification ---
    if not sig or not hmac.compare_digest(sig, sign_url(target_url)):
        logging.warning(f"Forbidden: invalid or missing signature for {target_url}")
        return "Forbidden", 403

    try:
        headers = json.loads(base64.b64decode(headers_b64).decode("utf-8"))
    except:
        return "Invalid headers", 400

    keys_to_remove = [
        "Host",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Accept-Encoding",
    ]
    for key in keys_to_remove:
        if key in headers:
            del headers[key]

    async with cffi_requests.AsyncSession(impersonate="chrome120") as session:
        try:
            resp = await session.get(target_url, headers=headers)

            if resp.status_code != 200:
                logging.error(f"Proxy failed: {resp.status_code} for {target_url}")
                return f"Proxy Error {resp.status_code}", resp.status_code

            content_type = resp.headers.get("Content-Type", "")

            if "mpegurl" in content_type or target_url.endswith(".m3u8"):
                content = resp.content
                text = content.decode("utf-8")
                new_lines = []
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("#"):
                        new_lines.append(line)
                    else:
                        absolute_link = urljoin(target_url, line)
                        encoded_headers = base64.b64encode(
                            json.dumps(headers).encode("utf-8")
                        ).decode("utf-8")
                        proxy_link = f"{url_for('proxy_stream', _external=True)}?url={quote(absolute_link)}&headers={encoded_headers}&sig={sign_url(absolute_link)}"
                        new_lines.append(proxy_link)

                final_resp = Response(
                    "\n".join(new_lines), mimetype="application/vnd.apple.mpegurl"
                )
                final_resp.headers["Cache-Control"] = (
                    "no-cache, no-store, must-revalidate"
                )
                final_resp.headers["Pragma"] = "no-cache"
                final_resp.headers["Expires"] = "0"
                return final_resp
            else:
                return Response(
                    resp.content, status=resp.status_code, mimetype=content_type
                )

        except Exception as e:
            logging.error(f"Proxy Exception: {e}")
            return str(e), 500


# --- Standard Routes ---


@app.route("/logo.png")
async def serve_logo():
    return await send_from_directory(".", "SportsSphereLogo.png")


@app.route("/manifest.json")
async def manifest():
    matches = await get_all_matches()
    available_sports = sorted(list(set(m.get("category", "Misc") for m in matches)))
    host_url = url_for("serve_logo", _external=True)

    return jsonify(
        {
            "id": "org.stremio.sportssphere",
            "version": "1.1.0",
            "name": "Sports Sphere",
            "description": "Aggregated Live Sports Events (Multi-Source)",
            "logo": host_url,
            "resources": ["catalog", "stream", "meta"],
            "types": ["movie"],
            "idPrefixes": ["pk_"],
            "catalogs": [
                {
                    "type": "movie",
                    "id": "pk_live",
                    "name": "Live Sports",
                    "extra": [{"name": "genre", "options": available_sports}],
                }
            ],
        }
    )


@app.route("/catalog/<type>/<id>.json")
@app.route("/catalog/<type>/<id>/genre=<genre>.json")
async def catalog(type, id, genre=None):
    if id != "pk_live":
        return jsonify({"metas": []})
    matches = await get_all_matches()
    metas = []
    now_ms = time.time() * 1000
    show_threshold = now_ms + 900000
    for m in matches:
        if genre and m.get("category") != genre:
            continue
        match_time = m.get("date", 0)
        if match_time > show_threshold:
            continue
        if match_time < (now_ms - 21600000):
            continue
        metas.append(
            {
                "id": f"pk_{m['id']}",
                "type": "movie",
                "name": m["title"],
                "poster": get_poster_url(m),
                "description": f"Live on Streamed.pk - {m.get('category', 'Sports')}",
                "genre": [m.get("category", "Misc")],
                "behaviorHints": {"defaultVideoId": f"pk_{m['id']}"},
            }
        )
    return jsonify({"metas": metas})


# --- WORKER ---
async def process_stream_option(embed_data, browser, semaphore):
    embed_url = embed_data["embed_url"]
    label = embed_data["label"]
    source = embed_data.get("source", "")
    queued_at = time.time()

    # If label starts with "Admin", change "Admin" text to "Alpha"
    if label.startswith("Admin"):
        label = label.replace("Admin", "Alpha")

    logging.info(
        "Resolver queued: source=%s label=%s embed=%s queued_at=%.3f",
        source,
        label,
        embed_url,
        queued_at,
    )

    try:
        async with semaphore:
            started_at = time.time()
            logging.info(
                "Resolver started: source=%s label=%s embed=%s waited=%.2fs started_at=%.3f",
                source,
                label,
                embed_url,
                started_at - queued_at,
                started_at,
            )
            data = await asyncio.wait_for(
                resolve_with_playwright(embed_url, browser, source, label),
                timeout=PER_EMBED_TIMEOUT,
            )
    except asyncio.TimeoutError:
        logging.warning(
            "Resolver timed out: source=%s label=%s embed=%s waited=%.2fs ran=%.2fs",
            source,
            label,
            embed_url,
            started_at - queued_at,
            time.time() - started_at,
        )
        return None
    except Exception as exc:
        logging.error(
            "Resolver failed: source=%s label=%s embed=%s waited=%.2fs ran=%.2fs error=%s",
            source,
            label,
            embed_url,
            started_at - queued_at,
            time.time() - started_at,
            exc,
        )
        return None

    status = "resolved" if data and "url" in data else "no_stream"
    logging.info(
        "Resolver finished: source=%s label=%s status=%s embed=%s waited=%.2fs ran=%.2fs finished_at=%.3f",
        source,
        label,
        status,
        embed_url,
        started_at - queued_at,
        time.time() - started_at,
        time.time(),
    )

    if not data or "url" not in data:
        return None

    stream_url = data["url"]
    headers = data["headers"]
    clean_root = data.get("clean_root", "")

    # Golf streams need correct exposestrat.com Referer (captured by actual_referer fix above)
    if source == "golf":
        logging.info(
            f"Golf stream direct with Referer: {headers.get('Referer', 'N/A')}"
        )
        return {
            "title": f"{label} (Direct)",
            "url": stream_url,
            "behaviorHints": {
                "notWebReady": True,
                "proxyHeaders": {"request": headers},
            },
        }

    is_strict = "strmd.top" in stream_url or "delta" in stream_url

    if is_strict:
        proxy_headers = headers.copy()
        proxy_headers["Referer"] = clean_root
        if "Origin" in proxy_headers:
            del proxy_headers["Origin"]

        headers_json = json.dumps(proxy_headers)
        headers_b64 = base64.b64encode(headers_json.encode("utf-8")).decode("utf-8")
        final_url = f"{url_for('proxy_stream', _external=True)}?url={quote(stream_url)}&headers={headers_b64}&sig={sign_url(stream_url)}"

        return {"title": f"{label} (Proxy)", "url": final_url}
    else:
        return {
            "title": f"{label} (Direct)",
            "url": stream_url,
            "behaviorHints": {
                "notWebReady": True,
                "proxyHeaders": {"request": headers},
            },
        }


@app.route("/stream/<type>/<id>.json")
async def stream(type, id):
    if not id.startswith("pk_"):
        return jsonify({"streams": []})

    real_match_id = id.replace("pk_", "")
    current_time = time.time()

    if real_match_id in stream_cache:
        cached = stream_cache[real_match_id]
        if current_time < cached["expires_at"]:
            logging.info(f"CACHE HIT: {real_match_id}")
            return jsonify({"streams": cached["streams_list"]})
        else:
            del stream_cache[real_match_id]

    embeds_list = await get_all_stream_embeds(real_match_id)
    if not embeds_list:
        return jsonify({"streams": []})

    logging.info(
        "Resolving %s options concurrently (max %s, total timeout %.1fs, per-embed timeout %.1fs)...",
        len(embeds_list),
        MAX_CONCURRENT_RESOLVERS,
        STREAM_RESOLUTION_TIMEOUT,
        PER_EMBED_TIMEOUT,
    )

    # --- Shared Browser + Concurrency Limit ---
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                channel="chrome",
                args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--mute-audio",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
        except Exception:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESOLVERS)
        tasks = [
            asyncio.create_task(process_stream_option(e, browser, semaphore))
            for e in embeds_list
        ]

        pending_tasks = set(tasks)
        results = []
        deadline = time.monotonic() + STREAM_RESOLUTION_TIMEOUT

        while pending_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logging.warning(
                    "Partial result returned due to timeout: completed=%s pending=%s budget=%.1fs",
                    len(results),
                    len(pending_tasks),
                    STREAM_RESOLUTION_TIMEOUT,
                )
                break

            done, pending_tasks = await asyncio.wait(
                pending_tasks,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                logging.warning(
                    "Partial result returned due to timeout: completed=%s pending=%s budget=%.1fs",
                    len(results),
                    len(pending_tasks),
                    STREAM_RESOLUTION_TIMEOUT,
                )
                break

            for task in done:
                try:
                    results.append(task.result())
                except Exception as exc:
                    logging.error(f"Unhandled resolver task failure: {exc}")
                    results.append(None)

        for task in pending_tasks:
            task.cancel()

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        await browser.close()

    valid_streams = [r for r in results if r]

    if not valid_streams:
        return jsonify({"streams": []})

    stream_cache[real_match_id] = {
        "streams_list": valid_streams,
        "expires_at": current_time + STREAM_CACHE_DURATION,
    }

    return jsonify({"streams": valid_streams})


@app.route("/meta/<type>/<id>.json")
async def meta(type, id):
    matches = await get_all_matches()
    real_id = id.replace("pk_", "")
    match = next((m for m in matches if m["id"] == real_id), None)
    if match:
        poster = get_poster_url(match)
        start_time = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(match.get("date") / 1000)
        )
        return jsonify(
            {
                "meta": {
                    "id": id,
                    "type": "movie",
                    "name": match["title"],
                    "poster": poster,
                    "background": poster,
                    "description": f"LIVE SPORT\nCategory: {match.get('category')}\nStart Time: {start_time}",
                }
            }
        )
    return jsonify({"meta": {}})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
