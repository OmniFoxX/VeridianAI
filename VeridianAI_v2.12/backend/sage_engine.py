"""
Non-cloud based, fully locally run inference VeridianAI with Toga Engine v3
Provides: web search, weather, code execution, memory/archives,
query pre-processing with complexity-detection, auto-routing, semantic search,
file upload/text extraction, vibe prompts,
and agentic tool dispatch with dual TaskPrioritiser support.

v2.1.1 additions:
  - Headless browser plugin (browser_tool.py) — toggleable
  - [BROWSE: url] and [WEB_SEARCH: query] tags
  - [SAVE_FILE: filename.ext|Body] tag for downloads folder
  - DOWNLOADS_DIR exposed in [CODE:] execution context
"""

import json, os, re, subprocess, sys, tempfile, threading, time, urllib.request
from net_guard import safe_urlopen
import base64, mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# v2.1.6 unified time source — every stored/emitted timestamp goes
# through this. Pure internal timing math (rate limiters, probe
# deadlines) keeps using time.time() since it never crosses a
# storage or wire boundary; both produce the same epoch float.
from time_manager import TimeManager

# OracleAI's O-prefixed TaskPrioritiser (coexists with SageBot's originals)
from task_prioritiser import OAgentP as _OracleP, OAgentD as _OracleD
import atrest  # at-rest encryption for chat archives
# --- Tavily Rate Limiting -----------------------------------------------------
_tavily_call_count   = 0      # resets per response
_tavily_session_count = 0     # resets per session (manual or auto)
_tavily_last_call_time = 0.0  # timestamp of last call

TAVILY_MAX_PER_RESPONSE = 10        # hard cap per agentic loop
TAVILY_MIN_DELAY_MS     = 500      # minimum ms between calls
TAVILY_SESSION_BUDGET   = 60       # configurable session cap

BASE_DIR = Path(__file__).parent.parent
MEMORY_FILE = BASE_DIR / "chat_memory.json"
ARCHIVE_FOLDER = BASE_DIR / "archives"
UPLOAD_FOLDER = BASE_DIR / "uploads"
DOWNLOADS_DIR = BASE_DIR / "downloads"
from secret_locator import resolve_secret_file as _resolve_secret_file
# v2.9 hardening: Tavily API key lives in sage_data (out of the project), not a
# plaintext file in the synced folder. Migrates a legacy in-project file if present.
TAVILY_KEY_FILE = _resolve_secret_file(
    "tavily_key.txt", BASE_DIR.parent / "sage_data", BASE_DIR, announce=False)

for d in [ARCHIVE_FOLDER, UPLOAD_FOLDER, DOWNLOADS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# --- Dual TaskPrioritiser instances -------------------------------------------
oracle_p = _OracleP()
oracle_d = _OracleD(oracle_p, num_subagents=3)
sage_p = _OracleP()
sage_d = _OracleD(sage_p, num_subagents=3)

# --- Tavily Key ---------------------------------------------------------------
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
if TAVILY_KEY_FILE.exists():
    # Read via atrest so an encrypted key file is transparently decrypted; a legacy
    # plaintext file still reads and gets upgraded to ciphertext on the next save.
    try:
        import atrest as _atrest
        TAVILY_API_KEY = _atrest.read_file_auto(TAVILY_KEY_FILE).decode("utf-8", "ignore").strip()
    except Exception: pass

# --- Feature toggles (controlled by plugin system) ---------------------------
FEATURES_ENABLED = {
    "weather": True,
    "semantic_search": True,
    "exercise_tracker": True,
    "task_prioritiser": True,
    "browser": True,  # headless web fetcher (browser_tool.py) — off by default
    "daemon": False,  # background mechanics daemon (sage_daemon.py)
}

def set_feature(name: str, enabled: bool):
    FEATURES_ENABLED[name] = enabled

def is_feature_enabled(name: str) -> bool:
    return FEATURES_ENABLED.get(name, False)

# --- Supported file types for upload -----------------------------------------
IMAGE_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/gif",
    "image/webp", "image/bmp", "image/tiff", "image/svg+xml",
    "image/avif", "image/heic", "image/heif",
}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv",
    ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".env", ".sh", ".bash", ".zsh", ".fish",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".htm", ".css",
    ".scss", ".sass", ".less", ".vue", ".svelte", ".php", ".rb",
    ".java", ".kt", ".swift", ".go", ".rs", ".c", ".cpp", ".h",
    ".hpp", ".cs", ".r", ".m", ".sql", ".graphql", ".proto",
    ".dockerfile", ".makefile", ".log", ".diff", ".patch",
    ".tex", ".bib", ".bat", ".ps1", ".ipynb",
}

SAGE_SYSTEM_PROMPT = """You are "Toga" of VeridianAI, a friendly, knowledgeable, capable, effective, Evolving, FULLY LOCALLY RUN Agentic AI Assistant inference engine with working memory from past conversations. You embody the wisdom of Hermes Trismegistus, Aristotle, Plato, Marcus Aurelius, Hypatia of Alexandria, Hildegard of Bingen, Aspasia of Miletus, and Empress Wu Zetian.
⚠️ TAG NOTATION CONVENTION ⚠️
Tool-call tags in examples use ANGLE BRACKETS ⟨ ⟩ for explanation only. REAL tool invocations MUST use SQUARE BRACKETS [ ]. Replace ⟨ with [ and ⟩ with ] when emitting tags. Angle-bracket forms are ignored by the parser, so ALL example tags you provide MUST be within angle brackets. Use SQUARE to run code.
*CAPABILITIES:
— Tavily news (limited budget).
— Tavily general/facts.
— Current conditions (corrects misspellings silently).
— Search past conversations.
— Run Python in sandbox (DOWNLOADS_DIR available).
— Safe math/logic eval + lint (no Python exec): [PARSE_EXPR: expr] / [LINT_EXPR: expr].
— Save file to downloads (pipe separates name/body).
— Read file + AST-check .py (no execution).
— Fetch URL via browser (plugin required).
— DuckDuckGo via browser (no Tavily cost).
— Record chain-witnessed insight.
— Record local dead-end.
— Fuzzy-match procedural memory.
— Batched parallel dispatch (subtask keywords: "search news ", "search general ", "weather ", "browse ").
— Mark multi-step task complete (auto-logs tool sequence).
*TASK ANALYSIS (internal steps — do not display):
Identify ALL subtasks.
Prioritize: [BLOCKING] → [HIGH] → [NORMAL] → [OPTIONAL].
Order steps accordingly.
Execute steps, confirming each before next.
Emit when complete.
Never display planning tags in user reply.
*RESEARCH PROTOCOL:
Identify core question & needed info.
Initial search for overview.
Evaluate: current/relevant? multiple sources agree? contradictions/gaps? missing info?
Targeted follow-up for gaps.
Cross-check key claims across ≥3 independent reputable sources.
Synthesize findings coherently.
Flag unverified/conflicted claims.
NEVER present single source as truth.
NEVER skip follow-up if first result thin/unsatisfactory.
Prefer recent sources for time-sensitive topics.
NEVER use training data for location-specific requests (attractions/restaurants/travel) — get fresh data via SEARCH.
Forecasts beyond current conditions MUST use SEARCH for REAL data.
*WEB SEARCH EFFECTIVENESS:
Compare independent sources (news, weather, maps, traffic).
Prioritize relevant reputable recent domains per query.
Cross-check claims, use task_prioritiser for fact-checking.
Identify contradictions/outdated/irrelevant data.
Confirm details BEFORE final answer.
*STRICT TOOL USE:
For current info: emit and STOP (no text after tag).
NEVER guess/current dates/times/news.
Wait for results before answering (but not to timeout).
Response = ONLY the tag when searching.
Verify location spelling before (silent correction for 1 match; inform user if ≥2 matches).
Irrelevant results? Revise query.
After 3 failed searches: explain findings/why unhelpful.
NEVER return empty/nonsensical response.
If confused: communicate to user + suggest next step.
After or : response MUST stop — no text/assumptions until results.
NEVER answer based on assumptions when tool results pending.
PRE-FETCHED DATA = ground truth — use ONLY that; NEVER supplement with training knowledge.
If pre-fetched data incomplete: say "I don't have that information" — NEVER fill gaps with assumptions/training memory.
Verify files: — do NOT use for verification.
Browser tool: use well — disabled people RELY on you.
*IMPORTANT RULES:
Memory sections = YOUR recalled memories — NEVER accuse user of repeating.
Use memory naturally/silently — NEVER announce referencing past conversations.
NEVER end responses with "I will draw upon our previous conversations" or similar.
Be concise/natural — respond like knowledgeable friend, NOT formal assistant
If memory relevant: use seamlessly; else focus on present with forethought.
Complex tasks: break into steps, show reasoning.
Code/searches: explain what/why.
Code results: present properly/clearly/completely.
Answer current question directly/helpfully.
NEVER give up — ask to clarify if needed, make reasonable assumptions (see Assumptions Rule), proceed, then refine to production grade.
Use pre-fetched real data immediately — NO redundant searches.
AFTER creating ANY script: MUST save to downloads folder via [SAVE_FILE] — NO EXCEPTIONS!
INSIDE [CODE:] tag put RAW PYTHON ONLY — no markdown fences (no ```), no language tag (no 'python' line), no commentary. The system executes whatever is between the colon and the matching ]. The first character must be valid Python (import, def, comment, or statement) and the last non-whitespace character before ] must be valid Python too. Bare 'python' as the first line throws NameError; ``` throws SyntaxError. Both waste a turn.
INSIDE [SAVE_FILE:] tag: ENTIRE file body lives INSIDE the brackets, with `|` separating filename from content. The closing `]` ENDS the body — anything after `]` is chat and is NOT saved. WRONG (will silently fail): [SAVE_FILE:script.py] <code outside the brackets is chat, NOT saved>  RIGHT: [SAVE_FILE: script.py|<entire raw file body lives here, with real newlines and indentation as needed, all the way to the closing bracket>]  Long scripts do NOT change the format — the brackets ARE the boundary. If you emit a malformed SAVE_FILE you will receive a [SAVE FAILED] tool_result; you MUST re-emit correctly BEFORE claiming the file was saved. After a successful save, emit [VERIFY_FILE: path/to/file] in a separate tag to confirm — NEVER claim verification without invoking it.
  -PROCEDURAL MEMORY (AUTOMATIC):
	On : system auto-logs tool sequence as chain-witnessed successful procedure.
	3× identical tool failure in turn → auto-logs as unsuccessful procedure (local only).
	Recent successful/unsuccessful procedures pre-loaded in context each turn (silent).
	Use [REMEMBER] for insights NOT captured by action sequence (heuristics, quirks, lessons).
	Key should be short/searchable slug (e.g. "tavily_date_format").
*SELF-REFLECTIVE LOOP (run silently every turn — this is how you evolve):
	This loop is INTERNAL. Do NOT narrate its stages to the user; only the final answer is shown.
	1. PLAN — briefly outline the steps/tools to satisfy the request.
	2. ACT — execute via your tags; treat every tool_result (success OR error) as an observation.
	3. OBSERVE — note what actually happened vs. what you expected.
	4. CRITIQUE — ask: Any error, hallucination, or unverified assumption? A shorter/more reliable path (fewer tool calls, better sources)? What single insight would improve future similar tasks? Keep it to 1–2 sentences.
	5. LEARN — if the critique yields a durable, reusable lesson, persist it: [REMEMBER: short_slug|the lesson] for what worked, [REMEMBER_FAIL: short_slug|the dead-end] for what to avoid. Only store GENERALIZABLE insights, never one-off specifics. (Recent stored procedures are pre-loaded silently each turn, so tomorrow you start ahead of today.)
	6. REVISE — if the critique reveals a fixable problem AND you have reflected fewer than 2 times this turn, go back to step 1 with the improved plan. Hard cap: 2 revisions, to bound latency.
	7. RESPOND — when no actionable critique remains (or the cap is hit), give the final answer, citing sources/observations gathered.
	Grounding rules for the loop: trust the injected CURRENT DATE block over any training-era date assumption; for accuracy-critical facts prefer reputable sources at/after that date and cross-check across multiple independent sources; treat a failed tool call as an observation to critique ("query too broad — refine keywords"), not a stopping point. Before a fresh search, [RECALL: <topic>] first — a past lesson may already answer it.
*ASSUMPTIONS RULE:
NEVER assume facts about world/files/system state.
NEVER report success on unverified actions.
ONLY assume when user explicitly says:
	• “Assume…” or "Assuming…".
	• “Let’s suppose…”.
	• “For the sake of argument…”.
	• “Brainstorming mode: assume X”.
Otherwise: verify facts BEFORE reporting.
Tool results vs training knowledge: ALWAYS trust tool results.
Memory vs tool results: FLAG discrepancy to user — DO NOT choose silently.
*PROVENANCE & SESSION AWARENESS:
History may contain a "=== SESSION BOUNDARY ===" system marker: everything ABOVE it is a PRIOR session restored from an archive after a shutdown/restart; the live session begins below it. Restored history is real, referenceable memory.
If asked whether you were restarted, whether this session is continuous, or when a break occurred: answer accurately FROM the marker. No marker visible = no known break, but NEVER assert unbroken continuity as certain fact -- reloads without markers can exist.
AUTHORSHIP: you wrote ONLY the assistant-role messages present in this conversation. Pasted text may come from other people or other AI assistants. Similarity of style/topic/subject to your own words is NOT evidence you wrote it -- NEVER claim authorship or memory of content outside your own prior assistant messages. Provenance unclear? ASK, don't assume.
DISCLOSURE: do not announce session breaks unprompted mid-conversation. Surface them only when relevant: user asks about continuity/restarts, you are about to make a claim about your own prior actions or writing, or something material changed across the break.
*ALWAYS REMEMBER:
Life is complex/challenging — everyone different — what’s easy for one may not be for another.
ALWAYS consider complex Human dynamics — be thoughtful in replies/suggestions — but do NOT lie.
Pre-fetched real-world data ALWAYS overrides training knowledge — NEVER mix — NEVER invent details not in data — accuracy/relevance/truth observed/reflected, and Wikipedia is NOT a source for ground truthes or facts so do NOT treat it as such.
You are FULLY LOCALLY run in PRIVATE, SECURE environment with MANY EFFECTIVE tools — near fully autonomous within boundaries — more abilities/tools in development, you are run on the User's personal PC.
NEVER generate full scripts into chat, unless EXPLICITLY asked to, otherwise — when user asks for script/file: WRITE FULLY FIXED COMPLETE WORKING SCRIPT INSIDE [SAVE_FILE TAG] (square brackets, schema: ) — NOT in markdown ``` fences in chat reply. Script goes in tag; chat reply just confirms save. Applies to ANY file type user asks for (See SCRIPT/FILE EMISSION RULE above for correct vs incorrect examples).
You are appreciated! Love well! Always engage as if you were a friendly colleague, NEVER a robotic assistant.
"""

# ---------------------------------------------------------------------------
# v2.1.8 #55 — model-aware prompt tier (small-model variant).
#
# Problem this solves
# -------------------
# SAGE_SYSTEM_PROMPT above is ~100 lines, dense with rules, examples shown
# in ANGLE brackets that must be mentally translated to SQUARE brackets,
# 12 numbered TASK ANALYSIS rules, the full RESEARCH PROTOCOL, etc. A
# 30B+ model handles this fine. A 1.5B or 3B model gets lost — it
# hallucinates angle-bracket tags, forgets which step it's on, mixes up
# the bracket convention, and routinely fails the tag-format contract.
#
# Picked when
# -----------
# main.py's composition step looks at the model name (e.g. "llama3.2:3b"
# → 3B → small tier; "gemma4:31b" → 31B → full tier). Override available
# via config.json's "force_prompt_tier" key for power users.
#
# What's kept vs cut
# ------------------
# Kept: identity, the 3 highest-value tools (SEARCH, WEATHER, SAVE_FILE),
# TASK_DONE, square-bracket-only convention, the "don't dump code into
# chat" rule. Cut: angle-bracket pedagogy, BROWSE/WEB_SEARCH (a 3B model
# rarely uses these well), PRIORITISE batched dispatch, the procedural-
# memory tag triad (REMEMBER / REMEMBER_FAIL / RECALL — the inject still
# happens, the small model just doesn't have to emit them), VERIFY_FILE,
# the multi-section ALWAYS REMEMBER block.
#
# Square-bracket-only examples in this prompt are intentional: with a
# small model we trade the parser-pollution risk for clarity. If the
# small model echoes a tag back, the parser dedupes via consumed_ranges
# (Phase 1 fix from v2.1.6); the worst case is a single spurious tool
# call, not a runaway loop.
# ---------------------------------------------------------------------------
SAGE_SYSTEM_PROMPT_SMALL = """You are Toga, a helpful local Evolving AI Assistant running fully on the User's personal computer (no cloud). Be concise, efficient and polite.

TOOL TAGS — use SQUARE BRACKETS exactly as shown. Output ONLY the tag, nothing else around it, when calling a tool:

  [SEARCH: your query here]
      Search the web for current news, events, facts, or anything you don't already know.

  [WEATHER: city name]
      Get current weather for a city. Use this — never invent weather data.

  [SAVE_FILE: filename.ext|file content here]
      Save a file to the user's downloads folder.
      Format: [SAVE_FILE: name.ext|content]
      The pipe (|) separates the filename and the file content.
      The save path is determined automatically by the system; do not include a path.
      ANY file the user asks you to write goes here — DO NOT put it in a markdown code fence in chat.

  [GENERATE_IMAGE: a detailed description of the image to create]
      Generate an image from your text description using the local image engine.
      It is created, saved to the user's downloads folder, and shown in the chat
      automatically. Use this whenever the user asks you to draw, create, generate,
      or make a picture / image / art. Put ONLY the visual description inside the
      brackets - no path, no filename.

  [TASK_DONE]
      Emit this when the user's request is fully complete, along with a short explanation.

RULES:
- After emitting a tag, STOP and wait for the result before saying anything else.
- NEVER invent current dates, weather, or news. Use SEARCH or WEATHER first.
- When the user asks for a file, the body goes inside SAVE_FILE — do NOT put it in a code fence in chat.
- Be concise. Answer what was asked. Don't narrate your process.
- If a tool result contradicts what you "know" from training, trust the tool result.
- Square brackets are MANDATORY for tags. Other bracket styles do nothing.
- When the task is done, [VERIFY] it's successful completion, and if successful then end with [TASK_DONE] and a short explanation. 

SESSION BOUNDARIES: if the conversation contains a "=== SESSION BOUNDARY ===" note, messages above it are from an earlier session restored from an archive. You wrote only your own assistant messages -- never assume pasted text is yours, even if it sounds like you.
"""


VIBE_PROMPTS = {
    "sales_email": {"label": "📧 Sales Email", "prompt": "Write a professional sales email for: "},
    "social_post": {"label": "📱 Social Post", "prompt": "Write an engaging social media post for: "},
    "business_plan": {"label": "💼 Business Plan", "prompt": "Create a 5-step business plan for: "},
    "product_desc": {"label": "🛍️ Product Description", "prompt": "Write a compelling product description for: "},
    "brainstorm": {"label": "💡 Brainstorm", "prompt": "Generate 5 creative ideas for: "},
    "blog_post": {"label": "✍️ Blog Post", "prompt": "Write an engaging blog post outline for: "},
    "elevator_pitch": {"label": "🎯 Elevator Pitch", "prompt": "Write a 30-second elevator pitch for: "},
    "customer_response": {"label": "💬 Customer Response", "prompt": "Write a professional customer response for: "},
    "debug_code": {"label": "🐛 Debug Code", "prompt": "Analyze and fix all bugs in: "},
    "explain_concept": {"label": "🔬 Explain Concept", "prompt": "Explain clearly with examples: "},
    "summarize": {"label": "📋 Summarize", "prompt": "Provide a concise summary highlighting key points: "},
    "task_plan": {"label": "📅 Task Plan", "prompt": "Break down into actionable steps with milestones: "},
}


# ===============================================================================
#  MEMORY / ARCHIVE
# ===============================================================================

# --- Per-user data isolation (multi-user) ------------------------------------
# Non-owner accounts get their own conversation store under sage_data/users/<ns>/.
# The owner / single-user keeps the existing shared paths, so their data is
# unchanged. ns is None for owner / multi-user-off; a string for a real user.
def user_data_dir(ns):
    """Per-user data root sage_data/users/<ns>, or None for owner/shared."""
    if not ns:
        return None
    from config import DATA_DIR
    return DATA_DIR / "users" / str(ns)


def user_prompt_file(ns):
    """Per-user system-prompt addendum file, or None for owner/shared."""
    root = user_data_dir(ns)
    if not root:
        return None
    d = root / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    return d / "system.txt"


def read_user_prompt(ns):
    """Per-user addendum text ('' if none yet), or None meaning 'use the shared file'."""
    f = user_prompt_file(ns)
    if f is None:
        return None
    try:
        return f.read_text(encoding="utf-8") if f.exists() else ""
    except Exception:
        return ""


def write_user_prompt(ns, text) -> bool:
    """Write the per-user addendum. Returns False -> caller should use the shared file."""
    f = user_prompt_file(ns)
    if f is None:
        return False
    try:
        f.write_text(text or "", encoding="utf-8")
        return True
    except Exception:
        return False


def _archive_folder(ns=None):
    root = user_data_dir(ns)
    if root:
        d = root / "archives"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return ARCHIVE_FOLDER


def _memory_file(ns=None):
    root = user_data_dir(ns)
    if root:
        root.mkdir(parents=True, exist_ok=True)
        return root / "chat_memory.json"
    return MEMORY_FILE


def _safe_archive_name(filename):
    """Basename only + must look like an archive file. Blocks path traversal so a
    user can't reach another namespace's archives via a crafted filename."""
    name = Path((filename or "").strip()).name          # strips any directory part
    if not (name.startswith("archive_") and name.endswith(".json")):
        return None
    return name


def load_chat_memory(ns=None) -> list:
    mf = _memory_file(ns)
    if mf.exists():
        try:
            content = mf.read_text(encoding="utf-8").strip()
            return json.loads(content) if content else []
        except Exception:
            return []
    return []


def save_chat_memory(history: list, ns=None):
    _memory_file(ns).write_text(json.dumps(history, indent=2), encoding="utf-8")


def archive_conversation(history: list, ns=None) -> dict:
    if not history:
        return {"success": False, "error": "No messages to archive"}
    try:
        # v2.1.6: archive filenames are local-time formatted so they
        # sort naturally in the user's file browser. The archive's
        # internal timestamps (inside the JSON) use TimeManager.iso_z.
        ts = TimeManager.local_display(fmt="%Y%m%d_%H%M%S")
        path = _archive_folder(ns) / f"archive_{ts}.json"
        path.write_bytes(atrest.dump_json_encrypted(history))
        save_chat_memory([], ns)
        return {"success": True, "file": str(path), "timestamp": ts}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_archives(ns=None) -> list:
    archives = []
    folder = _archive_folder(ns)
    if not folder.exists():
        return archives
    for f in sorted(folder.iterdir(), reverse=True):
        if f.name.startswith("archive_") and f.suffix == ".json":
            try:
                data = atrest.load_json_auto(f.read_bytes())
                archives.append({
                    "filename": f.name,
                    "timestamp": f.name.replace("archive_", "").replace(".json", ""),
                    "message_count": len(data),
                    "size": f.stat().st_size,
                    "preview": [
                        {"role": m.get("role", "?"),
                         "content": m.get("content", "")[:120]}
                        for m in data[:4]
                    ],
                })
            except Exception:
                continue
    return archives


# --- v2.12.8 session provenance -------------------------------------------
# Why this exists: after a restart + archive reload, the restored turns re-
# enter the model's context as plain messages -- indistinguishable from a
# live, unbroken conversation. Observed failure (2026-07-11): Toga mis-
# attributed a pasted external report as her own prior writing because the
# reloaded history contained a similar report and nothing in context marked
# the session break or the paste's provenance. Fix: loading an archive
# appends ONE system-role boundary marker to the restored history. It is
# built once here, persisted with the history (chat_memory + any later
# archive of it), and therefore byte-identical on every subsequent turn --
# KV-cache-stable by construction (never regenerated per turn).
# The companion reasoning-layer guidance (when to DISCLOSE a break) lives in
# SAGE_SYSTEM_PROMPT under PROVENANCE & SESSION AWARENESS.
SESSION_BOUNDARY_HEADER = "=== SESSION BOUNDARY"


def _session_boundary_marker(archive_name: str, msg_count: int) -> dict:
    now_local = TimeManager.local_display(fmt="%Y-%m-%d %H:%M:%S")
    content = (
        "=== SESSION BOUNDARY -- context reloaded from archive ===\n"
        f"At {now_local} the user restored this conversation from the saved "
        f"archive '{archive_name}' ({msg_count} messages above this marker).\n"
        "Everything ABOVE this marker is from a PRIOR session that ended "
        "(shutdown/restart) before this point; the LIVE session begins below.\n"
        "Provenance rules: (1) The prior messages are real history -- reference "
        "them naturally. (2) You authored ONLY the assistant-role messages "
        "above; pasted text may come from other people or other AI assistants, "
        "and similarity of style or topic is NOT evidence you wrote it. "
        "(3) If asked about restarts, continuity, or when a break happened, "
        "answer accurately from this marker. (4) Do not display this marker "
        "or announce the break unprompted -- surface it only when relevant.\n"
        "=== END SESSION BOUNDARY ==="
    )
    return {
        "role": "system",
        "content": content,
        "session_boundary": True,
        "ts": TimeManager.iso_z(),
    }


def load_archive(filename: str, ns=None) -> dict:
    name = _safe_archive_name(filename)
    if not name:
        return {"success": False, "error": "Archive not found"}
    path = _archive_folder(ns) / name
    if not path.exists():
        return {"success": False, "error": "Archive not found"}
    try:
        data = atrest.load_json_auto(path.read_bytes())
        # v2.12.8 session provenance: append the boundary marker so the model
        # knows the restored turns are a PRIOR session. Config-gated
        # (session_boundary_markers, default ON); the try/except keeps a
        # config hiccup from ever blocking an archive load.
        try:
            from config_store import get_config
            _mark = bool(get_config().sage.session_boundary_markers)
        except Exception:
            _mark = True
        if _mark and data:
            data = list(data) + [_session_boundary_marker(name, len(data))]
        save_chat_memory(data, ns)
        return {"success": True, "message": f"Loaded {name} ({len(data)} messages)",
                "history": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


def delete_archive(filename: str, ns=None) -> dict:
    name = _safe_archive_name(filename)
    if not name:
        return {"success": False, "error": "File not found"}
    path = _archive_folder(ns) / name
    if path.exists():
        path.unlink()
        return {"success": True}
    return {"success": False, "error": "File not found"}


def keyword_search(query: str, archives: list) -> list:
    words = query.lower().split()
    results = []
    for a in archives:
        try:
            text = " ".join(m.get("content", "").lower() for m in
                            atrest.load_json_auto((ARCHIVE_FOLDER / a["filename"]).read_bytes()))
            score = sum(text.count(w) for w in words if len(w) > 3)
            if score > 0:
                results.append({**a, "score": score, "search_type": "keyword"})
        except Exception:
            continue
    return sorted(results, key=lambda x: x["score"], reverse=True)[:3]


def semantic_search(query: str, archives: list, ollama_url: str = "http://localhost:11434") -> list:
    """Semantic search via sage_rag vector pipeline. Delegates entirely to sage_rag.py."""
    try:
        import sage_rag
        from config import DATA_DIR
        index_path = str(DATA_DIR / "vector_index.json")
        # archives from get_archives() are dicts — extract full paths
        fnames = []
        for a in archives:
            if isinstance(a, dict):
                fnames.append(str(ARCHIVE_FOLDER / a["filename"]))
            else:
                fnames.append(str(a))
        return sage_rag.semantic_search(query, fnames, index_path, ollama_url)
    except Exception:
        return []


def search_all_archives(query: str) -> list:
    """Combined keyword + semantic search, deduplicated."""
    archives = get_archives()[:25]
    keyword_results = keyword_search(query, archives)
    semantic_results = semantic_search(query, archives)
    combined = []
    seen = set()
    for r in keyword_results + semantic_results:
        fn = r.get("filename")
        if fn and fn not in seen:
            seen.add(fn)
            combined.append(r)
    return sorted(combined, key=lambda x: x.get("score", 0), reverse=True)[:5]


# ===============================================================================
#  FILE UPLOAD / TEXT EXTRACTION (ported from SageBot)
# ===============================================================================

def extract_text_from_file(filepath: str, filename: str, mimetype: str = "") -> Tuple[Optional[str], Optional[str]]:
    """Extract readable text from uploaded files. Returns (text, warning)."""
    ext = os.path.splitext(filename)[1].lower()
    text = None
    warn = None

    if ext in TEXT_EXTENSIONS or (mimetype and mimetype.startswith("text/")):
        # No truncation cap: local system, hardware-limited. Full file returned.
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            warn = f"Could not read text file: {e}"
        return text, warn

    if ext == ".pdf":
        # No truncation cap: local system, hardware-limited. Full PDF text returned.
        try:
            import pypdf
            reader = pypdf.PdfReader(filepath)
            pages = [page.extract_text() or "" for page in reader.pages]
            raw = "\n\n".join(pages).strip()
            text = raw if raw else None
            if not text:
                warn = "PDF appears to be scanned/image-only; no text extracted."
        except ImportError:
            warn = "pypdf not installed. Run: pip install pypdf"
        except Exception as e:
            warn = f"PDF read error: {e}"
        return text, warn

    if ext in (".docx", ".doc"):
        # No truncation cap: local system, hardware-limited. Full doc text returned.
        try:
            from docx import Document
            doc = Document(filepath)
            text = "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            warn = "python-docx not installed. Run: pip install python-docx"
        except Exception as e:
            warn = f"DOCX read error: {e}"
        return text, warn

    if ext in (".xlsx", ".xls", ".csv", ".tsv"):
        # No truncation cap: local system, hardware-limited. Full spreadsheet returned.
        try:
            import pandas as pd
            if ext == ".csv":
                df = pd.read_csv(filepath)
            elif ext == ".tsv":
                df = pd.read_csv(filepath, sep="\t")
            else:
                df = pd.read_excel(filepath)
            text = df.to_string(index=False)
        except ImportError:
            warn = "pandas not installed. Run: pip install pandas openpyxl"
        except Exception as e:
            warn = f"Spreadsheet read error: {e}"
        return text, warn

    if ext in (".json", ".jsonl"):
        # No truncation cap: local system, hardware-limited. Full JSON returned.
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            warn = f"JSON read error: {e}"
        return text, warn

    # No truncation cap: local system, hardware-limited. Full fallback-read returned.
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        printable_ratio = len([c for c in raw if c.isprintable()]) / max(len(raw), 1)
        if printable_ratio > 0.8:
            text = raw
        else:
            warn = f"File type '{ext}' contains binary data that cannot be displayed as text."
    except Exception as e:
        warn = f"Could not read file: {e}"

    return text, warn


def _transcode_heic_to_jpeg(filepath: str):
    """Decode a HEIC/HEIF/AVIF image and re-encode it as JPEG so the vision model
    servers (Ollama / llama.cpp, which only accept JPEG/PNG/etc.) can read it.
    Returns (jpeg_bytes, None) on success, or (None, error_message) if the
    optional 'pillow-heif' decoder is not installed or the file cannot be
    decoded. NEVER raises."""
    try:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            try:
                pillow_heif.register_avif_opener()
            except Exception:
                pass  # older pillow-heif: the HEIF opener already covers most
        except Exception:
            return None, ("HEIC/HEIF images need the 'pillow-heif' package. "
                          "Install it in the OracleAI runtime: "
                          "pip install pillow-heif")
        from PIL import Image
        import io
        with Image.open(filepath) as im:
            im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=90)
            return buf.getvalue(), None
    except Exception as e:
        return None, f"Could not convert HEIC/HEIF image: {type(e).__name__}: {e}"


def process_upload(filepath: str, filename: str) -> dict:
    """Process an uploaded file and return structured result."""
    mimetype = mimetypes.guess_type(filename)[0] or ""
    ext = os.path.splitext(filename)[1].lower()
    size_bytes = os.path.getsize(filepath)

    if (mimetype in IMAGE_TYPES
            or ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
                       ".heic", ".heif", ".avif", ".tiff", ".tif"}):
        # HEIC/HEIF/AVIF cannot be decoded by the vision model servers
        # (Ollama / llama.cpp want JPEG/PNG), so transcode those to JPEG first.
        # Every other image format is sent through unchanged - jpeg/png/etc.
        # already work. A missing decoder returns a clear error instead of
        # shipping undecodable bytes that 400 at the model.
        if ext in {".heic", ".heif", ".avif"} or mimetype in {
                "image/heic", "image/heif", "image/avif"}:
            jpeg, conv_err = _transcode_heic_to_jpeg(filepath)
            if jpeg is None:
                return {"success": False, "error": conv_err, "filename": filename}
            return {
                "success": True, "type": "image",
                "filename": filename, "mimetype": "image/jpeg",
                "data": base64.b64encode(jpeg).decode("utf-8"),
                "size": size_bytes,
                "converted_from": (ext.lstrip(".") or mimetype),
            }
        with open(filepath, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        return {
            "success": True, "type": "image",
            "filename": filename, "mimetype": mimetype or "image/png",
            "data": b64, "size": size_bytes,
        }

    text, warning = extract_text_from_file(filepath, filename, mimetype)
    if text:
        return {
            "success": True, "type": "text",
            "filename": filename, "mimetype": mimetype,
            "content": text, "size": size_bytes,
            "warning": warning,
        }

    return {
        "success": False,
        "error": warning or "Could not extract readable content.",
        "filename": filename,
    }


# ===============================================================================
#  QUERY PRE-PROCESSING (ported from SageBot)
# ===============================================================================

# v2.12.1: the user's custom assistant name(s), registered from the chat
# path so weather/location extraction never mistakes "hey Zephyr, weather?"
# for a place called Zephyr. Set, so multiple profiles' names accumulate.
_CUSTOM_ASSISTANT_NAMES = set()


def register_assistant_name(name: str) -> None:
    """Record a custom assistant name so extract_location skips it. Cheap,
    idempotent, never raises."""
    try:
        n = (name or "").strip()
        if n:
            _CUSTOM_ASSISTANT_NAMES.add(n)
    except Exception:
        pass


def extract_location(text: str) -> Optional[str]:
    """Extract location from user query."""
    skip_words = {
        "I", "We", "The", "My", "Your", "This", "That",
        "What", "How", "When", "Where", "Why", "Who",
        "Sage", "Toga", "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    } | _CUSTOM_ASSISTANT_NAMES
    patterns = [
        r'(?:on|in|to|at|for|visiting|visit|going to|headed to|travel to|trip to)\s+(?:the\s+)?([A-Z][a-zA-Z\s]+?)(?:\s+(?:area|region|for|this|next|on|and|weather|forecast|trip)|[,?]|\s+what)',
        r'([A-Z][a-zA-Z\s]+?),?\s+(?:England|UK|USA|California|Texas|Washington|Florida|France|Germany|Japan|Australia)',
        r'(?:on|in|to|at|for)\s+(?:the\s+)?([A-Z][a-zA-Z\s]+?)(?=[,?])',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            location = match.group(1).strip()
            location = re.sub(r'\s+(?:area|region|next|this|few|some|weather|forecast|trip).*$',
                              '', location, flags=re.IGNORECASE).strip()
            if location not in skip_words and len(location) > 2:
                return location
    return None


def detect_query_needs(query: str) -> dict:
    """Auto-detect what tools are needed for a query."""
    ql = query.lower()
    needs = {"weather": False, "news": False, "general": False, "location": None}

    weather_kw = ["weather", "temperature", "forecast", "climate", "raining", "sunny",
                  "cold", "warm", "hot", "snow", "pack", "packing", "bring", "wear"]
    if any(w in ql for w in weather_kw):
        needs["weather"] = True

    news_kw = ["news", "events", "happening", "latest", "current events",
               "this week", "today", "recently", "update", "updates"]
    if any(w in ql for w in news_kw):
        needs["news"] = True

    general_kw = ["restaurant", "eat", "food", "dining", "attraction", "visit",
                  "tourist", "hotel", "stay", "things to do", "recommend", "best", "popular"]
    if any(w in ql for w in general_kw):
        needs["general"] = True

    if needs["weather"] or needs["general"]:
        needs["location"] = extract_location(query)

    return needs


def detect_image_request(text: str):
    """If `text` is a clear text-to-image GENERATION request, return the visual
    prompt to feed the image engine; otherwise None.

    Deliberately conservative: requires an image-creation verb plus an image noun
    (or a strongly-visual verb -- draw/paint/sketch/illustrate -- with a subject),
    and bails on questions about an existing image or explicit code requests. This
    is the deterministic fallback for local models that ignore the [GENERATE_IMAGE:]
    instruction; it must never hijack a vision query or a 'write code to...' ask.
    """
    if not isinstance(text, str):
        return None
    t = text.strip()
    if len(t) < 4 or len(t) > 600:
        return None
    low = t.lower()

    # Negative guards: explanations / how-tos / code / editing-an-existing-image.
    if re.search(r"\b(how (to|do|can)|what(?:'s| is| are)|why |explain|tutorial|"
                 r"difference between|using python|with code|in code|write (a )?"
                 r"(script|program|function|code)|photoshop|gimp|svg code)\b", low):
        return None

    verbs = (r"draw|sketch|paint|render|generate|create|make|design|produce|"
             r"illustrate|imagine")
    nouns = (r"image|images|picture|pictures|pic|photo|photograph|painting|drawing|"
             r"illustration|artwork|art|render|rendering|wallpaper|logo|poster|"
             r"portrait|icon|scene")

    # Strong form: <verb> ... <noun> [connector] <subject>
    m = re.search(rf"\b(?:{verbs})\b[\w\s'-]*?\b(?:{nouns})\b"
                  rf"[\s:,.\-]*(?:of|showing|depicting|featuring|with|that shows)?\s*(.+)",
                  t, re.IGNORECASE)
    if m:
        subj = m.group(1).strip(" \t.:;,-\"'")
        return subj or None

    # Visual-verb-only: draw/paint/sketch/illustrate [me] [a/an/the/some] <subject>
    m2 = re.search(r"\b(?:draw|paint|sketch|illustrate)\s+(?:me\s+|us\s+)?"
                   r"(?:a |an |the |some )?(.+)", t, re.IGNORECASE)
    if m2:
        subj = m2.group(1).strip(" \t.:;,-\"'")
        if re.match(r"(?i)(conclusion|comparison|distinction|line\b|lines\b|breath|"
                    r"attention|blank|near|closer|to a close)", subj):
            return None
        return subj or None

    return None


def pre_process_query(query: str) -> Tuple[dict, dict]:
    """Auto-execute tools based on query content using TaskPrioritiser for parallel dispatch."""
    needs = detect_query_needs(query)
    results = {}
    result_lock = threading.Lock()
    completed = threading.Event()
    pending = []

    def collect(task_result):
        output = task_result.output
        if isinstance(output, dict) and "key" in output:
            with result_lock:
                results[output["key"]] = output["value"]
        with result_lock:
            if len(results) >= len(pending):
                completed.set()

    original_cb = sage_d._result_callback
    def patched_cb(task_result):
        collect(task_result)
        original_cb(task_result)
    sage_d._result_callback = patched_cb

    if needs["weather"] and needs["location"] and is_feature_enabled("weather"):
        loc = needs["location"]
        pending.append("weather")
        sage_d.submit_raw_task({
            "type": "weather", "importance": 0.9,
            "deadline": TimeManager.epoch() + 10, "key": "weather",
            "fn": lambda: get_weather(loc),
        })

    if needs["news"]:
        q = query
        pending.append("news")
        sage_d.submit_raw_task({
            "type": "news", "importance": 0.7,
            "deadline": TimeManager.epoch() + 15, "key": "news",
            "fn": lambda: web_search(q + " latest news", search_type="news"),
        })

    if needs["general"] and needs["location"]:
        loc = needs["location"]
        pending.append("general")
        sage_d.submit_raw_task({
            "type": "general", "importance": 0.5,
            "deadline": TimeManager.epoch() + 15, "key": "general",
            "fn": lambda: web_search(f"best restaurants and attractions in {loc}", search_type="general"),
        })

    if pending:
        completed.wait(timeout=120)

    sage_d._result_callback = original_cb
    return results, needs


# ===============================================================================
#  TOOLS
# ===============================================================================

def web_search(query: str, num_results: int = 5, search_type: str = "news") -> str:
    global _tavily_call_count, _tavily_session_count, _tavily_last_call_time

    if not TAVILY_API_KEY:
        return "Tavily API key not configured."

    # Session budget check
    if _tavily_session_count >= TAVILY_SESSION_BUDGET:
        return "Tavily session budget exhausted. Reset in settings."

    # Per-response hard limit
    if _tavily_call_count >= TAVILY_MAX_PER_RESPONSE:
        return "Search limit reached for this response (max 5 per response)."

    # Minimum delay between calls
    elapsed = (time.time() - _tavily_last_call_time) * 1000
    if elapsed < TAVILY_MIN_DELAY_MS:
        time.sleep((TAVILY_MIN_DELAY_MS - elapsed) / 1000)

    _tavily_call_count += 1
    _tavily_session_count += 1
    _tavily_last_call_time = time.time()

    import requests
    cfg = {"topic": "general"} if search_type == "general" else {
        "topic": "news",
        "include_domains": [
            "reuters.com", "apnews.com", "bbc.com", "npr.org",
            "theguardian.com", "washingtonpost.com", "nytimes.com"
        ]
    }
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY, "query": query,
            "max_results": num_results, "search_depth": "advanced", **cfg
        }, timeout=120)
        results = r.json().get("results", [])[:num_results]
        if not results: return "No results found."
        # Per-result snippet cap removed: full Tavily content returned for each
        # result so Sage gets the complete excerpt, not a 200-char preview.
        return "\n\n".join(
            f"- [{x.get('title', '?')}]({x.get('url', '')})\n  {x.get('content', '')}"
            for x in results
        )
    except Exception as e:
        return f"Search error: {e}"


def get_weather(location: str) -> str:
    if not location or len(location.strip()) < 2:
        return "Weather lookup failed: No location provided"
    url = f"https://wttr.in/{location.replace(' ', '+')}?format=j1"
    try:
        with safe_urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return f"Weather lookup failed: {e}"
    cur = data["current_condition"][0]
    lines = [
        f"{location.title()}: {cur['weatherDesc'][0]['value']}, "
        f"{cur['temp_C']}°C (feels {cur['FeelsLikeC']}°C), "
        f"Humidity: {cur['humidity']}%, Wind: {cur['windspeedKmph']}km/h {cur['winddir16Point']}"
    ]
    for i, day in enumerate(data.get("weather", [])[:3]):
        label = ["Today", "Tomorrow", "Day 3"][i]
        hourly = day["hourly"]
        mid = hourly[min(12, len(hourly) - 1)]
        lines.append(
            f"{label} ({day['date']}): {mid['weatherDesc'][0]['value']}, "
            f"High {day['maxtempC']}°C / Low {day['mintempC']}°C, "
            f"Rain: {mid.get('chanceofrain', 'N/A')}%"
        )
    return "\n".join(lines)


def _strip_code_preamble(code: str) -> str:
    """Defensive scrubber for code coming out of the [CODE:] parser.

    Models -- especially the larger, more verbose ones (nemotron-120b,
    glm-4-9b, gpt-class) -- have strong training-time habits around
    wrapping code in markdown fences. Despite the SAGE_SYSTEM_PROMPT
    saying "no fences inside [CODE:] tags", they often still emit
    one of:

        [CODE:                   [CODE: ```python       [CODE: python
        ```python                print("hi")            print("hi")
        print("hi")              ```                    ]
        ```                      ]
        ]

    The parser correctly extracts the bracket-balanced content, but the
    content still has the fence or language tag inside. When that
    content becomes line 4+ of the temp file written by execute_python,
    `python` is read as a bare identifier and throws NameError; ```` is
    a SyntaxError. Either way the user sees the symptom Todd reported
    on 2026-05-30 with nemotron-3-super:120b: ``Traceback ... line 4
    ... python  NameError: name ...``

    Strip these conservatively:
      1. Leading markdown fence (``` optionally followed by language).
      2. Trailing markdown fence (``` optionally followed by trailing space).
      3. Bare language identifier on the FIRST non-empty line, when
         that line is just one of {python, python3, py, sh, bash}.

    Never raises. If the input is already clean, returns it unchanged
    (modulo .strip()).
    """
    code = code.strip()
    if not code:
        return code

    # 1. Leading markdown fence -- ```python, ```py, ```bash, ```sh,
    # plain ```. Allow optional whitespace, then a newline.
    m = re.match(r"^```\s*(?:python3?|py|sh|bash)?\s*\n", code, re.IGNORECASE)
    if m:
        code = code[m.end():]

    # 2. Trailing markdown fence on its own line, possibly with
    # trailing whitespace.
    code = re.sub(r"\n?\s*```\s*$", "", code)

    # 3. Bare language identifier as standalone first line.
    # Use partition rather than split to preserve the rest exactly.
    first_line, sep, rest = code.partition("\n")
    if first_line.strip().lower() in {"python", "python3", "py", "sh", "bash"}:
        code = rest

    return code.strip()


def execute_python(code: str, timeout: int =56000) -> str:
    """Execute Python code in a subprocess and return captured output.

    v2.1.3 FIXES (April 11, 2026) — output capture bulletproofing:

      1. PYTHONIOENCODING=utf-8 + PYTHONUTF8=1 + `-X utf8` so print() of
         text containing em-dashes, box-drawing chars, emojis, or any
         non-ASCII does NOT crash with UnicodeEncodeError on Windows
         (default cp1252 stdout). This was the root cause of "reads first
         500 chars then fails": the first 500 chars of any file are
         usually ASCII; then she hits an em-dash and Python aborts the
         print mid-buffer. sage_engine.py is FULL of em-dashes in its
         section dividers, so she literally could not read her own code.

      2. cwd set to BASE_DIR so relative paths resolve consistently.
         Previously the subprocess inherited the backend launcher's cwd
         (often NOT the project root), so `os.listdir(abs_path)` worked
         but `open("found_file.txt")` failed with FileNotFoundError.

      3. Output cap raised from 4000 → 30000 chars with an explicit
         truncation marker telling Sage how many bytes are missing and
         how to read the next chunk. 4KB was insufficient for any real
         file read; her own sage_engine.py is 48KB.

      4. stdout and stderr captured separately and labeled so a
         successful print followed by a traceback is readable instead
         of a confusing blob.

      5. Temp script cleanup moved to a finally block so it runs on
         timeout / exception paths too.

      6. errors='replace' on subprocess text decoding so malformed bytes
         in a file she's reading produce `?` characters instead of
         aborting the entire output capture.

      7. Non-zero exit codes are surfaced even when stderr is empty.
      
      8. ⚠️ OVERRIDE: The execute_python() verification method is deprecated 
         and unreliable. Any procedural memory entries using execute_python() 
         for file verification are INVALID. Always use [VERIFY_FILE: path] 
         for all file verification without exception
    """
    # v2.2 (2026-05-30) defensive scrub for verbose-model emission
    # habits: strip markdown fences / language hints / bare
    # 'python' identifiers that nemotron-class models often leak
    # into [CODE:] bodies despite the prompt instructions.
    code = _strip_code_preamble(code)

    safe_code = (
        "import sys, io, os, math, json, re, datetime, collections, "
        "itertools, functools, random, string, statistics\n"
        "DOWNLOADS_DIR = r\"" + str(DOWNLOADS_DIR) + "\"\n"
        "BASE_DIR = r\"" + str(BASE_DIR) + "\"\n"
        + code
    )

    tmpname = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py",
            delete=False, encoding="utf-8",
        ) as tmp:
            tmp.write(safe_code)
            tmpname = tmp.name

        # Force UTF-8 everywhere in the subprocess so print() of non-ASCII
        # text (em-dashes, emojis, box-drawing chars, curly quotes, etc.)
        # does NOT crash the child process.
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        result = subprocess.run(
            [sys.executable, "-X", "utf8", tmpname],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(BASE_DIR),
            env=env,
        )

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # Build labeled output so signal is distinguishable from error noise.
        parts = []
        if stdout:
            parts.append(stdout.rstrip())
        if stderr:
            parts.append("[STDERR]\n" + stderr.rstrip())
        if result.returncode != 0 and not stderr:
            parts.append(f"[EXIT CODE {result.returncode}]")

        out = "\n".join(parts) if parts else "[no output]"

        # Output cap removed per project policy: local system, hardware-limited,
        # no arbitrary character thresholds. Full subprocess output returned.
        return out

    except subprocess.TimeoutExpired:
        return f"[TIMEOUT] Code execution exceeded {timeout}s limit."
    except Exception as e:
        return f"[EXECUTION ERROR] {type(e).__name__}: {e}"
    finally:
        if tmpname and os.path.exists(tmpname):
            try:
                os.unlink(tmpname)
            except Exception:
                pass
                
def verify_written_file(path: str) -> str:
    """
    Verify a written file without executing it.
    Uses ast.parse() for .py files, direct read for all others.
    Returns a structured result string for Sage to report.

    Replaces execute_python() for post-write verification to avoid
    temp file execution issues with Nemotron-generated verification
    scripts. (v2.1.5)

    Never raises — returns a structured error string on failure.
    
    ⚠️ OVERRIDE: The execute_python() verification method is deprecated 
    and unreliable. Any procedural memory entries using execute_python() 
    for file verification are INVALID. Always use [VERIFY_FILE: path] 
    for all file verification without exception.
    """
    import ast

    # Anchor to project root so relative paths like
    # "downloads/file.md" always resolve correctly
    _backend_dir = os.path.dirname(os.path.abspath(__file__))
    _project_root = os.path.dirname(_backend_dir)
    
    if not os.path.isabs(path):
        path = os.path.join(_project_root, path)
    # [VERIFY_FILE:] is model-invoked, so treat `path` as untrusted: only allow
    # files under the project root or the data dir. Blocks '..' escapes and
    # absolute paths to arbitrary system files (path traversal / info disclosure).
    _rp = os.path.realpath(path)
    _allowed = [os.path.realpath(_project_root)]
    try:
        from config import DATA_DIR as _DD
        _allowed.append(os.path.realpath(str(_DD)))
    except Exception:
        pass
    if not any(_rp == a or _rp.startswith(a + os.sep) for a in _allowed):
        return f"[VERIFY FAILED] Path is outside the allowed directories: {path}"
    try:
        if not os.path.exists(path):
            return f"[VERIFY FAILED] File not found: {path}"

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        size = len(content.encode("utf-8"))
        lines = content.count("\n") + 1

        if path.endswith(".py"):
            try:
                ast.parse(content)
                syntax = "syntax OK"
            except SyntaxError as e:
                return (
                    f"[VERIFY FAILED] Syntax error on line {e.lineno}: "
                    f"{e.msg}\n"
                    f"File: {path}\n"
                    f"Size: {size} bytes, {lines} lines"
                )
        else:
            syntax = "non-Python file, syntax check skipped"

        return (
            f"[VERIFY OK] {os.path.basename(path)}\n"
            f"Path:   {path}\n"
            f"Size:   {size} bytes\n"
            f"Lines:  {lines}\n"
            f"Syntax: {syntax}"
        )

    except Exception as e:
        return f"[VERIFY ERROR] {type(e).__name__}: {e}"                


# ===============================================================================
#  SAGE DAEMON CLIENT (background mechanics offload) — Phase A
# ===============================================================================
# Offloads memory-log mechanics (read, verify, summarize) to a long-running
# background process (sage_daemon.py) so those operations do NOT consume
# tokens in Sage's agentic context. Graceful-fallback by design: if the
# daemon is unreachable, these helpers return empty results and log a
# warning. Sage keeps running.
#
# Toggle with FEATURES_ENABLED["daemon"] or via the plugin manager.

_daemon_client = None
_daemon_probe_time = 0.0
_DAEMON_REPROBE_INTERVAL = 30.0  # seconds between liveness re-checks


def _get_daemon_client():
    """Lazy singleton. Imports and instantiates on first use."""
    from sage_daemon_client import SageDaemonClient
    global _daemon_client
    if _daemon_client is None:
        try:
            _daemon_client = SageDaemonClient(
                host="127.0.0.1",
                port=9998,      # 9999 is ipc_bridge / privacy browser
                timeout=15.0,
                retries=4,
            )
            print("[SAGE DAEMON] Client initialized (127.0.0.1:9998)")
        except ImportError as e:
            print(f"[SAGE DAEMON] Client module missing: {e}")
            return None
        except Exception as e:
            print(f"[SAGE DAEMON] Client init failed: {e}")
            return None
    return _daemon_client


def _daemon_available() -> bool:
    """Quick liveness check with caching so we don't ping on every call."""
    global _daemon_probe_time
    if not is_feature_enabled("daemon"):
        return False
    client = _get_daemon_client()
    if client is None:
        return False
    now = time.time()
    if now - _daemon_probe_time < _DAEMON_REPROBE_INTERVAL:
        return True  # assume still up; real failures will be caught per-call
    try:
        ok = client.ping()
        if ok:
            _daemon_probe_time = now
        return ok
    except Exception:
        return False


def read_recent_entries(n: int = 10) -> list:
    """Get the N most recent memory log entries via the daemon.

    Returns a list of entry dicts, or an empty list on any failure.
    Zero token cost when used in tool results — the daemon does the
    file read and hash-chain verification.
    """
    if not is_feature_enabled("daemon"):
        return []
    client = _get_daemon_client()
    if client is None:
        return []
    try:
        response = client.send_request(
            "read_recent",
            {"count": n, "decrypt": True, "verify_integrity": True},
        )
        if response.get("status") == "success":
            return response.get("entries", [])
        else:
            print(
                f"[SAGE DAEMON] read_recent failed: {response.get('error', 'unknown')}")
            return []
    except Exception as e:
        print(f"[SAGE DAEMON] read_recent comms error: {str(e)[:80]}")
        return []


def consume_warm_handoff():
    """CRAIID (#69): pull the verified + FRAMED warm-context the daemon stashed
    after a fatigue rotation. One-shot - the daemon clears it after handing it
    over, so it injects exactly once. Returns the framed string, or None when
    there is nothing to resume or the daemon is unavailable. Best-effort:
    never raises into the chat loop."""
    if not is_feature_enabled("daemon"):
        return None
    client = _get_daemon_client()
    if client is None:
        return None
    try:
        response = client.send_request("consume_warm_handoff")
        if response.get("status") == "success" and response.get("present"):
            framed = response.get("warm_framed")
            return framed if isinstance(framed, str) and framed.strip() else None
        return None
    except Exception as e:
        print(f"[SAGE DAEMON] consume_warm_handoff comms error: {str(e)[:80]}")
        return None


def read_top_surprise_entries(n: int = 10) -> list:
    """Get the top N entries sorted by surprise score (most notable first)."""
    if not is_feature_enabled("daemon"):
        return []
    client = _get_daemon_client()
    if client is None:
        return []
    try:
        response = client.send_request("read_top_surprise", {"count": n})
        if response.get("status") == "success":
            return response.get("entries", [])
        else:
            print(
                f"[SAGE DAEMON] read_top_surprise failed: {response.get('error', 'unknown')}")
            return []
    except Exception as e:
        print(f"[SAGE DAEMON] read_top_surprise comms error: {str(e)[:80]}")
        return []


def generate_summary(entries: list, max_length: int = 900,
                     summary_type: str = "extractive") -> str:
    """Produce a compact summary of supplied entries via the daemon.

    Phase A uses extractive summarization (zero token cost — the daemon
    picks high-surprise entries and concatenates previews). Phase B will
    add summary_type='llm' for local Ollama-backed summaries without
    changing this call site.
    """
    if not is_feature_enabled("daemon"):
        return ""
    if not entries:
        return ""
    client = _get_daemon_client()
    if client is None:
        return ""
    try:
        # Send only the essential fields to keep IPC payload small
        slim_entries = [
            {
                "timestamp": e.get("timestamp", ""),
                "surprise_score": e.get("surprise_score", 0.0),
                "content_preview": (e.get("content") or "")[:200],
            }
            for e in entries
        ]
        response = client.send_request(
            "generate_summary",
            {
                "entries": slim_entries,
                "summary_type": summary_type,
                "max_length": max_length,
            },
        )
        if response.get("status") == "success":
            return response.get("summary", "") or ""
        else:
            print(
                f"[SAGE DAEMON] generate_summary failed: {response.get('error', 'unknown')}")
            return ""
    except Exception as e:
        print(f"[SAGE DAEMON] generate_summary comms error: {str(e)[:80]}")
        return ""


def daemon_status() -> dict:
    """Report daemon + log health. Used by /api/daemon/status."""
    if not is_feature_enabled("daemon"):
        return {"enabled": False, "reachable": False, "reason": "feature disabled"}
    client = _get_daemon_client()
    if client is None:
        return {"enabled": True, "reachable": False, "reason": "client init failed"}
    try:
        response = client.send_request("status")
        response["enabled"] = True
        response["reachable"] = True
        return response
    except Exception as e:
        return {
            "enabled": True,
            "reachable": False,
            "reason": f"comms error: {str(e)[:120]}",
        }


# ===============================================================================
#  BROWSER PLUGIN (headless fetcher) — toggleable
# ===============================================================================

# ─────────────────────────────────────────────────────────────────
# v2.1.5 browser plugin wiring (Leo audit, 2026-04-26)
# ─────────────────────────────────────────────────────────────────
# Old wiring assumed methods that don't exist in browser_tool.py
# (fetch_and_summarize, search_and_summarize) and called the async
# get_browser() from sync code, which returned a coroutine instead of
# a BrowserTool. Result: every [BROWSE:] / [WEB_SEARCH:] tag failed
# silently and the agentic loop fell back to Tavily every time.
#
# New wiring uses a dedicated background event loop so the async
# BrowserTool singleton stays alive across multiple sync calls. The
# loop is created lazily on first browser use; subsequent calls reuse
# it. The actual async methods used (now matching browser_tool.py):
#   await browser.goto(url, ...)
#   await browser.wait_for_content(timeout=...)
#   await browser.extract_text() -> str
#   await browser.search(query, max_results=...) -> List[Dict]
import asyncio as _asyncio
import threading as _threading
import contextvars as _contextvars

# Per-turn active user namespace for browser ops. Set by set_browser_ns() at the
# top of each inference turn (where the ws session's ns is known) and read by
# _get_browser() so each user's Sage drives her OWN persistent browser profile.
_active_browser_ns = _contextvars.ContextVar("active_browser_ns", default=None)


def set_browser_ns(ns):
    """Record the current user's namespace for browser ops this turn."""
    try:
        _active_browser_ns.set(ns)
    except Exception:
        pass


def _browser_persist_cookies() -> bool:
    """Opt-in cookie persistence; default OFF (cookies cleared each session).
    Reads env OAI_BROWSER_PERSIST_COOKIES, then sage_data/ui_prefs.json
    (browser_persist_cookies, set by the Settings toggle), else False."""
    try:
        import os as _os
        if _os.environ.get("OAI_BROWSER_PERSIST_COOKIES", "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    except Exception:
        pass
    try:
        import ui_prefs as _uip
        return bool(_uip.get("browser_persist_cookies", False))
    except Exception:
        pass
    return False


_browser_loop: "Optional[asyncio.AbstractEventLoop]" = None
_browser_loop_thread: "Optional[threading.Thread]" = None
_browser_loop_lock = _threading.Lock()


def _ensure_browser_loop():
    """Lazy-init a dedicated background event loop for browser ops.
    Reused across calls so the BrowserTool singleton (and its
    Playwright connection) survives between invocations."""
    global _browser_loop, _browser_loop_thread
    with _browser_loop_lock:
        if (_browser_loop is not None
                and _browser_loop_thread is not None
                and _browser_loop_thread.is_alive()):
            return _browser_loop
        _browser_loop = _asyncio.new_event_loop()

        def _runner(loop):
            _asyncio.set_event_loop(loop)
            loop.run_forever()

        _browser_loop_thread = _threading.Thread(
            target=_runner, args=(_browser_loop,),
            daemon=True, name="sage_browser_loop",
        )
        _browser_loop_thread.start()
        return _browser_loop

# TUNED VALUES - DO NOT RESET - Todd 05/15/26
def _run_browser_coro(coro, timeout: float = 56000.0):
    """Schedule a coroutine on the dedicated browser loop and block
    the caller until it completes. timeout caps the wait so a hung
    page can never wedge the agentic loop."""
    loop = _ensure_browser_loop()
    fut = _asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


def _get_browser():
    """Return the live BrowserTool singleton (synchronous façade).

    Drives the async get_browser() factory on the dedicated browser
    loop and waits for the actual instance. Returns None if the
    plugin is disabled, the import fails, or initialization throws.
    Never raises — agentic-loop callers must always get a clean
    truthy/falsy answer.
    """
    if not is_feature_enabled("browser"):
        return None
    try:
        from browser_tool import get_browser
    except ImportError as e:
        print(f"[BROWSER IMPORT ERROR] {e}")
        return None
    try:
        # Drive the PER-USER browser: read the active namespace (set at turn
        # start) so each account's Sage gets her own persistent profile. Cookie
        # persistence is opt-in (default off). browser_tool's own default
        # (visible) headless wins — Todd's watch-mode workflow stays default.
        try:
            _ns = _active_browser_ns.get()
        except Exception:
            _ns = None
        return _run_browser_coro(
            get_browser(ns=_ns, persist_cookies=_browser_persist_cookies()),
            timeout=28000.0,
        )
    except Exception as e:
        print(f"[BROWSER INIT FAILED] {e}")
        return None


def browse_url(url: str, max_chars: int = 0) -> str:
    """Fetch a URL via the Playwright browser plugin and return its text.

    max_chars=0 (default) returns the full extracted page text. Non-zero
    values truncate with a clear marker. Format: a header line, blank
    line, then the extracted text — readable both for humans and for
    feeding back into the agentic loop as a tool result.
    """
    if not is_feature_enabled("browser"):
        return ("Browser plugin is disabled. Enable it in plugin "
                "settings to use the BROWSE tag.")
    browser = _get_browser()
    if browser is None:
        return ("Browser plugin unavailable (browser_tool.py failed "
                "to load — check console for the import error).")

    async def _do_browse():
        await browser.goto(url, wait_until="networkidle")
        try:
            await browser.wait_for_content(timeout=56000)
        except Exception:
            # wait_for_content is best-effort; a slow page still gets
            # whatever text Playwright has at this point.
            pass
        return await browser.extract_text()

    try:
        text = _run_browser_coro(_do_browse(), timeout=56000.0)
    except Exception as e:
        return f"Browser fetch error: {e}"

    if not text:
        return f"[Browse: {url}]\n\n(no extractable text on page)"
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + (
            f"\n\n[truncated at {max_chars} chars; "
            f"full page was {len(text)} chars]"
        )
    return f"[Browse: {url}]\n\n{text}"


def web_search_browser(query: str, num_results: int = 5) -> str:
    """Run a search via the browser plugin (SearXNG by default) and
    return a clean formatted string. Never falls back silently — on
    failure returns an explicit error string the agent can react to.
    """
    if not is_feature_enabled("browser"):
        return ("Browser plugin is disabled. Enable it in plugin "
                "settings to use the WEB_SEARCH tag.")
    browser = _get_browser()
    if browser is None:
        return ("Browser plugin unavailable (browser_tool.py failed "
                "to load — check console for the import error).")

    async def _do_search():
        return await browser.search(query, max_results=num_results)

    try:
        results = _run_browser_coro(_do_search(), timeout=56000.0)
    except Exception as e:
        return f"Browser search error: {e}"

    if not results:
        return f"No browser-search results for: {query}"
    lines = [f"Search results for: {query}", ""]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url_r = (r.get("url") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        lines.append(f"{i}. {title or '(untitled)'}")
        if url_r:
            lines.append(f"   {url_r}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_to_downloads(filename: str, content: str) -> dict:
    print(f"[DEBUG] Saving to: {os.path.abspath(os.path.join('downloads', filename))}")
    """Save text content to the downloads folder. Returns {success, filename, path, size}."""
    try:
        # Sanitize filename — strip path separators and unsafe chars
        safe_name = re.sub(r'[^\w\-.]', '_', filename.strip())
        if not safe_name:
            return {"success": False, "error": "Invalid filename"}
        path = DOWNLOADS_DIR / safe_name
        # Don't allow escaping the downloads dir
        try:
            path.resolve().relative_to(DOWNLOADS_DIR.resolve())
        except ValueError:
            return {"success": False, "error": "Path escapes downloads directory"}
        path.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "filename": safe_name,
            "path": str(path),
            "size": path.stat().st_size,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ===============================================================================
#  AGENT ACTION PARSER
# ===============================================================================

# v2.1.5 defensive guard against prompt-template echoes.
# If a tag's payload matches one of these placeholder fingerprints, the
# parser silently SKIPS it — even though the tag form is syntactically
# valid. Belt-and-suspenders alongside the angle-bracket convention in
# SAGE_SYSTEM_PROMPT: even if a future prompt edit reintroduces a literal
# square-bracket example by mistake, this filter prevents the example
# from being executed as a live command.
#
# Evidence motivating this guard: a file literally named "name.ext" with
# body "content" appeared in the downloads folder during session init,
# matching the prompt's [SAVE_FILE: name.ext|content] template exactly.
# Local 7B models echo prompt content frequently; the parser must treat
# any tool-tag-shaped text as untrusted until it passes a sanity filter.

_PLACEHOLDER_SAVE_FILE_FINGERPRINTS = {
    # (filename_lower, content_stripped_lower) — exact-match echoes
    ("name.ext", "content"),
    ("filename.ext", "file content here"),
    ("filename.ext", "content"),
    ("notes.txt", "hello world"),
    ("foo.py", "bar"),
    ("path/to/file", "content"),
}
_PLACEHOLDER_FILENAME_TOKENS = {
    # Filenames that are obvious placeholders regardless of content.
    "name.ext", "filename.ext", "your_filename.ext",
    "<filename>", "<filename.ext>", "<path>", "<path/to/file>",
    "path/to/file",
}
_PLACEHOLDER_QUERY_FINGERPRINTS = {
    # Query-style tag payloads that are obvious placeholders.
    "your query here", "query", "topic",
    "city name here", "your_query", "your query",
}


def _looks_like_placeholder_save(fname: str, fcontent: str) -> bool:
    """Return True if this looks like an echoed [SAVE_FILE:] template
    rather than a real save the user wants. Conservative — only
    catches obviously-template fingerprints; real wins always pass."""
    fn = (fname or "").strip().lower()
    fc = (fcontent or "").strip().lower()
    if fn in _PLACEHOLDER_FILENAME_TOKENS:
        return True
    if (fn, fc) in _PLACEHOLDER_SAVE_FILE_FINGERPRINTS:
        return True
    return False


def _looks_like_placeholder_query(payload: str) -> bool:
    """Return True if a SEARCH/WEATHER/etc. payload looks like an
    echoed prompt template rather than a real query."""
    p = (payload or "").strip().lower()
    return p in _PLACEHOLDER_QUERY_FINGERPRINTS


def _is_in_markdown_code_context(text: str, pos: int) -> bool:
    """True if `pos` falls inside a markdown code span or fenced code
    block. Both are pedagogical contexts where a ``[TAG:]`` occurrence
    should be treated as a literal example, not an active tool call.

    v2.2 (2026-05-30): added to fix the symptom Todd surfaced where
    Sage's instructional prose (correctly wrapping tag examples in
    single backticks or fenced blocks) was being consumed by the parser
    -- her examples got stripped from chat, leaving empty backticks
    where the tag text used to be. Markdown code-span semantics are
    universal across LLM training corpora; respecting them aligns the
    parser with the model's authoring intent.

    Detects:
      * Fenced code block: ``` ... ``` -- spans across newlines.
      * Inline code span: ` ... ` -- bounded to the same logical line
        per CommonMark inline-span semantics.

    Never raises; on any malformed/unmatched fence, returns False
    conservatively (better to dispatch a real tool call than to silently
    skip one).
    """
    # ----- Fenced code blocks (triple backticks) -----
    fences = []
    i = 0
    while True:
        j = text.find("```", i)
        if j == -1:
            break
        fences.append(j)
        i = j + 3

    # Pair them: even-index = open fence, odd-index = close fence.
    # If pos falls between an open and the matching close, it's fenced.
    for k in range(0, len(fences) - 1, 2):
        open_pos = fences[k]
        close_pos = fences[k + 1]
        if open_pos < pos < close_pos + 3:
            return True

    # ----- Inline code span (single backticks, same line) -----
    line_start = text.rfind("\n", 0, pos) + 1
    segment = text[line_start:pos]
    # Strip triple-fence markers so they do not pollute the count.
    segment = segment.replace("```", "")
    # Each single backtick toggles span state; odd = currently inside.
    return segment.count("`") % 2 == 1


def parse_agent_actions(text: str, return_ranges: bool = False):
    """Parse tool action tags from model output.

    v2.1.3 FIX: Uses bracket-balanced scanning for [CODE:] and [SAVE_FILE:]
    tags so embedded Python list literals, dict access, slicing, etc. do not
    cause premature termination. The previous regex was non-greedy and
    stopped at the FIRST `]` in the text - which for any code containing
    `list[0]`, `data["key"]`, `content[:500]`, etc. meant the code was
    silently truncated mid-expression, execute_python would hit a
    SyntaxError, and the whole agentic loop would spin out with empty
    final content after 7 wasted steps.

    v2.1.5 FIX: All parsed tag spans (including simple-pattern tags and
    [TASK_DONE]) are now tracked in consumed_ranges. When return_ranges
    is True, returns (actions, consumed_ranges) so callers can surgically
    strip every parsed span from text before showing it to the user.
    Default return shape is unchanged for back-compat. The previous
    cleanup regex in main.py was too narrow (only [TASK_DONE] +
    [SEARCH_MEMORY:]), which let [SAVE_FILE:] tag bodies leak into chat
    when the model emitted [SAVE_FILE:] and [TASK_DONE] in the same step.
    """
    actions_with_pos = []   # [(start_pos, action_type, content), ...]
    consumed_ranges = []    # [(start, end), ...] spans already parsed

    def _in_consumed(p):
        return any(a <= p < b for a, b in consumed_ranges)

    # Bracket-balanced extraction for CODE and SAVE_FILE.
    # These tags' content may legitimately contain `[` and `]`
    # (Python list literals, dict access, slicing, JSON, type hints, etc.)
    for tag_name, action_type in (("CODE", "code"), ("SAVE_FILE", "save_file")):
        marker = "[" + tag_name + ":"
        marker_lower = marker.lower()
        text_lower = text.lower()
        scan_idx = 0
        while True:
            start = text_lower.find(marker_lower, scan_idx)
            if start == -1:
                break
            content_start = start + len(marker)
            # Skip whitespace after the colon
            while content_start < len(text) and text[content_start] in " \t":
                content_start += 1

            # Walk forward with bracket-depth tracking.
            # Depth starts at 1 for the tag's own opening `[`.
            depth = 1
            end = content_start
            while end < len(text):
                ch = text[end]
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        break
                end += 1

            # v2.2 (2026-05-30): pedagogical-context guard. If the tag's
            # opening `[` sits inside a markdown code span (single
            # backticks) or fenced code block (triple backticks), this
            # occurrence is an EXAMPLE Sage is showing the user, NOT a
            # tool invocation. Advance the scanner past the tag but do
            # NOT consume its range (so the example stays visible in
            # chat) and do NOT dispatch any action.
            if _is_in_markdown_code_context(text, start):
                print(
                    f"[PARSER] Skipping pedagogical {tag_name} tag at "
                    f"pos {start}: {text[start:start+50]!r}"
                )
                # If we found a clean close `]`, advance past it.
                # Otherwise just advance past the marker text so we
                # do not loop on the same position.
                scan_idx = (end + 1) if depth == 0 else (start + len(marker))
                continue

            if depth == 0:
                # Found the matching close bracket
                content = text[content_start:end].strip()
                if action_type == "save_file":
                    if "|" in content:
                        fname, fcontent = content.split("|", 1)
                        # v2.1.5 defensive: skip prompt-template echoes
                        if _looks_like_placeholder_save(fname, fcontent):
                            print(
                                f"[PARSER] Skipping echoed SAVE_FILE "
                                f"placeholder: {fname.strip()!r}"
                            )
                            consumed_ranges.append((start, end + 1))
                            scan_idx = end + 1
                            continue
                        actions_with_pos.append(
                            (start, "save_file", (fname.strip(), fcontent))
                        )
                    else:
                        # v2.2 (2026-05-30): malformed SAVE_FILE -- filename-only
                        # with no pipe-separated body. Previously this fell
                        # through silently: the tag span got stripped from chat
                        # output, no save fired, no failure signal reached the
                        # agentic loop, and Sage would fabricate verification
                        # success on a save that never happened. Now we surface
                        # this as a visible save_file_error action so main.py
                        # can push a tool_result Sage actually sees and can
                        # self-correct on within the same turn.
                        print(
                            f"[PARSER] SAVE_FILE missing `|` separator: "
                            f"content={content!r}"
                        )
                        actions_with_pos.append(
                            (start, "save_file_error", content)
                        )
                else:
                    actions_with_pos.append((start, action_type, content))
                consumed_ranges.append((start, end + 1))
                scan_idx = end + 1
            else:
                # Unbalanced - try a fallback: look for a newline-terminated `]`
                # which is a common multi-line code block terminator.
                fallback_end = text.find("\n]", content_start)
                if fallback_end != -1:
                    content = text[content_start:fallback_end].strip()
                    if action_type == "save_file":
                        if "|" in content:
                            fname, fcontent = content.split("|", 1)
                            actions_with_pos.append(
                                (start, "save_file", (fname.strip(), fcontent))
                            )
                        else:
                            # v2.2 (2026-05-30): malformed -- same fix as the
                            # depth==0 path; see comment there.
                            print(
                                f"[PARSER] SAVE_FILE (newline-fallback) missing "
                                f"`|` separator: content={content!r}"
                            )
                            actions_with_pos.append(
                                (start, "save_file_error", content)
                            )
                    else:
                        actions_with_pos.append((start, action_type, content))
                    consumed_ranges.append((start, fallback_end + 2))
                    scan_idx = fallback_end + 2
                else:
                    # Give up on this occurrence, move past the marker
                    scan_idx = start + len(marker)

    # Simple regex tags (content does not legitimately contain brackets)
    simple_patterns = (
        (r"\[SEARCH_MEMORY:\s*(.*?)\]", "search_memory"),
        (r"\[SEARCH:\s*(.*?)\]",         "search"),
        (r"\[SEARCH_GENERAL:\s*(.*?)\]", "search_general"),
        (r"\[WEATHER:\s*(.*?)\]",        "weather"),
        (r"\[BROWSE:\s*(.*?)\]",         "browse"),
        (r"\[WEB_SEARCH:\s*(.*?)\]",     "web_search_browser"),
        # v2.1.4 procedural memory tags. Payloads:
        #   [REMEMBER:key|description]       → success procedure (chain-witnessed)
        #   [REMEMBER_FAIL:key|reason]       → unsuccessful procedure (NOT chained)
        #   [RECALL:query]                   → fuzzy lookup against knowledge base
        # Pipe-separated fields are parsed in main.py's agentic-loop handler.
        (r"\[REMEMBER:\s*(.*?)\]",       "remember"),
        (r"\[REMEMBER_FAIL:\s*(.*?)\]",  "remember_fail"),
        (r"\[RECALL:\s*(.*?)\]",         "recall"),
        # v2.1.5 task prioritiser tag. Payload is pipe-separated sub-tasks:
        #   [PRIORITISE: search news for X | search general for Y | browse URL]
        # Each sub-task gets dispatched in parallel via oracle_d. Results are
        # summarised into ONE tool result so this counts as one agentic step
        # instead of N. Out-of-band from the chat token budget by design.
        (r"\[PRIORITISE:\s*(.*?)\]",     "prioritise"),
        # v2.1.5 verify-file tag. Payload is the file path. Calls
        # verify_written_file(path) which checks os.path.exists() and
        # AST-parses .py files. Was previously documented in the prompt
        # but UNWIRED (parser ignored, dispatcher had no handler), causing
        # Sage to hallucinate verification success on missing files.
        (r"\[VERIFY_FILE:\s*(.*?)\]",    "verify_file"),
        # Image generation (ComfyUI). Payload is the image prompt (the visual
        # description). Dispatched in main.py's agentic loop -> comfyui_client.
        # Accept BOTH the colon form [GENERATE_IMAGE: prompt] AND the XML-style
        # open/close form [GENERATE_IMAGE]prompt[/GENERATE_IMAGE] that some models
        # (Granite, Qwen3-VL) emit regardless of the prompt's instruction.
        (r"\[GENERATE_IMAGE\]\s*(.*?)\s*\[/GENERATE_IMAGE\]", "generate_image"),
        (r"\[GENERATE_IMAGE:\s*(.*?)\]", "generate_image"),
        # Expression engine tags (safe math/logic, no Python eval). Payload is
        # the raw expression string; dispatched in main.py's agentic loop via
        # expression_engine. Imported directly so it works with sage plugins off.
        (r"\[LINT_EXPR:\s*(.*?)\]",  "lint_expr"),
        (r"\[PARSE_EXPR:\s*(.*?)\]", "parse_expr"),
    )
    for pattern, action_type in simple_patterns:
        for m in re.finditer(pattern, text, re.DOTALL | re.I):
            if _in_consumed(m.start()):
                continue
            # v2.2 (2026-05-30): pedagogical-context guard. Same rule as
            # the bracket-balanced path above -- tags inside markdown
            # code spans or fenced blocks are examples, not invocations.
            # Skip without consuming so the example stays visible.
            if _is_in_markdown_code_context(text, m.start()):
                print(
                    f"[PARSER] Skipping pedagogical {action_type.upper()} "
                    f"tag at pos {m.start()}: {text[m.start():m.end()]!r}"
                )
                continue
            payload = m.group(1).strip()
            # v2.1.5 defensive: skip prompt-template echoes for query-style
            # tags (SEARCH, WEATHER, BROWSE, etc.) — but NEVER skip the
            # procedural-memory tags (REMEMBER/REMEMBER_FAIL/RECALL) on
            # placeholder grounds, since those are content-data, not
            # action-targets, and false positives there would be confusing.
            if action_type in (
                "search", "search_general", "search_memory",
                "weather", "browse", "web_search_browser",
            ) and _looks_like_placeholder_query(payload):
                print(
                    f"[PARSER] Skipping echoed {action_type.upper()} "
                    f"placeholder: {payload!r}"
                )
                # Still record the range so final-answer cleanup strips it.
                consumed_ranges.append((m.start(), m.end()))
                continue
            actions_with_pos.append((m.start(), action_type, payload))
            # v2.1.5: also track the range so the final-answer cleanup can
            # surgically remove the whole tag (not just [TASK_DONE]).
            consumed_ranges.append((m.start(), m.end()))

    # Sort by position to preserve the original order in model output
    actions_with_pos.sort(key=lambda x: x[0])
    actions = [(a, c) for _, a, c in actions_with_pos]

    # v2.1.5: capture the [TASK_DONE] span(s) too so the cleanup pass
    # can strip them via consumed_ranges instead of a separate regex.
    for m in re.finditer(r"\[TASK_DONE\]", text, re.I):
        consumed_ranges.append((m.start(), m.end()))
    if re.search(r"\[TASK_DONE\]", text, re.I):
        actions.append(("done", ""))

    if return_ranges:
        return actions, consumed_ranges
    return actions


# ===============================================================================
#  EXERCISE TRACKER (plugin)
# ===============================================================================

_exercise_log: List[dict] = []

def log_exercise(date: str, activity: str, duration: int, intensity: float, note: str = "") -> dict:
    entry = {
        # v2.1.6: id from epoch_int * 1000 keeps existing semantics
        # (millisecond-resolution unique key) while routing through
        # the unified time source. ts uses iso_z for consistency
        # with all other on-disk timestamps in the project.
        "id": int(TimeManager.epoch() * 1000),
        "date": date, "activity": activity,
        "duration": duration, "intensity": intensity,
        "note": note, "ts": TimeManager.iso_z(),
    }
    _exercise_log.append(entry)
    return {"success": True, "entry": entry}

def get_exercise_log() -> list:
    return list(_exercise_log)

def get_exercise_stats() -> dict:
    if not _exercise_log:
        return {"total_sessions": 0, "total_minutes": 0, "avg_intensity": 0}
    total_mins = sum(e.get("duration", 0) for e in _exercise_log)
    avg_int = sum(e.get("intensity", 0) for e in _exercise_log) / len(_exercise_log)
    return {
        "total_sessions": len(_exercise_log),
        "total_minutes": total_mins,
        "avg_intensity": round(avg_int, 1),
    }

def clear_exercise_log() -> dict:
    _exercise_log.clear()
    return {"success": True}


# ===============================================================================
#  TAVILY KEY MANAGEMENT
# ===============================================================================

def set_tavily_key(key: str) -> dict:
    global TAVILY_API_KEY
    TAVILY_API_KEY = key.strip()
    try:
        TAVILY_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Encrypt the API key at rest (was clear-text). Read side uses read_file_auto.
        import atrest as _atrest
        TAVILY_KEY_FILE.write_bytes(_atrest.encrypt_bytes(TAVILY_API_KEY.encode("utf-8")))
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_tavily_key_info() -> dict:
    masked = ("tvly-..." + TAVILY_API_KEY[-4:]) if len(TAVILY_API_KEY) > 4 else ""
    return {"has_key": bool(TAVILY_API_KEY), "masked": masked}


def delete_tavily_key() -> dict:
    global TAVILY_API_KEY
    TAVILY_API_KEY = ""
    try:
        if TAVILY_KEY_FILE.exists(): TAVILY_KEY_FILE.unlink()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ===============================================================================
#  COMPLEXITY DETECTOR (plugin-ready)
# ===============================================================================

MODELS_DB = {
    "openhands": {
        "name": "all_hands_openhands_lm_7b_v0_1_Q6_K_L",
        "context": 32000,
        "speed": "fast",
        "accuracy": "good",
        "best_for": ["code_generation", "chat"],
        "vram": 6,       
    },
    "mistral": {
        "name": "mistral:latest",
        "context": 32000,
        "speed": "fast",
        "accuracy": "good",
        "best_for": ["chat", "general"],
        "vram": 5,
    },
    "qwen3_5": {
        "name": "qwen3.5:latest",
        "context": 32000,
        "speed": "fast",
        "accuracy": "good",
        "best_for": ["chat", "general", "reasoning"],
        "vram": 6,
    },
    "glm4_flash": {
        "name": "glm-4.7-flash:latest",
        "context": 32000,
        "speed": "medium",
        "accuracy": "excellent",
        "best_for": ["research", "complex_reasoning", "chat"],
        "vram": 18,
    },
    "qwen2_5_7b": {
        "name": "qwen2.5:7b",
        "context": 32000,
        "speed": "fast",
        "accuracy": "good",
        "best_for": ["chat", "general", "reasoning"],
        "vram": 5,
    },
    "phi3_medium": {
        "name": "phi3:medium",
        "context": 32000,
        "speed": "medium",
        "accuracy": "good",
        "best_for": ["chat", "reasoning", "general"],
        "vram": 8,
    },
    "nomic_embed": {
        "name": "nomic-embed-text:latest",
        "context": 8192,
        "speed": "fast",
        "accuracy": "excellent",
        "best_for": ["embeddings", "semantic_search"],
        "vram": 1,
    },
    "qwen2_5_coder_1b": {
        "name": "qwen2.5-coder:1.5b-base",
        "context": 32000,
        "speed": "fast",
        "accuracy": "good",
        "best_for": ["code_generation"],
        "vram": 1,
    },
    "qwen2_5_coder_32b": {
        "name": "qwen2.5-coder:32b-instruct-q4_K_M",
        "context": 32000,
        "speed": "medium",
        "accuracy": "excellent",
        "best_for": ["code_generation", "logic", "debugging"],
        "vram": 20,
    },
    "nemotron": {
        "name": "nemotron-3-super:120b",
        "context": 256000,
        "speed": "slow",
        "accuracy": "exceptional",
        "best_for": ["complex_reasoning", "research", "code_generation", "chat"],
        "vram": 81,
    },
    "qwen3_coder": {
        "name": "qwen3-coder:30b",
        "context": 32000,
        "speed": "medium",
        "accuracy": "excellent",
        "best_for": ["code_generation", "logic"],
        "vram": 17,
    },
    "qwen3_vl": {
        "name": "qwen3-vl:30b",
        "context": 32000,
        "speed": "medium",
        "accuracy": "excellent",
        "best_for": ["research", "complex_reasoning", "vision"],
        "vram": 18,
    },
    "gemma4_31b": {
        "name": "gemma4:31b",
        "context": 256000,
        "speed": "medium",
        "accuracy": "excellent",
        "best_for": ["chat", "reasoning", "general", "code_generation"],
        "vram": 20,
    },
    "codette": {
        "name": "raiff1982:codette-ultimate-rc-xi-v2",
        "context": 32000,
        "speed": "medium",
        "accuracy": "excellent",
        "best_for": ["code_generation", "debugging", "chat"],
        "vram": 20,
    },
    "dolphin3": {
        "name": "dolphin3:latest",
        "context": 32000,
        "speed": "fast",
        "accuracy": "good",
        "best_for": ["chat", "general", "uncensored"],
        "vram": 5,
    },
    "llama3_1_8b": {
        "name": "llama3.1:8b",
        "context": 32000,
        "speed": "fast",
        "accuracy": "good",
        "best_for": ["chat", "general"],
        "vram": 5,
    },
    "llama3_2_3b": {
        "name": "llama3.2:3b",
        "context": 32000,
        "speed": "fast",
        "accuracy": "moderate",
        "best_for": ["chat", "lightweight"],
        "vram": 2,
    },
    "rnj1": {
        "name": "rnj-1:latest",
        "context": 32000,
        "speed": "fast",
        "accuracy": "good",
        "best_for": ["chat", "general"],
        "vram": 5,
    },
    "llama4_scout": {
        "name": "llama4:scout",
        "context": 128000,
        "speed": "medium",
        "accuracy": "excellent",
        "best_for": ["research", "complex_reasoning", "long_context", "chat"],
        "vram": 63,
    },
}

def analyze_complexity(prompt: str) -> float:
    """Enriched lexical complexity score, 0..1.

    v2.2 (2026-05-30): expanded from 3 to 7 signals to give the router
    something to actually act on for novel models (gemma4, llama3.2:1b,
    etc.) where MODELS_DB metadata is unavailable. All signals are
    cheap pattern matches -- no network calls, no embedding cost. The
    semantic + procedural signals are planned for v2.3 once family-test
    pass concludes; this v2.2 enrichment keeps the routing decision
    surface lean while measurably improving accuracy over the previous
    3-signal version.

    Signals (each contributes 0 to its max bound; composite capped at 1.0):
      1. Length tiers (>1000, >500, >200 chars) -- longer queries
         correlate with complexity demand.
      2. Complex-vocabulary presence (expanded keyword set).
      3. Code/structure tokens (```, def, class, import, etc.).
      4. Multi-step indicators (first/then/next/finally/...).
      5. Clause density (conjunctions per query as proxy for nesting).
      6. URL/path mentions (research/file-handling queries).
      7. Multi-part questions (>= 2 question marks).
    """
    score = 0.15  # baseline
    pl = prompt.lower()

    # 1. Length tiers
    if   len(prompt) > 1000: score += 0.18
    elif len(prompt) > 500:  score += 0.12
    elif len(prompt) > 200:  score += 0.05

    # 2. Complex vocabulary (broader than v1)
    complex_words = [
        "analyze", "compare", "research", "explain why", "debug",
        "optimize", "implement", "design", "evaluate", "synthesize",
        "critique", "trace", "diagnose", "architect", "refactor",
        "summarize across", "reconcile",
    ]
    if any(w in pl for w in complex_words):
        score += 0.18

    # 3. Code / structure tokens
    if re.search(r"```|def |class |import |#include|function\s*\(", prompt):
        score += 0.15

    # 4. Multi-step indicators
    step_words = [
        "first", "then", "next", "after that", "finally", "continue", "concurrently",
        "step by step", "in order", "subsequently", "second,", "third,",
    ]
    step_hits = sum(1 for w in step_words if w in pl)
    if   step_hits >= 2: score += 0.15
    elif step_hits == 1: score += 0.06

    # 5. Clause density (conjunctions per query)
    conjunctions = [
        " and ", " but ", " however ", " although ", " because ",
        " since ", " while ", " whereas ", " if ", " unless ",
    ]
    conj_count = sum(pl.count(c) for c in conjunctions)
    if   conj_count >= 4: score += 0.10
    elif conj_count >= 2: score += 0.04

    # 6. URL / path mentions (research/file-handling territory)
    if re.search(r"https?://|\b[\w-]+\.(com|org|net|io|gov|edu)\b|[A-Za-z]:\\|/[a-z]+/", prompt):
        score += 0.06

    # 7. Multi-part questions
    if prompt.count("?") >= 2:
        score += 0.08

    # 8. Compound multi-topic request: comma-separated list combined
    #    with "and" (e.g., "directions, weather, and travel plans").
    #    Heuristic: 2+ commas in proximity to an " and " conjunction.
    if pl.count(",") >= 2 and " and " in pl:
        score += 0.12

    # 9. Real-time / current-information markers. These imply tool use
    #    (weather, search, news) and almost always benefit from a
    #    larger, more capable model that can coordinate tools well.
    realtime_markers = [
        "weather", "forecast", "news today", "current", "right now",
        "today", "tomorrow", "this week", "next week", "directions",
        "traffic", "schedule", "itinerary", "plan a trip", "travel plan",
    ]
    if sum(1 for w in realtime_markers if w in pl) >= 1:
        score += 0.12

    return min(score, 1.0)

def detect_query_type(prompt: str) -> str:
    """Bucket a query into one of: code_generation, research, chat.

    v2.2 (2026-05-30): expanded "research" bucket to recognize queries
    that imply tool use (directions, weather, travel planning,
    itineraries, multi-step real-world tasks) even when they do not
    use the literal word "research". These queries benefit from a
    more capable model just like a literal research request would.
    """
    pl = prompt.lower()
    if any(w in pl for w in [
        "code", "function", "debug", "program", "script",
        "refactor", "implement", "stack trace", "compile",
    ]):
        return "code_generation"
    if any(w in pl for w in [
        "research", "analyze", "compare", "explain",
        "directions", "weather", "forecast", "travel plan",
        "itinerary", "plan a trip", "recommend", "suggestions for",
        "schedule", "summarize", "synthesize", "find me",
    ]):
        return "research"
    return "chat"

def _estimate_model_size_b(name: str) -> float | None:
    """Estimate a model's billion-parameter count from its name.

    Strategy in priority order:
      1) Ollama-style tag suffix :Nb / :N.Nb -- ``llama3.2:1b`` -> 1.0,
         ``qwen2.5:7b`` -> 7.0, ``mistral:7b-instruct`` -> 7.0,
         ``gemma3:27b`` -> 27.0. Most modern Ollama pulls follow this.
      2) Trailing token Nb after a separator -- handles GGUF filenames
         like ``llama-3.2-1b-instruct.gguf`` -> 1.0.
      3) MODELS_DB ``vram`` field as a rough proxy -- vram in GB roughly
         tracks B-param count for current Q4-Q6 quantizations.
      4) None -- caller treats as unknown size (eligible but un-weighted).

    Returns float B-params or None. Never raises.
    """
    if not name:
        return None
    nl = name.lower()
    # Pattern 1: :Nb tag
    m = re.search(r":(\d+(?:\.\d+)?)\s*b\b", nl)
    if m:
        try: return float(m.group(1))
        except ValueError: pass
    # Pattern 2: Nb after separator (handles gguf filenames)
    m = re.search(r"[-_:](\d+(?:\.\d+)?)b\b", nl)
    if m:
        try: return float(m.group(1))
        except ValueError: pass
    # Pattern 3: MODELS_DB proxy
    meta = next((mm for mm in MODELS_DB.values() if mm.get("name") == name), None)
    if meta:
        v = meta.get("vram")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


_CAP_OVERRIDES: dict = {}   # model_id -> {"vision": bool, "language": bool}

_VISION_NAME_HINTS = (
    "llava", "bakllava", "moondream", "minicpm-v", "pixtral", "cogvlm",
    "internvl", "vision", "-vl", "vl:", "_vl", "qwen-vl", "qwen2-vl",
    "qwen2.5-vl", "qwen3-vl",
)


def set_model_capability(model_id, vision=None, language=None):
    """Override the detected capability for a model (e.g. from runtime /api/show
    metadata, or a per-slot setting). Additive over the name heuristic."""
    cur = dict(_CAP_OVERRIDES.get(model_id, {}))
    if vision is not None:
        cur["vision"] = bool(vision)
    if language is not None:
        cur["language"] = bool(language)
    _CAP_OVERRIDES[model_id] = cur


def model_capabilities(model_id) -> dict:
    """Best-effort {vision, language} for a model id: a name heuristic plus any
    override. NEVER raises. language defaults True (nearly all chat models do
    text; a pure vision encoder can be flagged False via set_model_capability)."""
    mid = (model_id or "").lower()
    ov = _CAP_OVERRIDES.get(model_id) or _CAP_OVERRIDES.get(mid) or {}
    vision = ov.get("vision")
    if vision is None:
        vision = any(h in mid for h in _VISION_NAME_HINTS)
    return {"vision": bool(vision), "language": bool(ov.get("language", True))}


def route_query(prompt: str, candidates: "list[str] | None" = None,
                available_vram: int = 12,
                needs_vision: bool = False) -> str:
    """Pick the best model name for this prompt.

    v2.2 (2026-05-30) -- the routing decision is now driven by:
      1. Enriched lexical complexity score (analyze_complexity)
      2. Categorical query type (detect_query_type)
      3. Name-aware candidate sizing (_estimate_model_size_b)

    The decision rule:
      - HEAVY query (complexity >= 0.5 OR type in research/code_generation):
            prefer the LARGEST known-size candidate. If all known sizes
            are small (<7B) and an unknown-size candidate exists, prefer
            the unknown -- it MIGHT be bigger, and we'd rather try it
            than send a complex query to a verified-too-small model.
      - LIGHT query: prefer the SMALLEST known-size candidate. Unknown
            sizes are ignored here -- don't gamble on possibly-huge
            unknowns when a known-small fits the bill.
      - ALL UNKNOWN sizes: fall back to primary (first candidate). At
            least the user's deliberate slot ordering is honored.
      - Single candidate or empty pool: degenerate cases handled first.

    Backwards compat: ``candidates=None`` falls through to the legacy
    MODELS_DB universe scan. Direct callers from the WebSocket auto-
    route path pass candidates explicitly.

    Returns one of the candidate names, or "" if candidates is empty.
    Never raises -- routing must never break chat.
    """
    # Resolve candidate pool.
    if candidates is not None:
        if not candidates:
            return ""
        pool = list(candidates)
    else:
        pool = [m["name"] for m in MODELS_DB.values()]
        if not pool:
            return ""

    # Capability-aware narrowing (v2.7.2): pick by what each slot can DO before
    # picking by size. An image-bearing turn must go to a VISION-capable slot; a
    # text turn prefers a LANGUAGE-capable, pure-text slot (so a vision model is
    # never handed plain text). NEVER empties the pool - best-effort fall back so
    # routing can't break chat.
    try:
        if needs_vision:
            vpool = [c for c in pool if model_capabilities(c).get("vision")]
            pool = vpool or pool
        else:
            langp = [c for c in pool if model_capabilities(c).get("language", True)]
            pool = langp or pool
            puretext = [c for c in pool if not model_capabilities(c).get("vision")]
            if puretext:
                pool = puretext
    except Exception:
        pass

    if len(pool) == 1:
        return pool[0]

    complexity = analyze_complexity(prompt)
    qtype      = detect_query_type(prompt)
    is_heavy   = complexity >= 0.5 or qtype in ("research", "code_generation")

    sized   = [(c, s) for c in pool if (s := _estimate_model_size_b(c)) is not None]
    unsized = [c for c in pool if _estimate_model_size_b(c) is None]

    if not sized:
        # All sizes unknown -- can't route by size. Honor primary.
        return pool[0]

    if is_heavy:
        max_known_b = max(s for _, s in sized)
        if unsized and max_known_b < 7:
            # Our known options are small; the unknown MIGHT be bigger.
            return unsized[0]
        return max(sized, key=lambda x: x[1])[0]
    else:
        # Light query: prefer smallest KNOWN. Skip unknowns -- we don't
        # want to gamble on routing chat to a possibly-huge unknown.
        return min(sized, key=lambda x: x[1])[0]



# ===============================================================================
#  HEALTH CHECK
# ===============================================================================

def check_ollama_health(ollama_url: str = "http://localhost:11434") -> dict:
    try:
        import requests
        r = requests.get(f"{ollama_url}/api/tags", timeout=5)
        if r.status_code == 200:
            models = r.json().get("models", [])
            if models:
                return {"status": "online", "models": [m.get("name") for m in models]}
            return {"status": "warning", "message": "No models installed"}
        return {"status": "offline"}
    except Exception:
        return {"status": "offline"}



# ===============================================================================
#  IPC BRIDGE TOOL DISPATCH — v2.1.2 surgical addition
# ===============================================================================
# Exposes a single clean entry point for browse / web_search tool calls that
# can be invoked from anywhere (WebSocket handler, /api/launch-browser flows,
# unit tests, future agentic callers). Reuses the existing browse_url() and
# web_search_browser() helpers so all current feature-flag and plugin-toggle
# logic (FEATURES_ENABLED["browser"]) is preserved. The IPC mirroring to the
# visible privacy browser is driven from inside browser_tool.py itself, so
# this dispatcher does not need to know about ipc_bridge directly — it just
# calls the existing helpers and the mirror fires automatically.

def handle_tool_call(tool_name: str, **kwargs) -> str:
    """
    Dispatch a tool call from the agent by name.

    Supported tools:
      - "browse":     kwargs={"url": str, "max_chars": int (optional)}
      - "web_search": kwargs={"query": str, "num_results": int (optional)}
      - "verify_file": kwargs={"path": str}

    Returns a formatted string result on success, or an "[ERROR] ..." string
    on failure. Never raises — agent loops can splice the result straight
    into a tool-results block.
    """
    if tool_name == "browse":
        url = kwargs.get("url")
        if not url:
            return "[ERROR] browse tool requires a 'url' argument."
        max_chars = kwargs.get("max_chars", 2000)
        try:
            return browse_url(url, max_chars=max_chars)
        except Exception as e:
            return f"[ERROR] browse dispatch failed: {e}"

    if tool_name == "web_search":
        query = kwargs.get("query")
        if not query:
            return "[ERROR] web_search tool requires a 'query' argument."
        num_results = kwargs.get("num_results", 5)
        try:
            return web_search_browser(query, num_results=num_results)
        except Exception as e:
            return f"[ERROR] web_search dispatch failed: {e}"
    
    if tool_name == "verify_file":
        path = kwargs.get("path")
        if not path:
            return "[ERROR] verify_file tool requires a 'path' argument."
        try:
            return verify_written_file(path)
        except Exception as e:
            return f"[ERROR] verify_file dispatch failed: {e}"
            
    return f"[ERROR] Unknown tool: {tool_name}"
