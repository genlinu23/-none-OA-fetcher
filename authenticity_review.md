# CO2RR Catalysis Crossref Authenticity Review

## Conclusion
The old `strict_v3_primary_clean` set is thematically much cleaner than before, but it is not a pure paper corpus. After authenticity and record-type review, the trustworthy first-layer core is `2013` DOI records, down from `2674`.

## Input
- `C:\Users\logan\doi_harvest\outputs\co2rr_catalysis_crossref_strict_v3_primary_clean.csv`

## Main Output
- `C:\Users\logan\doi_harvest\outputs\co2rr_catalysis_crossref_strict_v3_authenticity_audited.csv`
- `C:\Users\logan\doi_harvest\outputs\co2rr_catalysis_crossref_strict_v3_authenticity_summary.json`

## Bucket Counts
- `core_journal_article`: `2013`
- `residual_review_like`: `41`
- `journal_commentary_editorial`: `4`
- `cover_teaser`: `23`
- `peer_review_material`: `71`
- `chemrxiv_preprint`: `31`
- `ssrn_record`: `152`
- `posted_content_other`: `121`
- `book_or_chapter`: `119`
- `proceedings`: `88`
- `report`: `9`
- `dataset`: `2`

## Key Findings
- Crossref `journal-article` alone is not enough to trust a record as a core paper.
- Some SSRN records still come back from Crossref as `journal-article`, so DOI prefix had to be used as a second trust signal.
- `172` rows had HTML-like title markup and now have cleaned `display_title` values.
- `23` cover / teaser records were found; `18` of them matched likely core-paper DOIs after title stripping, so many were duplicate publisher highlights rather than distinct papers.

## Spot Checks
- `10.1002/cssc.201800083` -> real journal article
- `10.1002/cjoc.201700761` -> real journal article
- `10.26434/chemrxiv.6205322.v1` -> posted content
- `10.1002/cjce.24816/v1/decision1` -> peer review material
- `10.1039/9781782623809-00063` -> book chapter
- `10.1002/cssc.201800959` -> cover feature record

## Recommendation
Use `core_journal_articles.csv` as the current first-layer DOI library for catalysis-focused CO2 reduction work. Keep the other buckets separate and only merge back records after topic-specific or source-specific review.
