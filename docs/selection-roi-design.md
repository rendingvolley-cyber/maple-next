# Selection ROI assisted input and learning

The Selection recognizer is image-based, not full-frame text OCR.

## NEW MATCH screenshot boundary

Selection ROI matching is event-driven rather than continuously sampled.

1. The human presses `NEW MATCH`.
2. Before the canonical new-match command runs, Maple copies the freshest available
   canonical 1280x720 UGREEN frame into an immutable in-memory screenshot.
3. The canonical new-match command creates the new Selection identity.
4. Only when that command succeeds and the new state is `SELECTION_OPEN` is the
   frozen screenshot bound to the new session, match, and generation.
5. The six configured opponent ROIs are cropped from that one screenshot.
6. The six crops are matched once and rendered as assisted input.

There is no Selection ROI polling timer, repeated recapture, or background stream
of new Selection candidates. Later live frames cannot change the six crops,
candidates, auto-filled values, or their training provenance for that match.

If no fresh canonical frame exists at the instant `NEW MATCH` is pressed, the new
match still opens safely and the operator enters the opponent six manually. Maple
does not keep polling for a later frame.

## Capture and matching

1. Accept only the frozen canonical 1280x720 NEW MATCH screenshot.
2. Crop the six configured opponent slots in top-to-bottom order.
3. Save one distinct six-slot observation under `data/ocr/selection/captures`.
4. Compare each crop with trusted images under
   `data/ocr/selection/reference/labeled/<pokemon-name>`.
5. Resolve the six slots as one unique-team assignment.

Historical label-directory variants are normalized on read. For example,
`イダイトウ (オス)` and `イダイトウ(オス)` contribute to one visible matcher
label without renaming or deleting either source directory.

## Editable Selection UI

- assigned score `>= 0.80`: fill an empty slot once as `ocr_auto`
- candidate score `>= 0.60`: show as one of up to three clickable chips
- candidate click: set `candidate_click` and lock the slot
- direct typing: set `manual_text` and lock the slot
- a locked slot is never overwritten by any later operation
- a new match identity starts with empty opponent fields unless canonical values
  already exist for that identity

The matcher remains assistance rather than authority. The operator can keep an
auto-filled value, choose a chip, or type a different name.

## Canonical and Gemini boundary

The supported action is the trusted OS-input button
`現在の6体でGeminiに送る`.

That action performs this order:

1. validate six non-empty unique editable names
2. create the existing immutable canonical Selection snapshot
3. reuse the existing explicit Gemini Selection send path
4. record origin-aware ROI feedback for the same Selection identity and frozen
   NEW MATCH screenshot

Failure to create the canonical snapshot yields provider send 0. Screenshot
capture, ROI matching, and feedback storage never trigger a provider request. No
Selection Advice is applied to the game automatically.

## Feedback and learning

Trusted feedback:

```text
candidate_click
manual_text
```

Provisional feedback:

```text
ocr_auto
restored
```

Before storage, the service checks normalized-pixel exact identity and perceptual
near-duplicate similarity across trusted and provisional roots. A matching image
under another label is copied to quarantine and is not learned.

Provisional promotion requires all of the following:

- at least three distinct match IDs with non-conflicting provisional evidence
- an existing trusted example for the same normalized label
- same-label similarity at least `0.85`
- margin over the strongest other trusted label at least `0.05`
- no trusted cross-label exact or near conflict

Promotion is append-only. Source captures, historical assets, and provisional
files are not deleted.

## Local storage boundary

Historical ROI config and images are copied from `C:\pokemon_ai` to
`C:\work\maple-next\data\ocr` by a separate verified local operation. Production
code has no dependency on the old path, and runtime assets do not use AppData.
