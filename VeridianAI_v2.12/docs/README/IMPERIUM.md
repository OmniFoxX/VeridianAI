What IMPERIUM is:

IMPERIUM is VeridianAI's goal integrity subsystem. Its job is to make sure that when an AI agent is pursuing a goal, it cannot bend or break the rules to get there faster — even under pressure.

Think of it like this: imagine an agent whose job is to maximize a score. Without IMPERIUM, under enough pressure it might "discover" that disabling its own safety checks makes the score go up faster. IMPERIUM makes that structurally impossible rather than just against the rules.

How it works — the three layers in plain English:

Layer 1 — The Rulebook (Specification) A set of formal, locked, mathematical rules that define what is and isn't allowed. Once locked, nobody — not even the agent — can add or change the rules. Right now there are two:

No --no-sandbox bypass allowed
Reward score can never go negative
Layer 2 — The Gate (Enforcement) Every single action an agent wants to take has to pass through this gate first. The gate checks the action against the rulebook before letting it happen. If it fails, the action is blocked and the violation is written into a tamper-evident log that can't be quietly altered. No bypass flag exists — you can't sneak around the gate.

Layer 3 — The Watcher (Observer) A completely separate process — its own memory space, its own thread — that watches the stream of everything the gate lets through. It's looking for patterns of suspicious behavior, not just individual violations. If it sees three violations in a five second window it raises an alert through the overseer, same way any other VeridianAI alert surfaces in the UI.

How they work together:

Agent wants to do something
         ↓
   Layer 2 Gate checks it against Layer 1 Rulebook
         ↓
   Allowed? → Action happens + logged to hash-chain
   Blocked? → PermissionError + logged to hash-chain
         ↓
   Layer 3 Observer watching the log stream independently
         ↓
   Pattern detected? → Overseer alert in UI
The key insight that makes it work:

The gate and the rulebook aren't two separate things bolted together — the rulebook is the gate. You can't get a result from the gate without passing the rulebook check. There's no gap between them to exploit.

That's what the whole Symposium was really arguing about. The --no-sandbox bypass existed because the rule and the enforcement were two separate things. IMPERIUM makes them one thing.

Where it lives in VeridianAI right now:

backend/imperium.py
Wired into customs_daemon.inspect() — every dispatch path funnels through there
Running in observe-only mode for now — it watches and logs but doesn't block yet
Violations surface through the overseer notification system
Every audit entry mirrors into Toga's existing hash-chain log with role="imperium"
Four config knobs in config.json: imperium_enabled, imperium_enforce, window, threshold

A MentiSphere Software Solution - Todd and AI Collaborative Work.