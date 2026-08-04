# Selection ROI image matching

The selection recognizer is image-based, not full-frame text OCR.

1. Accept only the canonical 1280x720 UGREEN frame.
2. Crop the six configured opponent slots.
3. Save a distinct six-slot observation under `data/ocr/selection/captures`.
4. Compare each crop with human-confirmed images under
   `data/ocr/selection/reference/labeled/<pokemon-name>`.
5. Resolve the six displayed candidates as one unique-team assignment.
6. Never change the opponent inputs automatically.
7. Only after the existing `6体を確認` command succeeds may the six current crops
   be copied into the labeled reference set and recorded in feedback JSONL.

The historical ROI config and images are copied from `C:\pokemon_ai` to the
repository-local `C:\work\maple-next\data\ocr` tree by a separate local file
operation. Production code has no dependency on the old path.
