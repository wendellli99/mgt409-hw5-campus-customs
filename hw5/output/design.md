# Campus Customs operations desk

The desk uses warm paper, navy ink, and muted green, ochre, clay, and lilac to feel like a small New Haven shop with a capable team behind the counter. Georgia headlines add a collegiate editorial touch; system sans-serif text keeps tasks and numbers easy to read. A very light paper texture and the CC monogram give the page character without relying on downloaded fonts or illustrations.

## Layout and hierarchy

The top of the page introduces the desk and shows three useful facts: the checking balance, resolved tickets, and payments waiting for the human. The darker checking card makes the financial result easy to find. It shows the starting balance, current balance, and approved outflow together so a prepared payment cannot be mistaken for a cash movement.

The inbox sits on the left on wide screens. Each ticket shows its number, subject, description, category, and status. Selecting it places the full request in the main workspace alongside the Start the team action. The requester is included when the backend provides it. A restrained green border marks the selected ticket, and explicit text marks open, working, approval, failed, and resolved states.

On smaller screens the financial overview stacks into two columns, the inbox moves above the workspace, and agent cards rearrange. At narrow phone widths tickets become full-width rows and the start action fills its row. Every important control stays available.

## The five agents

All five agents have their own card. Boss uses navy and a crown, Inventory green and a package, Accounting ochre and a dollar symbol, Facilities clay and a wrench, and Customer Service lilac and a headset. Each has a plain-language role, its actual participation state, a short excerpt of its real recorded contribution or latest message (with a full-contribution expander for longer reports), and badges for the MCP tools it actually used. Idle specialists stay on standby, which makes selective delegation visible.

A card highlights when its agent is the most recent participant during a running ticket. The paper trail below gives the fuller explanation: agent messages, delegations, tool calls, human actions, and expandable tool inputs and results. A filter switches between the selected request and all requests in the current desk session. The page polls the backend once each second; it does not invent agent messages or progress.

## Human approvals and cash

A pending payment appears directly below the selected request in a warm ochre panel. The person sees its purpose, payee when provided, amount, approving name, and an explicit Approve payment button. The panel explains that approval changes the checking balance. The button calls the backend's human approval route; only after the backend commits the approval does the team continue. A confirmation notice makes that transition clear.

A resolved ticket gains a green status badge, a completed action button, and its recorded outcome. The inbox retains all three requests, so completion is visible without making earlier work disappear. Cash stays at the top throughout the session and refreshes from the database-backed API.

## Interaction details

Controls give immediate pressed feedback and clear keyboard focus. Reset confirmation supports Escape, contains keyboard focus while open, and restores focus when dismissed. Loading starts with quiet skeletons, connection failures show an actionable inline error, and an empty paper trail explains what will appear after a run. Buttons prevent duplicate starts and approvals while a request is being submitted. The page offers a fresh-desk reset with a confirmation because it restores the original working database and clears current payment state. Earlier evidence remains in the append-only audit file, while the activity view begins after the latest reset.

The small connection indicator, short outcome notes, and emphasis on a human's final decision keep the interface calm and predictable. Motion is limited to loading, a subtle live indicator, and small state transitions. Reduced-motion preferences disable those animations; stronger contrast and reduced-transparency preferences also have explicit fallbacks. Status is always conveyed in words as well as color.
