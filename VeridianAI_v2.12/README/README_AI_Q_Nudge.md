Enablement: add the two lines, save, restart VeridianAI. On boot you'll see [AIQ_NUDGE] Initialised... and [AIQ_NUDGE] enabled in config: True in the backend console.
On usage — this is a one-way nudge channel, not a chat:
The helper script sends ONE nudge per invocation. It signs the text, drops a file in sage_data/nudges/, and exits. There's no persistent session in the terminal.
The flow is:

You start a long run in VeridianAI's normal chat UI (e.g., "do the WCAG audit")
Toga starts working
Mid-run, in a separate terminal window, you run: python Drive:\path\to\file\aiq_nudge_send.py "message" Then hit enter to send. Example:

*open terminal* python E:\VeridianAI_v2.2\tools\aiq_nudge_send.py "skip the lint pass, do the structural audit first"

The script writes the signed file and exits — back to your prompt
Within ~1 agentic step, Toga picks it up. The VeridianAI chat UI shows the green confirmation banner with the preview. Her next reasoning incorporates your guidance.
Toga's response continues appearing in the VeridianAI chat UI, not the terminal
Need to send another nudge later? Run the helper again with new text — each call is independent

You can run the helper from anywhere — cd into tools/ if you want, or just call it with the full path from wherever. Tab-completion helps. If you want quicker invocation, you could even make a desktop shortcut like:
C:\path\to\python.exe E:\VeridianAI_v2.2\tools\aiq_nudge_send.py
and pipe text into it.
Tip for the WCAG audit specifically: you can also pipe a file in. If you have prep notes ready:
type wcag_guidance.txt | python E:\VeridianAI_v2\tools\aiq_nudge_send.py -
That sends the entire file as one nudge — useful for longer mid-course corrections.
So: terminal = your "outbox" to Toga. Chat UI = Toga's continued response stream. They're decoupled, which is what makes it non-disruptive during long runs