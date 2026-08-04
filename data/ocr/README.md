# Maple Next selection ROI data

This directory is the only supported storage root for selection ROI assets in the
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

The application writes only candidate/feedback assets under:

```text
selection/captures/
selection/feedback/
selection/manifests/
selection/quarantine/
```

`roi_config.json` and all runtime images/manifests are local data and are ignored
by Git. Copy the validated historical ROI config and labeled images from
`C:\pokemon_ai` into this tree. Maple Next must not retain a runtime dependency
on the old path.

OCR/ROI candidates never update canonical Selection facts automatically. A human
must use the existing input controls and successfully press `6体を確認`.
