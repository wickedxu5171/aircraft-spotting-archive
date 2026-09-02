# ADS-B linkage comparison — corrected 50-event sample

Updated: 2026-08-27

## Dataset and selection

The corrected `航空` worksheet contains 57 valid spotting events across 56 aircraft. The website uses the first 50 events in reproducible workbook order, representing 49 aircraft; the final seven events remain unchanged as reserve data.

The controlled sample contains:

- 46 events on 2025-11-05;
- 4 events on 2026-05-16;
- 50 London Heathrow observations;
- 14 British Airways, 16 Virgin Atlantic, 9 American Airlines, 8 United Airlines, 2 Delta and 1 Singapore Airlines events.

The workbook's corrected aircraft type is authoritative for display. External metadata is used for ICAO24, generic ICAO type codes and conflict evidence, not to overwrite the workbook. The source workbook was not edited.

## Current linkage results

The same downloaded ADSB.lol daily archives were imported against the corrected sample. All 49 aircraft have ICAO24 mappings, and every one of the 50 observations has at least one candidate.

| Measure | 2025-11-05 | 2026-05-16 | Total |
| --- | ---: | ---: | ---: |
| Spotting events | 46 | 4 | 50 |
| Unique aircraft | 46 | 4 | 49 |
| Events with at least one candidate | 46 | 4 | 50 |
| Candidate coverage | 100.0% | 100.0% | 100.0% |
| Candidate MatchResult rows | 117 | 13 | 130 |
| Top candidate matched (>=80) | 33 | 3 | 36 |
| Top candidate review (60–79.99) | 13 | 1 | 14 |
| Top candidate below 60 | 0 | 0 | 0 |
| Mean top-candidate score | 83.62 | 82.50 | — |
| Top candidates with route evidence | 6 | 0 | 6 |

Candidate coverage is availability, not correctness. The top-status distribution shows that 34% of observations still require review even though coverage is complete.

The update uses the primary photo capture timestamp only when its date matches the observation date. After the user confirmed that 11-05 belongs to 2025 and 05-16 belongs to 2026, eight timestamps were corrected from 2026-11-05 to 2025-11-05. Five later uploads bring the photo total to 30. G-VJAM's upload-time value `2026-11-25 14:24` was explicitly confirmed as a form-entry error and corrected to `2025-11-05 14:24`; all 30 photo records now meet the date guard. The new uploads and this data correction have not triggered a rematch, so the frozen 36/14 distribution and n=25 metrics remain unchanged.

The narrow-body examples show why date-only matching remains ambiguous. G-EUPJ produces six same-day candidates; its valid 12:48 photo time places #368/BAW390 inside the flight window and raises the top score from 82.5 to 97.5. G-NEOU produces eight candidates; its 12:34 photo time puts #359/SHT8Y within two minutes of the closest boundary, raising the score from 67.5 to 79.5, but the operational callsign still disagrees with BA1446 and the record correctly remains `review`.

G-VPRD demonstrates a second ambiguity mechanism that is not explained by narrow-body operating frequency. The same wide-body airframe produced three ADS-B candidates with different operational callsigns: #555/VIR92MC, #556/VIR103M and #557/VIR104L. Each receives the same 40 registration points. After the photo date correction, the independently reviewed 12:08 capture time gives #556 25 time points and an 82.5 total, while #555 receives 4 time points and 59 overall and #557 receives no time points and 40 overall. The example shows both sides of ADS-B enrichment: registration-only blocking exposes competing legs, while valid human-reviewed photo metadata supplies the decisive evidence.

The clean MariaDB validation import produced 130 ADS-B flights and 17,017 sampled track points for the same controlled sample. These are the reproducible dissertation counts. The working SQLite database retains older source-day rows, so its total ADS-B table counts are intentionally larger.

## Ground-truth metrics (25-row evaluation set)

The database contains 25 persisted manual decisions, meeting the planned 20–30-row quantity target. All 25 verified flights remain the highest-ranked candidate, giving 25/25 correct-top ranking within this reviewed subset. The 80-point automatic-acceptance threshold accepts 19 and leaves six correct top candidates in `review`:

- TP 19, FP 0, FN 6, TN 0;
- Precision 1.000;
- Recall 0.760;
- F1 0.864.

The result distinguishes ranking from threshold acceptance. The matcher orders all 25 reviewed cases correctly, but automatically accepts 76% of them. Precision 1.000 must not be described as universal accuracy: the set contains no verified negative/no-match case and all 25 rows come from 2025-11-05. All 25 Notes fields are now populated and 13 verified observations have linked photos. The Notes use six recurring evidence phrasings; some third-party sources are not named and five `manual_review` rows use capture-time/third-party-program wording. The sample size is sufficient for the planned controlled evaluation, but the date, class, method-label and evidence-source limitations remain.

Representative verified cases include:

| Registration | Workbook flight | Expected ADS-B flight | Top score | Interpretation |
| --- | --- | --- | ---: | --- |
| G-VOOH | VS358 | #354 / VIR358 | 97.5 | exact photo time and callsign support |
| G-VBZZ | VS19 | #342 / VIR19Z | 85.0 | exact time and route evidence; operational callsign variant |
| G-VPRD | VS103 | #556 / VIR103M | 82.5 | corrected photo time resolves three registration-linked callsigns |
| N223UA | UA4 | #357 / UAL4 | 82.5 | accepted correct match |
| 9V-SKN | SQ317 | #543 / SIA317 | 82.5 | corrected photo time moves the correct top above threshold |

## Ablation on the 25-row evaluation set

Ablation uses the same 25 verified rows and fixed candidate sets. After removing one component, remaining scores are normalised by their theoretical maximum, candidates are re-ranked and the 80-point threshold is reapplied.

| Variant | Mean expected-candidate score | Correct top | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline weighted-v1 | 82.200 | 25/25 | 1.000 | 0.760 | 0.864 |
| Without time | 88.267 | 25/25 | 1.000 | 0.600 | 0.750 |
| Without callsign | 86.824 | 25/25 | 1.000 | 0.480 | 0.649 |
| Without route | 83.579 | 25/25 | 1.000 | 0.760 | 0.864 |

All four variants preserve the correct top candidate on this subset, so the ablation evidence concerns automatic acceptance rather than ranking. Removing time reduces accepted correct matches from 19 to 15; removing callsign reduces them to 12; removing route leaves 19. Callsign remains the strongest contributor to crossing the current acceptance threshold in this evaluated subset. This does not prove universal feature importance because the reviewed set has no negative cases and covers only one collection date.

## Interpretation

The corrected workbook and targeted re-import provide 100% candidate availability. The remaining challenge is disambiguation, not blocking: 14 of 50 frozen top candidates remain in the review range. All 30 current photo records pass the date guard. Missing capture time on records without photos, multiple same-airframe legs, operational callsign differences and limited route endpoints remain the main explainable uncertainties.

The present results support four bounded claims:

1. the compact corrected workbook can be transformed reproducibly into a 50-event controlled sample;
2. the current ADS-B archive and ICAO24 mappings produce at least one candidate for every controlled observation;
3. all 25 manually verified matches are ranked first within the reviewed subset;
4. metrics and ablation run reproducibly against stored ground truth, while the 80-point threshold accepts 19 of those 25 correct top candidates.

They do not support a general claim that the linkage is 100% accurate.

## Remaining evaluation safeguards

The numerical sample target, Notes completion and photo-date audit are complete. Before submission, audit repeated Notes wording, name third-party evidence sources where possible, align each verification method with its Notes, and then archive the final CSV/JSON export. No additional ground truth is planned, so every reported Precision, Recall and F1 value must retain `n=25` and the one-date/no-negative limitation. A `Verified: no candidate matches` case should be added only if independent evidence genuinely supports it.
