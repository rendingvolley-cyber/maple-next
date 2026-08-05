# Selection ROI assisted input and learning

The Selection recognizer is image-based, not full-frame text OCR.

## Capture and matching

1. Accept only the canonical 1280x720 UGREEN frame.
2. Crop the six configured opponent slots.
3. Save one distinct six-slot observation under `data/ocr/selection/captures`.
4. Compare each crop with trusted images under
   `data/ocr/selection/reference/labeled/<pokemon-name>`.
5. Resolve the six slots as one unique-team assignment.
6. Run latest-only at no more than 2 fps; never queue a matching backlog.

Historical label-directory variants are normalized on read. For example,
`イダイトウ (オス)` and `イダイトウ(オス)` contribute to one visible matcher
label without renaming or deleting either source directory.

## Editable Selection UI

- assigned score `>= 0.80`: fill an empty slot once as `ocr_auto`
- candidate score `>= 0.60`: show as one of up to three clickable chips
- candidate click: set `candidate_click` and lock the slot
- direct typing: set `manual_text` and lock the slot
- a locked slot is never overwritten by a later matcher result
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
4. record origin-aware ROI feedback for the same Selection identity

Failure to create the canonical snapshot yields provider send 0. The matcher,
OCR timer, and feedback storage never trigger a provider request. No Selection
Advice is applied to the game automatically.

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
