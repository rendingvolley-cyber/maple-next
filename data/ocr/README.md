# Maple Next Selection ROI data

This directory is the only supported storage root for Selection ROI assets in the
official local checkout.

Runtime root:

```text
C:\work\maple-next\data\ocr
```

The application reads:

```text
selection/config/roi_config.json
selection/reference/labeled/<pokemon-name>/*.png
```

The application writes local runtime data under:

```text
selection/reference/provisional/
selection/captures/
selection/feedback/
selection/manifests/
selection/quarantine/
```

`roi_config.json` and runtime images/manifests are ignored by Git. Historical ROI
config and labeled images are copied from `C:\pokemon_ai` into this tree by a
separate verified local operation. Maple Next has no runtime dependency on the
old path and never deletes the source assets.

## Screenshot and operator flow

- pressing `NEW MATCH` copies the freshest available canonical 1280x720 UGREEN
  frame into one immutable screenshot before the canonical new-match command
- only after the new Selection identity is created successfully are the six ROIs
  cropped and matched from that screenshot
- Selection ROI has no polling timer and does not consume later live frames
- when no fresh screenshot is available, the match still opens and manual entry
  remains available; Maple does not keep retrying automatically
- a unique-assignment score of at least `0.80` may fill an empty opponent slot once
- scores of at least `0.60` appear as clickable candidate chips
- candidate clicks and direct typing lock that slot against later changes
- the operator may correct any value before pressing `現在の6体でGeminiに送る`
- that trusted action saves the current six as the canonical Selection snapshot,
  then reuses the existing explicit Gemini send path
- screenshot capture, matching, and feedback never send to Gemini automatically

## Learning boundary

- candidate click or direct typing is stored as trusted feedback
- untouched OCR auto-fill is stored as provisional feedback
- exact and perceptual duplicates are suppressed
- cross-label conflicts are quarantined
- provisional promotion requires evidence from three distinct matches plus an
  existing trusted-label similarity and margin gate

Historical label-folder spacing variants are normalized by the matcher without
renaming or deleting the source directories.
