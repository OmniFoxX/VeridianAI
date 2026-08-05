"""
branding.py — single source of truth for every user-facing name.

v2.12.0 (2026-07-06): OracleAI -> VeridianAI, Sage -> Toga, and the
publisher is MentiSphere Software. Renamed for trademark safety before
launch (Oracle Corp. markets "Oracle AI"; Sage Group is a major software
mark). New names USPTO-checked by Todd 2026-07-06: 0 live hits.

RULES OF THE ROAD:
  * Anything a USER or STORE REVIEWER sees comes from here: window
    titles, UI labels, prompts/persona, dialogs, legal docs, store
    metadata, MCP server name, socials prefixes, wake word default.
  * INTERNAL identifiers (module names like sage_engine.py, config keys
    like sage_mode, env vars like ORACLE_APP_PORT, the sage_data folder)
    deliberately KEEP their old names — they are not marketplace use of
    a mark, and renaming them breaks upgrades and tester installs for
    zero legal benefit. That's Phase 2, if ever.
  * If the names must change again: change THIS file, then sweep static
    HTML/docs for the old strings (grep the previous values below).
"""

PRODUCT_NAME    = "VeridianAI"
PRODUCT_SPACED  = "V E R I D I A N  A I"      # splash / banner styling
ASSISTANT_NAME  = "Toga"                       # the AI persona
PUBLISHER       = "MentiSphere Software"
WAKE_WORD_DEFAULT = "Toga"                     # voice + socials wake word
MCP_SERVER_NAME = "veridianai-toga"            # shown in MCP client configs

# Display names for the inference tiers. INTERNAL tier labels/keys stay
# "Oracle"/"Sage"/"Daemon"/"NPU" (routing, logs, config) — these are what
# the UI renders instead. Functional, so they never need a rebrand.
TIER_DISPLAY = {
    "Oracle": "Reasoning",
    "Sage":   "Agent",
    "Daemon": "Utility",
    "NPU":    "NPU",
}

# Previous names, kept for grep-ability and migration notes:
_LEGACY = {"product": "OracleAI", "assistant": "Sage",
           "publisher": "Electrum Consiliarius"}
